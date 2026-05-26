"""Tests for history CLI command (ops.py)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from nexus.cli import app
from nexus.db import init_db

runner = CliRunner()


def _setup_test_db() -> sqlite3.Connection:
    """In-memory DB with two strategies and sample transactions."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO broker_accounts (profile_name, margin_multiplier, cash_balance)"
        " VALUES ('paper1', 2.0, 100000.0)"
    )
    conn.execute(
        "INSERT INTO strategies (name, broker_account_id, cash_balance, is_active, created_at)"
        " VALUES ('alpha', 1, 10000.0, 1, ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO strategies (name, broker_account_id, cash_balance, is_active, created_at)"
        " VALUES ('beta', 1, 5000.0, 1, ?)",
        (now,),
    )
    conn.commit()
    return conn


def _invoke(conn: sqlite3.Connection, args: list[str]):
    with patch("nexus.cli.ops.get_connection", return_value=conn), \
         patch("nexus.cli.ops.init_db"):
        return runner.invoke(app, args)


def _insert_txn(
    conn: sqlite3.Connection,
    strategy_id: int,
    txn_type: str,
    amount: float,
    created_at: str,
    note: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO transactions (strategy_id, order_id, type, amount, actor, note, created_at)"
        " VALUES (?, NULL, ?, ?, 'cli:manual', ?, ?)",
        (strategy_id, txn_type, amount, note, created_at),
    )
    conn.commit()


class TestHistoryShows:
    def test_history_shows_transactions(self):
        conn = _setup_test_db()
        now = datetime.now(timezone.utc).isoformat()
        _insert_txn(conn, 1, "deposit", 5000.0, now, note="initial")
        result = _invoke(conn, ["history"])
        assert result.exit_code == 0
        assert "deposit" in result.output
        assert "5000" in result.output
        assert "alpha" in result.output

    def test_history_filters_by_strategy(self):
        conn = _setup_test_db()
        now = datetime.now(timezone.utc).isoformat()
        _insert_txn(conn, 1, "deposit", 1000.0, now)
        _insert_txn(conn, 2, "deposit", 2000.0, now)
        result = _invoke(conn, ["history", "--strategy", "alpha"])
        assert result.exit_code == 0
        assert "alpha" in result.output
        assert "beta" not in result.output
        assert "1000" in result.output
        assert "2000" not in result.output

    def test_history_filters_by_since(self):
        conn = _setup_test_db()
        old_date = "2020-01-01T00:00:00+00:00"
        new_date = datetime.now(timezone.utc).isoformat()
        _insert_txn(conn, 1, "deposit", 100.0, old_date, note="old")
        _insert_txn(conn, 1, "deposit", 999.0, new_date, note="new")
        # Filter to only recent transactions.
        result = _invoke(conn, ["history", "--since", "2024-01-01"])
        assert result.exit_code == 0
        assert "999" in result.output
        # The old transaction should not appear.
        assert "old" not in result.output

    def test_history_empty(self):
        conn = _setup_test_db()
        result = _invoke(conn, ["history"])
        assert result.exit_code == 0
        assert "No transactions found" in result.output

    def test_history_two_strategies_no_filter(self):
        conn = _setup_test_db()
        now = datetime.now(timezone.utc).isoformat()
        _insert_txn(conn, 1, "deposit", 1000.0, now)
        _insert_txn(conn, 2, "withdrawal", -500.0, now)
        result = _invoke(conn, ["history"])
        assert result.exit_code == 0
        assert "alpha" in result.output
        assert "beta" in result.output
        assert "deposit" in result.output
        assert "withdrawal" in result.output
