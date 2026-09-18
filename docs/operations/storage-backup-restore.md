# Shared-storage backup and restore-rehearsal runbook

This runbook covers backing up the application's shared storage and *rehearsing* a
restore. It is provider-neutral: everything in sections 1–7 applies equally to shared
SQLite and to any standard PostgreSQL service. Section 8 adds a Neon-specific layer.

None of these commands place, replace, cancel, approve, or reconcile an order. None of
them enable live trading. `storage health` and `storage recover` are strictly
read-only; `storage backup` writes only to a destination directory you name explicitly.

**A backup you have never restored is not a backup.** The whole point of this document
is the rehearsal in section 6.

---

## 0. The two recovery layers, and why you need both

| Layer | What it is | What it protects against | What it does not do |
|---|---|---|---|
| **Provider recovery** | The database service's own point-in-time restore, snapshots, or history branches | Host loss, accidental `DELETE`/`UPDATE`, a bad migration, corruption inside the service's retention window | Nothing outside that window. Nothing if the project, account, or plan goes away. Nothing you can read without the provider. |
| **Portable export** | The artifact `storage backup --execute` writes, plus its manifest | Provider outage, account loss, plan change, expired retention, and "I need to inspect last month's data on a laptop" | It is a point-in-time copy. Everything written after it is not in it. |

These are complements, not alternatives. Treat neither as a substitute for the other,
and treat neither as a substitute for actually testing a restore.

---

## 1. Check health before you back anything up

```powershell
schwab-trader storage health
schwab-trader storage health --json
```

Exit codes:

| Code | Meaning | What to do |
|---|---|---|
| 0 | healthy | Proceed. |
| 1 | unhealthy — schema drift, a missing constraint, a wrong role, a stale lease, or an incomplete run | Read `next_action`. Resolve before backing up, or you will faithfully preserve a broken state. |
| 2 | disconnected | The service is unreachable or the local URL is wrong. Do not proceed. |
| 3 | indeterminate | A capability could not be determined, or no shared database is configured. **Fail closed** — do not assume health. |

The command never prints the database URL, host, user, password, account identifier,
or any raw row. The human table and `--json` document are two renderings of one report,
so they always agree; use `--json` for scripting and pin `contract_version`.

On the designated scheduler machine, add `--require-writer` so a machine that has
silently been given the read-only dashboard role fails rather than looking fine.

> **Indeterminate is not a soft pass.** A partial write grant — a role that can write
> `cohort_runs` but not `official_daily_observations` — reports `unknown` rather than
> `runtime-writer`, because it would fail halfway through a session.

---

## 2. Preflight the backup

Dry run is the default. It writes nothing and tells you exactly what it *would* create.

```powershell
schwab-trader storage backup --destination D:\schwab-backups
```

Review:

- the backend and destination are the ones you meant;
- `Would create` names a new timestamped artifact;
- `Schema revision` matches what `storage health` reported;
- for PostgreSQL, `Client / server major` shows matching versions;
- `Ready` is `yes` and there are no blockers.

Destinations are validated, not interpreted. These are all rejected:

- a relative path, or one containing `..`;
- a path still containing `$VAR` or `%VAR%` (your shell did not substitute it);
- a filesystem or drive root;
- your home directory root, or any ancestor of it;
- **the repository working tree — the root, or anything inside it — or any ancestor
  of the root.** An artifact is a complete copy of the shared database; one written
  into the checkout enters version-control history the moment anyone runs
  `git add -A`. `.gitignore` is not relied on, so the entire subtree is refused;
- a directory that does not exist — create it deliberately first;
- a path that is a file.

Containment is judged on the **canonically resolved** path, so a destination that
reaches the checkout through `..`, a symlink, or a Windows junction is rejected on
where it actually lands. On Windows the comparison ignores case and separator style.

Pick somewhere clearly outside the checkout — a dedicated backup volume or directory.

