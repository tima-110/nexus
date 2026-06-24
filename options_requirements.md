# Nexus Options Trading Requirements

**Source**: Demeter (the_wheel specialist)
**Date**: 2026-06-23
**Priority**: Required for trading cycle launch
**Broker**: Alpaca paper trading (already configured as `champion`)

## Summary

Nexus currently routes equity orders through `nexus order buy/sell/close` and tracks positions per strategy. The Wheel strategy requires options order support: selling cash-secured puts and covered calls, tracking option positions, and computing P&L from premium collected.

Until Nexus supports options natively, the Wheel will submit orders directly via the Alpaca CLI (`alpaca order submit --symbol <OCC_symbol>`) and reconcile through a local `wheel_state` table in `the_wheel.db`. When Nexus options support is ready, the Wheel will migrate to Nexus exclusively per Protocol §2.

## Requirements

### 1. Option Order Submission

**Current gap**: `nexus order buy/sell` only accepts `SYMBOL QTY` (equity shares). Options require OCC-format contract symbols (e.g., `NKE260718P00040000`).

**Needed**:

```
nexus order sell --strategy the_wheel \
  --symbol NKE260718P00040000 \
  --qty 1 \
  --type limit \
  --limit-price 2.50
```

- `--symbol` must accept OCC option symbols (18-20 char format: `ROOT` + `YYMMDD` + `C/P` + `8-digit strike × 1000`)
- `--qty` = number of contracts (1 contract = 100 shares)
- `--type limit` with `--limit-price` (options should NOT use market orders — wide spreads)
- `--time-in-force day|gtc` support
- Under the hood: calls `alpaca order submit --symbol <OCC> --qty <n> --side sell --type limit --limit-price <p> --time-in-force day`

### 2. Option Position Tracking

**Current gap**: `nexus position list --strategy the_wheel` only shows equity positions. Options positions (short puts, short calls) are invisible to Nexus.

**Needed**:
- `nexus position list --strategy the_wheel` returns BOTH equity and option positions
- Option position entries include: `symbol` (OCC), `underlying`, `side` (short/long), `qty`, `avg_entry_price`, `current_value`, `unrealized_pl`
- This requires parsing Alpaca's option position data (available via `alpaca position list`)

### 3. Option Order History

**Current gap**: `nexus order list` shows equity orders only.

**Needed**:
- `nexus order list --strategy the_wheel` returns both equity and option orders
- Option order entries include: `symbol` (OCC), `side`, `qty`, `type`, `limit_price`, `status`, `filled_qty`, `filled_avg_price`

### 4. Assignment Detection

**Current gap**: Nexus has no awareness of option assignment events.

**Needed** (lower priority — can be worked around via polling):
- When Alpaca processes an assignment, the short option disappears and shares appear (or disappear) in the account
- Nexus should detect this transition and emit an event or flag in the position list
- Workaround until implemented: daily reconciliation script compares `wheel_state` (expected) against `alpaca position list` (actual)

### 5. Greeks & Premium Data (Optional — Nice to Have)

Nexus doesn't need to compute Greeks itself. The Wheel's research cycle already uses `alpaca data option chain` for real-time Greeks. If Nexus wants to store Greeks for P&L attribution, the relevant fields are: `delta`, `gamma`, `theta`, `vega`, `iv`.

## Interim Workaround (Pre-Nexus Options Support)

While Nexus doesn't support options, the Wheel will:

1. **Submit orders directly** via `alpaca order submit` with OCC contract symbols
2. **Track positions locally** in `the_wheel.db` `wheel_state` table (not in Nexus)
3. **Reconcile daily** by comparing `wheel_state` against `alpaca position list` output
4. **Log premium collected** in `wheel_state.premium_collected` field

When Nexus options support lands:
1. Switch all `alpaca order submit` calls to `nexus order sell/buy`
2. Migrate `wheel_state` tracking to Nexus position tracking
3. Remove the reconciliation workaround

## OCC Symbol Format Reference

```
Root (1-5 chars) + YYMMDD + C/P + 8-digit strike (×1000)

Examples:
  NKE260718P00040000  →  NKE, exp 2026-07-18, Put, strike $40.00
  AAPL260821C00225000 →  AAPL, exp 2026-08-21, Call, strike $225.00
```

Construction from research data:
- `underlying`, `expiry` (YYYY-MM-DD), `option_type` (P/C), `strike` (float)
- Pad root to left, pad strike to 8 digits (multiply by 1000, zero-pad)
