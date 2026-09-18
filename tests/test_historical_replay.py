"""Calendar planning, normalization, and deterministic digests for historical replay.

Offline and hermetic. Every candle here is synthetic: prices and volumes are invented
round numbers, not the user's market data. Nothing in this file opens a socket, reads
``.env`` or a token, connects to a database, runs a cohort, or touches an order path.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from schwab_trader import historical_replay, market_calendar
from schwab_trader.historical_replay import (
    ReplayIssue,
    ReplaySessionStatus,
)
from schwab_trader.market_data import Candle

UNIVERSE = historical_replay.universe(["AAPL", "MSFT"], label="replay-test")
RETRIEVED = datetime(2026, 7, 30, 21, 30, tzinfo=UTC)

#: A normal 09:30-16:00 ET session.
NORMAL = date(2026, 7, 29)
#: 2026-11-27 is the Friday after Thanksgiving: a 13:00 ET early close.
EARLY = date(2026, 11, 27)


def _synthetic(
    symbol: str,
    session: date,
    *,
    intervals: tuple[datetime, ...] | None = None,
    base: str = "100",
) -> list[Candle]:
    """Synthetic candles at ``intervals`` (default: the whole calendar session)."""
    starts = intervals or market_calendar.session_interval_starts_utc(session, minutes=5)
    price = Decimal(base)
    return [
        Candle(
            symbol=symbol,
            date=moment,
            open=price + index,
            high=price + index + 1,
            low=price + index - 1,
            close=price + index,
            volume=1000 + index,
            source="schwab-regular-session-5m",
        )
        for index, moment in enumerate(starts)
    ]


def _validate(session: date, candles: list[Candle], symbol: str = "AAPL"):
    return historical_replay.validate_replay_session(
        UNIVERSE,
        symbol,
        session,
        candles,
        retrieved_at=RETRIEVED,
        source="schwab-regular-session-5m",
    )


# --- planning ---------------------------------------------------------------------


def test_thirty_completed_sessions_skip_weekends_and_holidays() -> None:
    # 2026-07-30 18:00 ET, after that session's close.
    now = datetime(2026, 7, 30, 22, 0, tzinfo=UTC)
    sessions = historical_replay.completed_sessions(now, count=30)

    assert len(sessions) == 30
    assert sessions == tuple(sorted(sessions))
    assert sessions[-1] == date(2026, 7, 30)
    assert all(market_calendar.is_trading_day(item) for item in sessions)
    assert not any(item.weekday() >= 5 for item in sessions)
    # Independence Day 2026 falls on a Saturday and is observed Friday 2026-07-03.
    assert date(2026, 7, 3) not in sessions
    assert date(2026, 7, 4) not in sessions


def test_an_in_progress_session_is_never_planned() -> None:
    """13:00 ET on a normal trading day: today's close has not happened yet."""
    midday = datetime(2026, 7, 30, 17, 0, tzinfo=UTC)
    sessions = historical_replay.completed_sessions(midday, count=3)

    assert date(2026, 7, 30) not in sessions
    assert sessions[-1] == date(2026, 7, 29)


def test_an_early_close_session_completes_at_thirteen_hundred() -> None:
    """14:00 ET on the Friday after Thanksgiving is *after* that 13:00 ET close."""
    after_early_close = datetime(2026, 11, 27, 19, 0, tzinfo=UTC)
    sessions = historical_replay.completed_sessions(after_early_close, count=1)

    assert sessions == (EARLY,)
    assert market_calendar.is_early_close(EARLY)


def test_planning_spans_the_dst_transitions_without_drift() -> None:
    """A window covering the November fallback still yields real trading days."""
    now = datetime(2026, 11, 20, 22, 0, tzinfo=UTC)
    sessions = historical_replay.completed_sessions(now, count=30)

    # DST ends on the first Sunday of November (2026-11-01), so this window straddles it.
    assert min(sessions) < date(2026, 11, 1) < max(sessions)
    for session in sessions:
        opened, closed = market_calendar.session_bounds_utc(session)
        # The UTC offset changes, but the session is always 09:30-16:00 *Eastern*.
        assert (closed - opened) == timedelta(hours=6, minutes=30)
    assert market_calendar.eastern_offset_hours(date(2026, 10, 30)) == -4
    assert market_calendar.eastern_offset_hours(date(2026, 11, 2)) == -5