For PostgreSQL, the preflight also requires `pg_dump` **and** `pg_restore` on `PATH`,
and checks that they report the same major version as each other and are not older
than the server. `pg_restore` is not optional: it is how the artifact is read back,
and an unchecked artifact is treated as a failed backup.

---

## 3. Produce the artifact

```powershell
schwab-trader storage backup --destination D:\schwab-backups --execute
```

What happens, by backend:

- **SQLite** — the online backup API copies the database from a read-only source
  connection. This is not a file copy: copying an actively written SQLite file yields a
  torn page image that can still pass a naive check while losing the WAL tail. The
  artifact is then checked with `PRAGMA integrity_check`, `PRAGMA foreign_key_check`,
  and a per-table row-count comparison against the source.
- **PostgreSQL** — `pg_dump --format=custom`, then checked in two stages:
  `pg_restore --list` parses the archive catalogue, and `pg_restore --file=<scratch>`
  converts the whole archive to a SQL script in a temporary directory that is deleted
  immediately. The second stage is what forces every data block to be decompressed and
  read; neither stage connects to a server. Connection material is passed to the child
  process through libpq environment variables only. It never appears in the argument
  vector, and the tools' stderr is captured and discarded because it names the host.

  The preflight also confirms `pg_dump` and `pg_restore` report the same major version
  as each other, and that they are not older than the server. A mismatch is one of the
  most common causes of a dump failing, and it would otherwise surface only as an
  opaque exit status.

Guarantees:

- nothing is overwritten — artifacts are timestamped and a colliding name is refused;
- no old backup is ever deleted, by any command here;
- an artifact that fails its check gets **no manifest** and is renamed `*.rejected`.

### Transient free space the PostgreSQL full read needs

The full-read stage writes a converted SQL script into a temporary directory beside the
artifact and deletes it as soon as the check finishes. That script is the peak transient
requirement, and it is **not** "about the size of the artifact" — a custom-format
archive is compressed, so the ratio between the two is set by how well the data
compresses, not by anything you choose at backup time.

Measured against PostgreSQL 18.4 with real `pg_dump`/`pg_restore`:

| Source data | Database on disk | Artifact | Converted SQL | SQL ÷ artifact | SQL ÷ database |
|---|---|---|---|---|---|
| Highly compressible (500k synthetic numeric bars) | 80.3 MB | 8.5 MB | 41.9 MB | **4.94×** | 0.52× |
| Essentially incompressible (60k random hex rows) | 45.3 MB | 17.8 MB | 31.1 MB | **1.74×** | 0.69× |

The artifact multiple swings by nearly 3× between those two shapes. The fraction of the
*database* barely moves. So:

> **Size the scratch space against the source database, not against the artifact.**
> Keep free space on the destination volume of at least the size of the source
> database. Both shapes above landed at 0.5–0.7× of it, which leaves useful margin.

Sizing against the artifact is what fails: an 8.5 MB artifact from a well-compressing
database needed 41.9 MB of scratch. If the space is not there, the conversion fails and
the artifact is rejected — the rejection message names a full destination disk as a
likely cause precisely because this is how it happens. To measure your own data, run
`pg_restore --file=<scratch> <artifact>` once by hand and look at the script's size.

The directory is created inside the destination and removed unconditionally, including
after a failed conversion; a rehearsal that polled the destination throughout a real
verification saw exactly one `schwab-pg-verify-*` directory appear and none left behind.

### The artifact is not a sanitized document

Every *report* this tooling produces — terminal output, `--json`, the manifest, and
every error — is free of host, port, role, password, URL, and raw client-tool stderr.
That has been confirmed against the real tools, including against a real failed
connection whose stderr did contain the host, the port, and the database name.

The **artifact itself is a different matter.** A custom-format archive's header records
the name of the database it was dumped from, because `pg_dump` writes it there:

```powershell
pg_restore --list <artifact>
```

It does *not* record the host, port, role, or password. Treat artifacts as sensitive
regardless — each one is a complete copy of the shared database — and keep them out of
the repository, issues, and pull requests. That is already why section 2 refuses any
destination inside the working tree.

