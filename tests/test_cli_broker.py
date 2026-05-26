"""Tests for broker CLI commands: show, sync, remove."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from nexus.cli import app
from nexus.broker.types import BrokerAccount, BrokerPosition
from nexus.db import init_db

runner = CliRunner()


def _setup_test_db() -> sqlite3.Connection:
    """In-memory DB with one broker account."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    conn.execute(
        "INSERT INTO broker_accounts (profile_name, margin_multiplier, cash_balance, last_synced_at)"
        " VALUES ('paper1', 2.0, 50000.0, ?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit()
    return conn


def _invoke(conn: sqlite3.Connection, args: list[str], mock_broker_cls=None):
    """Invoke with get_connection patched; optionally patch AlpacaBroker too."""
    patches = [
        patch("nexus.cli.broker_cmd.get_connection", return_value=conn),
        patch("nexus.cli.broker_cmd.init_db"),
    ]
    if mock_broker_cls is not None:
        patches.append(patch("nexus.cli.broker_cmd.AlpacaBroker", mock_broker_cls))
    with patches[0], patches[1]:
        if mock_broker_cls is not None:
            with patches[2]:
                return runner.invoke(app, args)
        return runner.invoke(app, args)


class TestBrokerRemove:
    def test_remove_deletes_broker(self):
        conn = _setup_test_db()
        result = _invoke(conn, ["broker", "remove", "paper1"])
        assert result.exit_code == 0
        assert "removed" in result.output.lower()
        row = conn.execute(
            "SELECT id FROM broker_accounts WHERE profile_name = 'paper1'"
        ).fetchone()
        assert row is None

    def test_remove_blocks_with_strategies(self):
        conn = _setup_test_db()
        # Attach a strategy to the broker.
        conn.execute(
            "INSERT INTO strategies (name, broker_account_id, cash_balance, is_active, created_at)"
            " VALUES ('attached_strat', 1, 1000.0, 1, ?)",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()
        result = _invoke(conn, ["broker", "remove", "paper1"])
        assert result.exit_code != 0
        assert "attached_strat" in result.output or "attached_strat" in (result.stderr or "")
        # Broker still exists.
        row = conn.execute(
            "SELECT id FROM broker_accounts WHERE profile_name = 'paper1'"
        ).fetchone()
        assert row is not None

    def test_remove_unknown_broker_fails(self):
        conn = _setup_test_db()
        result = _invoke(conn, ["broker", "remove", "ghost"])
        assert result.exit_code != 0


class TestBrokerShow:
    def test_show_displays_cached_info(self):
        """When live broker raises RuntimeError, cached info is still displayed."""
        conn = _setup_test_db()

        mock_broker_cls = MagicMock()
        mock_broker_instance = MagicMock()
        mock_broker_cls.return_value = mock_broker_instance
        mock_broker_instance.get_account.side_effect = RuntimeError("network error")
        mock_broker_instance.get_positions.side_effect = RuntimeError("network error")

        result = _invoke(conn, ["broker", "show", "paper1"], mock_broker_cls=mock_broker_cls)
        assert result.exit_code == 0
        assert "paper1" in result.output
        assert "50000.00" in result.output
        assert "Live data unavailable" in result.output

    def test_show_displays_live_info_when_available(self):
        conn = _setup_test_db()

        mock_account = BrokerAccount(
            cash=Decimal("75000.00"),
            buying_power=Decimal("150000.00"),
            equity=Decimal("75000.00"),
        )
        mock_broker_cls = MagicMock()
        mock_broker_instance = MagicMock()
        mock_broker_cls.return_value = mock_broker_instance
        mock_broker_instance.get_account.return_value = mock_account
        mock_broker_instance.get_positions.return_value = []

        result = _invoke(conn, ["broker", "show", "paper1"], mock_broker_cls=mock_broker_cls)
        assert result.exit_code == 0
        assert "75000.00" in result.output
        assert "Live account" in result.output


class TestBrokerSync:
    def test_sync_updates_balance(self):
        conn = _setup_test_db()

        mock_account = BrokerAccount(
            cash=Decimal("99000.00"),
            buying_power=Decimal("198000.00"),
            equity=Decimal("99000.00"),
        )
        mock_broker_cls = MagicMock()
        mock_broker_instance = MagicMock()
        mock_broker_cls.return_value = mock_broker_instance
        mock_broker_instance.get_account.return_value = mock_account

        result = _invoke(conn, ["broker", "sync", "paper1"], mock_broker_cls=mock_broker_cls)
        assert result.exit_code == 0
        assert "99000.00" in result.output
        row = conn.execute(
            "SELECT cash_balance FROM broker_accounts WHERE profile_name = 'paper1'"
        ).fetchone()
        assert abs(row["cash_balance"] - 99000.0) < 0.01

    def test_sync_unknown_broker_fails(self):
        conn = _setup_test_db()
        result = _invoke(conn, ["broker", "sync", "ghost"])
        assert result.exit_code != 0

    def test_sync_continues_on_broker_error(self):
        """Sync error prints error message but does not crash."""
        conn = _setup_test_db()

        mock_broker_cls = MagicMock()
        mock_broker_instance = MagicMock()
        mock_broker_cls.return_value = mock_broker_instance
        mock_broker_instance.get_account.side_effect = RuntimeError("connection refused")

        result = _invoke(conn, ["broker", "sync", "paper1"], mock_broker_cls=mock_broker_cls)
        # sync command reports per-broker errors but exits 0 by design (continue on error).
        assert "Error syncing" in result.output or "connection refused" in result.output
