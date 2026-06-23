# Lessons Learned

### 2026-06-21: Cancel-flow needs broker confirmation, not local-only mark
**Context:** Bug investigation of ghost orders — Nexus marked orders `cancelled`
in SQLite but Alpaca's broker-side cancel silently failed. Deadlock for weeks.
**Insight:** Marking terminal order state in local DB *before* verifying broker
confirmation is a silent data-corruption trap. The reservation/release flow
assumes `cancelled` means "broker no longer holding shares/cash" — if that's
not true, double-spending results. Required state machine: `cancel_pending`
(attempted, awaiting confirmation) and `cancel_failed` (exhausted retries,
needs manual intervention), with the reconciler driving the transition.
**Apply when:** Designing any "act on remote system, then record locally"
flow — particularly anything that releases a reservation on a remote system.
Don't conflate "we sent the cancel" with "the cancel happened."
**Global?** No — specific to execution gateway with broker reservation model.

### 2026-06-21: `from nexus.X import Y` in a function body bypasses module-level patches
**Context:** Tests were patching `nexus.cli.strategy.AlpacaBroker` but a local
import inside the function (`from nexus.broker import AlpacaBroker`) re-resolved
the name, bypassing the patch.
**Insight:** When a module does `from x import y` at module level, patching
`module.y` works because Python looks up `y` in the module's namespace. But
when the same import is *inside* a function body, every call re-executes the
import, which always re-binds from the original source — patches on
`module.y` are ignored. Patch the *source* of the import (e.g.
`nexus.broker.AlpacaBroker`) instead.
**Apply when:** Writing tests that mock a symbol imported via local
(function-scoped) `from x import y` statements.
**Global?** Yes — Python import semantics, applies anywhere.
