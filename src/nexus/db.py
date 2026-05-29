"""SQLite database layer for Nexus."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def get_connection(path: Path | None = None) -> sqlite3.Connection:
    """Return a sqlite3 connection with WAL mode and foreign key enforcement.

    If *path* is None, the database path from the loaded configuration is used.
    """
    if path is None:
        from nexus.config import get_db_path
        path = get_db_path()

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables if they do not already exist."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS broker_accounts (
            id                  INTEGER PRIMARY KEY,
            profile_name        TEXT    UNIQUE NOT NULL,
            margin_multiplier   REAL    DEFAULT 2.0,
            cash_balance        REAL    DEFAULT 0.0,
            last_synced_at      TEXT
        );

        CREATE TABLE IF NOT EXISTS strategies (
            id                  INTEGER PRIMARY KEY,
            name                TEXT    UNIQUE NOT NULL,
            broker_account_id   INTEGER NOT NULL REFERENCES broker_accounts(id),
            cash_balance        REAL    DEFAULT 0.0,
            is_active           INTEGER DEFAULT 1,
            created_at          TEXT
        );

        CREATE TABLE IF NOT EXISTS orders (
            id                  INTEGER PRIMARY KEY,
            strategy_id         INTEGER NOT NULL REFERENCES strategies(id),
            symbol              TEXT    NOT NULL,
            side                TEXT    NOT NULL,
            qty                 INTEGER NOT NULL,
            order_type          TEXT    NOT NULL,
            limit_price         REAL,
            stop_price          REAL,
            trail_percent       REAL,
            time_in_force       TEXT,
            status              TEXT    NOT NULL,
            client_order_id     TEXT    UNIQUE,
            broker_order_id     TEXT,
            reserved_amount     REAL,
            filled_qty          INTEGER DEFAULT 0,
            filled_avg_price    REAL,
            filled_at           TEXT,
            actor               TEXT,
            created_at          TEXT,
            updated_at          TEXT
        );

        CREATE TABLE IF NOT EXISTS positions (
            id                  INTEGER PRIMARY KEY,
            strategy_id         INTEGER NOT NULL REFERENCES strategies(id),
            symbol              TEXT    NOT NULL,
            qty                 INTEGER NOT NULL,
            reserved_qty        INTEGER DEFAULT 0,
            avg_entry_price     REAL,
            opened_at           TEXT,
            updated_at          TEXT,
            UNIQUE (strategy_id, symbol)
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id                  INTEGER PRIMARY KEY,
            strategy_id         INTEGER NOT NULL REFERENCES strategies(id),
            order_id            INTEGER REFERENCES orders(id),
            type                TEXT    NOT NULL,
            amount              REAL    NOT NULL,
            actor               TEXT,
            note                TEXT,
            created_at          TEXT
        );

        CREATE TABLE IF NOT EXISTS reservations (
            id                  INTEGER PRIMARY KEY,
            strategy_id         INTEGER NOT NULL REFERENCES strategies(id),
            order_id            INTEGER NOT NULL REFERENCES orders(id),
            amount              REAL    NOT NULL,
            created_at          TEXT
        );
        """
    )
    conn.commit()

    # Migration: add time_in_force to existing orders tables
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN time_in_force TEXT")
        conn.commit()
    except Exception:
        pass  # column already exists
