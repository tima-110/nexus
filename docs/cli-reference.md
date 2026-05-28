# Nexus CLI Reference

Complete reference for all `nexus` commands. Version 0.1.0.

---

## Global Options

These options are available on all commands.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--json` | bool | `false` | Output as JSON (machine-readable) |
| `--version` | bool | `false` | Print version and exit |
| `--help` | — | — | Show help message and exit |

```
nexus --version
nexus --json order list --strategy momentum
```

---

## Order Commands

Manage buy/sell orders. All order commands perform an eager sync of outstanding orders for the calling strategy before proceeding.

### `nexus order buy`

Place a buy order.

**Synopsis:** `nexus order buy SYMBOL QTY [OPTIONS]`

**Arguments:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `SYMBOL` | str | yes | Ticker symbol |
| `QTY` | int | yes | Number of shares |

**Options:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--strategy` | str | (required) | Strategy name |
| `--type` | str | `market` | Order type (market, limit, stop, stop_limit, trailing_stop) |
| `--limit-price` | float | None | Limit price |
| `--stop-price` | float | None | Stop price |
| `--trail-percent` | float | None | Trailing stop percent |
| `--actor` | str | `cli:manual` | Actor identifier for audit trail |

**Example:**

```bash
nexus order buy AAPL 10 --strategy momentum --type limit --limit-price 175.00
nexus --json order buy TSLA 5 --strategy growth
```

---

### `nexus order sell`

Place a sell order.

**Synopsis:** `nexus order sell SYMBOL QTY [OPTIONS]`

**Arguments:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `SYMBOL` | str | yes | Ticker symbol |
| `QTY` | int | yes | Number of shares |

**Options:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--strategy` | str | (required) | Strategy name |
| `--type` | str | `market` | Order type |
| `--limit-price` | float | None | Limit price |
| `--stop-price` | float | None | Stop price |
| `--trail-percent` | float | None | Trailing stop percent |
| `--actor` | str | `cli:manual` | Actor identifier for audit trail |

**Example:**

```bash
nexus order sell AAPL 10 --strategy momentum
nexus order sell TSLA 5 --strategy growth --type limit --limit-price 250.00
```

---

### `nexus order close`

Close entire position in a symbol (sell all available shares). Submits a market sell order for all unreserved shares.

**Synopsis:** `nexus order close SYMBOL [OPTIONS]`

**Arguments:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `SYMBOL` | str | yes | Ticker symbol |

**Options:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--strategy` | str | (required) | Strategy name |
| `--actor` | str | `cli:manual` | Actor identifier for audit trail |

**Example:**

```bash
nexus order close AAPL --strategy momentum
```

---

### `nexus order cancel`

Cancel an open order. Only orders with status `submitted` or `partially_filled` can be cancelled.

**Synopsis:** `nexus order cancel ORDER_ID`

**Arguments:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `ORDER_ID` | int | yes | Order ID to cancel |

**Example:**

```bash
nexus order cancel 42
```

---

### `nexus order replace`

Replace (modify) an existing order at the broker. Updates quantity, prices, or time-in-force on a live order.

**Synopsis:** `nexus order replace ORDER_ID [OPTIONS]`

**Arguments:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `ORDER_ID` | int | yes | Nexus order ID |

**Options:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--qty` | int | None | New quantity |
| `--limit-price` | float | None | New limit price |
| `--stop-price` | float | None | New stop price |
| `--trail` | float | None | New trail value |
| `--time-in-force`, `-t` | str | None | New time in force |

**Example:**

```bash
nexus order replace 42 --qty 20 --limit-price 180.00
nexus order replace 42 --time-in-force gtc
```

---

### `nexus order status`

Show order status and all stored fields for a single order.

**Synopsis:** `nexus order status ORDER_ID`

**Arguments:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `ORDER_ID` | int | yes | Order ID |

**Example:**

```bash
nexus order status 42
nexus --json order status 42
```

---

### `nexus order list`

List orders with optional filters.

**Synopsis:** `nexus order list [OPTIONS]`

**Options:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--strategy` | str | None | Filter by strategy name |
| `--status` | str | None | Filter by order status |
| `--symbol` | str | None | Filter by ticker symbol |

