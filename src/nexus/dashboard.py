"""Self-contained HTML status dashboard for Nexus."""
from __future__ import annotations

import html
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nexus.config import NexusConfig, get_audit_path, get_db_path

_CONTRACT_MULTIPLIER = 100
_STALE_HOURS = 24
_AUDIT_TAIL_LINES = 300
_DEFAULT_OUT_FILE = "dashboard.html"


def resolve_dashboard_path(cfg: NexusConfig, explicit: Path | None = None) -> Path:
    """Resolve the dashboard output path.

    Precedence: explicit CLI flag wins, then config-file settings
    ([dashboard] out_dir/out_file), then the historical default
    (dashboard.html alongside the database). Empty config values
    fall back to the default.
    """
    if explicit is not None:
        return explicit
    out_dir = cfg.dashboard.out_dir.strip()
    out_file = cfg.dashboard.out_file.strip() or _DEFAULT_OUT_FILE
    base = Path(out_dir).expanduser() if out_dir else get_db_path(cfg).parent
    return base / out_file


def generate_dashboard(
    cfg: NexusConfig,
    *,
    strategy: str | None = None,
    days: int = 7,
    live: bool = True,
    output: Path | None = None,
) -> Path:
    """Gather data, render HTML, write to disk. Returns output path."""
    from nexus.db import get_connection, init_db

    conn = get_connection()
    init_db(conn)
    try:
        data = _collect_data(conn, cfg, strategy=strategy, days=days, live=live)
    finally:
        conn.close()

    rendered = _render_html(data)

    out = output if output is not None else resolve_dashboard_path(cfg)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered, encoding="utf-8")
    return out


def _collect_data(
    conn: sqlite3.Connection,
    cfg: NexusConfig,
    *,
    strategy: str | None,
    days: int,
    live: bool,
) -> dict:
    """Assemble all dashboard data into a plain dict."""
    from nexus.doctor import run_doctor
    from nexus.schedule.cron import get_schedule_status

    try:
        from nexus import __version__ as _v
    except Exception:
        _v = "unknown"

    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).isoformat()
    stale_cutoff = (now - timedelta(hours=_STALE_HOURS)).isoformat()

    if strategy is not None:
        exists = conn.execute(
            "SELECT id FROM strategies WHERE name = ?", (strategy,)
        ).fetchone()
        if exists is None:
            raise ValueError(f"strategy '{strategy}' not found")

    live_maps: dict = {"enabled": live, "degraded": False, "profiles": {}}
    if live:
        live_maps = _collect_live(conn, strategy_filter=strategy)

    brokers = _collect_brokers(conn, live_maps, strategy_filter=strategy)
    strategies = _collect_strategies(conn, live_maps, strategy_filter=strategy)
    positions = _collect_positions(conn, live_maps, strategy_filter=strategy)
    orders = _collect_orders(conn, stale_cutoff, cutoff, strategy_filter=strategy)
    transactions = _collect_transactions(conn, cutoff, strategy_filter=strategy)
    reservations = _collect_reservations(conn, strategy_filter=strategy)
    audit = _collect_audit(cfg)
    try:
        checks = run_doctor(conn, cfg)
        doctor_checks = [
            {"name": c.name, "passed": c.passed, "detail": c.detail} for c in checks
        ]
        doctor_ok = all(c.passed for c in checks)
    except Exception as exc:
        doctor_checks = [{"name": "doctor", "passed": False, "detail": str(exc)}]
        doctor_ok = False
    try:
        schedule = get_schedule_status()
    except Exception as exc:
        schedule = {"installed": False, "schedule": None, "command": None, "error": str(exc)}

    attention = _build_attention(orders, reservations, live_maps, doctor_checks)

    totals = _build_totals(strategies, positions, orders, transactions)

    return {
        "version": _v,
        "generated_at": now.isoformat(),
        "scope": strategy or "all strategies",
        "days": days,
        "live_enabled": live,
        "live_degraded": live_maps.get("degraded", False),
        "brokers": brokers,
        "strategies": strategies,
        "positions": positions,
        "orders": orders,
        "transactions": transactions,
        "reservations": reservations,
        "audit": audit,
        "doctor_checks": doctor_checks,
        "doctor_ok": doctor_ok,
        "schedule": schedule,
        "attention": attention,
        "totals": totals,
        "config": {
            "db_path": str(get_db_path(cfg)),
            "audit_path": str(get_audit_path(cfg)),
            "reconcile_minutes": cfg.reconciler.interval_minutes,
            "market_hours_only": cfg.reconciler.market_hours_only,
            "slippage_buffer_percent": cfg.order.slippage_buffer_percent,
        },
    }


