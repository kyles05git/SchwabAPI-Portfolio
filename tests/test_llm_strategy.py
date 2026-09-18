"""Tests for the LLM-backed strategy (fully offline; no anthropic, no network).

The network boundary is build_anthropic_decider(); LLMStrategy takes an injected
decider, so every test here uses a stub. The one test that exercises
build_anthropic_decider injects a fake client object.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from schwab_trader.agent import AgentRunner, MarketContext
from schwab_trader.llm_strategy import (
    AnthropicUnavailable,
    LLMDecision,
    LLMOrderIdea,
    LLMRequestError,
    LLMStrategy,
    build_anthropic_decider,
)
from schwab_trader.market_data import Quote
from schwab_trader.paper import PaperEngine

NOW = datetime(2026, 7, 15, 15, 0, tzinfo=UTC)
UNIVERSE = ["AAA", "BBB"]


def _quote(symbol: str, *, price: str) -> Quote:
    p = Decimal(price)
    return Quote(symbol=symbol, last=p, previous_close=p, ask=p, bid=p, mark=p, quote_time=NOW)


def _context(cash: str, positions: dict[str, int] | None = None) -> MarketContext:
    pos = positions or {}
    equity = Decimal(cash) + sum((Decimal(q) * Decimal("10.00") for q in pos.values()), Decimal(0))
    return MarketContext(
        now=NOW,
        cash=Decimal(cash),
        positions=pos,
        quotes={s: _quote(s, price="10.00") for s in UNIVERSE},
        equity=equity,
    )


def _decider(decision: LLMDecision):
    return lambda _context: decision


def test_buy_idea_becomes_marketable_proposal() -> None:
    decision = LLMDecision(
        market_view="mildly bullish",
        orders=[LLMOrderIdea(action="BUY", symbol="AAA", quantity=5, reason="cheap")],
    )
    strategy = LLMStrategy(UNIVERSE, decider=_decider(decision))
    proposals = strategy.decide(_context("1000"))
    assert len(proposals) == 1
    request = proposals[0].request
    assert request.symbol == "AAA"
    assert request.quantity == 5
    assert request.limit_price == Decimal("10.00")
    assert proposals[0].rationale == "cheap"


def test_symbol_outside_universe_is_dropped() -> None:
    decision = LLMDecision(
        market_view="",
        orders=[LLMOrderIdea(action="BUY", symbol="ZZZ", quantity=1, reason="off-list")],
    )
    strategy = LLMStrategy(UNIVERSE, decider=_decider(decision))
    assert strategy.decide(_context("1000")) == []


def test_buy_quantity_capped_to_position_fraction() -> None:
    decision = LLMDecision(
        market_view="",
        orders=[LLMOrderIdea(action="BUY", symbol="AAA", quantity=100, reason="greedy")],
    )
    # 9.5% of $1000 equity = $95 cap -> 9 whole shares at $10.
    strategy = LLMStrategy(
        UNIVERSE, decider=_decider(decision), max_position_fraction=Decimal("0.095")
    )
    proposals = strategy.decide(_context("1000"))
    assert len(proposals) == 1
    assert proposals[0].request.quantity == 9


def test_position_fraction_accounts_for_existing_holding() -> None:
    # Holds 15 shares of AAA = $150. Equity = $1000 cash + $150 = $1150; a 20% cap
    # is $230, leaving $80 -> 8 more shares even though the model asked for 100.
    decision = LLMDecision(
        market_view="",
        orders=[LLMOrderIdea(action="BUY", symbol="AAA", quantity=100, reason="add")],
    )
    strategy = LLMStrategy(
        UNIVERSE, decider=_decider(decision), max_position_fraction=Decimal("0.20")
    )
    proposals = strategy.decide(_context("1000", positions={"AAA": 15}))
    assert len(proposals) == 1
    assert proposals[0].request.quantity == 8


def test_buy_dropped_when_cash_insufficient() -> None:
    decision = LLMDecision(
        market_view="",
        orders=[LLMOrderIdea(action="BUY", symbol="AAA", quantity=1, reason="broke")],
    )
    strategy = LLMStrategy(UNIVERSE, decider=_decider(decision))
    assert strategy.decide(_context("5")) == []  # can't afford one $10 share


def test_sell_requires_holdings() -> None:
    decision = LLMDecision(
        market_view="",
        orders=[LLMOrderIdea(action="SELL", symbol="AAA", quantity=3, reason="take profit")],
    )
    strategy = LLMStrategy(UNIVERSE, decider=_decider(decision))
    assert strategy.decide(_context("1000")) == []  # nothing held

    proposals = strategy.decide(_context("1000", positions={"AAA": 2}))
    assert len(proposals) == 1
    assert proposals[0].request.quantity == 2  # capped to held shares


def test_buys_capped_to_max_positions() -> None:
    decision = LLMDecision(
        market_view="",
        orders=[
            LLMOrderIdea(action="BUY", symbol="AAA", quantity=1, reason="a"),
            LLMOrderIdea(action="BUY", symbol="BBB", quantity=1, reason="b"),
        ],
    )
    strategy = LLMStrategy(UNIVERSE, decider=_decider(decision), max_positions=1)
    proposals = strategy.decide(_context("1000"))
    assert len(proposals) == 1  # only one new position allowed


def test_empty_decision_yields_no_proposals() -> None:
    strategy = LLMStrategy(UNIVERSE, decider=_decider(LLMDecision(market_view="flat", orders=[])))
    assert strategy.decide(_context("1000")) == []


def test_end_to_end_fills_against_paper_engine(tmp_path) -> None:
    engine = PaperEngine(tmp_path / "paper.sqlite3", starting_cash=Decimal("1000.00"))
    decision = LLMDecision(
        market_view="buy the dip",
        orders=[LLMOrderIdea(action="BUY", symbol="AAA", quantity=3, reason="cheap")],
    )
    strategy = LLMStrategy(UNIVERSE, decider=_decider(decision))
    report = AgentRunner(strategy, engine, lambda s: _quote(s, price="10.00")).run_cycle(now=NOW)
    assert report.num_filled == 1
    assert engine.positions()[0].symbol == "AAA"
    assert engine.positions()[0].quantity == 3


# --- build_anthropic_decider (network boundary) --------------------------------


def test_decider_requires_api_key() -> None:
    with pytest.raises(AnthropicUnavailable):
        build_anthropic_decider(api_key="", model="claude-haiku-4-5")


class _FakeMessages:
    def __init__(self, decision: LLMDecision | None, *, raise_exc: bool = False) -> None:
        self._decision = decision
        self._raise = raise_exc
        self.last_kwargs: dict[str, object] = {}

    def parse(self, **kwargs):
        self.last_kwargs = kwargs
        if self._raise:
            raise RuntimeError("boom")

        class _Parsed:
            parsed_output = self._decision

        return _Parsed()


class _FakeClient:
    def __init__(self, decision: LLMDecision | None, *, raise_exc: bool = False) -> None:
        self.messages = _FakeMessages(decision, raise_exc=raise_exc)


def test_decider_with_injected_client_returns_decision() -> None:
    decision = LLMDecision(
        market_view="ok",
        orders=[LLMOrderIdea(action="BUY", symbol="AAA", quantity=1, reason="x")],
    )
    decider = build_anthropic_decider(
        api_key="unused", model="claude-haiku-4-5", client=_FakeClient(decision)
    )
    assert decider(_context("1000")) == decision


def test_decider_wraps_api_errors() -> None:
    decider = build_anthropic_decider(
        api_key="unused", model="claude-haiku-4-5", client=_FakeClient(None, raise_exc=True)
    )
    with pytest.raises(LLMRequestError):
        decider(_context("1000"))


def test_decider_reports_usage() -> None:
    seen: list[object] = []
    decider = build_anthropic_decider(
        api_key="unused",
        model="claude-haiku-4-5",
        client=_FakeClient(LLMDecision(market_view="", orders=[])),
        on_usage=seen.append,
    )
    decider(_context("1000"))
    assert len(seen) == 1
    assert seen[0].model == "claude-haiku-4-5"  # type: ignore[attr-defined]


def test_decider_handles_no_structured_output() -> None:
    decider = build_anthropic_decider(
        api_key="unused", model="claude-haiku-4-5", client=_FakeClient(None)
    )
    result = decider(_context("1000"))
    assert result.orders == []


def test_spec_guidance_is_injected_into_system_prompt() -> None:
    client = _FakeClient(LLMDecision(market_view="", orders=[]))
    decider = build_anthropic_decider(
        api_key="unused",
        model="claude-haiku-4-5",
        spec_guidance="Market regime: defensive\nAvoid symbols: AAA",
        client=client,
    )
    decider(_context("1000"))
    system = client.messages.last_kwargs["system"]
    assert "defensive" in system
    assert "Avoid symbols: AAA" in system
