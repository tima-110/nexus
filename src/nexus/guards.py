"""Pre-order validation guards."""

from __future__ import annotations

import sqlite3

from nexus.occ import is_occ_symbol, parse_occ_symbol

ACTIVE_ORDER_STATUSES = (
    "pending",
    "submitted",
    "new",
    "accepted",
    "held",
    "partially_filled",
)


def _check_inflight_duplicate_order(
    conn: sqlite3.Connection,
    strategy_id: int,
    symbol: str,
    side: str,
) -> tuple[bool, str]:
    """Reject duplicate submission when an active non-terminal order exists."""
    placeholders = ", ".join("?" for _ in ACTIVE_ORDER_STATUSES)
    existing = conn.execute(
        f"SELECT id, status FROM orders"
        f" WHERE strategy_id = ? AND symbol = ? AND side = ? AND status IN ({placeholders})"
        f" ORDER BY id DESC LIMIT 1",
        (strategy_id, symbol, side, *ACTIVE_ORDER_STATUSES),
    ).fetchone()
    if existing is not None:
        return False, (
            f"active {side} order #{existing['id']} ({existing['status']}) already open for {symbol}"
        )
    return True, ""


def _get_available_cash(conn: sqlite3.Connection, strategy_id: int) -> tuple[float | None, str]:
    """Compute available cash for opening new positions (long or short).

    Subtracts active order reservations and locked short-sale proceeds + collateral
    (2 * abs(qty) * avg_entry_price for any open short equity position with qty < 0,
    since short sale proceeds were credited to cash_balance on fill).
    """
    strategy = conn.execute(
        "SELECT cash_balance FROM strategies WHERE id = ?",
        (strategy_id,),
    ).fetchone()
    if strategy is None:
        return None, f"strategy {strategy_id} not found"

    reserved = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM reservations WHERE strategy_id = ?",
        (strategy_id,),
    ).fetchone()[0]

    short_locked = conn.execute(
        "SELECT COALESCE(SUM(2.0 * ABS(qty) * COALESCE(avg_entry_price, 0.0)), 0.0) "
        "FROM positions WHERE strategy_id = ? AND qty < 0",
        (strategy_id,),
    ).fetchone()[0]

    available = float(strategy["cash_balance"]) - float(reserved) - float(short_locked)
    return available, ""


def check_buy_guard(
    conn: sqlite3.Connection,
    strategy_id: int,
    symbol: str,
    estimated_cost: float,
    *,
    short: bool = False,
    qty: int = 0,
    close_only: bool = False,
) -> tuple[bool, str]:
    """Check if a buy order is allowed.

    When short=False (default — Open Long):
    1. No active buy order already open for this symbol
    2. Strategy does NOT already hold the symbol (no duplicate positions)
    3. Available buying power >= estimated_cost

    When short=True (Close Short / Buy-to-Cover):
    1. No active buy order already open for this symbol
    2. Strategy MUST hold a short position in the symbol (qty < 0)
    3. Available short shares (abs(qty) - reserved_qty) >= requested qty

    Returns (True, "") if OK, (False, "reason") if blocked.
    """
    if is_occ_symbol(symbol):
        return check_option_buy_guard(
            conn, strategy_id, symbol, estimated_cost, close_only=close_only or short
        )

    ok, err = _check_inflight_duplicate_order(conn, strategy_id, symbol, "buy")
    if not ok:
        return False, err

    if short:
        position = conn.execute(
            "SELECT qty, reserved_qty FROM positions WHERE strategy_id = ? AND symbol = ? AND qty < 0",
            (strategy_id, symbol),
        ).fetchone()
        if position is None:
            return False, f"no short position in {symbol}"

        available_shares = abs(position["qty"]) - (position["reserved_qty"] or 0)
        if available_shares < qty:
            return False, (
                f"insufficient shares: need {qty}, have {available_shares} available"
            )
        return True, ""

    row = conn.execute(
        "SELECT id FROM positions WHERE strategy_id = ? AND symbol = ? AND qty != 0",
        (strategy_id, symbol),
    ).fetchone()
    if row is not None:
        return False, f"already holding position in {symbol}"

    available, err = _get_available_cash(conn, strategy_id)
    if available is None:
        return False, err

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
    *,
    short: bool = False,
    estimated_cost: float = 0.0,
) -> tuple[bool, str]:
    """Check if a sell order is allowed.

    When short=False (default — Close Long):
    1. Strategy MUST hold the symbol (position exists with qty > 0)
    2. Available shares (position.qty - position.reserved_qty) >= requested qty

    When short=True (Open Short — mirrors check_buy_guard with short=False):
    1. Strategy does NOT already hold the symbol (no duplicate positions)
    2. Available buying power >= estimated_cost

    Returns (True, "") if OK, (False, "reason") if blocked.
    """
    if is_occ_symbol(symbol):
        return check_option_sell_guard(conn, strategy_id, symbol, qty)

    ok, err = _check_inflight_duplicate_order(conn, strategy_id, symbol, "sell")
    if not ok:
        return False, err

    if short:
        row = conn.execute(
            "SELECT id FROM positions WHERE strategy_id = ? AND symbol = ? AND qty != 0",
            (strategy_id, symbol),
        ).fetchone()
        if row is not None:
            return False, f"already holding position in {symbol}"

        available, err = _get_available_cash(conn, strategy_id)
        if available is None:
            return False, err

        if available < estimated_cost:
            return False, (
                f"insufficient buying power: need {estimated_cost:.2f}, have {available:.2f}"
            )
        return True, ""

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


