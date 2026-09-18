"""Intraday bar strategies for the intraday backtester.

This starts with a small reference strategy so the intraday pipeline runs end to
end on real bars. The headline day-trading strategies from the research doc - SPY
noise-area momentum with a VWAP stop, and opening-range breakout (ORB) - land here
next, built on the volatility/opening-range signals (A3).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import date
from decimal import Decimal

from schwab_trader.intraday_backtest import Action, BarContext, IntradayStrategy, Side


class VwapTrendStrategy(IntradayStrategy):
    """Reference strategy: hold long while price trades above the session VWAP.

    After a warmup (VWAP is noisy in the first few bars), go long when the close is
    at least ``band`` above VWAP and exit when it falls back below VWAP. Not tuned for
    edge - it exists to exercise the engine and provide a baseline to beat.
    """

    name = "vwap-trend"

    def __init__(
        self,
        *,
        warmup_bars: int = 3,
        band: Decimal = Decimal("0.0005"),
        no_entry_after_min: int = 210,  # stop opening new trades in the last ~30 min
    ) -> None:
        self.warmup_bars = warmup_bars
        self.band = band
        self.no_entry_after_min = no_entry_after_min

    def on_bar(self, ctx: BarContext) -> Action:
        if ctx.position is not None:
            # Exit when price drops back to/below VWAP (VWAP acting as a trailing stop).
            return Action.EXIT if ctx.bar.close <= ctx.vwap else Action.HOLD
        if ctx.bar_index < self.warmup_bars or ctx.minutes_since_open > self.no_entry_after_min:
            return Action.HOLD
        if ctx.vwap > 0 and (ctx.bar.close / ctx.vwap - 1) >= self.band:
            return Action.ENTER_LONG
        return Action.HOLD


class NoiseAreaMomentumStrategy(IntradayStrategy):
    """SPY-style noise-area momentum with a VWAP stop.

    Each session defines a "noise area" around the open: ``open +/- band``, where
    ``band = band_mult * (average recent daily range)`` - so the breakout threshold is
    volatility-normalized, not a fixed percentage (the doc's key critique). When price
    closes above the upper edge it goes long (momentum breakout); below the lower edge,
    short. The exit is a **VWAP stop**: a long exits when price closes back below the
    session VWAP, a short when it closes above it (plus a forced flat at the close). One
    directional trade per session by default, and no new entries in the last stretch.
    """

    name = "noise-area"

    def __init__(
        self,
        *,
        band_mult: Decimal = Decimal("0.5"),
        lookback_sessions: int = 14,
        min_sessions: int = 10,
        warmup_min: int = 5,
        no_entry_after_min: int = 210,
        one_trade_per_session: bool = True,
    ) -> None:
        self.band_mult = band_mult
        self.min_sessions = min_sessions
        self.warmup_min = warmup_min
        self.no_entry_after_min = no_entry_after_min
        self.one_trade_per_session = one_trade_per_session
        self._ranges: deque[Decimal] = deque(maxlen=lookback_sessions)
        self._band_half: Decimal | None = None
        self._prior_range: Decimal | None = None
        self._entered = False

    def on_session_start(self, session_date: date) -> None:
        if self._prior_range is not None:  # fold the just-finished session's range in
            self._ranges.append(self._prior_range)
            self._prior_range = None
        if len(self._ranges) >= self.min_sessions:
            avg_range = sum(self._ranges, Decimal(0)) / len(self._ranges)
            self._band_half = self.band_mult * avg_range
        else:
            self._band_half = None  # not enough history yet -> stand aside
        self._entered = False

    def on_bar(self, ctx: BarContext) -> Action:
        self._prior_range = ctx.session_high - ctx.session_low  # running; used next session

        if ctx.position is not None:
            if ctx.position.side is Side.LONG and ctx.bar.close < ctx.vwap:
                return Action.EXIT
            if ctx.position.side is Side.SHORT and ctx.bar.close > ctx.vwap:
                return Action.EXIT
            return Action.HOLD

        if self._band_half is None or self._band_half <= 0:
            return Action.HOLD
        if self._entered and self.one_trade_per_session:
            return Action.HOLD
        if (
            ctx.minutes_since_open < self.warmup_min
            or ctx.minutes_since_open > self.no_entry_after_min
        ):
            return Action.HOLD

        if ctx.bar.close > ctx.session_open + self._band_half:
            self._entered = True
            return Action.ENTER_LONG
        if ctx.bar.close < ctx.session_open - self._band_half:
            self._entered = True
            return Action.ENTER_SHORT
        return Action.HOLD


class OpeningRangeBreakoutStrategy(IntradayStrategy):
    """Opening-range breakout (ORB), single-symbol.

    Builds the high/low of the first ``or_minutes`` of the session, then goes long on
    a close above the range high or short on a close below the range low. The stop is
    the opposite side of the opening range; the engine also forces a flat close. One
    trade per session. (The multi-symbol universe + relative-volume "stocks in play"
    ranking and short-borrow handling from the ORB paper are deferred.)
    """

    name = "orb"

    def __init__(
        self,
        *,
        or_minutes: int = 5,
        no_entry_after_min: int = 210,
    ) -> None:
        self.or_minutes = or_minutes
        self.no_entry_after_min = no_entry_after_min
        self._or_high: Decimal | None = None
        self._or_low: Decimal | None = None
        self._entered = False

    def on_session_start(self, session_date: date) -> None:
        self._or_high = None
        self._or_low = None
        self._entered = False

    def on_bar(self, ctx: BarContext) -> Action:
        high = ctx.bar.high if ctx.bar.high is not None else ctx.bar.close
        low = ctx.bar.low if ctx.bar.low is not None else ctx.bar.close
        if ctx.minutes_since_open < self.or_minutes:  # still building the opening range
            self._or_high = high if self._or_high is None else max(self._or_high, high)
            self._or_low = low if self._or_low is None else min(self._or_low, low)
            return Action.HOLD
        if self._or_high is None or self._or_low is None:
            return Action.HOLD

        if ctx.position is not None:  # stop at the opposite side of the opening range
            if ctx.position.side is Side.LONG and ctx.bar.close < self._or_low:
                return Action.EXIT
            if ctx.position.side is Side.SHORT and ctx.bar.close > self._or_high:
                return Action.EXIT
            return Action.HOLD

        if self._entered or ctx.minutes_since_open > self.no_entry_after_min:
            return Action.HOLD
        if ctx.bar.close > self._or_high:
            self._entered = True
            return Action.ENTER_LONG
        if ctx.bar.close < self._or_low:
            self._entered = True
            return Action.ENTER_SHORT
        return Action.HOLD


_STRATEGIES: dict[str, Callable[[], IntradayStrategy]] = {
    VwapTrendStrategy.name: VwapTrendStrategy,
    NoiseAreaMomentumStrategy.name: NoiseAreaMomentumStrategy,
    OpeningRangeBreakoutStrategy.name: OpeningRangeBreakoutStrategy,
}


def available() -> list[str]:
    return sorted(_STRATEGIES)


def build(name: str) -> IntradayStrategy:
    """Construct an intraday strategy by name (raises KeyError if unknown)."""
    return _STRATEGIES[name]()
