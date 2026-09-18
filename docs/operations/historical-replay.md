# Historical replay: research-only bar acquisition

This workflow downloads and validates the previous 30 completed exchange sessions of
regular-hours five-minute bars for a supplied universe, and stores the result as
**research evidence**.

As of this document, **no download has ever been run**. The code, schema, and tests
exist; acquisition is an explicit operator action described below.

## The experiment boundary

Historical replay records are research evidence and nothing else. By construction they
cannot:

- create or alter an official cohort observation;
- create a paper fill, order, position, valuation, or cash movement;
- satisfy forward cohort readiness;
- backfill, repair, replay, or modify `paper-first-2026-07-27` or
  `paper-first-2026-07-28`;
- change the forward five-minute fallback or its post-close retrieval requirements;
- change any strategy's official historical result.

How that is enforced, rather than merely intended:

| Concern | Official forward evidence | Historical replay |
|---|---|---|
| Domain type | `market_bar_evidence.DerivedDailyEvidence` | `historical_replay.ReplaySessionEvidence` |
| Payload schema | `schwab-intraday-daily-evidence/1` | `historical-replay-research-evidence/1` |
| Identity prefix | `schwab-intraday-derived-daily:` | `historical-replay:` |
| Tables | `market_data_daily_evidence`, `market_data_evidence_constituents` | `historical_replay_universes`, `historical_replay_sessions`, `historical_replay_bars`, `historical_replay_observations` |
| Store | `SqlAlchemyMarketDataEvidenceStore` | `SqlAlchemyHistoricalReplayStore` |
| Readiness | `storage_factory.MARKET_DATA_EVIDENCE_TABLES` | not referenced by readiness at all |

No foreign key crosses the boundary in either direction, and the digest namespaces
differ, so identical candles do not produce interchangeable content addresses.
`tests/test_historical_replay_boundary.py` pins every one of these claims, including
that ingesting replay evidence into a database that also holds cohort and paper tables
leaves every one of those tables at zero rows.

## What is validated

Per `(symbol, session)`:

- expected bar count comes from the exchange calendar — 78 for a normal 09:30–16:00 ET
  session, 42 for a 13:00 ET early close;
- weekends and observed holidays are never planned, and are `unavailable` if asked for;
- DST is handled by the calendar's Eastern-to-UTC conversion, so session bounds are
  correct on both sides of the March and November transitions;
- missing opening bar, missing final bar, and interior gaps are reported separately;
- duplicate timestamps are recorded; an exact duplicate is deduplicated and does not
  demote the session, while a *conflicting* duplicate does;
- out-of-session bars are excluded from the normalized set and demote the session,
  because their presence means `needExtendedHoursData=false` did not behave as asked;
- provider corrections are detected on later ingestion and appended as a new revision.

Each record stores the requested cohort-independent universe, session date, symbol, the
raw provider-ordered payload digest, the normalized bar digest, provider and source,
retrieval timestamp, the exact sanitized request parameters, the validation result, and
the detected gaps, duplicates, and out-of-session intervals.

## Request shape

Every request uses explicit UTC session bounds:

```text
symbol=<SYMBOL>
frequencyType=minute
frequency=5
startDate=<session open, epoch ms>
endDate=<session close, epoch ms>
needExtendedHoursData=false
```

Schwab's date-free `periodType=day&period=1` shape is **never** used: after a session
close it can answer with the previous session. Requests go through the existing
`SchwabClient` rate limiter; the downloader adds no second limiter and no retry of its
own, so a flaky provider cannot multiply request volume.

## Operator steps to actually run a download

Preflight is the default. It is pure calendar arithmetic: no socket, no client, no
token, no writes.

```bash
# 1. See exactly what would be requested. Safe, and the default with no subcommand.
python scripts/replay_download.py preflight --symbols AAPL,MSFT

# 2. Confirm the schema exists on whichever backend is configured.
#    Shared PostgreSQL: apply the migration first, following
#    docs/migrations/postgresql-runbook.md.
alembic upgrade head

# 3. Confirm you are authenticated (the download is read-only market data,
#    but it still needs a valid token).
python -m schwab_trader auth login      # only if not already authenticated

# 4. Run the acquisition. This is the only command that contacts Schwab or writes.
python scripts/replay_download.py download --symbols AAPL,MSFT
#    It prints the full plan, then requires this phrase typed exactly:
#      DOWNLOAD REPLAY RESEARCH DATA

# 5. Inspect what was stored. Read-only.
python scripts/replay_download.py report --symbols AAPL,MSFT
```

Useful flags: `--sessions N` (default 30), `--through YYYY-MM-DD` to plan back from a
specific date, `--label` to name the universe.

Re-running `download` is safe and idempotent: unchanged content resolves to the same
content-addressed row and only advances `last_seen_at`. Changed content is recorded as
a new observation revision naming the record it replaced — never an overwrite.

## Storage layout

Local (no `SCHWAB_DATABASE_URL`): a dedicated SQLite file at
`SCHWAB_HISTORICAL_REPLAY_DB_PATH`, default `./data/historical_replay.sqlite3` —
deliberately a different file from the official evidence store.

Shared PostgreSQL: the same four tables, created by Alembic revision
`20260730_0004`. That revision is declarative, touches no existing revision, and its
downgrade removes only the replay tables.

## Reporting

`download` and `report` emit JSON with per-bucket totals:

- `complete` — every calendar-expected bar present exactly once;
- `incomplete` — a gap, a conflicting duplicate, an out-of-session bar, or bad OHLC;
- `unavailable` — no usable payload (provider failure, or a closed exchange date);
- `corrected` — content differed from what was already on record;
- `unchanged` — an idempotent repeat of content already stored.

Provider failures are recorded as a sanitized label only (`ApiError:503`,
`TransportFailure`). Exception messages are never stored, because they can carry a URL,
a query string, or a response body.
