"""Acquisition orchestration for the historical-replay research workflow.

Two operations, deliberately separated:

* :func:`preflight` performs **no** I/O of any kind. It reports exactly which sessions
  would be requested, with which parameters, and how many bars the exchange calendar
  says each should contain. This is the default posture for any operator command.
* :func:`acquire` performs the download through an injected fetcher and persists
  research evidence. It is only reached when an operator explicitly asks for it.

The fetcher is injected so the whole workflow is exercised offline with fixture
candles. :func:`schwab_session_fetcher` builds the real one from an existing
:class:`~schwab_trader.client.SchwabClient`, whose sliding-window ``RateLimiter``
already bounds request volume — this module adds no second limiter and no retry of its
own, so provider pressure stays governed by the one configured limit.

Nothing here creates a cohort observation, a paper fill, an order, a position, a
valuation, or a cash movement, and nothing here can satisfy forward cohort readiness.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from schwab_trader import client as api
from schwab_trader import historical_replay, market_calendar, market_data
from schwab_trader.historical_replay import (
    ReplaySessionEvidence,
    ReplaySessionStatus,
    ReplayUniverse,
)
from schwab_trader.market_data import Candle
from schwab_trader.storage.historical_replay import (
    ReplayIngestOutcome,
    ReplayIngestResult,
    SqlAlchemyHistoricalReplayStore,
)


class ReplayBarFetcher(Protocol):
    """Returns the provider's regular-session five-minute candles, order preserved."""

    def __call__(self, symbol: str, session: date) -> list[Candle]: ...


class PlannedRequest(BaseModel):
    """One request the downloader would issue, with its calendar expectations."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    session_date: date
    expected_bar_count: int
    early_close: bool
    request_params: dict[str, str | int | bool]


class ReplayPreflight(BaseModel):
    """The complete plan, computed with zero network, storage, or credential access."""

    model_config = ConfigDict(frozen=True)

    universe_id: str
    universe_symbols: tuple[str, ...]
    sessions: tuple[date, ...]
    requests: tuple[PlannedRequest, ...]
    total_expected_bars: int


class SessionOutcome(BaseModel):
    """What happened for one (symbol, session) during an acquisition run."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    session_date: date
    status: ReplaySessionStatus
    ingest_outcome: ReplayIngestOutcome
    revision: int
    replay_id: str
    previous_replay_id: str | None = None
    expected_bar_count: int
    unique_bar_count: int
    issues: tuple[str, ...] = ()
    error: str | None = None


