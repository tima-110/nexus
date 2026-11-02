"""Eager sync: reconcile outstanding orders against the broker before processing commands."""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone

from nexus.broker import AlpacaBroker
from nexus.ledger import process_cancel, process_cancel_option, process_fill, process_option_fill
from nexus.models import OrderStatus
from nexus.occ import is_occ_symbol


def _sync_order_legs(
    conn: sqlite3.Connection,
    parent_order_id: int,
    strategy_id: int,
    symbol: str,
    parent_side: str,
    parent_qty: int,
    order_class: str,
    actor: str,
    legs: list[dict] | None,
) -> None:
    """Sync child bracket/oco legs from a parent broker order."""
    if not legs:
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    for leg in legs:
        leg_broker_id = leg.get("id")
        leg_client_id = leg.get("client_order_id")
        leg_side = leg.get("side") or ("sell" if parent_side == "buy" else "buy")
        leg_qty = int(leg.get("qty") or parent_qty)
        leg_type = leg.get("order_type") or leg.get("type") or "limit"
        leg_limit = float(leg["limit_price"]) if leg.get("limit_price") is not None else None
        leg_stop = float(leg["stop_price"]) if leg.get("stop_price") is not None else None
        leg_status = leg.get("status") or "held"
        leg_tif = leg.get("time_in_force") or "gtc"

        child_row = None
        if leg_broker_id:
            child_row = conn.execute(
                "SELECT id, status, filled_qty FROM orders WHERE broker_order_id = ?", (leg_broker_id,)
            ).fetchone()
        if child_row is None and leg_client_id:
            child_row = conn.execute(
                "SELECT id, status, filled_qty FROM orders WHERE client_order_id = ?", (leg_client_id,)
            ).fetchone()

        if child_row is None:
            cur = conn.execute(
                "INSERT INTO orders ("
                "  strategy_id, symbol, side, qty, order_type, limit_price, stop_price,"
                "  time_in_force, asset_class, order_class, status, client_order_id,"
                "  broker_order_id, parent_order_id, actor, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'equity', ?, 'submitted', ?, ?, ?, ?, ?, ?)",
                (
                    strategy_id, symbol, leg_side, leg_qty, leg_type,
                    leg_limit, leg_stop, leg_tif, order_class,
                    leg_client_id, leg_broker_id, parent_order_id,
                    actor or "system:bracket", now_iso, now_iso,
                ),
            )
            conn.commit()
            child_order_id = cur.lastrowid
            child_status = "submitted"
            child_filled_qty = 0
        else:
            child_order_id = child_row["id"]
            child_status = child_row["status"]
            child_filled_qty = child_row["filled_qty"] or 0

        if leg_status == OrderStatus.filled and (child_status != OrderStatus.filled or child_filled_qty == 0):
            filled_at = leg.get("filled_at") or now_iso
            filled_avg_price = float(leg["filled_avg_price"]) if leg.get("filled_avg_price") is not None else 0.0
            filled_qty = int(leg.get("filled_qty") or leg_qty)
            process_fill(conn, child_order_id, filled_qty, filled_avg_price, filled_at)
        elif leg_status in (OrderStatus.cancelled, OrderStatus.expired, "replaced", "canceled") and child_status not in (OrderStatus.cancelled, OrderStatus.expired):
            process_cancel(conn, child_order_id)


