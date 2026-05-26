"""Tests for strategy CLI commands: show, deposit, withdraw, set-broker."""
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
    """Create an in-memory DB with schema and one broker + one strategy."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    conn.execute(
        "INSERT INTO broker_accounts (profile_name, margin_multiplier, cash_balance)"
        " VALUES ('paper1', 2.0, 100000.0)"
    )
    conn.execute(
        "INSERT INTO strategies (name, broker_account_id, cash_balance, is_active, created_at)"
        " VALUES ('test_strat', 1, 10000.0, 1, ?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit()
    return conn


def _invoke(conn: sqlite3.Connection, args: list[str]):
    """Invoke the CLI with both get_connection and init_db patched."""
    with patch("nexus.cli.strategy.get_connection", return_value=conn), \
         patch("nexus.cli.strategy.init_db"):
        return runner.invoke(app, args)


class TestStrategyDeposit:
    def test_deposit_increases_balance(self):
        conn = _setup_test_db()
        result = _invoke(conn, ["strategy", "deposit", "test_strat", "5000"])
        assert result.exit_code == 0
        assert "15000.00" in result.output
        row = conn.execute(
            "SELECT cash_balance FROM strategies WHERE name = 'test_strat'"
        ).fetchone()
        assert row["cash_balance"] == 15000.0

    def test_deposit_unknown_strategy_fails(self):
        conn = _setup_test_db()
        result = _invoke(conn, ["strategy", "deposit", "no_such_strat", "1000"])
        assert result.exit_code != 0


class TestStrategyWithdraw:
    def test_withdraw_decreases_balance(self):
        conn = _setup_test_db()
        result = _invoke(conn, ["strategy", "withdraw", "test_strat", "3000"])
        assert result.exit_code == 0
        assert "7000.00" in result.output
        row = conn.execute(
            "SELECT cash_balance FROM strategies WHERE name = 'test_strat'"
        ).fetchone()
        assert row["cash_balance"] == 7000.0

    def test_withdraw_blocks_insufficient_balance(self):
        conn = _setup_test_db()
        # Attempt to withdraw more than the $10000 balance.
        result = _invoke(conn, ["strategy", "withdraw", "test_strat", "99999"])
        assert result.exit_code != 0
        assert "Insufficient" in result.output or "Insufficient" in (result.stderr or "")
        # Balance unchanged.
        row = conn.execute(
            "SELECT cash_balance FROM strategies WHERE name = 'test_strat'"
        ).fetchone()
        assert row["cash_balance"] == 10000.0


class TestStrategyShow:
    def test_show_displays_strategy_info(self):
        conn = _setup_test_db()
        result = _invoke(conn, ["strategy", "show", "test_strat"])
        assert result.exit_code == 0
        assert "test_strat" in result.output
        assert "paper1" in result.output
        assert "10000.00" in result.output

    def test_show_unknown_strategy_fails(self):
        conn = _setup_test_db()
        result = _invoke(conn, ["strategy", "show", "ghost"])
        assert result.exit_code != 0


class TestStrategySetBroker:
    def _setup_with_second_broker(self) -> sqlite3.Connection:
        conn = _setup_test_db()
        conn.execute(
            "INSERT INTO broker_accounts (profile_name, margin_multiplier, cash_balance)"
            " VALUES ('paper2', 1.0, 50000.0)"
        )
        conn.commit()
        return conn

    def test_set_broker_changes_broker(self):
        conn = self._setup_with_second_broker()
        result = _invoke(conn, ["strategy", "set-broker", "test_strat", "--broker", "paper2"])
        assert result.exit_code == 0
        assert "paper2" in result.output
        row = conn.execute(
            "SELECT b.profile_name FROM strategies s"
            " JOIN broker_accounts b ON s.broker_account_id = b.id"
            " WHERE s.name = 'test_strat'"
        ).fetchone()
        assert row["profile_name"] == "paper2"

    def test_set_broker_blocks_with_open_orders(self):
        conn = self._setup_with_second_broker()
        # Insert an open order for the strategy.
        conn.execute(
            "INSERT INTO orders (strategy_id, symbol, side, qty, order_type, status)"
            " VALUES (1, 'AAPL', 'buy', 10, 'market', 'submitted')"
        )
        conn.commit()
        result = _invoke(conn, ["strategy", "set-broker", "test_strat", "--broker", "paper2"])
        assert result.exit_code != 0
        assert "open" in result.output.lower() or "open" in (result.stderr or "").lower()
