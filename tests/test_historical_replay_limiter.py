"""The replay downloader rides the existing client limiter, not a private one.

Issue #80 requires the acquisition to respect the configured request limiter. This
suite drives :func:`historical_replay_acquire.schwab_session_fetcher` through a real
:class:`~schwab_trader.client.SchwabClient` with respx-mocked HTTP, so the limiter, the
header path, and the request shape are the real ones.

Offline: every response is a synthetic fixture served by respx. No socket reaches the
network, no OAuth token is read (the token provider is a stub returning a placeholder),
and nothing is persisted outside ``tmp_path``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx

from schwab_trader import client as api
from schwab_trader import historical_replay, historical_replay_acquire, market_calendar
from schwab_trader.client import SchwabClient
from schwab_trader.config import Settings
from schwab_trader.storage.database import Database
from schwab_trader.storage.historical_replay import SqlAlchemyHistoricalReplayStore

PRICE_HISTORY_URL = api.API_BASE_URL + "/marketdata/v1/pricehistory"
UNIVERSE = historical_replay.universe(["AAPL", "MSFT"], label="limiter")
NORMAL = date(2026, 7, 29)
OTHER = date(2026, 7, 28)
NOW = datetime(2026, 7, 30, 21, 0, tzinfo=UTC)


class _StubTokens:
    def get_access_token(self) -> str:
        return "PLACEHOLDER-NOT-A-REAL-TOKEN"


def _client(*, rate_limit: int = 100) -> SchwabClient:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        client_id="x",
        client_secret="y",
        rate_limit_per_minute=rate_limit,
    )
    return SchwabClient(settings, _StubTokens(), sleep=lambda _seconds: None)


def _candles(session: date, *, count: int | None = None) -> list[dict[str, object]]:
    starts = market_calendar.session_interval_starts_utc(session, minutes=5)
    if count is not None:
        starts = starts[:count]
    return [
        {
            "datetime": int(moment.timestamp() * 1000),
            "open": 100 + index,
            "high": 101 + index,
            "low": 99 + index,
            "close": 100 + index,
            "volume": 1000 + index,
        }
        for index, moment in enumerate(starts)
    ]


@pytest.fixture
def store(tmp_path: Path) -> SqlAlchemyHistoricalReplayStore:
    database = Database(f"sqlite:///{tmp_path / 'replay.sqlite3'}", create_schema=True)
    return SqlAlchemyHistoricalReplayStore(database)


@respx.mock
def test_every_request_passes_through_the_configured_limiter(
    store: SqlAlchemyHistoricalReplayStore,
) -> None:
    route = respx.get(PRICE_HISTORY_URL).mock(
        side_effect=lambda request: httpx.Response(200, json={"candles": _candles(NORMAL)})
    )
    acquired: list[int] = []

    with _client() as client:
        original = client._limiter.acquire

        def counting() -> None:
            acquired.append(1)
            original()

        client._limiter.acquire = counting  # type: ignore[method-assign]
        report = historical_replay_acquire.acquire(
            historical_replay_acquire.schwab_session_fetcher(client),
            store,
            UNIVERSE,
            (OTHER, NORMAL),
            clock=lambda: NOW,
        )

    # Two symbols x two sessions: four requests, four limiter acquisitions, no extras.
    assert route.call_count == 4
    assert len(acquired) == 4
    assert len(report.outcomes) == 4


@respx.mock
def test_the_wire_request_uses_explicit_session_bounds_only(
    store: SqlAlchemyHistoricalReplayStore,
) -> None:
    route = respx.get(PRICE_HISTORY_URL).mock(
        return_value=httpx.Response(200, json={"candles": _candles(NORMAL)})
    )
    with _client() as client:
        historical_replay_acquire.acquire(
            historical_replay_acquire.schwab_session_fetcher(client),
            store,
            historical_replay.universe(["AAPL"]),
            (NORMAL,),
            clock=lambda: NOW,
        )

    params = route.calls[0].request.url.params
    opened, closed = market_calendar.session_bounds_utc(NORMAL)
    assert params["symbol"] == "AAPL"
    assert params["frequencyType"] == "minute"
    assert params["frequency"] == "5"
    assert params["startDate"] == str(int(opened.timestamp() * 1000))
    assert params["endDate"] == str(int(closed.timestamp() * 1000))
    assert params["needExtendedHoursData"] == "false"
    # The date-free shape that can answer with a different session.
    assert "periodType" not in params
    assert "period" not in params


@respx.mock
def test_a_short_payload_over_the_wire_is_reported_incomplete(
    store: SqlAlchemyHistoricalReplayStore,
) -> None:
    respx.get(PRICE_HISTORY_URL).mock(
        return_value=httpx.Response(200, json={"candles": _candles(NORMAL, count=70)})
    )
    with _client() as client:
        report = historical_replay_acquire.acquire(
            historical_replay_acquire.schwab_session_fetcher(client),
            store,
            historical_replay.universe(["AAPL"]),
            (NORMAL,),
            clock=lambda: NOW,
        )

    assert len(report.incomplete) == 1
    assert report.incomplete[0].unique_bar_count == 70
    assert report.incomplete[0].expected_bar_count == 78


@respx.mock
def test_a_provider_error_over_the_wire_is_recorded_without_its_body(
    store: SqlAlchemyHistoricalReplayStore,
) -> None:
    respx.get(PRICE_HISTORY_URL).mock(
        return_value=httpx.Response(403, json={"message": "forbidden: apikey=SECRET"})
    )
    with _client() as client:
        report = historical_replay_acquire.acquire(
            historical_replay_acquire.schwab_session_fetcher(client),
            store,
            historical_replay.universe(["AAPL"]),
            (NORMAL,),
            clock=lambda: NOW,
        )

    assert len(report.unavailable) == 1
    assert report.unavailable[0].error == "ApiError:403"
    assert "SECRET" not in str(historical_replay_acquire.report_payload(report))


@respx.mock
def test_the_downloader_adds_no_retry_of_its_own(
    store: SqlAlchemyHistoricalReplayStore,
) -> None:
    """A 403 is not retryable, so exactly one request is issued for it."""
    route = respx.get(PRICE_HISTORY_URL).mock(return_value=httpx.Response(403, json={}))
    with _client() as client:
        historical_replay_acquire.acquire(
            historical_replay_acquire.schwab_session_fetcher(client),
            store,
            historical_replay.universe(["AAPL"]),
            (NORMAL,),
            clock=lambda: NOW,
        )

    assert route.call_count == 1