def _collect_live(conn: sqlite3.Connection, *, strategy_filter: str | None) -> dict:
    """Query Alpaca per broker profile. Failures degrade per profile, never abort."""
    from nexus.broker import AlpacaBroker

    if strategy_filter is not None:
        rows = conn.execute(
            "SELECT DISTINCT b.profile_name FROM broker_accounts b"
            " JOIN strategies s ON s.broker_account_id = b.id"
            " WHERE s.name = ?",
            (strategy_filter,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT profile_name FROM broker_accounts").fetchall()

    profiles: dict = {}
    degraded = False
    for row in rows:
        profile = row["profile_name"]
        info: dict = {
            "reachable": False,
            "error": None,
            "account": None,
            "prices": {},
            "option_premiums": {},
            "open_orders": [],
            "bypass_orders": [],
        }
        try:
            broker = AlpacaBroker(profile)
            account = broker.get_account()
            info["account"] = {
                "cash": float(account.cash),
                "buying_power": float(account.buying_power),
                "equity": float(account.equity),
            }
            for p in broker.get_positions():
                info["prices"][p.symbol] = {
                    "current": float(p.current_price),
                    "avg_entry": float(p.avg_entry_price),
                    "unrealized": float(p.unrealized_pl),
                    "qty": int(p.qty),
                }
            try:
                for op in broker.list_option_positions():
                    info["option_premiums"][op.symbol] = {
                        "current": float(op.current_price),
                        "unrealized": float(op.unrealized_pl),
                    }
            except RuntimeError:
                pass
            for o in broker.list_orders("open"):
                entry = {
                    "broker_order_id": o.broker_order_id,
                    "client_order_id": o.client_order_id,
                    "symbol": o.symbol,
                    "side": o.side,
                    "qty": o.qty,
                }
                info["open_orders"].append(entry)
                if not (o.client_order_id or "").startswith("nx-"):
                    info["bypass_orders"].append(entry)
            info["reachable"] = True
        except RuntimeError as exc:
            info["error"] = str(exc)
            degraded = True
        profiles[profile] = info

    ghost_ids: set[str] = set()
    for info in profiles.values():
        for o in info["open_orders"]:
            if o["broker_order_id"]:
                ghost_ids.add(o["broker_order_id"])
    ghosts: list[dict] = []
    if ghost_ids:
        placeholders = ",".join("?" * len(ghost_ids))
        rows = conn.execute(
            f"SELECT o.id, o.broker_order_id, o.symbol, o.status, s.name AS strategy"
            f" FROM orders o JOIN strategies s ON o.strategy_id = s.id"
            f" WHERE o.status IN ('cancelled', 'cancel_pending', 'cancel_failed')"
            f" AND o.broker_order_id IN ({placeholders})",
            tuple(ghost_ids),
        ).fetchall()
        for r in rows:
            if strategy_filter is not None and r["strategy"] != strategy_filter:
                continue
            ghosts.append(
                {
                    "order_id": r["id"],
                    "broker_order_id": r["broker_order_id"],
                    "symbol": r["symbol"],
                    "strategy": r["strategy"],
                    "local_status": r["status"],
                }
            )

    return {"enabled": True, "degraded": degraded, "profiles": profiles, "ghosts": ghosts}


def _collect_brokers(
    conn: sqlite3.Connection, live_maps: dict, *, strategy_filter: str | None
) -> list[dict]:
    """Cached broker rows enriched with live account data when available."""
    query = "SELECT id, profile_name, margin_multiplier, cash_balance, last_synced_at FROM broker_accounts ORDER BY profile_name"
    rows = conn.execute(query).fetchall()
    profiles = live_maps.get("profiles", {})
    if strategy_filter is not None:
        attached = {
            r["profile_name"]
            for r in conn.execute(
                "SELECT b.profile_name FROM broker_accounts b"
                " JOIN strategies s ON s.broker_account_id = b.id"
                " WHERE s.name = ?",
                (strategy_filter,),
            ).fetchall()
        }
        rows = [r for r in rows if r["profile_name"] in attached]

    brokers = []
    for r in rows:
        live = profiles.get(r["profile_name"], {})
        account = live.get("account")
        cached = float(r["cash_balance"] or 0.0)
        drift = (account["cash"] - cached) if account else None
        brokers.append(
            {
                "profile": r["profile_name"],
                "margin_multiplier": r["margin_multiplier"],
                "cached_cash": cached,
                "last_synced_at": r["last_synced_at"],
                "reachable": live.get("reachable", False),
                "live_error": live.get("error"),
                "live_cash": account["cash"] if account else None,
                "live_buying_power": account["buying_power"] if account else None,
                "live_equity": account["equity"] if account else None,
                "drift": drift,
                "open_orders": len(live.get("open_orders", [])),
                "bypass_orders": live.get("bypass_orders", []),
            }
        )
    return brokers


def _collect_strategies(
    conn: sqlite3.Connection, live_maps: dict, *, strategy_filter: str | None
) -> list[dict]:
    """Per-strategy cash, reservations, positions value, and equity."""
    prices: dict[str, float] = {}
    for info in live_maps.get("profiles", {}).values():
        for symbol, p in info.get("prices", {}).items():
            prices.setdefault(symbol, p["current"])

    query = (
        "SELECT s.id, s.name, s.cash_balance, s.is_active,"
        " b.profile_name, b.margin_multiplier"
        " FROM strategies s JOIN broker_accounts b ON s.broker_account_id = b.id"
    )
    params: list = []
    if strategy_filter is not None:
        query += " WHERE s.name = ?"
        params.append(strategy_filter)
    query += " ORDER BY s.name"
    rows = conn.execute(query, params).fetchall()

    strategies = []
    for r in rows:
        sid = r["id"]
        reserved = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM reservations WHERE strategy_id = ?",
            (sid,),
        ).fetchone()[0]
        open_orders = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE strategy_id = ? AND status IN ('submitted', 'partially_filled')",
            (sid,),
        ).fetchone()[0]
        eq_rows = conn.execute(
            "SELECT symbol, qty, avg_entry_price FROM positions WHERE strategy_id = ? AND qty > 0",
            (sid,),
        ).fetchall()
        opt_count = conn.execute(
            "SELECT COUNT(*) FROM option_positions WHERE strategy_id = ? AND qty > 0",
            (sid,),
        ).fetchone()[0]
        cash = float(r["cash_balance"] or 0.0)
        available = cash - float(reserved or 0.0)
        mv = 0.0
        live_any = False
        for p in eq_rows:
            px = prices.get(p["symbol"])
            if px is None:
                px = float(p["avg_entry_price"] or 0.0)
            else:
                live_any = True
            mv += int(p["qty"] or 0) * px
        strategies.append(
            {
                "name": r["name"],
                "broker": r["profile_name"],
                "is_active": bool(r["is_active"]),
                "cash": cash,
                "reserved": float(reserved or 0.0),
                "available": available,
                "buying_power": available * float(r["margin_multiplier"] or 1.0),
                "positions_value": mv,
                "total_equity": cash + mv,
                "prices_live": live_any,
                "open_orders": int(open_orders or 0),
                "equity_positions": len(eq_rows),
                "option_positions": int(opt_count or 0),
            }
        )
    return strategies


