"""Tests for nexus.sync — eager sync with mocked broker."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from nexus.broker.types import BrokerOrder
from nexus.ledger import create_reservation
from nexus.models import OrderStatus
from nexus.sync import sync_outstanding_orders


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert_order(
    conn,
    strategy_id,
    symbol="AAPL",
    side="buy",
    status="submitted",
    qty=10,
    client_order_id="coid-1",
    actor="test",
):
    cur = conn.execute(
        "INSERT INTO orders (strategy_id, symbol, side, qty, order_type, status, "
        "client_order_id, actor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (strategy_id, symbol, side, qty, "market", status, client_order_id, actor, _now()),
    )
    conn.commit()
    return cur.lastrowid


def _insert_position(conn, strategy_id, symbol="AAPL", qty=100, reserved_qty=0):
    now = _now()
    conn.execute(
        "INSERT INTO positions (strategy_id, symbol, qty, reserved_qty, avg_entry_price, "
        "opened_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (strategy_id, symbol, qty, reserved_qty, 150.0, now, now),
    )
    conn.commit()


def _make_broker_order(
    broker_order_id="b-123",
    client_order_id="coid-1",
    status="filled",
    symbol="AAPL",
    side="buy",
    qty=10,
    filled_qty=10,
    filled_avg_price="150.00",
    submitted_at=None,
    filled_at=None,
) -> BrokerOrder:
    return BrokerOrder(
        broker_order_id=broker_order_id,
        client_order_id=client_order_id,
        status=status,
        symbol=symbol,
        side=side,
        qty=qty,
        filled_qty=filled_qty,
        filled_avg_price=Decimal(filled_avg_price) if filled_avg_price else None,
        submitted_at=submitted_at or _now(),
        filled_at=filled_at or _now(),
    )


def _mock_broker(broker_order: BrokerOrder) -> MagicMock:
    broker = MagicMock()
    broker.get_order_by_client_id.return_value = broker_order
    return broker


class TestSyncOutstandingOrders:
    def test_processes_filled_order(self, conn, sample_strategy):
        order_id = _insert_order(conn, sample_strategy, "AAPL", "buy", "submitted", 10, "coid-1")
        create_reservation(conn, sample_strategy, order_id, 1500.0)
        conn.commit()

        broker = _mock_broker(
            _make_broker_order(status="filled", filled_qty=10, filled_avg_price="150.00")
        )
        sync_outstanding_orders(conn, sample_strategy, broker)

        order = conn.execute(
            "SELECT status, filled_qty FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        assert order["status"] == OrderStatus.filled
        assert order["filled_qty"] == 10

        # Position created
        pos = conn.execute(
            "SELECT qty FROM positions WHERE strategy_id = ? AND symbol = ?",
            (sample_strategy, "AAPL"),
        ).fetchone()
        assert pos is not None
        assert pos["qty"] == 10

    def test_processes_cancelled_order(self, conn, sample_strategy):
        order_id = _insert_order(conn, sample_strategy, "AAPL", "buy", "submitted", 10, "coid-2")
        create_reservation(conn, sample_strategy, order_id, 1500.0)
        conn.commit()

        broker = _mock_broker(
            _make_broker_order(
                client_order_id="coid-2",
                status="cancelled",
                filled_qty=0,
                filled_avg_price=None,
            )
        )
        sync_outstanding_orders(conn, sample_strategy, broker)

        order = conn.execute(
            "SELECT status FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        assert order["status"] == OrderStatus.cancelled

        # Reservation released
        res = conn.execute(
            "SELECT * FROM reservations WHERE order_id = ?", (order_id,)
        ).fetchone()
        assert res is None

    def test_updates_partial_fill_when_qty_changes(self, conn, sample_strategy):
        order_id = _insert_order(
            conn, sample_strategy, "AAPL", "buy", "submitted", 10, "coid-3"
        )
        # No reservation needed for this test path
        conn.commit()

        broker = _mock_broker(
            _make_broker_order(
                client_order_id="coid-3",
                status="partially_filled",
                qty=10,
                filled_qty=5,
                filled_avg_price="148.00",
            )
        )
        sync_outstanding_orders(conn, sample_strategy, broker)

        order = conn.execute(
            "SELECT status, filled_qty, filled_avg_price FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        # Status unchanged (still submitted from original insert), filled_qty updated
        assert order["filled_qty"] == 5
        assert order["filled_avg_price"] == pytest.approx(148.0)

    def test_skips_orders_that_havent_changed(self, conn, sample_strategy):
        # Already partially filled with qty 5
        cur = conn.execute(
            "INSERT INTO orders (strategy_id, symbol, side, qty, order_type, status, "
            "client_order_id, filled_qty, actor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sample_strategy,
                "AAPL",
                "buy",
                10,
                "market",
                "partially_filled",
                "coid-4",
                5,
                "test",
                _now(),
            ),
        )
        order_id = cur.lastrowid
        conn.commit()

        broker = _mock_broker(
            _make_broker_order(
                client_order_id="coid-4",
                status="partially_filled",
                qty=10,
                filled_qty=5,  # same as DB — no change
                filled_avg_price="148.00",
            )
        )
        sync_outstanding_orders(conn, sample_strategy, broker)

        order = conn.execute(
            "SELECT updated_at FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        # updated_at should still be None since we never set it and skipped the update
        assert order["updated_at"] is None

    def test_continues_on_broker_error_for_one_order(self, conn, sample_strategy):
        order1_id = _insert_order(
            conn, sample_strategy, "AAPL", "buy", "submitted", 10, "coid-err"
        )
        order2_id = _insert_order(
            conn, sample_strategy, "TSLA", "buy", "submitted", 5, "coid-ok"
        )
        create_reservation(conn, sample_strategy, order1_id, 1500.0)
        create_reservation(conn, sample_strategy, order2_id, 750.0)
        conn.commit()

        broker = MagicMock()

        def side_effect(client_order_id):
            if client_order_id == "coid-err":
                raise RuntimeError("broker exploded")
            return _make_broker_order(
                client_order_id="coid-ok",
                symbol="TSLA",
                status="filled",
                filled_qty=5,
                filled_avg_price="150.00",
            )

        broker.get_order_by_client_id.side_effect = side_effect

        # Should not raise despite the broker error on order1
        sync_outstanding_orders(conn, sample_strategy, broker)

        # order1 remains in submitted state (broker errored)
        order1 = conn.execute(
            "SELECT status FROM orders WHERE id = ?", (order1_id,)
        ).fetchone()
        assert order1["status"] == "submitted"

        # order2 was processed and filled
        order2 = conn.execute(
            "SELECT status FROM orders WHERE id = ?", (order2_id,)
        ).fetchone()
        assert order2["status"] == OrderStatus.filled

    def test_no_outstanding_orders_no_broker_calls(self, conn, sample_strategy):
        broker = MagicMock()
        sync_outstanding_orders(conn, sample_strategy, broker)
        broker.get_order_by_client_id.assert_not_called()

    def test_expired_order_processed_as_cancel(self, conn, sample_strategy):
        order_id = _insert_order(
            conn, sample_strategy, "AAPL", "buy", "submitted", 10, "coid-exp"
        )
        create_reservation(conn, sample_strategy, order_id, 1500.0)
        conn.commit()

        broker = _mock_broker(
            _make_broker_order(
                client_order_id="coid-exp",
                status="expired",
                filled_qty=0,
                filled_avg_price=None,
            )
        )
        sync_outstanding_orders(conn, sample_strategy, broker)

        order = conn.execute(
            "SELECT status FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        assert order["status"] == OrderStatus.cancelled
