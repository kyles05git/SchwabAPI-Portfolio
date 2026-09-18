"""Append-only persistence for cohort accounting reviews and operator decisions.

One adapter serves both supported backends. There is no second SQLite implementation
because there is nothing backend-specific to implement: every write is an ``INSERT``,
and the guarantees that matter — deterministic identity, no lost update under
concurrency, no partial write — come from the primary key, a unique constraint on
``(identity, revision)``, and a single transaction, all of which SQLite and PostgreSQL
provide identically.

The revision race is worth spelling out. Two operators correcting the same entry at once
both read revision 2 and both try to append revision 3. They do not both succeed and they
do not silently overwrite each other: one commits, the other's insert violates
``uq_cohort_accounting_checks_entry`` and is reported as a conflict to re-run against the
record that won. Losing a correction is not possible; being told to look again is.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from schwab_trader.cohort_review import (
    AccountingCheck,
    CheckIntent,
    CheckOutcome,
    DecisionIntent,
    DecisionOutcome,
    NoteIntent,
    NoteOutcome,
    ReviewConflictError,
    ReviewFinding,
    ReviewNote,
    SleeveDecision,
    WriteStatus,
    check_entry_id,
    decision_entry_id,
    note_entry_id,
)
from schwab_trader.operational_gate import AccountingArea, OperatorAction
from schwab_trader.storage.database import Database
from schwab_trader.storage.schema import CohortAccountingCheck as CheckRow
from schwab_trader.storage.schema import CohortOperatorDecision as DecisionRow
from schwab_trader.storage.schema import CohortReviewNote as NoteRow


def _utc(value: datetime) -> datetime:
    """Normalize a stored timestamp to UTC.

    SQLite has no native timezone, so ``AwareTimestamp`` round-trips a naive value for
    it. Anything reaching the store has already been rejected unless it was aware, so a
    naive value read back is that same instant in UTC rather than a local guess.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class SqlAlchemyCohortReviewStore:
    """Cohort review repository over SQLite or PostgreSQL."""

    def __init__(self, database: Database) -> None:
        self.database = database

    # --- accounting checks --------------------------------------------------

    def record_check(self, intent: CheckIntent, *, allow_supersede: bool) -> CheckOutcome:
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(CheckRow)
                    .where(
                        CheckRow.cohort_id == intent.cohort_id,
                        CheckRow.observation_key == intent.observation_key,
                        CheckRow.area == intent.area.value,
                    )
                    .order_by(CheckRow.revision.desc())
                    .with_for_update()
                )
            )
            latest = rows[0] if rows else None
            if latest is not None:
                current = _check(latest)
                if intent.matches(current):
                    return CheckOutcome(WriteStatus.UNCHANGED, current)
                if not allow_supersede:
                    raise ReviewConflictError(
                        f"{intent.area.value} for {intent.observation_key} is already recorded "
                        f"as {current.finding.value} (revision {current.revision}); re-run with "
                        "an explicit supersede to correct it"
                    )
            revision = 0 if latest is None else latest.revision + 1
            entry_id = check_entry_id(
                intent.cohort_id, intent.observation_key, intent.area, revision
            )
            session.add(
                CheckRow(
                    entry_id=entry_id,
                    cohort_id=intent.cohort_id,
                    sleeve_id=intent.sleeve_id,
                    observation_key=intent.observation_key,
                    session_date=intent.session_date,
                    area=intent.area.value,
                    finding=intent.finding.value,
                    summary=intent.summary,
                    explanation=intent.explanation,
                    recorded_at=intent.recorded_at,
                    recorded_by=intent.recorded_by,
                    revision=revision,
                    supersedes=None if latest is None else latest.entry_id,
                )
            )
            try:
                session.flush()
            except IntegrityError as exc:
                raise ReviewConflictError(
                    f"another writer recorded revision {revision} of {intent.area.value} for "
                    f"{intent.observation_key} first; re-read the review and try again"
                ) from exc
            status = WriteStatus.RECORDED if latest is None else WriteStatus.SUPERSEDED
        stored = self.check(entry_id)
        assert stored is not None  # written in the transaction above
        return CheckOutcome(status, stored)

    def check(self, entry_id: str) -> AccountingCheck | None:
        with self.database.session() as session:
            row = session.get(CheckRow, entry_id)
            return None if row is None else _check(row)

    def checks(self, cohort_id: str) -> list[AccountingCheck]:
        statement = (
            select(CheckRow)
            .where(CheckRow.cohort_id == cohort_id)
            .order_by(CheckRow.session_date, CheckRow.sleeve_id, CheckRow.area, CheckRow.revision)
        )
        with self.database.session() as session:
            return [_check(row) for row in session.scalars(statement)]

    # --- notes --------------------------------------------------------------

    def add_note(self, intent: NoteIntent) -> NoteOutcome:
        note_id = note_entry_id(
            intent.cohort_id, intent.sleeve_id, intent.observation_key, intent.note
        )
        with self.database.session() as session:
            existing = session.get(NoteRow, note_id)
            if existing is not None:
                return NoteOutcome(WriteStatus.UNCHANGED, _note(existing))
            session.add(
                NoteRow(
                    note_id=note_id,
                    cohort_id=intent.cohort_id,
                    sleeve_id=intent.sleeve_id,
                    observation_key=intent.observation_key,
                    note=intent.note,
                    recorded_at=intent.recorded_at,
                    recorded_by=intent.recorded_by,
                )
            )
            try:
                session.flush()
            except IntegrityError as exc:
                # Another writer added the identical note between the read and the
                # insert. Nothing is lost — their row says exactly what this one would
                # have — but this transaction cannot continue, so it is reported rather
                # than quietly reissued inside a failed transaction.
                raise ReviewConflictError(
                    "an identical note was recorded concurrently; re-read the review"
                ) from exc
        stored = self.note(note_id)
        assert stored is not None  # written in the transaction above
        return NoteOutcome(WriteStatus.RECORDED, stored)

    def note(self, note_id: str) -> ReviewNote | None:
        with self.database.session() as session:
            row = session.get(NoteRow, note_id)
            return None if row is None else _note(row)

    def notes(self, cohort_id: str) -> list[ReviewNote]:
        statement = (
            select(NoteRow)
            .where(NoteRow.cohort_id == cohort_id)
            .order_by(NoteRow.recorded_at, NoteRow.note_id)
        )
        with self.database.session() as session:
            return [_note(row) for row in session.scalars(statement)]

    # --- operator decisions -------------------------------------------------

    def record_decision(
        self, intent: DecisionIntent, *, allow_supersede: bool
    ) -> DecisionOutcome:
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(DecisionRow)
                    .where(
                        DecisionRow.cohort_id == intent.cohort_id,
                        DecisionRow.sleeve_id == intent.sleeve_id,
                    )
                    .order_by(DecisionRow.revision.desc())
                    .with_for_update()
                )
            )
            latest = rows[0] if rows else None
            if latest is not None:
                current = _decision(latest)
                if intent.matches(current):
                    return DecisionOutcome(WriteStatus.UNCHANGED, current)
                if not allow_supersede:
                    raise ReviewConflictError(
                        f"sleeve {intent.sleeve_id} already has a recorded "
                        f"{current.action.value} decision (revision {current.revision}); "
                        "re-run with an explicit supersede to replace it"
                    )
            revision = 0 if latest is None else latest.revision + 1
            decision_id = decision_entry_id(intent.cohort_id, intent.sleeve_id, revision)
            session.add(
                DecisionRow(
                    decision_id=decision_id,
                    cohort_id=intent.cohort_id,
                    sleeve_id=intent.sleeve_id,
                    action=intent.action.value,
                    rationale=intent.rationale,
                    recorded_at=intent.recorded_at,
                    recorded_by=intent.recorded_by,
                    revision=revision,
                    supersedes=None if latest is None else latest.decision_id,
                )
            )
            try:
                session.flush()
            except IntegrityError as exc:
                raise ReviewConflictError(
                    f"another writer recorded revision {revision} for sleeve "
                    f"{intent.sleeve_id} first; re-read the review and try again"
                ) from exc
            status = WriteStatus.RECORDED if latest is None else WriteStatus.SUPERSEDED
        stored = self.decision(decision_id)
        assert stored is not None  # written in the transaction above
        return DecisionOutcome(status, stored)

    def decision(self, decision_id: str) -> SleeveDecision | None:
        with self.database.session() as session:
            row = session.get(DecisionRow, decision_id)
            return None if row is None else _decision(row)

    def decisions(self, cohort_id: str) -> list[SleeveDecision]:
        statement = (
            select(DecisionRow)
            .where(DecisionRow.cohort_id == cohort_id)
            .order_by(DecisionRow.sleeve_id, DecisionRow.revision)
        )
        with self.database.session() as session:
            return [_decision(row) for row in session.scalars(statement)]


