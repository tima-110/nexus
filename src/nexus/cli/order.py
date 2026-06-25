"""Order commands: buy, sell, close, cancel, status, list."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import typer

from nexus.audit import log_event
from nexus.broker import AlpacaBroker
from nexus.cli import json_output
from nexus.config import load_config, get_audit_path
from nexus.db import get_connection, init_db
from nexus.guards import check_buy_guard, check_option_sell_guard, check_sell_guard
from nexus.ledger import (
    create_reservation,
    process_cancel,
    process_cancel_failed,
    process_cancel_pending,
    release_reservation,
    release_shares,
    reserve_shares,
)
from nexus.models import AssetClass, OrderSide, OrderStatus, OrderType
from nexus.occ import is_occ_symbol, occ_to_underlying, parse_occ_symbol
from nexus.sync import sync_outstanding_orders

order_app = typer.Typer(name="order", no_args_is_help=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lookup_strategy(conn, strategy_name: str) -> dict:
    """Return strategy row joined with broker profile_name, or exit with error."""
    row = conn.execute(
        "SELECT s.id, s.name, s.cash_balance, b.profile_name AS broker_profile"
        " FROM strategies s"
        " JOIN broker_accounts b ON s.broker_account_id = b.id"
        " WHERE s.name = ?",
        (strategy_name,),
    ).fetchone()
    if row is None:
        json_output({"error": f"strategy '{strategy_name}' not found"})
        typer.echo(f"Error: strategy '{strategy_name}' not found.", err=True)
        raise typer.Exit(1)
    return row


_INSUFFICIENT_QTY_CODE = "40310000"


def _looks_like_insufficient_qty(exc: RuntimeError) -> bool:
    """Detect Alpaca's 'insufficient qty available' error.

    The broker signals this with code 40310000 in the error message. Used
    by sell/close to suggest a likely ghost-order cause in the failure
    output.
    """
    return _INSUFFICIENT_QTY_CODE in str(exc)


def _ghost_order_hint(broker: AlpacaBroker, symbol: str) -> dict:
    """Return a hint dict listing likely ghost orders consuming a symbol's shares.

    On error, returns an empty dict. The hint is best-effort and never
    modifies broker state — auto-cancellation stays in the reconciler.
    """
    try:
        open_orders = broker.list_orders("open")
    except RuntimeError:
        return {"open_orders": [], "error": "could_not_list_open_orders"}
    matching = [
        {
            "broker_order_id": o.broker_order_id,
            "client_order_id": o.client_order_id,
            "side": o.side,
            "qty": o.qty,
            "filled_qty": o.filled_qty,
        }
        for o in open_orders
        if o.symbol == symbol and o.side == "sell"
    ]
    return {"open_orders": matching}


def _emit_ghost_hint(exc: RuntimeError, broker: AlpacaBroker, symbol: str, err_payload: dict) -> None:
    """Enrich error payload and echo stderr hint if the error looks like a ghost-order issue."""
    if not _looks_like_insufficient_qty(exc):
        return
    hint = _ghost_order_hint(broker, symbol)
    err_payload["hint"] = (
        "insufficient_qty_available — possibly caused by ghost orders "
        "consuming shares. Run 'nexus reconcile --dry-run' to inspect."
    )
    err_payload["open_orders_on_symbol"] = hint.get("open_orders", [])
    typer.echo(
        "Hint: this looks like the 'insufficient qty available' error "
        "(40310000). It can be caused by ghost orders — Nexus rows "
        "marked cancelled but still open on Alpaca. Run "
        "'nexus reconcile --dry-run' to inspect.",
        err=True,
    )


@order_app.command("buy")
def order_buy(
    symbol: str = typer.Argument(..., help="Ticker symbol"),
    qty: int = typer.Argument(..., help="Number of shares"),
    strategy: str = typer.Option(..., "--strategy", help="Strategy name"),
    order_type: str = typer.Option("market", "--type", help="Order type"),
    limit_price: float | None = typer.Option(None, "--limit-price"),
    stop_price: float | None = typer.Option(None, "--stop-price"),
    trail_percent: float | None = typer.Option(None, "--trail-percent"),
    time_in_force: str | None = typer.Option(None, "--time-in-force", help="Time in force (day, gtc, ioc, fok)"),
    actor: str = typer.Option("cli:manual", "--actor"),
) -> None:
    """Place a buy order."""
    config = load_config()
    audit_path = get_audit_path(config)
    slippage = config.order.slippage_buffer_percent / 100.0

    conn = get_connection()
    init_db(conn)

    strat = _lookup_strategy(conn, strategy)
    strategy_id: int = strat["id"]
    broker_profile: str = strat["broker_profile"]
    broker = AlpacaBroker(broker_profile)

    # Eager sync
    sync_outstanding_orders(conn, strategy_id, broker)

    # Estimate cost
    if order_type == "limit" and limit_price is not None:
        estimated_cost = limit_price * qty
    elif order_type in ("stop", "stop_limit") and stop_price is not None:
        estimated_cost = stop_price * qty * (1 + slippage)
    else:
        # market or trailing_stop: use last price
        try:
            last_price = float(broker.get_last_price(symbol))
        except RuntimeError as exc:
            json_output({"error": f"Error fetching price for {symbol}: {exc}"})
            typer.echo(f"Error fetching price for {symbol}: {exc}", err=True)
            raise typer.Exit(1)
        estimated_cost = last_price * qty * (1 + slippage)

    # Buy guard
    ok, reason = check_buy_guard(conn, strategy_id, symbol, estimated_cost)
    if not ok:
        json_output({"error": f"Buy blocked: {reason}"})
        typer.echo(f"Buy blocked: {reason}", err=True)
        raise typer.Exit(1)

    client_order_id = f"nx-{strategy[:6]}-{symbol}-{uuid4().hex[:8]}"
    now = _now()

    # Insert order record (status=pending)
    cur = conn.execute(
        "INSERT INTO orders"
        " (strategy_id, symbol, side, qty, order_type, limit_price, stop_price, trail_percent,"
        "  time_in_force, status, client_order_id, reserved_amount, filled_qty, actor, created_at, updated_at)"
        " VALUES (?, ?, 'buy', ?, ?, ?, ?, ?, ?, 'pending', ?, ?, 0, ?, ?, ?)",
        (
            strategy_id, symbol, qty, order_type,
            limit_price, stop_price, trail_percent, time_in_force,
            client_order_id, estimated_cost,
            actor, now, now,
        ),
    )
    order_id: int = cur.lastrowid

    # Create cash reservation
    create_reservation(conn, strategy_id, order_id, estimated_cost)
    conn.commit()

    # Submit to broker
    try:
        result = broker.submit_order(
            symbol, qty, "buy", order_type,
            client_order_id=client_order_id,
            limit_price=limit_price,
            stop_price=stop_price,
            trail_percent=trail_percent,
            time_in_force=time_in_force,
        )
    except RuntimeError as exc:
        # Roll back: cancel order + release reservation
        conn.execute(
            "UPDATE orders SET status = 'cancelled', updated_at = ? WHERE id = ?",
            (_now(), order_id),
        )
        release_reservation(conn, order_id)
        conn.commit()
        json_output({"error": f"Broker error: {exc}"})
        typer.echo(f"Broker error: {exc}", err=True)
        raise typer.Exit(1)

    # Update order with broker info
    conn.execute(
        "UPDATE orders SET status = 'submitted', broker_order_id = ?, updated_at = ? WHERE id = ?",
        (result.broker_order_id, _now(), order_id),
    )
    conn.commit()

    log_event(audit_path, {
        "event": "order_submitted",
        "side": "buy",
        "strategy": strategy,
        "symbol": symbol,
        "qty": qty,
        "order_type": order_type,
        "time_in_force": time_in_force,
        "client_order_id": client_order_id,
        "broker_order_id": result.broker_order_id,
        "estimated_cost": estimated_cost,
        "actor": actor,
    })

    if json_output({"status": "ok", "order_id": order_id, "client_order_id": client_order_id, "message": f"Order submitted: {client_order_id}"}):
        return
    typer.echo(f"Order submitted: {client_order_id}")


@order_app.command("sell")
def order_sell(
    symbol: str = typer.Argument(..., help="Ticker symbol"),
    qty: int = typer.Argument(..., help="Number of shares"),
    strategy: str = typer.Option(..., "--strategy", help="Strategy name"),
    order_type: str = typer.Option("market", "--type", help="Order type"),
    limit_price: float | None = typer.Option(None, "--limit-price"),
    stop_price: float | None = typer.Option(None, "--stop-price"),
    trail_percent: float | None = typer.Option(None, "--trail-percent"),
    time_in_force: str | None = typer.Option(None, "--time-in-force", help="Time in force (day, gtc, ioc, fok)"),
    actor: str = typer.Option("cli:manual", "--actor"),
) -> None:
    """Place a sell order."""
    config = load_config()
    audit_path = get_audit_path(config)

    conn = get_connection()
    init_db(conn)

    strat = _lookup_strategy(conn, strategy)
    strategy_id: int = strat["id"]
    broker_profile: str = strat["broker_profile"]
    broker = AlpacaBroker(broker_profile)

    # Eager sync
    sync_outstanding_orders(conn, strategy_id, broker)

    # Sell guard
    ok, reason = check_sell_guard(conn, strategy_id, symbol, qty)
    if not ok:
        json_output({"error": f"Sell blocked: {reason}"})
        typer.echo(f"Sell blocked: {reason}", err=True)
        raise typer.Exit(1)

    client_order_id = f"nx-{strategy[:6]}-{symbol}-{uuid4().hex[:8]}"
    now = _now()

    # Insert order record (status=pending), reserved_amount = qty (share count for reference)
    cur = conn.execute(
        "INSERT INTO orders"
        " (strategy_id, symbol, side, qty, order_type, limit_price, stop_price, trail_percent,"
        "  time_in_force, status, client_order_id, reserved_amount, filled_qty, actor, created_at, updated_at)"
        " VALUES (?, ?, 'sell', ?, ?, ?, ?, ?, ?, 'pending', ?, ?, 0, ?, ?, ?)",
        (
            strategy_id, symbol, qty, order_type,
            limit_price, stop_price, trail_percent, time_in_force,
            client_order_id, float(qty),
            actor, now, now,
        ),
    )
    order_id: int = cur.lastrowid

    # Reserve shares
    reserve_shares(conn, strategy_id, symbol, qty)
    conn.commit()

    # Submit to broker
    try:
        result = broker.submit_order(
            symbol, qty, "sell", order_type,
            client_order_id=client_order_id,
            limit_price=limit_price,
            stop_price=stop_price,
            trail_percent=trail_percent,
            time_in_force=time_in_force,
        )
    except RuntimeError as exc:
        # Roll back: cancel order + release shares
        conn.execute(
            "UPDATE orders SET status = 'cancelled', updated_at = ? WHERE id = ?",
            (_now(), order_id),
        )
        release_shares(conn, strategy_id, symbol, qty)
        conn.commit()
        err_payload: dict = {"error": f"Broker error: {exc}"}
        _emit_ghost_hint(exc, broker, symbol, err_payload)
        json_output(err_payload)
        typer.echo(f"Broker error: {exc}", err=True)
        raise typer.Exit(1)

    # Update order with broker info
    conn.execute(
        "UPDATE orders SET status = 'submitted', broker_order_id = ?, updated_at = ? WHERE id = ?",
        (result.broker_order_id, _now(), order_id),
    )
    conn.commit()

    log_event(audit_path, {
        "event": "order_submitted",
        "side": "sell",
        "strategy": strategy,
        "symbol": symbol,
        "qty": qty,
        "order_type": order_type,
        "time_in_force": time_in_force,
        "client_order_id": client_order_id,
        "broker_order_id": result.broker_order_id,
        "actor": actor,
    })

    if json_output({"status": "ok", "order_id": order_id, "client_order_id": client_order_id, "message": f"Order submitted: {client_order_id}"}):
        return
    typer.echo(f"Order submitted: {client_order_id}")


@order_app.command("close")
def order_close(
    symbol: str = typer.Argument(..., help="Ticker symbol"),
    strategy: str = typer.Option(..., "--strategy", help="Strategy name"),
    actor: str = typer.Option("cli:manual", "--actor"),
) -> None:
    """Close entire position in a symbol (sell all available shares)."""
    conn = get_connection()
    init_db(conn)

    strat = _lookup_strategy(conn, strategy)
    strategy_id: int = strat["id"]

    config = load_config()
    audit_path = get_audit_path(config)

    broker_profile: str = strat["broker_profile"]
    broker = AlpacaBroker(broker_profile)

    sync_outstanding_orders(conn, strategy_id, broker)

    position = conn.execute(
        "SELECT qty, reserved_qty FROM positions WHERE strategy_id = ? AND symbol = ? AND qty > 0",
        (strategy_id, symbol),
    ).fetchone()
    if position is None:
        json_output({"error": f"no position in {symbol} for strategy '{strategy}'"})
        typer.echo(f"Error: no position in {symbol} for strategy '{strategy}'.", err=True)
        raise typer.Exit(1)

    available = position["qty"] - position["reserved_qty"]
    if available <= 0:
        json_output({"error": f"no available shares to sell for {symbol} (all reserved)"})
        typer.echo(f"Error: no available shares to sell for {symbol} (all reserved).", err=True)
        raise typer.Exit(1)

    ok, reason = check_sell_guard(conn, strategy_id, symbol, available)
    if not ok:
        json_output({"error": f"Sell blocked: {reason}"})
        typer.echo(f"Sell blocked: {reason}", err=True)
        raise typer.Exit(1)

    client_order_id = f"nx-{strategy[:6]}-{symbol}-{uuid4().hex[:8]}"
    now = _now()

    cur = conn.execute(
        "INSERT INTO orders"
        " (strategy_id, symbol, side, qty, order_type, limit_price, stop_price, trail_percent,"
        "  status, client_order_id, reserved_amount, filled_qty, actor, created_at, updated_at)"
        " VALUES (?, ?, 'sell', ?, 'market', NULL, NULL, NULL, 'pending', ?, ?, 0, ?, ?, ?)",
        (strategy_id, symbol, available, client_order_id, float(available), actor, now, now),
    )
    order_id: int = cur.lastrowid

    reserve_shares(conn, strategy_id, symbol, available)
    conn.commit()

    try:
        result = broker.submit_order(symbol, available, "sell", "market", client_order_id=client_order_id)
    except RuntimeError as exc:
        conn.execute(
            "UPDATE orders SET status = 'cancelled', updated_at = ? WHERE id = ?",
            (_now(), order_id),
        )
        release_shares(conn, strategy_id, symbol, available)
        conn.commit()
        err_payload: dict = {"error": f"Broker error: {exc}"}
        _emit_ghost_hint(exc, broker, symbol, err_payload)
        json_output(err_payload)
        typer.echo(f"Broker error: {exc}", err=True)
        raise typer.Exit(1)

    conn.execute(
        "UPDATE orders SET status = 'submitted', broker_order_id = ?, updated_at = ? WHERE id = ?",
        (result.broker_order_id, _now(), order_id),
    )
    conn.commit()

    log_event(audit_path, {
        "event": "order_submitted",
        "side": "sell",
        "action": "close",
        "strategy": strategy,
        "symbol": symbol,
        "qty": available,
        "order_type": "market",
        "client_order_id": client_order_id,
        "broker_order_id": result.broker_order_id,
        "actor": actor,
    })

    if json_output({"status": "ok", "order_id": order_id, "client_order_id": client_order_id, "message": f"Order submitted: {client_order_id}"}):
        return
    typer.echo(f"Order submitted: {client_order_id}")


@order_app.command("cancel")
def order_cancel(
    order_id: int = typer.Argument(..., help="Order ID to cancel"),
) -> None:
    """Cancel an open order.

    Confirms cancellation with the broker before marking the order
    `cancelled` locally. If the broker-side cancel fails or the order is
    still open on the broker after the call, the order is marked
    `cancel_pending` so the reconciler can retry on its next sweep.
    """
    config = load_config()
    audit_path = get_audit_path(config)

    conn = get_connection()
    init_db(conn)

    order = conn.execute(
        "SELECT o.id, o.broker_order_id, o.status, o.client_order_id, o.symbol, o.actor,"
        "       b.profile_name AS broker_profile"
        " FROM orders o"
        " JOIN strategies s ON o.strategy_id = s.id"
        " JOIN broker_accounts b ON s.broker_account_id = b.id"
        " WHERE o.id = ?",
        (order_id,),
    ).fetchone()

    if order is None:
        json_output({"error": f"order {order_id} not found"})
        typer.echo(f"Error: order {order_id} not found.", err=True)
        raise typer.Exit(1)

    cancellable = {
        OrderStatus.submitted.value,
        OrderStatus.partially_filled.value,
        OrderStatus.cancel_pending.value,
    }
    if order["status"] not in cancellable:
        json_output({"error": f"order {order_id} has status '{order['status']}' and cannot be cancelled"})
        typer.echo(
            f"Error: order {order_id} has status '{order['status']}' and cannot be cancelled.",
            err=True,
        )
        raise typer.Exit(1)

    broker_order_id = order["broker_order_id"]
    broker = AlpacaBroker(order["broker_profile"])

    # Step 1: send cancel to broker
    broker_error: str | None = None
    try:
        broker.cancel_order(broker_order_id)
    except RuntimeError as exc:
        broker_error = str(exc)

    # Step 2: if broker call failed, mark cancel_pending and exit cleanly.
    # The reconciler will retry on its next sweep.
    if broker_error is not None:
        process_cancel_pending(conn, order_id)
        log_event(audit_path, {
            "event": "cancel_pending",
            "order_id": order_id,
            "client_order_id": order["client_order_id"],
            "broker_order_id": broker_order_id,
            "symbol": order["symbol"],
            "reason": f"broker_error: {broker_error}",
            "actor": order["actor"] or "cli:manual",
        })
        json_output({
            "status": "pending",
            "order_id": order_id,
            "message": (
                f"Broker cancel failed ({broker_error}); marked cancel_pending. "
                "Reconciler will retry."
            ),
        })
        typer.echo(
            f"Warning: broker cancel failed ({broker_error}); "
            f"order {order_id} marked cancel_pending. "
            "Reconciler will retry on next sweep.",
            err=True,
        )
        return

    # Step 3: broker call succeeded — verify resulting status.
    broker_confirmed = False
    broker_status: str | None = None
    verify_error: str | None = None
    try:
        broker_state = broker.get_order(broker_order_id)
        broker_status = broker_state.status
        if broker_status in ("cancelled", "canceled", "expired"):
            broker_confirmed = True
    except RuntimeError as exc:
        verify_error = str(exc)

    if broker_confirmed:
        process_cancel(conn, order_id)
        log_event(audit_path, {
            "event": "order_cancelled",
            "order_id": order_id,
            "client_order_id": order["client_order_id"],
            "broker_order_id": broker_order_id,
            "symbol": order["symbol"],
            "actor": order["actor"] or "cli:manual",
        })
        if json_output({"status": "ok", "order_id": order_id}):
            return
        typer.echo(f"Order {order_id} cancelled.")
        return

    # Step 4: broker call returned without error but status is still open.
    # Mark cancel_pending so the reconciler retries.
    process_cancel_pending(conn, order_id)
    log_event(audit_path, {
        "event": "cancel_pending",
        "order_id": order_id,
        "client_order_id": order["client_order_id"],
        "broker_order_id": broker_order_id,
        "symbol": order["symbol"],
        "reason": (
            f"broker_status_still_open: {broker_status}"
            if broker_status is not None
            else f"verify_failed: {verify_error}"
        ),
        "actor": order["actor"] or "cli:manual",
    })
    json_output({
        "status": "pending",
        "order_id": order_id,
        "message": (
            f"Broker still reports order as '{broker_status or 'unknown'}'; "
            "marked cancel_pending. Reconciler will retry."
        ),
    })
    typer.echo(
        f"Warning: broker still reports order as "
        f"'{broker_status or 'unknown'}'; order {order_id} marked "
        "cancel_pending. Reconciler will retry on next sweep.",
        err=True,
    )


@order_app.command("resolve")
def order_resolve(
    order_id: int = typer.Argument(..., help="Order ID to resolve"),
    action: str = typer.Option(
        ..., "--action", help="Resolution action: 'force-cancel' or 'reset'"
    ),
) -> None:
    """Manually resolve a cancel_failed order.

    Use --action force-cancel to release the reservation (accepting that
    broker-side state is unknown), or --action reset to move back to
    cancel_pending so the reconciler retries.
    """
    valid_actions = {"force-cancel", "reset"}
    if action not in valid_actions:
        json_output({"error": f"--action must be one of: {', '.join(sorted(valid_actions))}"})
        typer.echo(
            f"Error: --action must be one of: {', '.join(sorted(valid_actions))}",
            err=True,
        )
        raise typer.Exit(1)

    config = load_config()
    audit_path = get_audit_path(config)
    conn = get_connection()
    init_db(conn)

    order = conn.execute(
        "SELECT id, status, symbol, client_order_id, broker_order_id, actor, strategy_id"
        " FROM orders WHERE id = ?",
        (order_id,),
    ).fetchone()

    if order is None:
        json_output({"error": f"order {order_id} not found"})
        typer.echo(f"Error: order {order_id} not found.", err=True)
        raise typer.Exit(1)

    if order["status"] != OrderStatus.cancel_failed.value:
        json_output({
            "error": f"order {order_id} has status '{order['status']}'; "
            "only cancel_failed orders can be resolved"
        })
        typer.echo(
            f"Error: order {order_id} has status '{order['status']}'; "
            "only cancel_failed orders can be resolved.",
            err=True,
        )
        raise typer.Exit(1)

    if action == "force-cancel":
        process_cancel(conn, order_id)
        log_event(audit_path, {
            "event": "order_force_cancelled",
            "order_id": order_id,
            "client_order_id": order["client_order_id"],
            "broker_order_id": order["broker_order_id"],
            "symbol": order["symbol"],
            "actor": "cli:manual",
        })
        if json_output({"status": "ok", "order_id": order_id, "action": "force-cancel"}):
            return
        typer.echo(f"Order {order_id} force-cancelled. Reservation released.")
    else:
        conn.execute(
            "UPDATE orders SET status = ?, cancel_attempts = 0, updated_at = ? WHERE id = ?",
            (OrderStatus.cancel_pending, _now(), order_id),
        )
        conn.commit()
        log_event(audit_path, {
            "event": "order_reset_to_cancel_pending",
            "order_id": order_id,
            "client_order_id": order["client_order_id"],
            "broker_order_id": order["broker_order_id"],
            "symbol": order["symbol"],
            "actor": "cli:manual",
        })
        if json_output({"status": "ok", "order_id": order_id, "action": "reset"}):
            return
        typer.echo(
            f"Order {order_id} reset to cancel_pending (attempts=0). "
            "Reconciler will retry."
        )


@order_app.command("option-sell")
def order_option_sell(
    symbol: str = typer.Argument(..., help="OCC option symbol (e.g., NKE260718P00040000)"),
    qty: int = typer.Argument(..., help="Number of contracts"),
    strategy: str = typer.Option(..., "--strategy", help="Strategy name"),
    limit_price: float = typer.Option(..., "--limit-price", help="Limit price per contract"),
    order_type: str = typer.Option("limit", "--type", help="Order type (default: limit)"),
    time_in_force: str | None = typer.Option("day", "--time-in-force", help="Time in force (day, gtc)"),
    actor: str = typer.Option("cli:manual", "--actor"),
) -> None:
    """Sell an option (open a short position — cash-secured put or covered call).

    For puts: checks available cash >= strike * 100 * qty (assignment obligation).
    For calls: checks position holds >= 100 * qty shares of underlying.

    Limit price is required for options (wide spreads make market orders unsafe).
    """
    if not is_occ_symbol(symbol):
        json_output({"error": f"'{symbol}' is not a valid OCC option symbol"})
        typer.echo(f"Error: '{symbol}' is not a valid OCC option symbol.", err=True)
        raise typer.Exit(1)

    config = load_config()
    audit_path = get_audit_path(config)

    conn = get_connection()
    init_db(conn)

    strat = _lookup_strategy(conn, strategy)
    strategy_id: int = strat["id"]
    broker_profile: str = strat["broker_profile"]
    broker = AlpacaBroker(broker_profile)

    # Eager sync
    sync_outstanding_orders(conn, strategy_id, broker)

    # Parse OCC for guard check
    parsed = parse_occ_symbol(symbol)
    right = parsed["right"]
    strike = parsed["strike"]

    # Guard check
    ok, reason = check_option_sell_guard(conn, strategy_id, symbol, qty)
    if not ok:
        json_output({"error": f"Option sell blocked: {reason}"})
        typer.echo(f"Option sell blocked: {reason}", err=True)
        raise typer.Exit(1)

    client_order_id = f"nx-{strategy[:6]}-{symbol}-{uuid4().hex[:8]}"
    now = _now()

    # Determine reservation amount:
    # - Put: reserve strike * 100 * qty (assignment obligation)
    # - Call: reserve 0 (covered call needs no cash outlay)
    assignment_obligation = strike * 100 * qty if right == "put" else 0.0

    # Insert order record
    cur = conn.execute(
        "INSERT INTO orders"
        " (strategy_id, symbol, side, qty, order_type, limit_price,"
        "  time_in_force, status, client_order_id, reserved_amount, filled_qty, actor, created_at, updated_at)"
        " VALUES (?, ?, 'sell', ?, ?, ?, ?, 'pending', ?, ?, 0, ?, ?, ?)",
        (
            strategy_id, symbol, qty, order_type, limit_price,
            time_in_force, client_order_id, assignment_obligation,
            actor, now, now,
        ),
    )
    order_id: int = cur.lastrowid

    # Create cash reservation (for puts — assignment obligation)
    if assignment_obligation > 0:
        create_reservation(conn, strategy_id, order_id, assignment_obligation)
    conn.commit()

    # Submit to broker
    try:
        result = broker.submit_order(
            symbol, qty, "sell", order_type,
            client_order_id=client_order_id,
            limit_price=limit_price,
            time_in_force=time_in_force,
        )
    except RuntimeError as exc:
        # Roll back
        conn.execute(
            "UPDATE orders SET status = 'cancelled', updated_at = ? WHERE id = ?",
            (_now(), order_id),
        )
        if assignment_obligation > 0:
            release_reservation(conn, order_id)
        conn.commit()
        err_payload: dict = {"error": f"Broker error: {exc}"}
        json_output(err_payload)
        typer.echo(f"Broker error: {exc}", err=True)
        raise typer.Exit(1)

    # Update order with broker info
    conn.execute(
        "UPDATE orders SET status = 'submitted', broker_order_id = ?, updated_at = ? WHERE id = ?",
        (result.broker_order_id, _now(), order_id),
    )
    conn.commit()

    log_event(audit_path, {
        "event": "order_submitted",
        "side": "option_sell",
        "right": right,
        "strategy": strategy,
        "symbol": symbol,
        "underlying": parsed["root"],
        "strike": strike,
        "expiry": parsed["expiry"],
        "qty": qty,
        "order_type": order_type,
        "limit_price": limit_price,
        "time_in_force": time_in_force,
        "client_order_id": client_order_id,
        "broker_order_id": result.broker_order_id,
        "assignment_obligation": assignment_obligation,
        "actor": actor,
    })

    if json_output({"status": "ok", "order_id": order_id, "client_order_id": client_order_id, "message": f"Option sell submitted: {client_order_id}"}):
        return
    typer.echo(f"Option sell submitted: {client_order_id}")


@order_app.command("option-buy")
def order_option_buy(
    symbol: str = typer.Argument(..., help="OCC option symbol (e.g., NKE260718P00040000)"),
    qty: int = typer.Argument(..., help="Number of contracts"),
    strategy: str = typer.Option(..., "--strategy", help="Strategy name"),
    limit_price: float = typer.Option(..., "--limit-price", help="Limit price per contract"),
    order_type: str = typer.Option("limit", "--type", help="Order type (default: limit)"),
    time_in_force: str | None = typer.Option("day", "--time-in-force", help="Time in force (day, gtc)"),
    actor: str = typer.Option("cli:manual", "--actor"),
) -> None:
    """Buy an option (close a short position or open a long).

    Typically used to buy back a short put/call to close the position.
    Limit price is required for options.
    """
    if not is_occ_symbol(symbol):
        json_output({"error": f"'{symbol}' is not a valid OCC option symbol"})
        typer.echo(f"Error: '{symbol}' is not a valid OCC option symbol.", err=True)
        raise typer.Exit(1)

    config = load_config()
    audit_path = get_audit_path(config)

    conn = get_connection()
    init_db(conn)

    strat = _lookup_strategy(conn, strategy)
    strategy_id: int = strat["id"]
    broker_profile: str = strat["broker_profile"]
    broker = AlpacaBroker(broker_profile)

    # Eager sync
    sync_outstanding_orders(conn, strategy_id, broker)

    parsed = parse_occ_symbol(symbol)
    right = parsed["right"]

    # Estimate cost
    estimated_cost = limit_price * 100 * qty

    # Guard check
    ok, reason = check_buy_guard(conn, strategy_id, symbol, estimated_cost)
    if not ok:
        json_output({"error": f"Option buy blocked: {reason}"})
        typer.echo(f"Option buy blocked: {reason}", err=True)
        raise typer.Exit(1)

    client_order_id = f"nx-{strategy[:6]}-{symbol}-{uuid4().hex[:8]}"
    now = _now()

    # Insert order record
    cur = conn.execute(
        "INSERT INTO orders"
        " (strategy_id, symbol, side, qty, order_type, limit_price,"
        "  time_in_force, status, client_order_id, reserved_amount, filled_qty, actor, created_at, updated_at)"
        " VALUES (?, ?, 'buy', ?, ?, ?, ?, 'pending', ?, ?, 0, ?, ?, ?)",
        (
            strategy_id, symbol, qty, order_type, limit_price,
            time_in_force, client_order_id, estimated_cost,
            actor, now, now,
        ),
    )
    order_id: int = cur.lastrowid

    # Create cash reservation
    create_reservation(conn, strategy_id, order_id, estimated_cost)
    conn.commit()

    # Submit to broker
    try:
        result = broker.submit_order(
            symbol, qty, "buy", order_type,
            client_order_id=client_order_id,
            limit_price=limit_price,
            time_in_force=time_in_force,
        )
    except RuntimeError as exc:
        conn.execute(
            "UPDATE orders SET status = 'cancelled', updated_at = ? WHERE id = ?",
            (_now(), order_id),
        )
        release_reservation(conn, order_id)
        conn.commit()
        err_payload: dict = {"error": f"Broker error: {exc}"}
        json_output(err_payload)
        typer.echo(f"Broker error: {exc}", err=True)
        raise typer.Exit(1)

    # Update order with broker info
    conn.execute(
        "UPDATE orders SET status = 'submitted', broker_order_id = ?, updated_at = ? WHERE id = ?",
        (result.broker_order_id, _now(), order_id),
    )
    conn.commit()

    log_event(audit_path, {
        "event": "order_submitted",
        "side": "option_buy",
        "right": right,
        "strategy": strategy,
        "symbol": symbol,
        "underlying": parsed["root"],
        "strike": parsed["strike"],
        "expiry": parsed["expiry"],
        "qty": qty,
        "order_type": order_type,
        "limit_price": limit_price,
        "time_in_force": time_in_force,
        "client_order_id": client_order_id,
        "broker_order_id": result.broker_order_id,
        "estimated_cost": estimated_cost,
        "actor": actor,
    })

    if json_output({"status": "ok", "order_id": order_id, "client_order_id": client_order_id, "message": f"Option buy submitted: {client_order_id}"}):
        return
    typer.echo(f"Option buy submitted: {client_order_id}")


@order_app.command("replace")
def order_replace(
    order_id: int = typer.Argument(..., help="Nexus order ID"),
    qty: int | None = typer.Option(None, "--qty", help="New quantity"),
    limit_price: float | None = typer.Option(None, "--limit-price", help="New limit price"),
    stop_price: float | None = typer.Option(None, "--stop-price", help="New stop price"),
    trail: float | None = typer.Option(None, "--trail", help="New trail value"),
    time_in_force: str | None = typer.Option(None, "--time-in-force", "-t", help="New time in force"),
) -> None:
    """Replace (modify) an existing order."""
    conn = get_connection()
    init_db(conn)

    # Look up the order
    row = conn.execute(
        "SELECT broker_order_id, strategy_id FROM orders WHERE id = ?",
        (order_id,),
    ).fetchone()
    if row is None:
        json_output({"error": f"order {order_id} not found"})
        typer.echo("Error: Order not found.", err=True)
        raise typer.Exit(1)

    broker_order_id = row["broker_order_id"]
    if broker_order_id is None:
        json_output({"error": f"order {order_id} has no broker_order_id (not yet submitted)"})
        typer.echo("Error: Order has no broker_order_id (not yet submitted).", err=True)
        raise typer.Exit(1)

    # Get strategy's broker profile
    strategy = conn.execute(
        "SELECT ba.profile_name FROM strategies s JOIN broker_accounts ba ON s.broker_account_id = ba.id WHERE s.id = ?",
        (row["strategy_id"],),
    ).fetchone()

    try:
        broker = AlpacaBroker(strategy["profile_name"])
        new_order = broker.replace_order(
            broker_order_id,
            qty=qty,
            limit_price=limit_price,
            stop_price=stop_price,
            trail=trail,
            time_in_force=time_in_force,
        )
    except RuntimeError as exc:
        json_output({"error": f"Broker error: {exc}"})
        typer.echo(f"Broker error: {exc}", err=True)
        raise typer.Exit(1)

    # Update local DB with new values
    updates = []
    params = []
    if qty is not None:
        updates.append("qty = ?")
        params.append(qty)
    if limit_price is not None:
        updates.append("limit_price = ?")
        params.append(limit_price)
    if stop_price is not None:
        updates.append("stop_price = ?")
        params.append(stop_price)
    if new_order.broker_order_id != broker_order_id:
        updates.append("broker_order_id = ?")
        params.append(new_order.broker_order_id)

    if updates:
        updates.append("updated_at = ?")
        params.append(_now())
        params.append(order_id)
        conn.execute(f"UPDATE orders SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()

    # JSON output
    data = {"status": "ok", "order_id": order_id, "new_broker_order_id": new_order.broker_order_id}
    if json_output(data):
        return

    typer.echo(f"Order {order_id} replaced. New broker order: {new_order.broker_order_id}")


@order_app.command("status")
def order_status(
    order_id: int = typer.Argument(..., help="Order ID"),
) -> None:
    """Show order status."""
    conn = get_connection()
    init_db(conn)

    row = conn.execute(
        "SELECT * FROM orders WHERE id = ?",
        (order_id,),
    ).fetchone()

    if row is None:
        json_output({"error": f"order {order_id} not found"})
        typer.echo(f"Error: order {order_id} not found.", err=True)
        raise typer.Exit(1)

    if json_output(dict(row)):
        return

    fields = row.keys()
    for field in fields:
        typer.echo(f"{field}: {row[field]}")


@order_app.command("list")
def order_list(
    strategy: str | None = typer.Option(None, "--strategy"),
    status: str | None = typer.Option(None, "--status"),
    symbol: str | None = typer.Option(None, "--symbol"),
    order_type: OrderType | None = typer.Option(None, "--type", help="Filter by order type"),
    side: OrderSide | None = typer.Option(None, "--side", help="Filter by side"),
    asset_class: AssetClass | None = typer.Option(None, "--asset-class", help="Filter by asset class"),
) -> None:
    """List orders with optional filters."""
    conn = get_connection()
    init_db(conn)

    query = (
        "SELECT o.id, s.name AS strategy, o.symbol, o.side, o.qty, o.order_type,"
        "       o.time_in_force, o.status, o.client_order_id, o.filled_qty, o.created_at"
        " FROM orders o"
        " JOIN strategies s ON o.strategy_id = s.id"
        " WHERE 1=1"
    )
    params: list = []

    if strategy is not None:
        query += " AND s.name = ?"
        params.append(strategy)
    if status is not None:
        query += " AND o.status = ?"
        params.append(status)
    if symbol is not None:
        query += " AND o.symbol = ?"
        params.append(symbol)
    if order_type is not None:
        query += " AND o.order_type = ?"
        params.append(order_type)
    if side is not None:
        query += " AND o.side = ?"
        params.append(side)
    if asset_class is not None:
        if asset_class == AssetClass.option:
            query += " AND o.symbol GLOB '*[0-9][0-9][0-9][0-9][0-9][0-9][CP][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]*'"
        else:
            query += " AND o.symbol NOT GLOB '*[0-9][0-9][0-9][0-9][0-9][0-9][CP][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]*'"

    query += " ORDER BY o.id DESC"

    rows = conn.execute(query, params).fetchall()

    if json_output({"items": [dict(row) for row in rows]}):
        return

    if not rows:
        typer.echo("No orders found.")
        return

    header = (
        f"{'ID':<6} {'STRATEGY':<16} {'SYMBOL':<8} {'SIDE':<5} {'QTY':>6}"
        f" {'TYPE':<14} {'STATUS':<16} {'FILLED':>6} {'CREATED_AT'}"
    )
    typer.echo(header)
    typer.echo("-" * len(header))
    for row in rows:
        typer.echo(
            f"{row['id']:<6} {row['strategy']:<16} {row['symbol']:<8}"
            f" {row['side']:<5} {row['qty']:>6}"
            f" {row['order_type']:<14} {row['status']:<16}"
            f" {row['filled_qty']:>6} {row['created_at'] or ''}"
        )
