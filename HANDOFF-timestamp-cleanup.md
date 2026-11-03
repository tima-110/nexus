# Handoff: Normalize a malformed timestamp in `transactions` and harden Nexus against recurrence

> **Audience:** an AI agent (or engineer) with no prior context on this task.
> **Repo:** `~/projects/nexus` (the Nexus trading system).
> **Effort:** small. Mostly a one-row data repair plus a defensive health check.
> **Risk:** low, but this touches a live SQLite database holding real ledger state. Read §0 before writing anything.

---

## 0. TL;DR

A single row in the live Nexus database — `transactions.id = 20` — has a `created_at` value
in the **wrong format**:

```
2026-10-21 15:16:49          ← naive, space-separated, no timezone  (19 chars)
```

Every other timestamp in the entire database is tz-aware ISO 8601:

```
2026-10-27T15:58:43.202287+00:00   ← (32 chars)
```

This bad row is **currently crashing the Argos dashboard** (`~/projects/Argos-Dashboard`)
on its "Order Blotter & Ledger" tab, because pandas infers the datetime format from the
first row and then hard-fails on the one row that doesn't match:

```
ValueError: time data "2026-10-21 15:16:49" doesn't match format "%Y-%m-%dT%H:%M:%S.%f%z"
```

Your job:
1. **Repair row 20** so it matches the tz-aware ISO 8601 convention (§2).
2. **Make Nexus defensive** so this class of bad row is caught and reported by `nexus doctor` (§3).

The root cause of the *bad row* is already diagnosed in §1 — you do **not** need to re-derive it.
The Python code path was never capable of producing this format; the row was inserted
out-of-band via the raw `sqlite3` shell.

---

## 1. Root cause (already diagnosed — do not re-investigate)

### The convention

All Nexus code writes timestamps via a single tz-aware helper:

```python
# src/nexus/ledger.py:11
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
```

The same pattern (`datetime.now(timezone.utc).isoformat()`) appears in every module that
writes a timestamp:

| File | Line(s) |
|---|---|
| `src/nexus/ledger.py` | 11 (`_now`), used throughout |
| `src/nexus/cli/order.py` | 31 (`_now`), 198, 433, 452, 459 |
| `src/nexus/cli/broker_cmd.py` | 28, 198 |
| `src/nexus/cli/strategy.py` | 42, 433, 452, 459 |
| `src/nexus/audit.py` | 12 |
| `src/nexus/reconciler.py` | 63, 142 |
| `src/nexus/sync.py` | 93 |
| `src/nexus/doctor.py` | 132 |

`git log -S` over the **entire** repo history confirms no commit ever contained a
naive-datetime pattern (`strftime("%Y-%m-%d %H:%M:%S")`, `str(datetime.now())`, etc.),
and `_now()` in `ledger.py` has been tz-aware ISO since the file was first created.
**The Python codebase has never been able to emit the bad format.**

### The actual origin of row 20

Row 20 was inserted **out-of-band**, via the raw `sqlite3` CLI, using SQLite's
`datetime('now')` or `CURRENT_TIMESTAMP`. SQLite emits exactly a 19-char naive
`YYYY-MM-DD HH:MM:SS` string, which is a byte-for-byte match for the bad value:

```
$ sqlite3 :memory: "SELECT datetime('now'), length(datetime('now'));"
2026-10-28 01:14:33|19
```

Corroborating evidence:

- The row is `type='deposit'`, `actor='cli:manual'`, `order_id=NULL`, note
  `"Initial capital allocation above baseline"` — a manual, one-off capital top-up.
- There is **no matching event in `~/.local/share/nexus/audit.jsonl`** for this deposit.
  The audit log records every CLI-driven action (order submissions, cancels, etc.), so its
  absence means this row never went through the Python CLI.
- It is the **only** non-ISO timestamp in **any** timestamp column of **any** table
  (verified across `created_at`, `updated_at`, `filled_at`, `opened_at`,
  `last_synced_at` in every table). Counts:

  | Shape | Count |
  |---|---|
  | `...T....+00:00` (tz-aware ISO 8601) | 22 |
  | `2026-10-21 15:16:49` (naive) | 1 |

### The downstream symptom (for reference)

`~/projects/Argos-Dashboard/src/argos/components/charts.py:150`:

```python
df["created_at"] = pd.to_datetime(df["created_at"])   # crashes on the bad row
```

