"""Offline tests for the NYSE market calendar: holidays, early closes, and DST."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from schwab_trader import market_calendar as mc


def test_weekday_helpers() -> None:
    # Third Monday of January 2026 (MLK Day) and last Monday of May 2026 (Memorial Day).
    assert mc._nth_weekday(2026, 1, 0, 3) == date(2026, 1, 19)
    assert mc._last_weekday(2026, 5, 0) == date(2026, 5, 25)
    # Existing callers still rely on _nth_sunday for the DST boundaries.
    assert mc._nth_sunday(2026, 3, 2) == date(2026, 3, 8)
    assert mc._nth_sunday(2026, 11, 1) == date(2026, 11, 1)


@pytest.mark.parametrize(
    ("year", "good_friday"),
    [(2025, date(2025, 4, 18)), (2026, date(2026, 4, 3)), (2027, date(2027, 3, 26))],
)
def test_easter_and_good_friday(year: int, good_friday: date) -> None:
    assert mc.easter(year) - timedelta(days=2) == good_friday
    assert mc.market_holidays(year)[good_friday] == "Good Friday"


def test_2026_full_holiday_set() -> None:
    holidays = mc.market_holidays(2026)
    assert holidays == {
        date(2026, 1, 1): "New Year's Day",
        date(2026, 1, 19): "Martin Luther King Jr. Day",
        date(2026, 2, 16): "Washington's Birthday",
        date(2026, 4, 3): "Good Friday",
        date(2026, 5, 25): "Memorial Day",
        date(2026, 6, 19): "Juneteenth National Independence Day",
        date(2026, 7, 3): "Independence Day",  # July 4 is a Saturday -> observed Friday
        date(2026, 9, 7): "Labor Day",
        date(2026, 11, 26): "Thanksgiving Day",
        date(2026, 12, 25): "Christmas Day",
    }


def test_observance_rules() -> None:
    # Sunday holiday -> observed Monday (Juneteenth 2021 fell on Saturday; test Sunday case).
    assert mc.is_holiday(date(2028, 6, 19)) is (date(2028, 6, 19).weekday() < 5)
    # Independence Day 2026: July 4 (Sat) observed on Friday July 3.
    assert mc.is_holiday(date(2026, 7, 3))
    assert not mc.is_holiday(date(2026, 7, 4))
    # New Year's Day on a Saturday is NOT observed on the prior-year Friday.
    assert date(2022, 1, 1).weekday() == 5
    assert not mc.is_holiday(date(2021, 12, 31))
    assert not mc.is_holiday(date(2022, 1, 1))


def test_juneteenth_only_from_2022() -> None:
    assert not mc.is_holiday(date(2021, 6, 18))  # observed Friday for Sat 6/19/2021
    assert mc.is_holiday(date(2022, 6, 20))  # Sunday 6/19/2022 -> observed Monday


def test_is_trading_day() -> None:
    assert mc.is_trading_day(date(2026, 7, 14))  # ordinary Tuesday
    assert not mc.is_trading_day(date(2026, 7, 18))  # Saturday
    assert not mc.is_trading_day(date(2026, 7, 19))  # Sunday
    assert not mc.is_trading_day(date(2026, 12, 25))  # Christmas


def test_early_closes_2025() -> None:
    early = mc.early_closes(2025)
    assert early == {
        date(2025, 7, 3): "Independence Day (early close)",
        date(2025, 11, 28): "Day after Thanksgiving (early close)",
        date(2025, 12, 24): "Christmas Eve (early close)",
    }
    assert mc.is_early_close(date(2025, 12, 24))
    assert mc.session_close(date(2025, 12, 24)) == mc.EARLY_CLOSE


def test_early_close_suppressed_when_full_holiday() -> None:
    # In 2026 July 4 is Saturday, so July 3 is a full holiday, not an early close.
    assert not mc.is_early_close(date(2026, 7, 3))
    assert mc.is_holiday(date(2026, 7, 3))
    # Christmas Eve 2027 becomes the observed Christmas holiday (Dec 25 is Saturday).
    assert not mc.is_early_close(date(2027, 12, 24))
    assert mc.is_holiday(date(2027, 12, 24))


def test_regular_session_close_is_full_on_normal_days() -> None:
    assert mc.session_close(date(2026, 7, 14)) == mc.MARKET_CLOSE


def test_next_and_previous_trading_day_span_gaps() -> None:
    # Friday July 17 2026 -> next trading day skips the weekend to Monday July 20.
    assert mc.next_trading_day(date(2026, 7, 17)) == date(2026, 7, 20)
    assert mc.previous_trading_day(date(2026, 7, 20)) == date(2026, 7, 17)
    # Christmas 2026 (Friday) -> previous trading day skips the early-close Thursday? No:
    # Dec 24 is a trading day (early close), so previous(Dec 25) is Dec 24.
    assert mc.previous_trading_day(date(2026, 12, 25)) == date(2026, 12, 24)
    # New Year's Day 2026 (Thursday) -> previous trading day is Wednesday Dec 31 2025.
    assert mc.previous_trading_day(date(2026, 1, 1)) == date(2025, 12, 31)


def test_eastern_offset_dst() -> None:
    assert mc.eastern_offset_hours(date(2026, 7, 1)) == -4  # EDT
    assert mc.eastern_offset_hours(date(2026, 1, 15)) == -5  # EST
    # Boundaries: DST starts 2026-03-08, ends 2026-11-01.
    assert mc.eastern_offset_hours(date(2026, 3, 7)) == -5
    assert mc.eastern_offset_hours(date(2026, 3, 8)) == -4
    assert mc.eastern_offset_hours(date(2026, 10, 31)) == -4
    assert mc.eastern_offset_hours(date(2026, 11, 1)) == -5


def test_eastern_to_utc_across_dst() -> None:
    # A 16:00 ET close is 20:00 UTC in summer (EDT) and 21:00 UTC in winter (EST).
    summer = mc.eastern_to_utc(datetime(2026, 7, 14, 16, 0))
    winter = mc.eastern_to_utc(datetime(2026, 1, 14, 16, 0))
    assert summer == datetime(2026, 7, 14, 20, 0, tzinfo=UTC)
    assert winter == datetime(2026, 1, 14, 21, 0, tzinfo=UTC)


def test_is_regular_session_boundaries() -> None:
    tue = datetime(2026, 7, 14, 12, 0)
    assert mc.is_regular_session(tue)
    assert not mc.is_regular_session(datetime(2026, 7, 14, 9, 0))  # pre-open
    assert not mc.is_regular_session(datetime(2026, 7, 14, 16, 1))  # post-close
    assert not mc.is_regular_session(datetime(2026, 7, 18, 12, 0))  # Saturday
    assert not mc.is_regular_session(datetime(2026, 12, 25, 12, 0))  # Christmas


def test_is_regular_session_respects_early_close() -> None:
    early_day = date(2025, 12, 24)
    assert mc.is_regular_session(datetime.combine(early_day, mc.EARLY_CLOSE))
    # 13:01 ET on an early-close day is past the session close.
    assert not mc.is_regular_session(datetime(2025, 12, 24, 13, 1))
    # The same wall-clock time on an ordinary day is still in session.
    assert mc.is_regular_session(datetime(2025, 12, 23, 13, 1))
