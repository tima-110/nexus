"""CLI application — typer app with all subcommands."""
from __future__ import annotations

import typer

app = typer.Typer(
    name="nexus",
    no_args_is_help=True,
    add_completion=False,
)

from nexus.cli.broker_cmd import broker_app  # noqa: E402
from nexus.cli.strategy import strategy_app  # noqa: E402
from nexus.cli.order import order_app  # noqa: E402

app.add_typer(broker_app)
app.add_typer(strategy_app)
app.add_typer(order_app)
