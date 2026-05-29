# Nexus CLI — AI Agent Integration Guide

## Overview

Nexus is a portfolio management layer between you and the Alpaca brokerage. Use Nexus instead of calling Alpaca directly because it provides:

- **Strategy isolation** — your strategy has its own virtual balance, preventing interference with other strategies
- **Reservation accounting** — buy orders reserve cash, sell orders reserve shares, preventing double-spending
- **Audit trail** — every order is attributed to an actor, enabling traceability across agents
- **Eager sync** — state is always fresh; Nexus syncs outstanding orders before each command

You interact with Nexus exclusively through its CLI with JSON output.

---

## Getting Started

Assumptions before you begin:

1. `nexus` is installed and on PATH
2. The broker (Alpaca) is configured and authenticated via `alpaca` CLI profiles
3. Your strategy exists and has a funded cash balance

Verify readiness:

```bash
nexus --json doctor
# Expected: {"checks": [...], "all_passed": true}
```

If `all_passed` is false, escalate — do not attempt to trade.

---

## JSON Mode

Always pass `--json` as the **first flag**, before the command name:

```bash
nexus --json <command> [subcommand] [args] [options]
```

Wrong:
```bash
nexus order buy AAPL 10 --json  # WRONG — flag must come first
```

Right:
```bash
nexus --json order buy AAPL 10 --strategy my_strat
```

### Output Structure

| Pattern | Shape |
|---------|-------|
| Confirmation (buy, sell, close) | `{"status": "ok", "message": "...", ...extra_fields}` |
| List (orders, positions) | `{"items": [...]}` |
| Single entity (order status, strategy show) | `{field: value, ...}` |
| Error | `{"error": "description"}` with exit code 1 |

---

## Command Quick Reference

| Command | Purpose | Output Shape |
|---------|---------|--------------|
| `nexus --json strategy show <name>` | Check balance, equity, and positions | Single entity |
| `nexus --json order buy <symbol> <qty> --strategy <name>` | Buy shares | Confirmation |
| `nexus --json order sell <symbol> <qty> --strategy <name>` | Sell shares | Confirmation |
| `nexus --json order close <symbol> --strategy <name>` | Close entire position | Confirmation |
| `nexus --json order status <id>` | Check one order | Single entity |
| `nexus --json order list --strategy <name>` | List orders | List |
| `nexus --json order list --strategy <name> --status submitted` | List open orders | List |
| `nexus --json order list --strategy <name> --type stop --side sell` | List stop-sell orders | List |
| `nexus --json position list --strategy <name>` | List positions with avg_entry_price | List |
| `nexus --json position show <strategy> <symbol>` | Show one position | Single entity |
| `nexus --json strategy delete <name> --yes` | Delete a strategy | Confirmation |
| `nexus --json strategy delete <name> --liquidate --yes` | Liquidate and delete | Confirmation |
| `nexus --json reconcile --strategy <name>` | Force sync with broker | Reconcile result |

---

## Workflow: Opening a Position

```bash
# 1. Check available balance and total equity
nexus --json strategy show my_strat
# Look at: cash_balance, total_equity, positions_market_value, prices_are_live
# total_equity = cash_balance + positions_market_value (live price when available)
# Buying power = (cash_balance - active_reservations) x margin_multiplier

# 2. Place buy order
nexus --json order buy AAPL 10 --strategy my_strat --actor "agent:my-agent"
# Returns:
# {"status": "ok", "order_id": 5, "client_order_id": "nx-mystrat-AAPL-a3f7c2e1"}

# 3. Check order status (if needed)
nexus --json order status 5
# Returns full order details including status field (e.g. "filled", "pending", "canceled")
```

Do not proceed to place another order for the same symbol until the first one resolves.

---

## Workflow: Closing a Position

```bash
# 1. Check available shares
nexus --json position show my_strat AAPL
# Look at: qty (total shares), available_qty (unreserved shares)

# 2. Close position (sells all available shares)
nexus --json order close AAPL --strategy my_strat --actor "agent:my-agent"
# Returns:
# {"status": "ok", "message": "Close order placed for AAPL", ...}
```

Use `order close` rather than manually issuing a sell for the full quantity. It handles partial-availability and reserved shares correctly.

---

## Workflow: Monitoring

```bash
# List open orders for your strategy
nexus --json order list --strategy my_strat --status open

# List all positions
nexus --json position list --strategy my_strat

# Check overall strategy state
nexus --json strategy show my_strat
```

You do not need to poll aggressively. Eager sync ensures that the next command you issue will reflect the latest broker state.