**Example:**

```bash
nexus order list
nexus order list --strategy momentum --status submitted
nexus --json order list --symbol AAPL
```

---

## Strategy Commands

Create and manage trading strategies. Each strategy is attached to a broker account and maintains its own cash balance and positions.

### `nexus strategy create`

Create a new strategy with initial balance.

**Synopsis:** `nexus strategy create NAME [OPTIONS]`

**Arguments:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `NAME` | str | yes | Strategy name |

**Options:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--broker` | str | (required) | Broker profile name |
| `--balance` | float | (required) | Initial cash balance |

**Example:**

```bash
nexus strategy create momentum --broker paper --balance 10000.00
```

---

### `nexus strategy list`

List all strategies.

**Synopsis:** `nexus strategy list`

**Example:**

```bash
nexus strategy list
nexus --json strategy list
```

---

### `nexus strategy show`

Show details for a strategy including positions and open orders.

**Synopsis:** `nexus strategy show NAME`

**Arguments:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `NAME` | str | yes | Strategy name |

**Example:**

```bash
nexus strategy show momentum
nexus --json strategy show momentum
```

---

### `nexus strategy deposit`

Deposit cash into a strategy.

**Synopsis:** `nexus strategy deposit NAME AMOUNT [OPTIONS]`

**Arguments:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `NAME` | str | yes | Strategy name |
| `AMOUNT` | float | yes | Amount to deposit |

**Options:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--note` | str | None | Optional note |

**Example:**

```bash
nexus strategy deposit momentum 5000.00 --note "Monthly funding"
```

---

### `nexus strategy withdraw`

Withdraw cash from a strategy. Fails if the strategy has insufficient balance.

**Synopsis:** `nexus strategy withdraw NAME AMOUNT [OPTIONS]`

**Arguments:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `NAME` | str | yes | Strategy name |
| `AMOUNT` | float | yes | Amount to withdraw |

**Options:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--note` | str | None | Optional note |

**Example:**

```bash
nexus strategy withdraw momentum 2000.00
```

---

### `nexus strategy set-broker`

Change the broker account for a strategy. Fails if the strategy has open orders.

**Synopsis:** `nexus strategy set-broker NAME [OPTIONS]`

**Arguments:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `NAME` | str | yes | Strategy name |

**Options:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--broker` | str | (required) | New broker profile name |

**Example:**

```bash
nexus strategy set-broker momentum --broker live
```

---

### `nexus strategy delete`

Delete a strategy and all its history. Fails if the strategy has open orders or positions unless `--liquidate` is passed.

**Synopsis:** `nexus strategy delete NAME [OPTIONS]`

**Arguments:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `NAME` | str | yes | Strategy name |

**Options:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--liquidate` | bool | `false` | Cancel open orders and market-sell all positions before deleting |
| `--yes`, `-y` | bool | `false` | Skip confirmation prompt |

**Example:**

```bash
nexus strategy delete old_strat --yes
nexus strategy delete bad_strat --liquidate --yes
```

---

## Broker Commands

Register and manage Alpaca CLI broker profiles. Nexus delegates authentication to the Alpaca CLI; no API keys are stored by Nexus.

### `nexus broker add`

Register an Alpaca CLI profile as a broker account.

**Synopsis:** `nexus broker add PROFILE_NAME [OPTIONS]`

**Arguments:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `PROFILE_NAME` | str | yes | Alpaca CLI profile name |

**Options:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--margin-multiplier` | float | `2.0` | Margin multiplier |

**Example:**

```bash
nexus broker add paper
nexus broker add live --margin-multiplier 4.0
```

---

### `nexus broker list`

List all registered broker accounts.

**Synopsis:** `nexus broker list`

**Example:**

```bash
nexus broker list
nexus --json broker list
```

---

### `nexus broker show`

Show details for a broker account including live account data and attached strategies.

**Synopsis:** `nexus broker show PROFILE_NAME`

**Arguments:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `PROFILE_NAME` | str | yes | Broker profile name |

**Example:**

```bash
nexus broker show paper
```

---

### `nexus broker sync`

