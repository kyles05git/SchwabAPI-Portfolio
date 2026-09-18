"""Offline exact-session five-minute evidence and aggregation tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from schwab_trader import market_bar_evidence as evidence
from schwab_trader import market_calendar as mc
from schwab_trader import market_data
from schwab_trader.market_data import Candle

SESSION = date(2026, 7, 28)


def _bar(stamp: datetime, index: int, *, symbol: str = "SPY") -> Candle:
    opened = Decimal(100 + index)
    return Candle(
        symbol=symbol,
        date=stamp,
        open=opened,
        high=opened + Decimal("2"),
        low=opened - Decimal("1"),
        close=opened + Decimal("1"),
        volume=index + 1,
    )


def _complete(session: date = SESSION, *, symbol: str = "SPY") -> list[Candle]:
    return [
        _bar(stamp, index, symbol=symbol)
        for index, stamp in enumerate(mc.session_interval_starts_utc(session))
    ]


def _retrieved(session: date = SESSION) -> datetime:
    _, closed = mc.session_bounds_utc(session)
    return closed + timedelta(minutes=1)


def test_complete_normal_session_aggregates_deterministically() -> None:
    bars = _complete()
    result = evidence.validate_regular_session(
        "spy",
        SESSION,
        bars,
        retrieved_at=_retrieved(),
    )

    assert result.aggregation_safe
    assert result.reasons == ()
    assert result.expected_interval_count == 78
    assert result.observed_interval_count == 78
    assert result.unique_interval_count == 78
    assert result.first_interval_at == datetime(2026, 7, 28, 13, 30, tzinfo=UTC)
    assert result.final_interval_at == datetime(2026, 7, 28, 19, 55, tzinfo=UTC)
    assert result.evidence is not None
    derived = result.evidence.candle
    assert derived.source == evidence.DERIVED_DAILY_SOURCE
    assert derived.open == Decimal("100")
    assert derived.high == Decimal("179")
    assert derived.low == Decimal("99")
    assert derived.close == Decimal("178")
    assert derived.volume == sum(range(1, 79))

    repeated = evidence.validate_regular_session(
        "SPY",
        SESSION,
        bars,
        retrieved_at=_retrieved() + timedelta(hours=1),
    )
    assert repeated.evidence is not None
    assert repeated.evidence.dataset_id == result.evidence.dataset_id
    assert repeated.evidence.constituent_digest == result.evidence.constituent_digest


@pytest.mark.parametrize("missing_index", [0, 37, 77])
def test_missing_first_middle_or_final_interval_fails_closed(missing_index: int) -> None:
    bars = _complete()
    missing = bars.pop(missing_index).date

    result = evidence.validate_regular_session(
        "SPY",
        SESSION,
        bars,
        retrieved_at=_retrieved(),
    )

    assert not result.aggregation_safe
    assert evidence.CoverageReason.MISSING_INTERVALS in result.reasons
    assert missing in result.missing_intervals
    assert result.evidence is None


def test_duplicate_interval_fails_closed_even_when_count_is_78() -> None:
    bars = _complete()
    bars[40] = bars[39].model_copy()

    result = evidence.validate_regular_session(
        "SPY",
        SESSION,
        bars,
        retrieved_at=_retrieved(),
    )

    assert result.observed_interval_count == 78
    assert result.unique_interval_count == 77
    assert evidence.CoverageReason.DUPLICATE_INTERVALS in result.reasons
    assert evidence.CoverageReason.MISSING_INTERVALS in result.reasons
    assert not result.aggregation_safe


def test_out_of_order_intervals_fail_closed() -> None:
    bars = _complete()
    bars[20], bars[21] = bars[21], bars[20]

    result = evidence.validate_regular_session(
        "SPY",
        SESSION,
        bars,
        retrieved_at=_retrieved(),
    )

    assert result.out_of_order
    assert result.reasons == (evidence.CoverageReason.OUT_OF_ORDER,)
    assert not result.aggregation_safe


def test_premarket_and_after_hours_intervals_fail_closed() -> None:
    bars = _complete()
    opened, closed = mc.session_bounds_utc(SESSION)
    bars = [_bar(opened - timedelta(minutes=5), -1), *bars, _bar(closed, 78)]

    result = evidence.validate_regular_session(
        "SPY",
        SESSION,
        bars,
        retrieved_at=_retrieved(),
    )

    assert result.unexpected_intervals == (
        opened - timedelta(minutes=5),
        closed,
    )
    assert result.reasons == (evidence.CoverageReason.UNEXPECTED_INTERVALS,)
    assert not result.aggregation_safe


def test_previous_session_with_78_candles_is_rejected() -> None:
    previous = mc.previous_trading_day(SESSION)
    bars = _complete(previous)

    result = evidence.validate_regular_session(
        "SPY",
        SESSION,
        bars,
        retrieved_at=_retrieved(),
    )

    assert len(bars) == 78
    assert result.observed_interval_count == 0
    assert result.unique_interval_count == 0
    assert len(result.missing_intervals) == 78
    assert len(result.unexpected_intervals) == 78
    assert result.first_returned_interval_at == bars[0].date
    assert result.final_returned_interval_at == bars[-1].date
    assert not result.aggregation_safe


def test_retrieval_must_be_after_official_close() -> None:
    _, closed = mc.session_bounds_utc(SESSION)
    result = evidence.validate_regular_session(
        "SPY",
        SESSION,
        _complete(),
        retrieved_at=closed,
    )
    assert result.reasons == (evidence.CoverageReason.RETRIEVED_BEFORE_CLOSE,)
    assert not result.aggregation_safe


@pytest.mark.parametrize(
    "update",
    [
        {"open": None},
        {"high": Decimal("99")},
        {"low": Decimal("114")},
        {"close": Decimal("0")},
    ],
)
def test_invalid_ohlc_fails_closed(update: dict[str, Decimal | None]) -> None:
    bars = _complete()
    bars[12] = bars[12].model_copy(update=update)
    result = evidence.validate_regular_session(
        "SPY",
        SESSION,
        bars,
        retrieved_at=_retrieved(),
    )
    assert evidence.CoverageReason.INVALID_OHLC in result.reasons
    assert not result.aggregation_safe


def test_negative_volume_fails_closed() -> None:
    bars = _complete()
    bars[12] = bars[12].model_copy(update={"volume": -1})
    result = evidence.validate_regular_session(
        "SPY",
        SESSION,
        bars,
        retrieved_at=_retrieved(),
    )
    assert result.reasons == (evidence.CoverageReason.NEGATIVE_VOLUME,)
    assert not result.aggregation_safe


def test_early_close_has_42_intervals_ending_at_1255_et() -> None:
    early = date(2025, 11, 28)
    result = evidence.validate_regular_session(
        "SPY",
        early,
        _complete(early),
        retrieved_at=_retrieved(early),
    )

    assert result.aggregation_safe
    assert result.expected_interval_count == 42
    assert result.final_interval_at == datetime(2025, 11, 28, 17, 55, tzinfo=UTC)
    assert result.evidence is not None
    assert len(result.evidence.constituents) == 42


@pytest.mark.parametrize(
    "closed",
    [date(2026, 7, 25), date(2026, 12, 25)],
)
def test_weekend_and_holiday_have_no_session_to_aggregate(closed: date) -> None:
    result = evidence.validate_regular_session(
        "SPY",
        closed,
        [],
        retrieved_at=datetime(2026, 12, 26, tzinfo=UTC),
    )
    assert result.reasons == (evidence.CoverageReason.CLOSED_SESSION,)
    assert result.expected_interval_count == 0


def test_dst_changes_exact_utc_bounds_without_changing_interval_count() -> None:
    winter = date(2026, 1, 14)
    summer = date(2026, 7, 14)
    winter_starts = mc.session_interval_starts_utc(winter)
    summer_starts = mc.session_interval_starts_utc(summer)

    assert len(winter_starts) == len(summer_starts) == 78
    assert winter_starts[0].hour == 14
    assert summer_starts[0].hour == 13


class _CapturingClient:
    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body
        self.path = ""
        self.params: dict[str, object] = {}

    def get(self, path: str, *, params: dict[str, object]) -> dict[str, Any]:
        self.path = path
        self.params = params
        return self.body


def test_exact_session_request_uses_explicit_dates_and_preserves_provider_order() -> None:
    starts = mc.session_interval_starts_utc(SESSION)
    body = {
        "candles": [
            {
                "datetime": int(starts[1].timestamp() * 1000),
                "open": 2,
                "high": 3,
                "low": 1,
                "close": 2,
                "volume": 2,
            },
            {
                "datetime": int(starts[0].timestamp() * 1000),
                "open": 1,
                "high": 2,
                "low": 1,
                "close": 2,
                "volume": 1,
            },
        ]
    }
    client = _CapturingClient(body)
    candles = market_data.get_regular_session_history(  # type: ignore[arg-type]
        client,
        "spy",
        SESSION,
    )
    opened, closed = mc.session_bounds_utc(SESSION)

    assert client.path == market_data.PRICE_HISTORY_PATH
    assert client.params == {
        "symbol": "SPY",
        "frequencyType": "minute",
        "frequency": 5,
        "startDate": int(opened.timestamp() * 1000),
        "endDate": int(closed.timestamp() * 1000),
        "needExtendedHoursData": "false",
    }
    assert "period" not in client.params
    assert [candle.date for candle in candles] == [starts[1], starts[0]]


def test_diagnostic_payload_has_only_stable_sanitized_fields() -> None:
    result = evidence.validate_regular_session(
        "SPY",
        SESSION,
        _complete(),
        retrieved_at=_retrieved(),
    )
    payload = evidence.diagnostic_payload(result)
    encoded = str(payload).casefold()

    assert payload["schema"] == evidence.PAYLOAD_SCHEMA
    assert payload["aggregation_safe"] is True
    assert payload["derived"] is not None
    for forbidden in ("token", "credential", "database_url", "connection_string"):
        assert forbidden not in encoded
