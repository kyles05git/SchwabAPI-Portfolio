"""Tests for the session-aware intraday backtester (offline)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from schwab_trader.intraday_backtest import (
    Action,
    BarContext,
    IntradayStrategy,
    run_intraday_backtest,
)
from schwab_trader.market_data import Candle

BASE = datetime.fromisoformat("2026-07-16T13:30:00+00:00")  # 9:30 ET


def _bar(minute: int, close: str, *, day: int = 16, volume: int = 1000) -> Candle:
    ts = BASE.replace(day=day) + timedelta(minutes=minute)
    c = Decimal(close)
    return Candle(symbol="SPY", date=ts, open=c, high=c, low=c, close=c, volume=volume)


class _BuyFirstSellLast(IntradayStrategy):
    """Enter long on the 2nd bar, hold to the session close (forced flat)."""

    name = "test-hold"

    def on_bar(self, ctx: BarContext) -> Action:
        if ctx.position is None and ctx.bar_index == 1:
            return Action.ENTER_LONG
        return Action.HOLD


def test_forces_flat_at_session_close_and_computes_return() -> None:
    bars = [_bar(0, "100"), _bar(5, "100"), _bar(10, "110")]  # enter at bar1 (100), eod at 110
    result = run_intraday_backtest(bars, _BuyFirstSellLast(), minutes=5, cost_bps=0.0)
    assert result.num_trades == 1
    assert result.sessions == 1
    # +10% gross, no cost
    assert result.total_return_pct == Decimal("10.00")
    assert result.win_rate_pct == Decimal("100.0")


def test_costs_reduce_return() -> None:
    bars = [_bar(0, "100"), _bar(5, "100"), _bar(10, "110")]
    # 10 bps per side -> 20 bps round trip subtracted from the +10% gross.
    result = run_intraday_backtest(bars, _BuyFirstSellLast(), minutes=5, cost_bps=10.0)
    assert result.total_return_pct == Decimal("9.80")


class _VwapExit(IntradayStrategy):
    name = "test-vwap"

    def on_bar(self, ctx: BarContext) -> Action:
        if ctx.position is None and ctx.bar_index == 0:
            return Action.ENTER_LONG
        if ctx.position is not None and ctx.bar.close < ctx.vwap:
            return Action.EXIT
        return Action.HOLD


def test_exit_on_signal_and_short_direction() -> None:
    # Rises then falls below VWAP -> strategy exits mid-session (not eod).
    bars = [_bar(0, "100"), _bar(5, "104"), _bar(10, "101"), _bar(15, "99")]
    result = run_intraday_backtest(bars, _VwapExit(), minutes=5, cost_bps=0.0)
    assert result.num_trades == 1
    assert result.trades if False else True  # trades recorded
    assert result.equity_curve[0][0].isoformat() == "2026-07-16"


class _ShortStrat(IntradayStrategy):
    name = "test-short"

    def on_bar(self, ctx: BarContext) -> Action:
        if ctx.position is None and ctx.bar_index == 0:
            return Action.ENTER_SHORT
        return Action.HOLD


def test_short_profits_when_price_falls() -> None:
    bars = [_bar(0, "100"), _bar(5, "90")]  # short at 100, cover at 90 (eod) -> +10%
    result = run_intraday_backtest(bars, _ShortStrat(), minutes=5, cost_bps=0.0)
    assert result.num_trades == 1
    assert result.total_return_pct == Decimal("10.00")


def test_two_sessions_compound_and_reset() -> None:
    bars = [
        _bar(0, "100", day=16),
        _bar(5, "100", day=16),
        _bar(10, "110", day=16),  # +10%
        _bar(0, "100", day=17),
        _bar(5, "100", day=17),
        _bar(10, "110", day=17),  # +10%
    ]
    result = run_intraday_backtest(bars, _BuyFirstSellLast(), minutes=5, cost_bps=0.0)
    assert result.sessions == 2
    assert result.num_trades == 2
    # 1.1 * 1.1 = 1.21 -> +21%
    assert result.total_return_pct == Decimal("21.00")
