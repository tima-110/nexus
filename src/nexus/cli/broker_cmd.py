"""Broker account management commands."""
from __future__ import annotations

from datetime import datetime, timezone

import typer

from nexus.db import get_connection, init_db

broker_app = typer.Typer(name="broker", no_args_is_help=True)


@broker_app.command("add")
def broker_add(
    profile_name: str = typer.Argument(..., help="Alpaca CLI profile name"),
    margin_multiplier: float = typer.Option(2.0, "--margin-multiplier", help="Margin multiplier"),
) -> None:
    """Register an Alpaca CLI profile as a broker account."""
    conn = get_connection()
    init_db(conn)

    try:
        conn.execute(
            "INSERT INTO broker_accounts (profile_name, margin_multiplier, cash_balance, last_synced_at)"
            " VALUES (?, ?, 0.0, ?)",
            (profile_name, margin_multiplier, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Broker account '{profile_name}' registered (margin_multiplier={margin_multiplier}).")


@broker_app.command("list")
def broker_list() -> None:
    """List all registered broker accounts."""
    conn = get_connection()
    init_db(conn)

    rows = conn.execute(
        "SELECT id, profile_name, margin_multiplier, cash_balance, last_synced_at"
        " FROM broker_accounts ORDER BY id"
    ).fetchall()

    if not rows:
        typer.echo("No broker accounts registered.")
        return

    header = f"{'ID':<5} {'PROFILE':<20} {'MARGIN':>8} {'CASH_BAL':>12} {'LAST_SYNCED'}"
    typer.echo(header)
    typer.echo("-" * len(header))
    for row in rows:
        synced = row["last_synced_at"] or "never"
        typer.echo(
            f"{row['id']:<5} {row['profile_name']:<20}"
            f" {row['margin_multiplier']:>8.2f}"
            f" {row['cash_balance']:>12.2f}"
            f" {synced}"
        )
