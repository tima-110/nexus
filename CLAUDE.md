# nexus — Claude Code Context

## Install method

```bash
pipx install --editable .
```

After code changes: `pipx reinstall nexus`

## Dev workflow

```bash
pip install -e ".[dev]"   # or: pip install -e "." && pip install pytest
pytest                     # run tests
nexus --help               # verify CLI
```

## Key decisions

- **CLI is the primary interface**: all consumers (AI agents, scripts, humans) go through the CLI.
  Agents use `--json` for machine-readable output.
- **Alpaca CLI subprocess**: broker adapter shells out to `alpaca` CLI rather than using the SDK.
  Proven stable; swap to SDK later if needed.
- **Reservation model**: buy orders reserve cash; sell orders reserve shares. Prevents
  double-spending across concurrent strategies.
- **Eager sync**: every CLI command syncs outstanding orders for the calling strategy before
  proceeding, ensuring fresh state at decision time.
- **Secrets via Alpaca CLI profiles**: no API keys stored by Nexus. Alpaca CLI manages auth.
- **Standard exceptions**: `ValueError` for validation, `RuntimeError` for API/network errors.
  No custom hierarchy until module count warrants it.

## Architecture

See `docs/superpowers/specs/2026-05-24-nexus-design.md` for full spec.
