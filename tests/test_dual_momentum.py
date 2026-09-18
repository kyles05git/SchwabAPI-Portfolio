"""``dual-momentum-v1`` obeys the frozen contract and fails closed on bad evidence.

Everything here is synthetic: hand-built candles, hand-built quotes, and a
hand-built market context. Nothing touches a network, a credential, `.env`, a
real database, a broker, or an order path, and no test can submit an order.

The suite is organized as the contract is: what the strategy selects (§5.3),
when it refuses to select anything (§7), how it sizes what it selected (§2), and
that adding it changed no frozen identity (§8).
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from schwab_trader import agent, signals
from schwab_trader.agent import MarketContext
from schwab_trader.market_data import Candle, Quote
from schwab_trader.models import OrderSide
from schwab_trader.strategies import contract
from schwab_trader.strategies import dual_momentum as dm

_MODULE = Path(dm.__file__)
_REPO_ROOT = Path(agent.__file__).parents[2]

#: The signal session every test decides on.
SESSION = date(2026, 7, 31)

#: A baseline where SPY wins outright and the absolute gate passes.
BASELINE: dict[str, float] = {"SPY": 0.10, "EFA": 0.05, "EEM": 0.02, "VNQ": 0.01, "IEF": 0.03}


# --- synthetic evidence -------------------------------------------------------


def _candle(symbol: str, session: date, close: Decimal) -> Candle:
    """One daily bar, stamped inside ``session`` in UTC."""
    return Candle(
        symbol=symbol,
        date=datetime.combine(session, time(20, 0), tzinfo=UTC),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1_000_000,
    )


def _series(
    symbol: str,
    total_return: float | str,
    *,
    closes: int = dm.MINIMUM_CLOSES,
    end: date = SESSION,
    base: Decimal = Decimal("100"),
) -> list[Candle]:
    """``closes`` bars ending on ``end`` whose first-to-last return is exact.

    Every bar but the last sits at ``base``, so the trailing return is decided
    purely by the final close and a test can state the return it wants directly.
    A flat series therefore returns exactly ``0.0``, which is what the strict
    ``> 0`` gate has to be tested against.
    """
    last = base * (Decimal("1") + Decimal(str(total_return)))
    sessions = [end - timedelta(days=closes - 1 - offset) for offset in range(closes)]
    values = [*([base] * (closes - 1)), last]
    return [_candle(symbol, s, v) for s, v in zip(sessions, values, strict=True)]


def _history(returns: dict[str, float] | None = None, **kwargs: object) -> dict[str, list[Candle]]:
    """A complete, current five-symbol history."""
    resolved = BASELINE if returns is None else returns
    return {symbol: _series(symbol, value, **kwargs) for symbol, value in resolved.items()}  # type: ignore[arg-type]


def _quote(symbol: str, price: str = "100.00") -> Quote:
    value = Decimal(price)
    return Quote(
        symbol=symbol,
        bid=value,
        ask=value,
        last=value,
        mark=value,
        quote_time=datetime.combine(SESSION, time(20, 0), tzinfo=UTC),
    )


def _context(
    *,
    cash: Decimal = Decimal("10000"),
    positions: dict[str, int] | None = None,
    equity: Decimal | None = None,
    leverage: Decimal = Decimal("1"),
    buying_power: Decimal | None = None,
    now: datetime | None = None,
) -> MarketContext:
    held = positions or {}
    quotes = {symbol: _quote(symbol) for symbol in dm.REQUIRED_SYMBOLS}
    marked = sum((Decimal(qty) * Decimal("100.00") for qty in held.values()), Decimal(0))
    return MarketContext(
        now=now or datetime.combine(SESSION, time(20, 0), tzinfo=UTC),
        cash=cash,
        positions=held,
        quotes=quotes,
        equity=cash + marked if equity is None else equity,
        buying_power=buying_power,
        leverage=leverage,
    )


def _strategy(history: dict[str, list[Candle]] | None = None) -> dm.DualMomentumStrategy:
    return dm.DualMomentumStrategy(history=history if history is not None else _history())


# --- selection: relative momentum ---------------------------------------------


@pytest.mark.parametrize("winner", ["SPY", "EFA", "EEM", "VNQ"])
def test_each_risk_asset_can_win(winner: str) -> None:
    """Every member of the frozen universe is genuinely reachable as the target."""
    returns = {symbol: 0.01 for symbol in dm.RISK_UNIVERSE}
    returns[winner] = 0.25
    returns["IEF"] = 0.03

    decision = dm.evaluate(_history(returns), session=SESSION)

    assert decision.ok
    assert decision.winner == winner
    assert decision.target == winner
    assert decision.ranking[0] == winner
    assert decision.gate_passed


def test_ranking_is_descending_by_trailing_return() -> None:
    decision = dm.evaluate(_history(), session=SESSION)
    assert decision.ranking == ("SPY", "EFA", "EEM", "VNQ")
    scored = [decision.score(symbol) for symbol in decision.ranking]
    returns = [s.trailing_return for s in scored if s is not None]
    assert returns == sorted(returns, reverse=True)


def test_the_defensive_asset_never_competes_for_the_win() -> None:
    """IEF must be scored - it is required evidence - but it cannot be ranked."""
    returns = {**{s: 0.01 for s in dm.RISK_UNIVERSE}, "IEF": 0.99}
    decision = dm.evaluate(_history(returns), session=SESSION)

    assert "IEF" not in decision.ranking
    assert decision.target == "SPY"
    assert decision.score("IEF") is not None


# --- selection: the absolute-momentum gate ------------------------------------


def test_negative_winning_return_holds_the_defensive_asset() -> None:
    returns = {"SPY": -0.05, "EFA": -0.10, "EEM": -0.20, "VNQ": -0.30, "IEF": 0.02}
    decision = dm.evaluate(_history(returns), session=SESSION)

    assert decision.winner == "SPY"  # still the best risk asset
    assert not decision.gate_passed
    assert decision.target == dm.DEFENSIVE_ASSET == "IEF"


def test_zero_winning_return_does_not_pass_the_strict_gate() -> None:
    """A flat year is not an uptrend: the gate is ``> 0``, not ``>= 0``."""
    returns = {**{symbol: 0.0 for symbol in dm.RISK_UNIVERSE}, "IEF": 0.02}
    decision = dm.evaluate(_history(returns), session=SESSION)

    winner = decision.score(decision.ranking[0])
    assert winner is not None
    assert winner.trailing_return == 0.0  # exactly zero, not merely close to it
    assert not decision.gate_passed
    assert decision.target == "IEF"


def test_a_barely_positive_return_does_pass_the_gate() -> None:
    returns = {**{symbol: 0.0 for symbol in dm.RISK_UNIVERSE}, "SPY": 0.0001, "IEF": 0.02}
    decision = dm.evaluate(_history(returns), session=SESSION)

    assert decision.gate_passed
    assert decision.target == "SPY"


def test_the_gate_is_read_from_the_frozen_contract() -> None:
    assert dm.ABSOLUTE_GATE_MINIMUM == 0.0
    assert dm.SLEEVE.parameters["absolute_gate_minimum"] == "0"
    assert dm.SLEEVE.parameters["absolute_gate_metric"] == "trailing-price-return"


# --- selection: determinism ----------------------------------------------------


def test_a_four_way_tie_is_broken_by_frozen_universe_order() -> None:
    returns = {**{symbol: 0.07 for symbol in dm.RISK_UNIVERSE}, "IEF": 0.03}
    decision = dm.evaluate(_history(returns), session=SESSION)

    assert decision.ranking == dm.RISK_UNIVERSE == ("SPY", "EFA", "EEM", "VNQ")
    assert decision.target == "SPY"


def test_a_partial_tie_is_broken_by_frozen_universe_order() -> None:
    """EFA and EEM tie for the win; EFA precedes EEM in the frozen order."""
    returns = {"SPY": 0.01, "EFA": 0.20, "EEM": 0.20, "VNQ": 0.02, "IEF": 0.03}
    decision = dm.evaluate(_history(returns), session=SESSION)

    assert decision.ranking == ("EFA", "EEM", "VNQ", "SPY")
    assert decision.target == "EFA"


def test_provider_ordering_does_not_alter_the_result() -> None:
    """A mapping's iteration order is an accident and must not select anything."""
    returns = {**{symbol: 0.07 for symbol in dm.RISK_UNIVERSE}, "IEF": 0.07}
    forward = _history(returns)
    reversed_order = dict(reversed(list(forward.items())))
    rotated = {symbol: forward[symbol] for symbol in ("VNQ", "IEF", "SPY", "EEM", "EFA")}

    first = dm.evaluate(forward, session=SESSION)
    assert first == dm.evaluate(reversed_order, session=SESSION)
    assert first == dm.evaluate(rotated, session=SESSION)


