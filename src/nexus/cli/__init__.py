"""CLI application — typer app with all subcommands."""
from __future__ import annotations

import typer

app = typer.Typer(
    name="nexus",
    no_args_is_help=True,
    add_completion=False,
)
