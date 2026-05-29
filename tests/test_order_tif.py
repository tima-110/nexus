"""Tests for --time-in-force on order buy and order sell commands."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

import pytest
from typer.testing import CliRunner

from nexus.cli import app
from nexus.db import init_db

runner = CliRunner()


def _broker_order_response(
    broker_id: str = "broker-123",
    client_order_id: str = "nx-test-AAPL-00000000",
    symbol: str = "AAPL",
    side: str = "sell",
    qty: int = 10,
) -> dict:
    return {
        "id": broker_id,
        "client_order_id": client_order_id,
        "status": "submitted",
        "symbol": symbol,
        "side": side,
        "qty": str(qty),
        "filled_qty": "0",
        "filled_avg_price": None,
        "submitted_at": "2026-01-01T10:00:00Z",
        "filled_at": None,
    }


def _setup_conn_with_position(symbol: str = "AAPL", qty: int = 10) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO broker_accounts (profile_name, margin_multiplier, cash_balance) VALUES (?, ?, ?)",
        ("paper1", 2.0, 100000.0),
    )
    conn.execute(
        "INSERT INTO strategies (name, broker_account_id, cash_balance, is_active, created_at) VALUES (?, ?, ?, ?, ?)",
        ("test_strat", 1, 10000.0, 1, now),
    )
    conn.execute(
        "INSERT INTO positions (strategy_id, symbol, qty, reserved_qty, avg_entry_price, opened_at, updated_at)"
        " VALUES (?, ?, ?, 0, ?, ?, ?)",
        (1, symbol, qty, 150.0, now, now),
    )
    conn.commit()
    return conn


class TestBrokerSubmitOrderTif:
    @patch("subprocess.run")
    def test_submit_order_passes_time_in_force(self, mock_subprocess):
        """submit_order passes --time-in-force to alpaca CLI when provided."""
        from nexus.broker.alpaca import AlpacaBroker

        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(_broker_order_response()),
            stderr="",
        )
        broker = AlpacaBroker("paper1")
        broker.submit_order("AAPL", 10, "sell", "stop", stop_price=140.0, time_in_force="gtc")

        args_used = mock_subprocess.call_args[0][0]
        assert "--time-in-force" in args_used
        assert "gtc" in args_used

    @patch("subprocess.run")
    def test_submit_order_omits_time_in_force_when_none(self, mock_subprocess):
        """submit_order does not pass --time-in-force when not provided."""
        from nexus.broker.alpaca import AlpacaBroker

        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(_broker_order_response()),
            stderr="",
        )
        broker = AlpacaBroker("paper1")
        broker.submit_order("AAPL", 10, "sell", "stop", stop_price=140.0)

        args_used = mock_subprocess.call_args[0][0]
        assert "--time-in-force" not in args_used


class TestOrderSellTif:
    @patch("nexus.cli.order.sync_outstanding_orders")
    @patch("nexus.cli.order.get_connection")
    @patch("subprocess.run")
    def test_sell_with_gtc_persists_time_in_force(self, mock_subprocess, mock_conn, mock_sync):
        """--time-in-force gtc is stored in DB and passed to broker."""
        conn = _setup_conn_with_position()
        mock_conn.return_value = conn
        mock_sync.return_value = None
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(_broker_order_response(side="sell")),
            stderr="",
        )

        result = runner.invoke(app, [
            "--json", "order", "sell", "AAPL", "10",
            "--strategy", "test_strat",
            "--type", "stop",
            "--stop-price", "140.00",
            "--time-in-force", "gtc",
        ])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "ok"

        # Verify persisted in DB
        order = conn.execute("SELECT time_in_force FROM orders WHERE id = ?", (data["order_id"],)).fetchone()
        assert order["time_in_force"] == "gtc"

        # Verify passed to broker subprocess
        args_used = mock_subprocess.call_args[0][0]
        assert "--time-in-force" in args_used
        assert "gtc" in args_used

    @patch("nexus.cli.order.sync_outstanding_orders")
    @patch("nexus.cli.order.get_connection")
    @patch("subprocess.run")
    def test_sell_without_tif_stores_null(self, mock_subprocess, mock_conn, mock_sync):
        """Omitting --time-in-force stores NULL in DB."""
        conn = _setup_conn_with_position()
        mock_conn.return_value = conn
        mock_sync.return_value = None
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(_broker_order_response(side="sell")),
            stderr="",
        )

        result = runner.invoke(app, [
            "--json", "order", "sell", "AAPL", "10",
            "--strategy", "test_strat",
            "--type", "stop",
            "--stop-price", "140.00",
        ])

        assert result.exit_code == 0
        data = json.loads(result.output)
        order = conn.execute("SELECT time_in_force FROM orders WHERE id = ?", (data["order_id"],)).fetchone()
        assert order["time_in_force"] is None


class TestOrderListTif:
    @patch("nexus.cli.order.get_connection")
    def test_order_list_includes_time_in_force(self, mock_conn):
        """order list JSON includes time_in_force field."""
        conn = _setup_conn_with_position()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO orders (strategy_id, symbol, side, qty, order_type, time_in_force, status,"
            " client_order_id, filled_qty, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (1, "AAPL", "sell", 10, "stop", "gtc", "submitted",
             "nx-test-AAPL-tif", 0, now, now),
        )
        conn.commit()
        mock_conn.return_value = conn

        result = runner.invoke(app, ["--json", "order", "list", "--strategy", "test_strat"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["items"][0]["time_in_force"] == "gtc"
