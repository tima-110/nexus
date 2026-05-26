"""Append-only JSONL audit logger."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def log_event(path: Path, event: dict) -> None:
    """Append a JSON line to the audit log. Auto-sets 'ts' field to current UTC ISO timestamp."""
    event["ts"] = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(event) + "\n")
        f.flush()
