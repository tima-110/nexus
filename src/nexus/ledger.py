"""Ledger: reservations, transactions, and balance mutations."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from nexus.models import OrderSide, OrderStatus, TransactionType


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_reservation(
    conn: sqlite3.Connection, strategy_id: int, order_id: int, amount: float
) -> int:
    """Insert a cash reservation for a buy order. Returns reservation ID."""
    cur = conn.execute(
        "INSERT INTO reservations (strategy_id, order_id, amount, created_at) VALUES (?, ?, ?, ?)",
        (strategy_id, order_id, amount, _now()),
    )
    return cur.lastrowid


def release_reservation(conn: sqlite3.Connection, order_id: int) -> None:
    """Delete the reservation associated with an order (called on fill or cancel)."""
    conn.execute("DELETE FROM reservations WHERE order_id = ?", (order_id,))


def reserve_shares(
    conn: sqlite3.Connection, strategy_id: int, symbol: str, qty: int
) -> None:
    """Increment reserved_qty on a position for a sell order."""
    conn.execute(
        "UPDATE positions SET reserved_qty = reserved_qty + ?, updated_at = ? "
        "WHERE strategy_id = ? AND symbol = ?",
        (qty, _now(), strategy_id, symbol),
    )


def release_shares(
    conn: sqlite3.Connection, strategy_id: int, symbol: str, qty: int
) -> None:
    """Decrement reserved_qty on a position (called on sell fill or cancel)."""
    conn.execute(
        "UPDATE positions SET reserved_qty = reserved_qty - ?, updated_at = ? "
        "WHERE strategy_id = ? AND symbol = ?",
        (qty, _now(), strategy_id, symbol),
    )


def record_transaction(
    conn: sqlite3.Connection,
    strategy_id: int,
    order_id: int | None,
    txn_type: str,
    amount: float,
    actor: str,
    note: str | None = None,
) -> int:
    """Insert a transaction record AND update strategy.cash_balance. Returns transaction ID.

    amount is positive for credits (sell proceeds, deposits), negative for debits (buy cost,
    withdrawals).
    """
    cur = conn.execute(
        "INSERT INTO transactions (strategy_id, order_id, type, amount, actor, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (strategy_id, order_id, txn_type, amount, actor, note, _now()),
    )
    conn.execute(
        "UPDATE strategies SET cash_balance = cash_balance + ? WHERE id = ?",
        (amount, strategy_id),
    )
    return cur.lastrowid


def update_position_on_fill(
    conn: sqlite3.Connection,
    strategy_id: int,
    symbol: str,
    side: str,
    qty: int,
    price: float,
) -> None:
    """Create or update position record after a fill.

    Buy: create position if not exists, or increase qty and recalculate avg_entry_price.
    Sell: decrease qty. If qty reaches 0, delete the position row.

    Average entry recalculation for buys:
      new_avg = ((old_qty * old_avg) + (fill_qty * fill_price)) / (old_qty + fill_qty)
    """
    now = _now()
    if side == OrderSide.buy:
        row = conn.execute(
            "SELECT qty, avg_entry_price FROM positions WHERE strategy_id = ? AND symbol = ?",
            (strategy_id, symbol),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO positions (strategy_id, symbol, qty, reserved_qty, avg_entry_price, "
                "opened_at, updated_at) VALUES (?, ?, ?, 0, ?, ?, ?)",
                (strategy_id, symbol, qty, price, now, now),
            )
        else:
            old_qty = row["qty"]
            old_avg = row["avg_entry_price"] or 0.0
            new_qty = old_qty + qty
            new_avg = ((old_qty * old_avg) + (qty * price)) / new_qty
            conn.execute(
                "UPDATE positions SET qty = ?, avg_entry_price = ?, updated_at = ? "
                "WHERE strategy_id = ? AND symbol = ?",
                (new_qty, new_avg, now, strategy_id, symbol),
            )
    else:
        # sell side
        row = conn.execute(
            "SELECT qty FROM positions WHERE strategy_id = ? AND symbol = ?",
            (strategy_id, symbol),
        ).fetchone()
        if row is not None:
            new_qty = row["qty"] - qty
            if new_qty <= 0:
                conn.execute(
                    "DELETE FROM positions WHERE strategy_id = ? AND symbol = ?",
                    (strategy_id, symbol),
                )
            else:
                conn.execute(
                    "UPDATE positions SET qty = ?, updated_at = ? "
                    "WHERE strategy_id = ? AND symbol = ?",
                    (new_qty, now, strategy_id, symbol),
                )


def process_fill(
    conn: sqlite3.Connection,
    order_id: int,
    filled_qty: int,
    filled_avg_price: float,
    filled_at: str,
) -> None:
    """Atomic processing of an order fill.

    Steps:
    1. Fetch the order row
    2. Update order: status='filled', filled_qty, filled_avg_price, filled_at, updated_at
    3. Release reservation (for buys: delete from reservations; for sells: release_shares)
    4. Record transaction (buy: negative amount; sell: positive)
    5. Update position (buy: add shares; sell: remove shares)
    6. Commit
    """
    order = conn.execute(
        "SELECT strategy_id, symbol, side, qty, actor FROM orders WHERE id = ?",
        (order_id,),
    ).fetchone()
    if order is None:
        raise ValueError(f"order {order_id} not found")

    strategy_id = order["strategy_id"]
    symbol = order["symbol"]
    side = order["side"]
    actor = order["actor"] or "system"

    # Step 2: update order status
    conn.execute(
        "UPDATE orders SET status = ?, filled_qty = ?, filled_avg_price = ?, "
        "filled_at = ?, updated_at = ? WHERE id = ?",
        (OrderStatus.filled, filled_qty, filled_avg_price, filled_at, _now(), order_id),
    )

    # Step 3: release reservation
    if side == OrderSide.buy:
        release_reservation(conn, order_id)
    else:
        release_shares(conn, strategy_id, symbol, filled_qty)

    # Step 4: record transaction
    fill_value = filled_qty * filled_avg_price
    if side == OrderSide.buy:
        amount = -fill_value
        txn_type = TransactionType.fill_buy
    else:
        amount = fill_value
        txn_type = TransactionType.fill_sell

    record_transaction(conn, strategy_id, order_id, txn_type, amount, actor)

    # Step 5: update position
    update_position_on_fill(conn, strategy_id, symbol, side, filled_qty, filled_avg_price)

    # Step 6: commit
    conn.commit()


def process_cancel(conn: sqlite3.Connection, order_id: int) -> None:
    """Process order cancellation.

    Steps:
    1. Fetch order row
    2. Update order: status='cancelled', cancel_attempts=0, updated_at
    3. Release reservation (buy: delete reservation row; sell: release_shares)
    4. Commit
    """
    order = conn.execute(
        "SELECT strategy_id, symbol, side, qty FROM orders WHERE id = ?",
        (order_id,),
    ).fetchone()
    if order is None:
        raise ValueError(f"order {order_id} not found")

    strategy_id = order["strategy_id"]
    symbol = order["symbol"]
    side = order["side"]
    qty = order["qty"]

    # Step 2: update order status
    conn.execute(
        "UPDATE orders SET status = ?, cancel_attempts = 0, updated_at = ? WHERE id = ?",
        (OrderStatus.cancelled, _now(), order_id),
    )

    # Step 3: release reservation
    if side == OrderSide.buy:
        release_reservation(conn, order_id)
    else:
        release_shares(conn, strategy_id, symbol, qty)

    # Step 4: commit
    conn.commit()


def process_cancel_pending(conn: sqlite3.Connection, order_id: int) -> None:
    """Mark an order as cancel_pending and bump cancel_attempts.

    Unlike process_cancel, this does NOT release the reservation — the
    broker order is still open and the shares/cash remain committed there.
    The reconciler (or a subsequent user-initiated retry) will call
    process_cancel once the broker confirms cancellation.
    """
    order = conn.execute(
        "SELECT id FROM orders WHERE id = ?", (order_id,)
    ).fetchone()
    if order is None:
        raise ValueError(f"order {order_id} not found")

    conn.execute(
        "UPDATE orders SET status = ?, cancel_attempts = COALESCE(cancel_attempts, 0) + 1, "
        "updated_at = ? WHERE id = ?",
        (OrderStatus.cancel_pending, _now(), order_id),
    )
    conn.commit()


def process_cancel_failed(conn: sqlite3.Connection, order_id: int) -> None:
    """Move a cancel_pending order to cancel_failed after exhausting retries.

    Does NOT release the reservation — the broker order is still open and
    consuming shares/cash. Manual intervention required.
    """
    order = conn.execute(
        "SELECT id FROM orders WHERE id = ?", (order_id,)
    ).fetchone()
    if order is None:
        raise ValueError(f"order {order_id} not found")

    conn.execute(
        "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
        (OrderStatus.cancel_failed, _now(), order_id),
    )
    conn.commit()
