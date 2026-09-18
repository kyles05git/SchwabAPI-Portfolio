# SQLite to PostgreSQL migration runbook

This runbook is for Issue #52 after its pull request has been reviewed and merged.
Do not execute the authoritative migration from a feature branch. None of these
commands place, replace, cancel, approve, or reconcile an order.

The source SQLite files are never modified or deleted. The first PostgreSQL cutover
is reversible by clearing `SCHWAB_DATABASE_URL` and restarting against those
untouched files. PostgreSQL-only writes made after cutover are not copied back to
SQLite, so investigate before rolling back after a paper session.

**This runbook covers the one-time cutover only.** Ongoing operation is documented
separately, and the two should not be confused:

- [`docs/operations/storage-backup-restore.md`](../operations/storage-backup-restore.md)
  â€” routine backup/export, the restore rehearsal, and the Neon recovery layer.
- [`docs/operations/storage-operator-checklist.md`](../operations/storage-operator-checklist.md)
  â€” the recurring checks that keep the above honest.

In particular, the snapshots in `data/migration-backups/` are **cutover** artifacts,
not an ongoing backup: they capture the pre-cutover SQLite sources, not the PostgreSQL
state written since. Use `schwab-trader storage backup` for that.

## 1. Provision a standard PostgreSQL destination

Use a dedicated, empty database on a supported PostgreSQL service. PostgreSQL 15 or
newer is recommended. The service must provide:

- encrypted connections; use certificate verification where the service supports it;
- durable storage, automated backups, point-in-time recovery, and monitoring;
- UTC as the operational timezone;
- a migration owner allowed to create schema objects;
- a runtime scheduler role allowed to select, insert, update, delete, and use
  sequences, but not drop or truncate schema objects;
- a dashboard role with `CONNECT`, schema `USAGE`, and `SELECT` only.

Do not reuse a database containing unexplained application rows. Do not grant either
runtime role superuser, database-owner, role-management, replication, or bypass-RLS
privileges. Keep provider-specific connection pooling outside this migration; the
application uses a normal psycopg URL and does not assume a vendor.

## 2. Configure the URL without exposing it

In the reviewed checkout, add this assignment to the ignored local `.env` using a
local editor or approved secret-manager injection:

```text
SCHWAB_DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST/DATABASE?sslmode=verify-full
```

For a remote server, `sslmode=require`, `verify-ca`, or `verify-full` is mandatory;
prefer `verify-full`. Do not paste the URL into chat, shell history, a command-line
argument, logs, an Issue, or the PR. `.env.example` intentionally contains only an
empty placeholder. On Windows, restrict the local `.env` ACL to the current user and
administrators according to the machine's security policy.

Confirm only the redacted backend selector:

```powershell
schwab-trader config show
```

It must show `application_storage` as `PostgreSQL/shared`; it never prints the URL.

## 3. Update to the reviewed revision

```powershell
git switch main
git pull --ff-only origin main
python -m pip install -e .
git status --short
```

`git status --short` must be empty. Record the reviewed commit:

```powershell
git rev-parse HEAD
```

The migration command independently refuses a dirty checkout and records this
revision in `migration_runs`.

## 4. Stop every writer

Stop Task Scheduler jobs, services, terminals, and other machines that can run:

- `schwab-trader sleeve run`, `sleeve watch`, or an official cohort scheduler;
- standalone paper agents;
- research, price-panel, intraday, EDGAR, promotion, or usage writers.

Dashboard/read-only processes may be stopped as well for a simpler cutover. Confirm
there are no active paper/cohort processes. Do not use `--writers-stopped` until this
has actually been checked.

## 5. Create the destination schema

Run Alembic with the migration-owner role:

```powershell
alembic upgrade head
```

Then switch `SCHWAB_DATABASE_URL` to the restricted scheduler/runtime role for the
data copy. The URL remains local and must not be echoed.

### Applying a later revision to an existing database

`alembic upgrade head` is also how an **already-migrated** database picks up a table
added after its last upgrade. Run it with the migration-owner role, and read the output:

```powershell
alembic upgrade head          # expect a "Running upgrade ..." line per applied revision
alembic current               # confirm the recorded revision moved
```

**Silence is a finding, not success.** No `Running upgrade` line means nothing was
applied â€” either the database is genuinely at head, or a model was added without a
revision. Confirm which:

```powershell
schwab-trader storage health
python -m schwab_trader cohort readiness
```

`storage health` answers this directly: it compares the *applied* revision against
this checkout's expected head and lists any missing table by name, so a database that
`alembic upgrade head` reported success on while applying nothing is visible
immediately rather than at the next official session.

Every new model needs its own explicit revision. The baseline revision `20260723_0001`
is pinned to the twenty-four tables that existed when it was written and must not be
extended; `tests/test_migration_model_parity.py` fails if the models and the migration
chain ever diverge.

See issue #68 for the incident that established this: `cohort_alerts` shipped without a
revision, every fresh database and all of CI passed because schema is built from the
models there, and the shared database silently never received the table while
`alembic upgrade head` reported success.

## 6. Inventory and dry-run

The inventory command opens only the allow-listed SQLite databases and cohort JSON
manifests. It reports schemas, versions, counts, hashes, and checksums, never record
values.

