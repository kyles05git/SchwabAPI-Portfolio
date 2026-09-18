# Schwab exact-session bar evidence runbook

This runbook covers the paper-only intraday-to-daily fallback used by official cohorts,
plus its read-only diagnostics, empirical monitor, and reconciliation workflow. It does
not authorize live orders, install a scheduled task, or change an experiment's fill
methodology.

## Readiness contract

For every required symbol, an official snapshot follows one path:

1. Request Schwab daily history through `HistoryCache`.
2. If exactly one official daily candle covers the target exchange session, use it. Do
   not request intraday data for that symbol.
3. Otherwise request that exact session with explicit `startDate` and `endDate`,
   `frequencyType=minute`, `frequency=5`, and `needExtendedHoursData=false`.
4. Use the canonical XNYS calendar to validate the response after the official close.
5. Persist and append a derived daily candle only when all intervals are complete.

The fallback never uses the date-free `periodType=day&period=1` request. Live probes showed
that request can return only the previous session even after the explicit timestamp
request returns the complete target session.

A normal session requires the 78 unique interval starts from 9:30 AM through 3:55 PM ET.
A 1:00 PM early close requires 42, ending with the interval that begins at 12:55 PM ET.
The validator rejects retrieval at or before the close, another session's candles,
premarket or after-hours entries, missing or duplicate intervals, provider order
violations, invalid OHLC relationships, and negative volume. It does not sort away an
order defect, fill a gap, forward-fill a value, or substitute the previous session.

A complete daily aggregate is deterministic:

- open: first interval open;
- high: maximum interval high;
- low: minimum interval low;
- close: final interval close;
- volume: sum of interval volumes.

Its source is `schwab-intraday-derived-daily`, never the official Schwab daily source.
Earlier history and a later official candle are never overwritten.

## Durable evidence

Every accepted derived candle stores its symbol, session, retrieval time, source,
expected and observed counts, first and final interval times, aggregate OHLCV,
content-addressed dataset ID, constituent digest, and every exact five-minute
constituent. Identical evidence keeps the first persisted retrieval time and dataset ID.
A later corrected provider dataset is retained as a second immutable version.

With `SCHWAB_DATABASE_URL` configured, evidence uses shared PostgreSQL. Otherwise it uses
`SCHWAB_MARKET_DATA_EVIDENCE_DB_PATH`. Revision `20260728_0003` creates the two evidence
tables declaratively. Apply `alembic upgrade head` only through the reviewed deployment
runbook; development and diagnostics must not run DDL against Neon.

The shared store does not create those tables itself, so an unapplied revision would
otherwise stay invisible until the first session that needs the fallback failed for every
symbol as `provider_error` — a misleading cause that also blocks all seven members.
`schwab-trader cohort readiness` therefore inspects for both tables and fails the
`market-data-evidence-schema` check by name, with the runbook as its remedy. Run it on the
writer machine before relying on the scheduled job.

The content-addressed dataset ID becomes part of the cohort snapshot identity. Before
any member executes, every symbol and all seven members must be ready. One incomplete
symbol means zero executions and no observations, fills, positions, cash, or cycles.
While nothing has executed, a retry may adopt newly complete evidence. Once execution
starts, the snapshot identity is immutable.

## Diagnose one session

```powershell
schwab-trader market-data diagnose-bars `
  --symbols SPY,XLB,AAPL,ABBV `
  --session 2026-07-28

schwab-trader market-data diagnose-bars `
  --symbols SPY,XLB,AAPL,ABBV `
  --session 2026-07-28 `
  --json
```

This is read-only. It requests daily first for each symbol and requests exact-session
intraday only when daily is missing. The stable `schwab-bar-diagnostic/1` JSON includes
the latest official daily session, returned and target-session interval bounds, expected,
observed, and unique counts, missing/duplicate/unexpected/invalid intervals, safe
aggregation status, derived OHLCV when valid, evidence source, retrieval time, and a
sanitized provider error class. It contains no request headers, tokens, database URLs,
or connection strings. Exit `0` means every symbol is ready; exit `1` means at least one
is incomplete or errored.

The command computes a candidate aggregate but does not persist evidence and never
mutates a cohort or paper account.

## Measure provider publication empirically

```powershell
schwab-trader market-data monitor-bars `
  --symbols SPY,XLB,AAPL,ABBV `
  --session 2026-07-28 `
  --poll-seconds 60 `
  --stop-at 2026-07-29T18:00:00-04:00 `
  --jsonl
```

Each `schwab-bar-monitor/1` JSONL row records the poll timestamp and, per symbol, when
this process first observed the final regular-session five-minute interval and when it
first observed the official daily candle. A first-seen time is not a provider SLA or
publication timestamp. The monitor uses the existing client rate limiter, enforces a
30-second minimum interval, requires a hard future stop, caps total duration at 24
hours, stops querying a milestone after seeing it, and exits when every symbol has both
milestones or the deadline arrives. It never installs an operating-system task.

Do not turn monitor results into a hard-coded readiness time. Official execution always
depends on actual interval evidence and the existing scheduling deadline.

## Reconcile after official daily publication

```powershell
schwab-trader market-data reconcile-bars `
  --dataset-id 'schwab-intraday-derived-daily:<digest>' `
  --json
```

The read-only `schwab-derived-daily-reconciliation/1` result reloads the exact persisted
constituents, fetches later official daily history, and reports official-minus-derived
differences for O/H/L/C/V. Defaults are an absolute `0.01` tolerance independently for
each price field and an exact volume match (`0` tolerance). Override them explicitly
with `--price-tolerance` and `--volume-tolerance` when measuring a documented use case.
A missing official candle remains `awaiting_official_daily`; provider and duplicate-daily
conditions remain distinct. Material fields produce exit `1`.

**Expect `material_fields: ["volume"]` and exit `1` on a healthy session.** Schwab's
official daily volume normally includes trades that regular-session-only five-minute bars
exclude, so a derived candle summed from those bars is legitimately lower. With the default
`0` volume tolerance, that difference is reported as material every time. It is reporting
noise, not a data defect: no registered daily strategy reads `Candle.volume`, so signals,
fills, and observations are unaffected. Treat a volume-only difference as the expected
outcome and read the price fields for the real comparison. Pass an explicit
`--volume-tolerance` when measuring a documented use case; the default is deliberately
strict so a price break is never hidden, and changing it is a user decision.

Reconciliation never rewrites the derived dataset, an official daily candle, a cohort
snapshot, or an official observation. This preserves the evidence needed to measure
later Schwab corrections and adjustments.

## Retry and dashboard meaning

`Awaiting provider data — retryable` means the cohort-wide preflight executed nobody and
is still inside the scheduler's authoritative retry window. Passing the preferred grace
period does not make this terminal. Health and the dashboard show the target session,
latest official daily session, derived interval coverage, preferred grace end, and the
actual retry deadline. Only the real scheduling deadline or a structural failure is
terminal/red. Missing snapshots and observations during this wait are classified as
awaiting evidence, not reproducibility failures.

Do not manually backfill, force, partially execute, substitute a stale session, or alter
`paper-first-2026-07-27` or `paper-first-2026-07-28`.

## Experiment boundaries

`settlement_t1` models settled cash; it does not mean signal at T close and execute at
T+1 open. That methodology requires a new cohort/configuration version and is tracked in
issue #79. Downloading the previous 30 sessions of five-minute bars is separate
historical replay work tracked in issue #80. Replay results must never become forward
official observations or operational evidence.

All automated coverage is fixture-backed and offline. Run real Schwab verification only
with explicit approval, and limit it to read-only market-data GET requests.
