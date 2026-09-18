"""Fail-closed validation and deterministic aggregation of Schwab five-minute bars.

This module is pure: it does not fetch, persist, sleep, or mutate cohort/paper state.
The official provider seam fetches daily history first and calls this validator only
when the exact target session is absent.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise

from pydantic import BaseModel, ConfigDict

from schwab_trader import market_calendar, market_data
from schwab_trader.market_data import Candle

INTERVAL_MINUTES = 5
INTRADAY_SOURCE = market_data.SCHWAB_REGULAR_SESSION_SOURCE
DERIVED_DAILY_SOURCE = "schwab-intraday-derived-daily"
PAYLOAD_SCHEMA = "schwab-intraday-daily-evidence/1"


class CoverageReason(StrEnum):
    """Stable, sanitized reasons why aggregation is unsafe."""

    CLOSED_SESSION = "closed_session"
    RETRIEVED_BEFORE_CLOSE = "retrieved_before_close"
    MISSING_INTERVALS = "missing_intervals"
    DUPLICATE_INTERVALS = "duplicate_intervals"
    OUT_OF_ORDER = "out_of_order"
    UNEXPECTED_INTERVALS = "unexpected_intervals"
    INVALID_OHLC = "invalid_ohlc"
    NEGATIVE_VOLUME = "negative_volume"
    MISSING_OPEN = "missing_open"
    MISSING_CLOSE = "missing_close"


class DerivedDailyEvidence(BaseModel):
    """Complete reproducible evidence for one derived session candle."""

    model_config = ConfigDict(frozen=True)

    schema_name: str = PAYLOAD_SCHEMA
    dataset_id: str
    constituent_digest: str
    symbol: str
    session_date: date
    retrieved_at: datetime
    source: str = DERIVED_DAILY_SOURCE
    expected_interval_count: int
    observed_interval_count: int
    first_interval_at: datetime
    final_interval_at: datetime
    candle: Candle
    constituents: tuple[Candle, ...]


class SessionBarDiagnostic(BaseModel):
    """A stable secret-free result for diagnostics, monitoring, and preflight."""

    model_config = ConfigDict(frozen=True)

    schema_name: str = PAYLOAD_SCHEMA
    symbol: str
    session_date: date
    retrieved_at: datetime
    source: str = INTRADAY_SOURCE
    expected_interval_count: int
    observed_interval_count: int
    unique_interval_count: int
    first_interval_at: datetime | None = None
    final_interval_at: datetime | None = None
    first_returned_interval_at: datetime | None = None
    final_returned_interval_at: datetime | None = None
    missing_intervals: tuple[datetime, ...] = ()
    duplicate_intervals: tuple[datetime, ...] = ()
    unexpected_intervals: tuple[datetime, ...] = ()
    invalid_ohlc_intervals: tuple[datetime, ...] = ()
    negative_volume_intervals: tuple[datetime, ...] = ()
    out_of_order: bool = False
    aggregation_safe: bool = False
    reasons: tuple[CoverageReason, ...] = ()
    evidence: DerivedDailyEvidence | None = None


def _utc(value: datetime) -> datetime | None:
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


def _decimal(value: Decimal) -> str:
    normalized = value.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


def _constituent_payload(candles: tuple[Candle, ...]) -> list[dict[str, object]]:
    return [
        {
            "at": candle.date.astimezone(UTC).isoformat(),
            "open": _decimal(candle.open) if candle.open is not None else None,
            "high": _decimal(candle.high) if candle.high is not None else None,
            "low": _decimal(candle.low) if candle.low is not None else None,
            "close": _decimal(candle.close),
            "volume": candle.volume,
        }
        for candle in candles
    ]


def _digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _invalid_ohlc(candle: Candle) -> bool:
    if candle.open is None or candle.high is None or candle.low is None:
        return True
    prices = (candle.open, candle.high, candle.low, candle.close)
    if any(value <= 0 for value in prices):
        return True
    return (
        candle.high < candle.low
        or candle.high < max(candle.open, candle.close)
        or candle.low > min(candle.open, candle.close)
    )


def validate_regular_session(
    symbol: str,
    session: date,
    candles: list[Candle],
    *,
    retrieved_at: datetime,
) -> SessionBarDiagnostic:
    """Validate exact interval evidence and aggregate only when every check passes."""
    normalized = symbol.strip().upper()
    retrieval = _utc(retrieved_at)
    if retrieval is None:
        raise ValueError("retrieved_at must include a timezone")

    try:
        expected = market_calendar.session_interval_starts_utc(
            session,
            minutes=INTERVAL_MINUTES,
        )
        _, session_close = market_calendar.session_bounds_utc(session)
    except ValueError:
        return SessionBarDiagnostic(
            symbol=normalized,
            session_date=session,
            retrieved_at=retrieval,
            expected_interval_count=0,
            observed_interval_count=0,
            unique_interval_count=0,
            reasons=(CoverageReason.CLOSED_SESSION,),
        )

    returned_times = tuple(found for candle in candles if (found := _utc(candle.date)) is not None)
    expected_set = frozenset(expected)
    target_entries = tuple(
        (found, candle)
        for candle in candles
        if (found := _utc(candle.date)) is not None and found in expected_set
    )
    target_times = tuple(found for found, _ in target_entries)
    counts = Counter(target_times)
    duplicates = tuple(sorted(found for found, count in counts.items() if count > 1))
    unique_target = frozenset(target_times)
    missing = tuple(found for found in expected if found not in unique_target)
    unexpected = tuple(sorted({found for found in returned_times if found not in expected_set}))
    out_of_order = any(right < left for left, right in pairwise(returned_times))
    invalid_ohlc = tuple(found for found, candle in target_entries if _invalid_ohlc(candle))
    negative_volume = tuple(found for found, candle in target_entries if candle.volume < 0)

    reasons: list[CoverageReason] = []
    if retrieval <= session_close:
        reasons.append(CoverageReason.RETRIEVED_BEFORE_CLOSE)
    if missing:
        reasons.append(CoverageReason.MISSING_INTERVALS)
    if duplicates:
        reasons.append(CoverageReason.DUPLICATE_INTERVALS)
    if out_of_order:
        reasons.append(CoverageReason.OUT_OF_ORDER)
    if unexpected or len(returned_times) != len(candles):
        reasons.append(CoverageReason.UNEXPECTED_INTERVALS)
    if invalid_ohlc:
        reasons.append(CoverageReason.INVALID_OHLC)
    if negative_volume:
        reasons.append(CoverageReason.NEGATIVE_VOLUME)

    by_time = {interval: candle for interval, candle in target_entries}
    first = by_time.get(expected[0])
    final = by_time.get(expected[-1])
    if first is None or first.open is None:
        reasons.append(CoverageReason.MISSING_OPEN)
    if final is None:
        reasons.append(CoverageReason.MISSING_CLOSE)

    ordered_reasons = tuple(dict.fromkeys(reasons))
    base = {
        "symbol": normalized,
        "session_date": session,
        "retrieved_at": retrieval,
        "expected_interval_count": len(expected),
        "observed_interval_count": len(target_entries),
        "unique_interval_count": len(unique_target),
        "first_interval_at": min(target_times) if target_times else None,
        "final_interval_at": max(target_times) if target_times else None,
        "first_returned_interval_at": min(returned_times) if returned_times else None,
        "final_returned_interval_at": max(returned_times) if returned_times else None,
        "missing_intervals": missing,
        "duplicate_intervals": duplicates,
        "unexpected_intervals": unexpected,
        "invalid_ohlc_intervals": invalid_ohlc,
        "negative_volume_intervals": negative_volume,
        "out_of_order": out_of_order,
        "reasons": ordered_reasons,
    }
    if ordered_reasons:
        return SessionBarDiagnostic.model_validate(base)

    constituents = tuple(by_time[interval] for interval in expected)
    assert first is not None and first.open is not None
    assert final is not None
    highs = tuple(candle.high for candle in constituents if candle.high is not None)
    lows = tuple(candle.low for candle in constituents if candle.low is not None)
    assert len(highs) == len(lows) == len(constituents)

    aggregate = Candle(
        symbol=normalized,
        date=session_close,
        open=first.open,
        high=max(highs),
        low=min(lows),
        close=final.close,
        volume=sum(candle.volume for candle in constituents),
        source=DERIVED_DAILY_SOURCE,
    )
    canonical_constituents = _constituent_payload(constituents)
    constituent_digest = _digest(canonical_constituents)
    identity = _digest(
        {
            "source": DERIVED_DAILY_SOURCE,
            "exchange": market_calendar.EXCHANGE_MIC,
            "symbol": normalized,
            "session": session.isoformat(),
            "minutes": INTERVAL_MINUTES,
            "constituent_digest": constituent_digest,
            "aggregate": {
                "open": _decimal(first.open),
                "high": _decimal(max(highs)),
                "low": _decimal(min(lows)),
                "close": _decimal(aggregate.close),
                "volume": aggregate.volume,
            },
        }
    )
    evidence = DerivedDailyEvidence(
        dataset_id=f"{DERIVED_DAILY_SOURCE}:{identity}",
        constituent_digest=constituent_digest,
        symbol=normalized,
        session_date=session,
        retrieved_at=retrieval,
        expected_interval_count=len(expected),
        observed_interval_count=len(target_entries),
        first_interval_at=expected[0],
        final_interval_at=expected[-1],
        candle=aggregate,
        constituents=constituents,
    )
    return SessionBarDiagnostic.model_validate(
        {
            **base,
            "aggregation_safe": True,
            "evidence": evidence,
        }
    )


def diagnostic_payload(result: SessionBarDiagnostic) -> dict[str, object]:
    """Stable JSON primitives only; no request, credential, or connection fields."""
    evidence = result.evidence
    derived = None
    if evidence is not None:
        candle = evidence.candle
        derived = {
            "open": str(candle.open),
            "high": str(candle.high),
            "low": str(candle.low),
            "close": str(candle.close),
            "volume": candle.volume,
            "source": candle.source,
            "dataset_id": evidence.dataset_id,
            "constituent_digest": evidence.constituent_digest,
        }
    return {
        "schema": result.schema_name,
        "symbol": result.symbol,
        "session": result.session_date.isoformat(),
        "retrieved_at": result.retrieved_at.isoformat(),
        "source": result.source,
        "expected_interval_count": result.expected_interval_count,
        "observed_interval_count": result.observed_interval_count,
        "unique_interval_count": result.unique_interval_count,
        "first_interval_at": (
            None if result.first_interval_at is None else result.first_interval_at.isoformat()
        ),
        "final_interval_at": (
            None if result.final_interval_at is None else result.final_interval_at.isoformat()
        ),
        "first_returned_interval_at": (
            None
            if result.first_returned_interval_at is None
            else result.first_returned_interval_at.isoformat()
        ),
        "final_returned_interval_at": (
            None
            if result.final_returned_interval_at is None
            else result.final_returned_interval_at.isoformat()
        ),
        "missing_intervals": [item.isoformat() for item in result.missing_intervals],
        "duplicate_intervals": [item.isoformat() for item in result.duplicate_intervals],
        "unexpected_intervals": [item.isoformat() for item in result.unexpected_intervals],
        "invalid_ohlc_intervals": [item.isoformat() for item in result.invalid_ohlc_intervals],
        "negative_volume_intervals": [
            item.isoformat() for item in result.negative_volume_intervals
        ],
        "out_of_order": result.out_of_order,
        "aggregation_safe": result.aggregation_safe,
        "reasons": [reason.value for reason in result.reasons],
        "derived": derived,
    }


__all__ = [
    "DERIVED_DAILY_SOURCE",
    "INTERVAL_MINUTES",
    "INTRADAY_SOURCE",
    "PAYLOAD_SCHEMA",
    "CoverageReason",
    "DerivedDailyEvidence",
    "SessionBarDiagnostic",
    "diagnostic_payload",
    "validate_regular_session",
]
