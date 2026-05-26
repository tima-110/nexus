"""Shared test fixtures."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from nexus.db import init_db


@pytest.fixture
def conn():
    """In-memory SQLite connection with schema initialized."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_db(c)
    return c


@pytest.fixture
def sample_broker(conn):
    """Insert a broker account, return its id."""
    conn.execute(
        "INSERT INTO broker_accounts (profile_name, margin_multiplier, cash_balance) VALUES (?, ?, ?)",
        ("paper1", 2.0, 100000.0),
    )
    conn.commit()
    return 1  # first inserted row


@pytest.fixture
def sample_strategy(conn, sample_broker):
    """Insert a strategy with $10000 balance, return its id."""
    conn.execute(
        "INSERT INTO strategies (name, broker_account_id, cash_balance, is_active, created_at) VALUES (?, ?, ?, ?, ?)",
        ("test_strat", sample_broker, 10000.0, 1, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return 1
