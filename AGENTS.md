# nexus — Agent Instructions

## What this project is

Pollux Nexus is an execution gateway and strategy tracking layer for broker APIs.
It acts as the sole execution path between trading strategies and Alpaca paper accounts,
maintaining per-strategy virtual balances and positions.

## How to work in this repo

- Follow src-layout conventions (source in `src/nexus/`, tests in `tests/`)
- Run `pytest` before committing
- CLI commands go in `src/nexus/cli/` as Typer sub-apps
- All Alpaca interaction goes through `src/nexus/broker/alpaca.py` — nowhere else
- Domain logic lives in dedicated modules (guards.py, ledger.py, reconciler.py)
- Every order state change must write a JSONL audit entry

## Testing

```bash
pytest
```

Tests mock the Alpaca CLI subprocess calls — never hit real APIs in tests.
