"""Tests for the on-disk price-history cache (offline; fake client + fetch)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from schwab_trader import history_cache, market_data
from schwab_trader.market_data import Candle

START = datetime(2025, 1, 2, tzinfo=UTC)


def _candles(n: int) -> list[Candle]:
    return [
        Candle(symbol="AAA", date=START + timedelta(days=i), close=Decimal(str(100 + i)))
        for i in range(n)
    ]


class _CountingClient:
    """Stands in for SchwabClient; get_price_history is monkeypatched to count calls."""


def test_cache_avoids_refetch_same_day(tmp_path, monkeypatch) -> None:
    calls = {"n": 0}

    def fake_fetch(_client, symbol, *, days):
        calls["n"] += 1
        return _candles(days)

    monkeypatch.setattr(market_data, "get_price_history", fake_fetch)
    cache = history_cache.HistoryCache(tmp_path / "hc")
    client = _CountingClient()

    first = cache.get(client, "AAA", days=50)
    second = cache.get(client, "AAA", days=50)
    assert len(first) == 50
    assert len(second) == 50
    assert calls["n"] == 1  # second served from cache


def test_cache_refetches_when_more_days_needed(tmp_path, monkeypatch) -> None:
    calls = {"n": 0}

    def fake_fetch(_client, symbol, *, days):
        calls["n"] += 1
        return _candles(days)

    monkeypatch.setattr(market_data, "get_price_history", fake_fetch)
    cache = history_cache.HistoryCache(tmp_path / "hc")
    client = _CountingClient()

    cache.get(client, "AAA", days=50)
    cache.get(client, "AAA", days=200)  # needs more than cached -> refetch
    assert calls["n"] == 2


def test_cache_roundtrip_preserves_values(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(market_data, "get_price_history", lambda _c, _s, *, days: _candles(days))
    cache = history_cache.HistoryCache(tmp_path / "hc")
    cache.get(_CountingClient(), "AAA", days=10)
    # Second call reads from disk; values must survive the JSON roundtrip.
    reloaded = cache.get(_CountingClient(), "AAA", days=10)
    assert reloaded[0].close == Decimal("100")
    assert reloaded[-1].close == Decimal("109")
    assert reloaded[0].symbol == "AAA"


def test_clear_removes_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(market_data, "get_price_history", lambda _c, _s, *, days: _candles(days))
    cache = history_cache.HistoryCache(tmp_path / "hc")
    cache.get(_CountingClient(), "AAA", days=5)
    cache.get(_CountingClient(), "BBB", days=5)
    assert cache.clear() == 2
    assert cache.clear() == 0
