# Pollux Nexus — Design Specification

**Date:** 2026-05-24
**Status:** Draft
**Version:** 1.0

## Overview

Pollux Nexus is the sole execution gateway between trading strategies and broker APIs. It solves the fundamental problem of tracking per-strategy P&L and cash balances when 10–20 virtual strategy "accounts" share 2–3 real Alpaca paper accounts.

All orders — buys, sells, closes, cancellations — must flow through Nexus. No strategy is permitted to call a broker API directly. This guarantees a complete record and enables per-strategy accounting against shared broker accounts.

## Architecture: Reservation + Eager-Sync Gateway

Every CLI command begins by syncing outstanding orders for the calling strategy (eager sync), then uses a reservation model for buying power. Pending orders immediately reduce available balance, preventing double-spending across concurrent strategies.

- **Eager sync** (per-command): queries the local DB for this strategy's outstanding orders (status: submitted/partially_filled), then calls `alpaca order get-by-client-id` for each to check for fills before processing the command
- **Reservations**: lock estimated cost (buys) or shares (sells) at order placement
- **Background reconciler**: cron-scheduled sweep catches fills for all strategies, detects drift, and identifies bypass orders
- **Non-blocking**: multiple orders can be outstanding simultaneously across any number of strategies

### Key Formula

```
available_buying_power = (cash_balance - sum(active_reservations)) × margin_multiplier
available_shares(symbol) = position.qty - sum(reserved_qty from pending sell orders for symbol)
```

## Data Model

### Storage

- **SQLite** (WAL mode): primary operational store — orders, positions, balances, transactions, reservations
- **JSONL**: append-only audit log recording every state transition (order received, submitted, filled, etc.)
- SQLite is authoritative. JSONL is the flight recorder for debugging and auditing.

### Tables

#### `broker_accounts`

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| profile_name | TEXT UNIQUE | Alpaca CLI profile name |
| margin_multiplier | REAL | Default 2.0 for paper |
| cash_balance | REAL | Last synced from Alpaca |
| last_synced_at | TEXT | ISO timestamp |

#### `strategies`

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| name | TEXT UNIQUE | Human-readable strategy name |
| broker_account_id | INTEGER FK | Which Alpaca profile routes through |
| cash_balance | REAL | Settled cash (changes on fills + manual adjustments) |
| is_active | INTEGER | 1=active, 0=disabled |
| created_at | TEXT | ISO timestamp |

#### `orders`

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| strategy_id | INTEGER FK | |
| symbol | TEXT | Ticker |
| side | TEXT | buy, sell |
| qty | INTEGER | Shares requested |
| order_type | TEXT | market, limit, stop, stop_limit, trailing_stop |
| limit_price | REAL | Nullable |
| stop_price | REAL | Nullable |
| trail_percent | REAL | Nullable |
| status | TEXT | pending, submitted, filled, partially_filled, cancelled, expired, rejected |
| client_order_id | TEXT UNIQUE | Format: `nx-{strategy_short}-{symbol}-{8char_uuid}` |
| broker_order_id | TEXT | Alpaca's returned order ID |
| reserved_amount | REAL | Cash (buys) or shares (sells) reserved |
| filled_qty | INTEGER | Shares filled so far |
| filled_avg_price | REAL | Nullable |
| filled_at | TEXT | ISO timestamp, nullable |
| actor | TEXT | Who placed it: `agent:claude`, `cli:manual`, etc. |
| created_at | TEXT | ISO timestamp |
| updated_at | TEXT | ISO timestamp |

#### `positions`

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| strategy_id | INTEGER FK | |
| symbol | TEXT | Ticker |
| qty | INTEGER | Total shares held |
| reserved_qty | INTEGER | Shares committed to pending sell orders |
| avg_entry_price | REAL | |
| opened_at | TEXT | ISO timestamp |
| updated_at | TEXT | ISO timestamp |

Unique constraint: `(strategy_id, symbol)`

#### `transactions`

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| strategy_id | INTEGER FK | |
| order_id | INTEGER FK | Nullable (null for manual deposits/withdrawals) |
| type | TEXT | fill_buy, fill_sell, deposit, withdrawal, adjustment |
| amount | REAL | Positive for credits, negative for debits |
| actor | TEXT | Who initiated |
| note | TEXT | Nullable, human-readable context |
| created_at | TEXT | ISO timestamp |

