"""Tests for the multi-symbol relative-volume ORB backtest (offline)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from schwab_trader.market_data import Candle
from schwab_trader.orb_universe import run_universe_orb_backtest

BASE = datetime.fromisoformat("2026-07-01T13:30:00+00:00")  # 9:30 ET


def _bar(day: int, minute: int, o, h, lo, c, volume) -> Candle:
    ts = BASE.replace(day=day) + timedelta(minutes=minute)
    return Candle(
        symbol="X",
        date=ts,
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(lo)),
        close=Decimal(str(c)),
        volume=volume,
    )


def _quiet_session(day: int, *, base: int = 100, vol: int = 1000) -> list[Candle]:
    """A flat session inside a 99-101 range (opening volume = vol)."""
    return [
        _bar(day, 0, base, base + 1, base - 1, base, vol),  # opening range 101/99
        _bar(day, 5, base, base, base, base, vol),
        _bar(day, 10, base, base, base, base, vol),
    ]


def _breakout_up(day: int, *, base: int = 100, vol: int) -> list[Candle]:
    return [
        _bar(day, 0, base, base + 1, base - 1, base, vol),  # OR 101/99
        _bar(day, 5, base, base + 3, base, base + 3, vol),  # close 103 > 101 -> long
        _bar(day, 10, base + 3, base + 4, base + 3, base + 4, vol),  # rides to 104
    ]


def test_selects_high_rvol_name_and_trades_its_breakout() -> None:
    # Symbol A: 3 baseline days then a high-volume breakout day (rvol high) -> traded.
    # Symbol B: normal volume every day (rvol ~1) -> filtered out.
    a = []
    b = []
    for d in (1, 2, 3):
        a += _quiet_session(d, vol=1000)
        b += _quiet_session(d, vol=1000)
    a += _breakout_up(6, vol=5000)  # 5x opening volume -> in play
    b += _quiet_session(6, vol=1000)  # normal -> not in play

    result = run_universe_orb_backtest(
        {"A": a, "B": b},
        minutes=5,
        or_minutes=5,
        top_n=5,
        rvol_lookback=3,
        min_rvol=1.5,
        cost_bps=0.0,
    )
    assert result.num_trades == 1
    assert result.long_trades == 1
    assert result.total_return_pct > 0  # A broke out 100 -> 104
    assert result.universe_size == 2


def test_no_trades_when_nothing_in_play() -> None:
    a = []
    for d in (1, 2, 3, 6):
        a += _quiet_session(d, vol=1000)  # rvol ~1 every day, never elevated
    result = run_universe_orb_backtest(
        {"A": a}, minutes=5, or_minutes=5, rvol_lookback=3, min_rvol=1.5, cost_bps=0.0
    )
    assert result.num_trades == 0
    assert result.sessions == 0  # no session had a selected name


def test_respects_lookback_warmup() -> None:
    # A single high-volume day with no baseline history -> can't rank -> no trade.
    a = _breakout_up(6, vol=5000)
    result = run_universe_orb_backtest(
        {"A": a}, minutes=5, or_minutes=5, rvol_lookback=20, min_rvol=1.5, cost_bps=0.0
    )
    assert result.num_trades == 0


def test_walk_forward_splits_into_folds() -> None:
    from schwab_trader.orb_universe import walk_forward_orb

    # Build enough sessions (baseline + repeated breakout days) to fold.
    a: list[Candle] = []
    for d in range(1, 5):  # baseline
        a += _quiet_session(d, vol=1000)
    for d in range(5, 21):  # 16 high-volume breakout sessions
        a += _breakout_up(d, vol=5000)
    wf = walk_forward_orb(
        {"A": a},
        folds=2,
        minutes=5,
        or_minutes=5,
        top_n=1,
        rvol_lookback=3,
        min_rvol=1.5,
        cost_bps=0.0,
    )
    assert len(wf.folds) == 2
    assert wf.folds[0].sessions + wf.folds[1].sessions == sum(f.sessions for f in wf.folds)
    assert wf.positive_folds >= 1  # breakouts trend up