def _collect_positions(
    conn: sqlite3.Connection, live_maps: dict, *, strategy_filter: str | None
) -> dict:
    """Equity and option positions with live prices when available."""
    prices: dict[str, dict] = {}
    premiums: dict[str, dict] = {}
    for info in live_maps.get("profiles", {}).values():
        for symbol, p in info.get("prices", {}).items():
            prices.setdefault(symbol, p)
        for symbol, p in info.get("option_premiums", {}).items():
            premiums.setdefault(symbol, p)

    eq_query = (
        "SELECT p.symbol, p.qty, p.reserved_qty, p.avg_entry_price, s.name AS strategy"
        " FROM positions p JOIN strategies s ON p.strategy_id = s.id"
        " WHERE p.qty > 0"
    )
    opt_query = (
        "SELECT op.symbol, op.underlying, op.option_right, op.side, op.qty,"
        " op.avg_entry_price, op.strike, op.expiry, s.name AS strategy"
        " FROM option_positions op JOIN strategies s ON op.strategy_id = s.id"
        " WHERE op.qty > 0"
    )
    params: list = []
    if strategy_filter is not None:
        eq_query += " AND s.name = ?"
        opt_query += " AND s.name = ?"
        params.append(strategy_filter)
    eq_query += " ORDER BY s.name, p.symbol"
    opt_query += " ORDER BY s.name, op.underlying, op.expiry"

    equities = []
    for r in conn.execute(eq_query, params).fetchall():
        qty = int(r["qty"] or 0)
        reserved = int(r["reserved_qty"] or 0)
        avg = float(r["avg_entry_price"] or 0.0)
        live = prices.get(r["symbol"])
        px = live["current"] if live else avg
        mv = qty * px
        equities.append(
            {
                "strategy": r["strategy"],
                "symbol": r["symbol"],
                "qty": qty,
                "reserved": reserved,
                "available": qty - reserved,
                "avg_entry": r["avg_entry_price"],
                "live_price": live["current"] if live else None,
                "market_value": mv,
                "unrealized": (mv - qty * avg) if live else None,
            }
        )

    options = []
    today = datetime.now(timezone.utc).date()
    for r in conn.execute(opt_query, params).fetchall():
        qty = int(r["qty"] or 0)
        avg = float(r["avg_entry_price"] or 0.0)
        live = premiums.get(r["symbol"])
        live_px = live["current"] if live else None
        if live_px is not None:
            unreal = (
                (avg - live_px) * qty * _CONTRACT_MULTIPLIER
                if r["side"] == "short"
                else (live_px - avg) * qty * _CONTRACT_MULTIPLIER
            )
        else:
            unreal = None
        try:
            dte = (datetime.strptime(r["expiry"], "%Y-%m-%d").date() - today).days
        except (ValueError, TypeError):
            dte = None
        premium_value = avg * qty * _CONTRACT_MULTIPLIER
        obligation = (
            float(r["strike"] or 0.0) * _CONTRACT_MULTIPLIER * qty
            if r["side"] == "short" and r["option_right"] == "put"
            else None
        )
        options.append(
            {
                "strategy": r["strategy"],
                "symbol": r["symbol"],
                "underlying": r["underlying"],
                "right": r["option_right"],
                "side": r["side"],
                "qty": qty,
                "strike": r["strike"],
                "expiry": r["expiry"],
                "dte": dte,
                "avg_premium": r["avg_entry_price"],
                "premium_value": premium_value,
                "live_premium": live_px,
                "unrealized": unreal,
                "obligation": obligation,
            }
        )

    return {"equities": equities, "options": options}


def _collect_orders(
    conn: sqlite3.Connection,
    stale_cutoff: str,
    window_cutoff: str,
    *,
    strategy_filter: str | None,
) -> dict:
    """Open, attention, recent, and in-window fills."""
    base = (
        "SELECT o.id, o.symbol, o.side, o.qty, o.order_type, o.limit_price,"
        " o.stop_price, o.time_in_force, o.asset_class, o.status, o.client_order_id,"
        " o.broker_order_id, o.filled_qty, o.filled_avg_price, o.filled_at,"
        " o.created_at, o.updated_at, s.name AS strategy"
        " FROM orders o JOIN strategies s ON o.strategy_id = s.id"
    )
    filt = " WHERE s.name = ?" if strategy_filter is not None else ""
    params = [strategy_filter] if strategy_filter is not None else []

    def _rows(where: str, order: str, extra: list | None = None) -> list[dict]:
        q = base + filt
        q += f" AND {where}" if filt else f" WHERE {where}"
        q += f" {order}"
        return [dict(r) for r in conn.execute(q, params + (extra or [])).fetchall()]

    open_orders = _rows("o.status IN ('submitted', 'partially_filled')", "ORDER BY o.id DESC")
    stale = [o for o in open_orders if (o.get("created_at") or "") < stale_cutoff]
    attention = _rows(
        "o.status IN ('cancel_pending', 'cancel_failed')", "ORDER BY o.id DESC"
    )
    recent = _rows("1 = 1", "ORDER BY o.id DESC LIMIT 20")
    fills = _rows(
        "o.status = 'filled' AND (o.filled_at >= ? OR (o.filled_at IS NULL AND o.updated_at >= ?))",
        "ORDER BY o.id DESC",
        [window_cutoff, window_cutoff],
    )
    status_counts: dict[str, int] = {}
    count_query = (
        "SELECT o.status, COUNT(*) AS cnt FROM orders o"
        " JOIN strategies s ON o.strategy_id = s.id"
        + filt
        + " GROUP BY o.status"
    )
    for r in conn.execute(count_query, params).fetchall():
        status_counts[r["status"]] = int(r["cnt"])

    return {
        "open": open_orders,
        "stale": stale,
        "attention": attention,
        "recent": recent,
        "fills": fills,
        "status_counts": status_counts,
    }


def _collect_transactions(
    conn: sqlite3.Connection, cutoff: str, *, strategy_filter: str | None
) -> dict:
    """Transaction sums and recent rows inside the lookback window."""
    filt = " AND s.name = ?" if strategy_filter is not None else ""
    extra = [strategy_filter] if strategy_filter is not None else []
    sums = conn.execute(
        "SELECT t.type, COALESCE(SUM(t.amount), 0) AS total, COUNT(*) AS cnt"
        " FROM transactions t JOIN strategies s ON t.strategy_id = s.id"
        " WHERE t.created_at >= ?" + filt + " GROUP BY t.type ORDER BY t.type",
        [cutoff] + extra,
    ).fetchall()
    recent = conn.execute(
        "SELECT t.created_at, t.type, t.amount, s.name AS strategy, o.symbol, t.actor, t.note"
        " FROM transactions t JOIN strategies s ON t.strategy_id = s.id"
        " LEFT JOIN orders o ON t.order_id = o.id"
        " WHERE t.created_at >= ?" + filt + " ORDER BY t.created_at DESC LIMIT 20",
        [cutoff] + extra,
    ).fetchall()
    return {
        "sums": [
            {"type": r["type"], "total": float(r["total"] or 0.0), "count": int(r["cnt"])}
            for r in sums
        ],
        "recent": [dict(r) for r in recent],
    }


