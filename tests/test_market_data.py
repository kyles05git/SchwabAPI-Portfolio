"""Tests for market-data quotes (Phase 6). Offline: mocked HTTP via respx."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from schwab_trader import client as api
from schwab_trader import market_data
from schwab_trader.client import SchwabClient
from schwab_trader.config import Settings
from schwab_trader.market_data import Quote, QuoteError, get_quote

QUOTES_URL = api.API_BASE_URL + market_data.QUOTES_PATH
QUOTE_TIME_MS = 1783987195505
QUOTE_BODY = {
    "AAPL": {
        "assetMainType": "EQUITY",
        "symbol": "AAPL",
        "quote": {
            "bidPrice": 150.00,
            "askPrice": 150.10,
            "lastPrice": 150.05,
            "mark": 150.05,
            "closePrice": 149.00,
            "quoteTime": QUOTE_TIME_MS,
            "tradeTime": QUOTE_TIME_MS + 500,
            "securityStatus": "Normal",
        },
    }
}


class _FakeTokens:
    def get_access_token(self) -> str:
        return "ACCESS-abc123"


def _client() -> SchwabClient:
    settings = Settings(_env_file=None, client_id="x", client_secret="y")  # type: ignore[call-arg]
    return SchwabClient(settings, _FakeTokens(), sleep=lambda _seconds: None)


@respx.mock
def test_get_quote_parses_prices_and_timestamp() -> None:
    respx.get(QUOTES_URL).mock(return_value=httpx.Response(200, json=QUOTE_BODY))
    with _client() as client:
        quote = get_quote(client, "aapl")  # lowercase should be normalized
    from decimal import Decimal

    assert quote.symbol == "AAPL"
    assert quote.bid == Decimal("150.00")
    assert quote.ask == Decimal("150.10")
    assert quote.last == Decimal("150.05")
    assert quote.mark == Decimal("150.05")
    assert quote.previous_close == Decimal("149.00")
    assert quote.quote_time == datetime.fromtimestamp(QUOTE_TIME_MS / 1000, tz=UTC)


@respx.mock
def test_get_quote_missing_symbol_raises() -> None:
    respx.get(QUOTES_URL).mock(return_value=httpx.Response(200, json={}))
    with _client() as client, pytest.raises(QuoteError):
        get_quote(client, "NOPE")


@respx.mock
def test_get_quote_without_timestamp_raises() -> None:
    body = {"AAPL": {"symbol": "AAPL", "quote": {"lastPrice": 150.0}}}
    respx.get(QUOTES_URL).mock(return_value=httpx.Response(200, json=body))
    with _client() as client, pytest.raises(QuoteError):
        get_quote(client, "AAPL")


PRICE_HISTORY_URL = api.API_BASE_URL + market_data.PRICE_HISTORY_PATH


@respx.mock
def test_get_price_history_parses_candles() -> None:
    from decimal import Decimal

    body = {
        "symbol": "AAPL",
        "empty": False,
        "candles": [
            {"open": 100, "high": 102, "low": 99, "close": 101, "volume": 10, "datetime": 1000},
            {"open": 101, "high": 104, "low": 100, "close": 103, "volume": 12, "datetime": 2000},
        ],
    }
    respx.get(PRICE_HISTORY_URL).mock(return_value=httpx.Response(200, json=body))
    with _client() as client:
        candles = market_data.get_price_history(client, "aapl", days=180)
    assert len(candles) == 2
    assert candles[0].close == Decimal("101")
    assert candles[1].close == Decimal("103")
    assert candles[0].date < candles[1].date  # sorted oldest-first


@respx.mock
def test_get_price_history_empty_returns_list() -> None:
    respx.get(PRICE_HISTORY_URL).mock(return_value=httpx.Response(200, json={"empty": True}))
    with _client() as client:
        assert market_data.get_price_history(client, "AAPL") == []


INSTRUMENTS_URL = api.API_BASE_URL + market_data.INSTRUMENTS_PATH


@respx.mock
def test_get_fundamentals_parses_fields() -> None:
    body = {
        "instruments": [
            {
                "symbol": "AAPL",
                "description": "APPLE INC",
                "fundamental": {
                    "peRatio": 39.7,
                    "pegRatio": 2.1,
                    "marketCap": 4592148726960.0,
                    "returnOnEquity": 141.47,
                    "operatingMarginTTM": 27.15,
                    "epsChangePercentTTM": 22.69,
                    "revChangeTTM": 12.75,
                    "totalDebtToEquity": 69.86,
                    "dividendYield": 0.34,
                    "avg3MonthVolume": 50087712,
                },
            }
        ]
    }
    respx.get(INSTRUMENTS_URL).mock(return_value=httpx.Response(200, json=body))
    with _client() as client:
        f = market_data.get_fundamentals(client, "aapl")
    assert f is not None
    assert f.symbol == "AAPL"
    assert f.pe_ratio == 39.7
    assert f.return_on_equity == 141.47
    assert f.earnings_yield is not None
    assert abs(f.earnings_yield - 1 / 39.7) < 1e-9


@respx.mock
def test_get_fundamentals_missing_returns_none() -> None:
    respx.get(INSTRUMENTS_URL).mock(return_value=httpx.Response(200, json={"instruments": []}))
    with _client() as client:
        assert market_data.get_fundamentals(client, "NOPE") is None


def test_earnings_yield_handles_bad_pe() -> None:
    from schwab_trader.market_data import Fundamentals

    assert Fundamentals(symbol="X", pe_ratio=0.0).earnings_yield is None
    assert Fundamentals(symbol="X", pe_ratio=-5.0).earnings_yield is None
    assert Fundamentals(symbol="X").earnings_yield is None


def _quote_at(quote_time: datetime) -> Quote:
    return Quote(symbol="AAPL", quote_time=quote_time)


def test_is_stale_detects_old_quote() -> None:
    old = _quote_at(datetime.now(UTC) - timedelta(minutes=5))
    assert old.is_stale(timedelta(minutes=1)) is True
    assert old.is_stale(timedelta(hours=1)) is False


def test_fresh_quote_is_not_stale() -> None:
    fresh = _quote_at(datetime.now(UTC))
    assert fresh.is_stale(timedelta(minutes=1)) is False


def test_age_is_measured_against_now() -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
    quote = _quote_at(now - timedelta(seconds=30))
    assert quote.age(now=now) == timedelta(seconds=30)
