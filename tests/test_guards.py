"""Tests for nexus.guards — pre-order validation logic."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from nexus.guards import check_buy_guard, check_sell_guard


def _insert_order(conn, strategy_id, symbol="AAPL", side="buy", status="submitted"):
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO orders (strategy_id, symbol, side, qty, order_type, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (strategy_id, symbol, side, 10, "market", status, now),
    )
    conn.commit()
    return cur.lastrowid


def _insert_position(conn, strategy_id, symbol="AAPL", qty=100, reserved_qty=0):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO positions (strategy_id, symbol, qty, reserved_qty, avg_entry_price, "
        "opened_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (strategy_id, symbol, qty, reserved_qty, 150.0, now, now),
    )
    conn.commit()


def _insert_reservation(conn, strategy_id, order_id, amount):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO reservations (strategy_id, order_id, amount, created_at) VALUES (?, ?, ?, ?)",
        (strategy_id, order_id, amount, now),
    )
    conn.commit()


class TestBuyGuard:
    def test_pass_no_position_sufficient_balance(self, conn, sample_strategy):
        ok, reason = check_buy_guard(conn, sample_strategy, "AAPL", 1000.0)
        assert ok is True
        assert reason == ""

    def test_fail_existing_position(self, conn, sample_strategy):
        _insert_position(conn, sample_strategy, "AAPL", qty=50)
        ok, reason = check_buy_guard(conn, sample_strategy, "AAPL", 100.0)
        assert ok is False
        assert "already holding" in reason.lower()

    def test_fail_insufficient_balance(self, conn, sample_strategy):
        # sample_strategy has $10000; try to buy $50000
        ok, reason = check_buy_guard(conn, sample_strategy, "TSLA", 50000.0)
        assert ok is False
        assert "insufficient" in reason.lower()

    def test_fail_balance_with_reservations(self, conn, sample_strategy):
        # Balance $10000, reserve $9900 for another order, then try to buy $200
        order_id = _insert_order(conn, sample_strategy, symbol="MSFT")
        _insert_reservation(conn, sample_strategy, order_id, 9900.0)
        # Available = 10000 - 9900 = 100, need 200
        ok, reason = check_buy_guard(conn, sample_strategy, "GOOGL", 200.0)
        assert ok is False
        assert "insufficient" in reason.lower()

    def test_pass_balance_exactly_sufficient_with_reservations(self, conn, sample_strategy):
        # Balance $10000, reserve $9800, need exactly $200
        order_id = _insert_order(conn, sample_strategy, symbol="MSFT")
        _insert_reservation(conn, sample_strategy, order_id, 9800.0)
        ok, reason = check_buy_guard(conn, sample_strategy, "GOOGL", 200.0)
        assert ok is True
        assert reason == ""

    def test_fail_strategy_not_found(self, conn):
        ok, reason = check_buy_guard(conn, 9999, "AAPL", 100.0)
        assert ok is False
        assert "not found" in reason.lower()


class TestSellGuard:
    def test_pass_position_with_available_shares(self, conn, sample_strategy):
        _insert_position(conn, sample_strategy, "AAPL", qty=100, reserved_qty=0)
        ok, reason = check_sell_guard(conn, sample_strategy, "AAPL", 50)
        assert ok is True
        assert reason == ""

    def test_fail_no_position(self, conn, sample_strategy):
        ok, reason = check_sell_guard(conn, sample_strategy, "AAPL", 10)
        assert ok is False
        assert "no position" in reason.lower()

    def test_fail_insufficient_shares(self, conn, sample_strategy):
        # qty=10, reserved_qty=5 → available=5, try to sell 10
        _insert_position(conn, sample_strategy, "AAPL", qty=10, reserved_qty=5)
        ok, reason = check_sell_guard(conn, sample_strategy, "AAPL", 10)
        assert ok is False
        assert "insufficient" in reason.lower()

    def test_pass_sell_exactly_available(self, conn, sample_strategy):
        _insert_position(conn, sample_strategy, "AAPL", qty=10, reserved_qty=5)
        ok, reason = check_sell_guard(conn, sample_strategy, "AAPL", 5)
        assert ok is True
        assert reason == ""

    def test_fail_zero_qty_position_treated_as_no_position(self, conn, sample_strategy):
        # Guard queries qty > 0 so a row with qty=0 should fail as "no position"
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO positions (strategy_id, symbol, qty, reserved_qty, avg_entry_price, "
            "opened_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sample_strategy, "AAPL", 0, 0, 150.0, now, now),
        )
        conn.commit()
        ok, reason = check_sell_guard(conn, sample_strategy, "AAPL", 1)
        assert ok is False
        assert "no position" in reason.lower()
