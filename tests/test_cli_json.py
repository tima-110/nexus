"""Tests for --json output across representative CLI commands."""
from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from nexus.cli import app
from nexus.config import NexusConfig
from nexus.db import init_db

runner = CliRunner()


def _get_test_conn():
    """Create an in-memory connection with schema for CLI tests."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    conn.execute(
        "INSERT INTO broker_accounts (profile_name, margin_multiplier, cash_balance) VALUES (?, ?, ?)",
        ("paper1", 2.0, 100000.0),
    )
    conn.commit()
    return conn


class TestJsonDoctor:
    @patch("nexus.doctor.get_schedule_status", return_value={"installed": True, "schedule": "*/5 * * * *", "command": "nexus reconcile"})
    @patch("subprocess.run")
    @patch("nexus.db.get_connection")
    @patch("nexus.config.load_config")
    def test_json_doctor_outputs_valid_json(self, mock_config, mock_conn, mock_subprocess, mock_cron_status):
        """--json doctor outputs valid JSON with checks array."""
        mock_config.return_value = NexusConfig()
        conn = _get_test_conn()
        mock_conn.return_value = conn

        result = runner.invoke(app, ["--json", "doctor"])

        assert result.exit_code in (0, 1)
        data = json.loads(result.output)
        assert "checks" in data
        assert isinstance(data["checks"], list)
        assert len(data["checks"]) > 0
        for check in data["checks"]:
            assert "name" in check
            assert "passed" in check
            assert "detail" in check


class TestJsonStrategyList:
    @patch("nexus.cli.strategy.get_connection")
    def test_json_strategy_list(self, mock_conn):
        """--json strategy list returns items with strategy data."""
        conn = _get_test_conn()
        conn.execute(
            "INSERT INTO strategies (name, broker_account_id, cash_balance, is_active, created_at)"
            " VALUES (?, ?, ?, 1, '2026-01-01T00:00:00Z')",
            ("test_strat", 1, 10000.0),
        )
        conn.commit()
        mock_conn.return_value = conn

        result = runner.invoke(app, ["--json", "strategy", "list"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "items" in data
        assert len(data["items"]) == 1
        assert data["items"][0]["name"] == "test_strat"
        assert data["items"][0]["cash_balance"] == 10000.0


class TestJsonOrderList:
    @patch("nexus.cli.order.get_connection")
    def test_json_order_list(self, mock_conn):
        """--json order list returns items with order data."""
        conn = _get_test_conn()
        conn.execute(
            "INSERT INTO strategies (name, broker_account_id, cash_balance, is_active, created_at)"
            " VALUES (?, ?, ?, 1, '2026-01-01T00:00:00Z')",
            ("test_strat", 1, 10000.0),
        )
        conn.execute(
            "INSERT INTO orders (strategy_id, symbol, side, qty, order_type, status,"
            " client_order_id, filled_qty, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (1, "AAPL", "buy", 10, "market", "submitted",
             "nx-test-AAPL-12345678", 0, "2026-01-01T10:00:00Z", "2026-01-01T10:00:00Z"),
        )
        conn.commit()
        mock_conn.return_value = conn

        result = runner.invoke(app, ["--json", "order", "list", "--strategy", "test_strat"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "items" in data
        assert len(data["items"]) == 1
        assert data["items"][0]["symbol"] == "AAPL"
        assert data["items"][0]["side"] == "buy"


class TestJsonReconcile:
    @patch("subprocess.run")
    @patch("nexus.db.get_connection")
    @patch("nexus.config.load_config")
    def test_json_reconcile(self, mock_config, mock_conn, mock_subprocess):
        """--json reconcile outputs JSON with reconcile result fields."""
        mock_config.return_value = NexusConfig()
        conn = _get_test_conn()
        mock_conn.return_value = conn
        # Broker unreachable
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="error")

        result = runner.invoke(app, ["--json", "reconcile"])

        assert result.exit_code in (0, 1)
        data = json.loads(result.output)
        assert "orders_synced" in data
        assert "orders_skipped" in data
        assert "orphans_cleaned" in data
        assert "dry_run" in data


class TestJsonConfigPath:
    @patch("nexus.cli.config_cmd._config_file_path")
    def test_json_config_path(self, mock_path):
        """--json config path returns path key."""
        from pathlib import Path

        mock_path.return_value = Path("/tmp/nexus/config.toml")

        result = runner.invoke(app, ["--json", "config", "path"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "path" in data
        assert "config.toml" in data["path"]


class TestJsonStatus:
    @patch("nexus.schedule.cron.get_schedule_status")
    def test_json_status(self, mock_status):
        """--json status returns installed key."""
        mock_status.return_value = {
            "installed": True,
            "schedule": "*/5 * * * *",
            "command": "/usr/local/bin/nexus reconcile",
        }

        result = runner.invoke(app, ["--json", "status"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "installed" in data
        assert data["installed"] is True
