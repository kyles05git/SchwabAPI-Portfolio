"""Deterministic quant signals computed from historical daily candles.

These are the transparent, non-ML overlays from the trading-strategy research:
price/momentum features, EWMA and realized volatility, cross-sectional momentum
ranks, and a 4-part market-regime score that maps to a gross-exposure cap. They
need no ML training and no research-grade data - just the daily candles Schwab
already provides.

Signal values are statistical estimates, so they are computed and returned as
plain ``float`` (money stays ``Decimal`` elsewhere). The one exception is
``gross_exposure_cap``, a portfolio fraction with exact tabulated values, which
is a ``Decimal`` because it multiplies cash. This module performs no network
calls; callers pass in candles.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise

from schwab_trader.market_data import Candle

# Regime score (0-4) -> fraction of the sleeve to deploy (gross exposure cap).
EXPOSURE_MAP: dict[int, Decimal] = {
    4: Decimal("1.00"),
    3: Decimal("0.80"),
    2: Decimal("0.60"),
    1: Decimal("0.35"),
    0: Decimal("0.15"),
}

# Momentum composite: weighted cross-sectional rank of these features.
_MOMENTUM_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("mom_12_1", 0.35),
    ("mom_6_1", 0.30),
    ("ret_60", 0.20),
    ("ret_20", 0.15),
)

_ANNUALIZE = math.sqrt(252)


def _closes(candles: list[Candle]) -> list[float]:
    return [float(candle.close) for candle in candles]


def _log_returns(closes: list[float]) -> list[float]:
    returns: list[float] = []
    for prev, curr in pairwise(closes):
        if prev > 0 and curr > 0:
            returns.append(math.log(curr / prev))
    return returns


def _at(closes: list[float], ago: int) -> float | None:
    """The close ``ago`` sessions before the last one, or None if not available."""
    index = len(closes) - 1 - ago
    return closes[index] if index >= 0 else None


def _change(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator - 1


def sma(closes: list[float], window: int) -> float | None:
    """Simple moving average of the last ``window`` closes, or None."""
    if window <= 0 or len(closes) < window:
        return None
    return sum(closes[-window:]) / window


def realized_vol(closes: list[float], window: int = 20) -> float | None:
    """Annualized realized volatility from the last ``window`` daily log returns."""
    returns = _log_returns(closes)
    if len(returns) < window or window < 2:
        return None
    return statistics.stdev(returns[-window:]) * _ANNUALIZE


def ewma_vol(closes: list[float], *, lam: float = 0.94) -> float | None:
    """Annualized EWMA volatility (RiskMetrics-style), seeded with sample variance."""
    returns = _log_returns(closes)
    if len(returns) < 2:
        return None
    variance = statistics.pvariance(returns)
    for ret in returns:
        variance = lam * variance + (1 - lam) * ret * ret
    return math.sqrt(252 * variance)


@dataclass(frozen=True)
class MomentumFeatures:
    ret_20: float | None
    ret_60: float | None
    mom_6_1: float | None
    mom_12_1: float | None


def momentum_features(candles: list[Candle]) -> MomentumFeatures:
    """Price-momentum features for one symbol (adjusted-close proxy = close)."""
    closes = _closes(candles)
    return MomentumFeatures(
        ret_20=_change(_at(closes, 0), _at(closes, 20)),
        ret_60=_change(_at(closes, 0), _at(closes, 60)),
        mom_6_1=_change(_at(closes, 21), _at(closes, 126)),
        mom_12_1=_change(_at(closes, 21), _at(closes, 252)),
    )


def percentile_ranks(values: dict[str, float]) -> dict[str, float]:
    """Cross-sectional percentile rank in [0, 1] (mid-rank for ties)."""
    n = len(values)
    if n == 0:
        return {}
    if n == 1:
        return {key: 0.5 for key in values}
    items = list(values.values())
    ranks: dict[str, float] = {}
    for symbol, value in values.items():
        less = sum(1 for other in items if other < value)
        equal = sum(1 for other in items if other == value)
        ranks[symbol] = (less + 0.5 * equal) / n
    return ranks


def momentum_composite(features: dict[str, MomentumFeatures]) -> dict[str, float]:
    """Weighted cross-sectional momentum rank in [0, 1] per symbol.

    Only symbols with all four momentum features present are ranked.
    """
    complete = {
        symbol: feats
        for symbol, feats in features.items()
        if all(getattr(feats, name) is not None for name, _ in _MOMENTUM_WEIGHTS)
    }
    if not complete:
        return {}
    composite = dict.fromkeys(complete, 0.0)
    for name, weight in _MOMENTUM_WEIGHTS:
        column = {symbol: float(getattr(feats, name)) for symbol, feats in complete.items()}
        ranks = percentile_ranks(column)
        for symbol in complete:
            composite[symbol] += weight * ranks[symbol]
    return composite


def breadth_above_ma(candles_by_symbol: dict[str, list[Candle]], window: int) -> float:
    """Fraction of symbols trading above their own ``window``-day SMA."""
    total = 0
    above = 0
    for candles in candles_by_symbol.values():
        closes = _closes(candles)
        average = sma(closes, window)
        if average is None:
            continue
        total += 1
        if closes[-1] > average:
            above += 1
    return above / total if total else 0.0


def _rolling_rv20(closes: list[float]) -> list[float]:
    returns = _log_returns(closes)
    series: list[float] = []
    for end in range(20, len(returns) + 1):
        series.append(statistics.stdev(returns[end - 20 : end]) * _ANNUALIZE)
    return series


def _spy_in_calm_regime(spy_closes: list[float]) -> bool:
    """True if SPY's current 20-day vol is below its ~3-year median (calm)."""
    series = _rolling_rv20(spy_closes)
    if len(series) < 2:
        return False
    window = series[-756:]
    return series[-1] < statistics.median(window)


@dataclass(frozen=True)
class RegimeSignal:
    score: int
    gross_exposure_cap: Decimal
    spy_above_200dma: bool
    trend_50_over_200: bool
    breadth_above_50: bool
    calm_volatility: bool
    breadth_pct: float


def regime_signal(
    spy_candles: list[Candle], universe_candles: dict[str, list[Candle]]
) -> RegimeSignal:
    """The 4-part transparent regime score and its gross-exposure cap.

    Score = 1{SPY>200DMA} + 1{50DMA>200DMA} + 1{breadth>50%} + 1{SPY calm}.
    """
    spy = _closes(spy_candles)
    sma_200 = sma(spy, 200)
    sma_50 = sma(spy, 50)
    spy_above_200 = sma_200 is not None and spy[-1] > sma_200
    trend = sma_50 is not None and sma_200 is not None and sma_50 > sma_200
    breadth_pct = breadth_above_ma(universe_candles, 50)
    breadth_ok = breadth_pct > 0.5
    calm = _spy_in_calm_regime(spy)

    score = int(spy_above_200) + int(trend) + int(breadth_ok) + int(calm)
    return RegimeSignal(
        score=score,
        gross_exposure_cap=EXPOSURE_MAP[score],
        spy_above_200dma=spy_above_200,
        trend_50_over_200=trend,
        breadth_above_50=breadth_ok,
        calm_volatility=calm,
        breadth_pct=breadth_pct,
    )