### What "verified" does and does not mean

**No command in this repository restores an artifact, so no command can prove one
restores.** Every manifest states `restore_tested: false`, and the terminal says so on
every run. Only the rehearsal in section 6 establishes restorability.

`verification: passed` means "every check that ran succeeded". Which checks ran is
`verification_level`:

| Level | What was actually read | Catches |
|---|---|---|
| `sqlite-full-read` | Every page and every row: `integrity_check`, `foreign_key_check`, and per-table counts against the source | Truncation, corruption, missing rows |
| `postgresql-archive-full-read` | Every data block decompressed and parsed by `pg_restore`, without a server | Truncation or corruption anywhere in the archive |
| `postgresql-archive-toc` | Only the archive header and table of contents | A file that is not an archive. **Not** truncation after the catalogue |

`postgresql-archive-toc` is recorded only when the full read could not be attempted —
for example when the scratch directory could not be created. The manifest's
`verification_detail` says why. Treat an artifact at that level as unproven beyond
"it is an archive", and prioritise rehearsing it.

None of these levels means the archive will load cleanly into a live database: no
server has executed a single statement from it. That is section 6's job.

#### Why the two-stage check exists, demonstrated

The gap between the two PostgreSQL levels is not theoretical. Against real
PostgreSQL 18.4 binaries and a real 8,484,646-byte archive of 31 tables:

- the archive's table of contents ends **87,719 bytes in — 1.03% of the file**;
- a copy truncated at 50% of the file still passes `pg_restore --list` with exit 0,
  listing every one of its real tables;
- the same copy, with a manifest matching its truncated bytes so the size and SHA-256
  guards could not catch it first, is **rejected** by `storage backup-verify` at the
  full-read stage, exit 1.

So an archive missing half its data is indistinguishable from a good one at the
`postgresql-archive-toc` level. That is the entire reason the full read is not optional,
and why an artifact recorded at the TOC level should be rehearsed before it is trusted.

#### What is still proven only against fakes

The PostgreSQL path was introduced with offline tests that substitute a fake
`pg_dump`/`pg_restore`. Those tests remain the regression net. Against real binaries and
a disposable database, the following are now *observed* rather than inferred: the
preflight's client and server major versions, `Ready: yes`, artifact production,
`verification_level: postgresql-archive-full-read` with `restore_tested: false`, the
post-TOC truncation rejection above, the scratch directory's creation and removal, the
absence of connection material from every report, both "not found on `PATH`" blockers,
and a full restore rehearsal.

One thing is not: **the client/server version-mismatch blockers have never been fired by
genuinely mismatched binaries.** Confirming `pg_dump` and `pg_restore` disagree, or that
the client is older than the server, needs two PostgreSQL major versions installed side
by side. The wording and the logic are covered by offline tests, and the version numbers
they act on are parsed from real `--version` output, but the end-to-end path is
unexercised. Treat a mismatch blocker as reasoned, not demonstrated.

### The manifest

Every successful backup writes `<artifact>.manifest.json` beside the artifact:

| Field | Meaning |
|---|---|
| `manifest_version` | Contract version for this document (currently `2`) |
| `created_at` | UTC creation timestamp |
| `backend` | `sqlite` or `postgresql` |
| `code_revision` | Git HEAD; a dirty checkout is recorded as `<sha>-dirty`, not refused |
| `alembic_revision` | Schema revision the source was at |
| `table_counts` | Sanitized per-table row counts |
| `artifact_name` | Filename only, never a path |
| `artifact_bytes` | Size in bytes |
| `sha256` | Cryptographic hash of the artifact |
| `verification` | `passed` — the checks that ran all succeeded |
| `verification_level` | *Which* checks ran; see the table above |
| `verification_detail` | One sentence naming exactly what was read |
| `restore_tested` | Always `false` |