def test_evaluation_is_repeatable() -> None:
    history = _history()
    assert dm.evaluate(history, session=SESSION) == dm.evaluate(history, session=SESSION)


def test_extra_symbols_are_ignored_rather_than_ranked() -> None:
    history = {**_history(), "QQQ": _series("QQQ", 0.99), "BIL": _series("BIL", 0.99)}
    decision = dm.evaluate(history, session=SESSION)

    assert decision.target == "SPY"
    assert {score.symbol for score in decision.scores} == set(dm.REQUIRED_SYMBOLS)


# --- history length ------------------------------------------------------------


def test_exactly_253_closes_is_enough() -> None:
    decision = dm.evaluate(_history(closes=253), session=SESSION)

    assert decision.ok
    assert all(score.sessions == 252 for score in decision.scores)


def test_252_closes_fails_closed() -> None:
    decision = dm.evaluate(_history(closes=252), session=SESSION)

    assert not decision.ok
    assert decision.target is None
    assert {fault.reason for fault in decision.faults} == {dm.INSUFFICIENT_HISTORY}
    assert len(decision.faults) == len(dm.REQUIRED_SYMBOLS)


@pytest.mark.parametrize("symbol", ["SPY", "EFA", "EEM", "VNQ", "IEF"])
def test_one_short_history_fails_the_whole_sleeve_closed(symbol: str) -> None:
    """A four-name universe cannot absorb a missing member; the floor is 100%."""
    history = _history()
    history[symbol] = _series(symbol, BASELINE[symbol], closes=252)

    decision = dm.evaluate(history, session=SESSION)

    assert not decision.ok
    assert [fault.symbol for fault in decision.faults] == [symbol]
    assert decision.faults[0].reason == dm.INSUFFICIENT_HISTORY
    assert decision.coverage == Decimal(4) / Decimal(5)


