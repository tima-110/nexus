"""Tests for multi-leg (bracket and oco) order submission in Nexus."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from nexus.broker.alpaca import AlpacaBroker
from nexus.broker.types import BrokerOrder
from nexus.cli import app
from nexus.db import init_db

runner = CliRunner()


def _broker_order_response(
    broker_id: str = "broker-bracket-123",
    client_order_id: str = "nx-test-AAPL-00000000",
    symbol: str = "AAPL",
    side: str = "buy",
    qty: int = 10,
    order_class: str = "bracket",
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
        "submitted_at": "2026-10-29T10:00:00Z",
        "filled_at": None,
        "order_class": order_class,
        "legs": [
            {"id": "leg-tp-1", "order_type": "limit", "limit_price": "165.00"},
            {"id": "leg-sl-1", "order_type": "stop", "stop_price": "145.00"},
        ],
    }


def _setup_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO broker_accounts (profile_name, margin_multiplier, cash_balance) VALUES (?, ?, ?)",
        ("champion", 2.0, 100000.0),
    )
    conn.execute(
        "INSERT INTO strategies (name, broker_account_id, cash_balance, is_active, created_at) VALUES (?, ?, ?, ?, ?)",
        ("test_strat", 1, 10000.0, 1, now),
    )
    conn.commit()
    return conn


class TestBrokerMultiLegSubmission:
    def test_submit_bracket_order_args(self):
        broker = AlpacaBroker("champion")
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=json.dumps(_broker_order_response()), stderr="")) as mock_run:
            order = broker.submit_order(
                symbol="AAPL",
                qty=10,
                side="buy",
                order_type="limit",
                limit_price=150.0,
                order_class="bracket",
                take_profit=165.0,
                stop_loss=145.0,
                time_in_force="gtc",
            )

        assert isinstance(order, BrokerOrder)
        assert order.order_class == "bracket"
        assert order.legs is not None
        assert len(order.legs) == 2

        # Verify CLI args passed to alpaca
        cmd_args = mock_run.call_args[0][0]
        assert "order" in cmd_args
        assert "submit" in cmd_args
        assert "--order-class" in cmd_args
        idx_oc = cmd_args.index("--order-class")
        assert cmd_args[idx_oc + 1] == "bracket"

        assert "--take-profit" in cmd_args
        idx_tp = cmd_args.index("--take-profit")
        assert cmd_args[idx_tp + 1] == "165.0"

        assert "--stop-loss" in cmd_args
        idx_sl = cmd_args.index("--stop-loss")
        assert cmd_args[idx_sl + 1] == "145.0"

    def test_submit_oco_order_payload(self):
        broker = AlpacaBroker("champion")
        sample_oco_dict = {
            "id": "broker-order-oco-1",
            "client_order_id": "nx-test-AAPL-87654321",
            "status": "new",
            "symbol": "AAPL",
            "side": "sell",
            "qty": "10",
            "filled_qty": "0",
            "filled_avg_price": None,
            "submitted_at": "2026-10-29T10:00:00Z",
            "filled_at": None,
            "order_class": "oco",
        }
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=json.dumps(sample_oco_dict), stderr="")) as mock_run:
            order = broker.submit_order(
                symbol="AAPL",
                qty=10,
                side="sell",
                order_type="limit",
                limit_price=165.0,
                order_class="oco",
                take_profit=165.0,
                stop_loss=145.0,
                time_in_force="gtc",
                client_order_id="nx-test-AAPL-87654321",
            )

        assert isinstance(order, BrokerOrder)
        assert order.order_class == "oco"

        # Verify API POST endpoint was used for OCO
        cmd_args = mock_run.call_args[0][0]
        assert "api" in cmd_args
        assert "POST" in cmd_args
        assert "/v2/orders" in cmd_args
        body_idx = cmd_args.index("--body")
        body_json = json.loads(cmd_args[body_idx + 1])
        assert body_json["order_class"] == "oco"
        assert body_json["take_profit"]["limit_price"] == "165.0"
        assert body_json["stop_loss"]["stop_price"] == "145.0"
        assert body_json["client_order_id"] == "nx-test-AAPL-87654321"


class TestCliMultiLegCommands:
    @patch("nexus.cli.order.sync_outstanding_orders")
    @patch("nexus.cli.order.get_connection")
    @patch("subprocess.run")
    def test_cli_buy_bracket(self, mock_subprocess, mock_conn, mock_sync):
        conn = _setup_conn()
        mock_conn.return_value = conn
        mock_sync.return_value = None
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(_broker_order_response()),
            stderr="",
        )

        res = runner.invoke(
            app,
            [
                "--json",
                "order",
                "buy",
                "AAPL",
                "10",
                "--strategy",
                "test_strat",
                "--type",
                "limit",
                "--limit-price",
                "150.0",
                "--order-class",
                "bracket",
                "--take-profit",
                "165.0",
                "--stop-loss",
                "145.0",
                "--time-in-force",
                "gtc",
            ],
        )
        assert res.exit_code == 0
        data = json.loads(res.output)
        assert data["status"] == "ok"
        order_id = data["order_id"]

        # Verify DB stored order_class and legs
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        assert row["order_class"] == "bracket"
        assert row["take_profit_price"] == 165.0
        assert row["stop_loss_price"] == 145.0
        assert row["time_in_force"] == "gtc"

        # Verify broker command
        args = mock_subprocess.call_args[0][0]
        assert "--order-class" in args
        assert "bracket" in args
        assert "--take-profit" in args
        assert "165.0" in args
        assert "--stop-loss" in args
        assert "145.0" in args
