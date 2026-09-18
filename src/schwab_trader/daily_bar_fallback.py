"""Daily-first Schwab history resolution for official paper-cohort snapshots."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from schwab_trader import client as api
from schwab_trader import market_bar_evidence, market_data
from schwab_trader.market_bar_evidence import (
    DerivedDailyEvidence,
    SessionBarDiagnostic,
)
from schwab_trader.market_data import Candle
from schwab_trader.storage.contracts import MarketDataEvidenceRepository


class DailyHistoryCache(Protocol):
    """The settled-evidence subset of :class:`history_cache.HistoryCache`."""

    def get(
        self,
        client: api.SchwabClient,
        symbol: str,
        *,
        days: int = 180,
        settled_through: date | None = None,
        now: datetime | None = None,
    ) -> list[Candle]: ...


class DailyBarState(StrEnum):
    """How the exact target session was resolved."""

    OFFICIAL = "official"
    DERIVED = "derived"
    INCOMPLETE = "incomplete"


class DailyBarResolution(BaseModel):
    """Daily history plus explicit target-session provenance and coverage."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    session_date: date
    state: DailyBarState
    retrieved_at: datetime
    source: str
    history: tuple[Candle, ...]
    latest_official_session: date | None = None
    target_candle: Candle | None = None
    diagnostic: SessionBarDiagnostic | None = None
    evidence: DerivedDailyEvidence | None = None

    @property
    def ready(self) -> bool:
        return self.state in {DailyBarState.OFFICIAL, DailyBarState.DERIVED}

    @property
    def dataset_id(self) -> str | None:
        return None if self.evidence is None else self.evidence.dataset_id


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def operator_payload(resolution: DailyBarResolution) -> dict[str, object]:
    """Stable sanitized coverage facts for durable retry status and dashboards."""
    diagnostic = resolution.diagnostic
    latest = resolution.latest_official_session
    return {
        "symbol": resolution.symbol,
        "target_session": resolution.session_date.isoformat(),
        "state": resolution.state.value,
        "latest_official_session": None if latest is None else latest.isoformat(),
        "evidence_source": resolution.source,
        "expected_interval_count": None
        if diagnostic is None
        else diagnostic.expected_interval_count,
        "observed_interval_count": None
        if diagnostic is None
        else diagnostic.observed_interval_count,
        "first_interval_at": _iso(None if diagnostic is None else diagnostic.first_interval_at),
        "final_interval_at": _iso(None if diagnostic is None else diagnostic.final_interval_at),
        "aggregation_safe": None if diagnostic is None else diagnostic.aggregation_safe,
        "dataset_id": resolution.dataset_id,
        "error": None,
    }


def resolve_daily_history(
    client: api.SchwabClient,
    cache: DailyHistoryCache,
    evidence_store: MarketDataEvidenceRepository,
    symbol: str,
    session: date,
    *,
    days: int = 300,
    clock: Callable[[], datetime] = _utc_now,
) -> DailyBarResolution:
    """Resolve the target session daily-first and persist only complete fallback data.

    Provider/storage exceptions intentionally propagate. The cohort snapshot layer
    classifies those as provider/snapshot errors, distinct from an enabled provider
    returning incomplete interval coverage.
    """
    normalized = symbol.strip().upper()
    requested_at = clock()
    daily = cache.get(
        client,
        normalized,
        days=days,
        settled_through=session,
        now=requested_at,
    )
    retrieved_daily_at = clock()
    eligible = tuple(candle for candle in daily if candle.date.date() <= session)
    latest_official = max(
        (candle.date.date() for candle in eligible),
        default=None,
    )
    target = tuple(candle for candle in eligible if candle.date.date() == session)
    if len(target) > 1:
        raise ValueError(
            f"Schwab daily history returned duplicate candles for {normalized} {session}"
        )
    if target:
        return DailyBarResolution(
            symbol=normalized,
            session_date=session,
            state=DailyBarState.OFFICIAL,
            retrieved_at=retrieved_daily_at,
            source=market_data.SCHWAB_DAILY_HISTORY_SOURCE,
            history=eligible,
            latest_official_session=session,
            target_candle=target[0],
        )

    intraday = market_data.get_regular_session_history(client, normalized, session)
    retrieved_intraday_at = clock()
    diagnostic = market_bar_evidence.validate_regular_session(
        normalized,
        session,
        intraday,
        retrieved_at=retrieved_intraday_at,
    )
    if not diagnostic.aggregation_safe or diagnostic.evidence is None:
        return DailyBarResolution(
            symbol=normalized,
            session_date=session,
            state=DailyBarState.INCOMPLETE,
            retrieved_at=retrieved_intraday_at,
            source=market_data.SCHWAB_REGULAR_SESSION_SOURCE,
            history=eligible,
            latest_official_session=latest_official,
            diagnostic=diagnostic,
        )

    persisted = evidence_store.save(diagnostic.evidence)
    augmented = (*eligible, persisted.candle)
    return DailyBarResolution(
        symbol=normalized,
        session_date=session,
        state=DailyBarState.DERIVED,
        retrieved_at=persisted.retrieved_at,
        source=persisted.source,
        history=augmented,
        latest_official_session=latest_official,
        target_candle=persisted.candle,
        diagnostic=diagnostic.model_copy(update={"evidence": persisted}),
        evidence=persisted,
    )


__all__ = [
    "DailyBarResolution",
    "DailyBarState",
    "DailyHistoryCache",
    "operator_payload",
    "resolve_daily_history",
]
