"""Health-check module for Nexus trading system."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nexus.broker import AlpacaBroker
from nexus.config import NexusConfig, get_audit_path
from nexus.schedule.cron import get_schedule_status


@dataclass
class DoctorCheck:
    name: str
    passed: bool
    detail: str


def run_doctor(conn: sqlite3.Connection, config: NexusConfig) -> list[DoctorCheck]:
    """Run all health checks and return results."""
    checks = []
    checks.append(_check_db_valid(conn))
    checks.append(_check_alpaca_reachable(conn))
    checks.append(_check_no_orphaned_reservations(conn))
    checks.append(_check_no_stale_orders(conn))
    checks.append(_check_balance_consistent(conn))
    checks.append(_check_audit_writable(config))
    checks.append(_check_cron_installed())
    return checks


def _check_db_valid(conn: sqlite3.Connection) -> DoctorCheck:
    """Verify expected tables exist in the database."""
    name = "db_valid"
    try:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        existing = {row["name"] for row in cursor.fetchall()}
        required = {
            "broker_accounts",
            "strategies",
            "orders",
            "positions",
            "transactions",
            "reservations",
        }
        missing = required - existing
        if missing:
            return DoctorCheck(
                name=name,
                passed=False,
                detail=f"missing tables: {', '.join(sorted(missing))}",
            )
        return DoctorCheck(name=name, passed=True, detail="all tables present")
    except Exception as e:
        return DoctorCheck(name=name, passed=False, detail=str(e))


def _check_alpaca_reachable(conn: sqlite3.Connection) -> DoctorCheck:
    """Try each broker profile and verify at least one is reachable."""
    name = "alpaca_reachable"
    try:
        cursor = conn.execute("SELECT profile_name FROM broker_accounts")
        profiles = [row["profile_name"] for row in cursor.fetchall()]
        if not profiles:
            return DoctorCheck(
                name=name,
                passed=False,
                detail="no broker accounts registered",
            )
        reachable = 0
        for profile in profiles:
            try:
                AlpacaBroker(profile).get_account()
                reachable += 1
            except Exception:
                pass
        total = len(profiles)
        if reachable == 0:
            return DoctorCheck(
                name=name,
                passed=False,
                detail=f"0/{total} profiles reachable",
            )
        return DoctorCheck(
            name=name,
            passed=True,
            detail=f"{reachable}/{total} profiles reachable",
        )
    except Exception as e:
        return DoctorCheck(name=name, passed=False, detail=str(e))


def _check_no_orphaned_reservations(conn: sqlite3.Connection) -> DoctorCheck:
    """Check for reservations tied to terminal orders."""
    name = "no_orphaned_reservations"
    try:
        cursor = conn.execute(
            "SELECT COUNT(*) as cnt FROM reservations r "
            "JOIN orders o ON r.order_id = o.id "
            "WHERE o.status IN ('filled','cancelled','expired')"
        )
        count = cursor.fetchone()["cnt"]
        if count > 0:
            return DoctorCheck(
                name=name,
                passed=False,
                detail=f"{count} orphaned reservations found",
            )
        return DoctorCheck(name=name, passed=True, detail="no orphaned reservations")
    except Exception as e:
        return DoctorCheck(name=name, passed=False, detail=str(e))


def _check_no_stale_orders(conn: sqlite3.Connection) -> DoctorCheck:
    """Check for orders stuck in submitted status for over 24 hours."""
    name = "no_stale_orders"
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        cursor = conn.execute(
            "SELECT COUNT(*) as cnt FROM orders "
            "WHERE status = 'submitted' AND created_at < ?",
            (cutoff,),
        )
        count = cursor.fetchone()["cnt"]
        if count > 0:
            return DoctorCheck(
                name=name,
                passed=False,
                detail=f"{count} stale orders (>24h)",
            )
        return DoctorCheck(name=name, passed=True, detail="no stale orders")
    except Exception as e:
        return DoctorCheck(name=name, passed=False, detail=str(e))


def _check_balance_consistent(conn: sqlite3.Connection) -> DoctorCheck:
    """Compare strategy cash balances against transaction sums."""
    name = "balance_consistent"
    try:
        cursor = conn.execute("SELECT id, name, cash_balance FROM strategies")
        strategies = cursor.fetchall()
        inconsistent = []
        for strat in strategies:
            tx_cursor = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) as total FROM transactions "
                "WHERE strategy_id = ?",
                (strat["id"],),
            )
            tx_total = tx_cursor.fetchone()["total"]
            drift = abs(strat["cash_balance"] - tx_total)
            if drift > 0.01:
                inconsistent.append(f"{strat['name']} (drift=${drift:.2f})")
        if inconsistent:
            return DoctorCheck(
                name=name,
                passed=False,
                detail=f"inconsistent: {', '.join(inconsistent)}",
            )
        return DoctorCheck(name=name, passed=True, detail="all balances consistent")
    except Exception as e:
        return DoctorCheck(name=name, passed=False, detail=str(e))


def _check_audit_writable(config: NexusConfig) -> DoctorCheck:
    """Verify the audit log file can be opened for writing."""
    name = "audit_writable"
    try:
        path = get_audit_path(config)
        with open(path, "a"):
            pass
        return DoctorCheck(name=name, passed=True, detail=f"writable: {path}")
    except Exception as e:
        return DoctorCheck(name=name, passed=False, detail=str(e))


def _check_cron_installed() -> DoctorCheck:
    """Verify the reconciler cron job is installed."""
    name = "cron_installed"
    try:
        status = get_schedule_status()
        if status["installed"]:
            return DoctorCheck(
                name=name,
                passed=True,
                detail=f"installed: {status['schedule']}",
            )
        return DoctorCheck(
            name=name,
            passed=False,
            detail="reconciler cron not installed",
        )
    except Exception as e:
        return DoctorCheck(name=name, passed=False, detail=str(e))
