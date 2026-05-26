"""Tests for nexus.reconciler — background reconciliation sweep."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from nexus.config import NexusConfig
from nexus.reconciler import run_reconcile, ReconcileResult


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