class ReplayAcquisitionReport(BaseModel):
    """Clear per-bucket reporting over an acquisition run."""

    model_config = ConfigDict(frozen=True)

    universe_id: str
    universe_symbols: tuple[str, ...]
    sessions: tuple[date, ...]
    started_at: datetime
    finished_at: datetime
    outcomes: tuple[SessionOutcome, ...]

    @property
    def complete(self) -> tuple[SessionOutcome, ...]:
        return tuple(
            item for item in self.outcomes if item.status is ReplaySessionStatus.COMPLETE
        )

    @property
    def incomplete(self) -> tuple[SessionOutcome, ...]:
        return tuple(
            item for item in self.outcomes if item.status is ReplaySessionStatus.INCOMPLETE
        )

    @property
    def unavailable(self) -> tuple[SessionOutcome, ...]:
        return tuple(
            item for item in self.outcomes if item.status is ReplaySessionStatus.UNAVAILABLE
        )

    @property
    def corrected(self) -> tuple[SessionOutcome, ...]:
        return tuple(
            item
            for item in self.outcomes
            if item.ingest_outcome is ReplayIngestOutcome.CORRECTED
        )

    @property
    def unchanged(self) -> tuple[SessionOutcome, ...]:
        return tuple(
            item
            for item in self.outcomes
            if item.ingest_outcome is ReplayIngestOutcome.UNCHANGED
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def schwab_session_fetcher(client: api.SchwabClient) -> ReplayBarFetcher:
    """The real fetcher.

    It reuses :func:`schwab_trader.market_data.get_regular_session_history`, which sends
    explicit UTC ``startDate``/``endDate`` session bounds with
    ``needExtendedHoursData=false`` and never Schwab's date-free
    ``periodType=day&period=1`` shape. The client's limiter governs request rate.
    """

    def fetch(symbol: str, session: date) -> list[Candle]:
        return market_data.get_regular_session_history(client, symbol, session)

    return fetch


def preflight(
    replay_universe: ReplayUniverse,
    sessions: tuple[date, ...],
) -> ReplayPreflight:
    """Describe every request the downloader would make. Performs no I/O."""
    requests = tuple(
        PlannedRequest(
            symbol=symbol,
            session_date=session,
            expected_bar_count=historical_replay.expected_bar_count(session),
            early_close=market_calendar.is_early_close(session),
            request_params=historical_replay.request_params(symbol, session),
        )
        for session in sessions
        for symbol in replay_universe.symbols
    )
    return ReplayPreflight(
        universe_id=replay_universe.universe_id,
        universe_symbols=replay_universe.symbols,
        sessions=sessions,
        requests=requests,
        total_expected_bars=sum(item.expected_bar_count for item in requests),
    )


def _sanitized_error(exc: Exception) -> str:
    """A stable, secret-free label for a provider or transport failure.

    Only the exception class name is used (plus an HTTP status for ``ApiError``).
    Exception messages are never included: they can carry a URL, a query string, or a
    response body, none of which belong in durable research evidence.
    """
    if isinstance(exc, api.ApiError):
        return f"ApiError:{exc.status_code}"
    return type(exc).__name__


def acquire(
    fetcher: ReplayBarFetcher,
    store: SqlAlchemyHistoricalReplayStore,
    replay_universe: ReplayUniverse,
    sessions: tuple[date, ...],
    *,
    clock: Callable[[], datetime] = _utc_now,
) -> ReplayAcquisitionReport:
    """Download, validate, and idempotently persist research evidence.

    A provider failure for one (symbol, session) is recorded as an ``unavailable``
    observation and the run continues; it is never retried automatically here, so a
    flaky provider cannot silently multiply request volume.
    """
    started_at = clock()
    store.register_universe(replay_universe, now=started_at)

    outcomes: list[SessionOutcome] = []
    for session in sessions:
        for symbol in replay_universe.symbols:
            evidence = _retrieve(fetcher, replay_universe, symbol, session, clock=clock)
            result = store.ingest(evidence, now=clock())
            outcomes.append(_outcome(evidence, result))
    return ReplayAcquisitionReport(
        universe_id=replay_universe.universe_id,
        universe_symbols=replay_universe.symbols,
        sessions=sessions,
        started_at=started_at,
        finished_at=clock(),
        outcomes=tuple(outcomes),
    )


def _retrieve(
    fetcher: ReplayBarFetcher,
    replay_universe: ReplayUniverse,
    symbol: str,
    session: date,
    *,
    clock: Callable[[], datetime],
) -> ReplaySessionEvidence:
    try:
        candles = fetcher(symbol, session)
    except Exception as exc:
        # Every provider failure becomes safe, sanitized evidence rather than a crash.
        return historical_replay.unavailable_session(
            replay_universe,
            symbol,
            session,
            retrieved_at=clock(),
            error=_sanitized_error(exc),
            source=market_data.SCHWAB_REGULAR_SESSION_SOURCE,
        )
    return historical_replay.validate_replay_session(
        replay_universe,
        symbol,
        session,
        candles,
        retrieved_at=clock(),
        source=market_data.SCHWAB_REGULAR_SESSION_SOURCE,
    )


def _outcome(
    evidence: ReplaySessionEvidence,
    result: ReplayIngestResult,
) -> SessionOutcome:
    return SessionOutcome(
        symbol=evidence.symbol,
        session_date=evidence.session_date,
        status=evidence.status,
        ingest_outcome=result.outcome,
        revision=result.revision,
        replay_id=result.replay_id,
        previous_replay_id=result.previous_replay_id,
        expected_bar_count=evidence.expected_bar_count,
        unique_bar_count=evidence.unique_bar_count,
        issues=tuple(issue.value for issue in evidence.issues),
        error=evidence.error,
    )


def preflight_payload(plan: ReplayPreflight) -> dict[str, object]:
    """Stable JSON primitives; no credential, token, account, or URL is present."""
    return {
        "mode": "preflight",
        "universe_id": plan.universe_id,
        "universe_symbols": list(plan.universe_symbols),
        "session_count": len(plan.sessions),
        "sessions": [item.isoformat() for item in plan.sessions],
        "request_count": len(plan.requests),
        "total_expected_bars": plan.total_expected_bars,
        "requests": [
            {
                "symbol": item.symbol,
                "session": item.session_date.isoformat(),
                "expected_bar_count": item.expected_bar_count,
                "early_close": item.early_close,
                "request_params": dict(item.request_params),
            }
            for item in plan.requests
        ],
    }


def report_payload(report: ReplayAcquisitionReport) -> dict[str, object]:
    """Stable JSON primitives summarizing an acquisition run."""
    return {
        "mode": "download",
        "universe_id": report.universe_id,
        "universe_symbols": list(report.universe_symbols),
        "session_count": len(report.sessions),
        "started_at": report.started_at.isoformat(),
        "finished_at": report.finished_at.isoformat(),
        "totals": {
            "complete": len(report.complete),
            "incomplete": len(report.incomplete),
            "unavailable": len(report.unavailable),
            "corrected": len(report.corrected),
            "unchanged": len(report.unchanged),
        },
        "outcomes": [
            {
                "symbol": item.symbol,
                "session": item.session_date.isoformat(),
                "status": item.status.value,
                "ingest_outcome": item.ingest_outcome.value,
                "revision": item.revision,
                "replay_id": item.replay_id,
                "previous_replay_id": item.previous_replay_id,
                "expected_bar_count": item.expected_bar_count,
                "unique_bar_count": item.unique_bar_count,
                "issues": list(item.issues),
                "error": item.error,
            }
            for item in report.outcomes
        ],
    }


__all__ = [
    "PlannedRequest",
    "ReplayAcquisitionReport",
    "ReplayBarFetcher",
    "ReplayPreflight",
    "SessionOutcome",
    "acquire",
    "preflight",
    "preflight_payload",
    "report_payload",
    "schwab_session_fetcher",
]