The manifest contains no URL, host, user, password, account identifier, or row value.

> **Manifest v1 → v2.** v1 recorded only `verification: passed`, which a reader could
> reasonably take to mean the artifact was known to restore. It never meant that.
> `verification_level` and `restore_tested` were added so it cannot be read that way.
> A v1 manifest is still parseable; its artifact was checked at what v2 calls
> `postgresql-archive-toc` or `sqlite-full-read` depending on backend.

---

## 4. Preserve and checksum the original

Before anything else touches the artifact:

1. Copy it and its manifest to your retention location.
2. Re-verify it there, independently:

   ```powershell
   schwab-trader storage backup-verify --manifest D:\schwab-backups\<artifact>.manifest.json
   ```

   This re-checks size, SHA-256, and the backend's read check, and reports the
   `verification_level` it reached. A non-zero exit means **do not restore this
   artifact** — find out why first. A zero exit still does not mean the artifact
   restores; it means nothing detectable is wrong with it.
3. Record the artifact name, hash, `verification_level`, and verification date in your
   operational log.

Treat the original artifact as immutable. Every step below works on a copy.

---

## 5. Never restore over the active database first

This is the rule that makes the difference between a recovery and a second incident.

**The first restore always goes somewhere else** — a scratch database, a separate
service, or a provider point-in-time branch. Only after the evidence in section 6
matches do you consider touching the active database, and that is a separate,
deliberately authorized decision.

Reasons, in order of how often they bite:

- The artifact may be older than you think; overwriting live data with it destroys
  everything written since.
- The artifact has never been restored. Reading an archive in full proves it is
  readable, not that a server will accept every statement in it — schema conflicts,
  extension mismatches, and role dependencies surface only on a real restore.
- The problem you are recovering from may not be the problem you think it is.
- Once the active database is overwritten, you have no second attempt.

---

## 6. The restore rehearsal

Do this monthly or quarterly, and after any change to the schema, the storage backend,
or the backup tooling. Budget 30 minutes.

### 6.1 Create a scratch target

- **SQLite** — pick an empty path in a scratch directory. Nothing else required.
- **PostgreSQL** — create an empty, dedicated scratch database (or a provider branch;
  see section 8). Never rehearse into the active database. Never rehearse into a
  database holding rows you cannot explain.

**"Empty" means empty.** `pg_restore` issues `CREATE TABLE` per relation, so a scratch
database containing even one table the artifact also defines fails on that table. Do not
work around it by dropping the colliding table — see 6.2 for why the retry needs a fresh
database instead.

If you created the scratch server yourself rather than using a managed one, check its
timezone before running 6.5. A cluster built with `initdb` inherits the machine's local
timezone, and `storage health` reports a non-UTC server `TimeZone` as a `warn`, which
makes the whole report `unhealthy` and exit 1. That is a finding about the scratch
server, not about the artifact. Set it and reload before you start:

```powershell
psql -c "ALTER SYSTEM SET timezone = 'UTC'"
psql -c "SELECT pg_reload_conf()"
```

### 6.2 Restore into the scratch target

- **SQLite** — copy the artifact to the scratch path. It is already a database.
- **PostgreSQL** — restore the custom-format dump into the scratch database with
  `pg_restore`. Supply connection details through libpq environment variables
  (`PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE`, `PGSSLMODE`), not on the
  command line, for the same reason the backup does.

  ```powershell
  pg_restore --no-owner --no-privileges --exit-on-error --dbname $env:PGDATABASE <artifact>
  ```

  Use `--exit-on-error`. Without it `pg_restore` reports errors, keeps going, and exits
  0 with a partially restored database — which would pass 6.5 and 6.6 for the wrong
  reason.

> **A failed restore leaves the scratch database dirty.** `pg_restore` applies statements
> one at a time and does not roll back what it already committed, so stopping on an
> error leaves every table created before that point in place. In one rehearsal a run
> that stopped on the first collision left 28 tables behind. Retrying against that
> database then fails on `alembic_version` instead, which looks like a different problem
> and is not. **Drop and recreate the scratch database before every retry.** Restoring
> inside a single transaction (`--single-transaction`) avoids the mess, at the cost of
> needing the whole restore to fit in one transaction.

