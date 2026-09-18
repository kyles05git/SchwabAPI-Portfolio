"""Deterministic regime classification for the paper-only tactical allocator.

Version 0 deliberately keeps the allocation surface small: SPY exposure is 100%
in a confirmed risk-on regime, 50% in a neutral/transition regime, and 0% in a
confirmed risk-off regime.  Sector rotation and defensive ETFs are later layers;
the first research question is whether a slow SPY/cash router improves terminal
wealth after costs across several market cycles.

The confirmation rule is stateless so a one-shot paper command and a historical
replay make the same decision.  The current raw regime must agree with the raw
regime from five benchmark sessions earlier.  A disagreement resolves to neutral,
which prevents a single observation from flipping directly between risk-on and
risk-off.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from schwab_trader import signals
from schwab_trader.market_data import Candle

MIN_BENCHMARK_BARS = 205
DEFAULT_CONFIRMATION_LAG = 5


class RegimeState(StrEnum):
    """The allocator's three intentionally broad market states."""

    RISK_ON = "risk-on"
    NEUTRAL = "neutral"
    RISK_OFF = "risk-off"


TARGET_EQUITY_WEIGHTS: dict[RegimeState, Decimal] = {
    RegimeState.RISK_ON: Decimal("1.00"),
    RegimeState.NEUTRAL: Decimal("0.50"),
    RegimeState.RISK_OFF: Decimal("0.00"),
}


@dataclass(frozen=True)
class RegimeDecision:
    """Current and confirming observations plus the allocation they authorize."""

    state: RegimeState
    current_state: RegimeState
    confirmation_state: RegimeState
    current_signal: signals.RegimeSignal
    confirmation_signal: signals.RegimeSignal
    as_of: datetime

    @property
    def confirmed(self) -> bool:
        return self.current_state == self.confirmation_state

    @property
    def target_equity_weight(self) -> Decimal:
        return TARGET_EQUITY_WEIGHTS[self.state]


def classify(signal: signals.RegimeSignal) -> RegimeState:
    """Map the transparent four-part score to a conservative raw state.

    Risk-on requires both benchmark trend checks plus at least one of breadth or
    calm volatility.  Risk-off requires both trend checks to be negative.  Mixed
    evidence is neutral regardless of the aggregate score.
    """
    if signal.spy_above_200dma and signal.trend_50_over_200 and signal.score >= 3:
        return RegimeState.RISK_ON
    if not signal.spy_above_200dma and not signal.trend_50_over_200:
        return RegimeState.RISK_OFF
    return RegimeState.NEUTRAL


def _through(candles: list[Candle], cutoff: datetime) -> list[Candle]:
    return [candle for candle in candles if candle.date <= cutoff]


def decide_regime(
    spy_candles: list[Candle],
    universe_candles: dict[str, list[Candle]],
    *,
    confirmation_lag: int = DEFAULT_CONFIRMATION_LAG,
) -> RegimeDecision | None:
    """Return a confirmed as-of regime, or ``None`` when history is insufficient.

    All inputs must already be sliced to information available at the decision
    timestamp.  No future bar is consulted.  A raw-state disagreement between the
    latest observation and the lagged observation resolves to neutral.
    """
    if confirmation_lag < 1:
        msg = "confirmation_lag must be at least 1"
        raise ValueError(msg)
    minimum = 200 + confirmation_lag
    if len(spy_candles) < minimum:
        return None

    current_signal = signals.regime_signal(spy_candles, universe_candles)
    confirmation_cutoff = spy_candles[-1 - confirmation_lag].date
    prior_spy = _through(spy_candles, confirmation_cutoff)
    prior_universe = {
        symbol: _through(candles, confirmation_cutoff)
        for symbol, candles in universe_candles.items()
    }
    if len(prior_spy) < 200:
        return None

    confirmation_signal = signals.regime_signal(prior_spy, prior_universe)
    current_state = classify(current_signal)
    confirmation_state = classify(confirmation_signal)
    state = current_state if current_state == confirmation_state else RegimeState.NEUTRAL
    return RegimeDecision(
        state=state,
        current_state=current_state,
        confirmation_state=confirmation_state,
        current_signal=current_signal,
        confirmation_signal=confirmation_signal,
        as_of=spy_candles[-1].date,
    )


def is_weekly_evaluation_session(now: datetime) -> bool:
    """Version-0 cadence: evaluate Friday after the close (``weekday() == 4``)."""
    return now.weekday() == 4
