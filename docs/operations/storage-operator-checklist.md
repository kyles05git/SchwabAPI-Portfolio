# Recurring shared-storage operator checklist

A short, repeatable list. The full procedures live in
[`storage-backup-restore.md`](storage-backup-restore.md); this is what you actually run
on a schedule so the runbook is never first used during an incident.

None of these steps place, replace, cancel, approve, or reconcile an order, and none
enable live trading. Every command here is read-only except `storage backup --execute`,
which only writes to a destination directory you name.

Keep the results in an operational log outside this repository. Never record a database
URL, host, username, password, API key, Neon project ID, or account identifier.

---

## Before each official session (scheduler machine)

```powershell
schwab-trader storage health --require-writer
```

- [ ] Exit code is **0**. Anything else: read `next_action` and resolve before the
      session runs. A stale lease or an incomplete previous run will show up here.
- [ ] `Role` reads `runtime-writer`. If it reads `read-only`, this machine has the
      dashboard role and cannot run a session.
- [ ] `Latest official run` is `completed` (or a deliberately accepted outcome).

## Before each official session (every other machine)

```powershell
schwab-trader storage health
```

- [ ] `Role` reads `read-only`. A second `runtime-writer` is a single-writer violation.

---

## Weekly

| # | Check | Command | Pass condition |
|---|---|---|---|
| 1 | Backup completed and readable | `storage backup-verify --manifest <latest>` | Exit 0; `Result: passed` |
| 2 | Artifact hash still matches | same command | No `SHA-256 mismatch` and no `size mismatch` |
| 2a | The check that ran was the strong one | same command, or the manifest's `verification_level` | `sqlite-full-read` or `postgresql-archive-full-read`. A `postgresql-archive-toc` result means only the catalogue was read — prioritise that artifact for the next rehearsal |
| 3 | Backup is recent enough | inspect `created_at` in the manifest | Within your agreed interval |
| 4 | Schema has not drifted | `storage health --json` | `missing_tables` and `missing_constraints` are both empty; `alembic_current` equals `alembic_expected_head` |
| 5 | No stale official-session lease | `storage health --json` | `leases.stale` is `0` |
| 6 | No incomplete cohort run | `storage health --json` | Latest run is not `partial`, `failed`, `missed`, `running`, or `pending` |
| 7 | Every incomplete run is explained | `storage recover --cohort <id> --scheduled-for <date>` | The recommended action has been carried out or deliberately declined and logged |

- [ ] All seven pass, or each failure has a recorded decision.

---

## Monthly

- [ ] **Restore rehearsal.** Run [`storage-backup-restore.md` §6](storage-backup-restore.md)
      end to end against a scratch database or provider branch. Record the outcome per
      §6.9. *This is the item that actually proves the backups work — do not skip it
      because the weekly checks were green.* Every manifest says `restore_tested: false`,
      and it stays true until you do this; no automated check can change it.
- [ ] **Restore-window review.** Open the provider console, read the **currently
      configured** history/retention window, and record the value and the date you
      verified it. Never assume a previously recorded number is still correct.
- [ ] **Runtime role permissions.** Confirm the scheduler role can write the runtime
      tables and nothing more, and that the dashboard role is `CONNECT` + `USAGE` +
      `SELECT` only. `storage health --require-writer` catches a demoted scheduler; it
      does not catch an over-privileged one, so review the grants directly.
- [ ] **Single-writer configuration.** Confirm exactly one machine has the scheduler
      role and the scheduled task. Confirm no extra writable endpoint or branch was
      created since last month.
- [ ] **Last successful rehearsal is documented.** The log names the date, artifact,
      SHA-256, and result. If you cannot find it, the rehearsal did not happen.

---

## Quarterly

- [ ] Rehearse a restore of an **older** artifact — one whose `alembic_revision` is
      behind the current head — so the migrate-then-verify path in §6.4 is exercised,
      not just the happy path.
- [ ] Confirm `pg_dump` and `pg_restore` on the backup machine still match the server's
      major version. A server upgrade silently breaks the dump format otherwise.
- [ ] Re-read [`storage-backup-restore.md` §7](storage-backup-restore.md) — the list of
      things recovery must never do — before you need it.
- [ ] Review retained artifacts. Nothing in this repository deletes a backup; pruning is
      a deliberate human decision, and it is made with the rehearsal log in hand.

---

## After any of these events

Run the monthly list immediately, not at the next scheduled date:

- [ ] A schema migration was applied.
- [ ] The storage backend, service plan, or region changed.
- [ ] A machine was added, removed, or had its database role changed.
- [ ] An official session ended `partial`, `failed`, or `missed`.
- [ ] A stale lease was observed.
- [ ] Any recovery action was taken against the active database.
- [ ] The backup tooling or its client-tool versions changed.

---

## Escalate instead of improvising

Stop and escalate if an artifact fails verification, a restored revision is not in this
checkout's migration chain, table counts differ in a way the artifact's age does not
explain, a known cohort session classifies differently on a restore than on the source,
or you find yourself reaching for `DELETE`, `TRUNCATE`, or `DROP` to make evidence line
up. See [`storage-backup-restore.md` §6.10](storage-backup-restore.md).
