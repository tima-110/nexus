"""Pre-order validation guards."""

from __future__ import annotations

import sqlite3


def check_buy_guard(
    conn: sqlite3.Connection,
    strategy_id: int,
    symbol: str,
    estimated_cost: float,
) -> tuple[bool, str]:
    """Check if a buy order is allowed.

    Validates:
    1. Strategy does NOT already hold the symbol (no duplicate positions)
    2. Available buying power >= estimated_cost

    Available buying power = strategy.cash_balance - sum(active reservations for this strategy)

    Returns (True, "") if OK, (False, "reason") if blocked.
    """
    row = conn.execute(
        "SELECT id FROM positions WHERE strategy_id = ? AND symbol = ? AND qty > 0",
        (strategy_id, symbol),
    ).fetchone()
    if row is not None:
        return False, f"already holding position in {symbol}"

    strategy = conn.execute(
        "SELECT cash_balance FROM strategies WHERE id = ?",
        (strategy_id,),
    ).fetchone()
    if strategy is None:
        return False, f"strategy {strategy_id} not found"

    reserved = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM reservations WHERE strategy_id = ?",
        (strategy_id,),
    ).fetchone()[0]

    available = strategy["cash_balance"] - reserved
    if available < estimated_cost:
        return False, (
            f"insufficient buying power: need {estimated_cost:.2f}, have {available:.2f}"
        )

    return True, ""


def check_sell_guard(
    conn: sqlite3.Connection,
    strategy_id: int,
    symbol: str,
    qty: int,
) -> tuple[bool, str]:
    """Check if a sell order is allowed.

    Validates:
    1. Strategy MUST hold the symbol (position exists with qty > 0)
    2. Available shares >= requested qty

    Available shares = position.qty - position.reserved_qty

    Returns (True, "") if OK, (False, "reason") if blocked.
    """
    position = conn.execute(
        "SELECT qty, reserved_qty FROM positions WHERE strategy_id = ? AND symbol = ? AND qty > 0",
        (strategy_id, symbol),
    ).fetchone()
    if position is None:
        return False, f"no position in {symbol}"

    available = position["qty"] - position["reserved_qty"]
    if available < qty:
        return False, (
            f"insufficient shares: need {qty}, have {available} available"
        )

    return True, ""
