"""Research-only historical replay of regular-hours five-minute bars.

This module is a **research/replay boundary**. Everything it produces is
:class:`ReplaySessionEvidence` — deliberately a different type, with a different
identity prefix, a different schema name, and different storage tables from the
forward official evidence in :mod:`schwab_trader.market_bar_evidence`.

Replay evidence must never:

* create or alter an official cohort observation;
* create a paper fill, order, position, valuation, or cash movement;
* satisfy forward cohort readiness;
* be promoted into a production data snapshot.

Nothing here fetches, sleeps, persists, authenticates, or mutates cohort/paper state.
Acquisition lives in :mod:`schwab_trader.historical_replay_acquire`; persistence lives
in :mod:`schwab_trader.storage.historical_replay`.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from schwab_trader import market_calendar
from schwab_trader.market_data import Candle

#: Interval size this workflow requests and validates.
INTERVAL_MINUTES = 5

#: Default depth: the previous 30 *completed* exchange sessions.
DEFAULT_SESSION_COUNT = 30

#: Stable payload schema for research evidence. Intentionally not the official
#: ``schwab-intraday-daily-evidence/1`` used by forward cohort snapshots.
RESEARCH_EVIDENCE_SCHEMA = "historical-replay-research-evidence/1"

#: Identity prefix. A replay id can never be mistaken for — or collide with — an
#: official ``schwab-intraday-derived-daily:`` dataset id.
REPLAY_ID_PREFIX = "historical-replay"

#: Digest namespace, so a replay digest never equals an official one over the same bars.
REPLAY_NAMESPACE = "historical_replay"

#: The provider these bars come from. Recorded, never inferred at read time.
SCHWAB_PROVIDER = "schwab"

#: How many calendar days back the planner is willing to scan for ``count`` sessions.
_MAX_PLANNING_SPAN_DAYS = 400


class ReplaySessionStatus(StrEnum):
    """How one (symbol, session) retrieval turned out."""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    UNAVAILABLE = "unavailable"


class ReplayIssue(StrEnum):
    """Stable, sanitized reasons a retrieval is not a clean complete session."""

    CLOSED_SESSION = "closed_session"
    EMPTY_PAYLOAD = "empty_payload"
    MISSING_OPENING_BAR = "missing_opening_bar"
    MISSING_FINAL_BAR = "missing_final_bar"
    INTERIOR_GAP = "interior_gap"
    DUPLICATE_TIMESTAMP = "duplicate_timestamp"
    CONFLICTING_DUPLICATE = "conflicting_duplicate"
    OUT_OF_SESSION_BAR = "out_of_session_bar"
    INVALID_TIMESTAMP = "invalid_timestamp"
    INVALID_OHLC = "invalid_ohlc"
    NEGATIVE_VOLUME = "negative_volume"
    PROVIDER_ERROR = "provider_error"


#: Issues that describe the payload but do not make the session unusable for replay.
#: An exact duplicate of a bar carries no conflicting information once deduplicated,
#: so it is recorded in the evidence and does not by itself demote the session.
_BENIGN_ISSUES = frozenset({ReplayIssue.DUPLICATE_TIMESTAMP})

#: Issues meaning no usable payload existed at all, as opposed to a defective one.
_UNAVAILABLE_ISSUES = frozenset({ReplayIssue.PROVIDER_ERROR, ReplayIssue.CLOSED_SESSION})


class ReplayUniverse(BaseModel):
    """A cohort-independent symbol set.

    The identity digest covers only the normalized symbols and this module's research
    namespace. No cohort id, sleeve id, or run id participates, so a universe can never
    be used to look up — or be mistaken for — an official cohort membership.
    """

    model_config = ConfigDict(frozen=True)

    universe_id: str
    label: str
    symbols: tuple[str, ...]


class ReplayBar(BaseModel):
    """One normalized in-session five-minute bar."""

    model_config = ConfigDict(frozen=True)

    interval_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


class ReplaySessionEvidence(BaseModel):
    """Deterministic research evidence for one (symbol, session) retrieval.

    ``raw_payload_digest`` covers exactly what the provider returned, in provider
    order, so a reordering or a duplicate is visible. ``normalized_bar_digest`` covers
    the deduplicated in-session bars in chronological order, so two retrievals of the
    same underlying data agree regardless of the order they arrived in.
    """

    model_config = ConfigDict(frozen=True)

    schema_name: str = RESEARCH_EVIDENCE_SCHEMA
    replay_id: str
    universe_id: str
    universe_symbols: tuple[str, ...]
    symbol: str
    session_date: date
    provider: str = SCHWAB_PROVIDER
    source: str
    retrieved_at: datetime
    request_params: dict[str, str | int | bool]
    raw_payload_digest: str
    normalized_bar_digest: str
    status: ReplaySessionStatus
    expected_bar_count: int
    returned_bar_count: int
    in_session_bar_count: int
    unique_bar_count: int
    missing_intervals: tuple[datetime, ...] = ()
    duplicate_intervals: tuple[datetime, ...] = ()
    out_of_session_intervals: tuple[datetime, ...] = ()
    invalid_ohlc_intervals: tuple[datetime, ...] = ()
    negative_volume_intervals: tuple[datetime, ...] = ()
    issues: tuple[ReplayIssue, ...] = ()
    error: str | None = None
    bars: tuple[ReplayBar, ...] = ()


def _digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = value.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


def _aware_utc(value: datetime) -> datetime | None:
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


def universe(symbols: list[str] | tuple[str, ...], *, label: str = "") -> ReplayUniverse:
    """Build a cohort-independent universe from ``symbols``.

    Symbols are upper-cased, stripped, deduplicated, and sorted, so the same set spelled
    differently yields one identity.
    """
    normalized = tuple(sorted({item.strip().upper() for item in symbols if item.strip()}))
    if not normalized:
        raise ValueError("a replay universe needs at least one symbol")
    universe_id = _digest(
        {
            "namespace": REPLAY_NAMESPACE,
            "exchange": market_calendar.EXCHANGE_MIC,
            "symbols": list(normalized),
        }
    )
    return ReplayUniverse(
        universe_id=universe_id,
        label=label.strip(),
        symbols=normalized,
    )


def _eastern_date(instant: datetime) -> date:
    utc = instant.astimezone(UTC)
    offset = market_calendar.eastern_offset_hours(utc.date())
    return (utc + timedelta(hours=offset)).date()


def completed_sessions(
    now: datetime,
    *,
    count: int = DEFAULT_SESSION_COUNT,
) -> tuple[date, ...]:
    """The previous ``count`` completed XNYS sessions at ``now``, oldest first.

    A session counts only once its scheduled close has passed, so an in-progress
    session is never planned. Weekends, holidays, and early closes are resolved by the
    exchange calendar rather than by subtracting calendar days.
    """
    if count <= 0:
        raise ValueError("count must be positive")
    instant = _aware_utc(now)
    if instant is None:
        raise ValueError("now must include a timezone")

    found: list[date] = []
    candidate = _eastern_date(instant)
    for _ in range(_MAX_PLANNING_SPAN_DAYS):
        if len(found) == count:
            break
        if market_calendar.is_trading_day(candidate):
            _, closed = market_calendar.session_bounds_utc(candidate)
            if instant > closed:
                found.append(candidate)
        candidate -= timedelta(days=1)
    if len(found) != count:
        raise ValueError(
            f"could not find {count} completed sessions within "
            f"{_MAX_PLANNING_SPAN_DAYS} days before {instant.isoformat()}"
        )
    return tuple(reversed(found))


def expected_bar_count(session: date, *, minutes: int = INTERVAL_MINUTES) -> int:
    """Calendar-derived expected bar count: 78 for a normal session, 42 early-close."""
    return len(market_calendar.session_interval_starts_utc(session, minutes=minutes))


def request_params(
    symbol: str,
    session: date,
    *,
    minutes: int = INTERVAL_MINUTES,
) -> dict[str, str | int | bool]:
    """The exact sanitized price-history parameters this workflow records and sends.

    Explicit ``startDate``/``endDate`` bounds are mandatory: Schwab's date-free
    ``periodType=day&period=1`` shape can silently return a different session, so it is
    never used. Extended hours are always off. No credential, token, header, account
    identifier, or URL is present in this mapping.
    """
    opened, closed = market_calendar.session_bounds_utc(session)
    return {
        "symbol": symbol.strip().upper(),
        "frequencyType": "minute",
        "frequency": minutes,
        "startDate": int(opened.timestamp() * 1000),
        "endDate": int(closed.timestamp() * 1000),
        "needExtendedHoursData": "false",
    }


def _bar_payload(bar: ReplayBar) -> dict[str, object]:
    return {
        "at": bar.interval_at.astimezone(UTC).isoformat(),
        "open": _decimal_text(bar.open),
        "high": _decimal_text(bar.high),
        "low": _decimal_text(bar.low),
        "close": _decimal_text(bar.close),
        "volume": bar.volume,
    }


def _raw_payload(candles: list[Candle]) -> list[dict[str, object]]:
    """Provider order and provider content, exactly as returned."""
    return [
        {
            "at": None if (found := _aware_utc(candle.date)) is None else found.isoformat(),
            "naive_at": None if _aware_utc(candle.date) else candle.date.isoformat(),
            "open": _decimal_text(candle.open),
            "high": _decimal_text(candle.high),
            "low": _decimal_text(candle.low),
            "close": _decimal_text(candle.close),
            "volume": candle.volume,
        }
        for candle in candles
    ]


def _normalized_digest(
    symbol: str,
    session: date,
    bars: tuple[ReplayBar, ...],
    *,
    minutes: int,
) -> str:
    """Order-independent by construction: ``bars`` is always chronological and unique."""
    return _digest(
        {
            "namespace": REPLAY_NAMESPACE,
            "exchange": market_calendar.EXCHANGE_MIC,
            "symbol": symbol,
            "session": session.isoformat(),
            "minutes": minutes,
            "bars": [_bar_payload(bar) for bar in bars],
        }
    )


def _replay_id(
    symbol: str,
    session: date,
    *,
    status: ReplaySessionStatus,
    normalized_bar_digest: str,
    minutes: int,
) -> str:
    identity = _digest(
        {
            "namespace": REPLAY_NAMESPACE,
            "exchange": market_calendar.EXCHANGE_MIC,
            "symbol": symbol,
            "session": session.isoformat(),
            "minutes": minutes,
            "status": status.value,
            "normalized_bar_digest": normalized_bar_digest,
        }
    )
    return f"{REPLAY_ID_PREFIX}:{identity}"


def _invalid_ohlc(bar: ReplayBar) -> bool:
    prices = (bar.open, bar.high, bar.low, bar.close)
    if any(value <= 0 for value in prices):
        return True
    return (
        bar.high < bar.low
        or bar.high < max(bar.open, bar.close)
        or bar.low > min(bar.open, bar.close)
    )


def _as_bar(interval_at: datetime, candle: Candle) -> ReplayBar | None:
    if candle.open is None or candle.high is None or candle.low is None:
        return None
    return ReplayBar(
        interval_at=interval_at,
        open=candle.open,
        high=candle.high,
        low=candle.low,
        close=candle.close,
        volume=candle.volume,
    )


def unavailable_session(
    replay_universe: ReplayUniverse,
    symbol: str,
    session: date,
    *,
    retrieved_at: datetime,
    error: str,
    source: str,
    minutes: int = INTERVAL_MINUTES,
) -> ReplaySessionEvidence:
    """Evidence for a retrieval that produced no payload, e.g. a provider failure.

    ``error`` must already be sanitized by the caller; it is stored verbatim.
    """
    retrieval = _aware_utc(retrieved_at)
    if retrieval is None:
        raise ValueError("retrieved_at must include a timezone")
    normalized = symbol.strip().upper()
    try:
        expected = expected_bar_count(session, minutes=minutes)
        params = request_params(normalized, session, minutes=minutes)
        issue = ReplayIssue.PROVIDER_ERROR
    except ValueError:
        expected = 0
        params = {"symbol": normalized, "frequency": minutes, "needExtendedHoursData": "false"}
        issue = ReplayIssue.CLOSED_SESSION
    normalized_bar_digest = _normalized_digest(normalized, session, (), minutes=minutes)
    return ReplaySessionEvidence(
        # The sanitized error participates in the identity so two different failures are
        # two different observations rather than one silently overwritten record.
        replay_id=_replay_id(
            normalized,
            session,
            status=ReplaySessionStatus.UNAVAILABLE,
            normalized_bar_digest=f"{normalized_bar_digest}#{error}",
            minutes=minutes,
        ),
        universe_id=replay_universe.universe_id,
        universe_symbols=replay_universe.symbols,
        symbol=normalized,
        session_date=session,
        source=source,
        retrieved_at=retrieval,
        request_params=params,
        raw_payload_digest=_digest({"namespace": REPLAY_NAMESPACE, "candles": []}),
        normalized_bar_digest=normalized_bar_digest,
        status=ReplaySessionStatus.UNAVAILABLE,
        expected_bar_count=expected,
        returned_bar_count=0,
        in_session_bar_count=0,
        unique_bar_count=0,
        issues=(issue,),
        error=error,
    )


def validate_replay_session(
    replay_universe: ReplayUniverse,
    symbol: str,
    session: date,
    candles: list[Candle],
    *,
    retrieved_at: datetime,
    source: str,
    minutes: int = INTERVAL_MINUTES,
) -> ReplaySessionEvidence:
    """Normalize and classify one retrieved session into deterministic evidence.

    Unlike the forward official validator this never refuses to produce a record: an
    incomplete session is still durable research evidence. It simply carries
    ``status=incomplete`` and the exact gaps, duplicates, and out-of-session bars that
    made it so.
    """
    retrieval = _aware_utc(retrieved_at)
    if retrieval is None:
        raise ValueError("retrieved_at must include a timezone")
    normalized = symbol.strip().upper()

    try:
        expected = market_calendar.session_interval_starts_utc(session, minutes=minutes)
    except ValueError:
        return unavailable_session(
            replay_universe,
            normalized,
            session,
            retrieved_at=retrieval,
            error="session is not an XNYS trading day",
            source=source,
            minutes=minutes,
        )

    expected_set = frozenset(expected)
    issues: list[ReplayIssue] = []

    by_interval: dict[datetime, list[ReplayBar]] = defaultdict(list)
    out_of_session: set[datetime] = set()
    partial_ohlc: set[datetime] = set()
    naive_count = 0
    seen_counts: Counter[datetime] = Counter()
    for candle in candles:
        moment = _aware_utc(candle.date)
        if moment is None:
            naive_count += 1
            continue
        if moment not in expected_set:
            out_of_session.add(moment)
            continue
        seen_counts[moment] += 1
        bar = _as_bar(moment, candle)
        if bar is None:
            # A partial OHLC cannot be normalized. The slot stays empty unless a
            # complete bar for the same interval also arrived.
            partial_ohlc.add(moment)
            continue
        by_interval[moment].append(bar)

    duplicates = tuple(sorted(moment for moment, count in seen_counts.items() if count > 1))
    conflicting = any(len(set(found)) > 1 for found in by_interval.values())

    bars = tuple(by_interval[moment][0] for moment in expected if by_interval.get(moment))
    present = {bar.interval_at for bar in bars}
    missing = tuple(moment for moment in expected if moment not in present)

    defective = {bar.interval_at for bar in bars if _invalid_ohlc(bar)}
    invalid_ohlc = tuple(sorted(defective | (partial_ohlc - present)))
    negative_volume = tuple(bar.interval_at for bar in bars if bar.volume < 0)

    if not candles:
        issues.append(ReplayIssue.EMPTY_PAYLOAD)
    if naive_count:
        issues.append(ReplayIssue.INVALID_TIMESTAMP)
    if expected[0] in missing:
        issues.append(ReplayIssue.MISSING_OPENING_BAR)
    if expected[-1] in missing:
        issues.append(ReplayIssue.MISSING_FINAL_BAR)
    if any(moment not in {expected[0], expected[-1]} for moment in missing):
        issues.append(ReplayIssue.INTERIOR_GAP)
    if duplicates:
        issues.append(ReplayIssue.DUPLICATE_TIMESTAMP)
    if conflicting:
        issues.append(ReplayIssue.CONFLICTING_DUPLICATE)
    if out_of_session:
        issues.append(ReplayIssue.OUT_OF_SESSION_BAR)
    if invalid_ohlc:
        issues.append(ReplayIssue.INVALID_OHLC)
    if negative_volume:
        issues.append(ReplayIssue.NEGATIVE_VOLUME)

    ordered_issues = tuple(dict.fromkeys(issues))
    if any(issue in _UNAVAILABLE_ISSUES for issue in ordered_issues):
        status = ReplaySessionStatus.UNAVAILABLE
    elif all(issue in _BENIGN_ISSUES for issue in ordered_issues):
        status = ReplaySessionStatus.COMPLETE
    else:
        status = ReplaySessionStatus.INCOMPLETE

    normalized_bar_digest = _normalized_digest(normalized, session, bars, minutes=minutes)
    raw_payload_digest = _digest(
        {
            "namespace": REPLAY_NAMESPACE,
            "symbol": normalized,
            "session": session.isoformat(),
            "candles": _raw_payload(candles),
        }
    )
    return ReplaySessionEvidence(
        replay_id=_replay_id(
            normalized,
            session,
            status=status,
            normalized_bar_digest=normalized_bar_digest,
            minutes=minutes,
        ),
        universe_id=replay_universe.universe_id,
        universe_symbols=replay_universe.symbols,
        symbol=normalized,
        session_date=session,
        source=source,
        retrieved_at=retrieval,
        request_params=request_params(normalized, session, minutes=minutes),
        raw_payload_digest=raw_payload_digest,
        normalized_bar_digest=normalized_bar_digest,
        status=status,
        expected_bar_count=len(expected),
        returned_bar_count=len(candles),
        in_session_bar_count=sum(seen_counts.values()),
        unique_bar_count=len(bars),
        missing_intervals=missing,
        duplicate_intervals=duplicates,
        out_of_session_intervals=tuple(sorted(out_of_session)),
        invalid_ohlc_intervals=invalid_ohlc,
        negative_volume_intervals=negative_volume,
        issues=ordered_issues,
        bars=bars,
    )


def evidence_payload(evidence: ReplaySessionEvidence) -> dict[str, object]:
    """Stable JSON primitives for reports and durable handoffs.

    Contains no credential, token, account identifier, URL, or raw provider response.
    """
    return {
        "schema": evidence.schema_name,
        "replay_id": evidence.replay_id,
        "universe_id": evidence.universe_id,
        "universe_symbols": list(evidence.universe_symbols),
        "symbol": evidence.symbol,
        "session": evidence.session_date.isoformat(),
        "provider": evidence.provider,
        "source": evidence.source,
        "retrieved_at": evidence.retrieved_at.isoformat(),
        "request_params": dict(evidence.request_params),
        "raw_payload_digest": evidence.raw_payload_digest,
        "normalized_bar_digest": evidence.normalized_bar_digest,
        "status": evidence.status.value,
        "expected_bar_count": evidence.expected_bar_count,
        "returned_bar_count": evidence.returned_bar_count,
        "in_session_bar_count": evidence.in_session_bar_count,
        "unique_bar_count": evidence.unique_bar_count,
        "missing_intervals": [item.isoformat() for item in evidence.missing_intervals],
        "duplicate_intervals": [item.isoformat() for item in evidence.duplicate_intervals],
        "out_of_session_intervals": [
            item.isoformat() for item in evidence.out_of_session_intervals
        ],
        "invalid_ohlc_intervals": [item.isoformat() for item in evidence.invalid_ohlc_intervals],
        "negative_volume_intervals": [
            item.isoformat() for item in evidence.negative_volume_intervals
        ],
        "issues": [issue.value for issue in evidence.issues],
        "error": evidence.error,
    }


__all__ = [
    "DEFAULT_SESSION_COUNT",
    "INTERVAL_MINUTES",
    "REPLAY_ID_PREFIX",
    "REPLAY_NAMESPACE",
    "RESEARCH_EVIDENCE_SCHEMA",
    "SCHWAB_PROVIDER",
    "ReplayBar",
    "ReplayIssue",
    "ReplaySessionEvidence",
    "ReplaySessionStatus",
    "ReplayUniverse",
    "completed_sessions",
    "evidence_payload",
    "expected_bar_count",
    "request_params",
    "unavailable_session",
    "universe",
    "validate_replay_session",
]
