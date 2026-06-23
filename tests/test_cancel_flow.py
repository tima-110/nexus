"""Tests for the rewritten cancel flow: broker confirmation before local mark."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from nexus.cli import app
from nexus.db import init_db

runner = CliRunner()


def _setup_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    conn.execute(
        "INSERT INTO broker_accounts (profile_name, margin_multiplier, cash_balance)"
        " VALUES (?, ?, ?)",
        ("paper1", 2.0, 100000.0),
    )
    conn.execute(
        "INSERT INTO strategies (name, broker_account_id, cash_balance, is_active, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            "test_strat",
            1,
            10000.0,
            1,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    cur = conn.execute(
        "INSERT INTO orders (strategy_id, symbol, side, qty, order_type, status,"
        " client_order_id, broker_order_id, actor, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            1,
            "AAPL",
            "buy",
            10,
            "market",
            "submitted",
            "nx-test-AAPL-abcdef01",
            "broker-xyz",
            "cli:manual",
            datetime.now(timezone.utc).isoformat(),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    return conn, cur.lastrowid


def _insert_reservation(conn, order_id):
    conn.execute(
        "INSERT INTO reservations (strategy_id, order_id, amount, created_at)"
        " VALUES (?, ?, ?, ?)",
        (1, order_id, 1500.0, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


class TestCancelBrokerConfirms:
    @patch("nexus.cli.order.AlpacaBroker")
    @patch("nexus.cli.order.get_connection")
    @patch("nexus.cli.order.init_db")
    def test_cancel_marks_cancelled_when_broker_confirms(
        self, mock_init_db, mock_get_conn, mock_broker_cls
    ):
        conn, order_id = _setup_conn()
        _insert_reservation(conn, order_id)
        mock_get_conn.return_value = conn

        mock_broker = MagicMock()
        mock_broker_cls.return_value = mock_broker
        mock_broker.cancel_order.return_value = None
        verified = MagicMock()
        verified.status = "canceled"
        mock_broker.get_order.return_value = verified

        result = runner.invoke(app, ["order", "cancel", str(order_id)])
        assert result.exit_code == 0

        row = conn.execute(
            "SELECT status, cancel_attempts FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        assert row["status"] == "cancelled"
        assert row["cancel_attempts"] == 0
        # Reservation released
        assert conn.execute(
            "SELECT * FROM reservations WHERE order_id = ?", (order_id,)
        ).fetchone() is None

    @patch("nexus.cli.order.AlpacaBroker")
    @patch("nexus.cli.order.get_connection")
    @patch("nexus.cli.order.init_db")
    def test_cancel_accepts_cancelled_variants(
        self, mock_init_db, mock_get_conn, mock_broker_cls
    ):
        conn, _ = _setup_conn()
        mock_get_conn.return_value = conn

        mock_broker = MagicMock()
        mock_broker_cls.return_value = mock_broker
        mock_broker.cancel_order.return_value = None

        for variant in ("cancelled", "canceled", "expired"):
            # Reset the order to 'submitted' so the cancellable guard passes
            # for each variant.
            conn.execute(
                "UPDATE orders SET status = 'submitted', cancel_attempts = 0"
                " WHERE id = (SELECT MIN(id) FROM orders)"
            )
            order_id = conn.execute("SELECT MIN(id) AS id FROM orders").fetchone()["id"]
            conn.execute(
                "DELETE FROM reservations WHERE order_id = ?", (order_id,)
            )
            _insert_reservation(conn, order_id)
            conn.commit()

            verified = MagicMock()
            verified.status = variant
            mock_broker.get_order.return_value = verified

            result = runner.invoke(app, ["order", "cancel", str(order_id)])
            assert result.exit_code == 0, f"variant {variant}: {result.output}"

            row = conn.execute(
                "SELECT status FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            assert row["status"] == "cancelled", f"variant {variant}"


class TestCancelBrokerFails:
    @patch("nexus.cli.order.AlpacaBroker")
    @patch("nexus.cli.order.get_connection")
    @patch("nexus.cli.order.init_db")
    def test_cancel_marks_pending_when_broker_raises(
        self, mock_init_db, mock_get_conn, mock_broker_cls
    ):
        conn, order_id = _setup_conn()
        _insert_reservation(conn, order_id)
        mock_get_conn.return_value = conn

        mock_broker = MagicMock()
        mock_broker_cls.return_value = mock_broker
        mock_broker.cancel_order.side_effect = RuntimeError("network timeout")

        result = runner.invoke(app, ["order", "cancel", str(order_id)])
        # Exit code is 0 (warning, not error — reconciler will retry)
        assert result.exit_code == 0

        row = conn.execute(
            "SELECT status, cancel_attempts FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        assert row["status"] == "cancel_pending"
        assert row["cancel_attempts"] == 1
        # Reservation NOT released
        assert conn.execute(
            "SELECT * FROM reservations WHERE order_id = ?", (order_id,)
        ).fetchone() is not None

    @patch("nexus.cli.order.AlpacaBroker")
    @patch("nexus.cli.order.get_connection")
    @patch("nexus.cli.order.init_db")
    def test_cancel_increments_attempts_on_repeated_failure(
        self, mock_init_db, mock_get_conn, mock_broker_cls
    ):
        conn, order_id = _setup_conn()
        _insert_reservation(conn, order_id)
        mock_get_conn.return_value = conn

        mock_broker = MagicMock()
        mock_broker_cls.return_value = mock_broker
        mock_broker.cancel_order.side_effect = RuntimeError("timeout")

        for expected_attempts in (1, 2):
            result = runner.invoke(app, ["order", "cancel", str(order_id)])
            assert result.exit_code == 0
            row = conn.execute(
                "SELECT status, cancel_attempts FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            assert row["status"] == "cancel_pending"
            assert row["cancel_attempts"] == expected_attempts


class TestCancelBrokerReturnsStillOpen:
    @patch("nexus.cli.order.AlpacaBroker")
    @patch("nexus.cli.order.get_connection")
    @patch("nexus.cli.order.init_db")
    def test_cancel_pending_when_broker_says_still_open(
        self, mock_init_db, mock_get_conn, mock_broker_cls
    ):
        conn, order_id = _setup_conn()
        _insert_reservation(conn, order_id)
        mock_get_conn.return_value = conn

        mock_broker = MagicMock()
        mock_broker_cls.return_value = mock_broker
        mock_broker.cancel_order.return_value = None  # broker call "succeeded"
        verified = MagicMock()
        verified.status = "new"  # but order is still open on broker
        mock_broker.get_order.return_value = verified

        result = runner.invoke(app, ["order", "cancel", str(order_id)])
        assert result.exit_code == 0

        row = conn.execute(
            "SELECT status, cancel_attempts FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        assert row["status"] == "cancel_pending"
        assert row["cancel_attempts"] == 1
        # Reservation NOT released
        assert conn.execute(
            "SELECT * FROM reservations WHERE order_id = ?", (order_id,)
        ).fetchone() is not None


class TestCancelSuccessResetsAttempts:
    @patch("nexus.cli.order.AlpacaBroker")
    @patch("nexus.cli.order.get_connection")
    @patch("nexus.cli.order.init_db")
    def test_cancel_success_after_pending_resets_attempts(
        self, mock_init_db, mock_get_conn, mock_broker_cls
    ):
        conn, order_id = _setup_conn()
        _insert_reservation(conn, order_id)
        # Pre-load cancel_attempts=2 (would have failed twice before)
        conn.execute(
            "UPDATE orders SET cancel_attempts = 2 WHERE id = ?", (order_id,)
        )
        conn.commit()
        mock_get_conn.return_value = conn

        mock_broker = MagicMock()
        mock_broker_cls.return_value = mock_broker
        mock_broker.cancel_order.return_value = None
        verified = MagicMock()
        verified.status = "canceled"
        mock_broker.get_order.return_value = verified

        result = runner.invoke(app, ["order", "cancel", str(order_id)])
        assert result.exit_code == 0

        row = conn.execute(
            "SELECT status, cancel_attempts FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        assert row["status"] == "cancelled"
        assert row["cancel_attempts"] == 0
        # Reservation released
        assert conn.execute(
            "SELECT * FROM reservations WHERE order_id = ?", (order_id,)
        ).fetchone() is None


class TestCancelRejectsWrongStatus:
    @patch("nexus.cli.order.get_connection")
    @patch("nexus.cli.order.init_db")
    def test_cancel_rejects_filled_order(self, mock_init_db, mock_get_conn):
        conn, order_id = _setup_conn()
        conn.execute(
            "UPDATE orders SET status = 'filled' WHERE id = ?", (order_id,)
        )
        conn.commit()
        mock_get_conn.return_value = conn

        result = runner.invoke(app, ["order", "cancel", str(order_id)])
        assert result.exit_code != 0
        assert "cannot be cancelled" in result.output


class TestOrderResolve:
    @patch("nexus.cli.order.get_connection")
    @patch("nexus.cli.order.init_db")
    def test_resolve_force_cancel_releases_reservation(self, mock_init_db, mock_get_conn):
        conn, order_id = _setup_conn()
        _insert_reservation(conn, order_id)
        conn.execute(
            "UPDATE orders SET status = 'cancel_failed', cancel_attempts = 3 WHERE id = ?",
            (order_id,),
        )
        conn.commit()
        mock_get_conn.return_value = conn

        result = runner.invoke(app, ["order", "resolve", str(order_id), "--action", "force-cancel"])
        assert result.exit_code == 0

        row = conn.execute(
            "SELECT status, cancel_attempts FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        assert row["status"] == "cancelled"
        assert row["cancel_attempts"] == 0
        assert conn.execute(
            "SELECT * FROM reservations WHERE order_id = ?", (order_id,)
        ).fetchone() is None

    @patch("nexus.cli.order.get_connection")
    @patch("nexus.cli.order.init_db")
    def test_resolve_reset_moves_to_cancel_pending(self, mock_init_db, mock_get_conn):
        conn, order_id = _setup_conn()
        _insert_reservation(conn, order_id)
        conn.execute(
            "UPDATE orders SET status = 'cancel_failed', cancel_attempts = 3 WHERE id = ?",
            (order_id,),
        )
        conn.commit()
        mock_get_conn.return_value = conn

        result = runner.invoke(app, ["order", "resolve", str(order_id), "--action", "reset"])
        assert result.exit_code == 0

        row = conn.execute(
            "SELECT status, cancel_attempts FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        assert row["status"] == "cancel_pending"
        assert row["cancel_attempts"] == 0
        # Reservation NOT released (broker still holds it)
        assert conn.execute(
            "SELECT * FROM reservations WHERE order_id = ?", (order_id,)
        ).fetchone() is not None

    @patch("nexus.cli.order.get_connection")
    @patch("nexus.cli.order.init_db")
    def test_resolve_rejects_non_cancel_failed_order(self, mock_init_db, mock_get_conn):
        conn, order_id = _setup_conn()
        mock_get_conn.return_value = conn

        result = runner.invoke(app, ["order", "resolve", str(order_id), "--action", "force-cancel"])
        assert result.exit_code != 0
        assert "only cancel_failed orders can be resolved" in result.output

    @patch("nexus.cli.order.get_connection")
    @patch("nexus.cli.order.init_db")
    def test_resolve_rejects_invalid_action(self, mock_init_db, mock_get_conn):
        conn, order_id = _setup_conn()
        conn.execute(
            "UPDATE orders SET status = 'cancel_failed' WHERE id = ?", (order_id,)
        )
        conn.commit()
        mock_get_conn.return_value = conn

        result = runner.invoke(app, ["order", "resolve", str(order_id), "--action", "nuke"])
        assert result.exit_code != 0
        assert "--action must be one of" in result.output
