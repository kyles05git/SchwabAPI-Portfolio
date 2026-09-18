"""Durable, backend-neutral storage for intraday-derived daily evidence."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from schwab_trader import market_bar_evidence, market_calendar, market_data
from schwab_trader.market_bar_evidence import DerivedDailyEvidence
from schwab_trader.market_data import Candle
from schwab_trader.storage.database import Database
from schwab_trader.storage.schema import (
    MarketDataDailyEvidence,
    MarketDataEvidenceConstituent,
)


class EvidenceConflictError(RuntimeError):
    """Persisted content disagrees with its content-addressed dataset identity."""


class SqlAlchemyMarketDataEvidenceStore:
    """Store exact derived evidence identically in SQLite and PostgreSQL."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _verify(evidence: DerivedDailyEvidence) -> DerivedDailyEvidence:
        result = market_bar_evidence.validate_regular_session(
            evidence.symbol,
            evidence.session_date,
            list(evidence.constituents),
            retrieved_at=evidence.retrieved_at,
        )
        reproduced = result.evidence
        if not result.aggregation_safe or reproduced is None:
            raise ValueError("only complete reproducible five-minute evidence may be persisted")
        if (
            reproduced.dataset_id != evidence.dataset_id
            or reproduced.constituent_digest != evidence.constituent_digest
            or reproduced.candle != evidence.candle
            or reproduced.expected_interval_count != evidence.expected_interval_count
            or reproduced.observed_interval_count != evidence.observed_interval_count
        ):
            raise ValueError("derived evidence does not match its deterministic identity")
        return reproduced

    def save(self, evidence: DerivedDailyEvidence) -> DerivedDailyEvidence:
        """Persist once and return the canonical first-seen evidence.

        The dataset id excludes retrieval time, so an identical later retry converges
        on the original durable row rather than changing snapshot identity or rewriting
        the evidence first used by an official run.
        """
        verified = self._verify(evidence)
        candle = verified.candle
        if candle.open is None or candle.high is None or candle.low is None:
            raise ValueError("derived daily evidence must carry complete OHLC values")

        with self.database.session() as session:
            existing = session.get(MarketDataDailyEvidence, verified.dataset_id)
            if existing is None:
                session.add(
                    MarketDataDailyEvidence(
                        dataset_id=verified.dataset_id,
                        symbol=verified.symbol,
                        session_date=verified.session_date,
                        retrieved_at=verified.retrieved_at,
                        source=verified.source,
                        expected_interval_count=verified.expected_interval_count,
                        observed_interval_count=verified.observed_interval_count,
                        first_interval_at=verified.first_interval_at,
                        final_interval_at=verified.final_interval_at,
                        constituent_digest=verified.constituent_digest,
                        open=candle.open,
                        high=candle.high,
                        low=candle.low,
                        close=candle.close,
                        volume=candle.volume,
                    )
                )
                session.add_all(
                    [
                        MarketDataEvidenceConstituent(
                            dataset_id=verified.dataset_id,
                            ordinal=ordinal,
                            interval_at=constituent.date,
                            open=constituent.open,
                            high=constituent.high,
                            low=constituent.low,
                            close=constituent.close,
                            volume=constituent.volume,
                        )
                        for ordinal, constituent in enumerate(verified.constituents)
                    ]
                )

        stored = self.get(verified.dataset_id)
        if stored is None:
            raise EvidenceConflictError("derived evidence was not durable after commit")
        if not self._same_content(stored, verified):
            raise EvidenceConflictError(
                "persisted derived evidence conflicts with its dataset identity"
            )
        return stored

    @staticmethod
    def _same_content(
        stored: DerivedDailyEvidence,
        candidate: DerivedDailyEvidence,
    ) -> bool:
        return (
            stored.dataset_id == candidate.dataset_id
            and stored.constituent_digest == candidate.constituent_digest
            and stored.symbol == candidate.symbol
            and stored.session_date == candidate.session_date
            and stored.source == candidate.source
            and stored.expected_interval_count == candidate.expected_interval_count
            and stored.observed_interval_count == candidate.observed_interval_count
            and stored.first_interval_at == candidate.first_interval_at
            and stored.final_interval_at == candidate.final_interval_at
            and stored.candle == candidate.candle
            and len(stored.constituents) == len(candidate.constituents)
            and all(
                left.date == right.date
                and left.open == right.open
                and left.high == right.high
                and left.low == right.low
                and left.close == right.close
                and left.volume == right.volume
                for left, right in zip(
                    stored.constituents,
                    candidate.constituents,
                    strict=True,
                )
            )
        )

    def get(self, dataset_id: str) -> DerivedDailyEvidence | None:
        with self.database.session() as session:
            row = session.get(MarketDataDailyEvidence, dataset_id)
            if row is None:
                return None
            constituents = list(
                session.scalars(
                    select(MarketDataEvidenceConstituent)
                    .where(MarketDataEvidenceConstituent.dataset_id == dataset_id)
                    .order_by(MarketDataEvidenceConstituent.ordinal)
                )
            )
            return self._domain(row, constituents)

    def for_session(
        self,
        symbol: str,
        session_date: date,
    ) -> tuple[DerivedDailyEvidence, ...]:
        normalized = symbol.strip().upper()
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(MarketDataDailyEvidence)
                    .where(
                        MarketDataDailyEvidence.symbol == normalized,
                        MarketDataDailyEvidence.session_date == session_date,
                    )
                    .order_by(
                        MarketDataDailyEvidence.retrieved_at,
                        MarketDataDailyEvidence.dataset_id,
                    )
                )
            )
            results: list[DerivedDailyEvidence] = []
            for row in rows:
                constituents = list(
                    session.scalars(
                        select(MarketDataEvidenceConstituent)
                        .where(MarketDataEvidenceConstituent.dataset_id == row.dataset_id)
                        .order_by(MarketDataEvidenceConstituent.ordinal)
                    )
                )
                results.append(self._domain(row, constituents))
            return tuple(results)

    def reproduce(self, dataset_id: str) -> DerivedDailyEvidence | None:
        """Recompute and verify the aggregate from the exact persisted constituents."""
        stored = self.get(dataset_id)
        if stored is None:
            return None
        try:
            reproduced = self._verify(stored)
        except ValueError as exc:
            raise EvidenceConflictError(
                "persisted constituents do not reproduce their recorded daily aggregate"
            ) from exc
        if not self._same_content(stored, reproduced):
            raise EvidenceConflictError(
                "persisted constituents do not reproduce their recorded daily aggregate"
            )
        return stored

    @staticmethod
    def _domain(
        row: MarketDataDailyEvidence,
        constituents: list[MarketDataEvidenceConstituent],
    ) -> DerivedDailyEvidence:
        _, closed = market_calendar.session_bounds_utc(row.session_date)
        aggregate = Candle(
            symbol=row.symbol,
            date=closed,
            open=Decimal(row.open),
            high=Decimal(row.high),
            low=Decimal(row.low),
            close=Decimal(row.close),
            volume=row.volume,
            source=row.source,
        )
        exact = tuple(
            Candle(
                symbol=row.symbol,
                date=item.interval_at,
                open=Decimal(item.open),
                high=Decimal(item.high),
                low=Decimal(item.low),
                close=Decimal(item.close),
                volume=item.volume,
                source=market_data.SCHWAB_REGULAR_SESSION_SOURCE,
            )
            for item in constituents
        )
        return DerivedDailyEvidence(
            dataset_id=row.dataset_id,
            constituent_digest=row.constituent_digest,
            symbol=row.symbol,
            session_date=row.session_date,
            retrieved_at=row.retrieved_at,
            source=row.source,
            expected_interval_count=row.expected_interval_count,
            observed_interval_count=row.observed_interval_count,
            first_interval_at=row.first_interval_at,
            final_interval_at=row.final_interval_at,
            candle=aggregate,
            constituents=exact,
        )


__all__ = [
    "EvidenceConflictError",
    "SqlAlchemyMarketDataEvidenceStore",
]
