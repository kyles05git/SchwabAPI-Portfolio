# Quant research review workflow

Use this workflow to review the scientific validity of strategy research: a strategy
specification, a backtest, a paper-cohort experiment design, a factor or statistical
model, or a machine-learning artifact (regression, HMM, gradient boosting, reinforcement
learning). It produces a written review with a research-stage verdict. It is
**findings only**: it never edits a strategy, cohort, dataset, or configuration, and it
never runs a backtest, cohort, or live query itself. A separate authorized implementation
task applies any correction.

Read `docs/SAFETY.md` first for the trading-safety boundaries this review must respect.
Reason from the specification, code, configuration, and existing recorded results only.
Never open `.env`, credentials, tokens, logs, local databases, or production data, and
never contact Schwab, Neon, SMTP, or any live data provider to "verify" a claim.

## 1. Research question and hypothesis

- Require a falsifiable hypothesis and a stated economic rationale — why the effect
  should exist, not only that it was observed. "The signal worked in this window" is not
  a hypothesis.
- Distinguish exploratory research (hypothesis generation, free to iterate) from
  confirmatory evaluation (a single held-out test of a hypothesis fixed in advance). A
  review that treats exploratory results as confirmatory evidence is itself a defect.
- Name the benchmark, the controls, and the specific decision the evidence is meant to
  support. Evidence without a named decision cannot be scored as sufficient or
  insufficient for anything.

## 2. Point-in-time data integrity

- Universe membership as known at the evaluation date, not the constituents of today's
  index. Survivorship bias from dropping delisted, acquired, or failed names.
- Corporate actions: splits, dividends, spinoffs. Confirm which series (adjusted or
  unadjusted) each computation actually uses, and that the choice is consistent across
  signal and execution.
- Fundamental data: publication date versus period-end date, amendments, and
  restatements. A ratio computed with a number that was not yet public is leaked, even if
  the historical database now shows it as of that date.
- Feature availability timestamps and vintage correctness — a feature store or dataset
  that only records today's latest value cannot answer what was known as of `T`.
- Missing, conflicting, or non-vintage-safe data must fail closed (excluded or flagged),
  never silently forward-filled or imputed into apparent completeness.

## 3. Timing and execution

- Separate the signal timestamp (when the information became available) from the
  execution timestamp (when a resulting order could actually have been placed). No
  computation may assume a closing value was tradable before the close that produced it.
- Look-ahead: any feature, label, scaler, or selection step that uses information from
  after the decision point. Label leakage: a label that encodes the outcome it predicts,
  directly or through a correlated proxy.
- Same-bar execution — filling an order at the same bar whose close generated the
  signal — is look-ahead unless the strategy explicitly and correctly models a delay.
- T-close-signal-to-T+1-open-execution is the SchwabAPI paper-cohort convention; confirm
  the reviewed research actually follows it rather than assuming a same-bar fill.
- Settlement timing, whole-share constraints, liquidity, spreads, commissions, slippage,
  and market impact. A backtest that ignores costs is not evidence the strategy survives
  them.

## 4. Experimental design

- Train, validation, and untouched test periods, with the test period read at most once.
  Rolling or walk-forward evaluation where the strategy re-fits over time.
- Every transform fit on data — scaler, feature selection, regime detector, model — must
  be fit on training information only and applied, not refit, to validation and test.
- Embargo or purging around label boundaries when labels overlap in time, so a training
  and a test observation cannot share information through overlap.
- Hyperparameter-search accounting: every configuration tried counts against the result,
  not only the winner. Multiple-hypothesis bias from testing many signals, assets, or
  windows and reporting the best one without correction.
- Baselines, ablations, negative controls, and robustness checks — a result with no
  baseline to beat and no ablation showing which component matters is not evaluable.
- The July 2026 paper cohorts and any result already labeled a holdout must never be
  tuned against; a change proposed after seeing those results is confirmatory at best and
  must be scored as such, never treated as a fresh out-of-sample test.

## 5. Statistical validity

- Sample size and the number of *effective independent* observations — daily bars over
  months are not that many independent draws once autocorrelation and cross-asset
  dependence are accounted for.
- Confidence intervals and uncertainty on every reported statistic, not point estimates
  alone. Drawdown, turnover, exposure, capacity, and tail behavior, not average return in
  isolation.
- Dependence between assets and across time periods, which shrinks the effective sample
  size further and inflates apparent significance if ignored.
- Whether the improvement survives realistic costs from Section 3 — a positive edge that
  disappears net of commissions and slippage is not an edge.
- State explicitly, every time cohort evidence is part of the review: a roughly
  30-session operational paper cohort can assess execution reliability, plumbing, and
  operational readiness, but it cannot establish persistent alpha. Treat any claim to the
  contrary as a blocking defect regardless of who makes it.

## 6. Model-specific checks

- **Regression.** Stated assumptions (linearity, error structure, multicollinearity) and
  whether they were checked, not merely assumed. Coefficient stability across refits and
  subsamples.
- **Hidden Markov Models.** State identifiability — do the estimated states correspond to
  the same regime run over run — initialization sensitivity, and regime instability
  under small data perturbations.
- **Gradient boosting.** Feature and label leakage through engineered features that
  encode the target, feature-importance stability across seeds and folds, and calibration
  of predicted probabilities against realized outcomes.
- **Reinforcement learning.** Simulator/environment validity against the real execution
  model, reward design (does maximizing the reward actually maximize the stated
  objective), distribution shift between training and evaluation regimes, and unsafe
  policy extrapolation into states the training data never covered.
- **All models.** Reproducible seeds, dataset version, configuration, code version,
  runtime environment, and model artifact. A result that cannot be reproduced from a
  recorded configuration is not yet evidence.

## 7. Promotion verdict

Close with exactly one verdict, chosen from this list and no other wording:

- **invalid design** — a blocking defect makes the result uninterpretable.
- **insufficient evidence** — the design is sound but the evidence does not yet support
  the claim.
- **suitable for exploratory backtest** — ready for further offline research, not for
  cohort evaluation.
- **suitable for forward paper evaluation** — ready for a paper cohort under existing
  safety gates, not for capital allocation.
- **ready for a separate human promotion decision** — the research and operational
  evidence are both sufficient for a human to decide; this review does not decide it.

Never write or imply that a strategy is safe for live trading, and never authorize
capital allocation, a live order, or a configuration change — those decisions belong to
an explicit human promotion decision outside this review. Never claim persistent alpha
from a short cohort or from evidence this section found insufficient.

Separate blocking validity defects (the verdict cannot be better than "insufficient
evidence" while they stand) from non-blocking improvements (worth doing, do not gate the
verdict). Every finding needs the evidence (`file:line` or the specific computation), the
concrete impact on the claim, and a correction precise enough to act on.
