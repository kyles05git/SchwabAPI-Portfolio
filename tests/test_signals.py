"""Tests for deterministic quant signals (offline; synthetic candles)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from schwab_trader import signals
from schwab_trader.market_data import Candle

START = datetime(2026, 1, 2, tzinfo=UTC)


def _candles(closes: list[float], symbol: str = "AAA") -> list[Candle]:
    return [
        Candle(symbol=symbol, date=START + timedelta(days=i), close=Decimal(str(c)))
        for i, c in enumerate(closes)
    ]


def test_sma_and_change_helpers() -> None:
    closes = [10.0, 12.0, 14.0, 16.0]
    assert signals.sma(closes, 2) == 15.0  # (14+16)/2
    assert signals.sma(closes, 10) is None  # not enough history


def test_momentum_features_need_history() -> None:
    short = _candles([100.0, 101.0, 102.0])
    feats = signals.momentum_features(short)
    assert feats.ret_20 is None  # < 21 sessions
    assert feats.mom_12_1 is None

    rising = _candles([100.0 + i for i in range(300)])
    feats = signals.momentum_features(rising)
    assert feats.ret_20 is not None
    assert feats.ret_20 > 0  # uptrend
    assert feats.mom_12_1 is not None


def test_percentile_ranks_order_and_ties() -> None:
    ranks = signals.percentile_ranks({"a": 1.0, "b": 2.0, "c": 3.0})
    assert ranks["a"] < ranks["b"] < ranks["c"]
    tied = signals.percentile_ranks({"a": 5.0, "b": 5.0})
    assert tied["a"] == tied["b"] == 0.5


def test_momentum_composite_ranks_strong_over_weak() -> None:
    # Strong uptrend vs flat line -> strong ranks higher.
    features = {
        "STRONG": signals.momentum_features(_candles([100.0 + i for i in range(300)], "STRONG")),
        "FLAT": signals.momentum_features(_candles([100.0] * 300, "FLAT")),
    }
    composite = signals.momentum_composite(features)
    assert composite["STRONG"] > composite["FLAT"]


def test_ewma_and_realized_vol_positive() -> None:
    closes = [100.0]
    for i in range(60):  # alternating moves -> real volatility
        closes.append(closes[-1] * (1.02 if i % 2 else 0.98))
    assert signals.ewma_vol(closes) is not None
    assert signals.ewma_vol(closes) > 0
    assert signals.realized_vol(closes, 20) is not None


def test_breadth_fraction_above_ma() -> None:
    up = _candles([100.0 + i for i in range(60)], "UP")  # last close well above SMA
    down = _candles([100.0 - i for i in range(60)], "DOWN")  # last close below SMA
    breadth = signals.breadth_above_ma({"UP": up, "DOWN": down}, 50)
    assert breadth == 0.5


def test_regime_score_bullish_vs_bearish() -> None:
    # Steady uptrend SPY + a universe mostly rising -> high score, full exposure.
    spy_up = _candles([100.0 + i * 0.5 for i in range(300)], "SPY")
    universe_up = {"AAA": _candles([50.0 + i * 0.2 for i in range(300)], "AAA")}
    bull = signals.regime_signal(spy_up, universe_up)
    assert bull.spy_above_200dma is True
    assert bull.trend_50_over_200 is True
    assert bull.score >= 3
    assert bull.gross_exposure_cap >= Decimal("0.80")

    # Steady downtrend -> low score, reduced exposure.
    spy_down = _candles([300.0 - i * 0.5 for i in range(300)], "SPY")
    universe_down = {"AAA": _candles([200.0 - i * 0.2 for i in range(300)], "AAA")}
    bear = signals.regime_signal(spy_down, universe_down)
    assert bear.spy_above_200dma is False
    assert bear.gross_exposure_cap <= Decimal("0.35")


def test_exposure_map_is_monotonic() -> None:
    caps = [signals.EXPOSURE_MAP[s] for s in range(5)]
    assert caps == sorted(caps)  # higher regime score -> higher exposure
