# Pollux Nexus

Execution gateway and strategy tracking layer for Alpaca paper trading accounts.

## Overview

Nexus multiplexes 10-20 virtual strategy "accounts" across 2-3 real Alpaca paper trading accounts, tracking per-strategy P&L, cash balances, and positions independently. All orders flow through Nexus — no strategy calls the broker directly. The CLI serves both humans and AI agents (pass `--json` for structured output).

## Install

**Prerequisites:**

- Python 3.12+
- Alpaca CLI (`alpaca` binary in PATH, configured via `alpaca profile login`)

**Install with pipx (preferred):**

```bash
pipx install .
```

Using pipx creates an isolated environment, which is important on Ubuntu and other systems with restricted system Python.

**For development:**

```bash
pip install -e ".[dev]"
```

## Quick Start

```bash
# 1. Register a broker account
nexus broker add paper1

# 2. Create a strategy linked to that broker
nexus strategy create my_strat --broker paper1 --balance 0

# 3. Fund the strategy
nexus strategy deposit my_strat 10000

# 4. Place an order
nexus order buy AAPL 10 --strategy my_strat

# 5. Check order status
nexus order list --strategy my_strat
```

## Architecture

Nexus sits between strategies and the Alpaca API, enforcing isolation and providing consistent state. Key design elements:

| Concept | Description |
|---------|-------------|
| Reservation model | Buy orders reserve cash; sell orders reserve shares. Prevents double-spending across concurrent strategies. |
| Eager sync | Every CLI command syncs outstanding orders for the calling strategy before proceeding, ensuring fresh state at decision time. |
| Background reconciler | Cron-scheduled sweep catches missed fills and corrects drift. |
| Storage | SQLite WAL (`~/.local/share/nexus/nexus.db`) for operational data; JSONL (`~/.local/share/nexus/audit.jsonl`) for append-only audit trail. |
| Broker adapter | Shells out to the `alpaca` CLI subprocess rather than using the SDK directly. Proven stable; swappable later. |

## Configuration

Config file: `~/.config/nexus/config.toml` (auto-created on first run)

Manage configuration via the CLI:

```bash
nexus config show          # display current configuration
nexus config set KEY VALUE # update a setting
nexus config path          # print config file location
```

| Setting | Default | Description |
|---------|---------|-------------|
| `sync_timeout` | `30` | Seconds to wait for order sync before timeout |
| `reconcile_interval` | `300` | Seconds between background reconciliation sweeps |
| `audit_path` | `~/.local/share/nexus/audit.jsonl` | Path to audit log |
| `db_path` | `~/.local/share/nexus/nexus.db` | Path to SQLite database |

## Commands

| Group | Subcommands | Description |
|-------|-------------|-------------|
| `order` | buy, sell, close, cancel, replace, status, list, option-sell, option-buy | Place and manage equity and options orders |
| `strategy` | create, list, show, deposit, withdraw, set-broker | Manage virtual strategy accounts |
| `broker` | add, list, show, sync, remove | Register and inspect broker connections |
| `position` | list, show | View current equity and option holdings per strategy |
| `history` | (filter flags) | Query fills filtered by strategy, symbol, or date |
| `config` | show, set, path | View and modify configuration |
| `reconcile` | — | Manually trigger a reconciliation sweep |
| `install` | — | Install background reconciler (cron/systemd) |
| `uninstall` | — | Remove background reconciler |
| `status` | — | Show system health and sync state |
| `doctor` | — | Diagnose common setup issues |

See [docs/cli-reference.md](docs/cli-reference.md) for full command details.

Nexus supports both equity and option orders. Option symbols use OCC
format (e.g., `NKE260718P00040000`):
- `order option-sell` — sell cash-secured puts or covered calls
- `order option-buy` — buy options to close shorts or open longs
- `position list` — shows both equity and option positions
- `position show` — OCC-aware with premium, strike, expiry, P&L

See [Options Implementation Plan](docs/superpowers/plans/2026-06-24-options-implementation.md) for details.

## For AI Agents

Nexus is designed for agent consumption. Pass `--json` to any command for structured output suitable for programmatic parsing. All commands return consistent JSON schemas with explicit error codes.

See [Agent Integration Guide](docs/agent-integration.md) for schemas, workflows, and best practices.

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Verify CLI
nexus --help
```

**Project structure:**

```
src/nexus/
  cli/          # Typer command groups
  core/         # Domain logic (orders, strategies, positions)
  broker/       # Alpaca CLI adapter
  db/           # SQLite repository layer
  config/       # TOML config loading + Pydantic models
docs/           # Specification and reference docs
tests/          # pytest suite
```
