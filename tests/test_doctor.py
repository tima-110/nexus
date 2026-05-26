"""Tests for nexus.doctor — health check module."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from nexus.config import NexusConfig
from nexus.doctor import run_doctor, DoctorCheck


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _find_check(checks: list[DoctorCheck], name: str) -> DoctorCheck:
    """Find a check by name."""
    for check in checks:
        if check.name == name:
            return check
    raise ValueError(f"Check '{name}' not found in {[c.name for c in checks]}")


class TestDbValid:
    @patch("nexus.doctor.get_schedule_status", return_value={"installed": False, "schedule": None, "command": None})
    @patch("subprocess.run")
    def test_db_valid_passes_with_schema(self, mock_subprocess, mock_cron, conn, sample_broker):
        """db_valid check should pass when all tables are present."""
        # Make broker call fail gracefully (not reachable)
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="error")

        config = NexusConfig()
        checks = run_doctor(conn, config)
        check = _find_check(checks, "db_valid")
        assert check.passed is True
        assert "all tables present" in check.detail

    @patch("nexus.doctor.get_schedule_status", return_value={"installed": False, "schedule": None, "command": None})
    @patch("subprocess.run")
    def test_db_valid_fails_without_tables(self, mock_subprocess, mock_cron):
        """db_valid check should fail when tables are missing."""
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="error")

        raw_conn = sqlite3.connect(":memory:")
        raw_conn.row_factory = sqlite3.Row

        config = NexusConfig()
        checks = run_doctor(raw_conn, config)
        check = _find_check(checks, "db_valid")
        assert check.passed is False
        assert "missing tables" in check.detail


class TestOrphanedReservations:
    @patch("nexus.doctor.get_schedule_status", return_value={"installed": False, "schedule": None, "command": None})
    @patch("subprocess.run")
    def test_no_orphaned_reservations_passes_clean(self, mock_subprocess, mock_cron, conn, sample_broker):
        """Should pass when there are no orphaned reservations."""
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="error")

        config = NexusConfig()
        checks = run_doctor(conn, config)
        check = _find_check(checks, "no_orphaned_reservations")
        assert check.passed is True

    @patch("nexus.doctor.get_schedule_status", return_value={"installed": False, "schedule": None, "command": None})
    @patch("subprocess.run")
    def test_no_orphaned_reservations_fails_with_orphan(self, mock_subprocess, mock_cron, conn, sample_strategy):
        """Should fail when a reservation is tied to a terminal-state order."""
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="error")

        # Insert a filled order with a reservation still attached
        conn.execute(
            "INSERT INTO orders (strategy_id, symbol, side, qty, order_type, status, "
            "client_order_id, actor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sample_strategy, "AAPL", "buy", 10, "market", "filled", "nx-orphan-1", "test", _now()),
        )
        order_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO reservations (strategy_id, order_id, amount, created_at) VALUES (?, ?, ?, ?)",
            (sample_strategy, order_id, 1500.0, _now()),
        )
        conn.commit()

        config = NexusConfig()
        checks = run_doctor(conn, config)
        check = _find_check(checks, "no_orphaned_reservations")
        assert check.passed is False
        assert "orphaned" in check.detail


class TestStaleOrders:
    @patch("nexus.doctor.get_schedule_status", return_value={"installed": False, "schedule": None, "command": None})
    @patch("subprocess.run")
    def test_no_stale_orders_passes(self, mock_subprocess, mock_cron, conn, sample_broker):
        """Should pass when there are no orders at all."""
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="error")

        config = NexusConfig()
        checks = run_doctor(conn, config)
        check = _find_check(checks, "no_stale_orders")
        assert check.passed is True

    @patch("nexus.doctor.get_schedule_status", return_value={"installed": False, "schedule": None, "command": None})
    @patch("subprocess.run")
    def test_no_stale_orders_fails(self, mock_subprocess, mock_cron, conn, sample_strategy):
        """Should fail when a submitted order is older than 24 hours."""
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="error")

        old_time = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        conn.execute(
            "INSERT INTO orders (strategy_id, symbol, side, qty, order_type, status, "
            "client_order_id, actor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sample_strategy, "AAPL", "buy", 10, "market", "submitted", "nx-stale-1", "test", old_time),
        )
        conn.commit()

        config = NexusConfig()
        checks = run_doctor(conn, config)
        check = _find_check(checks, "no_stale_orders")
        assert check.passed is False
        assert "stale" in check.detail


class TestBalanceConsistent:
    @patch("nexus.doctor.get_schedule_status", return_value={"installed": False, "schedule": None, "command": None})
    @patch("subprocess.run")
    def test_balance_consistent_passes(self, mock_subprocess, mock_cron, conn, sample_strategy):
        """Should pass when strategy balance matches transaction sum."""
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="error")

        # Strategy has cash_balance=10000 from fixture; add a matching transaction
        conn.execute(
            "INSERT INTO transactions (strategy_id, type, amount, actor, created_at) VALUES (?, ?, ?, ?, ?)",
            (sample_strategy, "deposit", 10000.0, "test", _now()),
        )
        conn.commit()

        config = NexusConfig()
        checks = run_doctor(conn, config)
        check = _find_check(checks, "balance_consistent")
        assert check.passed is True

    @patch("nexus.doctor.get_schedule_status", return_value={"installed": False, "schedule": None, "command": None})
    @patch("subprocess.run")
    def test_balance_consistent_fails(self, mock_subprocess, mock_cron, conn, sample_strategy):
        """Should fail when strategy balance does not match transaction sum."""
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="error")

        # Strategy has cash_balance=10000 but no transactions -> drift of $10000
        config = NexusConfig()
        checks = run_doctor(conn, config)
        check = _find_check(checks, "balance_consistent")
        assert check.passed is False
        assert "drift" in check.detail


class TestCronCheck:
    @patch("nexus.doctor.get_schedule_status")
    @patch("subprocess.run")
    def test_cron_check_reports_installed(self, mock_subprocess, mock_cron_status, conn, sample_broker):
        """Cron check should pass when schedule is installed."""
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        mock_cron_status.return_value = {"installed": True, "schedule": "*/5 * * * *", "command": "nexus reconcile"}

        config = NexusConfig()
        checks = run_doctor(conn, config)
        check = _find_check(checks, "cron_installed")
        assert check.passed is True

    @patch("nexus.doctor.get_schedule_status")
    @patch("subprocess.run")
    def test_cron_check_reports_not_installed(self, mock_subprocess, mock_cron_status, conn, sample_broker):
        """Cron check should fail when schedule is not installed."""
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        mock_cron_status.return_value = {"installed": False, "schedule": None, "command": None}

        config = NexusConfig()
        checks = run_doctor(conn, config)
        check = _find_check(checks, "cron_installed")
        assert check.passed is False
