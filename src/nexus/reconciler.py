"""Background reconciler — periodic sweep to sync balances, orders, and detect drift."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from nexus.broker import AlpacaBroker
from nexus.config import NexusConfig
from nexus.sync import sync_outstanding_orders

ET = ZoneInfo("America/New_York")


@dataclass
class ReconcileResult:
    orders_synced: int = 0
    orders_skipped: int = 0
    bypass_orders: list[str] = field(default_factory=list)
    balance_drift: dict[str, float] = field(default_factory=dict)
    orphans_cleaned: int = 0
    errors: list[str] = field(default_factory=list)


def _is_market_hours() -> bool:
    """Check if current time is Mon-Fri 9:30-16:00 ET."""
    now = datetime.now(ET)
    # Monday=0 ... Friday=4
    if now.weekday() > 4:
        return False
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now < market_close


def _sync_balances(conn: sqlite3.Connection, result: ReconcileResult, dry_run: bool) -> None:
    """Step 1: Sync cash balances from broker accounts."""
    rows = conn.execute("SELECT id, profile_name, cash_balance FROM broker_accounts").fetchall()
    for row in rows:
        profile_name = row["profile_name"]
        try:
            broker = AlpacaBroker(profile_name)
            account = broker.get_account()
            broker_cash = float(account.cash)
            local_cash = float(row["cash_balance"]) if row["cash_balance"] is not None else 0.0
            drift = abs(broker_cash - local_cash)
            result.balance_drift[profile_name] = drift

            if not dry_run:
                now_iso = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "UPDATE broker_accounts SET cash_balance = ?, last_synced_at = ? WHERE id = ?",
                    (broker_cash, now_iso, row["id"]),
                )
                conn.commit()
        except RuntimeError as e:
            result.errors.append(f"balance_sync({profile_name}): {e}")


def _sync_orders(conn: sqlite3.Connection, result: ReconcileResult) -> None:
    """Step 2: Sync outstanding orders for each active strategy."""
    rows = conn.execute(
        """
        SELECT s.id AS strategy_id, ba.profile_name
        FROM strategies s
        JOIN broker_accounts ba ON s.broker_account_id = ba.id
        WHERE s.is_active = 1
        """
    ).fetchall()

    for row in rows:
        strategy_id = row["strategy_id"]
        profile_name = row["profile_name"]
        try:
            broker = AlpacaBroker(profile_name)

            before_count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM orders WHERE strategy_id = ? AND status IN ('submitted', 'partially_filled')",
                (strategy_id,),
            ).fetchone()["cnt"]

            sync_outstanding_orders(conn, strategy_id, broker)

            after_count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM orders WHERE strategy_id = ? AND status IN ('submitted', 'partially_filled')",
                (strategy_id,),
            ).fetchone()["cnt"]

            result.orders_synced += before_count - after_count
        except RuntimeError:
            result.orders_skipped += 1


def _detect_bypass_orders(conn: sqlite3.Connection, result: ReconcileResult) -> None:
    """Step 3: Detect orders placed outside Nexus (no nx- prefix)."""
    rows = conn.execute("SELECT id, profile_name FROM broker_accounts").fetchall()
    for row in rows:
        profile_name = row["profile_name"]
        try:
            broker = AlpacaBroker(profile_name)
            open_orders = broker.list_orders("open")
            for order in open_orders:
                if order.client_order_id is None or not order.client_order_id.startswith("nx-"):
                    result.bypass_orders.append(order.broker_order_id)
        except RuntimeError as e:
            result.errors.append(f"bypass_detection({profile_name}): {e}")


def _cleanup_orphan_reservations(
    conn: sqlite3.Connection, result: ReconcileResult, dry_run: bool
) -> None:
    """Step 4: Remove reservations tied to terminal-state orders."""
    orphans = conn.execute(
        """
        SELECT r.id, r.order_id
        FROM reservations r
        JOIN orders o ON r.order_id = o.id
        WHERE o.status IN ('filled', 'cancelled', 'expired')
        """
    ).fetchall()

    count = len(orphans)
    if count > 0 and not dry_run:
        orphan_ids = [r["id"] for r in orphans]
        placeholders = ",".join("?" * len(orphan_ids))
        conn.execute(f"DELETE FROM reservations WHERE id IN ({placeholders})", orphan_ids)
        conn.commit()

    result.orphans_cleaned = count


def run_reconcile(
    conn: sqlite3.Connection,
    config: NexusConfig,
    dry_run: bool = False,
) -> ReconcileResult:
    """Run a full reconciliation sweep.

    Args:
        conn: SQLite connection with Row row_factory.
        config: Nexus configuration (includes reconciler settings).
        dry_run: If True, report what would change without modifying data.

    Returns:
        ReconcileResult summarizing actions taken.
    """
    result = ReconcileResult()
    market_hours_only = config.reconciler.market_hours_only
    outside_market = market_hours_only and not _is_market_hours()

    # Step 1: Balance sync (skip outside market hours if configured)
    if not outside_market:
        _sync_balances(conn, result, dry_run)

    # Step 2: Order sync (always runs)
    _sync_orders(conn, result)

    # Step 3: Bypass detection (skip outside market hours if configured)
    if not outside_market:
        _detect_bypass_orders(conn, result)

    # Step 4: Orphan cleanup (skip outside market hours if configured)
    if not outside_market:
        _cleanup_orphan_reservations(conn, result, dry_run)

    return result
