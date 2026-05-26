"""Tests for nexus.broker.alpaca — mocked subprocess."""
from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from nexus.broker.alpaca import AlpacaBroker
from nexus.broker.types import BrokerOrder


SAMPLE_ORDER_DICT = {
    "id": "broker-order-123",
    "client_order_id": "client-order-abc",
    "status": "filled",
    "symbol": "AAPL",
    "side": "buy",
    "qty": "10",
    "filled_qty": "10",
    "filled_avg_price": "150.25",
    "submitted_at": "2026-01-01T10:00:00Z",
    "filled_at": "2026-01-01T10:01:00Z",
}


def _make_run_result(data, returncode=0, stderr=""):
    result = MagicMock()
    result.returncode = returncode
    result.stdout = json.dumps(data)
    result.stderr = stderr
    return result


class TestSubmitOrder:
    def test_returns_broker_order_with_correct_fields(self):
        broker = AlpacaBroker("paper1")
        with patch("subprocess.run", return_value=_make_run_result(SAMPLE_ORDER_DICT)):
            order = broker.submit_order("AAPL", 10, "buy", "market")

        assert isinstance(order, BrokerOrder)
        assert order.broker_order_id == "broker-order-123"
        assert order.client_order_id == "client-order-abc"
        assert order.status == "filled"
        assert order.symbol == "AAPL"
        assert order.side == "buy"
        assert order.qty == 10
        assert order.filled_qty == 10
        assert order.filled_avg_price == Decimal("150.25")
        assert order.submitted_at == "2026-01-01T10:00:00Z"
        assert order.filled_at == "2026-01-01T10:01:00Z"

    def test_nonzero_exit_raises_runtime_error(self):
        broker = AlpacaBroker("paper1")
        with patch(
            "subprocess.run",
            return_value=_make_run_result({}, returncode=1, stderr="some error"),
        ):
            with pytest.raises(RuntimeError, match="Alpaca CLI error"):
                broker.submit_order("AAPL", 10, "buy", "market")

    def test_invalid_json_raises_runtime_error(self):
        broker = AlpacaBroker("paper1")
        bad_result = MagicMock()
        bad_result.returncode = 0
        bad_result.stdout = "not-valid-json{"
        with patch("subprocess.run", return_value=bad_result):
            with pytest.raises(RuntimeError, match="Failed to parse"):
                broker.submit_order("AAPL", 10, "buy", "market")

    def test_optional_params_passed_through(self):
        broker = AlpacaBroker("paper1")
        captured_args = []

        def fake_run(args, **kwargs):
            captured_args.extend(args)
            return _make_run_result(SAMPLE_ORDER_DICT)

        with patch("subprocess.run", side_effect=fake_run):
            broker.submit_order(
                "AAPL",
                10,
                "limit",
                "limit",
                client_order_id="my-coid",
                limit_price=149.00,
            )

        assert "--client-order-id" in captured_args
        assert "my-coid" in captured_args
        assert "--limit-price" in captured_args

    def test_order_with_no_fill_price(self):
        data = dict(SAMPLE_ORDER_DICT)
        data["filled_avg_price"] = None
        data["filled_at"] = None
        data["filled_qty"] = "0"
        data["status"] = "submitted"

        broker = AlpacaBroker("paper1")
        with patch("subprocess.run", return_value=_make_run_result(data)):
            order = broker.submit_order("AAPL", 10, "buy", "market")

        assert order.filled_avg_price is None
        assert order.filled_at is None


class TestGetOrder:
    def test_parses_response_correctly(self):
        broker = AlpacaBroker("paper1")
        with patch("subprocess.run", return_value=_make_run_result(SAMPLE_ORDER_DICT)):
            order = broker.get_order("broker-order-123")

        assert order.broker_order_id == "broker-order-123"
        assert order.symbol == "AAPL"
        assert order.qty == 10

    def test_nonzero_exit_raises_runtime_error(self):
        broker = AlpacaBroker("paper1")
        with patch(
            "subprocess.run",
            return_value=_make_run_result({}, returncode=1, stderr="not found"),
        ):
            with pytest.raises(RuntimeError, match="Alpaca CLI error"):
                broker.get_order("missing-order")

    def test_invalid_json_raises_runtime_error(self):
        broker = AlpacaBroker("paper1")
        bad_result = MagicMock()
        bad_result.returncode = 0
        bad_result.stdout = "garbage"
        with patch("subprocess.run", return_value=bad_result):
            with pytest.raises(RuntimeError, match="Failed to parse"):
                broker.get_order("any-id")


class TestGetLastPrice:
    def test_returns_decimal_from_list_response(self):
        data = {"trade": {"p": "173.50"}}
        broker = AlpacaBroker("paper1")
        with patch("subprocess.run", return_value=_make_run_result(data)):
            price = broker.get_last_price("AAPL")

        assert price == Decimal("173.50")
        assert isinstance(price, Decimal)

    def test_returns_decimal_from_price_field(self):
        data = {"trade": {"price": "200.00"}}
        broker = AlpacaBroker("paper1")
        with patch("subprocess.run", return_value=_make_run_result(data)):
            price = broker.get_last_price("AAPL")

        assert price == Decimal("200.00")

    def test_raises_if_no_price_field(self):
        data = {"trade": {"no_price_here": True}}
        broker = AlpacaBroker("paper1")
        with patch("subprocess.run", return_value=_make_run_result(data)):
            with pytest.raises(RuntimeError, match="no price field"):
                broker.get_last_price("AAPL")
