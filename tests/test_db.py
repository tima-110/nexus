"""Tests for nexus.db — schema creation and constraints."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from nexus.db import init_db


@pytest.fixture
def empty_conn():
    """Fresh in-memory connection without schema."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


class TestInitDb:
    def test_all_tables_exist(self, empty_conn):
        init_db(empty_conn)
        tables = {
            row[0]
            for row in empty_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert tables == {
            "broker_accounts",
            "strategies",
            "orders",
            "positions",
            "transactions",
            "reservations",
            "option_positions",
        }

    def test_idempotent_double_call(self, empty_conn):
        """Calling init_db twice must not raise an error."""
        init_db(empty_conn)
        init_db(empty_conn)  # second call should be a no-op


class TestUniqueConstraints:
    def test_duplicate_broker_profile_name_raises(self, conn):
        conn.execute(
            "INSERT INTO broker_accounts (profile_name) VALUES (?)", ("dup",)
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO broker_accounts (profile_name) VALUES (?)", ("dup",)
            )

    def test_duplicate_strategy_name_raises(self, conn, sample_broker):
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO strategies (name, broker_account_id, cash_balance, is_active, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("strat_a", sample_broker, 0.0, 1, now),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO strategies (name, broker_account_id, cash_balance, is_active, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("strat_a", sample_broker, 0.0, 1, now),
            )

    def test_duplicate_client_order_id_raises(self, conn, sample_strategy):
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO orders (strategy_id, symbol, side, qty, order_type, status, "
            "client_order_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sample_strategy, "AAPL", "buy", 10, "market", "submitted", "coid-1", now),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO orders (strategy_id, symbol, side, qty, order_type, status, "
                "client_order_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sample_strategy, "TSLA", "sell", 5, "market", "submitted", "coid-1", now),
            )


class TestForeignKeyEnforcement:
    def test_order_with_nonexistent_strategy_raises(self, conn):
        """Inserting an order that references a strategy that doesn't exist should fail."""
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO orders (strategy_id, symbol, side, qty, order_type, status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (9999, "AAPL", "buy", 10, "market", "submitted"),
            )
            conn.commit()

    def test_strategy_with_nonexistent_broker_raises(self, conn):
        """Inserting a strategy that references a broker that doesn't exist should fail."""
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO strategies (name, broker_account_id, cash_balance, is_active) "
                "VALUES (?, ?, ?, ?)",
                ("orphan_strat", 9999, 0.0, 1),
            )
            conn.commit()
