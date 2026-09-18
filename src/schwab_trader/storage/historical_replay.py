"""Durable storage for historical-replay **research** evidence.

This adapter is intentionally isolated from the official evidence store in
:mod:`schwab_trader.storage.market_data`. It writes only the four
``historical_replay_*`` tables and imports nothing from the paper engine, the
evaluation/official-observation store, the cohort lifecycle, or the order path.

Repeated ingestion of identical content is idempotent: the content-addressed
``replay_id`` row is reused and only ``last_seen_at`` advances. Different content for a
(symbol, session) already on record is a **provider correction** — a new observation
revision naming the exact record it replaced, never an overwrite.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from schwab_trader import historical_replay
from schwab_trader.historical_replay import (
    ReplayBar,
    ReplaySessionEvidence,
    ReplaySessionStatus,
    ReplayUniverse,
)
from schwab_trader.storage.database import Database
from schwab_trader.storage.schema import (
    HistoricalReplayBar,
    HistoricalReplayObservation,
    HistoricalReplaySession,
    HistoricalReplayUniverse,
)


class ReplayIngestOutcome(StrEnum):
    """What an ingestion attempt did to durable research evidence."""

    RECORDED = "recorded"
    UNCHANGED = "unchanged"
    CORRECTED = "corrected"


class ReplayIngestResult(BaseModel):
    """The durable result of ingesting one piece of replay evidence."""

    model_config = ConfigDict(frozen=True)

    outcome: ReplayIngestOutcome
    revision: int
    replay_id: str
    previous_replay_id: str | None = None
    symbol: str
    session_date: date
    status: ReplaySessionStatus


class SqlAlchemyHistoricalReplayStore:
    """Store replay research evidence identically in SQLite and PostgreSQL."""

    def __init__(self, database: Database) -> None:
        self.database = database

    # -- universes ---------------------------------------------------------------

    def register_universe(
        self,
        replay_universe: ReplayUniverse,
        *,
        now: datetime,
    ) -> ReplayUniverse:
        """Record the requested cohort-independent universe once, then reuse it."""
        expected = historical_replay.universe(
            replay_universe.symbols,
            label=replay_universe.label,
        )
        if expected.universe_id != replay_universe.universe_id:
            raise ValueError("universe identity does not match its symbols")

        with self.database.session() as session:
            row = session.get(HistoricalReplayUniverse, replay_universe.universe_id)
            if row is None:
                session.add(
                    HistoricalReplayUniverse(
                        universe_id=replay_universe.universe_id,
                        label=replay_universe.label,
                        symbols=list(replay_universe.symbols),
                        created_at=now,
                    )
                )
        return replay_universe

    def universe(self, universe_id: str) -> ReplayUniverse | None:
        with self.database.session() as session:
            row = session.get(HistoricalReplayUniverse, universe_id)
            if row is None:
                return None
            return ReplayUniverse(
                universe_id=row.universe_id,
                label=row.label,
                symbols=tuple(row.symbols),
            )

    # -- ingestion ---------------------------------------------------------------

    def ingest(
        self,
        evidence: ReplaySessionEvidence,
        *,
        now: datetime,
    ) -> ReplayIngestResult:
        """Persist one observation idempotently and classify the outcome."""
        with self.database.session() as session:
            if session.get(HistoricalReplayUniverse, evidence.universe_id) is None:
                raise ValueError(
                    "the replay universe must be registered before its evidence is ingested"
                )

            latest = session.scalars(
                select(HistoricalReplayObservation)
                .where(
                    HistoricalReplayObservation.symbol == evidence.symbol,
                    HistoricalReplayObservation.session_date == evidence.session_date,
                )
                .order_by(HistoricalReplayObservation.revision.desc())
                .limit(1)
            ).first()

            if latest is not None and latest.replay_id == evidence.replay_id:
                row = session.get(HistoricalReplaySession, evidence.replay_id)
                if row is None:  # pragma: no cover - defended by the FK
                    raise ValueError("observation log references a missing replay session")
                row.last_seen_at = now
                return ReplayIngestResult(
                    outcome=ReplayIngestOutcome.UNCHANGED,
                    revision=latest.revision,
                    replay_id=evidence.replay_id,
                    previous_replay_id=latest.previous_replay_id,
                    symbol=evidence.symbol,
                    session_date=evidence.session_date,
                    status=evidence.status,
                )

            existing = session.get(HistoricalReplaySession, evidence.replay_id)
            if existing is None:
                self._insert_session(session, evidence, now=now)
            else:
                # Content seen before for this session and now returned again after a
                # different payload: still a correction, but the row already exists.
                existing.last_seen_at = now

            revision = 1 if latest is None else latest.revision + 1
            outcome = (
                ReplayIngestOutcome.RECORDED if latest is None else ReplayIngestOutcome.CORRECTED
            )
            session.add(
                HistoricalReplayObservation(
                    symbol=evidence.symbol,
                    session_date=evidence.session_date,
                    revision=revision,
                    replay_id=evidence.replay_id,
                    previous_replay_id=None if latest is None else latest.replay_id,
                    observed_at=now,
                    outcome=outcome.value,
                )
            )
            return ReplayIngestResult(
                outcome=outcome,
                revision=revision,
                replay_id=evidence.replay_id,
                previous_replay_id=None if latest is None else latest.replay_id,
                symbol=evidence.symbol,
                session_date=evidence.session_date,
                status=evidence.status,
            )

    @staticmethod
    def _insert_session(
        session: Session,
        evidence: ReplaySessionEvidence,
        *,
        now: datetime,
    ) -> None:
        session.add(
            HistoricalReplaySession(
                replay_id=evidence.replay_id,
                universe_id=evidence.universe_id,
                symbol=evidence.symbol,
                session_date=evidence.session_date,
                provider=evidence.provider,
                source=evidence.source,
                status=evidence.status.value,
                retrieved_at=evidence.retrieved_at,
                first_seen_at=now,
                last_seen_at=now,
                raw_payload_digest=evidence.raw_payload_digest,
                normalized_bar_digest=evidence.normalized_bar_digest,
                expected_bar_count=evidence.expected_bar_count,
                returned_bar_count=evidence.returned_bar_count,
                in_session_bar_count=evidence.in_session_bar_count,
                unique_bar_count=evidence.unique_bar_count,
                request_params=dict(evidence.request_params),
                validation=_validation_payload(evidence),
                error=evidence.error,
            )
        )
        # No ORM relationship ties these tables together, so the unit of work does not
        # know the bars and the observation log depend on this row. Flush it first or
        # the child inserts race ahead of their parent and trip the foreign key.
        session.flush()
        session.add_all(
            [
                HistoricalReplayBar(
                    replay_id=evidence.replay_id,
                    ordinal=ordinal,
                    interval_at=bar.interval_at,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                )
                for ordinal, bar in enumerate(evidence.bars)
            ]
        )

    # -- reads -------------------------------------------------------------------

    def get(self, replay_id: str) -> ReplaySessionEvidence | None:
        with self.database.session() as session:
            row = session.get(HistoricalReplaySession, replay_id)
            if row is None:
                return None
            universe_row = session.get(HistoricalReplayUniverse, row.universe_id)
            bars = list(
                session.scalars(
                    select(HistoricalReplayBar)
                    .where(HistoricalReplayBar.replay_id == replay_id)
                    .order_by(HistoricalReplayBar.ordinal)
                )
            )
            symbols = () if universe_row is None else tuple(universe_row.symbols)
            return _domain(row, bars, symbols)

    def latest(self, symbol: str, session_date: date) -> ReplaySessionEvidence | None:
        """The most recent observation's evidence for one (symbol, session)."""
        normalized = symbol.strip().upper()
        with self.database.session() as session:
            observation = session.scalars(
                select(HistoricalReplayObservation)
                .where(
                    HistoricalReplayObservation.symbol == normalized,
                    HistoricalReplayObservation.session_date == session_date,
                )
                .order_by(HistoricalReplayObservation.revision.desc())
                .limit(1)
            ).first()
            replay_id = None if observation is None else observation.replay_id
        return None if replay_id is None else self.get(replay_id)

    def history(
        self,
        symbol: str,
        session_date: date,
    ) -> tuple[ReplayIngestResult, ...]:
        """Every observation for one (symbol, session), oldest revision first."""
        normalized = symbol.strip().upper()
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(HistoricalReplayObservation)
                    .where(
                        HistoricalReplayObservation.symbol == normalized,
                        HistoricalReplayObservation.session_date == session_date,
                    )
                    .order_by(HistoricalReplayObservation.revision)
                )
            )
            statuses = {
                found.replay_id: found.status
                for found in session.scalars(
                    select(HistoricalReplaySession).where(
                        HistoricalReplaySession.symbol == normalized,
                        HistoricalReplaySession.session_date == session_date,
                    )
                )
            }
        return tuple(
            ReplayIngestResult(
                outcome=ReplayIngestOutcome(row.outcome),
                revision=row.revision,
                replay_id=row.replay_id,
                previous_replay_id=row.previous_replay_id,
                symbol=row.symbol,
                session_date=row.session_date,
                status=ReplaySessionStatus(statuses[row.replay_id]),
            )
            for row in rows
        )

    def status_counts(self, universe_id: str) -> dict[str, int]:
        """Complete/incomplete/unavailable totals for one universe's stored evidence."""
        with self.database.session() as session:
            rows = session.execute(
                select(
                    HistoricalReplaySession.status,
                    func.count(),
                )
                .where(HistoricalReplaySession.universe_id == universe_id)
                .group_by(HistoricalReplaySession.status)
            ).all()
        return {str(status): int(count) for status, count in rows}


