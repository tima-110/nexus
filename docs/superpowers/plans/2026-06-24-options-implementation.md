# Nexus Options Trading — Implementation Plan

**Date:** 2026-06-24
**Status:** Approved
**Version:** 1.0

---

## 0. Summary

Extend Nexus with options order submission (cash-secured puts, covered calls),
option position tracking, and order history for options — all following the
existing reservation + eager-sync model.

**Design decisions (user-confirmed):**
- **Cash reservation** for puts: full strike × 100 × qty (conservative)
- **Position display**: mixed equity + option in `position list`
- **CLI design**: explicit `order option-sell` / `order option-buy` subcommands

---

## 1. Data Model Changes

### 1a. New enums (`src/nexus/models.py`)

```python
class OptionRight(str, Enum):
    call = "call"
    put = "put"


class AssetClass(str, Enum):
    equity = "equity"
    option = "option"
```

### 1b. OCC utility functions (`src/nexus/occ.py` — new module)

```python
def parse_occ_symbol(symbol: str) -> dict:
    """Parse OCC into {root, expiry, option_type, strike}."""

def is_occ_symbol(symbol: str) -> bool:
    """Return True if symbol matches OCC format (18-21 alphanumeric)."""

def occ_to_underlying(symbol: str) -> str:
    """Strip expiry/right/strike from OCC to return root symbol."""
```

### 1c. Broker types (`src/nexus/broker/types.py`)

Add `BrokerOptionPosition` dataclass:
```python
@dataclass
class BrokerOptionPosition:
    symbol: str
    underlying: str
    side: str           # "short" | "long"
    qty: int
    avg_entry_price: Decimal
    current_price: Decimal
    unrealized_pl: Decimal
    strike: Decimal
    expiry: str
    option_right: str   # "call" | "put"
```

### 1d. DB table (`src/nexus/db.py`)

Add `option_positions` table:

```sql
CREATE TABLE IF NOT EXISTS option_positions (
    id              INTEGER PRIMARY KEY,
    strategy_id     INTEGER NOT NULL REFERENCES strategies(id),
    symbol          TEXT    NOT NULL,       -- OCC symbol
    underlying      TEXT    NOT NULL,       -- e.g. "NKE"
    option_right    TEXT    NOT NULL,       -- "call" or "put"
    side            TEXT    NOT NULL,       -- "short" or "long"
    qty             INTEGER NOT NULL,       -- contracts
    avg_entry_price REAL,                   -- per-contract
    strike          REAL    NOT NULL,
    expiry          TEXT    NOT NULL,       -- YYYY-MM-DD
    opened_at       TEXT,
    updated_at      TEXT,
    UNIQUE (strategy_id, symbol)
);
```

---

## 2. Broker Layer (`src/nexus/broker/alpaca.py`)

### 2a. `list_option_positions()`

Calls `alpaca position list`, filters entries with `asset_class == "us_option"`,
parses into `BrokerOptionPosition`. Also parses the OCC symbol on the fly to
populate `underlying`, `strike`, `expiry`, `option_right`.

### 2b. Existing `submit_order()` — no changes needed

Alpaca's order submit already supports OCC symbols and contract qty. The
existing method works as-is.

---

## 3. Guard Logic (`src/nexus/guards.py`)

### 3a. `check_option_sell_guard()`

- **Put** (cash-secured): `available_cash >= strike * 100 * qty`. Available
  cash = strategy.cash_balance - sum(reservations).
- **Call** (covered): strategy holds the underlying shares
  (`position.qty >= 100 * qty`). Also check shares aren't entirely reserved.
- No duplicate-position check (multiple puts on same underlying OK).

### 3b. `check_option_buy_guard()`

- **Buy to close short**: strategy has an open short position matching the OCC
  symbol with qty >= requested.
- **Buy to open long**: cash check for premium cost.

---

## 4. CLI — Option Order Submission (`src/nexus/cli/order.py`)

### 4a. `order option-sell`

```
nexus order option-sell <OCC_SYMBOL> <QTY> \
  --strategy <NAME> \
  --limit-price <PRICE> \
  --type limit              (default: limit) \
  --time-in-force day|gtc   (default: day) \
  --actor <ACTOR>
```

**Flow:**
1. Load config, resolve strategy + broker
2. Eager sync (existing)
3. Parse OCC → determine put/call
4. `check_option_sell_guard()` — cash check for puts, covered check for calls
5. Reserve cash: `strike * 100 * qty` for puts; 0 for calls (no cash outlay)
6. Insert order record (symbol=OCC, side="sell", order_type="limit", reserved_amount = strike * 100 * qty for puts, 0 for calls)
7. Submit to broker
8. Audit log event

### 4b. `order option-buy`

```
nexus order option-buy <OCC_SYMBOL> <QTY> \
  --strategy <NAME> \
  --limit-price <PRICE> \
  --type limit              (default: limit) \
  --time-in-force day|gtc   (default: day)
```