def _check(row: CheckRow) -> AccountingCheck:
    return AccountingCheck(
        entry_id=row.entry_id,
        cohort_id=row.cohort_id,
        sleeve_id=row.sleeve_id,
        observation_key=row.observation_key,
        session_date=row.session_date,
        area=AccountingArea(row.area),
        finding=ReviewFinding(row.finding),
        summary=row.summary,
        explanation=row.explanation,
        recorded_at=_utc(row.recorded_at),
        recorded_by=row.recorded_by,
        revision=row.revision,
        supersedes=row.supersedes,
    )


def _note(row: NoteRow) -> ReviewNote:
    return ReviewNote(
        note_id=row.note_id,
        cohort_id=row.cohort_id,
        sleeve_id=row.sleeve_id,
        observation_key=row.observation_key,
        note=row.note,
        recorded_at=_utc(row.recorded_at),
        recorded_by=row.recorded_by,
    )


def _decision(row: DecisionRow) -> SleeveDecision:
    return SleeveDecision(
        decision_id=row.decision_id,
        cohort_id=row.cohort_id,
        sleeve_id=row.sleeve_id,
        action=OperatorAction(row.action),
        rationale=row.rationale,
        recorded_at=_utc(row.recorded_at),
        recorded_by=row.recorded_by,
        revision=row.revision,
        supersedes=row.supersedes,
    )


__all__ = ["SqlAlchemyCohortReviewStore"]
