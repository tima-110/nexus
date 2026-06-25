"""Position management commands."""
from __future__ import annotations

import typer

from nexus.broker import AlpacaBroker
from nexus.cli import json_output
from nexus.db import get_connection, init_db
from nexus.occ import is_occ_symbol, parse_occ_symbol

position_app = typer.Typer(name="position", no_args_is_help=True)


@position_app.command("list")
def position_list(
    strategy: str | None = typer.Option(None, "--strategy", help="Filter by strategy"),
) -> None:
    """List open positions across all strategies (or a single strategy).

    Shows both equity and option positions. Options are identified by
    their OCC symbol format and include right, strike, and expiry.
    """
    conn = get_connection()
    init_db(conn)

    # Fetch equity positions
    eq_query = (
        "SELECT p.*, s.name AS strategy_name"
        " FROM positions p"
        " JOIN strategies s ON p.strategy_id = s.id"
        " WHERE p.qty > 0"
    )
    eq_params: list = []
    if strategy is not None:
        eq_query += " AND s.name = ?"
        eq_params.append(strategy)
    eq_query += " ORDER BY s.name, p.symbol"

    eq_rows = conn.execute(eq_query, eq_params).fetchall()

    # Fetch option positions
    opt_query = (
        "SELECT op.*, s.name AS strategy_name"
        " FROM option_positions op"
        " JOIN strategies s ON op.strategy_id = s.id"
        " WHERE op.qty > 0"
    )
    opt_params: list = []
    if strategy is not None:
        opt_query += " AND s.name = ?"
        opt_params.append(strategy)
    opt_query += " ORDER BY s.name, op.underlying, op.expiry"

    opt_rows = conn.execute(opt_query, opt_params).fetchall()

    # Build combined items
    items: list[dict] = []

    for r in eq_rows:
        items.append({
            "strategy": r["strategy_name"],
            "symbol": r["symbol"],
            "asset_class": "equity",
            "qty": r["qty"] or 0,
            "reserved_qty": r["reserved_qty"] or 0,
            "available": (r["qty"] or 0) - (r["reserved_qty"] or 0),
            "avg_entry_price": r["avg_entry_price"],
        })

    for r in opt_rows:
        items.append({
            "strategy": r["strategy_name"],
            "symbol": r["symbol"],
            "asset_class": "option",
            "underlying": r["underlying"],
            "right": r["option_right"],
            "side": r["side"],
            "qty": r["qty"] or 0,
            "strike": r["strike"],
            "expiry": r["expiry"],
            "avg_entry_price": r["avg_entry_price"],
        })

    # Sort by strategy name, then underlying for options
    items.sort(key=lambda x: (x["strategy"], x.get("underlying", x["symbol"]), x.get("expiry", "")))

    if json_output({"items": items}):
        return

    if not items:
        typer.echo("No open positions.")
        return

    # Print header
    typer.echo(f"{'STRATEGY':<16} {'SYMBOL/UNDERLYING':<24} {'RIGHT':<6} {'SD':<5} {'QTY':>5} {'STRIKE':>8} {'EXPIRY':<12} {'AVG_PREM':>10} {'VALUE':>12} {'TYPE':<7}")
    typer.echo("-" * 105)

    for item in items:
        if item["asset_class"] == "equity":
            qty = item["qty"]
            reserved = item["reserved_qty"]
            available = item["available"]
            avg_entry = item["avg_entry_price"] or 0.0
            value = qty * avg_entry
            typer.echo(
                f"{item['strategy']:<16} {item['symbol']:<24} {'':6} {'':5}"
                f" {qty:>5} {'':>8} {'':<12} {avg_entry:>10.4f} {value:>12.2f} equity"
            )
        else:
            qty = item["qty"]
            avg_prem = item["avg_entry_price"] or 0.0
            strike = item["strike"]
            expiry = item["expiry"]
            right = item["right"]
            side = item["side"][0]  # "S" or "L"
            display_symbol = f"{item['underlying']} {expiry} {right.upper()}"
            value = qty * avg_prem * 100
            typer.echo(
                f"{item['strategy']:<16} {display_symbol:<24} {right:<6} {side:<5}"
                f" {qty:>5} ${strike:>6.1f} {expiry:<12} ${avg_prem:>7.2f} ${value:>9.2f} option"
            )


