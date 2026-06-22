"""Tests for nexus.reconciler — background reconciliation sweep."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from decimal import Decimal

from nexus.broker.types import BrokerOrder, BrokerPosition
from nexus.config import NexusConfig
from nexus.reconciler import (
    CANCEL_RETRY_LIMIT,
    ReconcileResult,
    _sync_cancellation_state,
    _sync_position_prices,
    run_reconcile,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_subprocess_result(data, returncode=0, stderr=""):
    """Create a mock subprocess.CompletedProcess with JSON stdout."""
    result = MagicMock()
    result.returncode = returncode
    result.stdout = json.dumps(data) if isinstance(data, (dict, list)) else data
    result.stderr = stderr
    return result


def _insert_order(conn, strategy_id, symbol="AAPL", side="buy", status="submitted",
                  qty=10, client_order_id="nx-test-AAPL-12345678", actor="test"):
    cur = conn.execute(
        "INSERT INTO orders (strategy_id, symbol, side, qty, order_type, status, "
        "client_order_id, broker_order_id, actor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (strategy_id, symbol, side, qty, "market", status, client_order_id,
         "broker-abc", actor, _now()),
    )
    conn.commit()
    return cur.lastrowid


def _insert_reservation(conn, strategy_id, order_id, amount=1500.0):
    cur = conn.execute(
        "INSERT INTO reservations (strategy_id, order_id, amount, created_at) VALUES (?, ?, ?, ?)",
        (strategy_id, order_id, amount, _now()),
    )
    conn.commit()
    return cur.lastrowid


def _subprocess_side_effect(account_data=None, order_list_data=None, order_get_data=None):
    """Create a side_effect function that inspects subprocess args to return correct data."""
    def side_effect(args, **kwargs):
        cmd = args
        if "account" in cmd and "get" in cmd:
            if account_data is None:
                return _make_subprocess_result({}, returncode=1, stderr="connection error")
            return _make_subprocess_result(account_data)
        elif "order" in cmd and "list" in cmd:
            return _make_subprocess_result(order_list_data or [])
        elif "order" in cmd and "get-by-client-id" in cmd:
            return _make_subprocess_result(order_get_data or {})
        elif "order" in cmd and "get" in cmd:
            return _make_subprocess_result(order_get_data or {})
        # Default: success with empty object
        return _make_subprocess_result({})
    return side_effect


class TestBalanceSync:
    @patch("subprocess.run")
    def test_balance_sync_updates_broker_cache(self, mock_subprocess, conn, sample_broker):
        """Balance sync should update broker_accounts.cash_balance from broker."""
        account_data = {"cash": "105000.00", "buying_power": "200000.00", "equity": "150000.00"}
        mock_subprocess.side_effect = _subprocess_side_effect(
            account_data=account_data,
            order_list_data=[],
        )

        config = NexusConfig()
        result = run_reconcile(conn, config)

        row = conn.execute(
            "SELECT cash_balance FROM broker_accounts WHERE id = ?", (sample_broker,)
        ).fetchone()
        assert row["cash_balance"] == pytest.approx(105000.0)
        assert "paper1" in result.balance_drift
        assert result.balance_drift["paper1"] == pytest.approx(5000.0)

    @patch("subprocess.run")
    def test_balance_sync_records_error_on_failure(self, mock_subprocess, conn, sample_broker):
        """Balance sync should record error when broker API fails, not crash."""
        mock_subprocess.return_value = _make_subprocess_result(
            "", returncode=1, stderr="connection refused"
        )

        config = NexusConfig()
        result = run_reconcile(conn, config)

        assert len(result.errors) >= 1
        assert "balance_sync" in result.errors[0]
        # Cash balance should be unchanged
        row = conn.execute(
            "SELECT cash_balance FROM broker_accounts WHERE id = ?", (sample_broker,)
        ).fetchone()
        assert row["cash_balance"] == pytest.approx(100000.0)


class TestOrderSync:
    @patch("subprocess.run")
    def test_order_sync_processes_fills(self, mock_subprocess, conn, sample_strategy):
        """Order sync should update a submitted order to filled when broker reports it."""
        order_id = _insert_order(conn, sample_strategy, client_order_id="nx-test-AAPL-12345678")
        _insert_reservation(conn, sample_strategy, order_id, 1500.0)

        account_data = {"cash": "100000.00", "buying_power": "200000.00", "equity": "100000.00"}
        order_get_data = {
            "id": "broker-abc",
            "client_order_id": "nx-test-AAPL-12345678",
            "status": "filled",
            "symbol": "AAPL",
            "side": "buy",
            "qty": "10",
            "filled_qty": "10",
            "filled_avg_price": "150.00",
            "submitted_at": _now(),
            "filled_at": _now(),
        }

        def side_effect(args, **kwargs):
            cmd = args
            if "account" in cmd and "get" in cmd:
                return _make_subprocess_result(account_data)
            elif "order" in cmd and "list" in cmd:
                return _make_subprocess_result([])
            elif "order" in cmd and "get-by-client-id" in cmd:
                return _make_subprocess_result(order_get_data)
            return _make_subprocess_result({})

        mock_subprocess.side_effect = side_effect

        config = NexusConfig()
        result = run_reconcile(conn, config)

        order = conn.execute(
            "SELECT status, filled_qty FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        assert order["status"] == "filled"
        assert order["filled_qty"] == 10
        assert result.orders_synced >= 1


class TestBypassDetection:
    @patch("subprocess.run")
    def test_bypass_detection_finds_non_nexus_orders(self, mock_subprocess, conn, sample_broker):
        """Bypass detection should flag orders without nx- prefix as external."""
        account_data = {"cash": "100000.00", "buying_power": "200000.00", "equity": "100000.00"}
        bypass_order = {
            "id": "ext-1",
            "client_order_id": "external-123",
            "status": "submitted",
            "symbol": "TSLA",
            "side": "buy",
            "qty": "5",
            "filled_qty": "0",
            "filled_avg_price": None,
            "submitted_at": _now(),
            "filled_at": None,
        }

        def side_effect(args, **kwargs):
            cmd = args
            if "account" in cmd and "get" in cmd:
                return _make_subprocess_result(account_data)
            elif "order" in cmd and "list" in cmd:
                return _make_subprocess_result([bypass_order])
            return _make_subprocess_result({})

        mock_subprocess.side_effect = side_effect

        config = NexusConfig()
        result = run_reconcile(conn, config)

        assert "ext-1" in result.bypass_orders


class TestOrphanCleanup:
    @patch("subprocess.run")
    def test_orphan_cleanup_removes_stale_reservations(self, mock_subprocess, conn, sample_strategy):
        """Orphan cleanup should remove reservations tied to filled orders."""
        order_id = _insert_order(
            conn, sample_strategy, status="filled", client_order_id="nx-test-AAPL-filled"
        )
        _insert_reservation(conn, sample_strategy, order_id, 1500.0)

        account_data = {"cash": "100000.00", "buying_power": "200000.00", "equity": "100000.00"}
        mock_subprocess.side_effect = _subprocess_side_effect(
            account_data=account_data,
            order_list_data=[],
        )

        config = NexusConfig()
        result = run_reconcile(conn, config)

        res = conn.execute(
            "SELECT * FROM reservations WHERE order_id = ?", (order_id,)
        ).fetchone()
        assert res is None
        assert result.orphans_cleaned == 1

    @patch("subprocess.run")
    def test_dry_run_does_not_modify(self, mock_subprocess, conn, sample_strategy):
        """Dry run should report orphans but not delete them."""
        order_id = _insert_order(
            conn, sample_strategy, status="filled", client_order_id="nx-test-AAPL-dry"
        )
        _insert_reservation(conn, sample_strategy, order_id, 1500.0)

        account_data = {"cash": "100000.00", "buying_power": "200000.00", "equity": "100000.00"}
        mock_subprocess.side_effect = _subprocess_side_effect(
            account_data=account_data,
            order_list_data=[],
        )

        config = NexusConfig()
        result = run_reconcile(conn, config, dry_run=True)

        # Reservation should still exist
        res = conn.execute(
            "SELECT * FROM reservations WHERE order_id = ?", (order_id,)
        ).fetchone()
        assert res is not None
        # But result should report it
        assert result.orphans_cleaned == 1


class TestMarketHoursGate:
    @patch("nexus.reconciler._is_market_hours", return_value=False)
    @patch("subprocess.run")
    def test_market_hours_gate(self, mock_subprocess, mock_market_hours, conn, sample_strategy):
        """When outside market hours with market_hours_only=True, skip balance/bypass/orphan."""
        order_id = _insert_order(
            conn, sample_strategy, status="filled", client_order_id="nx-test-AAPL-mkt"
        )
        _insert_reservation(conn, sample_strategy, order_id, 1500.0)

        # Should not be called for account/list because gate skips those steps
        mock_subprocess.side_effect = _subprocess_side_effect(order_list_data=[])

        config = NexusConfig()
        config.reconciler.market_hours_only = True
        result = run_reconcile(conn, config)

        # Balance sync skipped (no drift reported)
        assert len(result.balance_drift) == 0
        # Bypass detection skipped
        assert len(result.bypass_orders) == 0
        # Orphan cleanup skipped (reservation still exists)
        res = conn.execute(
            "SELECT * FROM reservations WHERE order_id = ?", (order_id,)
        ).fetchone()
        assert res is not None
        assert result.orphans_cleaned == 0


class TestPositionPriceSync:
    def _insert_position(self, conn, strategy_id, symbol, avg_entry_price=None):
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO positions (strategy_id, symbol, qty, reserved_qty, avg_entry_price,"
            " opened_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (strategy_id, symbol, 10, 0, avg_entry_price, now, now),
        )
        conn.commit()

    @patch("nexus.reconciler.AlpacaBroker")
    def test_sync_populates_null_avg_entry_price(self, mock_broker_cls, conn, sample_strategy):
        """_sync_position_prices fills avg_entry_price when it is NULL."""
        self._insert_position(conn, sample_strategy, "AAPL", avg_entry_price=None)

        mock_broker = MagicMock()
        mock_broker_cls.return_value = mock_broker
        mock_broker.get_positions.return_value = [
            BrokerPosition(symbol="AAPL", qty=10, avg_entry_price=Decimal("150.00"),
                           current_price=Decimal("155.00"), unrealized_pl=Decimal("50.00")),
        ]

        result = ReconcileResult()
        _sync_position_prices(conn, result, dry_run=False)

        row = conn.execute(
            "SELECT avg_entry_price FROM positions WHERE strategy_id = ? AND symbol = ?",
            (sample_strategy, "AAPL"),
        ).fetchone()
        assert row["avg_entry_price"] == pytest.approx(150.0)
        assert result.positions_synced == 1

    @patch("nexus.reconciler.AlpacaBroker")
    def test_sync_does_not_overwrite_existing_avg_entry_price(self, mock_broker_cls, conn, sample_strategy):
        """_sync_position_prices skips rows that already have avg_entry_price set."""
        self._insert_position(conn, sample_strategy, "AAPL", avg_entry_price=140.0)

        mock_broker = MagicMock()
        mock_broker_cls.return_value = mock_broker
        mock_broker.get_positions.return_value = [
            BrokerPosition(symbol="AAPL", qty=10, avg_entry_price=Decimal("150.00"),
                           current_price=Decimal("155.00"), unrealized_pl=Decimal("50.00")),
        ]

        result = ReconcileResult()
        _sync_position_prices(conn, result, dry_run=False)

        row = conn.execute(
            "SELECT avg_entry_price FROM positions WHERE strategy_id = ? AND symbol = ?",
            (sample_strategy, "AAPL"),
        ).fetchone()
        assert row["avg_entry_price"] == pytest.approx(140.0)  # unchanged
        assert result.positions_synced == 0

    @patch("nexus.reconciler.AlpacaBroker")
    def test_sync_dry_run_does_not_modify(self, mock_broker_cls, conn, sample_strategy):
        """_sync_position_prices dry_run skips the update."""
        self._insert_position(conn, sample_strategy, "AAPL", avg_entry_price=None)

        mock_broker = MagicMock()
        mock_broker_cls.return_value = mock_broker
        mock_broker.get_positions.return_value = [
            BrokerPosition(symbol="AAPL", qty=10, avg_entry_price=Decimal("150.00"),
                           current_price=Decimal("155.00"), unrealized_pl=Decimal("50.00")),
        ]

        result = ReconcileResult()
        _sync_position_prices(conn, result, dry_run=True)

        row = conn.execute(
            "SELECT avg_entry_price FROM positions WHERE strategy_id = ? AND symbol = ?",
            (sample_strategy, "AAPL"),
        ).fetchone()
        assert row["avg_entry_price"] is None  # unchanged
        assert result.positions_synced == 0

    @patch("nexus.reconciler.AlpacaBroker")
    def test_sync_records_error_on_broker_failure(self, mock_broker_cls, conn, sample_strategy):
        """_sync_position_prices records error without crashing when broker fails."""
        mock_broker = MagicMock()
        mock_broker_cls.return_value = mock_broker
        mock_broker.get_positions.side_effect = RuntimeError("timeout")

        result = ReconcileResult()
        _sync_position_prices(conn, result, dry_run=False)

        assert len(result.errors) == 1
        assert "position_price_sync" in result.errors[0]


def _insert_cancel_state_order(
    conn, strategy_id, broker_id, status, cancel_attempts=0
):
    """Helper: insert an order in a cancellation-related state with a broker id."""
    cur = conn.execute(
        "INSERT INTO orders (strategy_id, symbol, side, qty, order_type, status,"
        " client_order_id, broker_order_id, cancel_attempts, actor, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            strategy_id,
            "AAPL",
            "buy",
            10,
            "market",
            status,
            f"nx-test-AAPL-{broker_id[:8]}",
            broker_id,
            cancel_attempts,
            "test",
            _now(),
            _now(),
        ),
    )
    conn.commit()
    return cur.lastrowid


class TestCancellationSync:
    @patch("nexus.reconciler.AlpacaBroker")
    def test_ghost_order_re_cancelled(self, mock_broker_cls, conn, sample_strategy):
        """Nexus='cancelled' + broker='open' → re-issue cancel; record ghost."""
        order_id = _insert_cancel_state_order(
            conn, sample_strategy, "broker-ghost-1", "cancelled"
        )

        mock_broker = MagicMock()
        mock_broker_cls.return_value = mock_broker
        mock_broker.list_orders.return_value = [
            BrokerOrder(
                broker_order_id="broker-ghost-1",
                client_order_id="nx-test-AAPL-broker-g",
                status="new",
                symbol="AAPL",
                side="buy",
                qty=10,
                filled_qty=0,
                filled_avg_price=None,
                submitted_at=_now(),
                filled_at=None,
            ),
        ]
        mock_broker.cancel_order.return_value = None

        result = ReconcileResult()
        audit_path = Path("/tmp/nexus-test-audit.jsonl")
        audit_path.unlink(missing_ok=True)
        _sync_cancellation_state(conn, result, dry_run=False, audit_path=audit_path)

        # Ghost recorded, broker re-cancel called
        assert len(result.ghosts_detected) == 1
        assert result.ghosts_detected[0]["local_status"] == "cancelled"
        assert result.ghosts_detected[0]["broker_open"] is True
        assert result.ghosts_resolved == 1
        mock_broker.cancel_order.assert_called_once_with("broker-ghost-1")

        # Local status unchanged — we wait for the next sweep to confirm
        row = conn.execute(
            "SELECT status FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        assert row["status"] == "cancelled"

    @patch("nexus.reconciler.AlpacaBroker")
    def test_cancel_pending_resolved_when_broker_confirms(
        self, mock_broker_cls, conn, sample_strategy
    ):
        """Nexus='cancel_pending' + broker='cancelled' → finalize via process_cancel."""
        order_id = _insert_cancel_state_order(
            conn, sample_strategy, "broker-pending-1", "cancel_pending"
        )
        # Add a reservation so we can verify it's released on finalize
        conn.execute(
            "INSERT INTO reservations (strategy_id, order_id, amount, created_at)"
            " VALUES (?, ?, ?, ?)",
            (sample_strategy, order_id, 1500.0, _now()),
        )
        conn.commit()

        mock_broker = MagicMock()
        mock_broker_cls.return_value = mock_broker
        mock_broker.list_orders.return_value = []  # not in open set → must verify
        mock_broker.get_order.return_value = BrokerOrder(
            broker_order_id="broker-pending-1",
            client_order_id="nx-test-AAPL-broker-p",
            status="canceled",
            symbol="AAPL",
            side="buy",
            qty=10,
            filled_qty=0,
            filled_avg_price=None,
            submitted_at=_now(),
            filled_at=None,
        )

        result = ReconcileResult()
        audit_path = Path("/tmp/nexus-test-audit.jsonl")
        audit_path.unlink(missing_ok=True)
        _sync_cancellation_state(conn, result, dry_run=False, audit_path=audit_path)

        row = conn.execute(
            "SELECT status, cancel_attempts FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        assert row["status"] == "cancelled"
        assert row["cancel_attempts"] == 0
        # Reservation released
        assert conn.execute(
            "SELECT * FROM reservations WHERE order_id = ?", (order_id,)
        ).fetchone() is None
        assert result.ghosts_resolved == 1

    @patch("nexus.reconciler.AlpacaBroker")
    def test_cancel_failed_skipped(self, mock_broker_cls, conn, sample_strategy):
        """Nexus='cancel_failed' → no broker call, just recorded as skipped."""
        _insert_cancel_state_order(
            conn, sample_strategy, "broker-failed-1", "cancel_failed"
        )

        mock_broker = MagicMock()
        mock_broker_cls.return_value = mock_broker
        mock_broker.list_orders.return_value = [
            BrokerOrder(
                broker_order_id="broker-failed-1",
                client_order_id="nx-test-AAPL-broker-f",
                status="new",
                symbol="AAPL",
                side="buy",
                qty=10,
                filled_qty=0,
                filled_avg_price=None,
                submitted_at=_now(),
                filled_at=None,
            ),
        ]

        result = ReconcileResult()
        audit_path = Path("/tmp/nexus-test-audit.jsonl")
        audit_path.unlink(missing_ok=True)
        _sync_cancellation_state(conn, result, dry_run=False, audit_path=audit_path)

        # Ghost recorded but no cancel issued
        assert len(result.ghosts_detected) == 1
        assert result.ghosts_detected[0]["action"] == "skipped"
        mock_broker.cancel_order.assert_not_called()

    @patch("nexus.reconciler.AlpacaBroker")
    def test_dry_run_reports_ghosts_without_changes(
        self, mock_broker_cls, conn, sample_strategy
    ):
        """dry_run=True → reports ghosts but does not call broker."""
        order_id = _insert_cancel_state_order(
            conn, sample_strategy, "broker-dry-1", "cancelled"
        )

        mock_broker = MagicMock()
        mock_broker_cls.return_value = mock_broker
        mock_broker.list_orders.return_value = [
            BrokerOrder(
                broker_order_id="broker-dry-1",
                client_order_id="nx-test-AAPL-broker-d",
                status="new",
                symbol="AAPL",
                side="buy",
                qty=10,
                filled_qty=0,
                filled_avg_price=None,
                submitted_at=_now(),
                filled_at=None,
            ),
        ]

        result = ReconcileResult()
        audit_path = Path("/tmp/nexus-test-audit.jsonl")
        audit_path.unlink(missing_ok=True)
        _sync_cancellation_state(conn, result, dry_run=True, audit_path=audit_path)

        # Ghost reported but no broker cancel, no resolved count
        assert len(result.ghosts_detected) == 1
        assert result.ghosts_resolved == 0
        mock_broker.cancel_order.assert_not_called()
        row = conn.execute(
            "SELECT status, cancel_attempts FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        assert row["status"] == "cancelled"
        assert row["cancel_attempts"] == 0

    @patch("nexus.reconciler.AlpacaBroker")
    def test_promote_to_cancel_failed_after_repeated_broker_rejects(
        self, mock_broker_cls, conn, sample_strategy
    ):
        """After CANCEL_RETRY_LIMIT broker failures, status becomes cancel_failed."""
        order_id = _insert_cancel_state_order(
            conn, sample_strategy,
            "broker-retry-1",
            "cancel_pending",
            cancel_attempts=CANCEL_RETRY_LIMIT - 1,
        )

        mock_broker = MagicMock()
        mock_broker_cls.return_value = mock_broker
        mock_broker.list_orders.return_value = [
            BrokerOrder(
                broker_order_id="broker-retry-1",
                client_order_id="nx-test-AAPL-broker-r",
                status="new",
                symbol="AAPL",
                side="buy",
                qty=10,
                filled_qty=0,
                filled_avg_price=None,
                submitted_at=_now(),
                filled_at=None,
            ),
        ]
        mock_broker.cancel_order.side_effect = RuntimeError("still rejecting")

        result = ReconcileResult()
        audit_path = Path("/tmp/nexus-test-audit.jsonl")
        audit_path.unlink(missing_ok=True)
        _sync_cancellation_state(conn, result, dry_run=False, audit_path=audit_path)

        row = conn.execute(
            "SELECT status, cancel_attempts FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        assert row["status"] == "cancel_failed"
        # process_cancel_pending bumps to CANCEL_RETRY_LIMIT, then we promote;
        # process_cancel_failed leaves cancel_attempts at the bumped value.
        assert row["cancel_attempts"] == CANCEL_RETRY_LIMIT
        assert result.cancel_failed_count == 1

    @patch("nexus.reconciler.AlpacaBroker")
    def test_one_list_orders_call_per_profile(
        self, mock_broker_cls, conn, sample_strategy
    ):
        """Reconcile batches by profile: one list_orders call per broker profile,
        regardless of how many orders are in cancellation-related states."""
        # Multiple orders in cancellation-related states on the same strategy.
        _insert_cancel_state_order(conn, sample_strategy, "broker-zzz-AAAA-1", "cancelled")
        _insert_cancel_state_order(conn, sample_strategy, "broker-yyy-BBBB-2", "cancelled")
        _insert_cancel_state_order(conn, sample_strategy, "broker-xxw-CCCC-3", "cancel_pending")

        mock_broker = MagicMock()
        mock_broker_cls.return_value = mock_broker
        mock_broker.list_orders.return_value = []  # nothing open
        mock_broker.get_order.side_effect = RuntimeError("not used")

        result = ReconcileResult()
        audit_path = Path("/tmp/nexus-test-audit.jsonl")
        audit_path.unlink(missing_ok=True)
        _sync_cancellation_state(conn, result, dry_run=False, audit_path=audit_path)

        # One list_orders call (not two)
        assert mock_broker.list_orders.call_count == 1
