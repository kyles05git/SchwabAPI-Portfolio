"""Offline daily-first fallback resolver tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from schwab_trader import daily_bar_fallback, market_calendar, market_data
from schwab_trader.market_bar_evidence import DerivedDailyEvidence
from schwab_trader.market_data import Candle

SESSION = date(2026, 7, 28)
RETRIEVED_AT = datetime(2026, 7, 28, 20, 1, tzinfo=UTC)


def _daily(session: date, *, close: str = "100") -> Candle:
    value = Decimal(close)
    return Candle(
        symbol="SPY",
        date=datetime.combine(session, datetime.min.time(), UTC),
        open=value - 1,
        high=value + 1,
        low=value - 2,
        close=value,
        volume=1_000,
        source=market_data.SCHWAB_DAILY_HISTORY_SOURCE,
    )


def _complete(session: date = SESSION) -> list[Candle]:
    candles: list[Candle] = []
    for index, stamp in enumerate(market_calendar.session_interval_starts_utc(session)):
        opened = Decimal(100 + index)
        candles.append(
            Candle(
                symbol="SPY",
                date=stamp,
                open=opened,
                high=opened + 2,
                low=opened - 1,
                close=opened + 1,
                volume=index + 1,
                source=market_data.SCHWAB_REGULAR_SESSION_SOURCE,
            )
        )
    return candles


class _Cache:
    def __init__(self, candles: list[Candle]) -> None:
        self.candles = candles
        self.calls: list[tuple[str, date | None, datetime | None]] = []

    def get(
        self,
        client: Any,
        symbol: str,
        *,
        days: int = 180,
        settled_through: date | None = None,
        now: datetime | None = None,
    ) -> list[Candle]:
        del client, days
        self.calls.append((symbol, settled_through, now))
        return list(self.candles)


class _Store:
    def __init__(self) -> None:
        self.saved: list[DerivedDailyEvidence] = []

    def save(self, evidence: DerivedDailyEvidence) -> DerivedDailyEvidence:
        self.saved.append(evidence)
        return evidence

    def get(self, dataset_id: str) -> DerivedDailyEvidence | None:
        del dataset_id
        return None

    def for_session(
        self,
        symbol: str,
        session_date: date,
    ) -> tuple[DerivedDailyEvidence, ...]:
        del symbol, session_date
        return ()

    def reproduce(self, dataset_id: str) -> DerivedDailyEvidence | None:
        del dataset_id
        return None


def test_official_target_daily_skips_intraday_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    prior = market_calendar.previous_trading_day(SESSION)
    cache = _Cache([_daily(prior), _daily(SESSION)])
    store = _Store()

    def forbidden(*args: object, **kwargs: object) -> list[Candle]:
        del args, kwargs
        raise AssertionError("intraday fallback must not be requested")

    monkeypatch.setattr(market_data, "get_regular_session_history", forbidden)
    result = daily_bar_fallback.resolve_daily_history(
        object(),  # type: ignore[arg-type]
        cache,
        store,
        "spy",
        SESSION,
        clock=lambda: RETRIEVED_AT,
    )

    assert result.state is daily_bar_fallback.DailyBarState.OFFICIAL
    assert result.ready
    assert result.target_candle == _daily(SESSION)
    assert result.source == market_data.SCHWAB_DAILY_HISTORY_SOURCE
    assert store.saved == []
    assert cache.calls == [("SPY", SESSION, RETRIEVED_AT)]


def test_missing_daily_with_complete_exact_session_is_derived_and_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prior = market_calendar.previous_trading_day(SESSION)
    official = _daily(prior)
    cache = _Cache([official])
    store = _Store()
    monkeypatch.setattr(
        market_data,
        "get_regular_session_history",
        lambda client, symbol, session: _complete(session),
    )

    result = daily_bar_fallback.resolve_daily_history(
        object(),  # type: ignore[arg-type]
        cache,
        store,
        "SPY",
        SESSION,
        clock=lambda: RETRIEVED_AT,
    )

    assert result.state is daily_bar_fallback.DailyBarState.DERIVED
    assert result.ready
    assert result.latest_official_session == prior
    assert result.target_candle is not None
    assert result.target_candle.source == "schwab-intraday-derived-daily"
    assert result.history[:-1] == (official,)
    assert result.history[-1] == result.target_candle
    assert len(store.saved) == 1
    assert result.dataset_id == store.saved[0].dataset_id


def test_previous_session_with_78_bars_is_incomplete_and_not_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prior = market_calendar.previous_trading_day(SESSION)
    cache = _Cache([_daily(prior)])
    store = _Store()
    monkeypatch.setattr(
        market_data,
        "get_regular_session_history",
        lambda client, symbol, session: _complete(prior),
    )

    result = daily_bar_fallback.resolve_daily_history(
        object(),  # type: ignore[arg-type]
        cache,
        store,
        "SPY",
        SESSION,
        clock=lambda: RETRIEVED_AT,
    )

    assert result.state is daily_bar_fallback.DailyBarState.INCOMPLETE
    assert not result.ready
    assert result.diagnostic is not None
    assert result.diagnostic.observed_interval_count == 0
    assert result.diagnostic.expected_interval_count == 78
    assert store.saved == []


def test_provider_error_remains_distinct_from_incomplete_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _Cache([])
    store = _Store()

    def unavailable(*args: object, **kwargs: object) -> list[Candle]:
        del args, kwargs
        raise RuntimeError("offline provider error")

    monkeypatch.setattr(market_data, "get_regular_session_history", unavailable)

    with pytest.raises(RuntimeError, match="offline provider error"):
        daily_bar_fallback.resolve_daily_history(
            object(),  # type: ignore[arg-type]
            cache,
            store,
            "SPY",
            SESSION,
            clock=lambda: RETRIEVED_AT,
        )

    assert store.saved == []


def test_missing_final_interval_remains_retryable_without_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _Cache([])
    store = _Store()
    incomplete = _complete()[:-1]
    monkeypatch.setattr(
        market_data,
        "get_regular_session_history",
        lambda client, symbol, session: incomplete,
    )

    result = daily_bar_fallback.resolve_daily_history(
        object(),  # type: ignore[arg-type]
        cache,
        store,
        "SPY",
        SESSION,
        clock=lambda: RETRIEVED_AT + timedelta(minutes=1),
    )

    assert result.state is daily_bar_fallback.DailyBarState.INCOMPLETE
    assert not result.ready
    assert store.saved == []
