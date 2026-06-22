"""Strategy management commands."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import typer

from nexus.broker import AlpacaBroker
from nexus.cli import json_output
from nexus.db import get_connection, init_db
from nexus.ledger import process_cancel, record_transaction

strategy_app = typer.Typer(name="strategy", no_args_is_help=True)


@strategy_app.command("create")
def strategy_create(
    name: str = typer.Argument(..., help="Strategy name"),
    broker: str = typer.Option(..., "--broker", help="Broker profile name"),
    balance: float = typer.Option(..., "--balance", help="Initial cash balance"),
) -> None:
    """Create a new strategy with initial balance."""
    conn = get_connection()
    init_db(conn)

    broker_row = conn.execute(
        "SELECT id FROM broker_accounts WHERE profile_name = ?",
        (broker,),
    ).fetchone()
    if broker_row is None:
        json_output({"error": f"broker account '{broker}' not found"})
        typer.echo(f"Error: broker account '{broker}' not found.", err=True)
        raise typer.Exit(1)

    broker_account_id = broker_row["id"]

    try:
        cur = conn.execute(
            "INSERT INTO strategies (name, broker_account_id, cash_balance, is_active, created_at)"
            " VALUES (?, ?, ?, 1, ?)",
            (name, broker_account_id, balance, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    except Exception as exc:
        json_output({"error": str(exc)})
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    if json_output({"status": "ok", "name": name, "strategy_id": cur.lastrowid}):
        return
    typer.echo(f"Strategy '{name}' created (broker={broker}, balance={balance:.2f}).")


@strategy_app.command("list")
def strategy_list() -> None:
    """List all strategies."""
    conn = get_connection()
    init_db(conn)

    rows = conn.execute(
        "SELECT s.id, s.name, b.profile_name AS broker, s.cash_balance, s.is_active, s.created_at"
        " FROM strategies s"
        " JOIN broker_accounts b ON s.broker_account_id = b.id"
        " ORDER BY s.id"
    ).fetchall()

    if json_output({"items": [
        {"name": r["name"], "cash_balance": r["cash_balance"], "broker": r["broker"], "is_active": bool(r["is_active"])}
        for r in rows
    ]}):
        return

    if not rows:
        typer.echo("No strategies found.")
        return

    header = f"{'ID':<5} {'NAME':<20} {'BROKER':<20} {'CASH_BAL':>12} {'ACTIVE':<8} {'CREATED_AT'}"
    typer.echo(header)
    typer.echo("-" * len(header))
    for row in rows:
        active = "yes" if row["is_active"] else "no"
        created = row["created_at"] or ""
        typer.echo(
            f"{row['id']:<5} {row['name']:<20} {row['broker']:<20}"
            f" {row['cash_balance']:>12.2f} {active:<8} {created}"
        )


@strategy_app.command("show")
def strategy_show(name: str = typer.Argument(..., help="Strategy name")) -> None:
    """Show details for a strategy including positions and open orders."""
    conn = get_connection()
    init_db(conn)

    row = conn.execute(
        "SELECT s.*, b.profile_name AS broker"
        " FROM strategies s"
        " JOIN broker_accounts b ON s.broker_account_id = b.id"
        " WHERE s.name = ?",
        (name,),
    ).fetchone()
    if row is None:
        json_output({"error": f"strategy '{name}' not found"})
        typer.echo(f"Error: strategy '{name}' not found.", err=True)
        raise typer.Exit(1)

    strategy_id = row["id"]
    positions = conn.execute(
        "SELECT * FROM positions WHERE strategy_id = ? AND qty > 0",
        (strategy_id,),
    ).fetchall()
    open_orders = conn.execute(
        "SELECT * FROM orders WHERE strategy_id = ? AND status IN ('submitted', 'partially_filled')",
        (strategy_id,),
    ).fetchall()

    cash_balance = row["cash_balance"] or 0.0

    broker = AlpacaBroker(row["broker"])
    live_prices: dict[str, float] = {}
    try:
        broker_positions = broker.get_positions()
        live_prices = {p.symbol: float(p.current_price) for p in broker_positions}
    except RuntimeError:
        pass

    positions_market_value = sum(
        p["qty"] * live_prices.get(p["symbol"], p["avg_entry_price"] or 0.0)
        for p in positions
    )
    total_equity = cash_balance + positions_market_value

    if json_output({
        "name": row["name"],
        "cash_balance": cash_balance,
        "broker_profile": row["broker"],
        "is_active": bool(row["is_active"]),
        "positions_market_value": positions_market_value,
        "total_equity": total_equity,
        "prices_are_live": bool(live_prices),
        "positions": [
            {"symbol": p["symbol"], "qty": p["qty"], "reserved_qty": p["reserved_qty"] or 0, "avg_entry_price": p["avg_entry_price"]}
            for p in positions
        ],
        "open_orders": [
            {"id": o["id"], "symbol": o["symbol"], "side": o["side"], "qty": o["qty"], "order_type": o["order_type"], "status": o["status"]}
            for o in open_orders
        ],
    }):
        return

    active = "yes" if row["is_active"] else "no"
    typer.echo(f"Strategy:       {row['name']}")
    typer.echo(f"Broker:         {row['broker']}")
    typer.echo(f"Active:         {active}")
    typer.echo(f"Cash balance:   ${cash_balance:.2f}")
    typer.echo(f"Position value: ${positions_market_value:.2f}{'  (live)' if live_prices else '  (cost basis)'}")
    typer.echo(f"Total equity:   ${total_equity:.2f}")

    if positions:
        typer.echo("")
        pos_header = f"{'SYMBOL':<10} {'QTY':>8} {'RESERVED':>10} {'AVAILABLE':>10} {'AVG_ENTRY':>12}"
        typer.echo(pos_header)
        typer.echo("-" * len(pos_header))
        for p in positions:
            reserved = p["reserved_qty"] or 0
            available = p["qty"] - reserved
            avg_entry = p["avg_entry_price"] or 0.0
            typer.echo(
                f"{p['symbol']:<10} {p['qty']:>8} {reserved:>10} {available:>10} {avg_entry:>12.4f}"
            )

    if open_orders:
        typer.echo("")
        ord_header = f"{'ID':<8} {'SYMBOL':<10} {'SIDE':<6} {'QTY':>8} {'TYPE':<12} {'STATUS'}"
        typer.echo(ord_header)
        typer.echo("-" * len(ord_header))
        for o in open_orders:
            typer.echo(
                f"{o['id']:<8} {o['symbol']:<10} {o['side']:<6} {o['qty']:>8}"
                f" {o['order_type']:<12} {o['status']}"
            )


@strategy_app.command("deposit")
def strategy_deposit(
    name: str = typer.Argument(..., help="Strategy name"),
    amount: float = typer.Argument(..., help="Amount to deposit"),
    note: str | None = typer.Option(None, "--note", help="Optional note"),
) -> None:
    """Deposit cash into a strategy."""
    conn = get_connection()
    init_db(conn)

    row = conn.execute(
        "SELECT id FROM strategies WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        json_output({"error": f"strategy '{name}' not found"})
        typer.echo(f"Error: strategy '{name}' not found.", err=True)
        raise typer.Exit(1)

    strategy_id = row["id"]
    record_transaction(conn, strategy_id, None, "deposit", amount, "cli:manual", note)
    conn.commit()

    new_balance = conn.execute(
        "SELECT cash_balance FROM strategies WHERE id = ?",
        (strategy_id,),
    ).fetchone()["cash_balance"]
    if json_output({"status": "ok", "new_balance": new_balance}):
        return
    typer.echo(f"Deposited ${amount:.2f} to '{name}'. New balance: ${new_balance:.2f}")


@strategy_app.command("withdraw")
def strategy_withdraw(
    name: str = typer.Argument(..., help="Strategy name"),
    amount: float = typer.Argument(..., help="Amount to withdraw"),
    note: str | None = typer.Option(None, "--note", help="Optional note"),
) -> None:
    """Withdraw cash from a strategy."""
    conn = get_connection()
    init_db(conn)

    row = conn.execute(
        "SELECT id, cash_balance FROM strategies WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        json_output({"error": f"strategy '{name}' not found"})
        typer.echo(f"Error: strategy '{name}' not found.", err=True)
        raise typer.Exit(1)

    strategy_id = row["id"]
    cash_balance = row["cash_balance"] or 0.0
    if cash_balance < amount:
        json_output({"error": f"Insufficient balance (have ${cash_balance:.2f}, requested ${amount:.2f})"})
        typer.echo(
            f"Error: Insufficient balance (have ${cash_balance:.2f}, requested ${amount:.2f})",
            err=True,
        )
        raise typer.Exit(1)

    record_transaction(conn, strategy_id, None, "withdrawal", -amount, "cli:manual", note)
    conn.commit()

    new_balance = conn.execute(
        "SELECT cash_balance FROM strategies WHERE id = ?",
        (strategy_id,),
    ).fetchone()["cash_balance"]
    if json_output({"status": "ok", "new_balance": new_balance}):
        return
    typer.echo(f"Withdrew ${amount:.2f} from '{name}'. New balance: ${new_balance:.2f}")


@strategy_app.command("set-broker")
def strategy_set_broker(
    name: str = typer.Argument(..., help="Strategy name"),
    broker: str = typer.Option(..., "--broker", help="New broker profile name"),
) -> None:
    """Change the broker account for a strategy."""
    conn = get_connection()
    init_db(conn)

    strategy_row = conn.execute(
        "SELECT id FROM strategies WHERE name = ?",
        (name,),
    ).fetchone()
    if strategy_row is None:
        json_output({"error": f"strategy '{name}' not found"})
        typer.echo(f"Error: strategy '{name}' not found.", err=True)
        raise typer.Exit(1)

    broker_row = conn.execute(
        "SELECT id FROM broker_accounts WHERE profile_name = ?",
        (broker,),
    ).fetchone()
    if broker_row is None:
        json_output({"error": f"broker account '{broker}' not found"})
        typer.echo(f"Error: broker account '{broker}' not found.", err=True)
        raise typer.Exit(1)

    strategy_id = strategy_row["id"]
    open_count = conn.execute(
        "SELECT count(*) AS cnt FROM orders"
        " WHERE strategy_id = ? AND status IN ('submitted', 'partially_filled')",
        (strategy_id,),
    ).fetchone()["cnt"]
    if open_count > 0:
        json_output({"error": f"Cannot change broker while orders are open ({open_count} open orders)"})
        typer.echo(
            f"Error: Cannot change broker while orders are open ({open_count} open orders)",
            err=True,
        )
        raise typer.Exit(1)

    conn.execute(
        "UPDATE strategies SET broker_account_id = ? WHERE id = ?",
        (broker_row["id"], strategy_id),
    )
    conn.commit()
    msg = f"Strategy '{name}' now using broker '{broker}'."
    if json_output({"status": "ok", "message": msg}):
        return
    typer.echo(msg)


@strategy_app.command("delete")
def strategy_delete(
    name: str = typer.Argument(..., help="Strategy name"),
    liquidate: bool = typer.Option(False, "--liquidate", help="Cancel open orders and market-sell all positions before deleting"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Delete a strategy and all its history."""
    conn = get_connection()
    init_db(conn)

    row = conn.execute(
        "SELECT s.id, s.name, b.profile_name AS broker_profile"
        " FROM strategies s"
        " JOIN broker_accounts b ON s.broker_account_id = b.id"
        " WHERE s.name = ?",
        (name,),
    ).fetchone()
    if row is None:
        json_output({"error": f"strategy '{name}' not found"})
        typer.echo(f"Error: strategy '{name}' not found.", err=True)
        raise typer.Exit(1)

    strategy_id = row["id"]
    broker_profile = row["broker_profile"]

    open_orders = conn.execute(
        "SELECT id, broker_order_id FROM orders"
        " WHERE strategy_id = ? AND status IN ('submitted', 'partially_filled')",
        (strategy_id,),
    ).fetchall()

    positions = conn.execute(
        "SELECT symbol, qty, reserved_qty FROM positions WHERE strategy_id = ? AND qty > 0",
        (strategy_id,),
    ).fetchall()

    if not liquidate:
        if open_orders:
            msg = f"Cannot delete: strategy has {len(open_orders)} open order(s). Use --liquidate to force."
            json_output({"error": msg})
            typer.echo(f"Error: {msg}", err=True)
            raise typer.Exit(1)
        if positions:
            msg = f"Cannot delete: strategy has {len(positions)} open position(s). Use --liquidate to force."
            json_output({"error": msg})
            typer.echo(f"Error: {msg}", err=True)
            raise typer.Exit(1)

        if not yes:
            typer.confirm(
                f"Delete strategy '{name}'? This removes all history.",
                abort=True,
            )

        _purge_strategy(conn, strategy_id)
        msg = f"Strategy '{name}' deleted."
        if json_output({"status": "ok", "message": msg}):
            return
        typer.echo(msg)
        return

    # --liquidate path
    if not yes:
        parts = []
        if open_orders:
            parts.append(f"cancel {len(open_orders)} open order(s)")
        if positions:
            parts.append(f"market-sell {len(positions)} position(s)")
        action = " and ".join(parts) if parts else "delete"
        typer.confirm(
            f"This will {action} for strategy '{name}', then delete it. Proceed?",
            abort=True,
        )

    from nexus.broker import AlpacaBroker
    from nexus.ledger import process_cancel_pending
    from nexus.sync import sync_outstanding_orders

    broker = AlpacaBroker(broker_profile)
    cancelled_count = 0
    pending_count = 0
    closed_symbols: list[str] = []

    # Cancel open orders — verify broker confirms before marking cancelled.
    # On broker failure or still-open status, mark cancel_pending instead.
    for order in open_orders:
        broker_order_id = order["broker_order_id"]
        broker_error: str | None = None
        try:
            broker.cancel_order(broker_order_id)
        except RuntimeError as exc:
            broker_error = str(exc)

        if broker_error is not None:
            process_cancel_pending(conn, order["id"])
            pending_count += 1
            continue

        broker_confirmed = False
        try:
            broker_state = broker.get_order(broker_order_id)
            if broker_state.status in ("cancelled", "canceled", "expired"):
                broker_confirmed = True
        except RuntimeError:
            pass

        if broker_confirmed:
            process_cancel(conn, order["id"])
            cancelled_count += 1
        else:
            process_cancel_pending(conn, order["id"])
            pending_count += 1
    conn.commit()

    # Market sell each position
    for pos in positions:
        symbol = pos["symbol"]
        available = pos["qty"] - (pos["reserved_qty"] or 0)
        if available <= 0:
            continue

        client_order_id = f"nx-{name[:6]}-{symbol}-{uuid4().hex[:8]}"
        now = datetime.now(timezone.utc).isoformat()

        cur = conn.execute(
            "INSERT INTO orders"
            " (strategy_id, symbol, side, qty, order_type,"
            "  status, client_order_id, reserved_amount, filled_qty, actor, created_at, updated_at)"
            " VALUES (?, ?, 'sell', ?, 'market', 'pending', ?, ?, 0, 'cli:liquidate', ?, ?)",
            (strategy_id, symbol, available, client_order_id, float(available), now, now),
        )
        order_id = cur.lastrowid

        from nexus.ledger import reserve_shares
        reserve_shares(conn, strategy_id, symbol, available)
        conn.commit()

        try:
            result = broker.submit_order(symbol, available, "sell", "market", client_order_id=client_order_id)
            conn.execute(
                "UPDATE orders SET status = 'submitted', broker_order_id = ?, updated_at = ? WHERE id = ?",
                (result.broker_order_id, datetime.now(timezone.utc).isoformat(), order_id),
            )
            conn.commit()
            closed_symbols.append(symbol)
        except RuntimeError as exc:
            conn.execute(
                "UPDATE orders SET status = 'cancelled', updated_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), order_id),
            )
            from nexus.ledger import release_shares
            release_shares(conn, strategy_id, symbol, available)
            conn.commit()
            msg = f"Broker error selling {symbol}: {exc}. Strategy not deleted."
            json_output({"error": msg})
            typer.echo(f"Error: {msg}", err=True)
            raise typer.Exit(1)

    # Sync to pick up fills
    if closed_symbols:
        sync_outstanding_orders(conn, strategy_id, broker)

    # Verify all positions are flat
    remaining = conn.execute(
        "SELECT symbol, qty FROM positions WHERE strategy_id = ? AND qty > 0",
        (strategy_id,),
    ).fetchall()
    if remaining:
        symbols = [r["symbol"] for r in remaining]
        msg = f"Positions not fully closed: {', '.join(symbols)}. Retry or close manually."
        json_output({"error": msg})
        typer.echo(f"Error: {msg}", err=True)
        raise typer.Exit(1)

    _purge_strategy(conn, strategy_id)

    result_data = {
        "status": "ok",
        "message": f"Strategy '{name}' liquidated and deleted.",
        "liquidated": {
            "cancelled_orders": cancelled_count,
            "cancel_pending_orders": pending_count,
            "closed_positions": closed_symbols,
        },
    }
    if json_output(result_data):
        return
    typer.echo(f"Strategy '{name}' liquidated and deleted.")
    if cancelled_count:
        typer.echo(f"  Cancelled orders: {cancelled_count}")
    if pending_count:
        typer.echo(f"  Cancel-pending orders: {pending_count} (reconciler will retry)")
    if closed_symbols:
        typer.echo(f"  Closed positions: {', '.join(closed_symbols)}")


def _purge_strategy(conn, strategy_id: int) -> None:
    """Delete a strategy and all dependent rows."""
    conn.execute("DELETE FROM reservations WHERE strategy_id = ?", (strategy_id,))
    conn.execute("DELETE FROM transactions WHERE strategy_id = ?", (strategy_id,))
    conn.execute("DELETE FROM orders WHERE strategy_id = ?", (strategy_id,))
    conn.execute("DELETE FROM positions WHERE strategy_id = ?", (strategy_id,))
    conn.execute("DELETE FROM strategies WHERE id = ?", (strategy_id,))
    conn.commit()