def test_more_than_the_minimum_history_uses_only_the_trailing_window() -> None:
    """A 252-session return is measured from the 253rd-latest close, not the first."""
    long_history = _history(closes=400)
    decision = dm.evaluate(long_history, session=SESSION)
    short = dm.evaluate(_history(closes=253), session=SESSION)

    assert decision.ok
    assert [score.trailing_return for score in decision.scores] == [
        score.trailing_return for score in short.scores
    ]


# --- missing, stale, and future evidence ---------------------------------------


@pytest.mark.parametrize("symbol", ["SPY", "EFA", "EEM", "VNQ", "IEF"])
def test_missing_history_for_any_symbol_fails_closed(symbol: str) -> None:
    history = _history()
    del history[symbol]

    decision = dm.evaluate(history, session=SESSION)

    assert not decision.ok
    assert decision.target is None
    assert [(f.symbol, f.reason) for f in decision.faults] == [(symbol, dm.MISSING_HISTORY)]


@pytest.mark.parametrize("symbol", ["SPY", "EFA", "EEM", "VNQ", "IEF"])
def test_stale_evidence_for_any_symbol_fails_closed(symbol: str) -> None:
    """Staleness tolerance is zero sessions: yesterday's close is not evidence."""
    history = _history()
    history[symbol] = _series(symbol, BASELINE[symbol], end=SESSION - timedelta(days=1))

    decision = dm.evaluate(history, session=SESSION)

    assert not decision.ok
    assert [(f.symbol, f.reason) for f in decision.faults] == [(symbol, dm.STALE_EVIDENCE)]