#### `reservations`

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| strategy_id | INTEGER FK | |
| order_id | INTEGER FK | |
| amount | REAL | Cash amount reserved for this buy order |
| created_at | TEXT | ISO timestamp |

This table tracks cash holds for **buy orders only**. Row is deleted when the associated order reaches a terminal state (filled, cancelled, expired).

Sell-side reservations (share holds) are tracked via `positions.reserved_qty` — incremented when a sell order is placed, decremented when it fills or is cancelled. The `orders.reserved_amount` field records what was reserved (cash or shares depending on side) for historical reference.

## Order Lifecycle

### State Machine

```
                    ┌─────────────────────────────────────────────┐
                    │                                             │
CLI Command         ▼                                             │
┌─────────┐  ┌───────────┐  ┌───────────┐                        │
│  Guard   │─▶│  PENDING  │─▶│ SUBMITTED │────────────────────┐   │
│Validation│  └───────────┘  └───────────┘                    │   │
└─────────┘   (reservation    (sent to                        │   │
     │         created)        Alpaca)                         │   │
     │                            │                           │   │
     │                            ├────────────┐              │   │
     ▼                            ▼            ▼              ▼   │
┌──────────┐              ┌────────────┐ ┌─────────┐  ┌──────────┐
│ REJECTED │              │ PARTIALLY  │ │ FILLED  │  │CANCELLED │
│(by Nexus)│              │   FILLED   │ │         │  │          │
└──────────┘              └────────────┘ └─────────┘  └──────────┘
```

### Transitions

| From | To | Trigger |
|------|----|---------|
| (new) | REJECTED | Guard validation fails (insufficient balance, duplicate position, no shares to sell) |
| (new) | PENDING | Guard passes, reservation created |
| PENDING | SUBMITTED | Alpaca accepts the order |
| PENDING | CANCELLED | Alpaca rejects submission (API error, market closed) |
| SUBMITTED | FILLED | Reconciler/eager-sync detects full fill |
| SUBMITTED | PARTIALLY_FILLED | Reconciler detects partial fill |
| SUBMITTED | CANCELLED | User cancels, or Alpaca cancels (day order expired, etc.) |
| PARTIALLY_FILLED | FILLED | Remaining qty fills |
| PARTIALLY_FILLED | CANCELLED | User cancels remainder |

### Terminal State Effects

When an order reaches a terminal state:
1. Release/adjust reservation
2. Create transaction record (if filled — full or partial)
3. Update `strategy.cash_balance`
4. Update/create position record
5. Write JSONL audit entry

### Guards

**Buy guard:**
- Strategy must NOT already hold the symbol (no duplicate entries unless scaling is explicitly enabled later)
- `available_buying_power >= estimated_cost` (last_price × qty × (1 + slippage_buffer))

**Sell guard:**
- Strategy MUST hold the symbol
- `position.available_qty >= sell_qty` (accounts for shares reserved by other pending sell orders)

### Reservation Model

**Buy orders:** Reserve estimated cash cost at placement time.
- Market orders: `last_price × qty × (1 + slippage_buffer_percent)` — last price is fetched from Alpaca via `alpaca data quotes <SYMBOL> --profile <name>`
- Limit orders: `limit_price × qty` (exact maximum known)
- Stop/trailing: `stop_price × qty × (1 + buffer)` (or for trailing stops without a stop price, use last_price × qty × buffer)

**Sell orders:** Reserve shares (not cash).
- `reserved_qty` on the position record increases by the sell qty

**On fill:** Reservation is released. Actual cost/proceeds replace the estimate. The delta (over/under-reservation) is absorbed into the balance adjustment.

**On cancel/expire:** Reservation is released entirely. No balance change.

### Client Order ID Convention

Format: `nx-{strategy_short}-{symbol}-{8char_uuid}`

Example: `nx-hybmom-AAPL-a3f7c2e1`

- Max 128 characters (Alpaca limit)
- `nx-` prefix enables bypass detection (any order on the broker account without this prefix was placed outside Nexus)
- Human-readable in Alpaca dashboard

## CLI Command Surface

### Global Flags

| Flag | Description |
|------|-------------|
| `--json` | Machine-readable JSON output on all commands |
| `--version` | Print version |
| `--help` | Help |

