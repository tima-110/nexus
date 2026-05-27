"""Operational commands (history, etc.)."""
from __future__ import annotations

import typer

from nexus.cli import json_output
from nexus.db import get_connection, init_db

ops_app = typer.Typer(no_args_is_help=False)


@ops_app.callback(invoke_without_command=True)
def history(
    strategy: str | None = typer.Option(None, "--strategy", help="Filter by strategy name"),
    symbol: str | None = typer.Option(None, "--symbol", help="Filter by symbol"),
    since: str | None = typer.Option(None, "--since", help="Show only after this date (YYYY-MM-DD)"),
) -> None:
    """Show transaction history."""
    conn = get_connection()
    init_db(conn)

    query = (
        "SELECT t.created_at, t.type, t.amount, s.name AS strategy,"
        " o.symbol, t.actor, t.note"
        " FROM transactions t"
        " JOIN strategies s ON t.strategy_id = s.id"
        " LEFT JOIN orders o ON t.order_id = o.id"
        " WHERE 1=1"
    )
    params: list[str] = []

    if strategy:
        query += " AND s.name = ?"
        params.append(strategy)
    if symbol:
        query += " AND o.symbol = ?"
        params.append(symbol)
    if since:
        query += " AND t.created_at >= ?"
        params.append(since)

    query += " ORDER BY t.created_at DESC"

    rows = conn.execute(query, params).fetchall()

    if json_output({"items": [
        {
            "date": row["created_at"], "type": row["type"],
            "amount": row["amount"] or 0.0, "strategy": row["strategy"],
            "symbol": row["symbol"], "actor": row["actor"], "note": row["note"],
        }
        for row in rows
    ]}):
        return

    if not rows:
        typer.echo("No transactions found.")
        return

    header = (
        f"{'DATE':<26} {'TYPE':<14} {'AMOUNT':>12} {'STRATEGY':<20}"
        f" {'SYMBOL':<10} {'ACTOR':<20} NOTE"
    )
    typer.echo(header)
    typer.echo("-" * len(header))

    for row in rows:
        date = row["created_at"] or ""
        tx_type = row["type"] or ""
        amount = row["amount"] or 0.0
        strat = row["strategy"] or ""
        sym = row["symbol"] or ""
        actor = row["actor"] or ""
        note = row["note"] or ""
        if len(note) > 30:
            note = note[:27] + "..."
        amount_str = f"{amount:+.2f}"
        typer.echo(
            f"{date:<26} {tx_type:<14} {amount_str:>12} {strat:<20}"
            f" {sym:<10} {actor:<20} {note}"
        )