Sync cash balance from the broker. If no profile is specified, syncs all registered accounts.

**Synopsis:** `nexus broker sync [PROFILE_NAME]`

**Arguments:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `PROFILE_NAME` | str | no | Broker profile (omit for all) |

**Example:**

```bash
nexus broker sync
nexus broker sync paper
```

---

### `nexus broker remove`

Remove a registered broker account. Fails if strategies are still attached.

**Synopsis:** `nexus broker remove PROFILE_NAME`

**Arguments:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `PROFILE_NAME` | str | yes | Broker profile to remove |

**Example:**

```bash
nexus broker remove paper
```

---

## Position Commands

View open positions tracked by Nexus.

### `nexus position list`

List open positions across all strategies (or a single strategy).

**Synopsis:** `nexus position list [OPTIONS]`

**Options:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--strategy` | str | None | Filter by strategy |

**Example:**

```bash
nexus position list
nexus position list --strategy momentum
nexus --json position list
```

---

### `nexus position show`

Show details for a single position including live price if available.

**Synopsis:** `nexus position show STRATEGY SYMBOL`

**Arguments:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `STRATEGY` | str | yes | Strategy name |
| `SYMBOL` | str | yes | Ticker symbol |

**Example:**

```bash
nexus position show momentum AAPL
nexus --json position show growth TSLA
```

---

## History

View the transaction ledger (deposits, withdrawals, fills).

### `nexus history`

Show transaction history with optional filters.

**Synopsis:** `nexus history [OPTIONS]`

**Options:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--strategy` | str | None | Filter by strategy name |
| `--symbol` | str | None | Filter by symbol |
| `--since` | str | None | Show only after this date (YYYY-MM-DD) |

**Example:**

```bash
nexus history
nexus history --strategy momentum --since 2026-01-01
nexus --json history --symbol AAPL
```

---

## Config Commands

View and modify Nexus configuration. Configuration is stored in TOML format.

### `nexus config show`

Show the current configuration.

**Synopsis:** `nexus config show`

**Example:**

```bash
nexus config show
nexus --json config show
```

---

### `nexus config set`

Set a configuration value. Key must be in `section.field` format. The value is validated after writing; invalid values are reverted.

**Synopsis:** `nexus config set KEY VALUE`

**Arguments:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `KEY` | str | yes | Dotted key (e.g. `reconciler.interval_minutes`) |
| `VALUE` | str | yes | New value (auto-cast to int/float/bool/str) |

**Example:**

```bash
nexus config set reconciler.interval_minutes 10
nexus config set order.slippage_buffer_percent 0.5
```

---

### `nexus config path`

Show the config file path.

**Synopsis:** `nexus config path`

**Example:**

```bash
nexus config path
```

---

## Operations

System-level commands for reconciliation, scheduling, and diagnostics.

### `nexus reconcile`

Run the reconciliation sweep. Syncs order statuses from the broker, cleans orphaned records, and detects balance drift or bypass orders.

**Synopsis:** `nexus reconcile [OPTIONS]`

**Options:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--dry-run` | bool | `false` | Report without making changes |
| `--strategy`, `-s` | str | None | Reconcile only this strategy |

**Example:**

```bash
nexus reconcile
nexus reconcile --dry-run
nexus reconcile --strategy momentum
```

---

### `nexus install`

Install the reconciler cron schedule. Creates a system cron job that runs `nexus reconcile` at the configured interval.

**Synopsis:** `nexus install [OPTIONS]`

**Options:**

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--interval` | int | (from config) | Interval in minutes |

**Example:**

```bash
nexus install
nexus install --interval 5
```

---

### `nexus uninstall`

Remove the reconciler cron schedule.

**Synopsis:** `nexus uninstall`

**Example:**

```bash
nexus uninstall
```

---

### `nexus status`

Show reconciler cron schedule status.

**Synopsis:** `nexus status`

**Example:**

```bash
nexus status
nexus --json status
```

---

### `nexus doctor`

Run health checks on the Nexus system. Verifies database integrity, broker connectivity, and configuration validity.

**Synopsis:** `nexus doctor`

**Example:**

```bash
nexus doctor
nexus --json doctor
```
