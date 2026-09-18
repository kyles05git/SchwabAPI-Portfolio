"""Tests for the paper-only tactical regime allocator (synthetic, offline)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from schwab_trader import cli, regime_allocator, signals
from schwab_trader.agent import MarketContext, TacticalRegimeStrategy
from schwab_trader.market_data import Candle, Quote

FRIDAY = datetime(2026, 7, 17, 20, 0, tzinfo=UTC)
THURSDAY = FRIDAY - timedelta(days=1)
START = datetime(2025, 5, 1, tzinfo=UTC)


def _series(symbol: str, closes: list[float]) -> list[Candle]:
    return [
        Candle(symbol=symbol, date=START + timedelta(days=i), close=Decimal(str(close)))
        for i, close in enumerate(closes)
    ]


def _quote(symbol: str, price: str) -> Quote:
    value = Decimal(price)
    return Quote(
        symbol=symbol,
        bid=value,
        ask=value,
        last=value,
        mark=value,
        quote_time=FRIDAY,
    )


def _context(
    cash: str,
    quotes: dict[str, Quote],
    positions: dict[str, int] | None = None,
    *,
    now: datetime = FRIDAY,
) -> MarketContext:
    held = positions or {}
    position_value = sum(
        (
            Decimal(quantity) * (quotes[symbol].mark or Decimal(0))
            for symbol, quantity in held.items()
        ),
        Decimal(0),
    )
    return MarketContext(
        now=now,
        cash=Decimal(cash),
        positions=held,
        quotes=quotes,
        equity=Decimal(cash) + position_value,
    )


def _uptrend_spy() -> list[Candle]:
    return _series("SPY", [400.0 + index * 0.5 for index in range(300)])


def _downtrend_spy() -> list[Candle]:
    return _series("SPY", [600.0 - index * 0.5 for index in range(300)])


def _transition_spy() -> list[Candle]:
    closes = [400.0 + index * 0.5 for index in range(295)]
    closes.extend([500.0, 450.0, 400.0, 350.0, 300.0])
    return _series("SPY", closes)


def test_classify_uses_both_trend_checks() -> None:
    bullish = signals.regime_signal(_uptrend_spy(), {"SPY": _uptrend_spy()})
    bearish = signals.regime_signal(_downtrend_spy(), {"SPY": _downtrend_spy()})
    assert regime_allocator.classify(bullish) is regime_allocator.RegimeState.RISK_ON
    assert regime_allocator.classify(bearish) is regime_allocator.RegimeState.RISK_OFF


def test_regime_requires_one_week_confirmation() -> None:
    spy = _transition_spy()
    decision = regime_allocator.decide_regime(spy, {"SPY": spy})
    assert decision is not None
    assert decision.current_state is not decision.confirmation_state
    assert decision.state is regime_allocator.RegimeState.NEUTRAL
    assert decision.target_equity_weight == Decimal("0.50")
    assert decision.confirmed is False


def test_regime_fails_closed_without_lookback_and_rejects_bad_lag() -> None:
    short = _uptrend_spy()[:204]
    assert regime_allocator.decide_regime(short, {"SPY": short}) is None
    with pytest.raises(ValueError, match="at least 1"):
        regime_allocator.decide_regime(_uptrend_spy(), {}, confirmation_lag=0)


def test_tactical_targets_full_index_exposure_when_risk_on() -> None:
    spy = _uptrend_spy()
    strategy = TacticalRegimeStrategy(["SPY"], history={"SPY": spy}, benchmark_history=spy)
    proposals = strategy.decide(_context("5000", {"SPY": _quote("SPY", "50")}))
    assert len(proposals) == 1
    assert proposals[0].request.side.value == "BUY"
    assert proposals[0].request.quantity == 100
    assert "risk-on" in proposals[0].rationale


def test_tactical_transition_targets_half_index_half_cash() -> None:
    spy = _transition_spy()
    strategy = TacticalRegimeStrategy(["SPY"], history={"SPY": spy}, benchmark_history=spy)
    proposals = strategy.decide(_context("5000", {"SPY": _quote("SPY", "50")}))
    assert len(proposals) == 1
    assert proposals[0].request.side.value == "BUY"
    assert proposals[0].request.quantity == 50
    assert "neutral" in proposals[0].rationale


def test_tactical_moves_fully_to_cash_when_risk_off() -> None:
    spy = _downtrend_spy()
    strategy = TacticalRegimeStrategy(["SPY"], history={"SPY": spy}, benchmark_history=spy)
    proposals = strategy.decide(_context("0", {"SPY": _quote("SPY", "50")}, positions={"SPY": 100}))
    assert len(proposals) == 1
    assert proposals[0].request.side.value == "SELL"
    assert proposals[0].request.quantity == 100
    assert "risk-off" in proposals[0].rationale


def test_tactical_only_evaluates_friday_and_respects_drift_band() -> None:
    spy = _uptrend_spy()
    strategy = TacticalRegimeStrategy(["SPY"], history={"SPY": spy}, benchmark_history=spy)
    quote = {"SPY": _quote("SPY", "50")}
    assert strategy.decide(_context("5000", quote, now=THURSDAY)) == []
    # 96 shares + $200 cash = $5,000; 96% exposure is inside the 5% band.
    assert strategy.decide(_context("200", quote, positions={"SPY": 96})) == []


def test_tactical_rejects_invalid_configuration() -> None:
    spy = _uptrend_spy()
    with pytest.raises(ValueError, match="one risk asset"):
        TacticalRegimeStrategy([], history={}, benchmark_history=spy)
    with pytest.raises(ValueError, match="drift_band"):
        TacticalRegimeStrategy(
            ["SPY"],
            history={"SPY": spy},
            benchmark_history=spy,
            drift_band=Decimal("1"),
        )


def test_tactical_is_available_for_research_but_not_live_routing() -> None:
    assert TacticalRegimeStrategy.name in cli._RULE_STRATEGIES
    assert TacticalRegimeStrategy.name in cli._SLEEVE_STRATEGIES
    assert TacticalRegimeStrategy.name not in cli._LIVE_STRATEGIES
