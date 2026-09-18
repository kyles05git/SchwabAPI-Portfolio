# Shared storage architecture

Status: implemented in the draft PR for Issue #52. It must be reviewed and merged
before any authoritative laptop data is copied.

## Boundary

Shared storage contains paper trading, sleeve/cohort definitions and observations,
research specifications, validation verdicts, price history, immutable exact-session
five-minute constituents and derived daily evidence, SEC EDGAR facts, and
optional usage history. It never contains OAuth credentials, account identifiers,
approval tokens, live-order reconciliation state, tax lots, autonomous safety
ledgers, the kill switch, `.env`, token files, logs, caches, or temporary backtests.

`SCHWAB_DATABASE_URL` is the only shared-database selector. It is a secret setting,
has an empty example value, and is never accepted as a CLI argument. SQLite remains
the default when it is empty. PostgreSQL uses psycopg; remote hosts must specify
`sslmode=require`, `verify-ca`, or `verify-full`.

## Identity model

`sleeve_id` is the immutable primary key. Human-readable names are unique only inside
a scope:

- `cohort:<cohort_id>` for formal cohort members;
- `namespace:<namespace_id>` for legacy or standalone imports.

Every imported sleeve keeps `original_name`, `source_identity`, and any newer
SQLite registry `source_sleeve_id`. Existing
unassigned sleeves enter an explicitly labeled legacy namespace; migration never
invents a cohort relationship. A new official cohort can therefore contain
`bench-spy` without replacing legacy `bench-spy`.

Name resolution follows these rules:

1. A `sleeve_id` is exact.
2. A `(cohort_id, name)` pair is exact.
3. A bare name succeeds only if exactly one sleeve has that name.
4. Ambiguous bare names fail with the available sanitized scopes; they never choose
   by recency or overwrite another sleeve.

## Data model

Provider-neutral protocols define the sleeve, paper, evaluation, and official-run
ports. The existing SQLite classes and shared SQLAlchemy repositories implement the
same application behavior; one factory chooses the backend at the composition root.
Application workflows do not contain PostgreSQL conditionals.

The SQLAlchemy 2.x schema uses:

- native PostgreSQL `NUMERIC` and lossless text-backed decimals on SQLite;
- native timezone-aware timestamps on PostgreSQL and offset-preserving ISO text on
  SQLite when the source timestamp has an offset, plus a separate source-text field
  when legacy meaning is unknown;
- JSON/JSONB for immutable strategy definitions, manifests, provenance, reason codes,
  snapshot maps, run errors, research specifications, and verdicts;
- foreign keys for all sleeve, cohort, paper, evaluation, run, and observation
  relationships;
- natural uniqueness for price bars and EDGAR facts;
- content-addressed `market_data_daily_evidence` rows and ordinal
  `market_data_evidence_constituents`, preserving every five-minute candle used by an
  official derived daily bar without overwriting later provider corrections;
- `(cohort_id, scheduled_for)` and
  `(cohort_id, sleeve_id, session_date)` uniqueness for official runs and
  observations;
- append-only `cohort_accounting_checks`, `cohort_review_notes`, and
  `cohort_operator_decisions`. A correction is a new `revision` for the same identity,
  never an update, and `(identity, revision)` uniqueness makes a concurrent correction
  collide rather than silently overwrite. These three deliberately carry **no** foreign
  key to cohorts, sleeves, or observations: the local layout keeps its sleeve registry
  outside this database, so the constraint could exist on only one backend. Referential
  validity is enforced identically for both in
  `schwab_trader.cohort_review.CohortReviewService`, before any row is written.

Alembic owns schema versioning. Application startup does not silently mutate a
PostgreSQL schema.

## Concurrency

An official runner first acquires a database-backed lease for
`(cohort_id, scheduled_for)`. PostgreSQL additionally holds a session advisory lock
on a deterministic 64-bit hash for the whole run. The unique run constraint and
per-member conditional checkpoint updates are the final idempotency barriers.

The lock is scoped to the official paper session only. PostgreSQL MVCC allows other
machines to keep reading dashboards and historical results while the owner writes.
No lock crosses into live-order, approval, reconciliation, or safety-ledger state.

## Migration protocol

The operator workflow is inventory, dry-run, execute, then verify:

1. Discover only the allow-listed SQLite paths and cohort manifests.
2. Report DDL, versions, row counts, SHA-256 file hashes, normalized table checksums,
   and eligibility without record values.
3. Require an explicit assertion that all cohort runners and paper writers are
   stopped.
4. Copy every source with SQLite's backup API into an ignored local backup directory.
5. Hash and migrate the immutable copies in dependency order.
6. Commit one source as a transaction and record its status, counts, checksums, code
   revision, and timestamps.
7. Treat identical rows as resumable no-ops and any differing natural-key row as a
   conflict. Never truncate or silently overwrite.
8. Re-run all source/destination checks and read-model comparisons in verification
   mode.

The deterministic source-set hash identifies a migration. Repeating it cannot create
duplicates. A later source snapshot becomes a separate migration and must reconcile
with existing natural keys without overwrites.

## Safety consequences

This architecture does not change live order behavior or weaken the kill switch.
Normal tests use local SQLite and no network. PostgreSQL integration tests require
`SCHWAB_TEST_DATABASE_URL` and are skipped otherwise. No migration command submits,
replaces, cancels, approves, or reconciles an order.
