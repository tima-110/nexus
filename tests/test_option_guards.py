"""Tests for option-specific guards."""
from __future__ import annotations

import pytest

from nexus.guards import check_option_buy_guard, check_option_sell_guard
from nexus.models import OrderStatus


@pytest.fixture
def conftest(conn, sample_strategy):
    """Return (conn, strategy_id) tuple for guard tests."""
    return conn, sample_strategy


def _setup_equity_position(conn, strategy_id: int, symbol: str, qty: int = 100):
    conn.execute(
        "INSERT INTO positions (strategy_id, symbol, qty, reserved_qty, avg_entry_price, opened_at, updated_at)"
        " VALUES (?, ?, ?, 0, 50.0, '2025-01-01T00:00:00', '2025-01-01T00:00:00')",
        (strategy_id, symbol, qty),
    )
    conn.commit()


def test_option_sell_put_cash_check(conftest):
    """A cash-secured put requires enough cash to cover assignment."""
    conn, strategy_id = conftest

    # Strategy has $10000 cash (from conftest), put strike is $50 x 100 x 1 = $5000 obligation
    ok, reason = check_option_sell_guard(conn, strategy_id, "NKE260718P00040000", 1)
    assert ok, f"Should pass with sufficient cash: {reason}"

    # Try with 3 contracts → $15000 obligation, only $10000 cash
    ok, reason = check_option_sell_guard(conn, strategy_id, "NKE260718P00040000", 3)
    assert not ok
    assert "insufficient cash" in reason


def test_option_sell_put_with_reservation(conftest):
    """Reservations reduce available cash for put assignment check."""
    conn, strategy_id = conftest

    # Need a valid order_id for the FK on reservations
    cur = conn.execute(
        "INSERT INTO orders (strategy_id, symbol, side, qty, order_type, status, client_order_id,"
        " reserved_amount, filled_qty, actor, created_at, updated_at)"
        " VALUES (?, 'DUMMY', 'buy', 1, 'market', 'pending', 'dummy-res-1', 0, 0, 'test', ?, ?)",
        (strategy_id, "2025-01-01T00:00:00", "2025-01-01T00:00:00"),
    )
    dummy_order_id = cur.lastrowid
    conn.commit()

    # Reserve $8000, leaving $2000
    conn.execute(
        "INSERT INTO reservations (strategy_id, order_id, amount, created_at) VALUES (?, ?, ?, '2025-01-01T00:00:00')",
        (strategy_id, dummy_order_id, 8000.0),
    )
    conn.commit()

    # $2000 available < $5000 obligation for 1 contract
    ok, reason = check_option_sell_guard(conn, strategy_id, "NKE260718P00040000", 1)
    assert not ok
    assert "insufficient cash" in reason


def test_option_sell_call_covered(conftest):
    """A covered call requires holding the underlying shares."""
    conn, strategy_id = conftest

    # No position yet
    ok, reason = check_option_sell_guard(conn, strategy_id, "AAPL260821C00225000", 1)
    assert not ok
    assert "no position held" in reason

    # Add 100 shares of AAPL → covered for 1 contract
    _setup_equity_position(conn, strategy_id, "AAPL", 100)
    ok, reason = check_option_sell_guard(conn, strategy_id, "AAPL260821C00225000", 1)
    assert ok, f"Should pass with 100 shares: {reason}"

    # 100 shares < 200 needed for 2 contracts
    ok, reason = check_option_sell_guard(conn, strategy_id, "AAPL260821C00225000", 2)
    assert not ok
    assert "insufficient shares" in reason


def test_option_sell_call_reserved_shares(conftest):
    """Reserved shares don't count as available for covered calls."""
    conn, strategy_id = conftest

    _setup_equity_position(conn, strategy_id, "AAPL", 100)
    conn.execute(
        "UPDATE positions SET reserved_qty = 100 WHERE strategy_id = ? AND symbol = ?",
        (strategy_id, "AAPL"),
    )
    conn.commit()

    # 0 available shares < 100 needed
    ok, reason = check_option_sell_guard(conn, strategy_id, "AAPL260821C00225000", 1)
    assert not ok
    assert "insufficient shares" in reason


def test_option_buy_guard_cash(conftest):
    """Buying an option requires cash for the premium."""
    conn, strategy_id = conftest

    # Premium = $2.50 * 100 * 1 = $250, cash = $10000
    ok, reason = check_option_buy_guard(conn, strategy_id, "NKE260718P00040000", 250.0)
    assert ok, f"Should pass with sufficient cash: {reason}"

    # Premium = $12000 > $10000 cash
    ok, reason = check_option_buy_guard(conn, strategy_id, "NKE260718P00040000", 12000.0)
    assert not ok
    assert "insufficient buying power" in reason


def test_option_buy_guard_blocks_duplicate_long(conftest):
    """Buy-to-open is blocked if a long position already exists for the same OCC symbol."""
    conn, strategy_id = conftest

    # Create an existing long position
    conn.execute(
        "INSERT INTO option_positions (strategy_id, symbol, underlying, option_right, side, qty,"
        " avg_entry_price, strike, expiry, opened_at, updated_at)"
        " VALUES (?, 'NKE260718P00040000', 'NKE', 'put', 'long', 1, 2.50, 40.0, '2026-07-18',"
        " '2025-01-01T00:00:00', '2025-01-01T00:00:00')",
        (strategy_id,),
    )
    conn.commit()

    ok, reason = check_option_buy_guard(conn, strategy_id, "NKE260718P00040000", 250.0)
    assert not ok
    assert "already holding long position" in reason


def test_option_buy_guard_allows_close_short(conftest):
    """Buy-to-close is allowed even when no long exists (closes short instead)."""
    conn, strategy_id = conftest

    # Create a short position
    conn.execute(
        "INSERT INTO option_positions (strategy_id, symbol, underlying, option_right, side, qty,"
        " avg_entry_price, strike, expiry, opened_at, updated_at)"
        " VALUES (?, 'NKE260718P00040000', 'NKE', 'put', 'short', 1, 2.50, 40.0, '2026-07-18',"
        " '2025-01-01T00:00:00', '2025-01-01T00:00:00')",
        (strategy_id,),
    )
    conn.commit()

    ok, reason = check_option_buy_guard(conn, strategy_id, "NKE260718P00040000", 250.0)
    assert ok, f"Should pass (closing short): {reason}"