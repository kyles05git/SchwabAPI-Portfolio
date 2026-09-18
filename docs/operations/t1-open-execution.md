# Signal on T, paper execution at T+1 open

This runbook defines the paper-only execution methodology introduced by issue #79. It
does not authorize live trading, submit or preview an order, retrieve Schwab opening
bars, install a scheduler, or change either historical July cohort.

## Methodology identity

`signal-t-close-execute-t1-open/v1` is a new, opt-in experiment identity. It is not a
rename or reinterpretation of `settlement_t1`: that setting continues to model when
sale proceeds become settled cash. The historical behavior remains
`mark-to-close/v1` and remains the default meaning of an empty methodology field.

The protected cohorts `paper-first-2026-07-27` and `paper-first-2026-07-28` cannot be
configured with the new methodology. Their definitions, configuration hashes,
observations, run identities, and close-marked behavior remain unchanged.

## Session and timestamp contract

The run remains keyed by signal session T. The canonical XNYS calendar resolves the
execution session as the next valid exchange session, so Friday crosses to Monday and
exchange holidays are skipped. An early close changes T's decision time to 1:00 PM ET;
it does not change the next session's 9:30 AM opening auction. An early-close execution
session also opens at the ordinary 9:30 AM ET.

Every separated-timing observation persists:

- signal session and signal time;
- decision time, equal to the settled T evidence boundary;
- execution session and execution time;
- valuation time, equal to the execution instant;
- methodology key and the methodology hash in snapshot lineage;
- a content digest for every opening bar used.

The strategy sees only T quote/history evidence and T starting equity. T+1 opening
marks are used for the paper fill and ending valuation, never for sizing, ranking, or
any other decision input.

## Opening evidence

Version 1 uses the first regular-session five-minute interval. A bar is usable only
after that interval finishes and only when all of these agree: symbol, XNYS session,
interval start/end, interval length, source, timezone-aware retrieval timestamp, OHLC
relationships, non-negative volume, and the reproducible content digest.

The gate fails closed for:

- no exact opening bar;
- retrieval before the interval closes or evidence stale under a declared age limit;
- a weekend, holiday, wrong session, extended-hours row, or wrong symbol;
- duplicate or conflicting rows, including case-colliding mapping keys;
- missing open/source, naive or inconsistent timestamps, invalid OHLC, negative volume,
  or a digest that does not reproduce;
- T+1/current quotes presented as though they were frozen T decision evidence.

It never substitutes the T close, a later interval, a previous open, a last quote, or a
partially printed bar.

Corporate actions receive no invented adjustment in this layer. The decision keeps the
existing settled-history semantics, while execution uses the provider's actual T+1
opening print. A split or distribution can therefore create a genuine overnight gap.
If provider symbol/session/OHLC lineage is inconsistent across the action, evidence is
unavailable and the cohort waits; the runner never manufactures an adjusted open.

## Fill and cost model

The observed opening print is the reference price. `next-open-fill-v1` applies a
declared 2 bps half-spread plus 3 bps slippage per side. Buys round up and sells round
down to the configured one-cent increment, so rounding can never improve the modeled
fill. Quantities remain whole-share. A gap that makes a T-sized buy unaffordable is
rejected instead of overspending paper cash.

Spread/slippage cost is the adverse difference between the modeled fill and observed
opening print, multiplied by filled quantity, and is persisted as `modeled_cost`.
Version 1 declares zero commission. A methodology with non-zero commission is refused
until the paper engine can book it explicitly.

## All-or-nothing, retry, and recovery

One methodology is resolved for the whole cohort before data is fetched. Preflight then
checks every member's normal readiness, frozen T quotes, and every required T+1 opening
bar before any member can change paper cash, positions, orders, cycles, or observations.
One missing or invalid input means zero members mutate.

At T close, T+1 evidence does not exist. The run records retryable `awaiting-data`; it
does not record an observation or pin a snapshot. A later scheduler invocation may use
newly arrived evidence within the existing signal-session deadline. No second timer is
introduced. Once execution begins, the run pins quote/data identities, methodology
hash, and opening-bar digests. A restart must reproduce all of them.

Run keys and official observation keys still use T, so repeated invocation returns the
same run and cannot duplicate a fill or observation. A member interrupted after paper
mutation is never blindly replayed.

## Current integration boundary

`CohortSnapshot.opening_bars` is the validated provider seam. Production Schwab
opening-bar retrieval is intentionally deferred to the later integration issue. The
current production snapshot provider supplies no opening bars, so a cohort opting into
this methodology remains `awaiting-data` and executes nobody. This is the required
fail-closed state.

The later integration must supply exact-session validated evidence and retain frozen T
decision quotes in the same snapshot. It must not add a close-price fallback, fabricate
evidence, accept current T+1 quotes for the signal, or touch live-order authorization.

## Offline verification

Use disposable local databases and the repository's globally isolated test suite:

```powershell
python -m pytest -q tests/test_execution_timing.py tests/test_next_open_fill.py `
  tests/test_t1_open_methodology_isolation.py tests/test_t1_open_orchestration.py
python -m pytest -q tests/test_migration_model_parity.py tests/test_storage_backends.py
ruff check .
mypy src
pytest -q
```

The PostgreSQL adapter has the same round-trip contract and an opt-in test that requires
an explicitly dedicated test database. Never point it at Neon or an operator database.
Offline verification reads no `.env` and cannot contact Schwab, OAuth, SMTP, Neon, or
any order endpoint.
