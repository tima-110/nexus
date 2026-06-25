"""Tests for option fill and cancellation processing."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from nexus.ledger import process_cancel_option, process_option_fill
from nexus.models import OptionRight


@pytest.fixture
def ctx(conn, sample_strategy):
    """Set up test context with a strategy and an option order."""
    strategy_id = sample_strategy

    # Create an option sell order for NKE260718P00040000 (NKE put, strike 40)
    cur = conn.execute(
        "INSERT INTO orders (strategy_id, symbol, side, qty, order_type, limit_price,"
        " time_in_force, status, client_order_id, reserved_amount, filled_qty, actor, created_at, updated_at)"
        " VALUES (?, 'NKE260718P00040000', 'sell', 1, 'limit', 2.50,"
        " 'day', 'submitted', 'test-opt-sell-1', 4000.0, 0, 'test', ?, ?)",
        (strategy_id, datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()),
    )
    order_id = cur.lastrowid

    # Create the assignment reservation (as the CLI would)
    conn.execute(
        "INSERT INTO reservations (strategy_id, order_id, amount, created_at) VALUES (?, ?, 4000.0, ?)",
        (strategy_id, order_id, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()

    return conn, strategy_id, order_id


class TestProcessOptionFill:
    def test_sell_fill_opens_short(self, ctx):
        """Selling an option creates a short option position and credits premium."""
        conn, strategy_id, order_id = ctx

        process_option_fill(conn, order_id, 1, 2.50, "2025-01-15T10:00:00Z")

        # Check option position created
        pos = conn.execute(
            "SELECT * FROM option_positions WHERE strategy_id = ? AND symbol = 'NKE260718P00040000'",
            (strategy_id,),
        ).fetchone()
        assert pos is not None
        assert pos["side"] == "short"
        assert pos["qty"] == 1
        assert pos["avg_entry_price"] == 2.50
        assert pos["strike"] == 40.0
        assert pos["option_right"] == "put"
        assert pos["underlying"] == "NKE"
        assert pos["origin_order_id"] == order_id

        # Check order marked filled
        order = conn.execute("SELECT status, filled_qty, filled_avg_price FROM orders WHERE id = ?", (order_id,)).fetchone()
        assert order["status"] == "filled"

        # Check cash balance credited with premium ($2.50 * 100)
        strat = conn.execute("SELECT cash_balance FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
        assert strat["cash_balance"] == 10250.0  # 10000 + 250

        # Check reservation still exists (assignment obligation held)
        res = conn.execute(
            "SELECT COUNT(*) AS cnt FROM reservations WHERE order_id = ?", (order_id,)
        ).fetchone()
        assert res["cnt"] == 1  # Still held

    def test_buy_fill_closes_short(self, conn, sample_strategy):
        """Buying to close reduces or removes a short option position and releases the original reservation."""
        strategy_id = sample_strategy
        now = datetime.now(timezone.utc).isoformat()

        # Create the original sell order (simulates what option-sell CLI does)
        sell_order_id = conn.execute(
            "INSERT INTO orders (strategy_id, symbol, side, qty, order_type, limit_price,"
            " time_in_force, asset_class, status, client_order_id, reserved_amount, filled_qty, actor, created_at, updated_at)"
            " VALUES (?, 'NKE260718P00040000', 'sell', 1, 'limit', 2.50,"
            " 'day', 'option', 'filled', 'test-opt-sell-orig', 4000.0, 1, 'test', ?, ?)",
            (strategy_id, now, now),
        ).lastrowid

        # Create the assignment reservation under the sell order
        conn.execute(
            "INSERT INTO reservations (strategy_id, order_id, amount, created_at) VALUES (?, ?, 4000.0, ?)",
            (strategy_id, sell_order_id, now),
        )

        # Create a short option position with origin_order_id pointing to the sell
        conn.execute(
            "INSERT INTO option_positions (strategy_id, symbol, underlying, option_right, side, qty,"
            " avg_entry_price, strike, expiry, origin_order_id, opened_at, updated_at)"
            " VALUES (?, 'NKE260718P00040000', 'NKE', 'put', 'short', 1, 2.50, 40.0, '2026-07-18', ?, ?, ?)",
            (strategy_id, sell_order_id, now, now),
        )

        # Create buy-to-close order
        buy_order_id = conn.execute(
            "INSERT INTO orders (strategy_id, symbol, side, qty, order_type, limit_price,"
            " time_in_force, asset_class, status, client_order_id, reserved_amount, filled_qty, actor, created_at, updated_at)"
            " VALUES (?, 'NKE260718P00040000', 'buy', 1, 'limit', 0.50,"
            " 'day', 'option', 'submitted', 'test-opt-buy-1', 50.0, 0, 'test', ?, ?)",
            (strategy_id, now, now),
        ).lastrowid

        # Create the buy order's own reservation (premium cost)
        conn.execute(
            "INSERT INTO reservations (strategy_id, order_id, amount, created_at) VALUES (?, ?, 50.0, ?)",
            (strategy_id, buy_order_id, now),
        )
        conn.commit()

        process_option_fill(conn, buy_order_id, 1, 0.50, "2025-01-20T10:00:00Z")

        # Check position removed
        pos = conn.execute(
            "SELECT * FROM option_positions WHERE strategy_id = ? AND symbol = 'NKE260718P00040000'",
            (strategy_id,),
        ).fetchone()
        assert pos is None

        # Check cash debited: 10000 - 50 = 9950
        strat = conn.execute("SELECT cash_balance FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
        assert strat["cash_balance"] == 9950.0

        # CRITICAL: both reservations released (original sell + buy order)
        res = conn.execute(
            "SELECT COUNT(*) AS cnt FROM reservations WHERE strategy_id = ?", (strategy_id,)
        ).fetchone()
        assert res["cnt"] == 0

    def test_buy_fill_opens_long(self, conn, sample_strategy):
        """Buying an option without an existing short opens a long position."""
        strategy_id = sample_strategy

        order_id = conn.execute(
            "INSERT INTO orders (strategy_id, symbol, side, qty, order_type, limit_price,"
            " time_in_force, status, client_order_id, reserved_amount, filled_qty, actor, created_at, updated_at)"
            " VALUES (?, 'AAPL260821C00225000', 'buy', 1, 'limit', 3.00,"
            " 'day', 'submitted', 'test-opt-long-1', 300.0, 0, 'test', ?, ?)",
            (strategy_id, datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()),
        ).lastrowid
        conn.commit()

        process_option_fill(conn, order_id, 1, 3.00, "2025-01-15T10:00:00Z")

        pos = conn.execute(
            "SELECT * FROM option_positions WHERE strategy_id = ? AND symbol = 'AAPL260821C00225000'",
            (strategy_id,),
        ).fetchone()
        assert pos is not None
        assert pos["side"] == "long"
        assert pos["qty"] == 1

        # Cash: 10000 - 300 = 9700
        strat = conn.execute("SELECT cash_balance FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
        assert strat["cash_balance"] == 9700.0


class TestProcessCancelOption:
    def test_cancel_option_sell_releases_reservation(self, ctx):
        """Cancelling an option sell order releases the assignment reservation."""
        conn, strategy_id, order_id = ctx

        # Verify reservation exists
        res = conn.execute(
            "SELECT amount, order_id FROM reservations WHERE order_id = ?", (order_id,)
        ).fetchone()
        assert res is not None

        process_cancel_option(conn, order_id)

        # Reservation released
        res = conn.execute(
            "SELECT COUNT(*) AS cnt FROM reservations WHERE order_id = ?", (order_id,)
        ).fetchone()
        assert res["cnt"] == 0

        # Order marked cancelled
        order = conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
        assert order["status"] == "cancelled"