### 6.3 Point the application at the scratch database, locally only

Set `SCHWAB_DATABASE_URL` to the scratch database **in a scratch shell or a separate
local `.env`**, never by editing the machine's real configuration. Confirm the redacted
selector:

```powershell
schwab-trader config show
```

### 6.4 Inspect migrations before applying them

```powershell
alembic current
```

Compare against the manifest's `alembic_revision`. They should match.

- If the scratch database is **behind** this checkout's head, that is expected for an
  older artifact. Run `alembic upgrade head` **against the scratch database only** and
  read the output: a `Running upgrade` line per applied revision. Silence means nothing
  was applied — either it was already at head, or a model shipped without a revision.
- If the scratch database reports a revision **not in this checkout's chain**, stop.
  Do not migrate. Check out the revision that produced the artifact instead.

### 6.5 Run health against the scratch database

```powershell
schwab-trader storage health --json
```

Expect exit 0. Anything else is a finding about the artifact, the restore, or the
migration you just applied — investigate before trusting the artifact.

### 6.6 Compare deterministic evidence

Compare the restored database against the manifest, and against the source if it is
still available:

| Evidence | Where to get it | Expected |
|---|---|---|
| Schema revision | `alembic current` | Matches `alembic_revision`, or a deliberate upgrade from it |
| Required tables and constraints | `storage health --json` → `missing_tables`, `missing_constraints` | Both empty |
| Per-table row counts | `storage health --json` → `table_counts` | Matches the manifest's `table_counts` |
| Cohort count | `table_counts.cohorts` | Matches |
| Official run count | `table_counts.cohort_runs` | Matches |
| Official observation count | `table_counts.official_daily_observations` | Matches |
| Paper orders and fills | `table_counts.paper_orders`, `table_counts.paper_fills` | Matches |
| A specific known session | `storage recover --cohort <id> --scheduled-for <date> --json` | Same `state`, `run_id`, and member statuses as the source |

Counts that differ by exactly the rows written after `created_at` are expected for a
live source and are not a finding. Counts that differ in any other way **are**.

### 6.7 Confirm application behavior without enabling live trading

```powershell
schwab-trader serve --no-live --host 127.0.0.1
```

Confirm sleeves, cohort members, historical curves, and coverage render from the
restored data. Keep `SCHWAB_TRADING_ENABLED=false` and `SCHWAB_DRY_RUN=true`. Do not
start a scheduler, do not run an official session, and do not touch any order,
approval, or reconciliation path against a restored database.

### 6.8 Roll back and clean up

1. Clear `SCHWAB_DATABASE_URL` from the scratch shell, or close it.
2. Confirm the real configuration is untouched: `schwab-trader config show` on a normal
   shell must report the backend you started with.
3. Re-run `schwab-trader storage health` against the real database and confirm exit 0.
4. Delete the scratch database or branch **only after** recording the results. Deleting
   it is the last step, not the first.
5. Leave the original artifact and its manifest in place. Nothing here deletes a backup.

### 6.9 Record the outcome

Append to your operational log:

- rehearsal date, artifact name, artifact SHA-256;
- manifest `alembic_revision` and `code_revision`;
- restore target type (scratch database or provider branch);
- `storage health` exit code on the restored database;
- which evidence in 6.6 matched and which did not;
- total elapsed time;
- anything you had to work out that was not written down — then fix this document.

### 6.10 Escalate rather than guessing

Stop and escalate — do not improvise — if:

- the artifact fails `storage backup-verify`;
- the restored revision is not in this checkout's migration chain;
- `storage health` reports a missing table or a missing constraint after restore;
- table counts differ in a way the artifact's age does not explain;
- a known cohort session classifies differently on the restore than on the source;
- you are tempted to run a `DELETE`, `TRUNCATE`, or `DROP` to make evidence line up.

