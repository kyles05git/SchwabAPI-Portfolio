"""Survivorship-bias tooling for the historical backtester.

Our backtest universes (e.g. the ``large-cap`` preset) are *today's* list of
survivors. Replaying a strategy over them inflates historical results three ways:

1. **Inclusion bias** - the names were picked knowing they would *become* large-cap
   winners (NVDA, LLY, AVGO...). A researcher standing in 2013 would not have had
   that list. Fixing this needs point-in-time index membership we do not have.
2. **Delisting bias** - companies that went to zero, were acquired, or shrank out
   (Lehman, Enron, Kodak...) are absent, so the losers-to-zero are never held.
   Fixing this needs delisted price data; Schwab serves only live tickers.
3. **Backfill bias** - a name's history being used in a fold *before it was listed*
   (holding META in a 2011 fold when it IPO'd in 2012).

Only #3 is fixable with the data we already have: a symbol's first available bar is
a listing-date proxy, so we can make each walk-forward fold **point-in-time** -
a name is only eligible once it was actually trading. #1 and #2 remain and are
reported here, not silently ignored. A useful bound: SPY (the benchmark) is
survivorship-clean, and the strategies already *lag* SPY - so correcting the
residual bias would only widen that gap, never reverse the "just buy the index"
conclusion. The bias flatters our numbers; the honest verdict survives it.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from schwab_trader.market_data import Candle


def listing_date(candles: list[Candle]) -> datetime | None:
    """A symbol's listing-date proxy: the timestamp of its earliest available bar.

    This is a *lower bound* on true age - if the fetched history window is shorter
    than the symbol's real life, the first bar just marks how far back we can see.
    A recent IPO's first bar is its true listing; that is exactly the case that
    would cause backfill bias, so the proxy catches what matters.
    """
    return min((c.date for c in candles), default=None)


def listing_dates(bars_by_symbol: dict[str, list[Candle]]) -> dict[str, datetime]:
    """First-bar date per symbol (symbols with no candles are omitted)."""
    out: dict[str, datetime] = {}
    for symbol, candles in bars_by_symbol.items():
        first = listing_date(candles)
        if first is not None:
            out[symbol] = first
    return out


def eligible_as_of(bars_by_symbol: dict[str, list[Candle]], as_of: datetime) -> list[str]:
    """Symbols already listed as-of ``as_of`` (first bar on or before that date).

    This is the point-in-time universe: a name that had not started trading yet is
    excluded, removing backfill bias. Returned sorted for determinism.
    """
    return sorted(
        symbol
        for symbol, candles in bars_by_symbol.items()
        if (first := listing_date(candles)) is not None and first <= as_of
    )


class FoldCoverage(BaseModel):
    """How much of the universe was actually listed as-of one fold's start."""

    as_of: datetime
    eligible: int
    total: int
    not_yet_listed: list[str]

    @property
    def coverage_pct(self) -> float:
        return 100.0 * self.eligible / self.total if self.total else 0.0


def coverage_as_of(
    bars_by_symbol: dict[str, list[Candle]], as_of: datetime, universe: list[str]
) -> FoldCoverage:
    """Point-in-time coverage of ``universe`` as-of ``as_of``.

    ``not_yet_listed`` are the survivor-list names that had not started trading yet -
    the concrete footprint of backfill bias for a backtest ending at ``as_of``.
    """
    firsts = listing_dates(bars_by_symbol)
    eligible = [s for s in universe if s in firsts and firsts[s] <= as_of]
    missing = [s for s in universe if s not in firsts or firsts[s] > as_of]
    return FoldCoverage(
        as_of=as_of, eligible=len(eligible), total=len(universe), not_yet_listed=sorted(missing)
    )