Argos is a **read-only** consumer of this database (`PRAGMA journal_mode=WAL`, opened
read-only). The Argos side is being hardened separately to parse mixed formats gracefully;
that is **not** your responsibility. Your job is to make the *source* clean and self-checking.

---

## 2. Task 1 — Repair row 20

### Target

`~/.local/share/nexus/nexus.db` → table `transactions`, row `id = 20`.

Current state:

```
id  strategy_id  order_id  type     amount   actor       note                                    created_at
20  5            (NULL)    deposit  15000.0  cli:manual  Initial capital allocation above baseline  2026-10-21 15:16:49
```

Desired:

```
created_at = '2026-10-21T15:16:49.000000+00:00'
```

### Timezone assumption (verify this)

The naive value is **assumed to be UTC wall-clock time** and should be re-emitted as UTC.
Justification:

- `whale_walk` (strategy 5) has an audit event at `2026-10-21T15:26:42Z` (~10 minutes later)
  and transaction 21 at `2026-10-21T15:27:32Z` — same day, same actor, minutes apart.
- All production timestamps in this system are UTC.

So treating `15:16:49` as UTC keeps it chronologically consistent with its neighbours.
**If the local machine timezone on 2026-10-21 was not UTC, this assumption is wrong** —
sanity-check against the audit log before committing. (The dashboard reads this DB, and the
host appears to run UTC, so UTC is almost certainly correct.)

### Constraints — IMPORTANT

- **Do NOT change `amount`, `type`, `actor`, `note`, `strategy_id`, or `order_id`.**
  The strategy's `cash_balance` (`whale_walk` = 24880.96) already includes this +15000
  deposit. Changing the amount would desync the ledger. You are only fixing the timestamp.
- **Back up first.** The DB is live and in WAL mode. Make a consistent copy while the
  system is quiescent (or accept a small race):

  ```bash
  cp ~/.local/share/nexus/nexus.db ~/.local/share/nexus/nexus.db.bak-$(date +%Y%m%d-%H%M%S)
  ```

- **Respect WAL mode.** If you write via the `sqlite3` CLI directly, SQLite handles WAL
  automatically. If you write via Python, use `src/nexus/db.py:get_connection()` so you get
  `PRAGMA journal_mode=WAL` and foreign-key enforcement. After writing, ensure no stray
  `-wal` / `-shm` files are left dangling from a killed process.
- **Verify, don't trust.** After the update, re-run the format audit (§4) and confirm the
  row is correct and nothing else changed:

  ```sql
  SELECT * FROM transactions WHERE id = 20;
  SELECT COUNT(*) FROM transactions WHERE created_at NOT LIKE '%+00:00' AND created_at NOT LIKE '%Z';  -- expect 0
  ```

- **Do not delete the row.** It represents a real $15,000 capital allocation that is
  reflected in `whale_walk`'s balance. Only the timestamp is wrong.

### Suggested repair (raw CLI)

```bash
sqlite3 ~/.local/share/nexus/nexus.db <<'SQL'
BEGIN;
UPDATE transactions
   SET created_at = '2026-10-21T15:16:49.000000+00:00'
 WHERE id = 20
   AND created_at = '2026-10-21 15:16:49';   -- guard: only if still the bad value
COMMIT;
SQL
```

The `AND created_at = '...'` guard makes the update a no-op if someone else already fixed it.

---

## 3. Task 2 — Make Nexus self-checking (prevent recurrence)

The deeper problem is that nothing in Nexus noticed this row was malformed. A naive
timestamp silently breaks downstream consumers (the Argos dashboard) with no signal on the
Nexus side. Add a doctor check.

### Where

`src/nexus/doctor.py` defines `DoctorCheck` and a list of checks in `run_doctor()`:

```python
def run_doctor(conn: sqlite3.Connection, config: NexusConfig) -> list[DoctorCheck]:
    checks = []
    checks.append(_check_db_valid(conn))
    checks.append(_check_alpaca_reachable(conn))
    checks.append(_check_no_orphaned_reservations(conn))
    checks.append(_check_no_stale_orders(conn))
    checks.append(_check_balance_consistent(conn))
    checks.append(_check_audit_writable(config))
    checks.append(_check_cron_installed())
    return checks
```

It is surfaced via the CLI at `src/nexus/cli/__init__.py:273` (`run_doctor`).