---

## Error Handling

**Exit codes:**
- `0` — success
- `1` — error

**Error response format:**
```json
{"error": "description of what went wrong"}
```

### Common Errors and Actions

| Error Message | Cause | Action |
|---------------|-------|--------|
| `Buy blocked: insufficient buying power` | Order cost exceeds available cash | Reduce quantity or wait for pending sells to settle |
| `Sell blocked: insufficient available shares` | Shares are reserved by another pending sell | Wait for pending sells to fill or cancel, then retry |
| `Broker error: Alpaca CLI not found` | Infrastructure issue | Escalate; do not retry |
| `Error: Order not found` | Invalid order ID | Verify the ID with `order list` |
| `Error: Strategy not found` | Wrong strategy name | Check with `strategy list` |

### Retry Policy

- Never retry immediately on broker errors — rate limits apply
- For transient broker errors, wait at least 30 seconds before retrying
- For validation errors (insufficient funds, insufficient shares), do not retry without changing inputs
- For infrastructure errors (CLI not found), escalate immediately

---

## Order Types and Options

| Type | Flag | Behavior |
|------|------|----------|
| market | *(default)* | Executes immediately at current market price |
| limit | `--limit-price 150.00` | Fills only at this price or better |
| stop | `--stop-price 140.00` | Becomes market order when stop price is hit |
| stop_limit | `--stop-price 140.00 --limit-price 139.50` | Becomes limit order when stop price is hit |
| trailing_stop | `--trail-percent 2.0` | Stop price trails market by percentage |

### Time in Force

Use `--time-in-force` on `order buy` and `order sell` to control order duration:

| Value | Behavior |
|-------|----------|
| `day` | Expires at market close (Alpaca default when omitted) |
| `gtc` | Good-till-cancelled — survives overnight and weekends |
| `ioc` | Immediate-or-cancel |
| `fok` | Fill-or-kill |

**Stop-loss orders should use `--time-in-force gtc`** to remain active overnight. Day-only stops expire at 4 PM, creating a gap in protection.

Example — limit buy:
```bash
nexus --json order buy AAPL 10 --strategy my_strat --limit-price 150.00 --actor "agent:my-agent"
```

Example — trailing stop sell:
```bash
nexus --json order sell AAPL 10 --strategy my_strat --trail-percent 2.0 --actor "agent:my-agent"
```

Example — GTC stop-loss:
```bash
nexus --json order sell AAPL 10 --strategy my_strat --type stop --stop-price 140.00 --time-in-force gtc --actor "agent:my-agent"
```

### Filtering Orders by Type

Use `--type` and `--side` on `order list` to query specific order categories without client-side filtering:

```bash
# Audit all active stop-loss orders
nexus --json order list --strategy my_strat --type stop --side sell

# Check for GTC orders
nexus --json order list --strategy my_strat --status submitted
```

---

## Best Practices

1. **Always pass `--actor "agent:<your-name>"`** — this is how your orders are identified in audit logs and history.

2. **Check strategy balance before placing orders** — call `strategy show` and verify buying power covers the intended order. Use `total_equity` (not `cash_balance` alone) for accurate portfolio value when positions are held. Check `prices_are_live` to know whether the equity figure is based on live or cost-basis prices.

3. **Use `order list --status submitted` to track pending orders** — do not assume immediate fills for limit/stop orders.

4. **Do not poll in tight loops** — eager sync handles state freshness on each command invocation. If you need to check status, a single call is sufficient.

5. **Use `order close` to exit positions** — it handles reserved shares and partial quantities correctly. Do not manually calculate sell quantity.

6. **One order at a time per symbol per strategy** — the system guards against duplicate positions. Wait for the current order to resolve before placing another for the same symbol.

7. **Parse JSON output programmatically** — always check for the `error` key first. If present, the operation failed regardless of other fields.

8. **Respect market hours** — market orders placed outside trading hours may be queued or rejected depending on broker configuration.

9. **Use `--time-in-force gtc` for stop-loss orders** — day-only stops expire at market close. GTC stops remain active overnight and over weekends, providing continuous protection.

10. **Use `order list --type stop --side sell` to audit stop-losses** — server-side filtering is cheaper than fetching all orders and filtering in Python. The `time_in_force` field in each item confirms whether the stop is GTC.

11. **Run `nexus reconcile` to populate `avg_entry_price`** — positions entered before Nexus started tracking them will have `null` for `avg_entry_price` until the first reconcile sweep bootstraps it from the broker.