**Flow:**
1. Load config, resolve strategy + broker
2. Eager sync
3. Parse OCC
4. `check_option_buy_guard()` — check position exists if closing short
5. Reserve cash: `limit_price * 100 * qty`
6. Insert order record (symbol=OCC, side="buy")
7. Submit to broker

### 4c. `order list` — `--asset-class` filter

Add optional `--asset-class equity|option` filter. Option orders show OCC
symbol in the symbol column. No structural change needed — OCC symbols
already work in the existing list.

---

## 5. Position Tracking (`src/nexus/cli/position.py`)

### 5a. `position list` — mixed display

Append `option_positions` rows to the existing equity positions list.

**Equity entries** (unchanged):
```
STRATEGY  SYMBOL  QTY  RESERVED  AVAILABLE  AVG_ENTRY  VALUE  TYPE
the_wheel AAPL     100      10         90   175.0000  17500.00 equity
```

**Option entries** (new):
```
the_wheel NKE260718P00040000 NKE  put  short   1  40.00  2.50  250.00 option
```

Column layout for options:
```
STRATEGY  SYMBOL           UNDERLYING  RIGHT  SIDE   QTY  STRIKE  AVG_PREM  VALUE  TYPE
```

When `--json` is used, return `{"items": [...]}` with `"asset_class"` field on
each item so consumers can differentiate.

### 5b. `position show` — OCC-aware

When symbol matches OCC format, show:
- Underlying, Right, Strike, Expiry, Side, Qty
- Premium collected/paid (avg_entry_price)
- Current price from broker (if available)
- Premium P&L = (avg_entry_price - current_price) * qty * 100 (adjusted for side)

---

## 6. Ledger — Option Fill Processing (`src/nexus/ledger.py`)

### 6a. `process_option_fill()`

Handles option fills:

- **Sell fill** (short opened):
  - Insert/update `option_positions` (side="short", qty increased)
  - No equity position change
  - Cash: credit `premium * qty * 100` (premium collected)
  - No reservation release for option sells (cash was not reserved for the premium, it was reserved for assignment — the fill doesn't release the assignment reservation)

- **Buy fill** (short closed):
  - Reduce/remove from `option_positions`
  - Cash: debit `premium * qty * 100` (cost to buy back)
  - Release assignment reservation if any

- **Buy fill** (long opened):
  - Insert/update `option_positions` (side="long")
  - Cash: debit `premium * qty * 100`

- **Sell fill** (long closed):
  - Reduce/remove from `option_positions`
  - Cash: credit `premium * qty * 100`

### 6b. `process_cancel_option()` — release assignment reservation

When an option sell order is cancelled, release the strike * 100 * qty
reservation that was created.

---

## 7. File-by-File Change Summary

| File | Changes |
|------|---------|
| `src/nexus/__init__.py` | Bump version to 0.3.0 |
| `src/nexus/models.py` | Add `OptionRight`, `AssetClass` enums |
| `src/nexus/occ.py` | **New** — OCC parse/detect utility |
| `src/nexus/broker/types.py` | Add `BrokerOptionPosition` |
| `src/nexus/broker/alpaca.py` | Add `list_option_positions()` |
| `src/nexus/broker/__init__.py` | Export new type |
| `src/nexus/db.py` | Add `option_positions` table |
| `src/nexus/guards.py` | Add `check_option_sell_guard()`, `check_option_buy_guard()` |
| `src/nexus/ledger.py` | Add `process_option_fill()`, `process_cancel_option()` |
| `src/nexus/cli/order.py` | Add `order option-sell`, `order option-buy`, `--asset-class` on list |
| `src/nexus/cli/position.py` | Extend list/show with option positions |
| `src/nexus/cli/__init__.py` | (unchanged — `order_app` already registered) |
| `tests/test_occ.py` | **New** — OCC parse tests |
| `tests/test_option_order.py` | **New** — option order CLI tests |
| `tests/test_option_ledger.py` | **New** — option fill processing tests |

---

## 8. Implementation Order

1. **OCC utilities** (`occ.py`, update `models.py`) — no deps
2. **Broker types + method** (`types.py`, `alpaca.py`) — depends on #1
3. **DB table** (`db.py`) — independent
4. **Guards** (`guards.py`) — depends on OCC parsing, DB schema
5. **Ledger** (`ledger.py`) — depends on DB schema, can implement in parallel with #4
6. **CLI — order commands** (`order.py`) — depends on #2, #4, #5
7. **CLI — position display** (`position.py`) — depends on #2, #3
8. **Tests**
9. **Bump version**, update docs/help text

---

## 9. Non-Goals (Post-MVP)

- Greeks storage (delta, gamma, theta, vega, IV) — tracked externally by Wheel
- Multi-leg orders (spreads, straddles)
- Auto-exercise / DNE instructions
- Assignment detection (Wheel reconciles via wheel_state)
- `order replace` for options (can be added later — broker supports it)