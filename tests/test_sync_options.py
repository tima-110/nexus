"""Tests for sync routing of option orders."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from nexus.broker.types import BrokerOrder
from nexus.sync import sync_outstanding_orders


@pytest.fixture
def ctx(conn, sample_strategy):
    """Set up a strategy with a submitted option order."""
    strategy_id = sample_strategy
    now = datetime.now(timezone.utc).isoformat()

    # Create an option sell order
    cur = conn.execute(
        "INSERT INTO orders (strategy_id, symbol, side, qty, order_type, limit_price,"
        " time_in_force, asset_class, status, client_order_id, broker_order_id,"
        " reserved_amount, filled_qty, actor, created_at, updated_at)"
        " VALUES (?, 'NKE260718P00040000', 'sell', 1, 'limit', 2.50,"
        " 'day', 'option', 'submitted', 'nx-test-opt-1', 'broker-123',"
        " 4000.0, 0, 'test', ?, ?)",
        (strategy_id, now, now),
    )
    order_id = cur.lastrowid

    # Create the assignment reservation
    conn.execute(
        "INSERT INTO reservations (strategy_id, order_id, amount, created_at) VALUES (?, ?, 4000.0, ?)",
        (strategy_id, order_id, now),
    )
    conn.commit()

    return conn, strategy_id, order_id


def _make_broker_order(status, **overrides):
    defaults = dict(
        broker_order_id="broker-123",
        client_order_id="nx-test-opt-1",
        status=status,
        symbol="NKE260718P00040000",
        side="sell",
        qty=1,
        filled_qty=1,
        filled_avg_price=Decimal("2.50"),
        submitted_at="2025-01-15T10:00:00Z",
        filled_at="2025-01-15T10:05:00Z",
    )
    defaults.update(overrides)
    return BrokerOrder(**defaults)


def test_sync_routes_filled_option_to_process_option_fill(ctx):
    """A filled OCC order should create an option_position, not an equity position."""
    conn, strategy_id, order_id = ctx

    broker = MagicMock()
    broker.get_order_by_client_id.return_value = _make_broker_order("filled")

    sync_outstanding_orders(conn, strategy_id, broker)

    # Option position should exist
    pos = conn.execute(
        "SELECT * FROM option_positions WHERE strategy_id = ? AND symbol = 'NKE260718P00040000'",
        (strategy_id,),
    ).fetchone()
    assert pos is not None
    assert pos["side"] == "short"
    assert pos["origin_order_id"] == order_id

    # Equity position should NOT exist
    eq_pos = conn.execute(
        "SELECT * FROM positions WHERE strategy_id = ? AND symbol = 'NKE260718P00040000'",
        (strategy_id,),
    ).fetchone()
    assert eq_pos is None


def test_sync_routes_cancelled_option_to_process_cancel_option(ctx):
    """A cancelled OCC order should release the assignment reservation."""
    conn, strategy_id, order_id = ctx

    broker = MagicMock()
    broker.get_order_by_client_id.return_value = _make_broker_order(
        "cancelled", filled_qty=0, filled_avg_price=None, filled_at=None
    )

    # Verify reservation exists before sync
    res = conn.execute(
        "SELECT COUNT(*) AS cnt FROM reservations WHERE order_id = ?", (order_id,)
    ).fetchone()
    assert res["cnt"] == 1

    sync_outstanding_orders(conn, strategy_id, broker)

    # Reservation should be released
    res = conn.execute(
        "SELECT COUNT(*) AS cnt FROM reservations WHERE order_id = ?", (order_id,)
    ).fetchone()
    assert res["cnt"] == 0

    # Order should be cancelled
    order = conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
    assert order["status"] == "cancelled"


def test_sync_equity_cancel_does_not_use_option_path(conn, sample_strategy):
    """A cancelled equity order goes through the standard process_cancel path."""
    strategy_id = sample_strategy
    now = datetime.now(timezone.utc).isoformat()

    # Create equity position + sell order
    conn.execute(
        "INSERT INTO positions (strategy_id, symbol, qty, reserved_qty, avg_entry_price, opened_at, updated_at)"
        " VALUES (?, 'AAPL', 100, 10, 175.0, ?, ?)",
        (strategy_id, now, now),
    )
    cur = conn.execute(
        "INSERT INTO orders (strategy_id, symbol, side, qty, order_type,"
        " asset_class, status, client_order_id, broker_order_id,"
        " reserved_amount, filled_qty, actor, created_at, updated_at)"
        " VALUES (?, 'AAPL', 'sell', 10, 'market',"
        " 'equity', 'submitted', 'nx-test-eq-1', 'broker-456',"
        " 10.0, 0, 'test', ?, ?)",
        (strategy_id, now, now),
    )
    order_id = cur.lastrowid
    conn.commit()

    broker = MagicMock()
    broker.get_order_by_client_id.return_value = BrokerOrder(
        broker_order_id="broker-456",
        client_order_id="nx-test-eq-1",
        status="cancelled",
        symbol="AAPL",
        side="sell",
        qty=10,
        filled_qty=0,
        filled_avg_price=None,
        submitted_at="2025-01-15T10:00:00Z",
        filled_at=None,
    )

    sync_outstanding_orders(conn, strategy_id, broker)

    # Order cancelled
    order = conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
    assert order["status"] == "cancelled"

    # Shares released (reserved_qty back to 0)
    pos = conn.execute(
        "SELECT reserved_qty FROM positions WHERE strategy_id = ? AND symbol = 'AAPL'",
        (strategy_id,),
    ).fetchone()
    assert pos["reserved_qty"] == 0