### Order Commands

```
nexus order buy <SYMBOL> <QTY> --strategy <NAME> [--type market|limit|stop|stop_limit|trailing_stop] [--limit-price X] [--stop-price X] [--trail-percent X] [--actor "agent:name"]
nexus order sell <SYMBOL> <QTY> --strategy <NAME> [--type ...] [--limit-price X] [--stop-price X] [--trail-percent X] [--actor "agent:name"]
nexus order close <SYMBOL> --strategy <NAME>      # atomic: sell entire available position
nexus order cancel <ORDER_ID>
nexus order status <ORDER_ID>
nexus order list [--strategy NAME] [--status STATUS] [--symbol SYMBOL]
```

- `--strategy` is required on buy/sell/close (no default — forces explicit attribution)
- `--actor` defaults to `cli:manual` if omitted
- `--type` defaults to `market`
- Market orders during market hours optionally wait briefly for fill (configurable timeout)

### Strategy Commands

```
nexus strategy list
nexus strategy show <NAME>                          # balance, positions, open orders, P&L
nexus strategy create <NAME> --broker <PROFILE> --balance <AMOUNT>
nexus strategy deposit <NAME> <AMOUNT> [--note "..."]
nexus strategy withdraw <NAME> <AMOUNT>
nexus strategy set-broker <NAME> --broker <PROFILE>
```

### Broker Commands

```
nexus broker list
nexus broker add <PROFILE_NAME> [--margin-multiplier 2.0]
nexus broker show <PROFILE_NAME>                    # live balance + positions from Alpaca
nexus broker sync [PROFILE_NAME]                    # refresh cached balance
nexus broker remove <PROFILE_NAME>                  # only if no strategies attached
```

### Position Commands

```
nexus position list [--strategy NAME]
nexus position show <STRATEGY> <SYMBOL>             # qty, available, reserved, entry, P&L
```

### Schedule & Reconciliation Commands

```
nexus reconcile [--strategy NAME]                   # one-shot reconciliation sweep
nexus install                                       # install cron entry
nexus uninstall                                     # remove cron entry
nexus status                                        # schedule status + last run info
```

### Operational Commands

```
nexus doctor                                        # health checks (see below)
nexus config show
nexus config set <KEY> <VALUE>
nexus config path
nexus set-credentials                               # validate Alpaca profile connectivity
nexus history [--strategy NAME] [--symbol SYMBOL] [--since DATE]
```

### Doctor Checks

- SQLite database exists and is valid
- Alpaca CLI installed and profiles reachable
- No orphaned reservations (terminal orders with unreleased reservations)
- No stale submitted orders (configurable age threshold)
- Virtual balance vs broker balance drift within tolerance
- JSONL audit log writable
- Cron schedule installed and running

## Reconciler

### Responsibilities

1. **Sync broker balances** — GET account balance from Alpaca for each broker profile, update cache, check drift
2. **Poll outstanding orders** — query all orders in `submitted` or `partially_filled` status, check Alpaca for updates, apply state transitions
3. **Detect orphaned state** — reservations with no matching active order, orders stuck in PENDING
4. **Detect bypass** — orders on broker accounts whose `client_order_id` doesn't start with `nx-`

### Scheduling

- Installed via `nexus install` using `python-crontab`
- Cron comment: `nexus-reconciler`
- Default interval: every 5 minutes (`*/5 * * * *`)
- Configurable via `reconciler.interval_minutes` in TOML config
- The command itself (`nexus reconcile`) handles market-hours awareness: full sweep during market hours, reduced scope (stale-order check only) outside market hours
- Manual trigger: `nexus reconcile` at any time

### Concurrency Safety

- SQLite WAL mode allows concurrent readers/writers
- Each order state transition is an atomic transaction (update order + create transaction + update balance + delete reservation)
- Reconciler is idempotent — processing an already-terminal order is a no-op

### Failure Handling

- Alpaca unreachable → log error, skip cycle, try next run
- Single order query fails → log, skip that order, continue sweep
- DB write fails → rollback that order's transaction, log, continue

## Broker Abstraction Layer

### Design Principle

Thin adapter wrapping the Alpaca CLI. No abstract base class (YAGNI). The module boundary is the abstraction. If a second broker is added later, extract a Protocol from the concrete implementation.

