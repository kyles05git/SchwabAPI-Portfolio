"""Fundamental universe screening.

Builds a trading universe by *filtering* a candidate list on current fundamentals
(market cap, valuation, quality, growth, leverage) rather than hand-picking
tickers. This is a more disciplined, less hand-biased way to get a universe than
a hardcoded list - closer to the eligibility rules the trading-strategy research
calls for (size + liquidity + quality gates).

Screening uses *current* fundamentals (Schwab exposes no history), so it defines
a live universe for forward trading - it cannot reconstruct a point-in-time
historical universe for backtests. This module is pure/offline; callers fetch the
:class:`~schwab_trader.market_data.Fundamentals` and pass them in.
"""

from __future__ import annotations

from dataclasses import dataclass

from schwab_trader.market_data import Fundamentals


@dataclass(frozen=True)
class ScreenCriteria:
    """Fundamental screen thresholds. ``None`` means the criterion is not applied.

    Margins/ROE/growth are in percent (as Schwab reports them); market cap is in
    dollars. A name must satisfy every set criterion to pass.
    """

    min_market_cap: float | None = None
    max_pe: float | None = None
    min_pe: float | None = None  # e.g. 0 to exclude loss-making (negative P/E) names
    max_peg: float | None = None
    min_roe: float | None = None
    max_debt_to_equity: float | None = None
    min_eps_growth: float | None = None  # epsChangePercentTTM
    min_rev_growth: float | None = None  # revChangeTTM


def _fails(value: float | None, threshold: float | None, *, is_min: bool) -> bool:
    """A criterion fails if the threshold is set and the value is missing or off-side."""
    if threshold is None:
        return False
    if value is None:
        return True  # required data absent -> exclude (fail closed)
    return value < threshold if is_min else value > threshold


def passes(f: Fundamentals, c: ScreenCriteria) -> bool:
    """True if a symbol's fundamentals satisfy every set criterion."""
    return not (
        _fails(f.market_cap, c.min_market_cap, is_min=True)
        or _fails(f.pe_ratio, c.max_pe, is_min=False)
        or _fails(f.pe_ratio, c.min_pe, is_min=True)
        or _fails(f.peg_ratio, c.max_peg, is_min=False)
        or _fails(f.return_on_equity, c.min_roe, is_min=True)
        or _fails(f.total_debt_to_equity, c.max_debt_to_equity, is_min=False)
        or _fails(f.eps_change_pct_ttm, c.min_eps_growth, is_min=True)
        or _fails(f.rev_change_ttm, c.min_rev_growth, is_min=True)
    )


def apply_screen(rows: list[Fundamentals], c: ScreenCriteria) -> list[Fundamentals]:
    """Return the subset of ``rows`` that passes the screen."""
    return [f for f in rows if passes(f, c)]
