"""Tests for nexus.ledger — financial operations."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from nexus.ledger import (
    create_reservation,
    process_cancel,
    process_fill,
    record_transaction,
    release_reservation,
    release_shares,
    reserve_shares,
    update_position_on_fill,
)
from nexus.models import OrderStatus


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert_order(
    conn,
    strategy_id,
    symbol="AAPL",
    side="buy",
    status="submitted",
    qty=10,
    client_order_id=None,
    actor="test",
):
    cur = conn.execute(
        "INSERT INTO orders (strategy_id, symbol, side, qty, order_type, status, "
        "client_order_id, actor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            strategy_id,
            symbol,
            side,
            qty,
            "market",
            status,
            client_order_id,
            actor,
            _now(),
        ),
    )
    conn.commit()
    return cur.lastrowid


def _insert_position(conn, strategy_id, symbol="AAPL", qty=100, reserved_qty=0, avg_price=150.0):
    now = _now()
    conn.execute(
        "INSERT INTO positions (strategy_id, symbol, qty, reserved_qty, avg_entry_price, "
        "opened_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (strategy_id, symbol, qty, reserved_qty, avg_price, now, now),
    )
    conn.commit()


class TestCreateReservation:
    def test_inserts_row_and_returns_id(self, conn, sample_strategy):
        order_id = _insert_order(conn, sample_strategy)
        res_id = create_reservation(conn, sample_strategy, order_id, 1500.0)
        conn.commit()
        assert isinstance(res_id, int)
        row = conn.execute(
            "SELECT * FROM reservations WHERE id = ?", (res_id,)
        ).fetchone()
        assert row is not None
        assert row["amount"] == pytest.approx(1500.0)
        assert row["strategy_id"] == sample_strategy
        assert row["order_id"] == order_id


class TestReleaseReservation:
    def test_deletes_row(self, conn, sample_strategy):
        order_id = _insert_order(conn, sample_strategy)
        create_reservation(conn, sample_strategy, order_id, 1500.0)
        conn.commit()
        release_reservation(conn, order_id)
        conn.commit()
        row = conn.execute(
            "SELECT * FROM reservations WHERE order_id = ?", (order_id,)
        ).fetchone()
        assert row is None


class TestReserveShares:
    def test_increments_reserved_qty(self, conn, sample_strategy):
        _insert_position(conn, sample_strategy, "AAPL", qty=100, reserved_qty=0)
        reserve_shares(conn, sample_strategy, "AAPL", 30)
        conn.commit()
        row = conn.execute(
            "SELECT reserved_qty FROM positions WHERE strategy_id = ? AND symbol = ?",
            (sample_strategy, "AAPL"),
        ).fetchone()
        assert row["reserved_qty"] == 30


class TestReleaseShares:
    def test_decrements_reserved_qty(self, conn, sample_strategy):
        _insert_position(conn, sample_strategy, "AAPL", qty=100, reserved_qty=40)
        release_shares(conn, sample_strategy, "AAPL", 15)
        conn.commit()
        row = conn.execute(
            "SELECT reserved_qty FROM positions WHERE strategy_id = ? AND symbol = ?",
            (sample_strategy, "AAPL"),
        ).fetchone()
        assert row["reserved_qty"] == 25


class TestRecordTransaction:
    def test_inserts_transaction_and_updates_balance(self, conn, sample_strategy):
        # Initial balance is 10000
        txn_id = record_transaction(
            conn, sample_strategy, None, "deposit", 500.0, "test"
        )
        conn.commit()
        assert isinstance(txn_id, int)

        txn_row = conn.execute(
            "SELECT * FROM transactions WHERE id = ?", (txn_id,)
        ).fetchone()
        assert txn_row["amount"] == pytest.approx(500.0)
        assert txn_row["type"] == "deposit"

        strat_row = conn.execute(
            "SELECT cash_balance FROM strategies WHERE id = ?", (sample_strategy,)
        ).fetchone()
        assert strat_row["cash_balance"] == pytest.approx(10500.0)

    def test_negative_amount_decreases_balance(self, conn, sample_strategy):
        record_transaction(conn, sample_strategy, None, "fill_buy", -200.0, "test")
        conn.commit()
        row = conn.execute(
            "SELECT cash_balance FROM strategies WHERE id = ?", (sample_strategy,)
        ).fetchone()
        assert row["cash_balance"] == pytest.approx(9800.0)


class TestUpdatePositionOnFill:
    def test_creates_new_position_on_first_buy(self, conn, sample_strategy):
        update_position_on_fill(conn, sample_strategy, "AAPL", "buy", 10, 150.0)
        conn.commit()
        row = conn.execute(
            "SELECT * FROM positions WHERE strategy_id = ? AND symbol = ?",
            (sample_strategy, "AAPL"),
        ).fetchone()
        assert row is not None
        assert row["qty"] == 10
        assert row["avg_entry_price"] == pytest.approx(150.0)

    def test_updates_avg_price_on_second_buy(self, conn, sample_strategy):
        # First buy: 10 shares @ 100
        update_position_on_fill(conn, sample_strategy, "AAPL", "buy", 10, 100.0)
        conn.commit()
        # Second buy: 10 shares @ 200 → new avg = (10*100 + 10*200) / 20 = 150
        update_position_on_fill(conn, sample_strategy, "AAPL", "buy", 10, 200.0)
        conn.commit()
        row = conn.execute(
            "SELECT qty, avg_entry_price FROM positions WHERE strategy_id = ? AND symbol = ?",
            (sample_strategy, "AAPL"),
        ).fetchone()
        assert row["qty"] == 20
        assert row["avg_entry_price"] == pytest.approx(150.0)

    def test_decreases_qty_on_sell(self, conn, sample_strategy):
        _insert_position(conn, sample_strategy, "AAPL", qty=50, reserved_qty=0)
        update_position_on_fill(conn, sample_strategy, "AAPL", "sell", 20, 160.0)
        conn.commit()
        row = conn.execute(
            "SELECT qty FROM positions WHERE strategy_id = ? AND symbol = ?",
            (sample_strategy, "AAPL"),
        ).fetchone()
        assert row["qty"] == 30

    def test_deletes_position_when_qty_reaches_zero(self, conn, sample_strategy):
        _insert_position(conn, sample_strategy, "AAPL", qty=10, reserved_qty=0)
        update_position_on_fill(conn, sample_strategy, "AAPL", "sell", 10, 160.0)
        conn.commit()
        row = conn.execute(
            "SELECT * FROM positions WHERE strategy_id = ? AND symbol = ?",
            (sample_strategy, "AAPL"),
        ).fetchone()
        assert row is None


class TestProcessFill:
    def test_buy_fill_updates_order_releases_reservation_creates_position_decreases_balance(
        self, conn, sample_strategy
    ):
        order_id = _insert_order(conn, sample_strategy, "AAPL", "buy", qty=10)
        create_reservation(conn, sample_strategy, order_id, 1500.0)
        conn.commit()

        filled_at = _now()
        process_fill(conn, order_id, 10, 150.0, filled_at)

        # Order status updated
        order = conn.execute(
            "SELECT status, filled_qty, filled_avg_price FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        assert order["status"] == OrderStatus.filled
        assert order["filled_qty"] == 10
        assert order["filled_avg_price"] == pytest.approx(150.0)

        # Reservation released
        res = conn.execute(
            "SELECT * FROM reservations WHERE order_id = ?", (order_id,)
        ).fetchone()
        assert res is None

        # Transaction created
        txn = conn.execute(
            "SELECT * FROM transactions WHERE order_id = ?", (order_id,)
        ).fetchone()
        assert txn is not None
        assert txn["amount"] == pytest.approx(-1500.0)  # 10 * 150 debit

        # Position created
        pos = conn.execute(
            "SELECT * FROM positions WHERE strategy_id = ? AND symbol = ?",
            (sample_strategy, "AAPL"),
        ).fetchone()
        assert pos is not None
        assert pos["qty"] == 10

        # Balance decreased: 10000 - 1500 = 8500
        strat = conn.execute(
            "SELECT cash_balance FROM strategies WHERE id = ?", (sample_strategy,)
        ).fetchone()
        assert strat["cash_balance"] == pytest.approx(8500.0)

    def test_sell_fill_updates_order_releases_shares_creates_transaction_decreases_position_increases_balance(
        self, conn, sample_strategy
    ):
        # Set up a position
        _insert_position(conn, sample_strategy, "TSLA", qty=50, reserved_qty=20)
        order_id = _insert_order(conn, sample_strategy, "TSLA", "sell", qty=20)
        conn.commit()

        filled_at = _now()
        process_fill(conn, order_id, 20, 200.0, filled_at)

        # Order status updated
        order = conn.execute(
            "SELECT status FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        assert order["status"] == OrderStatus.filled

        # Shares released (reserved_qty decremented)
        pos = conn.execute(
            "SELECT qty, reserved_qty FROM positions WHERE strategy_id = ? AND symbol = ?",
            (sample_strategy, "TSLA"),
        ).fetchone()
        assert pos["qty"] == 30  # 50 - 20
        assert pos["reserved_qty"] == 0  # 20 - 20

        # Transaction created with positive amount
        txn = conn.execute(
            "SELECT * FROM transactions WHERE order_id = ?", (order_id,)
        ).fetchone()
        assert txn is not None
        assert txn["amount"] == pytest.approx(4000.0)  # 20 * 200 credit

        # Balance increased: 10000 + 4000 = 14000
        strat = conn.execute(
            "SELECT cash_balance FROM strategies WHERE id = ?", (sample_strategy,)
        ).fetchone()
        assert strat["cash_balance"] == pytest.approx(14000.0)


class TestProcessCancel:
    def test_cancel_buy_updates_status_and_releases_reservation(self, conn, sample_strategy):
        order_id = _insert_order(conn, sample_strategy, "AAPL", "buy", qty=10)
        create_reservation(conn, sample_strategy, order_id, 1500.0)
        conn.commit()

        process_cancel(conn, order_id)

        order = conn.execute(
            "SELECT status FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        assert order["status"] == OrderStatus.cancelled

        # Reservation deleted
        res = conn.execute(
            "SELECT * FROM reservations WHERE order_id = ?", (order_id,)
        ).fetchone()
        assert res is None

        # No transaction created
        txn = conn.execute(
            "SELECT * FROM transactions WHERE order_id = ?", (order_id,)
        ).fetchone()
        assert txn is None

    def test_cancel_sell_updates_status_and_releases_shares(self, conn, sample_strategy):
        _insert_position(conn, sample_strategy, "AAPL", qty=100, reserved_qty=30)
        order_id = _insert_order(conn, sample_strategy, "AAPL", "sell", qty=30)
        conn.commit()

        process_cancel(conn, order_id)

        order = conn.execute(
            "SELECT status FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        assert order["status"] == OrderStatus.cancelled

        # Shares released
        pos = conn.execute(
            "SELECT reserved_qty FROM positions WHERE strategy_id = ? AND symbol = ?",
            (sample_strategy, "AAPL"),
        ).fetchone()
        assert pos["reserved_qty"] == 0  # 30 - 30

        # No transaction created
        txn = conn.execute(
            "SELECT * FROM transactions WHERE order_id = ?", (order_id,)
        ).fetchone()
        assert txn is None

    def test_cancel_raises_for_missing_order(self, conn):
        with pytest.raises(ValueError, match="not found"):
            process_cancel(conn, 9999)
