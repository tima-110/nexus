"""Tests for the dashboard command and module."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from nexus.cli import app
from nexus.config import AuditLogConfig, NexusConfig
from nexus.dashboard import _collect_data, _render_html, generate_dashboard
from nexus.db import init_db

runner = CliRunner()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _setup_db() -> sqlite3.Connection:
    """In-memory DB with one broker, one strategy, positions, orders, txns."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    conn.execute(
        "INSERT INTO broker_accounts (profile_name, margin_multiplier, cash_balance)"
        " VALUES ('paper1', 2.0, 100000.0)"
    )
    conn.execute(
        "INSERT INTO strategies (name, broker_account_id, cash_balance, is_active, created_at)"
        " VALUES ('wheel', 1, 10000.0, 1, ?)",
        (_now(),),
    )
    conn.execute(
        "INSERT INTO positions (strategy_id, symbol, qty, reserved_qty, avg_entry_price, opened_at, updated_at)"
        " VALUES (1, 'AAPL', 100, 0, 150.0, ?, ?)",
        (_now(), _now()),
    )
    conn.execute(
        "INSERT INTO option_positions (strategy_id, symbol, underlying, option_right, side, qty,"
        " avg_entry_price, strike, expiry, opened_at, updated_at)"
        " VALUES (1, 'NKE260718P00040000', 'NKE', 'put', 'short', 1, 2.50, 40.0, '2026-07-18', ?, ?)",
        (_now(), _now()),
    )
    old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    conn.execute(
        "INSERT INTO orders (strategy_id, symbol, side, qty, order_type, asset_class, status,"
        " client_order_id, broker_order_id, reserved_amount, filled_qty, actor, created_at, updated_at)"
        " VALUES (1, 'TSLA', 'buy', 10, 'limit', 'equity', 'submitted',"
        " 'nx-wheel-TSLA-aaaabbbb', 'broker-1', 1500.0, 0, 'cli:manual', ?, ?)",
        (old, old),
    )
    conn.execute(
        "INSERT INTO reservations (strategy_id, order_id, amount, created_at) VALUES (1, 1, 1500.0, ?)",
        (_now(),),
    )
    conn.execute(
        "INSERT INTO transactions (strategy_id, order_id, type, amount, actor, note, created_at)"
        " VALUES (1, NULL, 'deposit', 10000.0, 'cli:manual', NULL, ?)",
        (_now(),),
    )
    conn.commit()
    return conn


def _test_config(tmp_path: Path) -> NexusConfig:
    return NexusConfig(audit_log=AuditLogConfig(path=str(tmp_path / "audit.jsonl")))


class TestCollectData:
    def test_empty_db(self, tmp_path):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        data = _collect_data(conn, _test_config(tmp_path), strategy=None, days=7, live=False)
        assert data["totals"]["strategy_count"] == 0
        assert data["totals"]["open_orders"] == 0
        assert data["strategies"] == []
        assert data["live_enabled"] is False

    def test_with_data(self, tmp_path):
        conn = _setup_db()
        data = _collect_data(conn, _test_config(tmp_path), strategy=None, days=7, live=False)
        assert data["totals"]["strategy_count"] == 1
        assert data["totals"]["cash"] == pytest.approx(10000.0)
        assert data["totals"]["reserved"] == pytest.approx(1500.0)
        assert data["totals"]["open_orders"] == 1
        assert len(data["positions"]["equities"]) == 1
        assert len(data["positions"]["options"]) == 1
        assert data["positions"]["options"][0]["obligation"] == pytest.approx(4000.0)
        # stale order (>24h submitted) surfaces in attention
        assert any("30" in a["text"] or "stale" in a["text"].lower() or "TSLA" in a["text"] for a in data["attention"])
        assert data["orders"]["status_counts"].get("submitted") == 1

    def test_strategy_filter(self, tmp_path):
        conn = _setup_db()
        data = _collect_data(conn, _test_config(tmp_path), strategy="wheel", days=7, live=False)
        assert data["scope"] == "wheel"
        assert len(data["strategies"]) == 1

    def test_unknown_strategy_raises(self, tmp_path):
        conn = _setup_db()
        with pytest.raises(ValueError, match="not found"):
            _collect_data(conn, _test_config(tmp_path), strategy="nope", days=7, live=False)

    def test_live_degrades_gracefully(self, tmp_path):
        conn = _setup_db()
        with patch("nexus.broker.AlpacaBroker", side_effect=RuntimeError("no broker")):
            data = _collect_data(conn, _test_config(tmp_path), strategy=None, days=7, live=True)
        assert data["live_enabled"] is True
        assert data["live_degraded"] is True
        assert data["brokers"][0]["reachable"] is False

    def test_live_enrichment(self, tmp_path):
        from decimal import Decimal

        conn = _setup_db()
        broker = MagicMock()
        broker.get_account.return_value = MagicMock(cash=Decimal("1002.50"), buying_power=Decimal("2000"), equity=Decimal("5000"))
        broker.get_positions.return_value = []
        broker.list_option_positions.return_value = []
        broker.list_orders.return_value = []
        with patch("nexus.broker.AlpacaBroker", return_value=broker):
            data = _collect_data(conn, _test_config(tmp_path), strategy=None, days=7, live=True)
        assert data["brokers"][0]["reachable"] is True
        assert data["brokers"][0]["live_cash"] == pytest.approx(1002.50)
        assert data["brokers"][0]["drift"] == pytest.approx(1002.50 - 100000.0)


