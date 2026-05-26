"""Position management commands."""
from __future__ import annotations

import typer

from nexus.broker import AlpacaBroker
from nexus.db import get_connection, init_db

position_app = typer.Typer(name="position", no_args_is_help=True)


@position_app.command("list")
def position_list(
    strategy: str | None = typer.Option(None, "--strategy", help="Filter by strategy"),
) -> None:
    """List open positions across all strategies (or a single strategy)."""
    conn = get_connection()
    init_db(conn)

    query = (
        "SELECT p.*, s.name AS strategy_name"
        " FROM positions p"
        " JOIN strategies s ON p.strategy_id = s.id"
        " WHERE p.qty > 0"
    )
    params: list = []

    if strategy is not None:
        query += " AND s.name = ?"
        params.append(strategy)

    query += " ORDER BY s.name, p.symbol"

    rows = conn.execute(query, params).fetchall()

    if not rows:
        typer.echo("No open positions.")
        return

    header = (
        f"{'STRATEGY':<20} {'SYMBOL':<8} {'QTY':>8} {'RESERVED':>10}"
        f" {'AVAILABLE':>10} {'AVG_ENTRY':>12} {'VALUE':>14}"
    )
    typer.echo(header)
    typer.echo("-" * len(header))
    for row in rows:
        qty = row["qty"] or 0
        reserved = row["reserved_qty"] or 0
        available = qty - reserved
        avg_entry = row["avg_entry_price"] or 0.0
        value = qty * avg_entry
        typer.echo(
            f"{row['strategy_name']:<20} {row['symbol']:<8} {qty:>8}"
            f" {reserved:>10} {available:>10} {avg_entry:>12.4f} {value:>14.2f}"
        )


@position_app.command("show")
def position_show(
    strategy: str = typer.Argument(..., help="Strategy name"),
    symbol: str = typer.Argument(..., help="Ticker symbol"),
) -> None:
    """Show details for a single position including live price if available."""
    conn = get_connection()
    init_db(conn)

    strat = conn.execute(
        "SELECT s.*, b.profile_name AS broker"
        " FROM strategies s"
        " JOIN broker_accounts b ON s.broker_account_id = b.id"
        " WHERE s.name = ?",
        (strategy,),
    ).fetchone()
    if strat is None:
        typer.echo(f"Error: strategy '{strategy}' not found.", err=True)
        raise typer.Exit(1)

    strategy_id: int = strat["id"]
    broker_profile: str = strat["broker"]

    position = conn.execute(
        "SELECT * FROM positions WHERE strategy_id = ? AND symbol = ?",
        (strategy_id, symbol),
    ).fetchone()
    if position is None or (position["qty"] or 0) <= 0:
        typer.echo(f"No position in {symbol} for strategy '{strategy}'", err=True)
        raise typer.Exit(1)

    qty = position["qty"] or 0
    reserved = position["reserved_qty"] or 0
    available = qty - reserved
    avg_entry = position["avg_entry_price"] or 0.0
    cost_basis = qty * avg_entry

    typer.echo(f"Strategy:    {strategy}")
    typer.echo(f"Symbol:      {symbol}")
    typer.echo(f"Quantity:    {qty}")
    typer.echo(f"Reserved:    {reserved}")
    typer.echo(f"Available:   {available}")
    typer.echo(f"Avg entry:   ${avg_entry:.4f}")
    typer.echo(f"Cost basis:  ${cost_basis:.2f}")

    broker = AlpacaBroker(broker_profile)
    try:
        current_price = broker.get_last_price(symbol)
        market_value = qty * float(current_price)
        unrealized_pnl = market_value - cost_basis
        typer.echo(f"Current px:  ${float(current_price):.4f}")
        typer.echo(f"Mkt value:   ${market_value:.2f}")
        typer.echo(f"Unreal P&L:  ${unrealized_pnl:.2f}")
    except RuntimeError:
        typer.echo("Current px:  (Live price unavailable)")

    open_orders = conn.execute(
        "SELECT id, side, qty, order_type, status FROM orders"
        " WHERE strategy_id = ? AND symbol = ? AND status IN ('submitted', 'partially_filled')",
        (strategy_id, symbol),
    ).fetchall()

    if open_orders:
        typer.echo("")
        ord_header = f"{'ID':<8} {'SIDE':<6} {'QTY':>8} {'TYPE':<14} {'STATUS'}"
        typer.echo(ord_header)
        typer.echo("-" * len(ord_header))
        for o in open_orders:
            typer.echo(
                f"{o['id']:<8} {o['side']:<6} {o['qty']:>8}"
                f" {o['order_type']:<14} {o['status']}"
            )
