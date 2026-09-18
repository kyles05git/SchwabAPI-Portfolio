"""Tests for token/cost accounting (offline)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from schwab_trader.usage import (
    Usage,
    UsageStore,
    estimate_cost,
    usage_from_message,
)


def test_estimate_cost_haiku() -> None:
    # 1000 in @ $1/M + 500 out @ $5/M = 0.001 + 0.0025 = 0.0035
    usage = Usage(model="claude-haiku-4-5", input_tokens=1000, output_tokens=500)
    assert estimate_cost(usage) == Decimal("0.003500")


def test_estimate_cost_includes_web_search() -> None:
    # Opus 1000 in @ $5/M + 1000 out @ $25/M = 0.03; plus 3 searches @ $0.01 = 0.03.
    usage = Usage(model="claude-opus-4-8", input_tokens=1000, output_tokens=1000, web_searches=3)
    assert estimate_cost(usage) == Decimal("0.060000")


def test_estimate_cost_discounts_cache_reads() -> None:
    # cache reads billed ~0.1x input rate: 1000 * 0.1 * $1/M = 0.0001
    usage = Usage(model="claude-haiku-4-5", input_tokens=0, output_tokens=0, cache_read_tokens=1000)
    assert estimate_cost(usage) == Decimal("0.000100")


def test_unknown_model_falls_back_to_opus_pricing() -> None:
    usage = Usage(model="mystery-model", input_tokens=1_000_000, output_tokens=0)
    assert estimate_cost(usage) == Decimal("5.000000")


def _message(*, input_tokens: int, output_tokens: int, searches: int = 0) -> SimpleNamespace:
    content = [SimpleNamespace(type="server_tool_use", name="web_search") for _ in range(searches)]
    usage_obj = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return SimpleNamespace(usage=usage_obj, content=content)


def test_usage_from_message_counts_searches() -> None:
    msg = _message(input_tokens=1200, output_tokens=300, searches=2)
    usage = usage_from_message(msg, "claude-opus-4-8")
    assert usage.input_tokens == 1200
    assert usage.output_tokens == 300
    assert usage.web_searches == 2
    assert usage.model == "claude-opus-4-8"


def test_usage_from_message_is_defensive_about_missing_fields() -> None:
    usage = usage_from_message(SimpleNamespace(), "claude-haiku-4-5")  # no usage/content
    assert usage.input_tokens == 0
    assert usage.web_searches == 0


def test_store_record_and_summary(tmp_path) -> None:
    store = UsageStore(tmp_path / "usage.sqlite3")
    assert store.summary().calls == 0

    store.record("execution", Usage("claude-haiku-4-5", input_tokens=1000, output_tokens=500))
    store.record(
        "research",
        Usage("claude-opus-4-8", input_tokens=1000, output_tokens=1000, web_searches=1),
    )

    summary = store.summary()
    assert summary.calls == 2
    assert summary.execution_cost == Decimal("0.003500")
    assert summary.research_cost == Decimal("0.040000")  # 0.03 tokens + 0.01 search
    assert summary.total_cost == Decimal("0.043500")
    assert summary.web_searches == 1
    assert summary.first_ts is not None