def check_option_sell_guard(
    conn: sqlite3.Connection,
    strategy_id: int,
    symbol: str,
    qty: int,
) -> tuple[bool, str]:
    """Check if an option sell order is allowed (open a short).

    Validates:
    - For puts (cash-secured): available cash >= strike * 100 * qty
    - For calls (covered): strategy holds >= 100 * qty shares of underlying

    Returns (True, "") if OK, (False, "reason") if blocked.
    """
    ok, err = _check_inflight_duplicate_order(conn, strategy_id, symbol, "sell")
    if not ok:
        return False, err

    parsed = parse_occ_symbol(symbol)
    strike = parsed["strike"]
    right = parsed["right"]
    underlying = parsed["root"]

    if right == "put":
        # Cash-secured put: need cash to cover assignment
        obligation = strike * 100 * qty
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
        if available < obligation:
            return False, (
                f"insufficient cash for cash-secured put: "
                f"need ${obligation:.2f} assignment obligation, "
                f"have ${available:.2f} available"
            )
        return True, ""

    else:
        # Covered call: need to hold the underlying shares
        position = conn.execute(
            "SELECT qty, reserved_qty FROM positions WHERE strategy_id = ? AND symbol = ? AND qty > 0",
            (strategy_id, underlying),
        ).fetchone()
        if position is None:
            return False, (
                f"cannot sell covered call on {underlying}: "
                f"no position held"
            )

        available = position["qty"] - position["reserved_qty"]
        needed = 100 * qty
        if available < needed:
            return False, (
                f"insufficient shares for covered call: "
                f"need {needed} shares of {underlying}, "
                f"have {available} available"
            )
        return True, ""


def check_option_buy_guard(
    conn: sqlite3.Connection,
    strategy_id: int,
    symbol: str,
    estimated_cost: float,
    *,
    close_only: bool = False,
) -> tuple[bool, str]:
    """Check if an option buy order is allowed (close a short or open a long).

    For closing a short: strategy must have an open short position in this symbol.
    For opening a long: blocked if close_only=True, or if long already exists.
    Always blocks if an active buy order is already in flight for this symbol.

    Returns (True, "") if OK, (False, "reason") if blocked.
    """
    ok, err = _check_inflight_duplicate_order(conn, strategy_id, symbol, "buy")
    if not ok:
        return False, err

    # Check if strategy has a short position in this symbol → closing
    short_pos = conn.execute(
        "SELECT id, qty FROM option_positions"
        " WHERE strategy_id = ? AND symbol = ? AND side = 'short' AND qty > 0",
        (strategy_id, symbol),
    ).fetchone()

    if short_pos is None:
        if close_only:
            return False, f"no short position in {symbol}"
        # Not closing a short — this is a buy-to-open. Block if long already exists.
        long_pos = conn.execute(
            "SELECT id FROM option_positions"
            " WHERE strategy_id = ? AND symbol = ? AND side = 'long' AND qty > 0",
            (strategy_id, symbol),
        ).fetchone()
        if long_pos is not None:
            return False, f"already holding long position in {symbol}"

    # Cash check for buying options
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