```powershell
schwab-trader storage inventory
schwab-trader storage inventory --json
schwab-trader storage migrate --dry-run
```

Review the source list and row totals against a fresh inventory saved privately for
your own environment. This public repository does not contain an operator inventory
or database snapshot. Every difference must be understood. In particular:

- all expected paper/evaluation databases are present;
- `runs.sqlite3` and cohort manifests are included if they now exist;
- optional usage history is included only when nonempty;
- no unsupported table makes a required source ineligible;
- the destination reports either an empty source scope or a recognized resumable
  attempt.

Do not proceed on an unexplained source, count, schema, or destination conflict.

## 7. Execute the immutable snapshot migration

The command repeats inventory, creates a local backup with SQLite's backup API,
hashes every copy, then imports those copies in dependency order. The backup root
defaults to the ignored `data/migration-backups/`.

```powershell
schwab-trader storage migrate --execute --writers-stopped
```

The operation is resumable. If interrupted, leave the source SQLite files and the
migration-backup directory untouched, fix the external cause, rerun the dry-run, and
then rerun the same execute command. Identical source hashes resume; conflicts fail
closed. Never truncate destination tables to force a retry.

## 8. Run full verification

```powershell
schwab-trader storage verify
```

The verification-only rerun recomputes destination counts and deterministic
destination checksums for every migrated table and compares them with the
source-derived import checksums. It also checks:

- exact paper cash, P&L, positions, orders, derived fills, and unsettled cash;
- evaluation cycles, decisions, and official observation uniqueness;
- run/member sets, statuses, errors, and snapshot metadata;
- strategy, cohort, research, and promotion JSON hashes;
- price and SEC natural-key uniqueness plus exact NUMERIC/date values;
- child relationships, foreign keys, and duplicate official sessions.

Every row count must read `source/destination` with equal values, every checksum must
say `match`, and the final status must be `passed`. Save the sanitized terminal
report according to the project's secure operational-record policy; it contains no
database URL or raw records.

## 9. Read-only first

Keep all schedulers stopped. Start only the local dashboard, with brokerage/live
sections disabled:

```powershell
schwab-trader serve --no-live --host 127.0.0.1
```

Confirm legacy sleeves, scoped official cohort members, historical curves, research,
promotion results, price coverage, and EDGAR coverage. A bare duplicate name such as
`bench-spy` must be rejected as ambiguous; select it by stable sleeve ID or cohort.

## 10. Controlled paper-only smoke test

Choose one valid, reviewed official cohort session while every automated scheduler is
still stopped. Run exactly one official paper session from the designated scheduler
machine:

```powershell
schwab-trader sleeve run --official --cohort COHORT_ID --scheduled-for YYYY-MM-DD
```

This path is paper-only. It may read market data but must not call any live-order,
approval, cancellation, replacement, or reconciliation path. Confirm one durable run,
one checkpoint per expected member, and no duplicate fill or official observation.
Use the dashboard and cohort run views for this post-write health check. The full
`storage verify` command compares the migrated SQLite point-in-time state exactly;
after a legitimate PostgreSQL paper write changes mutable cash/position head state,
that historical comparison is expected to report drift.

## 11. Connect a second machine

Install the same reviewed revision. Configure its URL locally using the read-only
dashboard role first, run `schwab-trader config show`, and start:

```powershell
schwab-trader serve --no-live --host 127.0.0.1
```

Confirm it sees the same shared histories. Do not copy `.env`, OAuth tokens, account
state, approval state, tax lots, safety ledgers, logs, or the kill-switch file between
machines.

Designate exactly one machine as the official scheduler. Only that machine receives
the scheduler/runtime database role and scheduled task. Other machines remain
read-only unless a separate operational review authorizes a writer. PostgreSQL's
session lock and `(cohort_id, scheduled_for)` constraint remain the final duplicate
execution barriers, not a substitute for the single-scheduler policy.

Verify the split on each machine â€” the scheduler must report `runtime-writer` and
every other machine `read-only`:

```powershell
schwab-trader storage health --require-writer   # scheduler machine only
schwab-trader storage health                    # everywhere else
```

## 12. Rollback

Stop all PostgreSQL-backed writers. Remove or clear `SCHWAB_DATABASE_URL` in the
local runtime configuration, restart the application, and confirm
`application_storage` reports `SQLite/local`. The original SQLite sources were never
changed by migration and remain the rollback point.

Do not delete PostgreSQL data while investigating. Preserve the original SQLite
files and `data/migration-backups/` until multiple successful PostgreSQL scheduler
runs and verification reruns have been reviewed. Retain them longer if required by
the machine's backup policy.

## 13. After cutover: ongoing backup and recovery

Cutover is not the end of the storage work. Once PostgreSQL is authoritative, the
pre-cutover SQLite sources stop being a usable rollback point for anything written
since, so a routine backup and a *practised* restore become the recovery path.

Start [`docs/operations/storage-operator-checklist.md`](../operations/storage-operator-checklist.md)
immediately after the first successful scheduler runs, and perform the first restore
rehearsal from
[`docs/operations/storage-backup-restore.md`](../operations/storage-backup-restore.md)
before relying on the shared database for an official cohort.