def test_future_evidence_cannot_enter_the_decision() -> None:
    """A bar dated after session T is excluded, not used and not fallen back on."""
    clean = _history()
    contaminated = {symbol: list(candles) for symbol, candles in clean.items()}
    contaminated["VNQ"].append(_candle("VNQ", SESSION + timedelta(days=1), Decimal("9999")))

    honest = dm.evaluate(clean, session=SESSION)
    guarded = dm.evaluate(contaminated, session=SESSION)

    assert guarded == honest
    assert guarded.target == "SPY"  # not VNQ, despite the enormous future close


def test_a_history_that_is_entirely_in_the_future_fails_closed() -> None:
    history = _history()
    history["EEM"] = _series("EEM", 0.05, end=SESSION + timedelta(days=400))

    decision = dm.evaluate(history, session=SESSION)

    assert not decision.ok
    assert [(f.symbol, f.reason) for f in decision.faults] == [("EEM", dm.MISSING_HISTORY)]


def test_an_earlier_session_reproduces_that_sessions_decision() -> None:
    """Passing a full history with an earlier session is a no-look-ahead replay."""
    earlier = SESSION - timedelta(days=1)
    history = {symbol: _series(symbol, value, closes=400) for symbol, value in BASELINE.items()}

    replay = dm.evaluate(history, session=earlier)

    assert replay.session == earlier
    assert all(score.last_session == earlier for score in replay.scores)


# --- malformed and conflicting evidence ----------------------------------------


def test_conflicting_closes_for_one_session_fail_the_sleeve_closed() -> None:
    """Two different values for the same session is a fault, not a gap to resolve."""
    history = _history()
    history["EFA"].append(_candle("EFA", SESSION, Decimal("175")))

    decision = dm.evaluate(history, session=SESSION)

    assert not decision.ok
    assert [(f.symbol, f.reason) for f in decision.faults] == [("EFA", dm.CONFLICTING_EVIDENCE)]


def test_duplicate_closes_for_one_session_fail_the_sleeve_closed() -> None:
    history = _history()
    history["SPY"].append(history["SPY"][-1].model_copy())

    decision = dm.evaluate(history, session=SESSION)

    assert not decision.ok
    assert [(f.symbol, f.reason) for f in decision.faults] == [("SPY", dm.DUPLICATE_EVIDENCE)]


@pytest.mark.parametrize("bad", ["0", "-12.50"])
def test_non_positive_closes_fail_the_sleeve_closed(bad: str) -> None:
    history = _history()
    history["EEM"][100] = _candle("EEM", history["EEM"][100].date.date(), Decimal(bad))

    decision = dm.evaluate(history, session=SESSION)

    assert not decision.ok
    assert [(f.symbol, f.reason) for f in decision.faults] == [("EEM", dm.INVALID_CLOSE)]


def test_non_finite_closes_fail_the_sleeve_closed() -> None:
    history = _history()
    corrupt = history["VNQ"][50]
    history["VNQ"][50] = Candle.model_construct(
        symbol="VNQ", date=corrupt.date, close=Decimal("NaN"), volume=1
    )

    decision = dm.evaluate(history, session=SESSION)

    assert not decision.ok
    assert [(f.symbol, f.reason) for f in decision.faults] == [("VNQ", dm.INVALID_CLOSE)]


def test_a_naive_timestamp_is_malformed_rather_than_guessed_at() -> None:
    history = _history()
    history["IEF"][-1] = Candle.model_construct(
        symbol="IEF",
        date=datetime.combine(SESSION, time(20, 0)),
        close=Decimal("100"),
        volume=1,
    )

    decision = dm.evaluate(history, session=SESSION)

    assert not decision.ok
    assert [(f.symbol, f.reason) for f in decision.faults] == [("IEF", dm.MALFORMED_TIMESTAMP)]


def test_an_ambiguous_decision_timestamp_fails_closed() -> None:
    strategy = _strategy()
    naive = datetime.combine(SESSION, time(20, 0))

    decision = strategy.evaluate(_context(now=naive))

    assert not decision.ok
    assert decision.faults[0].reason == dm.AMBIGUOUS_DECISION_TIME
    assert strategy.decide(_context(now=naive)) == []