def _collect_reservations(conn: sqlite3.Connection, *, strategy_filter: str | None) -> dict:
    """Active reservations plus orphan detection (mirrors the reconciler predicate)."""
    filt = " AND s.name = ?" if strategy_filter is not None else ""
    params = [strategy_filter] if strategy_filter is not None else []
    active = conn.execute(
        "SELECT r.order_id, r.amount, r.created_at, s.name AS strategy, o.symbol, o.status"
        " FROM reservations r JOIN strategies s ON r.strategy_id = s.id"
        " JOIN orders o ON r.order_id = o.id WHERE 1 = 1" + filt
        + " ORDER BY r.id DESC",
        params,
    ).fetchall()
    orphans = conn.execute(
        "SELECT r.order_id, s2.name AS strategy, o.symbol"
        " FROM reservations r JOIN orders o ON r.order_id = o.id"
        " JOIN strategies s2 ON r.strategy_id = s2.id"
        " WHERE o.status IN ('filled', 'cancelled', 'cancel_failed', 'expired')"
        " AND r.order_id NOT IN ("
        "   SELECT origin_order_id FROM option_positions"
        "   WHERE origin_order_id IS NOT NULL AND qty > 0)"
        + (" AND s2.name = ?" if strategy_filter is not None else ""),
        params,
    ).fetchall()
    total = sum(float(r["amount"] or 0.0) for r in active)
    return {
        "active": [dict(r) for r in active],
        "orphans": [dict(r) for r in orphans],
        "total": total,
    }


def _collect_audit(cfg: NexusConfig) -> dict:
    """Tail the audit log: event counts plus recent error-ish events."""
    path = get_audit_path(cfg)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {"present": False, "events": [], "counts": {}, "errors": [], "total": 0}
    except OSError as exc:
        return {"present": False, "events": [], "counts": {}, "errors": [], "total": 0, "error": str(exc)}

    tail = lines[-_AUDIT_TAIL_LINES:]
    events = []
    for line in tail:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    counts: dict[str, int] = {}
    for e in events:
        key = str(e.get("event", "unknown"))
        counts[key] = counts.get(key, 0) + 1
    error_kinds = {"cancel_failed", "ghost_order_detected", "cancel_pending"}
    errors = [e for e in events if str(e.get("event")) in error_kinds][-5:]
    return {
        "present": True,
        "total": len(lines),
        "counts": counts,
        "errors": errors,
        "recent": events[-10:][::-1],
    }


def _build_attention(
    orders: dict, reservations: dict, live_maps: dict, doctor_checks: list[dict]
) -> list[dict]:
    """Ordered list of items needing operator attention."""
    attention = []
    for o in orders["attention"]:
        attention.append(
            {
                "level": "bad" if o["status"] == "cancel_failed" else "warn",
                "text": f"Order {o['id']} ({o['strategy']} {o['symbol']}) is {o['status']}",
            }
        )
    for o in orders["stale"]:
        attention.append(
            {
                "level": "warn",
                "text": f"Order {o['id']} ({o['strategy']} {o['symbol']}) submitted >24h ago",
            }
        )
    for g in live_maps.get("ghosts", []):
        attention.append(
            {
                "level": "bad",
                "text": f"Ghost order {g['order_id']} ({g['strategy']} {g['symbol']}): local {g['local_status']} but open on broker",
            }
        )
    for b in _iter_bypass(live_maps):
        attention.append({"level": "warn", "text": f"Bypass order on broker: {b}"})
    for o in reservations["orphans"]:
        attention.append(
            {
                "level": "warn",
                "text": f"Orphaned reservation: order {o['order_id']} ({o['strategy']} {o['symbol']})",
            }
        )
    for c in doctor_checks:
        if not c["passed"]:
            attention.append({"level": "bad", "text": f"Doctor {c['name']}: {c['detail']}"})
    return attention


def _iter_bypass(live_maps: dict) -> list[str]:
    out = []
    for profile, info in live_maps.get("profiles", {}).items():
        for o in info.get("bypass_orders", []):
            out.append(f"{o.get('broker_order_id')} ({profile} {o.get('symbol')})")
    return out


def _build_totals(
    strategies: list[dict], positions: dict, orders: dict, transactions: dict
) -> dict:
    """Portfolio-wide summary cards."""
    cash = sum(s["cash"] for s in strategies)
    reserved = sum(s["reserved"] for s in strategies)
    pos_value = sum(p["market_value"] for p in positions["equities"])
    equity = cash + pos_value
    premium_in = sum(
        p["premium_value"]
        for p in positions["options"]
        if p["side"] == "short"
    )
    obligations = sum(p["obligation"] or 0.0 for p in positions["options"])
    return {
        "cash": cash,
        "reserved": reserved,
        "available": cash - reserved,
        "positions_value": pos_value,
        "total_equity": equity,
        "strategy_count": len(strategies),
        "equity_positions": len(positions["equities"]),
        "option_positions": len(positions["options"]),
        "open_orders": len(orders["open"]),
        "fills_in_window": len(orders["fills"]),
        "short_premium_held": premium_in,
        "put_obligations": obligations,
        "transactions_in_window": sum(s["count"] for s in transactions["sums"]),
    }


def _render_html(data: dict) -> str:
    """Produce complete HTML string from collected data."""
    e = html.escape
    totals = data["totals"]
    live_badge = (
        '<span class="badge muted">live off</span>'
        if not data["live_enabled"]
        else '<span class="badge warn">live degraded</span>'
        if data["live_degraded"]
        else '<span class="badge ok">live</span>'
    )
    health_badge = (
        '<span class="badge ok">healthy</span>'
        if data["doctor_ok"] and not data["attention"]
        else '<span class="badge warn">attention</span>'
        if data["doctor_ok"]
        else '<span class="badge bad">failing</span>'
    )

    attention_html = _attention_html(data["attention"])
    strategies_html = _strategies_html(data["strategies"])
    brokers_html = _brokers_html(data["brokers"], data["live_enabled"])
    equities_html, options_html = _positions_html(data["positions"])
    orders_html = _orders_html(data["orders"])
    txns_html = _transactions_html(data["transactions"])
    health_html = _health_html(data)
    system_html = _system_html(data)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nexus Dashboard &mdash; {e(data["scope"])}</title>