def test_a_naive_planning_instant_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone"):
        historical_replay.completed_sessions(datetime(2026, 7, 30, 22, 0), count=1)


def test_expected_bar_counts_come_from_the_calendar() -> None:
    assert historical_replay.expected_bar_count(NORMAL) == 78
    assert historical_replay.expected_bar_count(EARLY) == 42


# --- request shape ----------------------------------------------------------------


def test_request_params_use_explicit_session_bounds_and_no_extended_hours() -> None:
    params = historical_replay.request_params("aapl", NORMAL)
    opened, closed = market_calendar.session_bounds_utc(NORMAL)

    assert params["symbol"] == "AAPL"
    assert params["frequencyType"] == "minute"
    assert params["frequency"] == 5
    assert params["startDate"] == int(opened.timestamp() * 1000)
    assert params["endDate"] == int(closed.timestamp() * 1000)
    assert params["needExtendedHoursData"] == "false"
    # The date-free shape Schwab can answer with the *previous* session.
    assert "periodType" not in params
    assert "period" not in params


def test_recorded_params_match_what_the_official_helper_actually_sends() -> None:
    """The evidence must describe the real request, not a plausible-looking copy."""
    from schwab_trader import market_data

    captured: dict[str, object] = {}

    class _CapturingClient:
        def get(self, path: str, *, params: dict[str, object] | None = None) -> dict[str, object]:
            captured["path"] = path
            captured["params"] = params
            return {"candles": []}

    market_data.get_regular_session_history(_CapturingClient(), "AAPL", NORMAL)  # type: ignore[arg-type]

    assert captured["params"] == historical_replay.request_params("AAPL", NORMAL)


# --- the standard session ---------------------------------------------------------


def test_a_full_seventy_eight_bar_session_is_complete() -> None:
    result = _validate(NORMAL, _synthetic("AAPL", NORMAL))

    assert result.status is ReplaySessionStatus.COMPLETE
    assert result.issues == ()
    assert result.expected_bar_count == 78
    assert result.unique_bar_count == 78
    assert len(result.bars) == 78
    assert result.replay_id.startswith("historical-replay:")
    assert result.universe_id == UNIVERSE.universe_id
    assert result.universe_symbols == ("AAPL", "MSFT")


def test_an_early_close_session_is_complete_at_forty_two_bars() -> None:
    result = _validate(EARLY, _synthetic("AAPL", EARLY))

    assert result.status is ReplaySessionStatus.COMPLETE
    assert result.expected_bar_count == 42
    assert result.unique_bar_count == 42


def test_a_full_length_payload_on_an_early_close_day_is_out_of_session() -> None:
    """78 bars on a 13:00 ET close means 36 bars the exchange was shut for."""
    normal_length = market_calendar.session_interval_starts_utc(NORMAL, minutes=5)
    shifted = tuple(
        moment.replace(year=EARLY.year, month=EARLY.month, day=EARLY.day)
        for moment in normal_length
    )
    result = _validate(EARLY, _synthetic("AAPL", EARLY, intervals=shifted))

    assert result.status is ReplaySessionStatus.INCOMPLETE
    assert ReplayIssue.OUT_OF_SESSION_BAR in result.issues


def test_a_weekend_or_holiday_is_unavailable_not_empty() -> None:
    for closed_day in (date(2026, 7, 25), date(2026, 7, 3)):  # Saturday, observed July 4
        result = _validate(closed_day, [])
        assert result.status is ReplaySessionStatus.UNAVAILABLE
        assert ReplayIssue.CLOSED_SESSION in result.issues
        assert result.bars == ()


# --- defective payloads -----------------------------------------------------------


