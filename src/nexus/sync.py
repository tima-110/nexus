"""Eager sync: reconcile outstanding orders against the broker before processing commands."""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone

from nexus.broker import AlpacaBroker
from nexus.ledger import process_cancel, process_fill
from nexus.models import OrderStatus


def sync_outstanding_orders(
    conn: sqlite3.Connection, strategy_id: int, broker: AlpacaBroker
) -> None:
    """Sync outstanding orders for a strategy before processing new commands.

    Steps:
    1. Query DB for orders with status IN ('submitted', 'partially_filled') AND strategy_id = ?
    2. For each order:
       a. Call broker.get_order_by_client_id(order.client_order_id)
       b. If broker says 'filled' -> call process_fill()
       c. If broker says 'cancelled'/'expired' -> call process_cancel()
       d. If broker says 'partially_filled' and filled_qty changed -> update order fields
       e. Otherwise -> no-op (still pending at broker)
    3. Any broker error for a single order -> log/skip, continue to next order
    """
    rows = conn.execute(
        "SELECT id, client_order_id, filled_qty, filled_avg_price "
        "FROM orders WHERE strategy_id = ? AND status IN (?, ?)",
        (strategy_id, OrderStatus.submitted, OrderStatus.partially_filled),
    ).fetchall()

    for row in rows:
        order_id = row["id"]
        client_order_id = row["client_order_id"]

        try:
            broker_order = broker.get_order_by_client_id(client_order_id)
        except RuntimeError as exc:
            print(
                f"sync_outstanding_orders: broker error for order {order_id} "
                f"({client_order_id}): {exc}",
                file=sys.stderr,
            )
            continue

        broker_status = broker_order.status

        if broker_status == OrderStatus.filled:
            filled_at = broker_order.filled_at or ""
            filled_avg_price = float(broker_order.filled_avg_price) if broker_order.filled_avg_price is not None else 0.0
            process_fill(conn, order_id, broker_order.filled_qty, filled_avg_price, filled_at)

        elif broker_status in (OrderStatus.cancelled, OrderStatus.expired):
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
        # else: still pending/submitted at broker — no-op
