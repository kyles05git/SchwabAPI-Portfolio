"""Volatility-normalized intraday signal helpers.

The naive intraday strategies fail partly because they use fixed price bands that
ignore each symbol's actual volatility. These helpers - true range / ATR and the
session opening range - let strategies size entries relative to realized volatility
(the fix the research doc calls for). Pure functions over :class:`Candle` lists;
strategies keep their own rolling state and call these.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from schwab_trader.market_data import Candle


def true_range(high: Decimal, low: Decimal, prev_close: Decimal) -> Decimal:
    """Wilder's true range: the largest of the H-L, H-prevC, and L-prevC magnitudes."""
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def _hl(bar: Candle) -> tuple[Decimal, Decimal]:
    high = bar.high if bar.high is not None else bar.close
    low = bar.low if bar.low is not None else bar.close
    return high, low


def atr(bars: list[Candle], period: int) -> Decimal | None:
    """Average true range over the last ``period`` bars, or None if too few bars.

    Needs ``period + 1`` bars (each true range references the prior close).
    """
    if period < 1 or len(bars) < period + 1:
        return None
    total = Decimal(0)
    for i in range(len(bars) - period, len(bars)):
        high, low = _hl(bars[i])
        total += true_range(high, low, bars[i - 1].close)
    return total / period


def opening_range(
    session_bars: list[Candle], open_ts: datetime, minutes: int
) -> tuple[Decimal, Decimal] | None:
    """(high, low) over the first ``minutes`` of a session, or None if no bars qualify."""
    highs: list[Decimal] = []
    lows: list[Decimal] = []
    for bar in session_bars:
        if (bar.date - open_ts).total_seconds() < minutes * 60:
            high, low = _hl(bar)
            highs.append(high)
            lows.append(low)
    if not highs:
        return None
    return max(highs), min(lows)
