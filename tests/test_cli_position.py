"""Tests for position CLI commands: list, show."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from nexus.cli import app
from nexus.db import init_db

runner = CliRunner()


def _setup_test_db() -> sqlite3.Connection:
    """In-memory DB with one broker and two strategies."""
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
        " VALUES ('strat_a', 1, 10000.0, 1, ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO strategies (name, broker_account_id, cash_balance, is_active, created_at)"
        " VALUES ('strat_b', 1, 5000.0, 1, ?)",
        (now,),
    )
    conn.commit()
    return conn


def _invoke_list(conn: sqlite3.Connection, args: list[str]):
    with patch("nexus.cli.position.get_connection", return_value=conn), \
         patch("nexus.cli.position.init_db"):
        return runner.invoke(app, args)


def _invoke_show(conn: sqlite3.Connection, args: list[str], mock_broker_cls=None):
    patches = [
        patch("nexus.cli.position.get_connection", return_value=conn),
        patch("nexus.cli.position.init_db"),
    ]
    if mock_broker_cls is not None:
        patches.append(patch("nexus.cli.position.AlpacaBroker", mock_broker_cls))
    with patches[0], patches[1]:
        if mock_broker_cls is not None:
            with patches[2]:
                return runner.invoke(app, args)
        return runner.invoke(app, args)


class TestPositionList:
    def test_list_shows_positions(self):
        conn = _setup_test_db()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO positions (strategy_id, symbol, qty, reserved_qty, avg_entry_price,"
            " opened_at, updated_at) VALUES (1, 'AAPL', 10, 0, 150.0, ?, ?)",
            (now, now),
        )
        conn.commit()
        result = _invoke_list(conn, ["position", "list"])
        assert result.exit_code == 0
        assert "AAPL" in result.output
        assert "strat_a" in result.output
        assert "10" in result.output

    def test_list_filters_by_strategy(self):
        conn = _setup_test_db()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO positions (strategy_id, symbol, qty, reserved_qty, avg_entry_price,"
            " opened_at, updated_at) VALUES (1, 'AAPL', 10, 0, 150.0, ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO positions (strategy_id, symbol, qty, reserved_qty, avg_entry_price,"
            " opened_at, updated_at) VALUES (2, 'TSLA', 5, 0, 200.0, ?, ?)",
            (now, now),
        )
        conn.commit()
        result = _invoke_list(conn, ["position", "list", "--strategy", "strat_a"])
        assert result.exit_code == 0
        assert "AAPL" in result.output
        assert "TSLA" not in result.output

    def test_list_empty(self):
        conn = _setup_test_db()
        result = _invoke_list(conn, ["position", "list"])
        assert result.exit_code == 0
        assert "No open positions" in result.output

    def test_list_hides_zero_qty_positions(self):
        """Positions with qty=0 are not shown."""
        conn = _setup_test_db()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO positions (strategy_id, symbol, qty, reserved_qty, avg_entry_price,"
            " opened_at, updated_at) VALUES (1, 'GOOG', 0, 0, 100.0, ?, ?)",
            (now, now),
        )
        conn.commit()
        result = _invoke_list(conn, ["position", "list"])
        assert result.exit_code == 0
        assert "No open positions" in result.output


class TestPositionShow:
    def _insert_position(self, conn: sqlite3.Connection) -> None:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO positions (strategy_id, symbol, qty, reserved_qty, avg_entry_price,"
            " opened_at, updated_at) VALUES (1, 'AAPL', 10, 2, 150.0, ?, ?)",
            (now, now),
        )
        conn.commit()

    def test_show_displays_position_detail(self):
        conn = _setup_test_db()
        self._insert_position(conn)

        mock_broker_cls = MagicMock()
        mock_broker_instance = MagicMock()
        mock_broker_cls.return_value = mock_broker_instance
        mock_broker_instance.get_last_price.return_value = Decimal("175.00")

        result = _invoke_show(conn, ["position", "show", "strat_a", "AAPL"], mock_broker_cls)
        assert result.exit_code == 0
        assert "AAPL" in result.output
        assert "strat_a" in result.output
        assert "150.0000" in result.output  # avg entry
        assert "175.0000" in result.output  # live price

    def test_show_displays_unavailable_live_price(self):
        """When AlpacaBroker raises RuntimeError, shows unavailable message."""
        conn = _setup_test_db()
        self._insert_position(conn)

        mock_broker_cls = MagicMock()
        mock_broker_instance = MagicMock()
        mock_broker_cls.return_value = mock_broker_instance
        mock_broker_instance.get_last_price.side_effect = RuntimeError("timeout")

        result = _invoke_show(conn, ["position", "show", "strat_a", "AAPL"], mock_broker_cls)
        assert result.exit_code == 0
        assert "unavailable" in result.output.lower()

    def test_show_unknown_position_fails(self):
        conn = _setup_test_db()
        mock_broker_cls = MagicMock()
        result = _invoke_show(conn, ["position", "show", "strat_a", "ZZZZ"], mock_broker_cls)
        assert result.exit_code != 0

    def test_show_unknown_strategy_fails(self):
        conn = _setup_test_db()
        mock_broker_cls = MagicMock()
        result = _invoke_show(conn, ["position", "show", "ghost_strat", "AAPL"], mock_broker_cls)
        assert result.exit_code != 0
