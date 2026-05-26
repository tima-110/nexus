"""Configuration loading for Nexus."""

from __future__ import annotations

import tomllib
from pathlib import Path

from platformdirs import user_config_path, user_data_path
from pydantic import BaseModel, Field


_DEFAULT_CONFIG = """\
[reconciler]
interval_minutes = 5
market_hours_only = false

[order]
slippage_buffer_percent = 1.5
market_order_wait_seconds = 5

[database]
path = "~/.local/share/nexus/nexus.db"

[audit_log]
path = "~/.local/share/nexus/audit.jsonl"
"""


class ReconcilerConfig(BaseModel):
    interval_minutes: int = 5
    market_hours_only: bool = False


class OrderConfig(BaseModel):
    slippage_buffer_percent: float = 1.5
    market_order_wait_seconds: int = 5


class DatabaseConfig(BaseModel):
    path: str = "~/.local/share/nexus/nexus.db"


class AuditLogConfig(BaseModel):
    path: str = "~/.local/share/nexus/audit.jsonl"


class NexusConfig(BaseModel):
    reconciler: ReconcilerConfig = Field(default_factory=ReconcilerConfig)
    order: OrderConfig = Field(default_factory=OrderConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    audit_log: AuditLogConfig = Field(default_factory=AuditLogConfig)


def _config_file_path() -> Path:
    return user_config_path("nexus") / "config.toml"


def load_config() -> NexusConfig:
    """Load configuration from file, creating it with defaults if absent."""
    config_path = _config_file_path()

    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(_DEFAULT_CONFIG, encoding="utf-8")
        return NexusConfig()

    with config_path.open("rb") as fh:
        data = tomllib.load(fh)

    sections: dict = {}
    if "reconciler" in data:
        sections["reconciler"] = ReconcilerConfig(**data["reconciler"])
    if "order" in data:
        sections["order"] = OrderConfig(**data["order"])
    if "database" in data:
        sections["database"] = DatabaseConfig(**data["database"])
    if "audit_log" in data:
        sections["audit_log"] = AuditLogConfig(**data["audit_log"])

    return NexusConfig(**sections)


def get_db_path(config: NexusConfig | None = None) -> Path:
    """Return the resolved database path, creating parent directories as needed."""
    if config is None:
        config = load_config()
    path = Path(config.database.path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_audit_path(config: NexusConfig | None = None) -> Path:
    """Return the resolved audit log path, creating parent directories as needed."""
    if config is None:
        config = load_config()
    path = Path(config.audit_log.path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
