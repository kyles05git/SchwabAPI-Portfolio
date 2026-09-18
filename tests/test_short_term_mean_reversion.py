"""Focused tests for ``short-term-mean-reversion-v1`` (issue #94).

Synthetic candles only - no network, no Schwab, no Neon, no `.env`. Every test
constructs its own small universe and daily-history dict; nothing here reads
the frozen 74-name ``large-cap`` preset except the two tests that confirm the
strategy defaults to it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from schwab_trader.agent import MarketContext
from schwab_trader.market_data import Candle, Quote
from schwab_trader.models import OrderSide
from schwab_trader.strategies import contract
from schwab_trader.strategies.short_term_mean_reversion import (
    ENTRY_DIP,
    EXIT_RECOVERY_BAND,
    LONG_MA,
    MAX_POSITION_FRACTION,
    MAX_POSITIONS,
    MINIMUM_PRICE_SESSIONS,
    SHORT_MA,
    ShortTermMeanReversionStrategy,
)

# --- synthetic fixtures --------------------------------------------------

_START = datetime(2025, 1, 1, tzinfo=UTC)
_PLATEAU = 19  # SHORT_MA - 1: the last 20 closes are 19 plateau candles + today's.
_RAMP_LEN = MINIMUM_PRICE_SESSIONS - 1 - _PLATEAU  # fills out to MINIMUM_PRICE_SESSIONS closes


def _candle(symbol: str, when: datetime, close: float | Decimal) -> Candle:
    value = Decimal(close)
    return Candle(
        symbol=symbol, date=when, open=value, high=value, low=value, close=value, volume=1_000_000
    )


def _uptrend_with_final_close(
    symbol: str,
    final_close: float,
    *,
    count: int = MINIMUM_PRICE_SESSIONS,
    plateau: float = 100.0,
    start_date: datetime = _START,
) -> list[Candle]:
    """``count`` closes: a rising ramp, a flat plateau for the short window, then today.

    The last ``SHORT_MA`` closes are (``SHORT_MA`` - 1) copies of ``plateau`` plus
    ``final_close`` - the only two numbers that affect the 20-session average, so
    entry/exit thresholds can be aimed precisely without fighting the ramp. The
    ramp underneath (well below ``plateau``) keeps the 200-session average
    comfortably below ``final_close`` in every scenario used here.
    """
    ramp_len = count - 1 - _PLATEAU
    candles = [
        _candle(symbol, start_date + timedelta(days=i), 40.0 + 0.30 * i) for i in range(ramp_len)
    ]
    candles += [
        _candle(symbol, start_date + timedelta(days=ramp_len + i), plateau) for i in range(_PLATEAU)
    ]
    candles.append(_candle(symbol, start_date + timedelta(days=count - 1), final_close))
    return candles


def _flat(symbol: str, count: int = MINIMUM_PRICE_SESSIONS, value: float = 100.0) -> list[Candle]:
    return [_candle(symbol, _START + timedelta(days=i), value) for i in range(count)]


def _close_for_short_distance(distance: float, *, plateau: float = 100.0) -> float:
    """Solve for the final close giving an exact ``close/short_avg - 1`` distance.

    ``short_avg = ((SHORT_MA - 1) * plateau + x) / SHORT_MA``, and we want
    ``x / short_avg - 1 == distance``. Solving that fixed point:
    ``x = (1 + distance) * (SHORT_MA - 1) * plateau / (SHORT_MA - (1 + distance))``.
    """
    n = SHORT_MA
    factor = 1 + distance
    return factor * (n - 1) * plateau / (n - factor)


_SESSION = _START + timedelta(days=MINIMUM_PRICE_SESSIONS - 1)


def _context(
    *,
    now: datetime = _SESSION,
    cash: Decimal = Decimal("10000"),
    positions: dict[str, int] | None = None,
    universe: tuple[str, ...] = ("AAPL", "MSFT", "GOOG", "AMZN", "META", "NFLX"),
    price: Decimal = Decimal("100.00"),
) -> MarketContext:
    positions = positions or {}
    quotes = {
        symbol: Quote(
            symbol=symbol,
            bid=price - Decimal("0.01"),
            ask=price,
            last=price,
            mark=price,
            quote_time=now,
        )
        for symbol in universe
    }
    equity = cash + sum((Decimal(qty) * price for qty in positions.values()), Decimal(0))
    return MarketContext(now=now, cash=cash, positions=positions, quotes=quotes, equity=equity)


# --- entry ----------------------------------------------------------------


def test_valid_oversold_entry_inside_uptrend() -> None:
    universe = ["AAPL", "MSFT"]
    dip_close = _close_for_short_distance(-0.10)  # comfortably past the 5% threshold
    history = {"AAPL": _uptrend_with_final_close("AAPL", dip_close), "MSFT": _flat("MSFT")}
    strategy = ShortTermMeanReversionStrategy(universe, history=history)
    evaluation = strategy.evaluate(_context(universe=("AAPL", "MSFT")))

    assert evaluation.ok
    assert evaluation.entered == ("AAPL",)
    assert len(evaluation.proposals) == 1
    proposal = evaluation.proposals[0]
    assert proposal.request.side is OrderSide.BUY
    assert proposal.request.symbol == "AAPL"
    assert "below" in proposal.rationale
    assert f"{LONG_MA}-session" in proposal.rationale


def test_oversold_below_long_average_does_not_enter() -> None:
    """Same short-term dip, but the long average has broken - not an uptrend dip."""
    universe = ["AAPL", "MSFT"]
    # A close well below even a flat 100 plateau puts it under the long average too.
    history = {
        "AAPL": _uptrend_with_final_close("AAPL", 50.0, plateau=100.0),
        "MSFT": _flat("MSFT"),
    }
    strategy = ShortTermMeanReversionStrategy(universe, history=history)
    evaluation = strategy.evaluate(_context(universe=("AAPL", "MSFT")))

    assert evaluation.entered == ()
    assert evaluation.proposals == ()


def test_exactly_five_percent_below_qualifies() -> None:
    universe = ["AAPL", "MSFT"]
    close = _close_for_short_distance(-float(ENTRY_DIP))
    history = {"AAPL": _uptrend_with_final_close("AAPL", close), "MSFT": _flat("MSFT")}
    strategy = ShortTermMeanReversionStrategy(universe, history=history)
    evaluation = strategy.evaluate(_context(universe=("AAPL", "MSFT")))
    assert evaluation.entered == ("AAPL",)


def test_just_inside_five_percent_threshold_does_not_qualify() -> None:
    universe = ["AAPL", "MSFT"]
    close = _close_for_short_distance(-float(ENTRY_DIP) + 0.01)  # 4% below, not 5%
    history = {"AAPL": _uptrend_with_final_close("AAPL", close), "MSFT": _flat("MSFT")}
    strategy = ShortTermMeanReversionStrategy(universe, history=history)
    evaluation = strategy.evaluate(_context(universe=("AAPL", "MSFT")))
    assert evaluation.entered == ()


# --- exit -------------------------------------------------------------------


def test_recovery_to_one_percent_band_exits() -> None:
    universe = ["AAPL", "MSFT"]
    close = _close_for_short_distance(-float(EXIT_RECOVERY_BAND))
    history = {"AAPL": _uptrend_with_final_close("AAPL", close), "MSFT": _flat("MSFT")}
    strategy = ShortTermMeanReversionStrategy(universe, history=history)
    evaluation = strategy.evaluate(
        _context(universe=("AAPL", "MSFT"), positions={"AAPL": 10})
    )

    assert evaluation.exited == ("AAPL",)
    assert len(evaluation.proposals) == 1
    proposal = evaluation.proposals[0]
    assert proposal.request.side is OrderSide.SELL
    assert proposal.request.symbol == "AAPL"
    assert "recovered" in proposal.rationale


def test_long_average_break_exits() -> None:
    universe = ["AAPL", "MSFT"]
    history = {
        "AAPL": _uptrend_with_final_close("AAPL", 50.0, plateau=100.0),
        "MSFT": _flat("MSFT"),
    }
    strategy = ShortTermMeanReversionStrategy(universe, history=history)
    evaluation = strategy.evaluate(
        _context(universe=("AAPL", "MSFT"), positions={"AAPL": 10})
    )

    assert evaluation.exited == ("AAPL",)
    proposal = evaluation.proposals[0]
    assert proposal.request.side is OrderSide.SELL
    assert "uptrend broke" in proposal.rationale


def test_held_position_remains_held_without_explicit_exit() -> None:
    """Still oversold and still above the long average: no exit condition fires."""
    universe = ["AAPL", "MSFT"]
    dip_close = _close_for_short_distance(-0.08)
    history = {"AAPL": _uptrend_with_final_close("AAPL", dip_close), "MSFT": _flat("MSFT")}
    strategy = ShortTermMeanReversionStrategy(universe, history=history)
    evaluation = strategy.evaluate(
        _context(universe=("AAPL", "MSFT"), positions={"AAPL": 10})
    )

    assert evaluation.exited == ()
    assert evaluation.retained == ("AAPL",)
    assert evaluation.proposals == ()  # already held, not re-entered or resized


# --- ranking and position limits --------------------------------------------


def test_ranks_by_most_negative_distance_first() -> None:
    universe = ["AAPL", "MSFT", "GOOG"]
    history = {
        "AAPL": _uptrend_with_final_close("AAPL", _close_for_short_distance(-0.06)),
        "MSFT": _uptrend_with_final_close("MSFT", _close_for_short_distance(-0.20)),
        "GOOG": _uptrend_with_final_close("GOOG", _close_for_short_distance(-0.10)),
    }
    strategy = ShortTermMeanReversionStrategy(universe, history=history)
    evaluation = strategy.evaluate(_context(universe=("AAPL", "MSFT", "GOOG")))

    assert evaluation.entered == ("MSFT", "GOOG", "AAPL")
    buys = [p.request.symbol for p in evaluation.proposals if p.request.side is OrderSide.BUY]
    assert buys == ["MSFT", "GOOG", "AAPL"]


def test_ties_break_by_frozen_universe_order() -> None:
    universe = ["GOOG", "AAPL", "MSFT"]
    same_close = _close_for_short_distance(-0.10)
    history = {
        symbol: _uptrend_with_final_close(symbol, same_close) for symbol in universe
    }
    strategy = ShortTermMeanReversionStrategy(universe, history=history)
    evaluation = strategy.evaluate(_context(universe=tuple(universe)))

    assert evaluation.entered == ("GOOG", "AAPL", "MSFT")


_EIGHT_SYMBOLS = ["AAPL", "MSFT", "GOOG", "AMZN", "META", "NFLX", "NVDA", "TSLA"]


def test_no_more_than_five_positions_at_twenty_percent_each() -> None:
    universe = list(_EIGHT_SYMBOLS)
    history = {
        symbol: _uptrend_with_final_close(symbol, _close_for_short_distance(-0.05 - i * 0.01))
        for i, symbol in enumerate(universe)
    }
    strategy = ShortTermMeanReversionStrategy(universe, history=history)
    evaluation = strategy.evaluate(_context(universe=tuple(universe)))

    assert len(evaluation.entered) == MAX_POSITIONS == 5
    buys = [p for p in evaluation.proposals if p.request.side is OrderSide.BUY]
    assert len(buys) == 5
    for order in buys:
        notional = order.request.quantity * order.request.limit_price
        assert notional <= Decimal("10000") * MAX_POSITION_FRACTION


def test_fully_populated_book_approaches_full_exposure() -> None:
    """20% x 5 positions is 100%, unlike the registered 10% default (50% cap)."""
    universe = _EIGHT_SYMBOLS[:5]
    history = {
        symbol: _uptrend_with_final_close(symbol, _close_for_short_distance(-0.05 - i * 0.01))
        for i, symbol in enumerate(universe)
    }
    strategy = ShortTermMeanReversionStrategy(universe, history=history)
    evaluation = strategy.evaluate(
        _context(universe=tuple(universe), cash=Decimal("10000"), price=Decimal("100.00"))
    )

    assert len(evaluation.entered) == 5
    total_notional = sum(
        (p.request.quantity * p.request.limit_price for p in evaluation.proposals), Decimal(0)
    )
    assert total_notional > Decimal("9000")  # comfortably above the old 50% cap


# --- history and coverage ----------------------------------------------------


def test_exactly_minimum_history_works() -> None:
    universe = ["AAPL", "MSFT"]
    dip_close = _close_for_short_distance(-0.10)
    history = {
        "AAPL": _uptrend_with_final_close("AAPL", dip_close, count=MINIMUM_PRICE_SESSIONS),
        "MSFT": _flat("MSFT", count=MINIMUM_PRICE_SESSIONS),
    }
    strategy = ShortTermMeanReversionStrategy(universe, history=history)
    evaluation = strategy.evaluate(_context(universe=("AAPL", "MSFT")))
    assert "AAPL" in evaluation.eligible
    assert evaluation.entered == ("AAPL",)


def test_insufficient_history_is_reported_as_ineligible() -> None:
    universe = ["AAPL", "MSFT"]
    history = {
        "AAPL": _uptrend_with_final_close(
            "AAPL", _close_for_short_distance(-0.10), count=MINIMUM_PRICE_SESSIONS - 1
        ),
        "MSFT": _flat("MSFT", count=MINIMUM_PRICE_SESSIONS),
    }
    strategy = ShortTermMeanReversionStrategy(universe, history=history)
    now = _START + timedelta(days=MINIMUM_PRICE_SESSIONS - 2)
    evaluation = strategy.evaluate(_context(now=now, universe=("AAPL", "MSFT")))

    assert "AAPL" in evaluation.ineligible
    assert "AAPL" not in evaluation.eligible


def test_exactly_sixty_percent_coverage_passes() -> None:
    universe = [f"SYM{i}" for i in range(5)]  # 3/5 = 60% eligible
    history = {}
    for i, symbol in enumerate(universe):
        if i < 3:
            history[symbol] = _flat(symbol)
        else:
            history[symbol] = _flat(symbol, count=MINIMUM_PRICE_SESSIONS - 1)
    strategy = ShortTermMeanReversionStrategy(universe, history=history)
    now = _START + timedelta(days=MINIMUM_PRICE_SESSIONS - 1)
    evaluation = strategy.evaluate(_context(now=now, universe=tuple(universe)))

    assert evaluation.ok
    assert evaluation.coverage == Decimal("3") / Decimal("5")


def test_below_sixty_percent_coverage_fails_closed() -> None:
    universe = [f"SYM{i}" for i in range(5)]  # 2/5 = 40% eligible
    history = {}
    for i, symbol in enumerate(universe):
        if i < 2:
            history[symbol] = _flat(symbol)
        else:
            history[symbol] = _flat(symbol, count=MINIMUM_PRICE_SESSIONS - 1)
    strategy = ShortTermMeanReversionStrategy(universe, history=history)
    now = _START + timedelta(days=MINIMUM_PRICE_SESSIONS - 1)
    evaluation = strategy.evaluate(_context(now=now, universe=tuple(universe)))

    assert not evaluation.ok
    assert evaluation.proposals == ()
    assert "coverage" in evaluation.reason


# --- fail-closed evidence handling -------------------------------------------


def test_no_regime_overlay_applied() -> None:
    """Full gross exposure is used even though the design predates a regime module."""
    universe = ["AAPL", "MSFT"]
    dip_close = _close_for_short_distance(-0.10)
    history = {"AAPL": _uptrend_with_final_close("AAPL", dip_close), "MSFT": _flat("MSFT")}
    strategy = ShortTermMeanReversionStrategy(universe, history=history)
    evaluation = strategy.evaluate(_context(universe=("AAPL", "MSFT")))

    buy = next(p for p in evaluation.proposals if p.request.side is OrderSide.BUY)
    notional = buy.request.quantity * buy.request.limit_price
    # A single position with 5/5 room gets the full 20% cap, not a regime-scaled cap.
    assert notional > Decimal("1900")


def test_stale_evidence_is_ineligible_and_reported() -> None:
    universe = ["AAPL", "MSFT"]
    history = {
        "AAPL": _uptrend_with_final_close("AAPL", _close_for_short_distance(-0.10)),
        "MSFT": _flat("MSFT"),
    }
    strategy = ShortTermMeanReversionStrategy(universe, history=history)
    # A session one day after the last available AAPL/MSFT close.
    now = _START + timedelta(days=MINIMUM_PRICE_SESSIONS)
    evaluation = strategy.evaluate(_context(now=now, universe=("AAPL", "MSFT")))

    assert not evaluation.ok  # both names go stale together -> coverage 0%
    assert "AAPL" in evaluation.ineligible
    assert "MSFT" in evaluation.ineligible


def test_conflicting_evidence_fails_the_whole_sleeve_closed() -> None:
    universe = ["AAPL", "MSFT"]
    good = _uptrend_with_final_close("AAPL", _close_for_short_distance(-0.10))
    conflicting = list(good)
    # A second row for the final session with a different close: a data-integrity fault.
    conflicting.append(_candle("AAPL", conflicting[-1].date, conflicting[-1].close + Decimal(1)))
    history = {"AAPL": conflicting, "MSFT": _flat("MSFT")}
    strategy = ShortTermMeanReversionStrategy(universe, history=history)
    evaluation = strategy.evaluate(_context(universe=("AAPL", "MSFT")))

    assert not evaluation.ok
    assert "conflicting" in evaluation.reason
    assert evaluation.proposals == ()


def test_future_evidence_is_excluded_by_the_as_of_slice() -> None:
    universe = ["AAPL", "MSFT"]
    dip_close = _close_for_short_distance(-0.10)
    history_full = {
        "AAPL": _uptrend_with_final_close("AAPL", dip_close),
        "MSFT": _flat("MSFT"),
    }
    # A future bar appended after the decision session must not affect the decision.
    future_close = _candle("AAPL", _SESSION + timedelta(days=5), Decimal("999"))
    history_with_future = {
        "AAPL": [*history_full["AAPL"], future_close],
        "MSFT": history_full["MSFT"],
    }
    baseline = ShortTermMeanReversionStrategy(universe, history=history_full).evaluate(
        _context(universe=("AAPL", "MSFT"))
    )
    with_future = ShortTermMeanReversionStrategy(universe, history=history_with_future).evaluate(
        _context(universe=("AAPL", "MSFT"))
    )
    assert with_future.entered == baseline.entered
    assert with_future.proposals == baseline.proposals


def test_non_positive_price_is_ineligible() -> None:
    universe = ["AAPL", "MSFT"]
    bad = _uptrend_with_final_close("AAPL", 0.0)
    history = {"AAPL": bad, "MSFT": _flat("MSFT")}
    strategy = ShortTermMeanReversionStrategy(universe, history=history)
    evaluation = strategy.evaluate(_context(universe=("AAPL", "MSFT")))
    assert "AAPL" in evaluation.ineligible


# --- determinism --------------------------------------------------------------


def test_input_ordering_does_not_change_decisions() -> None:
    universe = ["AAPL", "MSFT", "GOOG"]
    history = {
        "AAPL": _uptrend_with_final_close("AAPL", _close_for_short_distance(-0.06)),
        "MSFT": _uptrend_with_final_close("MSFT", _close_for_short_distance(-0.20)),
        "GOOG": _uptrend_with_final_close("GOOG", _close_for_short_distance(-0.10)),
    }
    reordered_history = {"GOOG": history["GOOG"], "AAPL": history["AAPL"], "MSFT": history["MSFT"]}

    forward = ShortTermMeanReversionStrategy(universe, history=history).evaluate(
        _context(universe=("AAPL", "MSFT", "GOOG"))
    )
    reordered = ShortTermMeanReversionStrategy(universe, history=reordered_history).evaluate(
        _context(universe=("AAPL", "MSFT", "GOOG"))
    )

    assert forward.entered == reordered.entered
    assert [(p.request.symbol, p.request.side) for p in forward.proposals] == [
        (p.request.symbol, p.request.side) for p in reordered.proposals
    ]


# --- diagnostics ---------------------------------------------------------------


def test_turnover_diagnostic_counts_orders_without_touching_cost_bps() -> None:
    universe = ["AAPL", "MSFT"]
    dip_close = _close_for_short_distance(-0.10)
    history = {"AAPL": _uptrend_with_final_close("AAPL", dip_close), "MSFT": _flat("MSFT")}
    strategy = ShortTermMeanReversionStrategy(universe, history=history)
    evaluation = strategy.evaluate(_context(universe=("AAPL", "MSFT")))

    assert evaluation.turnover_orders == len(evaluation.proposals) == 1
    # The frozen per-side cost assumption is untouched by this module entirely.
    assert contract.COST_BPS_PER_SIDE == Decimal("5")
    assert contract.COST_BPS_ROUND_TRIP == Decimal("10")


# --- defaults and frozen universe ----------------------------------------------


def test_defaults_to_the_frozen_large_cap_universe() -> None:
    strategy = ShortTermMeanReversionStrategy(history={})
    assert tuple(strategy.universe) == contract.SHORT_TERM_MEAN_REVERSION.universe


def test_parameters_match_the_frozen_contract() -> None:
    sleeve = contract.SHORT_TERM_MEAN_REVERSION
    assert SHORT_MA == sleeve.parameters["short_ma"] == 20
    assert LONG_MA == sleeve.parameters["long_ma"] == 200
    assert ENTRY_DIP == Decimal("0.05")
    assert EXIT_RECOVERY_BAND == Decimal("0.01")
    assert MAX_POSITIONS == 5
    assert MAX_POSITION_FRACTION == Decimal("0.20")
    assert MINIMUM_PRICE_SESSIONS == 201


@pytest.mark.parametrize("count", [MINIMUM_PRICE_SESSIONS])
def test_ramp_fixture_produces_the_expected_close_count(count: int) -> None:
    candles = _uptrend_with_final_close("AAPL", 100.0, count=count)
    assert len(candles) == count
