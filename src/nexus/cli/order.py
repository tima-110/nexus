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
from nexus.guards import check_buy_guard, check_sell_guard
from nexus.ledger import (
    create_reservation,
    process_cancel,
    release_reservation,
    release_shares,
    reserve_shares,
)
from nexus.models import OrderStatus
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


@order_app.command("buy")
def order_buy(
    symbol: str = typer.Argument(..., help="Ticker symbol"),
    qty: int = typer.Argument(..., help="Number of shares"),
    strategy: str = typer.Option(..., "--strategy", help="Strategy name"),
    order_type: str = typer.Option("market", "--type", help="Order type"),
    limit_price: float | None = typer.Option(None, "--limit-price"),
    stop_price: float | None = typer.Option(None, "--stop-price"),
    trail_percent: float | None = typer.Option(None, "--trail-percent"),
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
        "  status, client_order_id, reserved_amount, filled_qty, actor, created_at, updated_at)"
        " VALUES (?, ?, 'buy', ?, ?, ?, ?, ?, 'pending', ?, ?, 0, ?, ?, ?)",
        (
            strategy_id, symbol, qty, order_type,
            limit_price, stop_price, trail_percent,
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
        "  status, client_order_id, reserved_amount, filled_qty, actor, created_at, updated_at)"
        " VALUES (?, ?, 'sell', ?, ?, ?, ?, ?, 'pending', ?, ?, 0, ?, ?, ?)",
        (
            strategy_id, symbol, qty, order_type,
            limit_price, stop_price, trail_percent,
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
        )
    except RuntimeError as exc:
        # Roll back: cancel order + release shares
        conn.execute(
            "UPDATE orders SET status = 'cancelled', updated_at = ? WHERE id = ?",
            (_now(), order_id),
        )
        release_shares(conn, strategy_id, symbol, qty)
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
        "side": "sell",
        "strategy": strategy,
        "symbol": symbol,
        "qty": qty,
        "order_type": order_type,
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
        json_output({"error": f"Broker error: {exc}"})
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
    """Cancel an open order."""
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

    cancellable = {OrderStatus.submitted.value, OrderStatus.partially_filled.value}
    if order["status"] not in cancellable:
        json_output({"error": f"order {order_id} has status '{order['status']}' and cannot be cancelled"})
        typer.echo(
            f"Error: order {order_id} has status '{order['status']}' and cannot be cancelled.",
            err=True,
        )
        raise typer.Exit(1)

    broker = AlpacaBroker(order["broker_profile"])
    try:
        broker.cancel_order(order["broker_order_id"])
    except RuntimeError as exc:
        json_output({"error": f"Broker error while cancelling: {exc}"})
        typer.echo(f"Broker error while cancelling: {exc}", err=True)
        raise typer.Exit(1)

    process_cancel(conn, order_id)

    log_event(audit_path, {
        "event": "order_cancelled",
        "order_id": order_id,
        "client_order_id": order["client_order_id"],
        "broker_order_id": order["broker_order_id"],
        "symbol": order["symbol"],
        "actor": order["actor"] or "cli:manual",
    })

    if json_output({"status": "ok", "order_id": order_id}):
        return
    typer.echo(f"Order {order_id} cancelled.")


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
) -> None:
    """List orders with optional filters."""
    conn = get_connection()
    init_db(conn)

    query = (
        "SELECT o.id, s.name AS strategy, o.symbol, o.side, o.qty, o.order_type,"
        "       o.status, o.client_order_id, o.filled_qty, o.created_at"
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