def test_every_fault_is_reported_rather_than_dropped() -> None:
    history = _history()
    del history["EEM"]
    history["VNQ"] = _series("VNQ", 0.01, closes=10)

    decision = dm.evaluate(history, session=SESSION)

    # Reported in frozen universe order, not in whatever order the faults arose.
    assert [(f.symbol, f.reason) for f in decision.faults] == [
        ("EEM", dm.MISSING_HISTORY),
        ("VNQ", dm.INSUFFICIENT_HISTORY),
    ]
    assert decision.coverage == Decimal(3) / Decimal(5)
    assert "EEM" in decision.rationale and "VNQ" in decision.rationale


# --- orders --------------------------------------------------------------------


def test_a_flat_sleeve_buys_the_target_at_full_weight() -> None:
    proposals = _strategy().decide(_context(cash=Decimal("10000")))

    assert len(proposals) == 1
    only = proposals[0]
    assert only.request.side is OrderSide.BUY
    assert only.request.symbol == "SPY"
    assert only.request.quantity == 100  # $10,000 at a $100.00 marketable limit
    assert only.request.limit_price == Decimal("100.00")


def test_no_order_is_proposed_when_the_target_is_already_held() -> None:
    """The 252-session signal barely moves; an unchanged target must not churn."""
    context = _context(cash=Decimal("0"), positions={"SPY": 100})
    assert _strategy().decide(context) == []


def test_rotation_sells_the_holding_that_is_no_longer_the_target() -> None:
    returns = {"SPY": 0.01, "EFA": 0.30, "EEM": 0.02, "VNQ": 0.03, "IEF": 0.02}
    context = _context(cash=Decimal("0"), positions={"SPY": 100})

    proposals = _strategy(_history(returns)).decide(context)

    assert [(p.request.side, p.request.symbol) for p in proposals] == [(OrderSide.SELL, "SPY")]
    assert proposals[0].request.quantity == 100


def test_rotation_sells_before_it_buys_the_new_target() -> None:
    returns = {"SPY": 0.01, "EFA": 0.30, "EEM": 0.02, "VNQ": 0.03, "IEF": 0.02}
    context = _context(cash=Decimal("10000"), positions={"SPY": 50})

    proposals = _strategy(_history(returns)).decide(context)

    assert [(p.request.side, p.request.symbol) for p in proposals] == [
        (OrderSide.SELL, "SPY"),
        (OrderSide.BUY, "EFA"),
    ]


def test_defensive_rotation_moves_a_risk_holding_into_the_defensive_asset() -> None:
    returns = {"SPY": -0.05, "EFA": -0.10, "EEM": -0.20, "VNQ": -0.30, "IEF": 0.02}
    context = _context(cash=Decimal("10000"), positions={"SPY": 50})

    proposals = _strategy(_history(returns)).decide(context)

    assert [(p.request.side, p.request.symbol) for p in proposals] == [
        (OrderSide.SELL, "SPY"),
        (OrderSide.BUY, "IEF"),
    ]


def test_holding_the_defensive_asset_through_a_bear_market_proposes_nothing() -> None:
    returns = {"SPY": -0.05, "EFA": -0.10, "EEM": -0.20, "VNQ": -0.30, "IEF": 0.02}
    context = _context(cash=Decimal("0"), positions={"IEF": 100})

    assert _strategy(_history(returns)).decide(context) == []


def test_failing_closed_proposes_no_orders_even_while_holding() -> None:
    """A sleeve that cannot decide does not liquidate either; it does nothing."""
    history = _history()
    del history["IEF"]
    context = _context(cash=Decimal("10000"), positions={"SPY": 50})

    assert _strategy(history).decide(context) == []


# --- portfolio constraints ------------------------------------------------------


def test_sizing_is_long_only_and_whole_share() -> None:
    context = _context(cash=Decimal("10000"))
    quotes = {symbol: _quote(symbol, "333.33") for symbol in dm.REQUIRED_SYMBOLS}
    context = MarketContext(
        now=context.now, cash=context.cash, positions={}, quotes=quotes, equity=context.equity
    )

    proposals = _strategy().decide(context)

    assert len(proposals) == 1
    order = proposals[0]
    assert order.request.side is OrderSide.BUY
    assert order.request.quantity == 30  # 10000 // 333.33; the remainder stays in cash
    assert order.request.quantity == int(order.request.quantity)
    assert order.request.quantity * order.request.limit_price <= Decimal("10000")


