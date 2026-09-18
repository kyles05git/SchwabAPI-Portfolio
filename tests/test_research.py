"""Tests for the research step (fully offline; no anthropic, no network).

The single network call lives in build_anthropic_researcher(); it is tested with
an injected fake client. Everything else - spec sanitizing, clamping, guidance
rendering, and the SQLite store - is exercised directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from schwab_trader.market_data import Quote
from schwab_trader.research import (
    AnthropicUnavailable,
    LLMRequestError,
    ResearchContext,
    ResearchStore,
    StrategyPlan,
    StrategySpec,
    build_anthropic_researcher,
    spec_from_plan,
)

NOW = datetime(2026, 7, 15, 15, 0, tzinfo=UTC)
UNIVERSE = ["AAA", "BBB", "CCC"]


def _quote(symbol: str, *, price: str) -> Quote:
    p = Decimal(price)
    return Quote(symbol=symbol, last=p, previous_close=p, ask=p, bid=p, mark=p, quote_time=NOW)


def _context(cash: str = "1000") -> ResearchContext:
    return ResearchContext(
        now=NOW,
        cash=Decimal(cash),
        positions={},
        quotes={s: _quote(s, price="10.00") for s in UNIVERSE},
        universe=UNIVERSE,
    )


def _plan(**overrides: object) -> StrategyPlan:
    base: dict[str, object] = {
        "market_regime": "risk-on",
        "thesis": "buy strength",
        "focus_symbols": ["AAA"],
        "avoid_symbols": ["BBB"],
        "rules": ["Buy names up on the day", "Skip anything down hard"],
        "max_positions": 2,
        "max_position_fraction": 0.15,
    }
    base.update(overrides)
    return StrategyPlan(**base)  # type: ignore[arg-type]


def test_spec_from_plan_copies_and_sanitizes() -> None:
    spec = spec_from_plan(_plan(), _context(), model="claude-opus-4-8")
    assert spec.model == "claude-opus-4-8"
    assert spec.market_regime == "risk-on"
    assert spec.focus_symbols == ["AAA"]
    assert spec.max_position_fraction == Decimal("0.15")
    assert spec.max_positions == 2
    assert spec.created_at == NOW


def test_spec_filters_symbols_outside_universe() -> None:
    plan = _plan(focus_symbols=["aaa", "ZZZ", "AAA"], avoid_symbols=["QQQ"])
    spec = spec_from_plan(plan, _context(), model="m")
    assert spec.focus_symbols == ["AAA"]  # normalized, de-duped, off-universe dropped
    assert spec.avoid_symbols == []


def test_sizing_is_clamped() -> None:
    # max_positions above universe size is capped; fraction above 1.0 capped to 1.0.
    plan = _plan(max_positions=99, max_position_fraction=5.0)
    spec = spec_from_plan(plan, _context(cash="500"), model="m")
    assert spec.max_positions == len(UNIVERSE)
    assert spec.max_position_fraction == Decimal("1.0")


def test_sizing_floor_applied() -> None:
    plan = _plan(max_positions=0, max_position_fraction=0.0)
    spec = spec_from_plan(plan, _context(cash="1000"), model="m")
    assert spec.max_positions == 1
    assert spec.max_position_fraction == Decimal("0.02")  # floor


def test_guidance_text_includes_key_fields() -> None:
    text = spec_from_plan(_plan(), _context(), model="m").guidance_text()
    assert "risk-on" in text
    assert "buy strength" in text
    assert "AAA" in text
    assert "Buy names up on the day" in text


# --- ResearchStore -------------------------------------------------------------


def test_store_roundtrip_and_latest(tmp_path) -> None:
    store = ResearchStore(tmp_path / "research.sqlite3")
    assert store.latest() is None

    spec1 = spec_from_plan(_plan(market_regime="first"), _context(), model="m")
    spec2 = spec_from_plan(_plan(market_regime="second"), _context(), model="m")
    store.record(spec1)
    store.record(spec2)

    latest = store.latest()
    assert latest is not None
    assert latest.market_regime == "second"
    assert isinstance(latest, StrategySpec)
    assert latest.max_position_fraction == Decimal("0.15")  # Decimal survives JSON roundtrip

    recent = store.recent(limit=10)
    assert [s.market_regime for s in recent] == ["second", "first"]


# --- build_anthropic_researcher (network boundary) -----------------------------


def test_researcher_requires_api_key() -> None:
    with pytest.raises(AnthropicUnavailable):
        build_anthropic_researcher(api_key="", model="claude-opus-4-8")


class _FakeMessages:
    def __init__(self, plan: StrategyPlan | None, *, raise_exc: bool = False) -> None:
        self._plan = plan
        self._raise = raise_exc
        self.last_kwargs: dict[str, object] = {}

    def parse(self, **kwargs: object):
        self.last_kwargs = kwargs
        if self._raise:
            raise RuntimeError("boom")

        class _Parsed:
            parsed_output = self._plan

        return _Parsed()


class _FakeClient:
    def __init__(self, plan: StrategyPlan | None, *, raise_exc: bool = False) -> None:
        self.messages = _FakeMessages(plan, raise_exc=raise_exc)


def test_researcher_returns_spec_from_injected_client() -> None:
    client = _FakeClient(_plan())
    researcher = build_anthropic_researcher(
        api_key="unused", model="claude-opus-4-8", client=client
    )
    spec = researcher(_context())
    assert spec.model == "claude-opus-4-8"
    assert spec.market_regime == "risk-on"
    # research should request thinking (a reasoning task)
    assert client.messages.last_kwargs.get("thinking") == {"type": "adaptive"}


def test_web_search_tool_passed_when_enabled() -> None:
    client = _FakeClient(_plan())
    researcher = build_anthropic_researcher(
        api_key="unused", model="m", use_web_search=True, max_searches=4, client=client
    )
    researcher(_context())
    tools = client.messages.last_kwargs.get("tools")
    assert tools is not None
    assert tools[0]["name"] == "web_search"
    assert tools[0]["max_uses"] == 4


def test_web_search_tool_omitted_when_disabled() -> None:
    client = _FakeClient(_plan())
    researcher = build_anthropic_researcher(
        api_key="unused", model="m", use_web_search=False, client=client
    )
    researcher(_context())
    assert "tools" not in client.messages.last_kwargs


def test_macro_and_feedback_included_in_prompt() -> None:
    client = _FakeClient(_plan())
    researcher = build_anthropic_researcher(api_key="unused", model="m", client=client)
    ctx = ResearchContext(
        now=NOW,
        cash=Decimal("1000"),
        positions={},
        quotes={},
        universe=["AAA"],
        macro="VIX 22.5, 10y-3m -0.30%",
        performance_feedback="previous thesis lagged; total return -4.20%",
    )
    researcher(ctx)
    content = client.messages.last_kwargs["messages"][0]["content"]
    assert "Macro: VIX 22.5" in content
    assert "Prior-strategy performance:" in content
    assert "-4.20%" in content


def test_fundamentals_are_included_in_prompt() -> None:
    from schwab_trader.market_data import Fundamentals

    client = _FakeClient(_plan())
    researcher = build_anthropic_researcher(api_key="unused", model="m", client=client)
    ctx = ResearchContext(
        now=NOW,
        cash=Decimal("1000"),
        positions={},
        quotes={},
        universe=["AAA"],
        fundamentals={"AAA": Fundamentals(symbol="AAA", pe_ratio=12.3, return_on_equity=25.0)},
    )
    researcher(ctx)
    content = client.messages.last_kwargs["messages"][0]["content"]
    assert "Fundamentals" in content
    assert "P/E 12.3" in content
    assert "ROE 25%" in content


def test_edgar_ratios_included_in_prompt() -> None:
    from schwab_trader.fundamentals import Ratios

    client = _FakeClient(_plan())
    researcher = build_anthropic_researcher(api_key="unused", model="m", client=client)
    ctx = ResearchContext(
        now=NOW,
        cash=Decimal("1000"),
        positions={},
        quotes={},
        universe=["AAA"],
        ratios={
            "AAA": Ratios(
                ticker="AAA",
                as_of=NOW.date(),
                price=Decimal("100"),
                pe_ttm=Decimal("15.0"),
                earnings_yield_ttm=Decimal("0.066"),
                book_to_market=Decimal("0.40"),
                roe_ttm=Decimal("0.22"),
            )
        },
    )
    researcher(ctx)
    content = client.messages.last_kwargs["messages"][0]["content"]
    assert "Point-in-time fundamentals (SEC EDGAR, TTM)" in content
    assert "P/E 15.0" in content
    assert "ROE 22%" in content
    assert "B/M 0.40" in content


def test_source_documents_are_included_in_prompt() -> None:
    client = _FakeClient(_plan())
    researcher = build_anthropic_researcher(api_key="unused", model="m", client=client)
    ctx = ResearchContext(
        now=NOW,
        cash=Decimal("1000"),
        positions={},
        quotes={},
        universe=UNIVERSE,
        source_documents=["ZEBRA_MARKER: the research says favor AAA"],
    )
    researcher(ctx)
    content = client.messages.last_kwargs["messages"][0]["content"]
    assert "ZEBRA_MARKER" in content


def test_researcher_wraps_api_errors() -> None:
    researcher = build_anthropic_researcher(
        api_key="unused", model="m", client=_FakeClient(None, raise_exc=True)
    )
    with pytest.raises(LLMRequestError):
        researcher(_context())


def test_researcher_errors_on_no_plan() -> None:
    researcher = build_anthropic_researcher(api_key="unused", model="m", client=_FakeClient(None))
    with pytest.raises(LLMRequestError):
        researcher(_context())
