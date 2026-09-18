"""Tests for batched quotes and the market-calendar helpers. Offline."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import httpx
import respx

from schwab_trader import client as api
from schwab_trader import market_calendar, market_data
from schwab_trader.client import SchwabClient
from schwab_trader.config import Settings

QUOTES_URL = api.API_BASE_URL + market_data.QUOTES_PATH
TS = 1783987195505
_BODY = {
    "AAPL": {
        "symbol": "AAPL",
        "quote": {"bidPrice": 150.0, "askPrice": 150.1, "lastPrice": 150.05, "quoteTime": TS},
    },
    "MSFT": {
        "symbol": "MSFT",
        "quote": {"bidPrice": 400.0, "askPrice": 400.2, "lastPrice": 400.1, "quoteTime": TS},
    },
    "BADD": {"symbol": "BADD"},  # no quote block -> should be skipped
}


class _FakeTokens:
    def get_access_token(self) -> str:
        return "ACCESS-abc123"


def _client() -> SchwabClient:
    settings = Settings(_env_file=None, client_id="x", client_secret="y")  # type: ignore[call-arg]
    return SchwabClient(settings, _FakeTokens(), sleep=lambda _seconds: None)


@respx.mock
def test_get_quotes_batches_and_skips_unusable() -> None:
    route = respx.get(QUOTES_URL).mock(return_value=httpx.Response(200, json=_BODY))
    with _client() as client:
        quotes = market_data.get_quotes(client, ["aapl", "msft", "badd"])

    # One HTTP call for the whole batch, with a comma-joined symbol list.
    assert route.call_count == 1
    assert route.calls[0].request.url.params["symbols"] == "AAPL,MSFT,BADD"
    assert set(quotes) == {"AAPL", "MSFT"}  # BADD dropped (no usable quote)
    assert quotes["AAPL"].ask == Decimal("150.1")


def test_get_quotes_empty_input_makes_no_request() -> None:
    # No symbols -> no client needed, returns empty.
    assert market_data.get_quotes(None, []) == {}  # type: ignore[arg-type]


def test_eastern_offset_dst_boundaries() -> None:
    assert market_calendar._nth_sunday(2026, 3, 2) == date(2026, 3, 8)  # DST starts
    assert market_calendar._nth_sunday(2026, 11, 1) == date(2026, 11, 1)  # DST ends
    assert market_calendar.eastern_offset_hours(date(2026, 7, 1)) == -4  # EDT (summer)
    assert market_calendar.eastern_offset_hours(date(2026, 1, 15)) == -5  # EST (winter)


def test_is_regular_session() -> None:
    tue = datetime(2026, 7, 14, 12, 0)  # Tuesday midday
    assert market_calendar.is_regular_session(tue)
    assert not market_calendar.is_regular_session(datetime(2026, 7, 14, 9, 0))  # pre-open
    assert not market_calendar.is_regular_session(datetime(2026, 7, 14, 16, 1))  # post-close
    assert not market_calendar.is_regular_session(datetime(2026, 7, 18, 12, 0))  # Saturday
