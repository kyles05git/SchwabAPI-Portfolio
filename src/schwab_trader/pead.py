"""Post-earnings-announcement drift (PEAD): a time-series earnings-surprise signal.

Classic PEAD sorts stocks by earnings *surprise* and rides the drift that persists
for weeks after the announcement. Surprise is normally measured against analyst
consensus - which the SEC data does not carry - so this uses a **time-series
surprise** (a seasonal random walk): unexpected earnings = this quarter's earnings
minus the same quarter a year ago, standardized by the volatility of the recent
year-over-year changes (the Foster-Olsen-Shevlin SUE). Two honest caveats bake in a
smaller expected edge than the textbook version:

- it is a noisier proxy than an analyst-based surprise, and
- it is anchored to the SEC *filing* date (10-Q/10-K), not the earlier earnings press
  release (an 8-K), so entry is somewhat late and misses part of the initial move.

Everything here is point-in-time (only facts filed on or before the as-of date) and
pure / offline. The strategy wrapper that turns these signals into orders lives in
``agent.py``; this module just computes the number.
"""

from __future__ import annotations

import statistics
from datetime import date
from decimal import Decimal

from pydantic import BaseModel

from schwab_trader.sec_edgar import Fact
from schwab_trader.sec_store import SecStore

# us-gaap concepts for quarterly earnings, in priority order. Net income is used as
# the earnings measure; SUE standardizes per company, so the level/scale washes out.
_EARNINGS_CONCEPTS = ("NetIncomeLoss", "ProfitLoss")

_MIN_QUARTERS = 6  # need enough history for at least a couple of seasonal differences
_DEFAULT_WINDOW = 8  # quarters of seasonal differences used to estimate the surprise vol
_MIN_DIFFS = 4  # minimum year-over-year differences to standardize against


class QuarterlyEarnings(BaseModel):
    """One discrete-quarter earnings value with its point-in-time filing date."""

    period_end: date
    filed: date
    value: Decimal


class PeadSignal(BaseModel):
    """A name's post-earnings-drift signal as of a date."""

    ticker: str
    sue: float  # standardized unexpected earnings (time-series)
    latest_period_end: date
    latest_filed: date
    days_since_filed: int  # age of the most recent earnings filing at the as-of date


def _discrete_quarters(facts: list[Fact]) -> list[QuarterlyEarnings]:
    """Keep only ~90-day (discrete-quarter) facts, latest filing per quarter, oldest first.

    Mirrors the TTM assembler's period filter so a YTD/annual figure is never mistaken
    for a quarter. Point-in-time is the caller's job (pass facts filed on/before as-of).
    """
    by_end: dict[date, QuarterlyEarnings] = {}
    for fact in facts:
        if fact.period_start is None:
            continue
        duration = (fact.period_end - fact.period_start).days
        if not (80 <= duration <= 100):  # discrete quarter only (not YTD/annual)
            continue
        existing = by_end.get(fact.period_end)
        if existing is None or fact.filed > existing.filed:  # latest filing wins
            by_end[fact.period_end] = QuarterlyEarnings(
                period_end=fact.period_end, filed=fact.filed, value=fact.value
            )
    return sorted(by_end.values(), key=lambda q: q.period_end)


def quarterly_earnings(store: SecStore, ticker: str, as_of: date) -> list[QuarterlyEarnings]:
    """Point-in-time discrete-quarter earnings for ``ticker`` (oldest first), or []."""
    for concept in _EARNINGS_CONCEPTS:
        quarters = _discrete_quarters(store.facts_as_of(ticker, concept, as_of))
        if len(quarters) >= _MIN_QUARTERS:
            return quarters
    return []


def standardized_unexpected_earnings(
    quarters: list[QuarterlyEarnings],
    *,
    window: int = _DEFAULT_WINDOW,
    min_diffs: int = _MIN_DIFFS,
) -> float | None:
    """Time-series SUE from an oldest-first quarterly series, or None if too little data.

    Unexpected earnings for the latest quarter is its year-over-year change (vs the same
    quarter one year / four quarters earlier); it is standardized by the standard
    deviation of the recent ``window`` such seasonal differences. Returns None when there
    are fewer than ``min_diffs`` differences or the surprise volatility is zero.
    """
    if len(quarters) < _MIN_QUARTERS:
        return None
    values = [q.value for q in quarters]  # chronological
    diffs = [float(values[i] - values[i - 4]) for i in range(4, len(values))]
    if len(diffs) < min_diffs:
        return None
    numerator = diffs[-1]
    sample = diffs[-window:]
    if len(sample) < 2:
        return None
    sigma = statistics.stdev(sample)
    if sigma == 0:
        return None
    return numerator / sigma


def pead_signal(
    store: SecStore, ticker: str, as_of: date, *, window: int = _DEFAULT_WINDOW
) -> PeadSignal | None:
    """Assemble the PEAD signal for ``ticker`` as of ``as_of``, or None if not computable."""
    quarters = quarterly_earnings(store, ticker, as_of)
    sue = standardized_unexpected_earnings(quarters, window=window)
    if sue is None:
        return None
    latest = quarters[-1]
    return PeadSignal(
        ticker=ticker.strip().upper(),
        sue=sue,
        latest_period_end=latest.period_end,
        latest_filed=latest.filed,
        days_since_filed=(as_of - latest.filed).days,
    )
