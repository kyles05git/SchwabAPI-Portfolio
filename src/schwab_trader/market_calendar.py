"""US equity (NYSE/``XNYS``) market-calendar helpers in Eastern time, DST-aware.

The regular session is 09:30-16:00 ET, Monday-Friday. Early-close sessions end at
13:00 ET. Exchange holidays, observance shifts, and early closes are modeled here so
that scheduling primitives (:mod:`schwab_trader.scheduling`) can decide when an
official paper-sleeve run is due without any network or broker access.

All datetimes produced here are **naive wall-clock Eastern time** unless a function
name says otherwise. The 2 a.m. DST transition instant never lands inside a trading
session (the market opens at 09:30 and closes by 16:00), so the UTC offset is chosen
by calendar date, which is correct for every hour that matters. Use
:func:`eastern_to_utc` when an unambiguous instant is required.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)

# Market Identifier Code for the primary US equity session this calendar models.
EXCHANGE_MIC = "XNYS"

# NYSE began observing Juneteenth as a full holiday in 2022.
_JUNETEENTH_FIRST_YEAR = 2022


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Date of the ``n``-th ``weekday`` (Mon=0 .. Sun=6) of a month."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Date of the last ``weekday`` (Mon=0 .. Sun=6) of a month."""
    next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last = next_month - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _nth_sunday(year: int, month: int, n: int) -> date:
    """Date of the ``n``-th Sunday of a month (retained for existing callers)."""
    return _nth_weekday(year, month, 6, n)


def _observed(holiday: date) -> date:
    """NYSE observance shift: Saturday -> preceding Friday, Sunday -> following Monday."""
    if holiday.weekday() == 5:  # Saturday
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:  # Sunday
        return holiday + timedelta(days=1)
    return holiday


def easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous computus). Good Friday is two days earlier."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def market_holidays(year: int) -> dict[date, str]:
    """Observed NYSE full-closure holidays for ``year`` keyed by the closed date.

    Weekend holidays are shifted with the NYSE observance rule. New Year's Day is not
    observed on the preceding Friday when January 1 falls on a Saturday, because that
    would move the closure into the prior calendar year.
    """
    holidays: dict[date, str] = {}

    new_year = date(year, 1, 1)
    if new_year.weekday() != 5:  # Saturday Jan 1 -> no prior-year Friday closure
        holidays[_observed(new_year)] = "New Year's Day"

    holidays[_nth_weekday(year, 1, 0, 3)] = "Martin Luther King Jr. Day"
    holidays[_nth_weekday(year, 2, 0, 3)] = "Washington's Birthday"
    holidays[easter(year) - timedelta(days=2)] = "Good Friday"
    holidays[_last_weekday(year, 5, 0)] = "Memorial Day"
    if year >= _JUNETEENTH_FIRST_YEAR:
        holidays[_observed(date(year, 6, 19))] = "Juneteenth National Independence Day"
    holidays[_observed(date(year, 7, 4))] = "Independence Day"
    holidays[_nth_weekday(year, 9, 0, 1)] = "Labor Day"
    holidays[_nth_weekday(year, 11, 3, 4)] = "Thanksgiving Day"
    holidays[_observed(date(year, 12, 25))] = "Christmas Day"
    return holidays


def is_holiday(day: date) -> bool:
    """True if ``day`` is an observed NYSE full-closure holiday."""
    return day in market_holidays(day.year)


def holiday_name(day: date) -> str | None:
    """The observed holiday name for ``day`` if it is a full closure, else ``None``."""
    return market_holidays(day.year).get(day)


def is_weekend(day: date) -> bool:
    """True on Saturday or Sunday."""
    return day.weekday() >= 5


def is_trading_day(day: date) -> bool:
    """True on a weekday that is not an observed full-closure holiday."""
    return not is_weekend(day) and not is_holiday(day)


