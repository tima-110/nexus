"""Strategy management commands."""
from __future__ import annotations

from datetime import datetime, timezone

import typer

from nexus.db import get_connection, init_db

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
        typer.echo(f"Error: broker account '{broker}' not found.", err=True)
        raise typer.Exit(1)

    broker_account_id = broker_row["id"]

    try:
        conn.execute(
            "INSERT INTO strategies (name, broker_account_id, cash_balance, is_active, created_at)"
            " VALUES (?, ?, ?, 1, ?)",
            (name, broker_account_id, balance, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

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
