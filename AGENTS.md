# nexus — Agent Instructions

## What this project is

Pollux Nexus is an execution gateway and strategy tracking layer for broker APIs.
It acts as the sole execution path between trading strategies and Alpaca paper accounts,
maintaining per-strategy virtual balances and positions. Supports both equity
and option orders (cash-secured puts, covered calls).

## How to work in this repo

- Follow src-layout conventions (source in `src/nexus/`, tests in `tests/`)
- Run `pytest` before committing
- CLI commands go in `src/nexus/cli/` as Typer sub-apps
- All Alpaca interaction goes through `src/nexus/broker/alpaca.py` — nowhere else
- Domain logic lives in dedicated modules (guards.py, ledger.py, reconciler.py)
- Every order state change must write a JSONL audit entry
- Option order flow: `option-sell`/`option-buy` commands, OCC symbols, `option_positions` DB table
- OCC symbol parsing lives in `src/nexus/occ.py`
- `process_option_fill()` in `ledger.py` handles option fill lifecycle (short open/close, long open/close)
- `check_option_sell_guard()` / `check_option_buy_guard()` in `guards.py` validate before submission
- Eager sync in `sync.py` auto-routes OCC symbol fills to `process_option_fill()`

## Testing

```bash
pytest
```

Tests mock the Alpaca CLI subprocess calls — never hit real APIs in tests.

## Option Orders

| Command | Purpose | Guard |
|---------|---------|-------|
| `order option-sell <OCC> <qty>` | Sell put/call (open short) | Cash check for puts, covered check for calls |
| `order option-buy <OCC> <qty>` | Buy option (close short or open long) | Cash check for premium |
| `order list --asset-class option` | Filter orders to options only | — |
| `position show <strat> <OCC>` | Show option position detail | — |

OCC format: `ROOT (1-6 chars) + YYMMDD + C/P + 8-digit strike × 1000`
Example: `NKE260718P00040000` = NKE, 2026-07-18, Put, $40.00 strike
