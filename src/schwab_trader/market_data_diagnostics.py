"""Read-only Schwab bar diagnostics, bounded monitoring, and reconciliation."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from schwab_trader import client as api
from schwab_trader import market_bar_evidence, market_calendar, market_data
from schwab_trader.market_bar_evidence import DerivedDailyEvidence
from schwab_trader.market_data import Candle
from schwab_trader.storage.contracts import MarketDataEvidenceRepository

DIAGNOSTIC_SCHEMA = "schwab-bar-diagnostic/1"
MONITOR_SCHEMA = "schwab-bar-monitor/1"
RECONCILIATION_SCHEMA = "schwab-derived-daily-reconciliation/1"
MIN_POLL_SECONDS = 30.0
MAX_MONITOR_DURATION = timedelta(hours=24)
DEFAULT_PRICE_TOLERANCE = Decimal("0.01")
DEFAULT_VOLUME_TOLERANCE = 0


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(UTC)


def _date_iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def normalize_symbols(symbols: Sequence[str]) -> tuple[str, ...]:
    """Normalize, deduplicate, and reject an empty diagnostic symbol list."""
    normalized = tuple(
        dict.fromkeys(symbol.strip().upper() for symbol in symbols if symbol.strip())
    )
    if not normalized:
        raise ValueError("at least one symbol is required")
    return normalized


def _candle_payload(candle: Candle) -> dict[str, object]:
    return {
        "open": None if candle.open is None else str(candle.open),
        "high": None if candle.high is None else str(candle.high),
        "low": None if candle.low is None else str(candle.low),
        "close": str(candle.close),
        "volume": candle.volume,
        "source": candle.source,
    }


def _diagnostic_base(
    symbol: str,
    session: date,
    *,
    retrieved_at: datetime,
    latest_official_session: date | None,
) -> dict[str, object]:
    return {
        "schema": DIAGNOSTIC_SCHEMA,
        "symbol": symbol,
        "session": session.isoformat(),
        "state": "incomplete",
        "latest_official_session": (
            None if latest_official_session is None else latest_official_session.isoformat()
        ),
        "official_daily_available": False,
        "intraday_requested": False,
        "first_interval_at": None,
        "final_interval_at": None,
        "first_returned_interval_at": None,
        "final_returned_interval_at": None,
        "expected_interval_count": None,
        "observed_interval_count": None,
        "unique_interval_count": None,
        "missing_intervals": [],
        "duplicate_intervals": [],
        "unexpected_intervals": [],
        "invalid_ohlc_intervals": [],
        "negative_volume_intervals": [],
        "out_of_order": False,
        "aggregation_safe": None,
        "derived": None,
        "official": None,
        "evidence_source": None,
        "retrieved_at": _utc(retrieved_at).isoformat(),
        "reasons": [],
        "error": None,
    }


def diagnose_symbol(
    client: api.SchwabClient,
    symbol: str,
    session: date,
    *,
    clock: Callable[[], datetime] = _utc_now,
) -> dict[str, object]:
    """Inspect daily-first readiness for one symbol without writing any state."""
    normalized = normalize_symbols((symbol,))[0]
    try:
        daily = market_data.get_price_history(client, normalized, days=300)
    except Exception as exc:
        payload = _diagnostic_base(
            normalized,
            session,
            retrieved_at=clock(),
            latest_official_session=None,
        )
        payload.update(
            {
                "state": "provider_error",
                "evidence_source": market_data.SCHWAB_DAILY_HISTORY_SOURCE,
                "error": {"phase": "daily", "kind": type(exc).__name__},
            }
        )
        return payload

    retrieved_daily_at = clock()
    eligible = tuple(candle for candle in daily if candle.date.date() <= session)
    latest = max((candle.date.date() for candle in eligible), default=None)
    target = tuple(candle for candle in eligible if candle.date.date() == session)
    payload = _diagnostic_base(
        normalized,
        session,
        retrieved_at=retrieved_daily_at,
        latest_official_session=latest,
    )
    if len(target) > 1:
        payload.update(
            {
                "state": "ambiguous_official_daily",
                "official_daily_available": True,
                "evidence_source": market_data.SCHWAB_DAILY_HISTORY_SOURCE,
                "error": {"phase": "daily", "kind": "duplicate_target_session"},
            }
        )
        return payload
    if target:
        payload.update(
            {
                "state": "official_daily",
                "official_daily_available": True,
                "aggregation_safe": None,
                "evidence_source": market_data.SCHWAB_DAILY_HISTORY_SOURCE,
                "official": _candle_payload(target[0]),
            }
        )
        return payload

    try:
        intraday = market_data.get_regular_session_history(client, normalized, session)
    except Exception as exc:
        payload.update(
            {
                "state": "provider_error",
                "intraday_requested": True,
                "evidence_source": market_data.SCHWAB_REGULAR_SESSION_SOURCE,
                "retrieved_at": _utc(clock()).isoformat(),
                "error": {"phase": "intraday", "kind": type(exc).__name__},
            }
        )
        return payload

    diagnostic = market_bar_evidence.validate_regular_session(
        normalized,
        session,
        intraday,
        retrieved_at=clock(),
    )
    interval_payload = market_bar_evidence.diagnostic_payload(diagnostic)
    for key in (
        "first_interval_at",
        "final_interval_at",
        "first_returned_interval_at",
        "final_returned_interval_at",
        "expected_interval_count",
        "observed_interval_count",
        "unique_interval_count",
        "missing_intervals",
        "duplicate_intervals",
        "unexpected_intervals",
        "invalid_ohlc_intervals",
        "negative_volume_intervals",
        "out_of_order",
        "aggregation_safe",
        "derived",
        "reasons",
        "retrieved_at",
    ):
        payload[key] = interval_payload[key]
    payload.update(
        {
            "state": (
                "intraday_derived_safe" if diagnostic.aggregation_safe else "intraday_incomplete"
            ),
            "intraday_requested": True,
            "evidence_source": diagnostic.source,
        }
    )
    return payload


def diagnose_symbols(
    client: api.SchwabClient,
    symbols: Sequence[str],
    session: date,
    *,
    clock: Callable[[], datetime] = _utc_now,
) -> dict[str, object]:
    """Return the stable multi-symbol diagnostic contract."""
    normalized = normalize_symbols(symbols)
    results = [diagnose_symbol(client, symbol, session, clock=clock) for symbol in normalized]
    return {
        "schema": DIAGNOSTIC_SCHEMA,
        "session": session.isoformat(),
        "all_ready": all(
            item["state"] in {"official_daily", "intraday_derived_safe"} for item in results
        ),
        "symbols": results,
    }


def monitor_session_bars(
    client: api.SchwabClient,
    symbols: Sequence[str],
    session: date,
    *,
    poll_seconds: float,
    stop_at: datetime,
    clock: Callable[[], datetime] = _utc_now,
    sleeper: Callable[[float], None] = time.sleep,
) -> Iterator[dict[str, object]]:
    """Poll until both bar milestones are observed or a hard deadline is reached.

    "First seen" is the timestamp of this process's first successful poll, not a
    provider publication timestamp. The loop is additionally capped at 24 hours and
    uses at least a 30-second interval. Each endpoint stops being queried for a symbol
    once its milestone has been observed.
    """
    normalized = normalize_symbols(symbols)
    if poll_seconds < MIN_POLL_SECONDS:
        raise ValueError(f"poll_seconds must be at least {MIN_POLL_SECONDS:g}")
    deadline = _utc(stop_at)
    started = _utc(clock())
    duration = deadline - started
    if duration <= timedelta(0):
        raise ValueError("stop_at must be in the future")
    if duration > MAX_MONITOR_DURATION:
        raise ValueError("monitoring duration must not exceed 24 hours")

    expected = market_calendar.session_interval_starts_utc(session)
    expected_set = frozenset(expected)
    final_expected = expected[-1]
    final_seen: dict[str, datetime] = {}
    daily_seen: dict[str, datetime] = {}
    latest_daily: dict[str, date | None] = dict.fromkeys(normalized)
    observed_count: dict[str, int] = dict.fromkeys(normalized, 0)
    unique_count: dict[str, int] = dict.fromkeys(normalized, 0)
    max_polls = math.ceil(duration.total_seconds() / poll_seconds) + 1

    for _ in range(max_polls):
        polled_at = _utc(clock())
        rows: list[dict[str, object]] = []
        for symbol in normalized:
            errors: dict[str, str] = {}
            if symbol not in daily_seen:
                try:
                    daily = market_data.get_price_history(client, symbol, days=300)
                    sessions = tuple(candle.date.date() for candle in daily)
                    latest_daily[symbol] = max(sessions, default=None)
                    if session in sessions:
                        daily_seen[symbol] = polled_at
                except Exception as exc:
                    errors["daily"] = type(exc).__name__

            if symbol not in final_seen:
                try:
                    intraday = market_data.get_regular_session_history(
                        client,
                        symbol,
                        session,
                    )
                    intervals = tuple(
                        candle.date.astimezone(UTC)
                        for candle in intraday
                        if candle.date.tzinfo is not None
                        and candle.date.utcoffset() is not None
                        and candle.date.astimezone(UTC) in expected_set
                    )
                    observed_count[symbol] = len(intervals)
                    unique_count[symbol] = len(set(intervals))
                    if final_expected in intervals:
                        final_seen[symbol] = polled_at
                except Exception as exc:
                    errors["intraday"] = type(exc).__name__

            rows.append(
                {
                    "symbol": symbol,
                    "latest_official_session": _date_iso(latest_daily[symbol]),
                    "official_daily_available": symbol in daily_seen,
                    "official_daily_first_seen_at": (
                        None if symbol not in daily_seen else daily_seen[symbol].isoformat()
                    ),
                    "final_interval_expected_at": final_expected.isoformat(),
                    "final_interval_available": symbol in final_seen,
                    "final_interval_first_seen_at": (
                        None if symbol not in final_seen else final_seen[symbol].isoformat()
                    ),
                    "expected_interval_count": len(expected),
                    "observed_interval_count": observed_count[symbol],
                    "unique_interval_count": unique_count[symbol],
                    "errors": errors,
                }
            )

        complete = all(symbol in daily_seen and symbol in final_seen for symbol in normalized)
        yield {
            "schema": MONITOR_SCHEMA,
            "session": session.isoformat(),
            "polled_at": polled_at.isoformat(),
            "stop_at": deadline.isoformat(),
            "complete": complete,
            "symbols": rows,
        }
        if complete or polled_at >= deadline:
            break
        remaining = (deadline - _utc(clock())).total_seconds()
        if remaining <= 0:
            break
        sleeper(min(poll_seconds, remaining))


def _difference(official: Decimal, derived: Decimal) -> str:
    return str(official - derived)


def reconcile_derived_daily(
    client: api.SchwabClient,
    evidence_store: MarketDataEvidenceRepository,
    dataset_id: str,
    *,
    price_tolerance: Decimal = DEFAULT_PRICE_TOLERANCE,
    volume_tolerance: int = DEFAULT_VOLUME_TOLERANCE,
    clock: Callable[[], datetime] = _utc_now,
) -> dict[str, object]:
    """Compare persisted evidence with later official daily history; never rewrite it."""
    if price_tolerance < 0:
        raise ValueError("price_tolerance must be nonnegative")
    if volume_tolerance < 0:
        raise ValueError("volume_tolerance must be nonnegative")
    evidence = evidence_store.get(dataset_id)
    if evidence is None:
        raise KeyError("derived evidence dataset was not found")

    try:
        daily = market_data.get_price_history(client, evidence.symbol, days=300)
    except Exception as exc:
        return _reconciliation_payload(
            evidence,
            compared_at=clock(),
            price_tolerance=price_tolerance,
            volume_tolerance=volume_tolerance,
            status="provider_error",
            error={"phase": "daily", "kind": type(exc).__name__},
        )

    target = tuple(candle for candle in daily if candle.date.date() == evidence.session_date)
    if not target:
        return _reconciliation_payload(
            evidence,
            compared_at=clock(),
            price_tolerance=price_tolerance,
            volume_tolerance=volume_tolerance,
            status="awaiting_official_daily",
        )
    if len(target) > 1:
        return _reconciliation_payload(
            evidence,
            compared_at=clock(),
            price_tolerance=price_tolerance,
            volume_tolerance=volume_tolerance,
            status="ambiguous_official_daily",
            error={"phase": "daily", "kind": "duplicate_target_session"},
        )

    official = target[0]
    derived = evidence.candle
    price_pairs = {
        "open": (official.open, derived.open),
        "high": (official.high, derived.high),
        "low": (official.low, derived.low),
        "close": (official.close, derived.close),
    }
    differences: dict[str, object] = {}
    material: list[str] = []
    for field, (official_value, derived_value) in price_pairs.items():
        if official_value is None or derived_value is None:
            differences[field] = {
                "difference": None,
                "within_tolerance": False,
            }
            material.append(field)
            continue
        within = abs(official_value - derived_value) <= price_tolerance
        differences[field] = {
            "difference": _difference(official_value, derived_value),
            "within_tolerance": within,
        }
        if not within:
            material.append(field)

    volume_difference = official.volume - derived.volume
    volume_within = abs(volume_difference) <= volume_tolerance
    differences["volume"] = {
        "difference": volume_difference,
        "within_tolerance": volume_within,
    }
    if not volume_within:
        material.append("volume")

    return _reconciliation_payload(
        evidence,
        compared_at=clock(),
        price_tolerance=price_tolerance,
        volume_tolerance=volume_tolerance,
        status="compared",
        official=official,
        differences=differences,
        material_fields=material,
    )


def _reconciliation_payload(
    evidence: DerivedDailyEvidence,
    *,
    compared_at: datetime,
    price_tolerance: Decimal,
    volume_tolerance: int,
    status: str,
    official: Candle | None = None,
    differences: dict[str, object] | None = None,
    material_fields: Sequence[str] = (),
    error: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "schema": RECONCILIATION_SCHEMA,
        "status": status,
        "dataset_id": evidence.dataset_id,
        "symbol": evidence.symbol,
        "session": evidence.session_date.isoformat(),
        "evidence_source": evidence.source,
        "evidence_retrieved_at": evidence.retrieved_at.isoformat(),
        "constituent_digest": evidence.constituent_digest,
        "compared_at": _utc(compared_at).isoformat(),
        "tolerances": {
            "price_absolute": str(price_tolerance),
            "volume_absolute": volume_tolerance,
        },
        "derived": _candle_payload(evidence.candle),
        "official": None if official is None else _candle_payload(official),
        "differences": differences,
        "material_fields": list(material_fields),
        "material_discrepancy": bool(material_fields),
        "error": error,
    }


__all__ = [
    "DEFAULT_PRICE_TOLERANCE",
    "DEFAULT_VOLUME_TOLERANCE",
    "DIAGNOSTIC_SCHEMA",
    "MAX_MONITOR_DURATION",
    "MIN_POLL_SECONDS",
    "MONITOR_SCHEMA",
    "RECONCILIATION_SCHEMA",
    "diagnose_symbol",
    "diagnose_symbols",
    "monitor_session_bars",
    "normalize_symbols",
    "reconcile_derived_daily",
]