def sync_outstanding_orders(
    conn: sqlite3.Connection, strategy_id: int, broker: AlpacaBroker
) -> None:
    """Sync outstanding orders for a strategy before processing new commands.

    Steps:
    1. Query DB for orders with status IN ('submitted', 'partially_filled',
       'cancel_pending', 'held', 'new', 'accepted') AND strategy_id = ?
    2. Also check parent bracket orders that have active or missing child legs.
    3. For each order:
       a. Call broker.get_order_by_client_id or broker.get_order
       b. Sync child legs if order is multi-leg
       c. If broker says 'filled' -> call process_fill()
       d. If broker says 'cancelled'/'expired' -> call process_cancel()
       e. If broker says 'partially_filled' and filled_qty changed -> update order fields
    """
    rows = conn.execute(
        "SELECT id, client_order_id, broker_order_id, symbol, side, qty, "
        "order_class, actor, filled_qty, filled_avg_price, status "
        "FROM orders WHERE strategy_id = ? AND status IN (?, ?, ?, ?, ?, ?)",
        (
            strategy_id,
            OrderStatus.submitted,
            OrderStatus.partially_filled,
            OrderStatus.cancel_pending,
            "held",
            "new",
            "accepted",
        ),
    ).fetchall()

    # Also include filled bracket/oco parent orders if their child legs are missing or non-terminal
    filled_parents = conn.execute(
        "SELECT id, client_order_id, broker_order_id, symbol, side, qty, "
        "order_class, actor, filled_qty, filled_avg_price, status "
        "FROM orders WHERE strategy_id = ? AND status = ? AND order_class IN ('bracket', 'oco') "
        "AND (id NOT IN (SELECT DISTINCT parent_order_id FROM orders WHERE parent_order_id IS NOT NULL) "
        "     OR id IN (SELECT DISTINCT parent_order_id FROM orders WHERE parent_order_id IS NOT NULL AND status IN ('submitted', 'held', 'new', 'accepted', 'partially_filled')))",
        (strategy_id, OrderStatus.filled),
    ).fetchall()

    all_rows = list(rows) + [p for p in filled_parents if p["id"] not in {r["id"] for r in rows}]

    for row in all_rows:
        order_id = row["id"]
        client_order_id = row["client_order_id"]
        broker_order_id = row["broker_order_id"]

        broker_order = None
        if client_order_id:
            try:
                broker_order = broker.get_order_by_client_id(client_order_id)
            except RuntimeError:
                pass
        if broker_order is None and broker_order_id:
            try:
                broker_order = broker.get_order(broker_order_id)
            except RuntimeError as exc:
                print(
                    f"sync_outstanding_orders: broker error for order {order_id} "
                    f"({broker_order_id or client_order_id}): {exc}",
                    file=sys.stderr,
                )
                continue

        if broker_order is None:
            continue

        broker_status = broker_order.status

        # Multi-leg sync: if broker returned child legs, ensure they are stored and synced
        if broker_order.legs:
            _sync_order_legs(
                conn,
                order_id,
                strategy_id,
                row["symbol"],
                row["side"],
                row["qty"],
                row["order_class"] or "bracket",
                row["actor"] or "system:bracket",
                broker_order.legs,
            )

        # If parent order is already filled, no further status transition needed for parent itself
        if row["status"] == OrderStatus.filled:
            continue

        # Routing decisions for option vs equity
        order_row = conn.execute(
            "SELECT symbol FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        is_option = order_row is not None and is_occ_symbol(order_row["symbol"])

        if broker_status == OrderStatus.filled:
            filled_at = broker_order.filled_at or ""
            filled_avg_price = float(broker_order.filled_avg_price) if broker_order.filled_avg_price is not None else 0.0

            if is_option:
                process_option_fill(
                    conn, order_id, broker_order.filled_qty,
                    filled_avg_price, filled_at,
                    filled_side=broker_order.side
                )
            else:
                process_fill(conn, order_id, broker_order.filled_qty, filled_avg_price, filled_at)

        elif broker_status in (OrderStatus.cancelled, OrderStatus.expired, "replaced", "canceled"):
            if is_option:
                process_cancel_option(conn, order_id)
            else:
                process_cancel(conn, order_id)

        elif broker_status == OrderStatus.partially_filled:
            db_filled_qty = row["filled_qty"] or 0
            if broker_order.filled_qty != db_filled_qty:
                filled_avg_price = float(broker_order.filled_avg_price) if broker_order.filled_avg_price is not None else None
                conn.execute(
                    "UPDATE orders SET filled_qty = ?, filled_avg_price = ?, updated_at = ? "
                    "WHERE id = ?",
                    (
                        broker_order.filled_qty,
                        filled_avg_price,
                        datetime.now(timezone.utc).isoformat(),
                        order_id,
                    ),
                )
                conn.commit()
