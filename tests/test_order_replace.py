"""Tests for order replace command and broker method."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from nexus.broker.alpaca import AlpacaBroker
from nexus.cli import app
from nexus.db import init_db

runner = CliRunner()


def _broker_order_response(
    broker_id: str = "new-broker-id",
    client_order_id: str = "nx-test-AAPL-12345678",
    status: str = "submitted",
    symbol: str = "AAPL",
    side: str = "buy",
    qty: int = 20,
) -> dict:
    """Return a mock broker order response dict."""
    return {
        "id": broker_id,
        "client_order_id": client_order_id,
        "status": status,
        "symbol": symbol,
        "side": side,
        "qty": str(qty),
        "filled_qty": "0",
        "filled_avg_price": None,
        "submitted_at": "2026-01-01T10:00:00Z",
        "filled_at": None,
    }


def _get_test_conn_with_order():
    """Create in-memory DB with broker, strategy, and a submitted order."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)

    conn.execute(
        "INSERT INTO broker_accounts (profile_name, margin_multiplier, cash_balance) VALUES (?, ?, ?)",
        ("paper1", 2.0, 100000.0),
    )
    conn.execute(
        "INSERT INTO strategies (name, broker_account_id, cash_balance, is_active, created_at)"
        " VALUES (?, ?, ?, 1, ?)",
        ("test_strat", 1, 10000.0, datetime.now(timezone.utc).isoformat()),
    )
    conn.execute(
        "INSERT INTO orders (strategy_id, symbol, side, qty, order_type, status,"
        " client_order_id, broker_order_id, reserved_amount, filled_qty, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (1, "AAPL", "buy", 10, "limit", "submitted",
         "nx-test-AAPL-12345678", "abc-broker-id", 1500.0, 0,
         "2026-01-01T10:00:00Z", "2026-01-01T10:00:00Z"),
    )
    conn.commit()
    return conn


class TestReplaceBrokerMethod:
    @patch("subprocess.run")
    def test_replace_order_broker_method(self, mock_subprocess):
        """AlpacaBroker.replace_order calls subprocess with correct args."""
        response_data = _broker_order_response(broker_id="replaced-id", qty=20)
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(response_data),
            stderr="",
        )

        broker = AlpacaBroker("paper1")
        result = broker.replace_order("old-id", qty=20, limit_price=150.0)

        assert result.broker_order_id == "replaced-id"
        assert result.qty == 20

        # Verify subprocess was called with expected args
        call_args = mock_subprocess.call_args[0][0]
        assert "alpaca" in call_args[0]
        assert "order" in call_args
        assert "replace" in call_args
        assert "--order-id" in call_args
        assert "old-id" in call_args
        assert "--qty" in call_args
        assert "20" in call_args
        assert "--limit-price" in call_args
        assert "150.0" in call_args
        assert "--profile" in call_args
        assert "paper1" in call_args


class TestReplaceCommandNotFound:
    @patch("nexus.cli.order.get_connection")
    def test_replace_command_not_found(self, mock_conn):
        """order replace with nonexistent order ID shows error."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        init_db(conn)
        mock_conn.return_value = conn

        result = runner.invoke(app, ["order", "replace", "999"])

        assert result.exit_code == 1
        assert "not found" in result.output.lower() or "Error" in result.output


class TestReplaceCommandSuccess:
    @patch("subprocess.run")
    @patch("nexus.cli.order.get_connection")
    def test_replace_command_success(self, mock_conn, mock_subprocess):
        """order replace with valid order succeeds and updates DB."""
        conn = _get_test_conn_with_order()
        mock_conn.return_value = conn

        response_data = _broker_order_response(broker_id="new-broker-id", qty=20)
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(response_data),
            stderr="",
        )

        result = runner.invoke(app, ["order", "replace", "1", "--qty", "20"])

        assert result.exit_code == 0
        assert "replaced" in result.output.lower() or "new-broker-id" in result.output

        # Verify DB was updated
        row = conn.execute("SELECT broker_order_id, qty FROM orders WHERE id = 1").fetchone()
        assert row["broker_order_id"] == "new-broker-id"
        assert row["qty"] == 20

    @patch("subprocess.run")
    @patch("nexus.cli.order.get_connection")
    def test_replace_command_json_output(self, mock_conn, mock_subprocess):
        """--json order replace returns valid JSON with order info."""
        conn = _get_test_conn_with_order()
        mock_conn.return_value = conn

        response_data = _broker_order_response(broker_id="json-broker-id", qty=20)
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(response_data),
            stderr="",
        )

        result = runner.invoke(app, ["--json", "order", "replace", "1", "--qty", "20"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["order_id"] == 1
        assert data["new_broker_order_id"] == "json-broker-id"


class TestReplaceCommandBrokerError:
    @patch("subprocess.run")
    @patch("nexus.cli.order.get_connection")
    def test_replace_command_broker_error(self, mock_conn, mock_subprocess):
        """order replace shows error when broker fails."""
        conn = _get_test_conn_with_order()
        mock_conn.return_value = conn

        mock_subprocess.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="order not replaceable",
        )

        result = runner.invoke(app, ["order", "replace", "1", "--qty", "20"])

        assert result.exit_code == 1
        assert "error" in result.output.lower() or "Error" in result.output