class TestRenderHtml:
    def test_empty_renders_no_data(self, tmp_path):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        data = _collect_data(conn, _test_config(tmp_path), strategy=None, days=7, live=False)
        out = _render_html(data)
        assert "Nexus Dashboard" in out
        assert "#1a1b26" in out
        assert "No strategies found." in out
        assert "No open equity positions." in out

    def test_escapes_user_content(self, tmp_path):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        conn.execute(
            "INSERT INTO broker_accounts (profile_name, margin_multiplier, cash_balance)"
            " VALUES ('paper1', 2.0, 0.0)"
        )
        conn.execute(
            "INSERT INTO strategies (name, broker_account_id, cash_balance, is_active, created_at)"
            " VALUES ('<script>alert(1)</script>', 1, 0.0, 1, ?)",
            (_now(),),
        )
        conn.commit()
        data = _collect_data(conn, _test_config(tmp_path), strategy=None, days=7, live=False)
        out = _render_html(data)
        assert "<script>alert(1)</script>" not in out
        assert "&lt;script&gt;" in out

    def test_tabs_present(self, tmp_path):
        conn = _setup_db()
        data = _collect_data(conn, _test_config(tmp_path), strategy=None, days=7, live=False)
        out = _render_html(data)
        for tab in ("tab-overview", "tab-accounts", "tab-positions", "tab-orders", "tab-health"):
            assert tab in out


class TestGenerateDashboard:
    def test_writes_file(self, tmp_path):
        conn = _setup_db()
        out_path = tmp_path / "dash.html"
        with patch("nexus.db.get_connection", return_value=conn), patch("nexus.db.init_db"):
            out = generate_dashboard(
                _test_config(tmp_path), days=7, live=False, output=out_path
            )
        assert out == out_path
        assert out_path.exists()
        assert "Nexus Dashboard" in out_path.read_text(encoding="utf-8")


class TestDashboardCli:
    def test_dashboard_smoke(self, tmp_path):
        conn = _setup_db()
        cfg = _test_config(tmp_path)
        out_path = tmp_path / "dashboard.html"
        with patch("nexus.config.load_config", return_value=cfg), \
             patch("nexus.db.get_connection", return_value=conn), \
             patch("nexus.db.init_db"), \
             patch("webbrowser.open") as mock_open:
            result = runner.invoke(app, [
                "dashboard", "--no-open", "--no-live", "--output", str(out_path),
            ])
        assert result.exit_code == 0, result.output
        assert "Dashboard:" in result.output
        assert out_path.exists()
        mock_open.assert_not_called()

    def test_dashboard_opens_browser_by_default(self, tmp_path):
        conn = _setup_db()
        cfg = _test_config(tmp_path)
        out_path = tmp_path / "dashboard.html"
        with patch("nexus.config.load_config", return_value=cfg), \
             patch("nexus.db.get_connection", return_value=conn), \
             patch("nexus.db.init_db"), \
             patch("webbrowser.open") as mock_open:
            result = runner.invoke(app, [
                "dashboard", "--no-live", "--output", str(out_path),
            ])
        assert result.exit_code == 0, result.output
        mock_open.assert_called_once()
        assert mock_open.call_args[0][0].startswith("file://")

    def test_dashboard_json(self, tmp_path):
        conn = _setup_db()
        cfg = _test_config(tmp_path)
        out_path = tmp_path / "dashboard.html"
        with patch("nexus.config.load_config", return_value=cfg), \
             patch("nexus.db.get_connection", return_value=conn), \
             patch("nexus.db.init_db"), \
             patch("webbrowser.open"):
            result = runner.invoke(app, [
                "--json", "dashboard", "--no-open", "--no-live", "--output", str(out_path),
            ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["path"] == str(out_path)

    def test_dashboard_unknown_strategy_fails(self, tmp_path):
        conn = _setup_db()
        cfg = _test_config(tmp_path)
        with patch("nexus.config.load_config", return_value=cfg), \
             patch("nexus.db.get_connection", return_value=conn), \
             patch("nexus.db.init_db"), \
             patch("webbrowser.open"):
            result = runner.invoke(app, ["dashboard", "--no-open", "--no-live", "--strategy", "nope"])
        assert result.exit_code != 0

    def test_dashboard_bad_days_fails(self, tmp_path):
        conn = _setup_db()
        cfg = _test_config(tmp_path)
        with patch("nexus.config.load_config", return_value=cfg), \
             patch("nexus.db.get_connection", return_value=conn), \
             patch("nexus.db.init_db"), \
             patch("webbrowser.open"):
            result = runner.invoke(app, ["dashboard", "--no-open", "--days", "0"])
        assert result.exit_code != 0

    def test_dashboard_help(self):
        result = runner.invoke(app, ["dashboard", "--help"])
        assert result.exit_code == 0
        assert "--no-open" in result.output