### What to add

A new check, e.g. `_check_timestamps_iso(conn)`, appended to `run_doctor()`:

- For each timestamp column in each table (`created_at`, `updated_at`, `filled_at`,
  `opened_at`, `last_synced_at`, …), count rows whose value is non-NULL and does **not**
  end in `+00:00` or `Z` (i.e. not tz-aware ISO 8601).
- Pass if all counts are 0; fail with a detail listing the offending `table.column` and
  their counts.

Use `pragma_table_info` to enumerate timestamp columns dynamically rather than hardcoding
table names, so new tables are covered automatically:

```sql
SELECT m.name AS tbl, p.name AS col
  FROM sqlite_master m
  JOIN pragma_table_info(m.name) p
 WHERE m.type = 'table'
   AND p.name IN ('created_at','updated_at','filled_at','opened_at','last_synced_at');
```

Follow the existing style: return a `DoctorCheck(name=..., passed=..., detail=...)`, wrap
the body in `try/except` returning `passed=False` on error (see `_check_db_valid`).

Add a unit test in `tests/test_doctor.py` (it already seeds transactions, see line 148):
one case with a clean tz-aware row (expect pass), one with a naive row (expect fail with
the table.column named in `detail`).

### Optional (nice-to-have, only if cheap)

A `nexus doctor --repair-timestamps` or a small `nexus db normalize-timestamps` subcommand
that rewrites naive values to tz-aware ISO. **Do not** auto-run repairs from `doctor` — it
should report, not mutate. If you add a repair path, gate it behind an explicit CLI flag
and print a preview of the rows it would change before writing.

---

## 4. Verification checklist

Run these after both tasks:

```bash
# 1. No malformed timestamps anywhere (expect 0)
sqlite3 "file:$HOME/.local/share/nexus/nexus.db?mode=ro" \
  "SELECT COUNT(*) FROM transactions WHERE created_at NOT LIKE '%+00:00' AND created_at NOT LIKE '%Z';"

# 2. Row 20 is repaired and otherwise untouched
sqlite3 -header -column "file:$HOME/.local/share/nexus/nexus.db?mode=ro" \
  "SELECT * FROM transactions WHERE id = 20;"

# 3. whale_walk balance unchanged (expect 24880.96)
sqlite3 "file:$HOME/.local/share/nexus/nexus.db?mode=ro" \
  "SELECT cash_balance FROM strategies WHERE id = 5;"

# 4. Doctor passes, including the new check
nexus doctor

# 5. Tests
cd ~/projects/nexus && uv run pytest -q
```

Finally, confirm the Argos dashboard's Ledger tab renders again (open the Streamlit app
and navigate to **Order Blotter & Ledger**). That tab was the original crash site.

---

## 5. Reference: relevant files

| Path | Why it matters |
|---|---|
| `src/nexus/ledger.py` | `_now()` (line 11) is the canonical timestamp writer; `record_transaction()` (line 53) inserts transaction rows. |
| `src/nexus/db.py` | `get_connection()` returns a WAL-mode connection; `init_db()` has the schema. Timestamp columns are plain `TEXT`. |
| `src/nexus/doctor.py` | Where the new defensive check goes. |
| `src/nexus/cli/__init__.py:273` | Where `run_doctor` is invoked by the `nexus doctor` CLI. |
| `src/nexus/cli/strategy.py:186` | `nexus strategy deposit` — the CLI command for deposits. It correctly uses `record_transaction` → `_now()` (tz-aware). Row 20 did **not** come from here. |
| `~/.local/share/nexus/nexus.db` | The live database to repair. |
| `~/.local/share/nexus/audit.jsonl` | Audit log; its *absence* of a deposit event on 2026-10-21 is how we know row 20 was a manual out-of-band insert. |
| `~/projects/Argos-Dashboard` | Downstream read-only consumer that was crashing. Not yours to fix, but the reason this matters. |

---

## 6. Out of scope

- Hardening the Argos dashboard parser (handled in the Argos repo).
- Changing how `_now()` formats timestamps (the current tz-aware ISO 8601 convention is
  correct and should stay).
- Any change to amounts, balances, positions, or the `whale_walk` strategy config.
- Investigating *who* ran the manual `sqlite3` insert — it was a one-off manual capital
  allocation and the audit gap is expected for an out-of-band write.