def _validation_payload(evidence: ReplaySessionEvidence) -> dict[str, object]:
    payload = historical_replay.evidence_payload(evidence)
    return {
        key: payload[key]
        for key in (
            "issues",
            "missing_intervals",
            "duplicate_intervals",
            "out_of_session_intervals",
            "invalid_ohlc_intervals",
            "negative_volume_intervals",
        )
    }


def _domain(
    row: HistoricalReplaySession,
    bars: list[HistoricalReplayBar],
    universe_symbols: tuple[str, ...],
) -> ReplaySessionEvidence:
    validation = row.validation
    return ReplaySessionEvidence(
        replay_id=row.replay_id,
        universe_id=row.universe_id,
        universe_symbols=universe_symbols,
        symbol=row.symbol,
        session_date=row.session_date,
        provider=row.provider,
        source=row.source,
        retrieved_at=row.retrieved_at,
        request_params=dict(row.request_params),
        raw_payload_digest=row.raw_payload_digest,
        normalized_bar_digest=row.normalized_bar_digest,
        status=ReplaySessionStatus(row.status),
        expected_bar_count=row.expected_bar_count,
        returned_bar_count=row.returned_bar_count,
        in_session_bar_count=row.in_session_bar_count,
        unique_bar_count=row.unique_bar_count,
        missing_intervals=_moments(validation.get("missing_intervals")),
        duplicate_intervals=_moments(validation.get("duplicate_intervals")),
        out_of_session_intervals=_moments(validation.get("out_of_session_intervals")),
        invalid_ohlc_intervals=_moments(validation.get("invalid_ohlc_intervals")),
        negative_volume_intervals=_moments(validation.get("negative_volume_intervals")),
        issues=tuple(
            historical_replay.ReplayIssue(item) for item in validation.get("issues", ()) or ()
        ),
        error=row.error,
        bars=tuple(
            ReplayBar(
                interval_at=bar.interval_at,
                open=Decimal(bar.open),
                high=Decimal(bar.high),
                low=Decimal(bar.low),
                close=Decimal(bar.close),
                volume=bar.volume,
            )
            for bar in bars
        ),
    )


def _moments(values: object) -> tuple[datetime, ...]:
    if not isinstance(values, list):
        return ()
    return tuple(datetime.fromisoformat(str(item)) for item in values)


__all__ = [
    "ReplayIngestOutcome",
    "ReplayIngestResult",
    "SqlAlchemyHistoricalReplayStore",
]
