"""Tests for the accumulating intraday panel and intraday history fetch (offline)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import httpx
import pytest
import respx

from schwab_trader import client as api
from schwab_trader import market_data
from schwab_trader.client import SchwabClient
from schwab_trader.config import Settings
from schwab_trader.intraday_panel import IntradayPanel
from schwab_trader.market_data import Candle

PH_URL = api.API_BASE_URL + market_data.PRICE_HISTORY_PATH


def _candle(symbol: str, ts: str, close: str, *, volume: int = 100) -> Candle:
    return Candle(
        symbol=symbol,
        date=datetime.fromisoformat(ts),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=volume,
    )


def test_upsert_is_idempotent_per_resolution(tmp_path) -> None:
    panel = IntradayPanel(tmp_path / "intraday.sqlite3")
    bars = [
        _candle("SPY", "2026-07-16T09:30:00+00:00", "600"),
        _candle("SPY", "2026-07-16T09:35:00+00:00", "601"),
    ]
    panel.upsert(5, bars)
    panel.upsert(5, bars)  # same bars again
    assert panel.total_bars() == 2
    # Same timestamps at a DIFFERENT resolution are distinct rows.
    panel.upsert(1, [_candle("SPY", "2026-07-16T09:30:00+00:00", "600")])
    assert panel.total_bars() == 3


def test_bars_filter_by_resolution_and_range(tmp_path) -> None:
    panel = IntradayPanel(tmp_path / "intraday.sqlite3")
    panel.upsert(
        5,
        [
            _candle("SPY", "2026-07-16T09:30:00+00:00", "600"),
            _candle("SPY", "2026-07-16T10:00:00+00:00", "602"),
            _candle("SPY", "2026-07-17T09:30:00+00:00", "605"),
        ],
    )
    day1 = panel.bars(
        "SPY",
        5,
        start=datetime.fromisoformat("2026-07-16T00:00:00+00:00"),
        end=datetime.fromisoformat("2026-07-16T23:59:00+00:00"),
    )
    assert [c.close for c in day1] == [Decimal("600"), Decimal("602")]
    assert panel.bars("SPY", 1) == []  # nothing at 1-min resolution


def test_coverage(tmp_path) -> None:
    panel = IntradayPanel(tmp_path / "intraday.sqlite3")
    panel.upsert(5, [_candle("SPY", "2026-07-16T09:30:00+00:00", "600")])
    cov = panel.coverage()
    assert len(cov) == 1
    assert cov[0].symbol == "SPY"
    assert cov[0].minutes == 5
    assert cov[0].bars == 1


class _FakeTokens:
    def get_access_token(self) -> str:
        return "ACCESS-abc123"


def _client() -> SchwabClient:
    settings = Settings(_env_file=None, client_id="x", client_secret="y")  # type: ignore[call-arg]
    return SchwabClient(settings, _FakeTokens(), sleep=lambda _s: None)


@respx.mock
def test_get_intraday_history_parses_and_requests_minute_bars() -> None:
    body = {
        "candles": [
            {
                "datetime": 1784000000000,
                "open": 600,
                "high": 601,
                "low": 599,
                "close": 600,
                "volume": 1000,
            },
            {
                "datetime": 1784000300000,
                "open": 600,
                "high": 602,
                "low": 600,
                "close": 602,
                "volume": 1200,
            },
        ]
    }
    route = respx.get(PH_URL).mock(return_value=httpx.Response(200, json=body))
    with _client() as client:
        candles = market_data.get_intraday_history(client, "spy", minutes=5, days=30)
    assert route.call_count == 1
    params = route.calls[0].request.url.params
    assert params["frequencyType"] == "minute"
    assert params["frequency"] == "5"
    assert "startDate" in params and "endDate" in params
    assert [c.close for c in candles] == [Decimal("600"), Decimal("602")]


def test_get_intraday_history_rejects_bad_frequency() -> None:
    with pytest.raises(ValueError, match="minutes must be one of"):
        market_data.get_intraday_history(object(), "SPY", minutes=7)  # type: ignore[arg-type]
