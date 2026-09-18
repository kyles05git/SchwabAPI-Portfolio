# challenger-v1 experiment contract

**Status:** frozen for review (issue #91, PR #98)
**Contract hash:** `47965e0f12dede2e74ba7100276eeafc09d9a7ae8577788d8906f5594f9bd981`
**Machine-readable twin:** `src/schwab_trader/strategies/contract.py`
**Parent epic:** #90

This document is the authoritative specification of the five-sleeve challenger
cohort. `contract.py` holds the same decisions as typed data so the three
strategy implementations read one set of constants instead of re-typing them;
where the two ever disagree, this document is what was reviewed and the code is
the defect.

Every value here is frozen *before* the experiment collects evidence. That is the
point. A parameter chosen after seeing an outcome is not a prediction, and the
whole contract hashes to the digest above so that a later edit fails
`tests/test_challenger_contract.py` rather than quietly redefining a running
experiment.

---

## 1. What this experiment is, and is not

challenger-v1 produces **operational and early-behavior evidence**:

- that five independent sleeves can be defined immutably and reconstructed from
  persisted state,
- that they run every session without partial mutation,
- that they execute on the T+1 open they claim to,
- and that their turnover, coverage, and failure modes are what the definitions
  imply.

It is **not** evidence of alpha, and no result from it authorizes live capital.
The observation window is far too short for statistical significance, the
universes carry survivorship bias, and the price inputs are not total returns.

Any report built on this cohort repeats the limitations in §9 rather than
presenting a return number on its own.

### Relationship to the existing cohort

`paper-first-2026-07-28` is immutable and continues collecting its own separate
evidence under its own configuration identity. challenger-v1 is a **new cohort**
with new definitions and new hashes. Nothing in this contract modifies the July 27
or July 28 definitions, observations, fills, positions, or lifecycle, and
`tests/test_strategy_seam.py` pins all seven July 28 definition digests to prove
it.

---

## 2. Membership and capital

Exactly five members:

| Sleeve | Role | Implementation |
|---|---|---|
| `control-cash` | accounting and no-trade control | existing `hold` |
| `bench-spy` | passive market benchmark | existing `buy-hold` |
| `dual-momentum-v1` | multi-asset relative + absolute momentum | new (#92) |
| `quality-profitability-v1` | point-in-time quality/profitability ranking | new (#93) |
| `short-term-mean-reversion-v1` | short-horizon oversold inside an uptrend | new (#94) |

Each sleeve is funded with **its own simulated $10,000**. Five sleeves × $10,000
is five separate simulations, **not $50,000**, and not pooled. None of it is
real, reserved in, divided from, or linked to the brokerage account.

`bench-spy` is the common benchmark; every strategy definition references it.

### Portfolio constraints (all sleeves)

- **Long-only.** No shorting, ever.
- **No leverage.** `leverage = 1`, `leverage_allowed = false`.
- **Whole shares only.** Fractional remainders stay in cash. A $10,000 sleeve
  holding one ~$600 ETF will carry a few hundred dollars of idle cash; that is
  expected and is not a defect.
- **Gross exposure cap pinned at 100%.** The existing paper sleeves scale
  exposure by `signals.regime_signal`. challenger-v1 deliberately does **not**:
  that regime score is a second, independent market-timing bet layered on top of
  the strategy under test, and it would make it impossible to say whether a
  sleeve's behavior came from its own rules or from the overlay. Each sleeve
  tests exactly one idea.

---

## 3. Timing: signal on T, execute at T+1 open

- **Signal session T:** the strategy decides from evidence available at the
  official XNYS close of session T (13:00 ET on early closes, otherwise 16:00 ET).
  Nothing dated after that close may enter the decision.
- **Execution session T+1:** the resulting orders fill at the official XNYS
  **opening print of the next trading session**.
- **Valuation:** every sleeve is marked at the official close of every trading
  session.

This depends on **issue #79**, which owns the execution-timing engine.
challenger-v1 must not introduce a second timing engine, and this contract does
not define one. Specifically, #79 owns:

- weekend, holiday, and early-close session arithmetic,
- the deterministic next-open fill model,
- behavior when the T+1 opening bar is missing or unusable,
- and the separate decision/execution/valuation timestamps persisted per order.

**Missing T+1 open:** the sleeve fails closed for that execution. It does not
fall back to the T close, to the T+1 close, or to a quote. The exact mechanism is
#79's; this contract only fixes that no substitute price is acceptable.

`settlement_model` remains `T+1`. Note that `settlement_t1` in the existing code
models *settled cash*, which is a different thing from *signal-T/execute-T+1* —
#79 keeps those distinct and challenger-v1 requires both.

---

## 4. Transaction costs, spread, and slippage

| Item | Value |
|---|---|
| Cost model id | `challenger-v1-open-fill-10bps-round-trip` |
| Per side | 5 bps |
| Round trip | 10 bps |
| Applied as | symmetric half-spread around the T+1 opening print |
| Commission | $0 |

Retail equity/ETF commissions are zero, so the modeled cost is spread plus
slippage only.

**Why 5 bps per side.** It is deliberately conservative for mega-cap ETFs (SPY's
quoted spread is well under 1 bp) and roughly realistic for liquid large caps at
the opening auction, which is the least liquid moment of the session. It matches
the round-trip convention already used by `backtest.py`'s `--cost-bps`. It was
chosen as a defensible default, not by searching for the value that produced the
best result.

**One uniform cost for all five sleeves, benchmark included.** A cheaper
assumption for `bench-spy` than for the challengers would flatter the benchmark;
a cheaper assumption for a high-turnover challenger would flatter that
challenger. `short-term-mean-reversion-v1` is by construction the most
cost-sensitive member, and revealing that is one of the experiment's purposes,
not a problem to be tuned away.

Costs are **modeled, not observed**. Real opening-auction slippage varies by
name, size, and day.

---

## 5. Frozen strategy specifications

### 5.1 `control-cash` (v1)

Holds $10,000 in cash and never trades. It exists so that accounting,
valuation, and reporting have a member whose correct answer is known exactly.
Cadence: never. `max_positions = 0`.

### 5.2 `bench-spy` (v1)

Buys SPY once on the first execution session and holds. Universe `("SPY",)`,
`max_positions = 1` at 100%. It pays the same 10 bps round trip on its single
entry as every other sleeve.

### 5.3 `dual-momentum-v1` (v1) — owner **#92**

| Decision | Value |
|---|---|
| Risk universe | `SPY`, `EFA`, `EEM`, `VNQ` |
| Defensive asset | `IEF` |
| Lookback | 252 sessions (trailing price return) |
| Relative momentum | rank the risk universe by trailing return, descending |
| Absolute gate | the top-ranked asset's trailing return must be **> 0** |
| If the gate fails | hold `IEF` instead |
| Max positions | 1 at 100% |
| Cadence | monthly, on the last XNYS session of the calendar month |
| Minimum history | 253 closes, for every risk asset **and** the defensive asset |
| Coverage floor | 100% — all five symbols required |
| Tie-break | frozen universe order (`SPY`, `EFA`, `EEM`, `VNQ`) |

**Universe rationale.** Four liquid, long-listed ETFs spanning the major
directional asset classes: US large cap, developed ex-US, emerging markets, and
US real estate. This is the standard multi-asset momentum setup and follows the
published dual-momentum design (relative momentum to pick, absolute momentum to
gate).

**Why the absolute gate is "> 0" rather than "> T-bills".** The canonical
formulation compares the winner against a T-bill proxy such as `BIL`. This
project's bars are **price return only**, and `BIL`'s return is almost entirely
coupon, so on the data actually available `BIL` is indistinguishable from a flat
line and the comparison would be meaningless. Comparing against zero is the
honest version of the same gate given the inputs. This is a documented
consequence of a data limitation, not a preference.

**Why `IEF` rather than `BIL` as the defensive asset.** Same reason: `IEF` at
least has a meaningful price series. It still understates the defensive sleeve's
true return, which §9 records.

**Why 100% coverage.** A four-name universe cannot absorb a missing member —
dropping one changes which asset classes are even eligible to win. Missing or
stale evidence for any of the five symbols fails the sleeve closed for that
session.

**Why monthly.** Monthly evaluation is the published cadence for dual momentum
and is what keeps a 252-session signal from generating daily churn. A 12-month
lookback barely moves day to day, so daily evaluation would add turnover and cost
without adding information.

### 5.4 `quality-profitability-v1` (v1) — owner **#93**

| Decision | Value |
|---|---|
| Universe | the `large-cap` preset (74 names) |
| Components | `gross_profit TTM / assets`, `net_income TTM / equity`, `net_income TTM / revenue TTM` |
| Weights | equal, 1/3 each |
| Direction | all three higher-is-better |
| Normalization | cross-sectional percentile rank among eligible names |
| Eligibility | **all three** components computable |
| Coverage floor | 60% of the universe |
| Max positions | 10 at 10% each |
| Cadence | monthly (last XNYS session), and only when the selected set changes |
| Tie-break | frozen universe order |

**Component rationale.** Gross profitability (Novy-Marx), return on equity, and
net margin are three long-documented, non-overlapping profitability measures. All
three are computable from canonical SEC facts this repository **already** maps in
`fundamentals.FIELD_CONCEPTS` (`gross_profit`, `assets`, `net_income`, `equity`,
`revenue`). Only `gross_profit / assets` needs a new helper; #93 owns that narrow
addition.

**No price enters the ranking.** All three components are pure fundamental
ratios, so a stale or missing quote cannot distort the selection. A price is
needed only to size the order, and #79 supplies the T+1 opening print for the
fill. This is a deliberate design advantage over a value factor such as
earnings yield, which would need a point-in-time price.

**Point-in-time discipline.** Facts are read through
`SecStore.point_in_time` / `facts_as_of` with `as_of` = signal session T's date,
which considers only filings dated on or before that date. **A restatement filed
after session T is invisible to session T's decision**, permanently. The current
snapshot of a company's financials is never substituted for what was known then,
and Schwab's current-fundamentals endpoint must never be used as historical
evidence.

**Missing components are reported, never imputed.** A name missing any of the
three is ineligible and appears in the coverage report. Scoring on partial
components would let a company rank well on the one ratio it happened to report,
which is a data artifact rather than a quality signal. Silently dropping it would
hide the artifact.

**Coverage floor.** If fewer than 60% of the universe is eligible on session T,
the sleeve fails closed for that session rather than ranking a rump universe.

### 5.5 `short-term-mean-reversion-v1` (v1) — owner **#94**

| Decision | Value |
|---|---|
| Universe | the `large-cap` preset (74 names) |
| Short average | 20 sessions |
| Long average | 200 sessions |
| Entry | close ≥ 5% below the 20-session average **while** above the 200-session average |
| Ranking | most negative close-to-short-average distance first |
| Exit | close recovers to within 1% of the 20-session average, **or** closes below the 200-session average |
| Max positions | 5 at 20% each |
| Cadence | every XNYS session |
| Minimum history | 201 closes |
| Coverage floor | 60% of the universe |
| Tie-break | frozen universe order |

**Parameter provenance.** 20 / 200 / 5% are the **registered defaults of the
existing `mean-reversion` strategy**, adopted unchanged. They predate the July 27
and July 28 cohorts and were not selected by looking at any challenger outcome.
This is the strongest available guarantee that these numbers are not fitted.

**Position fraction changed from the registered default.** The registered default
pairs `max_positions = 5` with `max_position_fraction = 0.10`, which caps a full
book at 50% invested and strands the other half in cash. Compared against a fully
invested benchmark that is not a strategy test, it is a half-cash portfolio.
challenger-v1 sets the fraction to **0.20** = 1/5 exactly, so a full book is
fully invested. This is the one deliberate departure from the registered
defaults, and it is a portfolio-construction fix, not a signal change.

**Explicit exits.** The registered implementation exits only *implicitly*, when a
name falls out of the recomputed target list. challenger-v1 requires two named,
deterministic exits computable from session-T closes: recovery to within 1% of
the 20-session average (the reversion completed) or a close below the
200-session average (the uptrend premise broke). Both must expose their exact
signal values in the rationale.

**Why daily.** The horizon *is* the strategy. A 20-session dip resolves in days,
so a monthly cadence would systematically miss both entry and exit. This makes
the sleeve the most turnover- and cost-sensitive member of the cohort by
construction, which is why #94 must report cost/turnover sensitivity.

---

## 6. Can the existing implementation express this?

Audited before designing anything new.

### `MeanReversionStrategy` — reusable signal, non-compliant construction

| Requirement | Existing behavior | Verdict |
|---|---|---|
| Oversold inside a longer uptrend | `price > long_ma and price/short_ma - 1 <= -dip` | **Expresses it** |
| Deterministic ranking | sorts by `below`, most negative first | Expresses it, but ties resolve by list order |
| Long-only, whole shares, position cap | via the shared `_rebalance` | **Reusable** (now `sizing.plan_rebalance`) |
| Gross cap pinned at 100% | applies `signals.regime_signal` when benchmark history is present | **Non-compliant** |
| Fail closed on insufficient history | silently `continue`s a short-history symbol | **Non-compliant** |
| Explicit recovery/exit rule | implicit only (fell out of the target list) | **Missing** |
| Exposed entry/exit signal values | generic rationale string | **Missing** |

**Conclusion:** versioned reuse, not duplication and not as-is registration. #94
keeps the entry and ranking mathematics and the shared sizing helper, and
implements a version-1 strategy that pins the gross cap at 100%, fails closed on
insufficient or stale history, implements the two named exits, and exposes the
signal values. `MeanReversionStrategy` itself is **not modified** — the July 28
cohort uses it and its definition hash must not move.

### `FundamentalStrategy` / `ValueMomentumStrategy` / `fundamentals.py` / `SecStore`

- `SecStore.point_in_time` and `facts_as_of` already implement exactly the
  point-in-time semantics #93 needs, including "latest filing wins" and
  restatement invisibility. **Reuse as-is.**
- `fundamentals.return_on_equity` and `ttm_value` are directly reusable.
  `net_margin_ttm` already exists inside `Ratios` but is not exposed as a
  cross-sectional factor. `gross_profit / assets` does not exist. #93 owns those
  narrow additions to `fundamentals.py` and must declare that ownership in its
  claim.
- `FundamentalStrategy` ranks by **one** `FACTORS` entry and is value-oriented;
  `ValueMomentumStrategy` blends momentum with fundamentals. Neither expresses a
  three-component equally weighted quality composite with an all-components
  eligibility rule and a coverage floor. A new module is justified; the factor
  math underneath is reused.

### Tactical / regime / momentum code

- `signals.momentum_features` computes four momentum windows for one symbol and
  `momentum_composite` blends their cross-sectional ranks. Dual momentum needs
  **one** trailing return plus an absolute gate, so #92 computes the return
  directly rather than bending the 4-component composite into a 1-component one.
- `regime_allocator` and `TacticalRegimeStrategy` implement a three-state
  SPY/cash allocator with a weekly confirmation rule. That is a *different*
  strategy from dual momentum (regime-score driven rather than cross-sectional
  momentum driven), and challenger-v1 explicitly excludes the regime overlay.
  **Not reused.**
- `signals.sma` is reused by #94.

### Parameter-selection discipline

No parameter in this contract was chosen by evaluating July 27 or July 28
outcomes, and none was selected by searching for the best historical return.
Provenance is one of: an existing registered default (mean reversion), a
published standard (dual momentum's 12-month lookback and monthly cadence,
gross profitability), or a documented conservative default (costs, coverage
floors). Where a data limitation forced a departure from the published version,
§5.3 says so explicitly.

---

## 7. Data requirements and failure behavior

### Required inputs

| Sleeve | Daily price history | SEC EDGAR facts |
|---|---|---|
| `control-cash` | — | — |
| `bench-spy` | SPY | — |
| `dual-momentum-v1` | 253 closes × 5 symbols | — |
| `quality-profitability-v1` | price for sizing only | point-in-time, as of T |
| `short-term-mean-reversion-v1` | 201 closes × universe | — |

### Fail-closed rules

These are non-negotiable and apply to every sleeve.

| Condition | Behavior |
|---|---|
| Symbol has fewer than the required closes | symbol ineligible, **reported** not dropped |
| Symbol's latest bar is older than session T | symbol ineligible (staleness tolerance is **0 sessions**) |
| Eligible fraction below the sleeve's coverage floor | **whole sleeve fails closed** for that session |
| Two different values for the same symbol and session | **whole sleeve fails closed** — a data-integrity fault, not a gap; the sleeve does not pick one |
| Required SEC component missing | name ineligible, reported; floor still applies |
| Invalid denominator (≤ 0 equity, assets, revenue) | component missing → name ineligible |
| Non-finite or non-positive price | symbol ineligible |
| T+1 opening print unavailable | execution fails closed; no substitute price (§3) |

A sleeve that fails closed produces **no orders** for that session and records
why. It never partially executes, and per the cohort's all-or-nothing semantics
one unready member must not partially mutate the ready ones.

---

## 8. Identity, hashing, and immutability

Each sleeve persists a `StrategyDefinition` whose `configuration_hash` covers, via
the existing `configuration_payload()`:

`strategy_id`, `strategy_version`, `implementation_name`, `parameters`,
`universe_definition`, `benchmark_symbol_or_sleeve`, `decision_frequency`,
`decision_time`, `data_requirements`, `long_only`, `leverage_allowed`.

The cohort-level contract hash additionally covers starting cash, settlement
model, leverage, gross exposure cap, cost model and basis points, signal /
execution / valuation basis, staleness tolerance, conflicting-evidence policy,
and every sleeve's frozen payload.

Rules:

- **Names and versions are stable.** `dual-momentum-v1` always means what §5.3
  says. A different parameter set is `-v2`, never a redefinition of `-v1`.
- **Universes are snapshotted into the definition** as explicit symbol lists, the
  way the existing bootstrap does, so a later edit to `universes.py` cannot
  retroactively change a running experiment. The `large-cap` preset is
  additionally pinned by digest in `tests/test_challenger_contract.py`.
- **No persisted schema change** is required by this contract. Everything fits
  the existing `StrategyDefinition` / `ExperimentCohort` models and their columns.
  No migration is needed and none is added.

---

## 9. Known limitations

These are load-bearing and travel with every result.

1. **Survivorship bias.** The `large-cap` preset is a curated list of companies
   listed and liquid *today*. Names that failed or were acquired never appear, so
   any historical comparison over it is biased upward. This is stated in
   `universes.py` itself and is not fixed by this contract.
2. **Price return, not total return.** The daily bars are unadjusted closes, so
   dividends are invisible. This understates every dividend-paying holding and
   systematically penalizes the higher-yielding members of the dual-momentum
   universe (`EFA`, `EEM`, `VNQ`, `IEF`) against `SPY`. It is also why the
   absolute-momentum gate compares against zero (§5.3).
3. **Modeled costs, not real fills.** 10 bps round trip is an assumption.
4. **Corporate actions** are handled by the underlying bar evidence, not by this
   contract, which makes no claim about splits, spin-offs, or symbol changes.
5. **Short window.** A cohort observed over weeks reveals operational behavior
   and turnover, not skill. Differences between sleeves over this horizon are
   noise unless they are differences in *behavior* rather than return.
6. **No ML.** Gradients, HMMs, and RL are explicitly out of scope until #86.

---

## 10. Ownership boundaries

The seam exists so three agents can work at once. The table below is a
coordination commitment recorded on each issue; what `tests/test_strategy_seam.py`
mechanically enforces is the *package* boundary — that nothing under
`strategies/` imports the central files reserved for #95, imports `agent` from
`sizing`, performs dynamic code loading, or does I/O.

| Issue | May create / edit | Must not touch |
|---|---|---|
| **#92** | `strategies/dual_momentum.py` + its tests | `agent.py`, `signals.py`, `fundamentals.py`, other strategy modules |
| **#93** | `strategies/quality_profitability.py` + its tests, **and** narrowly required helpers in `fundamentals.py` (declare in the claim) | `agent.py`, `signals.py`, other strategy modules |
| **#94** | `strategies/short_term_mean_reversion.py` + its tests, **and** `agent.py` / `signals.py` where declared in the claim | `fundamentals.py`, other strategy modules |
| **#95** | `strategy_registry.py`, `cli.py`, bootstrap/run scripts, dashboard, lifecycle, scheduler, readiness, combined tests | strategy algorithms (return defects to their owner) |

Reserved for #95 in all cases: `strategy_registry.py`, `cli.py`,
`scripts/bootstrap_paper_cohort.py`, `scripts/run_sleeves.py`, dashboard,
lifecycle, and scheduler integration. **No challenger strategy is registered or
placed in a cohort by #92–#94.**

If a strategy agent finds the shared seam insufficient, it reports that on #91
rather than inventing a competing abstraction.

### The shared seam

`src/schwab_trader/strategies/`:

- `contract.py` — the frozen values above, hashable.
- `sizing.py` — pure helpers, all injected-data, no I/O:
  `as_of_history` (look-ahead-safe slicing), `buy_limit` / `sell_limit`
  (marketable limits), `plan_rebalance` (long-only whole-share construction),
  `eligible_by_history` (coverage split that reports rather than drops), and
  `rank_desc` (ranking with a frozen-universe-order tie-break).

`sizing.py`'s first four functions were already private-but-shared inside
`agent.py` (`_asof`, `_buy_limit`, `_sell_limit`, `_rebalance`); they are
extracted unchanged and `agent.py` now delegates to them, keeping the private
names as aliases. The dependency is one-directional (`agent` → `sizing`) to avoid
an import cycle, which is why `plan_rebalance` takes explicit scalars rather than
a `MarketContext`.

The package performs **no dynamic import, no plugin discovery, and no I/O**, all
asserted by tests. A challenger's definition is data to be read, never code to be
resolved at runtime.

---

## 11. Definition of done for the contract

- [x] Five members, capital, and portfolio constraints frozen.
- [x] Signal-T / execute-T+1-open semantics stated, with the #79 dependency named.
- [x] Costs, spread/slippage, and valuation timing frozen.
- [x] Exact names, versions, universes, cadences, limits, and history lengths.
- [x] Entry, ranking, defensive/cash, rebalance, and exit rules.
- [x] Data requirements, coverage floors, and missing/stale/conflicting behavior.
- [x] Configuration-hash inputs and immutability requirements.
- [x] Existing-code audit with reuse decisions.
- [x] Interpretation and limitations recorded.
- [x] File ownership for #92–#94 explicit and test-enforced.
- [x] No schema change and no migration required.