def test_a_margin_context_cannot_lever_the_sleeve() -> None:
    """challenger-v1 is unlevered whatever account the sleeve is run against."""
    levered = _context(
        cash=Decimal("10000"), leverage=Decimal("2"), buying_power=Decimal("20000")
    )
    unlevered = _context(cash=Decimal("10000"))

    assert contract.LEVERAGE == Decimal("1")
    assert not contract.LEVERAGE_ALLOWED
    assert _strategy().decide(levered)[0].request.quantity == 100
    assert _strategy().decide(unlevered)[0].request.quantity == 100


def test_at_most_one_position_is_ever_targeted() -> None:
    for returns in (BASELINE, {**BASELINE, "SPY": -0.5, "EFA": -0.5, "EEM": -0.5, "VNQ": -0.5}):
        decision = dm.evaluate(_history(returns), session=SESSION)
        assert decision.target is not None
        assert dm.SLEEVE.max_positions == 1
        assert dm.SLEEVE.max_position_fraction == Decimal("1.00")


def test_the_regime_overlay_is_not_applied() -> None:
    """The pinned 100% gross cap is the point: each sleeve tests exactly one idea.

    The same flat history scores a reduced regime cap, so a sleeve that consulted
    ``signals.regime_signal`` would deploy a fraction of the sleeve here.
    """
    history = _history()
    regime = signals.regime_signal(history["SPY"], history)
    assert regime.gross_exposure_cap < Decimal("1.0")  # the overlay would shrink the order

    proposals = _strategy(history).decide(_context(cash=Decimal("10000")))

    assert contract.GROSS_EXPOSURE_CAP == Decimal("1.00")
    assert proposals[0].request.quantity == 100  # full weight, unscaled
    assert "regime" not in proposals[0].rationale


# --- rationale ------------------------------------------------------------------


def test_the_rationale_exposes_the_scores_the_gate_and_the_selection() -> None:
    proposals = _strategy().decide(_context(cash=Decimal("10000")))
    rationale = proposals[0].rationale

    assert dm.STRATEGY_NAME in rationale
    assert str(SESSION) in rationale
    for symbol in dm.REQUIRED_SYMBOLS:
        assert symbol in rationale
    assert "+10.00%" in rationale  # SPY's trailing return
    assert "absolute gate SPY +10.00% > 0% passed" in rationale
    assert "target SPY at 100%" in rationale


def test_the_rationale_records_a_failed_gate() -> None:
    returns = {"SPY": -0.05, "EFA": -0.10, "EEM": -0.20, "VNQ": -0.30, "IEF": 0.02}
    decision = dm.evaluate(_history(returns), session=SESSION)

    assert "absolute gate SPY -5.00% > 0% failed" in decision.rationale
    assert "target IEF at 100%" in decision.rationale


def test_the_rationale_records_why_a_sleeve_failed_closed() -> None:
    history = _history()
    del history["EFA"]
    decision = dm.evaluate(history, session=SESSION)

    assert "failed closed" in decision.rationale
    assert "coverage 4/5" in decision.rationale
    assert "EFA missing-history" in decision.rationale
    assert "proposing no orders" in decision.rationale


def test_rationales_carry_no_rich_markup() -> None:
    """Square brackets are parsed as style tags and silently deleted by Rich."""
    decision = dm.evaluate(_history(), session=SESSION)
    assert "[" not in decision.rationale and "]" not in decision.rationale


# --- contract compliance ---------------------------------------------------------


def test_the_module_reads_the_frozen_contract_rather_than_re_typing_it() -> None:
    sleeve = contract.sleeve("dual-momentum-v1")

    assert dm.SLEEVE is sleeve
    assert dm.STRATEGY_NAME == "dual-momentum-v1"
    assert dm.STRATEGY_VERSION == "1"
    assert dm.DualMomentumStrategy.name == "dual-momentum-v1"
    assert dm.RISK_UNIVERSE == ("SPY", "EFA", "EEM", "VNQ")
    assert dm.DEFENSIVE_ASSET == "IEF"
    assert dm.REQUIRED_SYMBOLS == ("SPY", "EFA", "EEM", "VNQ", "IEF")
    assert dm.LOOKBACK_SESSIONS == 252
    assert dm.MINIMUM_CLOSES == 253 == dm.LOOKBACK_SESSIONS + 1
    assert sleeve.coverage_floor == Decimal("1")
    assert sleeve.owner == "#92"
    assert sleeve.module == "schwab_trader/strategies/dual_momentum.py"


