"""Broker account management commands."""
from __future__ import annotations

from datetime import datetime, timezone

import typer

from nexus.broker import AlpacaBroker
from nexus.cli import json_output
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
        json_output({"error": str(exc)})
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    if json_output({"status": "ok", "profile_name": profile_name}):
        return
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

    if json_output({"items": [
        {"id": r["id"], "profile_name": r["profile_name"], "margin_multiplier": r["margin_multiplier"], "cash_balance": r["cash_balance"], "last_synced_at": r["last_synced_at"]}
        for r in rows
    ]}):
        return

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


@broker_app.command("show")
def broker_show(
    profile_name: str = typer.Argument(..., help="Broker profile name"),
) -> None:
    """Show details for a broker account."""
    conn = get_connection()
    init_db(conn)

    row = conn.execute(
        "SELECT * FROM broker_accounts WHERE profile_name = ?",
        (profile_name,),
    ).fetchone()

    if row is None:
        json_output({"error": f"Broker account '{profile_name}' not found"})
        typer.echo(f"Error: Broker account '{profile_name}' not found.", err=True)
        raise typer.Exit(1)

    # Collect live data for JSON output
    live_account = None
    live_positions = None
    try:
        broker = AlpacaBroker(profile_name)
        account = broker.get_account()
        positions = broker.get_positions()
        live_account = {"cash": account.cash, "buying_power": account.buying_power, "equity": account.equity}
        live_positions = [
            {"symbol": p.symbol, "qty": p.qty, "avg_entry_price": p.avg_entry_price, "current_price": p.current_price, "unrealized_pl": p.unrealized_pl}
            for p in positions
        ]
    except RuntimeError:
        pass

    strategies = conn.execute(
        "SELECT name, cash_balance FROM strategies WHERE broker_account_id = ?",
        (row["id"],),
    ).fetchall()

    if json_output({
        "profile_name": row["profile_name"],
        "margin_multiplier": row["margin_multiplier"],
        "cached_cash": row["cash_balance"],
        "last_synced_at": row["last_synced_at"],
        "live_account": live_account,
        "live_positions": live_positions,
        "strategies": [{"name": s["name"], "cash_balance": s["cash_balance"]} for s in strategies],
    }):
        return

    typer.echo(f"Profile:           {row['profile_name']}")
    typer.echo(f"Margin multiplier: {row['margin_multiplier']:.2f}")
    typer.echo(f"Cached cash:       ${row['cash_balance']:.2f}")
    typer.echo(f"Last synced:       {row['last_synced_at'] or 'never'}")

    if live_account is not None:
        typer.echo("")
        typer.echo("Live account:")
        typer.echo(f"  Cash:         ${account.cash:.2f}")
        typer.echo(f"  Buying power: ${account.buying_power:.2f}")
        typer.echo(f"  Equity:       ${account.equity:.2f}")
        if live_positions:
            typer.echo("")
            typer.echo("Live positions:")
            typer.echo(f"  {'SYMBOL':<10} {'QTY':>8} {'AVG_ENTRY':>12} {'CUR_PRICE':>12} {'UNREAL_PL':>12}")
            typer.echo("  " + "-" * 56)
            for pos in positions:
                typer.echo(
                    f"  {pos.symbol:<10} {pos.qty:>8}"
                    f" {pos.avg_entry_price:>12.2f}"
                    f" {pos.current_price:>12.2f}"
                    f" {pos.unrealized_pl:>12.2f}"
                )
        else:
            typer.echo("  (no open positions)")
    else:
        typer.echo("(Live data unavailable)")

    typer.echo("")
    if strategies:
        typer.echo("Attached strategies:")
        typer.echo(f"  {'NAME':<20} {'CASH_BAL':>12}")
        typer.echo("  " + "-" * 33)
        for s in strategies:
            typer.echo(f"  {s['name']:<20} {s['cash_balance']:>12.2f}")
    else:
        typer.echo("Attached strategies: (none)")


@broker_app.command("sync")
def broker_sync(
    profile_name: str | None = typer.Argument(None, help="Broker profile (omit for all)"),
) -> None:
    """Sync cash balance from the broker (one or all)."""
    conn = get_connection()
    init_db(conn)

    if profile_name is not None:
        row = conn.execute(
            "SELECT id, profile_name FROM broker_accounts WHERE profile_name = ?",
            (profile_name,),
        ).fetchone()
        if row is None:
            json_output({"error": f"Broker account '{profile_name}' not found"})
            typer.echo(f"Error: Broker account '{profile_name}' not found.", err=True)
            raise typer.Exit(1)
        brokers = [row]
    else:
        brokers = conn.execute(
            "SELECT id, profile_name FROM broker_accounts ORDER BY id"
        ).fetchall()

    if not brokers:
        if json_output({"items": [], "errors": []}):
            return
        typer.echo("No broker accounts registered.")
        return

    synced_items: list[dict] = []
    errors: list[str] = []
    for broker_row in brokers:
        profile = broker_row["profile_name"]
        try:
            account = AlpacaBroker(profile).get_account()
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE broker_accounts SET cash_balance = ?, last_synced_at = ? WHERE id = ?",
                (float(account.cash), now, broker_row["id"]),
            )
            conn.commit()
            synced_items.append({"profile": profile, "cash": float(account.cash)})
        except RuntimeError as exc:
            errors.append(f"{profile}: {exc}")

    if json_output({"items": synced_items, "errors": errors}):
        return

    for item in synced_items:
        typer.echo(f"Synced '{item['profile']}': cash=${item['cash']:.2f}")
    for err in errors:
        typer.echo(f"Error syncing {err}", err=True)


@broker_app.command("remove")
def broker_remove(
    profile_name: str = typer.Argument(..., help="Broker profile to remove"),
) -> None:
    """Remove a registered broker account."""
    conn = get_connection()
    init_db(conn)

    row = conn.execute(
        "SELECT id FROM broker_accounts WHERE profile_name = ?",
        (profile_name,),
    ).fetchone()

    if row is None:
        json_output({"error": f"Broker account '{profile_name}' not found"})
        typer.echo(f"Error: Broker account '{profile_name}' not found.", err=True)
        raise typer.Exit(1)

    attached = conn.execute(
        "SELECT name FROM strategies WHERE broker_account_id = ?",
        (row["id"],),
    ).fetchall()

    if attached:
        names = ", ".join(s["name"] for s in attached)
        json_output({"error": f"Cannot remove '{profile_name}': strategies still attached: {names}"})
        typer.echo(
            f"Error: Cannot remove '{profile_name}': strategies still attached: {names}",
            err=True,
        )
        raise typer.Exit(1)

    conn.execute("DELETE FROM broker_accounts WHERE id = ?", (row["id"],))
    conn.commit()
    if json_output({"status": "ok", "profile_name": profile_name}):
        return
    typer.echo(f"Broker account '{profile_name}' removed.")