### Interface

```python
class AlpacaBroker:
    def __init__(self, profile_name: str): ...

    # Orders
    def submit_order(self, symbol, qty, side, order_type, **params) -> BrokerOrder: ...
    def get_order(self, broker_order_id: str) -> BrokerOrder: ...
    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder: ...
    def list_orders(self, status: str = "open") -> list[BrokerOrder]: ...
    def cancel_order(self, broker_order_id: str) -> None: ...

    # Account
    def get_account(self) -> BrokerAccount: ...
    def get_positions(self) -> list[BrokerPosition]: ...

    # Market data
    def get_last_price(self, symbol: str) -> Decimal: ...
```

### Implementation

- Shells out to Alpaca CLI via `subprocess.run()`
- Uses `--profile <name>` and `--quiet` flags on every call
- Parses JSON from stdout
- Translates Alpaca's response format into our own dataclasses (`BrokerOrder`, `BrokerAccount`, `BrokerPosition`)
- All Alpaca CLI interaction is isolated to `src/nexus/broker/alpaca.py`
- Alpaca SDK exceptions are caught at the boundary and re-raised as Nexus exceptions (`BrokerAPIError`, `BrokerOrderRejected`, `BrokerTimeout`)

### Return Types

```python
@dataclass
class BrokerOrder:
    broker_order_id: str
    client_order_id: str
    status: str
    symbol: str
    side: str
    qty: int
    filled_qty: int
    filled_avg_price: Decimal | None
    submitted_at: str
    filled_at: str | None

@dataclass
class BrokerAccount:
    cash: Decimal
    buying_power: Decimal
    equity: Decimal

@dataclass
class BrokerPosition:
    symbol: str
    qty: int
    avg_entry_price: Decimal
    current_price: Decimal
    unrealized_pl: Decimal
```

## JSONL Audit Log

### Write Points

A JSONL entry is appended on every order state change:

1. **Order received** — Nexus accepts the order intent
2. **Order submitted** — sent to Alpaca, broker_order_id received
3. **Order state change** — filled, partially filled, cancelled, expired

### Schema

```json
{
  "ts": "2026-05-24T10:30:01.123Z",
  "event": "order_submitted",
  "order_id": "nx-hybmom-AAPL-a3f7c2e1",
  "broker_order_id": "abc-123-def",
  "strategy": "hybrid_mom",
  "actor": "agent:claude",
  "symbol": "AAPL",
  "side": "buy",
  "qty": 50,
  "order_type": "market",
  "reserved": 5025.00,
  "status": "submitted"
}
```

### Operational Details

- File location: configurable in TOML (`audit_log.path`)
- Default: `~/.local/share/nexus/audit.jsonl` (via platformdirs)
- Write is synchronous (flush before returning from state transition)
- File is append-only; rotation is the operator's concern (logrotate or manual)

## Configuration

### TOML Config File

Location: `~/.config/nexus/config.toml` (via platformdirs)

```toml
[reconciler]
interval_minutes = 5
market_hours_only = false        # if true, skip full sweep outside 9:30-16:00 ET

[order]
slippage_buffer_percent = 1.5    # reservation buffer for market orders
market_order_wait_seconds = 5    # how long to wait for market order fill

[database]
path = "~/.local/share/nexus/nexus.db"

[audit_log]
path = "~/.local/share/nexus/audit.jsonl"
```

### Credentials

- No secrets in TOML — Alpaca credentials are managed by Alpaca CLI profiles
- `nexus set-credentials` validates that profiles are configured and reachable

## Project Structure

```
nexus/
├── pyproject.toml
├── README.md
├── CLAUDE.md
├── AGENTS.md
├── .gitignore
├── .context/
│   └── lessons.md
├── docs/
│   └── config-guide.md
├── tests/
│   ├── conftest.py
│   ├── fixtures/
│   ├── test_orders.py
│   ├── test_guards.py
│   ├── test_reconciler.py
│   ├── test_strategies.py
│   └── test_broker.py
└── src/nexus/
    ├── __init__.py
    ├── __main__.py
    ├── main.py
    ├── cli/
    │   ├── __init__.py
    │   ├── order.py
    │   ├── strategy.py
    │   ├── broker_cmd.py
    │   ├── position.py
    │   ├── schedule.py
    │   └── ops.py
    ├── config.py
    ├── db.py
    ├── models.py
    ├── guards.py
    ├── ledger.py
    ├── reconciler.py
    ├── audit.py
    ├── broker/
    │   ├── __init__.py
    │   ├── alpaca.py
    │   └── types.py
    └── schedule/
        ├── __init__.py
        └── cron.py
```

