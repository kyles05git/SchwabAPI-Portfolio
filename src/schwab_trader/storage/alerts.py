"""Shared-database cohort alert transitions.

The SQLite adapter in :mod:`schwab_trader.cohort_alerts` serializes writes with one
local file lock. This adapter has to hold the same at-most-once guarantee when several
processes can reach the database at once, so the claim reads the row ``FOR UPDATE``
inside a transaction: two concurrent scheduler invocations serialize, and exactly one
of them sees "no record" and claims the transition.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from schwab_trader.cohort_alerts import (
    DEFAULT_AMBIGUOUS_AFTER,
    DEFAULT_MAX_ATTEMPTS,
    AlertDelivery,
    AlertKind,
    ClaimOutcome,
    ClaimResult,
    CohortAlert,
    alert_key,
)
from schwab_trader.storage.database import Database
from schwab_trader.storage.schema import CohortAlert as CohortAlertRow


def _utc(value: datetime | None = None) -> datetime:
    stamp = value or datetime.now(UTC)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        return stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


class SqlAlchemyCohortAlertStore:
    """Alert transition repository safe for multiple PostgreSQL writers."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def claim(
        self,
        *,
        cohort_id: str,
        session_id: str,
        scheduled_for: date,
        kind: AlertKind,
        detail: str | None = None,
        now: datetime | None = None,
        ambiguous_after: timedelta = DEFAULT_AMBIGUOUS_AFTER,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> ClaimResult:
        key = alert_key(cohort_id, session_id, kind)
        stamp = _utc(now)
        outcome = ClaimOutcome.CLAIMED
        with self.database.session() as session:
            row = session.scalar(
                select(CohortAlertRow).where(CohortAlertRow.alert_key == key).with_for_update()
            )
            if row is None:
                session.add(
                    CohortAlertRow(
                        alert_key=key,
                        cohort_id=cohort_id,
                        session_id=session_id,
                        scheduled_for=scheduled_for,
                        kind=kind.value,
                        delivery=AlertDelivery.PENDING.value,
                        attempts=1,
                        created_at=stamp,
                        updated_at=stamp,
                        delivered_at=None,
                        detail=detail,
                        failure_reason=None,
                    )
                )
            else:
                delivery = AlertDelivery(row.delivery)
                if delivery is AlertDelivery.SENT:
                    return ClaimResult(ClaimOutcome.ALREADY_SENT, self._domain(row))
                if delivery is AlertDelivery.PENDING:
                    # The row lock serializes concurrent writers, but a *crashed*
                    # writer left no lock behind — only a stale timestamp.
                    if stamp - _utc(row.updated_at) < ambiguous_after:
                        return ClaimResult(ClaimOutcome.IN_FLIGHT, self._domain(row))
                    if row.attempts >= max_attempts:
                        return ClaimResult(ClaimOutcome.EXHAUSTED, self._domain(row))
                    outcome = ClaimOutcome.RECLAIMED_UNCERTAIN
                elif row.attempts >= max_attempts:
                    return ClaimResult(ClaimOutcome.EXHAUSTED, self._domain(row))
                # Nothing was delivered on a failed attempt, so retrying it cannot
                # duplicate a notification the operator has already seen.
                row.delivery = AlertDelivery.PENDING.value
                row.attempts += 1
                row.updated_at = stamp
                row.detail = detail
                row.failure_reason = None
        return ClaimResult(outcome, self.get(key))

    def mark_sent(self, key: str, *, now: datetime | None = None) -> CohortAlert | None:
        stamp = _utc(now)
        with self.database.session() as session:
            row = session.scalar(
                select(CohortAlertRow).where(CohortAlertRow.alert_key == key).with_for_update()
            )
            if row is not None:
                row.delivery = AlertDelivery.SENT.value
                row.delivered_at = stamp
                row.updated_at = stamp
                row.failure_reason = None
        return self.get(key)

    def mark_failed(
        self, key: str, *, reason: str, now: datetime | None = None
    ) -> CohortAlert | None:
        stamp = _utc(now)
        with self.database.session() as session:
            row = session.scalar(
                select(CohortAlertRow).where(CohortAlertRow.alert_key == key).with_for_update()
            )
            if row is not None:
                row.delivery = AlertDelivery.FAILED.value
                row.updated_at = stamp
                row.failure_reason = reason
        return self.get(key)

    def get(self, key: str) -> CohortAlert | None:
        with self.database.session() as session:
            row = session.get(CohortAlertRow, key)
            return None if row is None else self._domain(row)

    def list(self, *, cohort_id: str | None = None, limit: int = 100) -> list[CohortAlert]:
        statement = select(CohortAlertRow)
        if cohort_id is not None:
            statement = statement.where(CohortAlertRow.cohort_id == cohort_id)
        statement = statement.order_by(
            CohortAlertRow.scheduled_for.desc(), CohortAlertRow.created_at.desc()
        ).limit(limit)
        with self.database.session() as session:
            return [self._domain(row) for row in session.scalars(statement)]

    @staticmethod
    def _domain(row: CohortAlertRow) -> CohortAlert:
        return CohortAlert(
            alert_key=row.alert_key,
            cohort_id=row.cohort_id,
            session_id=row.session_id,
            scheduled_for=row.scheduled_for,
            kind=AlertKind(row.kind),
            delivery=AlertDelivery(row.delivery),
            attempts=row.attempts,
            created_at=_utc(row.created_at),
            updated_at=_utc(row.updated_at),
            delivered_at=None if row.delivered_at is None else _utc(row.delivered_at),
            detail=row.detail,
            failure_reason=row.failure_reason,
        )


__all__ = ["SqlAlchemyCohortAlertStore"]
