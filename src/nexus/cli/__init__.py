"""CLI application — typer app with all subcommands."""
from __future__ import annotations

import json as _json

import typer

app = typer.Typer(
    name="nexus",
    no_args_is_help=True,
    add_completion=False,
)

_json_output: bool = False


@app.callback()
def main(
    json_flag: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Nexus — multi-strategy portfolio manager."""
    global _json_output
    _json_output = json_flag


def json_output(data) -> bool:
    """If JSON mode is active, print data as JSON and return True. Otherwise return False."""
    if _json_output:
        typer.echo(_json.dumps(data, default=str))
        return True
    return False

from nexus.cli.broker_cmd import broker_app  # noqa: E402
from nexus.cli.strategy import strategy_app  # noqa: E402
from nexus.cli.order import order_app  # noqa: E402
from nexus.cli.position import position_app  # noqa: E402
from nexus.cli.ops import ops_app  # noqa: E402
from nexus.cli.config_cmd import config_app  # noqa: E402

app.add_typer(broker_app)
app.add_typer(strategy_app)
app.add_typer(order_app)
app.add_typer(position_app)
app.add_typer(ops_app, name="history")
app.add_typer(config_app)


@app.command()
def reconcile(
    dry_run: bool = typer.Option(False, "--dry-run", help="Report without making changes"),
) -> None:
    """Run the reconciliation sweep."""
    from nexus.config import load_config
    from nexus.db import get_connection, init_db
    from nexus.reconciler import run_reconcile

    config = load_config()
    conn = get_connection()
    init_db(conn)

    result = run_reconcile(conn, config, dry_run=dry_run)

    if json_output({
        "orders_synced": result.orders_synced,
        "orders_skipped": result.orders_skipped,
        "orphans_cleaned": result.orphans_cleaned,
        "bypass_orders": result.bypass_orders,
        "balance_drift": result.balance_drift,
        "errors": result.errors,
        "dry_run": dry_run,
    }):
        if result.errors:
            raise typer.Exit(1)
        return

    if dry_run:
        typer.echo("[DRY RUN]")

    typer.echo(f"Orders synced:   {result.orders_synced}")
    typer.echo(f"Orders skipped:  {result.orders_skipped}")
    typer.echo(f"Orphans cleaned: {result.orphans_cleaned}")

    if result.bypass_orders:
        typer.echo(f"\nBypass orders detected ({len(result.bypass_orders)}):")
        for oid in result.bypass_orders:
            typer.echo(f"  {oid}")

    if result.balance_drift:
        typer.echo("\nBalance drift:")
        for profile, drift in result.balance_drift.items():
            typer.echo(f"  {profile}: ${drift:.2f}")

    if result.errors:
        typer.echo("\nErrors:", err=True)
        for err in result.errors:
            typer.echo(f"  {err}", err=True)
        raise typer.Exit(1)


@app.command()
def install(
    interval: int = typer.Option(None, "--interval", help="Interval in minutes (default from config)"),
) -> None:
    """Install the reconciler cron schedule."""
    from nexus.config import load_config
    from nexus.schedule.cron import install_schedule

    config = load_config()
    minutes = interval if interval is not None else config.reconciler.interval_minutes

    try:
        expression = install_schedule(minutes)
    except RuntimeError as exc:
        if json_output({"error": str(exc)}):
            raise typer.Exit(1)
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    if json_output({"status": "ok", "schedule": expression}):
        return
    typer.echo(f"Reconciler installed: {expression}")


@app.command()
def uninstall() -> None:
    """Remove the reconciler cron schedule."""
    from nexus.schedule.cron import uninstall_schedule

    removed = uninstall_schedule()
    if json_output({"status": "ok", "removed": removed}):
        return
    if removed:
        typer.echo("Reconciler cron schedule removed.")
    else:
        typer.echo("No reconciler cron schedule found.")


@app.command()
def status() -> None:
    """Show reconciler cron schedule status."""
    from nexus.schedule.cron import get_schedule_status

    info = get_schedule_status()
    if json_output({
        "installed": info["installed"],
        "schedule": info.get("schedule"),
        "command": info.get("command"),
    }):
        return
    if info["installed"]:
        typer.echo(f"Status:   installed")
        typer.echo(f"Schedule: {info['schedule']}")
        typer.echo(f"Command:  {info['command']}")
    else:
        typer.echo("Status: not installed")


@app.command()
def doctor() -> None:
    """Run health checks on the Nexus system."""
    from nexus.config import load_config
    from nexus.db import get_connection, init_db
    from nexus.doctor import run_doctor

    config = load_config()
    conn = get_connection()
    init_db(conn)

    checks = run_doctor(conn, config)

    all_passed = all(c.passed for c in checks)
    if json_output({
        "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in checks],
        "all_passed": all_passed,
    }):
        if not all_passed:
            raise typer.Exit(1)
        return

    for check in checks:
        icon = "PASS" if check.passed else "FAIL"
        typer.echo(f"  [{icon}] {check.name}: {check.detail}")

    typer.echo("")
    if all_passed:
        typer.echo("All checks passed.")
    else:
        typer.echo("Some checks failed.")
        raise typer.Exit(1)