<style>
:root {{ color-scheme: dark; }}
* {{ box-sizing: border-box; }}
body {{ background: #1a1b26; color: #c0caf5; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; margin: 0; padding: 0; }}
.wrap {{ max-width: 1200px; margin: 0 auto; padding: 24px 16px 64px; }}
header {{ margin-bottom: 16px; }}
header h1 {{ margin: 0 0 4px; font-size: 1.6rem; }}
header .meta {{ color: #9aa5ce; font-size: 0.85rem; }}
.mono {{ font-family: 'SF Mono', 'Fira Code', monospace; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 12px; margin: 16px 0; }}
.card {{ background: #24283b; border-radius: 10px; padding: 12px 14px; }}
.card .label {{ font-size: 0.75rem; color: #9aa5ce; text-transform: uppercase; letter-spacing: 0.04em; }}
.card .value {{ font-size: 1.25rem; font-weight: 700; margin-top: 4px; }}
.card .sub {{ font-size: 0.8rem; color: #9aa5ce; margin-top: 2px; }}
.badge {{ display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 0.75rem; font-weight: 600; }}
.badge.ok {{ background: rgba(158,206,106,0.15); color: #9ece6a; }}
.badge.warn {{ background: rgba(224,175,104,0.15); color: #e0af68; }}
.badge.bad {{ background: rgba(247,118,142,0.15); color: #f7768e; }}
.badge.muted {{ background: rgba(154,165,206,0.15); color: #9aa5ce; }}
.dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }}
.dot.ok {{ background: #9ece6a; }} .dot.warn {{ background: #e0af68; }} .dot.bad {{ background: #f7768e; }}
.tabs {{ display: flex; gap: 4px; flex-wrap: wrap; margin: 20px 0 0; border-bottom: 1px solid #343a55; }}
.tabs button {{ background: none; border: none; color: #9aa5ce; padding: 10px 16px; font-size: 0.9rem; cursor: pointer; border-bottom: 2px solid transparent; }}
.tabs button.active {{ color: #c0caf5; border-bottom-color: #7aa2f7; font-weight: 600; }}
.tab {{ display: none; padding-top: 16px; }}
.tab.active {{ display: block; }}
section.card-block {{ background: #24283b; border-radius: 10px; padding: 16px; margin-bottom: 16px; }}
section.card-block h2 {{ margin: 0 0 8px; font-size: 1.05rem; }}
section.card-block h3 {{ margin: 16px 0 8px; font-size: 0.95rem; color: #9aa5ce; }}
table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid #343a55; }}
th {{ color: #9aa5ce; font-weight: 600; text-transform: uppercase; font-size: 0.7rem; letter-spacing: 0.04em; }}
td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.empty {{ color: #9aa5ce; font-style: italic; padding: 8px 0; }}
.alert {{ border-left: 3px solid #e0af68; background: rgba(224,175,104,0.08); padding: 6px 10px; margin: 4px 0; border-radius: 0 6px 6px 0; font-size: 0.85rem; }}
.alert.bad {{ border-color: #f7768e; background: rgba(247,118,142,0.08); }}
.filter {{ margin: 8px 0; }}
.filter input {{ background: #1a1b26; border: 1px solid #343a55; color: #c0caf5; border-radius: 6px; padding: 6px 10px; width: 240px; }}
footer {{ margin-top: 24px; color: #565f89; font-size: 0.75rem; }}
</style>
</head>
<body>
<div class="wrap">
<header>
<h1>Nexus Dashboard</h1>
<div class="meta">Scope: <span class="mono">{e(str(data["scope"]))}</span> &middot; Generated <span class="mono">{e(data["generated_at"])}</span> &middot; nexus {e(str(data["version"]))} &middot; window {int(data["days"])}d &middot; {live_badge} {health_badge}</div>
</header>

<div class="cards">
<div class="card"><div class="label">Total equity</div><div class="value">${totals["total_equity"]:,.2f}</div><div class="sub">cash + equity positions</div></div>
<div class="card"><div class="label">Cash</div><div class="value">${totals["cash"]:,.2f}</div><div class="sub">available ${totals["available"]:,.2f} &middot; reserved ${totals["reserved"]:,.2f}</div></div>
<div class="card"><div class="label">Positions value</div><div class="value">${totals["positions_value"]:,.2f}</div><div class="sub">{totals["equity_positions"]} equity &middot; {totals["option_positions"]} option</div></div>
<div class="card"><div class="label">Open orders</div><div class="value">{totals["open_orders"]}</div><div class="sub">{len(data["orders"]["stale"])} stale &middot; {len(data["orders"]["attention"])} cancel-attention</div></div>
<div class="card"><div class="label">Fills ({int(data["days"])}d)</div><div class="value">{totals["fills_in_window"]}</div><div class="sub">{totals["transactions_in_window"]} transactions</div></div>
<div class="card"><div class="label">Short premium held</div><div class="value">${totals["short_premium_held"]:,.2f}</div><div class="sub">put obligations ${totals["put_obligations"]:,.2f}</div></div>
</div>

<div class="tabs" role="tablist">
<button class="active" data-tab="overview">Overview</button>
<button data-tab="accounts">Accounts &amp; Strategies</button>
<button data-tab="positions">Positions</button>
<button data-tab="orders">Orders &amp; Transactions</button>
<button data-tab="health">Health &amp; System</button>
</div>

<div class="tab active" id="tab-overview">
<section class="card-block"><h2>Needs attention ({len(data["attention"])})</h2>{attention_html}</section>
<section class="card-block"><h2>Order status</h2>{_status_counts_html(data["orders"]["status_counts"])}</section>
<section class="card-block"><h2>Recent fills</h2>{_fills_html(data["orders"]["fills"][:10])}</section>
</div>

<div class="tab" id="tab-accounts">
<section class="card-block"><h2>Broker accounts</h2>{brokers_html}</section>
<section class="card-block"><h2>Strategies ({len(data["strategies"])})</h2>{strategies_html}</section>
<section class="card-block"><h2>Reservations (active ${data["reservations"]["total"]:,.2f})</h2>{_reservations_html(data["reservations"])}</section>
</div>

<div class="tab" id="tab-positions">
<section class="card-block"><h2>Equity positions ({len(data["positions"]["equities"])})</h2>
<div class="filter"><input type="search" data-filter="eq-table" placeholder="Filter positions&hellip;"></div>
{equities_html}</section>
<section class="card-block"><h2>Option positions ({len(data["positions"]["options"])})</h2>
<div class="filter"><input type="search" data-filter="opt-table" placeholder="Filter options&hellip;"></div>
{options_html}</section>
</div>

<div class="tab" id="tab-orders">
<section class="card-block"><h2>Open orders ({len(data["orders"]["open"])})</h2>
<div class="filter"><input type="search" data-filter="open-table" placeholder="Filter orders&hellip;"></div>
{orders_html}</section>
<section class="card-block"><h2>Transactions ({int(data["days"])}d)</h2>{txns_html}</section>
</div>

<div class="tab" id="tab-health">
<section class="card-block"><h2>Doctor checks</h2>{health_html}</section>
<section class="card-block"><h2>Audit &amp; reconciler signals</h2>{_audit_html(data["audit"])}</section>
<section class="card-block"><h2>Configuration &amp; paths</h2>{system_html}</section>
</div>

<footer>Generated by <span class="mono">nexus dashboard</span> at <span class="mono">{e(data["generated_at"])}</span>.</footer>
</div>
<script>
document.querySelectorAll('.tabs button').forEach(function (btn) {{
  btn.addEventListener('click', function () {{
    document.querySelectorAll('.tabs button').forEach(function (b) {{ b.classList.remove('active'); }});
    document.querySelectorAll('.tab').forEach(function (t) {{ t.classList.remove('active'); }});
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
  }});
}});
document.querySelectorAll('[data-filter]').forEach(function (input) {{
  input.addEventListener('input', function () {{
    var q = input.value.toLowerCase();
    var table = document.getElementById(input.dataset.filter);
    if (!table) return;
    table.querySelectorAll('tbody tr').forEach(function (tr) {{
      tr.style.display = tr.textContent.toLowerCase().includes(q) ? '' : 'none';
    }});
  }});
}});
</script>
</body>
</html>"""


def _attention_html(attention: list[dict]) -> str:
    e = html.escape
    if not attention:
        return '<div class="empty">Nothing needs attention.</div>'
    parts = []
    for a in attention:
        cls = "alert bad" if a["level"] == "bad" else "alert"
        parts.append(f'<div class="{cls}">{e(a["text"])}</div>')
    return "\n".join(parts)


def _status_counts_html(counts: dict) -> str:
    e = html.escape
    if not counts:
        return '<div class="empty">No orders recorded.</div>'
    rows = "".join(
        f"<tr><td>{e(str(s))}</td><td class=\"num\">{c}</td></tr>"
        for s, c in sorted(counts.items())
    )
    return f"<table><thead><tr><th>Status</th><th class=\"num\">Count</th></tr></thead><tbody>{rows}</tbody></table>"


def _fills_html(fills: list[dict]) -> str:
    e = html.escape
    if not fills:
        return '<div class="empty">No fills in this window.</div>'
    rows = []
    for o in fills:
        px = o.get("filled_avg_price")
        rows.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td>"
            "<td class=\"num\">{}</td><td class=\"num\">{}</td><td>{}</td></tr>".format(
                e(str(o.get("strategy", ""))),
                e(str(o.get("symbol", ""))),
                e(str(o.get("side", ""))),
                e(str(o.get("asset_class") or "")),
                e(str(o.get("filled_qty") or 0)),
                f"{float(px):.4f}" if px is not None else "&mdash;",
                e(str(o.get("filled_at") or o.get("updated_at") or "")),
            )
        )
    return (
        "<table><thead><tr><th>Strategy</th><th>Symbol</th><th>Side</th><th>Class</th>"
        "<th class=\"num\">Filled</th><th class=\"num\">Avg px</th><th>Filled at</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _strategies_html(strategies: list[dict]) -> str:
    e = html.escape
    if not strategies:
        return '<div class="empty">No strategies found.</div>'
    rows = []
    for s in strategies:
        badge = (
            '<span class="badge ok">live</span>'
            if s["prices_live"]
            else '<span class="badge muted">cost basis</span>'
        )
        active = '<span class="dot ok"></span>active' if s["is_active"] else '<span class="dot warn"></span>inactive'
        rows.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td>"
            "<td class=\"num\">{}</td><td class=\"num\">{}</td><td class=\"num\">{}</td>"
            "<td class=\"num\">{}</td><td class=\"num\">{}</td>"
            "<td class=\"num\">{}</td><td>{}</td></tr>".format(
                e(s["name"]),
                e(s["broker"]),
                active,
                f"${s['cash']:,.2f}",
                f"${s['available']:,.2f}",
                f"${s['positions_value']:,.2f}",
                f"${s['total_equity']:,.2f}",
                s["open_orders"],
                f"{s['equity_positions']}/{s['option_positions']}",
                badge,
            )
        )
    return (
        "<table><thead><tr><th>Strategy</th><th>Broker</th><th>Status</th>"
        "<th class=\"num\">Cash</th><th class=\"num\">Available</th>"
        "<th class=\"num\">Positions</th><th class=\"num\">Equity</th>"
        "<th class=\"num\">Open</th><th class=\"num\">Eq/Opt</th><th>Prices</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _brokers_html(brokers: list[dict], live_enabled: bool) -> str:
    e = html.escape
    if not brokers:
        return '<div class="empty">No broker accounts registered.</div>'
    rows = []
    for b in brokers:
        if not live_enabled:
            state = '<span class="badge muted">live off</span>'
            live_cash = "&mdash;"
            drift = "&mdash;"
        elif b["reachable"]:
            state = '<span class="badge ok">reachable</span>'
            live_cash = f"${b['live_cash']:,.2f}" if b["live_cash"] is not None else "&mdash;"
            drift = f"${b['drift']:+,.2f}" if b["drift"] is not None else "&mdash;"
        else:
            state = '<span class="badge bad">unreachable</span>'
            live_cash = f"<span class=\"mono\">{e(b['live_error'] or 'error')}</span>"
            drift = "&mdash;"
        rows.append(
            "<tr><td>{}</td><td>{}</td><td class=\"num\">{}</td>"
            "<td class=\"num\">{}</td><td class=\"num\">{}</td><td>{}</td><td>{}</td></tr>".format(
                e(b["profile"]),
                state,
                f"${b['cached_cash']:,.2f}",
                live_cash,
                drift,
                b["open_orders"],
                e(str(b.get("last_synced_at") or "never")),
            )
        )
    return (
        "<table><thead><tr><th>Profile</th><th>State</th>"
        "<th class=\"num\">Nexus cash</th><th class=\"num\">Alpaca cash</th>"
        "<th class=\"num\">Drift</th><th class=\"num\">Broker open</th><th>Last synced</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _positions_html(positions: dict) -> tuple[str, str]:
    e = html.escape
    equities = positions["equities"]
    if not equities:
        eq_html = '<div class="empty">No open equity positions.</div>'
    else:
        rows = []
        for p in equities:
            badge = (
                '<span class="badge ok">live</span>'
                if p["live_price"] is not None
                else '<span class="badge muted">cost basis</span>'
            )
            unreal = f"${p['unrealized']:+,.2f}" if p["unrealized"] is not None else "&mdash;"
            avg = f"${float(p['avg_entry']):,.4f}" if p["avg_entry"] is not None else "&mdash;"
            live = f"${p['live_price']:,.4f}" if p["live_price"] is not None else "&mdash;"
            rows.append(
                "<tr><td>{}</td><td>{}</td><td class=\"num\">{}</td>"
                "<td class=\"num\">{}</td><td class=\"num\">{}</td>"
                "<td class=\"num\">{}</td><td class=\"num\">{}</td>"
                "<td class=\"num\">{}</td><td class=\"num\">{}</td><td>{}</td></tr>".format(
                    e(p["strategy"]),
                    e(p["symbol"]),
                    p["qty"],
                    p["reserved"],
                    p["available"],
                    avg,
                    live,
                    f"${p['market_value']:,.2f}",
                    unreal,
                    badge,
                )
            )
        eq_html = (
            '<table id="eq-table"><thead><tr><th>Strategy</th><th>Symbol</th>'
            '<th class="num">Qty</th><th class="num">Reserved</th><th class="num">Avail</th>'
            '<th class="num">Avg entry</th><th class="num">Live</th>'
            '<th class="num">Mkt value</th><th class="num">Unreal</th><th>Prices</th></tr></thead>'
            f"<tbody>{''.join(rows)}</tbody></table>"
        )

    options = positions["options"]
    if not options:
        opt_html = '<div class="empty">No open option positions.</div>'
    else:
        rows = []
        for p in options:
            unreal = f"${p['unrealized']:+,.2f}" if p["unrealized"] is not None else "&mdash;"
            live = f"${p['live_premium']:,.4f}" if p["live_premium"] is not None else "&mdash;"
            dte = str(p["dte"]) if p["dte"] is not None else "&mdash;"
            oblig = f"${p['obligation']:,.2f}" if p["obligation"] is not None else "&mdash;"
            rows.append(
                "<tr><td>{}</td><td class=\"mono\">{}</td><td>{}</td><td>{}</td><td>{}</td>"
                "<td class=\"num\">{}</td><td class=\"num\">{}</td><td>{}</td>"
                "<td class=\"num\">{}</td><td class=\"num\">{}</td>"
                "<td class=\"num\">{}</td><td class=\"num\">{}</td><td class=\"num\">{}</td></tr>".format(
                    e(p["strategy"]),
                    e(p["symbol"]),
                    e(str(p["underlying"])),
                    e(str(p["right"])),
                    e(str(p["side"])),
                    p["qty"],
                    f"${float(p['strike']):,.2f}" if p["strike"] is not None else "&mdash;",
                    e(str(p["expiry"])),
                    dte,
                    f"${float(p['avg_premium']):,.4f}" if p["avg_premium"] is not None else "&mdash;",
                    live,
                    unreal,
                    oblig,
                )
            )
        opt_html = (
            '<table id="opt-table"><thead><tr><th>Strategy</th><th>OCC</th><th>Under</th>'
            '<th>Right</th><th>Side</th><th class="num">Qty</th><th class="num">Strike</th>'
            '<th>Expiry</th><th class="num">DTE</th><th class="num">Avg prem</th>'
            '<th class="num">Live</th><th class="num">Unreal</th><th class="num">Obligation</th></tr></thead>'
            f"<tbody>{''.join(rows)}</tbody></table>"
        )
    return eq_html, opt_html


def _orders_html(orders: dict) -> str:
    e = html.escape
    parts = []
    if orders["attention"]:
        rows = "".join(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                e(str(o.get("id", ""))),
                e(str(o.get("strategy", ""))),
                e(str(o.get("symbol", ""))),
                e(str(o.get("status", ""))),
                e(str(o.get("updated_at") or "")),
            )
            for o in orders["attention"]
        )
        parts.append(
            "<h3>Cancel attention</h3><table><thead><tr><th>ID</th><th>Strategy</th>"
            "<th>Symbol</th><th>Status</th><th>Updated</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    if orders["stale"]:
        rows = "".join(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                e(str(o.get("id", ""))),
                e(str(o.get("strategy", ""))),
                e(str(o.get("symbol", ""))),
                e(str(o.get("side", ""))),
                e(str(o.get("created_at") or "")),
            )
            for o in orders["stale"]
        )
        parts.append(
            "<h3>Stale (&gt;24h submitted)</h3><table><thead><tr><th>ID</th><th>Strategy</th>"
            "<th>Symbol</th><th>Side</th><th>Created</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    if not orders["open"]:
        parts.append('<div class="empty">No open orders.</div>')
    else:
        rows = []
        for o in orders["open"]:
            limit = o.get("limit_price")
            rows.append(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td>"
                "<td class=\"num\">{}</td><td>{}</td><td class=\"num\">{}</td>"
                "<td>{}</td><td>{}</td><td>{}</td></tr>".format(
                    e(str(o.get("id", ""))),
                    e(str(o.get("strategy", ""))),
                    e(str(o.get("symbol", ""))),
                    e(str(o.get("side", ""))),
                    e(str(o.get("qty", ""))),
                    e(str(o.get("order_type") or "")),
                    f"{float(limit):.2f}" if limit is not None else "&mdash;",
                    e(str(o.get("time_in_force") or "")),
                    e(str(o.get("status", ""))),
                    e(str(o.get("created_at") or "")),
                )
            )
        parts.append(
            '<table id="open-table"><thead><tr><th>ID</th><th>Strategy</th><th>Symbol</th>'
            '<th>Side</th><th class="num">Qty</th><th>Type</th><th class="num">Limit</th>'
            '<th>TIF</th><th>Status</th><th>Created</th></tr></thead>'
            f"<tbody>{''.join(rows)}</tbody></table>"
        )
    return "\n".join(parts)


def _transactions_html(transactions: dict) -> str:
    e = html.escape
    parts = []
    if transactions["sums"]:
        rows = "".join(
            "<tr><td>{}</td><td class=\"num\">{}</td><td class=\"num\">{}</td></tr>".format(
                e(s["type"]), f"${s['total']:+,.2f}", s["count"]
            )
            for s in transactions["sums"]
        )
        parts.append(
            "<h3>Sums by type</h3><table><thead><tr><th>Type</th>"
            '<th class="num">Total</th><th class="num">Count</th></tr></thead>'
            f"<tbody>{rows}</tbody></table>"
        )
    else:
        parts.append('<div class="empty">No transactions in this window.</div>')
    if transactions["recent"]:
        rows = "".join(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td>"
            "<td class=\"num\">{}</td><td>{}</td></tr>".format(
                e(str(t.get("created_at") or "")),
                e(str(t.get("strategy") or "")),
                e(str(t.get("type") or "")),
                e(str(t.get("symbol") or "")),
                f"${float(t.get('amount') or 0.0):+,.2f}",
                e(str(t.get("actor") or "")),
            )
            for t in transactions["recent"]
        )
        parts.append(
            "<h3>Recent</h3><table><thead><tr><th>Date</th><th>Strategy</th><th>Type</th>"
            '<th>Symbol</th><th class="num">Amount</th><th>Actor</th></tr></thead>'
            f"<tbody>{rows}</tbody></table>"
        )
    return "\n".join(parts)


def _reservations_html(reservations: dict) -> str:
    e = html.escape
    parts = []
    if not reservations["active"]:
        return '<div class="empty">No active reservations.</div>'
    rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td class=\"num\">{}</td><td>{}</td></tr>".format(
            e(str(r.get("strategy", ""))),
            e(str(r.get("symbol", ""))),
            e(str(r.get("order_id", ""))),
            f"${float(r.get('amount') or 0.0):,.2f}",
            e(str(r.get("status", ""))),
        )
        for r in reservations["active"][:20]
    )
    parts.append(
        "<table><thead><tr><th>Strategy</th><th>Symbol</th><th>Order</th>"
        '<th class="num">Amount</th><th>Order status</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )
    if reservations["orphans"]:
        parts.append(
            f"<h3>Orphaned ({len(reservations['orphans'])})</h3>"
            + "".join(
                f"<div class=\"alert\">Order {e(str(o['order_id']))} "
                f"({e(str(o['strategy']))} {e(str(o['symbol']))})</div>"
                for o in reservations["orphans"]
            )
        )
    return "\n".join(parts)


def _health_html(data: dict) -> str:
    e = html.escape
    rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            e(c["name"]),
            '<span class="badge ok">PASS</span>' if c["passed"] else '<span class="badge bad">FAIL</span>',
            e(c["detail"]),
        )
        for c in data["doctor_checks"]
    )
    sched = data["schedule"]
    if sched.get("installed"):
        sched_html = (
            f"<div>Reconciler cron: <span class=\"badge ok\">installed</span> "
            f"<span class=\"mono\">{e(str(sched.get('schedule')))}</span> "
            f"<span class=\"mono\">{e(str(sched.get('command')))}</span></div>"
        )
    else:
        sched_html = '<div>Reconciler cron: <span class="badge warn">not installed</span></div>'
    return (
        f"<table><thead><tr><th>Check</th><th>Result</th><th>Detail</th></tr></thead>"
        f"<tbody>{rows}</tbody></table><div style=\"margin-top:12px\">{sched_html}</div>"
    )


def _audit_html(audit: dict) -> str:
    e = html.escape
    if not audit.get("present"):
        return '<div class="empty">Audit log not found (no events recorded yet).</div>'
    parts = [f"<div>Events in tail: {audit['total']} lines scanned.</div>"]
    if audit["counts"]:
        rows = "".join(
            f"<tr><td>{e(str(k))}</td><td class=\"num\">{v}</td></tr>"
            for k, v in sorted(audit["counts"].items())
        )
        parts.append(
            "<h3>Event counts (tail)</h3><table><thead><tr><th>Event</th>"
            '<th class="num">Count</th></tr></thead>'
            f"<tbody>{rows}</tbody></table>"
        )
    if audit["errors"]:
        parts.append("<h3>Recent warnings</h3>" + "".join(
            f"<div class=\"alert bad\"><span class=\"mono\">{e(str(x.get('event', '')))}</span>"
            f" order {e(str(x.get('order_id', '')))}"
            f" {e(str(x.get('symbol', '')))}"
            f" &mdash; {e(str(x.get('reason', x.get('action', ''))))}</div>"
            for x in audit["errors"]
        ))
    else:
        parts.append('<div class="empty">No cancel/ghost warnings in tail.</div>')
    return "\n".join(parts)


def _system_html(data: dict) -> str:
    e = html.escape
    cfg = data["config"]
    rows = [
        ("Database", cfg["db_path"]),
        ("Audit log", cfg["audit_path"]),
        ("Reconcile interval", f"{cfg['reconcile_minutes']} minutes"),
        ("Market hours only", str(cfg["market_hours_only"])),
        ("Slippage buffer", f"{cfg['slippage_buffer_percent']}%"),
        ("Dashboard scope", str(data["scope"])),
    ]
    body = "".join(
        f"<tr><td>{e(k)}</td><td class=\"mono\">{e(v)}</td></tr>" for k, v in rows
    )
    return f"<table><tbody>{body}</tbody></table>"