def early_closes(year: int) -> dict[date, str]:
    """Observed NYSE 13:00 ET early-close sessions for ``year`` keyed by date.

    Modeled early closes: the trading day before Independence Day (July 3 when both it
    and July 4 are weekdays), the Friday after Thanksgiving, and Christmas Eve when it
    is a regular trading session.
    """
    early: dict[date, str] = {}

    july_third = date(year, 7, 3)
    if date(year, 7, 4).weekday() < 5 and is_trading_day(july_third):
        early[july_third] = "Independence Day (early close)"

    day_after_thanksgiving = _nth_weekday(year, 11, 3, 4) + timedelta(days=1)
    if is_trading_day(day_after_thanksgiving):
        early[day_after_thanksgiving] = "Day after Thanksgiving (early close)"

    christmas_eve = date(year, 12, 24)
    if is_trading_day(christmas_eve):
        early[christmas_eve] = "Christmas Eve (early close)"

    return early


def is_early_close(day: date) -> bool:
    """True if ``day`` is a trading day that closes early at 13:00 ET."""
    return day in early_closes(day.year)


def early_close_name(day: date) -> str | None:
    """The early-close label for ``day`` if it is an early close, else ``None``."""
    return early_closes(day.year).get(day)


def session_close(day: date) -> time:
    """The scheduled closing wall-clock time for ``day`` (13:00 on early closes)."""
    return EARLY_CLOSE if is_early_close(day) else MARKET_CLOSE


def session_bounds_utc(day: date) -> tuple[datetime, datetime]:
    """Return the exact regular-session ``(open, close)`` instants in UTC.

    A closed exchange date has no session bounds. Refusing it here keeps provider
    request construction and evidence validation from silently treating a weekend or
    holiday as an ordinary day.
    """
    if not is_trading_day(day):
        raise ValueError(f"{day.isoformat()} is not an XNYS trading session")
    return (
        eastern_to_utc(datetime.combine(day, MARKET_OPEN)),
        eastern_to_utc(datetime.combine(day, session_close(day))),
    )


def session_interval_starts_utc(
    day: date,
    *,
    minutes: int = 5,
) -> tuple[datetime, ...]:
    """Exact regular-session interval starts, from 09:30 through close-exclusive.

    A normal 09:30-16:00 session has 78 five-minute starts ending at 15:55 ET. A
    09:30-13:00 early close has 42 ending at 12:55 ET.
    """
    if minutes <= 0:
        raise ValueError("minutes must be positive")
    opened, closed = session_bounds_utc(day)
    step = timedelta(minutes=minutes)
    starts: list[datetime] = []
    current = opened
    while current < closed:
        starts.append(current)
        current += step
    if current != closed:
        raise ValueError(f"{minutes}-minute intervals do not divide the {day.isoformat()} session")
    return tuple(starts)


def next_trading_day(day: date) -> date:
    """The first trading day strictly after ``day``."""
    nxt = day + timedelta(days=1)
    while not is_trading_day(nxt):
        nxt += timedelta(days=1)
    return nxt


def previous_trading_day(day: date) -> date:
    """The last trading day strictly before ``day``."""
    prev = day - timedelta(days=1)
    while not is_trading_day(prev):
        prev -= timedelta(days=1)
    return prev


def eastern_offset_hours(day: date) -> int:
    """UTC offset (in hours) for US/Eastern on ``day``: -4 during DST, else -5.

    DST runs from the 2nd Sunday of March to the 1st Sunday of November.
    """
    dst_start = _nth_sunday(day.year, 3, 2)
    dst_end = _nth_sunday(day.year, 11, 1)
    return -4 if dst_start <= day < dst_end else -5


def eastern_to_utc(eastern_naive: datetime) -> datetime:
    """Convert a naive Eastern wall-clock ``datetime`` to an aware UTC ``datetime``."""
    offset = eastern_offset_hours(eastern_naive.date())
    return (eastern_naive - timedelta(hours=offset)).replace(tzinfo=UTC)


def eastern_now() -> datetime:
    """Current wall-clock time in US/Eastern as a naive ``datetime``."""
    utc = datetime.now(UTC)
    offset = eastern_offset_hours(utc.date())
    return (utc + timedelta(hours=offset)).replace(tzinfo=None)


def is_regular_session(now_et: datetime) -> bool:
    """True during an open regular session, honoring weekends, holidays, early closes.

    The window runs from 09:30 ET to the day's scheduled close (16:00, or 13:00 on an
    early-close day).
    """
    if not is_trading_day(now_et.date()):
        return False
    return MARKET_OPEN <= now_et.time() <= session_close(now_et.date())
