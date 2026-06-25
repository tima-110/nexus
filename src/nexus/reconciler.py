"""Background reconciler — periodic sweep to sync balances, orders, and detect drift."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from nexus.audit import log_event
from nexus.broker import AlpacaBroker
from nexus.config import NexusConfig, get_audit_path
from nexus.ledger import process_cancel, process_cancel_failed, process_cancel_pending, process_cancel_option
from nexus.models import OrderStatus
from nexus.occ import is_occ_symbol
from nexus.sync import sync_outstanding_orders

ET = ZoneInfo("America/New_York")

CANCEL_RETRY_LIMIT = 3
CANCELLED_TERMINAL_STATES = {"cancelled", "canceled", "expired"}


@dataclass
class ReconcileResult:
    orders_synced: int = 0
    orders_skipped: int = 0
    bypass_orders: list[str] = field(default_factory=list)
    balance_drift: dict[str, float] = field(default_factory=dict)
    orphans_cleaned: int = 0
    positions_synced: int = 0
    ghosts_detected: list[dict] = field(default_factory=list)
    ghosts_resolved: int = 0
    cancel_failed_count: int = 0
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


def _sync_orders(conn: sqlite3.Connection, result: ReconcileResult, strategy_name: str | None = None) -> None:
    """Step 2: Sync outstanding orders for each active strategy."""
    if strategy_name is not None:
        rows = conn.execute(
            """
            SELECT s.id AS strategy_id, ba.profile_name
            FROM strategies s
            JOIN broker_accounts ba ON s.broker_account_id = ba.id
            WHERE s.is_active = 1 AND s.name = ?
            """,
            (strategy_name,),
        ).fetchall()
    else:
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


def _sync_position_prices(
    conn: sqlite3.Connection, result: ReconcileResult, dry_run: bool
) -> None:
    """Step 3b: Sync avg_entry_price from broker for positions where it is NULL."""
    rows = conn.execute(
        "SELECT s.id AS strategy_id, ba.profile_name"
        " FROM strategies s JOIN broker_accounts ba ON s.broker_account_id = ba.id"
        " WHERE s.is_active = 1"
    ).fetchall()
    now_iso = datetime.now(timezone.utc).isoformat()
    for row in rows:
        try:
            broker = AlpacaBroker(row["profile_name"])
            positions = broker.get_positions()
            for pos in positions:
                if not dry_run:
                    cur = conn.execute(
                        "UPDATE positions SET avg_entry_price = ?, updated_at = ?"
                        " WHERE strategy_id = ? AND symbol = ? AND avg_entry_price IS NULL",
                        (float(pos.avg_entry_price), now_iso, row["strategy_id"], pos.symbol),
                    )
                    result.positions_synced += cur.rowcount
            if not dry_run:
                conn.commit()
        except RuntimeError as e:
            result.errors.append(f"position_price_sync({row['profile_name']}): {e}")


def _cleanup_orphan_reservations(
    conn: sqlite3.Connection, result: ReconcileResult, dry_run: bool
) -> None:
    """Step 4: Remove reservations tied to terminal-state orders."""
    orphans = conn.execute(
        """
        SELECT r.id, r.order_id
        FROM reservations r
        JOIN orders o ON r.order_id = o.id
        WHERE o.status IN ('filled', 'cancelled', 'cancel_failed', 'expired')
          AND r.order_id NOT IN (
            SELECT origin_order_id FROM option_positions
            WHERE origin_order_id IS NOT NULL AND qty > 0
          )
        """
    ).fetchall()

    count = len(orphans)
    if count > 0 and not dry_run:
        orphan_ids = [r["id"] for r in orphans]
        placeholders = ",".join("?" * len(orphan_ids))
        conn.execute(f"DELETE FROM reservations WHERE id IN ({placeholders})", orphan_ids)
        conn.commit()

    result.orphans_cleaned = count


def _sync_cancellation_state(
    conn: sqlite3.Connection,
    result: ReconcileResult,
    dry_run: bool,
    audit_path,
) -> None:
    """Step 5: Reconcile cancellation state against the broker.

    Cross-checks Nexus orders in cancellation-related states against the
    broker's open orders:

    - `cancelled` (Nexus) + still open on broker → ghost. Re-issue cancel.
      On broker failure, bump `cancel_attempts`; promote to `cancel_failed`
      after CANCEL_RETRY_LIMIT attempts.
    - `cancel_pending` (Nexus) + cancelled/expired on broker → finalize via
      `process_cancel`.
    - `cancel_pending` (Nexus) + still open on broker → re-issue cancel,
      bump `cancel_attempts`; promote to `cancel_failed` after the limit.
    - `cancel_failed` (Nexus) → skip (manual intervention required). Logged
      via audit.

    One `list_orders(status='open')` call per broker profile (deduplicated
    across strategies).
    """
    # Collect candidate orders grouped by broker profile.
    rows = conn.execute(
        """
        SELECT o.id, o.broker_order_id, o.status, o.symbol, o.strategy_id,
               o.cancel_attempts, s.name AS strategy_name,
               ba.profile_name AS profile_name
        FROM orders o
        JOIN strategies s ON o.strategy_id = s.id
        JOIN broker_accounts ba ON s.broker_account_id = ba.id
        WHERE o.status IN ('cancelled', 'cancel_pending', 'cancel_failed')
          AND o.broker_order_id IS NOT NULL
        """
    ).fetchall()

    if not rows:
        return

    # Group by profile to issue one list_orders call per profile.
    by_profile: dict[str, list] = {}
    for row in rows:
        by_profile.setdefault(row["profile_name"], []).append(row)

    for profile_name, profile_rows in by_profile.items():
        try:
            broker = AlpacaBroker(profile_name)
            open_orders = broker.list_orders("open")
        except RuntimeError as exc:
            result.errors.append(f"cancellation_sync({profile_name}): {exc}")
            continue

        open_ids = {o.broker_order_id for o in open_orders if o.broker_order_id}

        for row in profile_rows:
            order_id = row["id"]
            broker_order_id = row["broker_order_id"]
            status = row["status"]
            attempts = row["cancel_attempts"] or 0
            ghost_record = {
                "order_id": order_id,
                "broker_order_id": broker_order_id,
                "symbol": row["symbol"],
                "strategy": row["strategy_name"],
                "profile": profile_name,
                "local_status": status,
                "broker_open": broker_order_id in open_ids,
            }

            if status == "cancel_failed":
                # Already exhausted retries — skip.
                result.ghosts_detected.append({**ghost_record, "action": "skipped"})
                continue

            if not ghost_record["broker_open"]:
                # Broker says it's not open. Verify it's actually terminal,
                # otherwise we might race with a fill that just happened.
                # If we can't determine, leave it alone.
                if status == "cancel_pending":
                    # Re-check broker state via direct get to be sure.
                    try:
                        state = broker.get_order(broker_order_id)
                        if state.status in CANCELLED_TERMINAL_STATES:
                            if not dry_run:
                                # Use option-aware cancel for option orders
                                if is_occ_symbol(row["symbol"]):
                                    process_cancel_option(conn, order_id)
                                else:
                                    process_cancel(conn, order_id)
                                log_event(audit_path, {
                                    "event": "ghost_order_resolved",
                                    "order_id": order_id,
                                    "broker_order_id": broker_order_id,
                                    "symbol": row["symbol"],
                                    "strategy": row["strategy_name"],
                                    "action": "confirmed_cancelled",
                                })
                            result.ghosts_detected.append({**ghost_record, "action": "confirmed_cancelled"})
                            result.ghosts_resolved += 1
                        # else: it's filled or otherwise terminal locally
                        # — process_fill/process_cancel already handles that
                        # in the eager-sync path.
                    except RuntimeError as exc:
                        result.errors.append(
                            f"cancellation_sync.get_order({broker_order_id}): {exc}"
                        )
                # For `cancelled` + not-open: nothing to do, DB is correct.
                # For `cancel_failed` + not-open: should not happen (broker
                # caught up), but nothing to do either.
                continue

            # Ghost detected: Nexus says cancelled/pending, broker still open.
            result.ghosts_detected.append({**ghost_record, "action": "re_cancel_attempted"})

            if dry_run:
                continue

            re_cancel_error: str | None = None
            try:
                broker.cancel_order(broker_order_id)
            except RuntimeError as exc:
                re_cancel_error = str(exc)

            if re_cancel_error is not None:
                # Broker rejected the re-cancel. Bump counter via process_cancel_pending
                # (which increments cancel_attempts), then promote to cancel_failed
                # if we've hit the retry limit.
                process_cancel_pending(conn, order_id)
                new_attempts = attempts + 1
                if new_attempts >= CANCEL_RETRY_LIMIT:
                    process_cancel_failed(conn, order_id)
                    log_event(audit_path, {
                        "event": "cancel_failed",
                        "order_id": order_id,
                        "broker_order_id": broker_order_id,
                        "symbol": row["symbol"],
                        "strategy": row["strategy_name"],
                        "cancel_attempts": new_attempts,
                        "reason": f"re_cancel_failed: {re_cancel_error}",
                    })
                    result.cancel_failed_count += 1
                    result.ghosts_detected.append({
                        **ghost_record,
                        "action": "promoted_to_cancel_failed",
                    })
                else:
                    log_event(audit_path, {
                        "event": "ghost_order_detected",
                        "order_id": order_id,
                        "broker_order_id": broker_order_id,
                        "symbol": row["symbol"],
                        "strategy": row["strategy_name"],
                        "cancel_attempts": new_attempts,
                        "action": "re_cancel_failed",
                        "error": re_cancel_error,
                    })
                continue

            # Re-cancel sent. Mark this sweep as resolved; if the broker
            # confirms on the next sweep, eager-sync/process_cancel will
            # finalize the DB state. We don't flip status to 'cancelled'
            # here — we only flip when we *see* the broker confirm.
            log_event(audit_path, {
                "event": "ghost_order_detected",
                "order_id": order_id,
                "broker_order_id": broker_order_id,
                "symbol": row["symbol"],
                "strategy": row["strategy_name"],
                "cancel_attempts": attempts,
                "action": "re_cancelled",
            })
            result.ghosts_resolved += 1


def run_reconcile(
    conn: sqlite3.Connection,
    config: NexusConfig,
    dry_run: bool = False,
    strategy_name: str | None = None,
) -> ReconcileResult:
    """Run a full reconciliation sweep.

    Args:
        conn: SQLite connection with Row row_factory.
        config: Nexus configuration (includes reconciler settings).
        dry_run: If True, report what would change without modifying data.
        strategy_name: If provided, only reconcile orders for this strategy.

    Returns:
        ReconcileResult summarizing actions taken.
    """
    result = ReconcileResult()
    market_hours_only = config.reconciler.market_hours_only
    outside_market = market_hours_only and not _is_market_hours()
    audit_path = get_audit_path(config)

    # Step 1: Balance sync (skip outside market hours if configured)
    if not outside_market:
        _sync_balances(conn, result, dry_run)

    # Step 2: Order sync (always runs)
    _sync_orders(conn, result, strategy_name=strategy_name)

    # Step 3: Bypass detection (skip outside market hours if configured)
    if not outside_market:
        _detect_bypass_orders(conn, result)

    # Step 3b: Sync NULL avg_entry_price from broker (always runs)
    _sync_position_prices(conn, result, dry_run)

    # Step 4: Orphan cleanup (skip outside market hours if configured)
    if not outside_market:
        _cleanup_orphan_reservations(conn, result, dry_run)

    # Step 5: Cancellation-state sync (always runs — safety net for ghost
    # orders. Critical correctness; should never be gated by market hours.)
    _sync_cancellation_state(conn, result, dry_run, audit_path)

    return result
