"""Config management commands."""
from __future__ import annotations

import tomllib
from pathlib import Path

import typer

from nexus.cli import json_output
from nexus.config import NexusConfig, _config_file_path, _DEFAULT_CONFIG, load_config

config_app = typer.Typer(name="config", no_args_is_help=True)


def _write_toml(data: dict, path: Path) -> None:
    """Write a simple flat-section TOML file from a dict of dicts."""
    lines: list[str] = []
    for section, values in data.items():
        lines.append(f"[{section}]")
        for key, val in values.items():
            if isinstance(val, bool):
                lines.append(f"{key} = {'true' if val else 'false'}")
            elif isinstance(val, int):
                lines.append(f"{key} = {val}")
            elif isinstance(val, float):
                lines.append(f"{key} = {val}")
            elif isinstance(val, str):
                lines.append(f'{key} = "{val}"')
            else:
                lines.append(f'{key} = "{val}"')
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _cast_value(value: str) -> int | float | bool | str:
    """Auto-cast a string value to the most specific type."""
    # Try int first
    try:
        return int(value)
    except ValueError:
        pass
    # Try float
    try:
        return float(value)
    except ValueError:
        pass
    # Try bool
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    # Fall back to string
    return value


@config_app.command("show")
def config_show() -> None:
    """Show the current configuration."""
    config_path = _config_file_path()

    if json_output(load_config().model_dump()):
        return

    if config_path.exists():
        typer.echo(config_path.read_text(encoding="utf-8"))
    else:
        typer.echo(_DEFAULT_CONFIG)


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Dotted key (e.g. reconciler.interval_minutes)"),
    value: str = typer.Argument(..., help="New value"),
) -> None:
    """Set a configuration value."""
    config_path = _config_file_path()

    # Parse dotted key
    parts = key.split(".")
    if len(parts) != 2:
        if json_output({"error": f"Key must be section.field (got '{key}')"}):
            raise typer.Exit(1)
        typer.echo(f"Error: Key must be section.field (got '{key}')", err=True)
        raise typer.Exit(1)

    section, field = parts

    # Load current data
    if config_path.exists():
        with config_path.open("rb") as fh:
            data = tomllib.load(fh)
    else:
        data = tomllib.loads(_DEFAULT_CONFIG)

    # Verify section exists
    if section not in data:
        if json_output({"error": f"Unknown section '{section}'"}):
            raise typer.Exit(1)
        typer.echo(f"Error: Unknown section '{section}'", err=True)
        raise typer.Exit(1)

    # Verify field exists
    if field not in data[section]:
        if json_output({"error": f"Unknown field '{field}' in section '{section}'"}):
            raise typer.Exit(1)
        typer.echo(f"Error: Unknown field '{field}' in section '{section}'", err=True)
        raise typer.Exit(1)

    # Cast value
    new_value = _cast_value(value)

    # Save original for revert
    original_data_text = config_path.read_text(encoding="utf-8") if config_path.exists() else None

    # Update
    data[section][field] = new_value

    # Write
    config_path.parent.mkdir(parents=True, exist_ok=True)
    _write_toml(data, config_path)

    # Validate by re-loading
    try:
        load_config()
    except (RuntimeError, Exception) as exc:
        # Revert
        if original_data_text is not None:
            config_path.write_text(original_data_text, encoding="utf-8")
        else:
            config_path.unlink(missing_ok=True)
        if json_output({"error": f"Validation failed: {exc}"}):
            raise typer.Exit(1)
        typer.echo(f"Error: Validation failed: {exc}", err=True)
        raise typer.Exit(1)

    if json_output({"status": "ok", "key": key, "value": new_value}):
        return
    typer.echo(f"Set {key} = {new_value}")


@config_app.command("path")
def config_path_cmd() -> None:
    """Show the config file path."""
    path = _config_file_path()
    if json_output({"path": str(path)}):
        return
    typer.echo(str(path))