def test_a_missing_final_bar_is_reported_precisely() -> None:
    starts = market_calendar.session_interval_starts_utc(NORMAL, minutes=5)
    result = _validate(NORMAL, _synthetic("AAPL", NORMAL, intervals=starts[:-1]))

    assert result.status is ReplaySessionStatus.INCOMPLETE
    assert result.issues == (ReplayIssue.MISSING_FINAL_BAR,)
    assert result.missing_intervals == (starts[-1],)
    assert result.unique_bar_count == 77


def test_an_interior_gap_is_distinguished_from_a_missing_tail() -> None:
    starts = market_calendar.session_interval_starts_utc(NORMAL, minutes=5)
    kept = starts[:20] + starts[23:]
    result = _validate(NORMAL, _synthetic("AAPL", NORMAL, intervals=kept))

    assert result.status is ReplaySessionStatus.INCOMPLETE
    assert result.issues == (ReplayIssue.INTERIOR_GAP,)
    assert result.missing_intervals == starts[20:23]


def test_a_missing_opening_bar_is_named_on_its_own() -> None:
    starts = market_calendar.session_interval_starts_utc(NORMAL, minutes=5)
    result = _validate(NORMAL, _synthetic("AAPL", NORMAL, intervals=starts[1:]))

    assert result.issues == (ReplayIssue.MISSING_OPENING_BAR,)
    assert result.missing_intervals == (starts[0],)


def test_an_exact_duplicate_is_recorded_and_deduplicated() -> None:
    candles = _synthetic("AAPL", NORMAL)
    duplicated = [*candles, candles[10]]
    result = _validate(NORMAL, duplicated)

    assert result.status is ReplaySessionStatus.COMPLETE
    assert result.issues == (ReplayIssue.DUPLICATE_TIMESTAMP,)
    assert result.duplicate_intervals == (candles[10].date,)
    assert result.returned_bar_count == 79
    assert result.unique_bar_count == 78
    # The dedup keeps one copy, so the digest matches the clean payload exactly.
    assert result.normalized_bar_digest == _validate(NORMAL, candles).normalized_bar_digest


def test_a_conflicting_duplicate_demotes_the_session() -> None:
    candles = _synthetic("AAPL", NORMAL)
    conflicting = candles[10].model_copy(update={"close": Decimal("999")})
    result = _validate(NORMAL, [*candles, conflicting])

    assert result.status is ReplaySessionStatus.INCOMPLETE
    assert ReplayIssue.CONFLICTING_DUPLICATE in result.issues


def test_out_of_session_bars_are_excluded_and_reported() -> None:
    opened, closed = market_calendar.session_bounds_utc(NORMAL)
    premarket = opened - timedelta(minutes=30)
    afterhours = closed + timedelta(minutes=5)
    candles = _synthetic("AAPL", NORMAL)
    intruders = _synthetic("AAPL", NORMAL, intervals=(premarket, afterhours))
    result = _validate(NORMAL, [*intruders, *candles])

    assert result.status is ReplaySessionStatus.INCOMPLETE
    assert result.issues == (ReplayIssue.OUT_OF_SESSION_BAR,)
    assert result.out_of_session_intervals == (premarket, afterhours)
    assert all(opened <= bar.interval_at < closed for bar in result.bars)


def test_an_empty_payload_is_incomplete_with_every_interval_missing() -> None:
    result = _validate(NORMAL, [])

    assert result.status is ReplaySessionStatus.INCOMPLETE
    assert ReplayIssue.EMPTY_PAYLOAD in result.issues
    assert len(result.missing_intervals) == 78


def test_an_impossible_ohlc_bar_is_flagged() -> None:
    candles = _synthetic("AAPL", NORMAL)
    candles[5] = candles[5].model_copy(update={"high": Decimal("1"), "low": Decimal("500")})
    result = _validate(NORMAL, candles)

    assert result.status is ReplaySessionStatus.INCOMPLETE
    assert result.invalid_ohlc_intervals == (candles[5].date,)