def test_the_strategy_universe_covers_every_symbol_it_may_have_to_trade() -> None:
    """The runner quotes ``universe``; without IEF the defensive buy is unpriceable."""
    assert _strategy().universe == list(dm.REQUIRED_SYMBOLS)


@pytest.mark.parametrize(
    "universe",
    [None, ["SPY", "EFA", "EEM", "VNQ"], ["spy", "efa", "eem", "vnq", "ief"]],
)
def test_the_frozen_universe_is_accepted_in_either_spelling(universe: list[str] | None) -> None:
    assert dm.DualMomentumStrategy(universe, history=_history()).universe == list(
        dm.REQUIRED_SYMBOLS
    )


@pytest.mark.parametrize(
    "universe",
    [
        ["SPY", "EFA", "EEM"],  # dropped a member
        ["SPY", "EFA", "EEM", "VNQ", "BIL"],  # substituted the defensive asset
        ["EFA", "SPY", "EEM", "VNQ"],  # reordered, which would move the tie-break
        ["AAPL", "MSFT"],
    ],
)
def test_a_different_universe_is_refused_rather_than_silently_run(universe: list[str]) -> None:
    """``-v1`` always means what the contract says; a different set is ``-v2``."""
    with pytest.raises(ValueError, match="frozen universe"):
        dm.DualMomentumStrategy(universe, history=_history())


def test_no_total_return_or_t_bill_substitute_is_introduced() -> None:
    """The gate compares against zero and the defensive asset is IEF, by contract.

    Swept across risk-on, mixed, and outright-bear evidence, the only symbol the
    sleeve will ever name is one of the five frozen ones - so no BIL, cash proxy,
    or dividend-adjusted alternative can have crept in as a target.
    """
    assert dm.SLEEVE.defensive_universe == ("IEF",)
    assert dm.SLEEVE.parameters["ranking_metric"] == "trailing-price-return"
    assert dm.SLEEVE.parameters["absolute_gate_metric"] == "trailing-price-return"

    scenarios = [
        BASELINE,
        {"SPY": -0.05, "EFA": -0.10, "EEM": -0.20, "VNQ": -0.30, "IEF": 0.02},
        {"SPY": 0.0, "EFA": 0.0, "EEM": 0.0, "VNQ": 0.0, "IEF": -0.40},
        {"SPY": -0.01, "EFA": 0.60, "EEM": -0.99, "VNQ": 0.03, "IEF": 0.00},
    ]
    for returns in scenarios:
        decision = dm.evaluate(_history(returns), session=SESSION)
        assert decision.target in dm.REQUIRED_SYMBOLS
        assert {score.symbol for score in decision.scores} == set(dm.REQUIRED_SYMBOLS)


# --- frozen identities are untouched ----------------------------------------------

FROZEN_CONTRACT_HASH = "47965e0f12dede2e74ba7100276eeafc09d9a7ae8577788d8906f5594f9bd981"

#: The seven strategy definitions shared by the July 27 incident cohort
#: (``paper-first-2026-07-27``) and the July 28 cohort (``paper-first-2026-07-28``),
#: both of which are running evidence. Adding a challenger module must not move them.
JULY_COHORT_HASHES = {
    "control-cash": "150d0d01fb2b5de052896de6683436a7ea81dbd7bf0a354cc7809ca68150b11e",
    "bench-spy": "1852156d8194ee4b707f791167cfda04bf09ee66b2eac239ceb9650e6e9ae243",
    "sector-momentum": "1570b08dd56bd96d2d9de8e9ec8987731b270594eea20343c1c5366785643a95",
    "trend-large": "021bd967366ee64ff6fab1bfe71b707fbb716a2014d4142bb7231ab1cd5f1738",
    "low-vol-large": "c5d8bc25e7fa26d1ff243de1eea9daf54c89b4f14afdcab6265f86d988d791a2",
    "momentum-large": "9d7c5ae22d1b90a6aeb8d667e6104c8f7e897191ef7cc8549697dbe4279aae00",
    "value-momentum-edgar": "2876fbe4e926764b71972a1ebb7a8ec1e83277daf9e1b1b67b49b37e70543bdc",
}


