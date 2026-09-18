"""Tests for intraday signals and the noise-area / ORB strategies (offline)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from schwab_trader import intraday_signals as sig
from schwab_trader.intraday_backtest import run_intraday_backtest
from schwab_trader.intraday_strategies import (
    NoiseAreaMomentumStrategy,
    OpeningRangeBreakoutStrategy,
)
from schwab_trader.market_data import Candle

BASE = datetime.fromisoformat("2026-07-16T13:30:00+00:00")  # 9:30 ET


def _bar(minute: int, o, h, lo, c, *, day: int = 16, volume: int = 1000) -> Candle:
    ts = BASE.replace(day=day) + timedelta(minutes=minute)
    return Candle(
        symbol="SPY",
        date=ts,
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(lo)),
        close=Decimal(str(c)),
        volume=volume,
    )


def test_true_range_and_atr() -> None:
    assert sig.true_range(Decimal("11"), Decimal("9"), Decimal("10")) == Decimal("2")
    # gap up: prev close 8, bar 11-9 -> TR = max(2, 3, 1) = 3
    assert sig.true_range(Decimal("11"), Decimal("9"), Decimal("8")) == Decimal("3")
    bars = [_bar(i, 100, 101, 99, 100) for i in range(5)]  # TR = 2 each
    assert sig.atr(bars, 3) == Decimal("2")
    assert sig.atr(bars, 10) is None  # not enough bars


def test_opening_range() -> None:
    bars = [
        _bar(0, 100, 102, 99, 101),
        _bar(5, 101, 103, 100, 102),  # outside a 5-min window
    ]
    rng = sig.opening_range(bars, bars[0].date, minutes=5)
    assert rng == (Decimal("102"), Decimal("99"))  # only the first bar (9:30-9:35)


def _flat_session(day: int, base: int) -> list[Candle]:
    """A quiet session with a ~1-wide range (for the noise-area lookback warmup)."""
    return [_bar(i * 5, base, base + 1, base - 1, base, day=day) for i in range(6)]


def test_noise_area_goes_long_on_upside_breakout() -> None:
    strat = NoiseAreaMomentumStrategy(
        band_mult=Decimal("0.5"), lookback_sessions=14, min_sessions=3, warmup_min=0
    )
    # 3 warmup sessions (range ~2 -> avg range 2 -> band_half = 1.0), then a breakout day.
    bars: list[Candle] = []
    for d in (13, 14, 15):
        bars += _flat_session(d, 100)
    # Session day 16 opens at 100; band = 100 +/- 1. A close at 103 breaks out -> long.
    breakout = [
        _bar(0, 100, 100, 100, 100, day=16),
        _bar(5, 100, 103, 100, 103, day=16),  # 103 > 101 upper edge -> long
        _bar(10, 103, 104, 103, 104, day=16),  # still above VWAP -> hold
    ]
    bars += breakout
    # Replay to confirm it enters long that day via the backtester's trade record.
    result = run_intraday_backtest(bars, strat, minutes=5, cost_bps=0.0)
    assert result.num_trades >= 1
    assert result.trades[-1].side == "LONG"
    assert result.trades[-1].return_pct > 0  # 100 -> 104 by EOD


def test_noise_area_stands_aside_without_history() -> None:
    strat = NoiseAreaMomentumStrategy(min_sessions=10, warmup_min=0)
    ctx_bars = _flat_session(16, 100)  # single session, no prior ranges
    result = run_intraday_backtest(ctx_bars, strat, minutes=5, cost_bps=0.0)
    assert result.num_trades == 0  # not enough session history -> no trades


def test_orb_long_breakout_and_range_stop() -> None:
    strat = OpeningRangeBreakoutStrategy(or_minutes=5)
    bars = [
        _bar(0, 100, 101, 99, 100, day=16),  # opening range: 101 / 99 (first 5 min)
        _bar(5, 100, 102, 100, 102, day=16),  # close 102 > 101 -> long
        _bar(10, 102, 103, 101, 103, day=16),  # hold
    ]
    result = run_intraday_backtest(bars, strat, minutes=5, cost_bps=0.0)
    assert result.num_trades == 1
    assert result.trades[0].side == "LONG"


def test_orb_no_trade_without_breakout() -> None:
    strat = OpeningRangeBreakoutStrategy(or_minutes=5)
    bars = [
        _bar(0, 100, 101, 99, 100, day=16),  # OR 101/99
        _bar(5, 100, 100.5, 99.5, 100, day=16),  # stays inside -> no trade
    ]
    result = run_intraday_backtest(bars, strat, minutes=5, cost_bps=0.0)
    assert result.num_trades == 0