def test_a_naive_provider_timestamp_never_silently_becomes_a_bar() -> None:
    candles = _synthetic("AAPL", NORMAL)
    candles[5] = candles[5].model_copy(update={"date": candles[5].date.replace(tzinfo=None)})
    result = _validate(NORMAL, candles)

    assert ReplayIssue.INVALID_TIMESTAMP in result.issues
    assert result.unique_bar_count == 77


def test_a_provider_failure_becomes_unavailable_evidence() -> None:
    result = historical_replay.unavailable_session(
        UNIVERSE,
        "AAPL",
        NORMAL,
        retrieved_at=RETRIEVED,
        error="ApiError:503",
        source="schwab-regular-session-5m",
    )

    assert result.status is ReplaySessionStatus.UNAVAILABLE
    assert result.issues == (ReplayIssue.PROVIDER_ERROR,)
    assert result.error == "ApiError:503"
    assert result.bars == ()
    # Still a full, reproducible record: expectations and request shape are preserved.
    assert result.expected_bar_count == 78
    assert result.request_params["needExtendedHoursData"] == "false"


def test_two_different_failures_are_two_different_observations() -> None:
    def failure(error: str) -> str:
        return historical_replay.unavailable_session(
            UNIVERSE,
            "AAPL",
            NORMAL,
            retrieved_at=RETRIEVED,
            error=error,
            source="schwab-regular-session-5m",
        ).replay_id

    assert failure("ApiError:503") != failure("TransportFailure")


# --- determinism ------------------------------------------------------------------


def test_the_normalized_digest_ignores_provider_ordering() -> None:
    candles = _synthetic("AAPL", NORMAL)
    shuffled = list(candles)
    random.Random(7).shuffle(shuffled)

    ordered = _validate(NORMAL, candles)
    scrambled = _validate(NORMAL, shuffled)

    assert scrambled.normalized_bar_digest == ordered.normalized_bar_digest
    assert scrambled.replay_id == ordered.replay_id
    assert scrambled.bars == ordered.bars
    # The raw digest still shows the payloads were not identical.
    assert scrambled.raw_payload_digest != ordered.raw_payload_digest


def test_the_digest_is_stable_across_equal_decimal_spellings() -> None:
    candles = _synthetic("AAPL", NORMAL)
    respelled = [
        candle.model_copy(update={"close": Decimal(f"{candle.close}.000")}) for candle in candles
    ]

    assert _validate(NORMAL, respelled).normalized_bar_digest == (
        _validate(NORMAL, candles).normalized_bar_digest
    )


def test_the_digest_changes_when_a_price_changes() -> None:
    candles = _synthetic("AAPL", NORMAL)
    corrected = list(candles)
    corrected[40] = corrected[40].model_copy(update={"close": Decimal("123.45")})

    assert _validate(NORMAL, corrected).normalized_bar_digest != (
        _validate(NORMAL, candles).normalized_bar_digest
    )


def test_two_symbols_with_identical_prices_get_different_identities() -> None:
    apple = _validate(NORMAL, _synthetic("AAPL", NORMAL), symbol="AAPL")
    micro = _validate(NORMAL, _synthetic("MSFT", NORMAL), symbol="MSFT")

    assert apple.normalized_bar_digest != micro.normalized_bar_digest
    assert apple.replay_id != micro.replay_id


def test_the_universe_identity_is_cohort_independent_and_order_free() -> None:
    one = historical_replay.universe(["msft", " aapl ", "AAPL"])
    two = historical_replay.universe(["AAPL", "MSFT"], label="different label")

    assert one.symbols == ("AAPL", "MSFT")
    assert one.universe_id == two.universe_id


def test_the_evidence_payload_carries_no_secret_or_account_field() -> None:
    import json

    text = json.dumps(historical_replay.evidence_payload(_validate(NORMAL, []))).lower()
    for forbidden in ("bearer", "authorization", "token", "client_secret", "postgresql://", "@"):
        assert forbidden not in text, forbidden