def test_the_frozen_contract_hash_is_unchanged() -> None:
    assert contract.contract_hash() == FROZEN_CONTRACT_HASH


def test_the_july_cohort_definition_hashes_are_unchanged() -> None:
    """This module adds a strategy; it must redefine no running experiment."""
    path = _REPO_ROOT / "scripts" / "bootstrap_paper_cohort.py"
    spec = importlib.util.spec_from_file_location("bootstrap_script_for_dual_momentum", path)
    assert spec is not None and spec.loader is not None
    bootstrap = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = bootstrap
    spec.loader.exec_module(bootstrap)

    actual = {
        spec_.name: spec_.definition.configuration_hash
        for spec_ in (bootstrap._make_spec(t) for t in bootstrap._TEMPLATES)
    }
    assert actual == JULY_COHORT_HASHES


def test_the_challenger_sleeve_is_registered_through_the_integration_adapter() -> None:
    """#95 registered this sleeve, and did so without reaching into this module.

    The original form of this test asserted the sleeve was *not* registered, which was
    #92's way of stating that registration belonged to the integration issue. #95 has
    now done it, so the assertion inverts: what must still hold is that the registered
    implementation is the adapter in ``challenger_strategies``, never this module's own
    class. ``test_the_module_imports_nothing_beyond_its_declared_dependencies`` below is
    the other half — this module still imports no registry, CLI, or storage.
    """
    from schwab_trader import challenger_strategies, strategy_registry

    assert dm.STRATEGY_NAME in strategy_registry.paper_strategy_names()
    entry = strategy_registry.entry(dm.STRATEGY_NAME)
    assert entry.implementation is challenger_strategies.DualMomentumV1Strategy
    # The frozen sleeve is paper-only: never offered to the rule-backtest or live paths.
    assert not entry.in_rule_backtest
    assert not entry.in_live
    # The generic sleeve knobs must not be able to reach a frozen experiment.
    assert entry.sleeve_parameters == frozenset()


# --- module hygiene ----------------------------------------------------------------


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_the_module_imports_nothing_beyond_its_declared_dependencies() -> None:
    """An exact import set, so a data fetch or a central-file coupling cannot creep in."""
    assert _imported_modules(_MODULE) == {
        "__future__",
        "collections.abc",
        "dataclasses",
        "datetime",
        "decimal",
        "math",
        "schwab_trader.agent",
        "schwab_trader.market_data",
        "schwab_trader.strategies",
    }


def test_the_module_performs_no_io_and_no_dynamic_loading() -> None:
    """No network, no filesystem data loading, no database, no plugin resolution."""
    forbidden = {
        "httpx",
        "requests",
        "sqlite3",
        "socket",
        "smtplib",
        "urllib",
        "os",
        "pathlib",
        "io",
        "importlib",
        "pkgutil",
        "runpy",
        "subprocess",
        "schwab_trader.state",
        "schwab_trader.paper",
        "schwab_trader.sec_store",
    }
    assert not (_imported_modules(_MODULE) & forbidden)

    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not (called & {"eval", "exec", "compile", "__import__", "open"})


def test_the_module_does_not_reach_into_files_reserved_for_integration() -> None:
    reserved = {
        "schwab_trader.strategy_registry",
        "schwab_trader.cli",
        "schwab_trader.dashboard",
        "schwab_trader.cohort_lifecycle",
        "schwab_trader.cohort_ops",
        "schwab_trader.cohort_readiness",
        "schwab_trader.scheduling",
        "schwab_trader.sleeves",
        "schwab_trader.sleeve_runs",
        "schwab_trader.signals",
        "schwab_trader.fundamentals",
    }
    assert not (_imported_modules(_MODULE) & reserved)


def test_evaluation_never_touches_a_clock() -> None:
    """The signal session is injected; a strategy that read ``now`` could not replay."""
    source = _MODULE.read_text(encoding="utf-8")
    assert "datetime.now" not in source
    assert "date.today" not in source