### Module Responsibilities

| Module | Responsibility |
|--------|---------------|
| `cli/*` | Parse commands, format output, delegate to domain modules |
| `config.py` | Load TOML, validate with Pydantic, resolve paths |
| `db.py` | SQLite connection, schema creation, WAL mode, migrations |
| `models.py` | Domain types shared across modules (enums, dataclasses) |
| `guards.py` | Pre-order validation (position check, balance check, duplicates) |
| `ledger.py` | Create transactions, manage reservations, update balances |
| `reconciler.py` | Poll Alpaca, apply state transitions, detect drift/bypass |
| `audit.py` | Append JSONL entries on state changes |
| `broker/alpaca.py` | Translate between Alpaca CLI and internal types |
| `schedule/cron.py` | Install/uninstall/check crontab entries |

### Dependency Flow

```
cli/* → guards, ledger, reconciler, broker, models, config
guards → db, models, broker
ledger → db, models, audit
reconciler → db, models, broker, ledger, audit
broker/alpaca.py → subprocess (Alpaca CLI), broker/types.py
audit → (standalone, writes JSONL)
```

No circular dependencies.

### pyproject.toml

```toml
[build-system]
requires = ["hatchling>=1.25.0"]
build-backend = "hatchling.build"

[project]
name = "nexus"
version = "0.1.0"
description = "Execution gateway and strategy tracking layer for broker APIs."
requires-python = ">=3.12"
dependencies = [
    "typer[all]>=0.12",
    "rich>=13",
    "pydantic>=2",
    "platformdirs>=4",
    "python-crontab>=3.0",
]

[dependency-groups]
dev = ["pytest>=8.0"]

[project.scripts]
nexus = "nexus.main:main"

[tool.hatch.build.targets.wheel]
packages = ["src/nexus"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
```

## Phased Build Order

### Phase 1: Foundation
- Project scaffolding (structure, pyproject.toml, CLAUDE.md)
- SQLite schema + db.py
- Config (Pydantic models + TOML loader)
- Domain models (enums, dataclasses)
- Broker adapter (Alpaca CLI wrapper — submit, get, list, cancel, account, positions)

### Phase 2: Core Order Flow
- Guards (buy guard, sell guard, balance check)
- Ledger (reservations, transactions, balance updates)
- Audit logger (JSONL append)
- CLI order commands (buy, sell, close, cancel, status, list)
- Eager sync logic (sync outstanding orders before command execution)

### Phase 3: Strategy & Position Management
- CLI strategy commands (create, show, list, deposit, withdraw, set-broker)
- CLI broker commands (add, show, list, sync, remove)
- CLI position commands (list, show)
- CLI history command

### Phase 4: Reconciler & Scheduling
- Reconciler logic (full sweep: sync balances, poll orders, detect drift, detect bypass)
- Schedule management (install/uninstall/status via python-crontab)
- Doctor command

### Phase 5: Polish
- Config commands (show, set, path)
- set-credentials command
- `--json` output on all commands
- Error handling hardening
- Edge cases (partial fills, order replacement, market hours detection)

## Decisions Deferred

| Decision | Reason | Revisit When |
|----------|--------|-------------|
| PDT protection | Real account is above $25k threshold | Account falls below threshold |
| Bracket/OCO orders | Individual orders sufficient for v1 | Strategy needs atomic entry+stop |
| Notifications (Telegram) | Logs/CLI sufficient for v1 | Operational maturity requires push alerts |
| MCP server interface | CLI+JSON sufficient for agents | Agent ecosystem demands structured tools |
| Per-strategy guard config | Strict-for-all is simpler | A strategy needs scaling-in behavior |
| Abstract broker Protocol | Only one broker (Alpaca) | Second broker is added |
| Streaming (WebSocket) fills | Polling every 5 min is adequate | Latency-sensitive strategies arrive |