@position_app.command("show")
def position_show(
    strategy: str = typer.Argument(..., help="Strategy name"),
    symbol: str = typer.Argument(..., help="Ticker symbol (equity or OCC option symbol)"),
) -> None:
    """Show details for a single position including live price if available.

    Accepts both equity symbols and OCC option symbols. For options,
    shows right, strike, expiry, premium, and current price.
    """
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
        json_output({"error": f"strategy '{strategy}' not found"})
        typer.echo(f"Error: strategy '{strategy}' not found.", err=True)
        raise typer.Exit(1)

    strategy_id: int = strat["id"]
    broker_profile: str = strat["broker"]

    # Check if this is an option symbol
    if is_occ_symbol(symbol):
        _show_option_position(conn, strategy_id, strategy, broker_profile, symbol)
        return

    # Equity position
    position = conn.execute(
        "SELECT * FROM positions WHERE strategy_id = ? AND symbol = ?",
        (strategy_id, symbol),
    ).fetchone()
    if position is None or (position["qty"] or 0) <= 0:
        json_output({"error": f"No position in {symbol} for strategy '{strategy}'"})
        typer.echo(f"Error: No position in {symbol} for strategy '{strategy}'.", err=True)
        raise typer.Exit(1)

    qty = position["qty"] or 0
    reserved = position["reserved_qty"] or 0
    available = qty - reserved
    avg_entry = position["avg_entry_price"]
    cost_basis = qty * (avg_entry or 0.0)

    live_price = None
    broker = AlpacaBroker(broker_profile)
    try:
        current_price = broker.get_last_price(symbol)
        live_price = float(current_price)
    except RuntimeError:
        pass

    open_orders = conn.execute(
        "SELECT id, side, qty, order_type, status FROM orders"
        " WHERE strategy_id = ? AND symbol = ? AND status IN ('submitted', 'partially_filled')",
        (strategy_id, symbol),
    ).fetchall()

    if json_output({
        "strategy": strategy, "symbol": symbol, "asset_class": "equity",
        "qty": qty, "reserved_qty": reserved, "available": available,
        "avg_entry_price": avg_entry, "live_price": live_price,
        "open_orders": [
            {"id": o["id"], "side": o["side"], "qty": o["qty"], "order_type": o["order_type"], "status": o["status"]}
            for o in open_orders
        ],
    }):
        return

    typer.echo(f"Strategy:    {strategy}")
    typer.echo(f"Symbol:      {symbol}")
    typer.echo(f"Asset class: equity")
    typer.echo(f"Quantity:    {qty}")
    typer.echo(f"Reserved:    {reserved}")
    typer.echo(f"Available:   {available}")
    typer.echo(f"Avg entry:   ${(avg_entry or 0.0):.4f}")
    typer.echo(f"Cost basis:  ${cost_basis:.2f}")

    if live_price is not None:
        market_value = qty * live_price
        unrealized_pnl = market_value - cost_basis
        typer.echo(f"Current px:  ${live_price:.4f}")
        typer.echo(f"Mkt value:   ${market_value:.2f}")
        typer.echo(f"Unreal P&L:  ${unrealized_pnl:.2f}")
    else:
        typer.echo("Current px:  (Live price unavailable)")

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


def _show_option_position(
    conn, strategy_id: int, strategy_name: str, broker_profile: str, symbol: str,
) -> None:
    """Show detail for an option position."""
    parsed = parse_occ_symbol(symbol)
    opt_pos = conn.execute(
        "SELECT * FROM option_positions WHERE strategy_id = ? AND symbol = ? AND qty > 0",
        (strategy_id, symbol),
    ).fetchone()

    if opt_pos is None or (opt_pos["qty"] or 0) <= 0:
        json_output({"error": f"No option position in {symbol} for strategy '{strategy_name}'"})
        typer.echo(f"Error: No option position in {symbol} for strategy '{strategy_name}'.", err=True)
        raise typer.Exit(1)

    qty = opt_pos["qty"] or 0
    side = opt_pos["side"]
    right = opt_pos["option_right"]
    strike = opt_pos["strike"]
    expiry = opt_pos["expiry"]
    underlying = opt_pos["underlying"]
    avg_entry = opt_pos["avg_entry_price"] or 0.0

    premium_collected = avg_entry * qty * 100 if side == "short" else -avg_entry * qty * 100

    # Try to get live price from broker
    live_premium = None
    broker = AlpacaBroker(broker_profile)
    try:
        from nexus.broker.alpaca import AlpacaBroker
        broker = AlpacaBroker(broker_profile)
        opt_positions = broker.list_option_positions()
        for p in opt_positions:
            if p.symbol == symbol:
                live_premium = float(p.current_price)
                break
    except RuntimeError:
        pass

    open_orders = conn.execute(
        "SELECT id, side, qty, order_type, status FROM orders"
        " WHERE strategy_id = ? AND symbol = ? AND status IN ('submitted', 'partially_filled')",
        (strategy_id, symbol),
    ).fetchall()

    # Unrealized P&L
    if live_premium is not None:
        if side == "short":
            unrealized_pl = (avg_entry - live_premium) * qty * 100
        else:
            unrealized_pl = (live_premium - avg_entry) * qty * 100
    else:
        unrealized_pl = None

    if json_output({
        "strategy": strategy_name, "symbol": symbol, "asset_class": "option",
        "underlying": underlying, "right": right, "side": side,
        "qty": qty, "strike": strike, "expiry": expiry,
        "avg_entry_price": avg_entry,
        "premium_collected_or_paid": premium_collected,
        "live_premium": live_premium,
        "unrealized_pl": unrealized_pl,
        "open_orders": [
            {"id": o["id"], "side": o["side"], "qty": o["qty"], "order_type": o["order_type"], "status": o["status"]}
            for o in open_orders
        ],
    }):
        return

    typer.echo(f"Strategy:    {strategy_name}")
    typer.echo(f"Symbol:      {symbol}")
    typer.echo(f"Asset class: option")
    typer.echo(f"Underlying:  {underlying}")
    typer.echo(f"Right:       {right.upper()}")
    typer.echo(f"Side:        {side}")
    typer.echo(f"Contracts:   {qty}")
    typer.echo(f"Strike:      ${strike:.2f}")
    typer.echo(f"Expiry:      {expiry}")
    typer.echo(f"Avg premium: ${avg_entry:.4f}")
    typer.echo(f"Net premium: ${premium_collected:.2f} ({'collected' if side == 'short' else 'paid'})")

    if live_premium is not None:
        typer.echo(f"Live prem:   ${live_premium:.4f}")
        if unrealized_pl is not None:
            typer.echo(f"Unreal P&L:  ${unrealized_pl:.2f}")
    else:
        typer.echo("Live prem:   (unavailable)")

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