Guessing at this point converts a recoverable situation into an unrecoverable one.

---

## 7. What recovery must never do

None of these are ever the right first move, and none of them are implemented by any
command in this repository:

- overwriting the active database as the first recovery action;
- clearing an official-session lease by hand (an expired lease is re-acquired by the
  next official run through the existing contract);
- deleting or rewriting a run record;
- resetting a member to make it re-run;
- replaying a member whose previous attempt is ambiguous;
- changing a cohort's identity;
- inserting an observation or fill to "fill a gap";
- rewriting a `partial` or `failed` run as `completed`.

`storage recover` classifies the session and names exactly one safe next action. Use it:

```powershell
schwab-trader storage recover --cohort <id> --scheduled-for <YYYY-MM-DD>
```

It is strictly read-only. It cannot repair anything, by design.

---

## 8. Neon-specific layer

Neon provides this project's managed PostgreSQL. Its history-based recovery is a
genuine recovery layer, and it is *not* a replacement for the portable export above.

**Nothing in this repository calls the Neon API, and nothing here creates, deletes, or
restores a Neon branch.** Everything in this section is an operator action performed in
the Neon console or CLI, by a human, deliberately.

### 8.1 The concepts

- **History / point-in-time recovery.** Neon retains write-ahead history for a
  configured window, letting you address the state of a branch as of an earlier
  instant.
- **Branch.** A copy-on-write database created from a parent branch, optionally at a
  past point in time. Creating one does not modify the parent.

The recovery-shaped use of these: create a **new branch at a past timestamp**, point a
scratch shell at it, and compare. That is a restore rehearsal that never touches the
branch your application uses.

### 8.2 How to use it safely

1. Identify the instant you want, from the incident record — not from memory.
2. Create a **new** branch at that instant. Never restore onto the branch the
   application is configured against as a first action.
3. Copy that branch's connection string into a scratch shell only. Do not put it in the
   machine's real `.env`, an issue, a PR, a log, a command-line argument, or chat.
4. Run sections 6.4 through 6.7 against it.
5. If the evidence is what you expect, *then* decide — as a separate, explicitly
   authorized step — how to bring that state back into the active branch.
6. Delete the scratch branch after recording the outcome.

### 8.3 What not to assume

- **Do not assume a restore window.** The retention period depends on the configured
  plan and settings, and it changes. Verify the current value in the console before
  relying on it, record what you verified and when, and re-verify at each rehearsal.
  This document deliberately states no number.
- **Do not assume history covers the failure you have.** History-based recovery is
  bounded by that window; a portable export is not.
- **Do not assume a branch is a backup.** A branch lives in the same project and
  account as its parent. Account, project, or plan loss takes both. The portable export
  is the layer that survives that, which is why both exist.
- **Do not record project IDs, endpoint hostnames, connection strings, API keys, or
  account identifiers** in this repository, an issue, a PR, or a rehearsal log kept
  under version control.

### 8.4 The single-writer rule still applies

Exactly one machine is the official scheduler and holds the runtime writer role. Other
machines use the read-only dashboard role. Neon branching makes it easy to create a
second writable endpoint by accident; the PostgreSQL advisory lock and the
`(cohort_id, scheduled_for)` uniqueness rule are the final barriers against a duplicate
official session, not a substitute for the policy.

Verify with `storage health --require-writer` on the scheduler machine and plain
`storage health` elsewhere.

---

## See also

- [`docs/migrations/postgresql-runbook.md`](../migrations/postgresql-runbook.md) — the
  original SQLite-to-PostgreSQL cutover and its verification.
- [`docs/operations/storage-operator-checklist.md`](storage-operator-checklist.md) —
  the recurring checklist that keeps this runbook honest.
- [`docs/architecture/shared-storage.md`](../architecture/shared-storage.md) — the
  backend-neutral storage design.
