"""Durable shared cohort runs and official-session ownership."""

from __future__ import annotations

import hashlib
import os
import secrets
import socket
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select, text

from schwab_trader import scheduling
from schwab_trader.sleeve_runs import (
    MemberRunStatus,
    SleeveRun,
    SleeveRunConflictError,
    SleeveRunError,
    SleeveRunMember,
    SleeveRunStatus,
    SnapshotMismatchError,
    record_run_error,
)
from schwab_trader.storage.database import Database
from schwab_trader.storage.schema import CohortRun, CohortRunMember, OfficialSessionLease

LEASE_DURATION = timedelta(hours=6)


def _utc(value: datetime | None = None) -> datetime:
    stamp = value or datetime.now(UTC)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        return stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


def _owner_id() -> str:
    machine_hash = hashlib.sha256(socket.gethostname().encode("utf-8")).hexdigest()[:12]
    return f"machine-{machine_hash}:process-{os.getpid()}"


def _advisory_key(cohort_id: str, scheduled_for: date) -> int:
    digest = hashlib.sha256(
        f"{cohort_id}\x1f{scheduled_for.isoformat()}".encode()
    ).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


class SqlAlchemySleeveRunStore:
    """Run/checkpoint repository safe for multiple PostgreSQL writers."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def ensure_run(
        self,
        *,
        cohort_id: str,
        session: scheduling.ExchangeSession,
        expected_members: Sequence[str],
        status: SleeveRunStatus = SleeveRunStatus.PENDING,
        now: datetime | None = None,
    ) -> SleeveRun:
        cohort = cohort_id.strip()
        if not cohort:
            raise ValueError("cohort_id must not be empty")
        members = tuple(value.strip() for value in expected_members if value.strip())
        if not members or len({member.casefold() for member in members}) != len(members):
            raise ValueError("expected_members must contain distinct sleeve identities")
        key = scheduling.run_key(cohort, session)
        run_id = scheduling.run_fingerprint(cohort, session)
        stamp = _utc(now)
        with self.database.session() as db_session:
            existing = db_session.scalar(
                select(CohortRun)
                .where(CohortRun.run_id == run_id)
                .with_for_update()
            )
            if existing is None:
                db_session.add(
                    CohortRun(
                        run_id=run_id,
                        run_key=key,
                        cohort_id=cohort,
                        session_id=session.session_id,
                        scheduled_for=session.session_date,
                        expected_members=list(members),
                        completed_members=[],
                        snapshot_id=None,
                        quote_snapshot_id=None,
                        data_snapshot_ids={},
                        started_at=stamp,
                        completed_at=None,
                        status=status.value,
                        errors=[],
                        source_path=None,
                    )
                )
                # The models intentionally have no ORM relationships. Persist the
                # run parent before inserting FK-constrained member checkpoints.
                db_session.flush()
                for ordinal, member in enumerate(members):
                    db_session.add(
                        CohortRunMember(
                            run_id=run_id,
                            sleeve_id=member,
                            source_sleeve_id=None,
                            cohort_id=cohort,
                            ordinal=ordinal,
                            status=MemberRunStatus.PENDING.value,
                            started_at=None,
                            completed_at=None,
                            error=None,
                            source_path=None,
                        )
                    )
            elif (
                existing.cohort_id != cohort
                or existing.session_id != session.session_id
                or tuple(existing.expected_members) != members
            ):
                raise SleeveRunConflictError(
                    "the cohort/session run already exists with different expected members"
                )
        result = self.get(run_id)
        assert result is not None
        return result

    def get(self, run_id: str) -> SleeveRun | None:
        with self.database.session() as session:
            row = session.get(CohortRun, run_id)
            if row is None:
                return None
            members = list(
                session.scalars(
                    select(CohortRunMember)
                    .where(CohortRunMember.run_id == run_id)
                    .order_by(CohortRunMember.ordinal)
                )
            )
            return self._domain_run(row, members)

    def get_by_key(self, run_key: str) -> SleeveRun | None:
        with self.database.session() as session:
            run_id = session.scalar(
                select(CohortRun.run_id).where(CohortRun.run_key == run_key)
            )
        return None if run_id is None else self.get(run_id)

    def list(self, *, cohort_id: str | None = None, limit: int = 200) -> list[SleeveRun]:
        statement = select(CohortRun.run_id)
        if cohort_id is not None:
            statement = statement.where(CohortRun.cohort_id == cohort_id)
        statement = statement.order_by(
            CohortRun.scheduled_for.desc(), CohortRun.started_at.desc()
        ).limit(limit)
        with self.database.session() as session:
            run_ids = list(session.scalars(statement))
        return [run for run_id in run_ids if (run := self.get(run_id)) is not None]

    def completed_run_keys(self, *, cohort_id: str | None = None) -> frozenset[str]:
        statement = select(CohortRun.run_key).where(
            CohortRun.status == SleeveRunStatus.COMPLETED.value
        )
        if cohort_id is not None:
            statement = statement.where(CohortRun.cohort_id == cohort_id)
        with self.database.session() as session:
            return frozenset(session.scalars(statement))

    def set_status(
        self,
        run_id: str,
        status: SleeveRunStatus,
        *,
        error: SleeveRunError | None = None,
        now: datetime | None = None,
        terminal: bool = False,
    ) -> SleeveRun:
        with self.database.session() as session:
            row = session.scalar(
                select(CohortRun)
                .where(CohortRun.run_id == run_id)
                .with_for_update()
            )
            if row is None:
                raise KeyError(run_id)
            errors = list(row.errors)
            if error is not None:
                # Shared with the local store on purpose: dedupe and recency ordering
                # are a durable contract every reader depends on, not per-backend detail.
                record_run_error(errors, error)
            row.status = status.value
            row.errors = errors
            row.completed_at = _utc(now) if terminal else None
        result = self.get(run_id)
        assert result is not None
        return result

    def set_snapshot(
        self,
        run_id: str,
        *,
        snapshot_id: str,
        quote_snapshot_id: str,
        data_snapshot_ids: Mapping[str, str],
        allow_replace: bool = False,
    ) -> SleeveRun:
        """Bind the run to its immutable snapshot. See the local-store counterpart in
        :meth:`schwab_trader.sleeve_runs.SleeveRunStore.set_snapshot` — ``allow_replace``
        relaxes the constraint only for a run in which no member has started, and both
        backends must behave identically.
        """
        if not snapshot_id.strip() or not quote_snapshot_id.strip():
            raise ValueError("snapshot identities must not be empty")
        normalized = dict(sorted(data_snapshot_ids.items()))
        with self.database.session() as session:
            row = session.scalar(
                select(CohortRun)
                .where(CohortRun.run_id == run_id)
                .with_for_update()
            )
            if row is None:
                raise KeyError(run_id)
            if row.snapshot_id is not None and not allow_replace:
                if (
                    row.snapshot_id != snapshot_id
                    or row.quote_snapshot_id != quote_snapshot_id
                    or row.data_snapshot_ids != normalized
                ):
                    raise SnapshotMismatchError(
                        "the captured snapshot does not match the persisted run snapshot"
                    )
            else:
                row.snapshot_id = snapshot_id
                row.quote_snapshot_id = quote_snapshot_id
                row.data_snapshot_ids = normalized
        result = self.get(run_id)
        assert result is not None
        return result

    def start_member(
        self,
        run_id: str,
        sleeve_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        with self.database.session() as session:
            member = session.scalar(
                select(CohortRunMember)
                .where(
                    CohortRunMember.run_id == run_id,
                    CohortRunMember.sleeve_id == sleeve_id,
                    CohortRunMember.status == MemberRunStatus.PENDING.value,
                )
                .with_for_update()
            )
            if member is None:
                return False
            member.status = MemberRunStatus.RUNNING.value
            member.started_at = _utc(now)
            member.error = None
            return True

    def finish_member(
        self,
        run_id: str,
        sleeve_id: str,
        *,
        status: MemberRunStatus,
        error: SleeveRunError | None = None,
        now: datetime | None = None,
    ) -> SleeveRun:
        if status in {MemberRunStatus.PENDING, MemberRunStatus.RUNNING}:
            raise ValueError("finish_member requires a terminal member status")
        stamp = _utc(now)
        with self.database.session() as session:
            member = session.scalar(
                select(CohortRunMember)
                .where(
                    CohortRunMember.run_id == run_id,
                    CohortRunMember.sleeve_id == sleeve_id,
                )
                .with_for_update()
            )
            if member is None:
                raise KeyError(f"{run_id}:{sleeve_id}")
            current = MemberRunStatus(member.status)
            if current is MemberRunStatus.COMPLETED and status is not MemberRunStatus.COMPLETED:
                raise SleeveRunConflictError("a completed member cannot be downgraded")
            member.status = status.value
            member.completed_at = stamp
            member.error = error.model_dump(mode="json") if error is not None else None
            run = session.scalar(
                select(CohortRun)
                .where(CohortRun.run_id == run_id)
                .with_for_update()
            )
            assert run is not None
            if status is MemberRunStatus.COMPLETED:
                run.completed_members = list(
                    session.scalars(
                        select(CohortRunMember.sleeve_id)
                        .where(
                            CohortRunMember.run_id == run_id,
                            CohortRunMember.status == MemberRunStatus.COMPLETED.value,
                        )
                        .order_by(CohortRunMember.ordinal)
                    )
                )
            if error is not None:
                errors = list(run.errors)
                item = error.model_dump(mode="json")
                if item not in errors:
                    errors.append(item)
                    run.errors = errors
        result = self.get(run_id)
        assert result is not None
        return result

    def finalize(self, run_id: str, *, now: datetime | None = None) -> SleeveRun:
        run = self.get(run_id)
        if run is None:
            raise KeyError(run_id)
        statuses = {member.status for member in run.members}
        if statuses == {MemberRunStatus.COMPLETED}:
            result = SleeveRunStatus.COMPLETED
        elif run.completed_members:
            result = SleeveRunStatus.PARTIAL
        elif MemberRunStatus.PENDING in statuses or MemberRunStatus.RUNNING in statuses:
            result = SleeveRunStatus.RUNNING
        else:
            result = SleeveRunStatus.FAILED
        return self.set_status(
            run_id,
            result,
            now=now,
            terminal=result
            in {
                SleeveRunStatus.COMPLETED,
                SleeveRunStatus.PARTIAL,
                SleeveRunStatus.FAILED,
            },
        )

    @staticmethod
    def _domain_run(row: CohortRun, members: Sequence[CohortRunMember]) -> SleeveRun:
        return SleeveRun(
            run_id=row.run_id,
            run_key=row.run_key,
            cohort_id=row.cohort_id,
            session_id=row.session_id,
            scheduled_for=row.scheduled_for,
            expected_members=tuple(row.expected_members),
            completed_members=tuple(row.completed_members),
            snapshot_id=row.snapshot_id,
            quote_snapshot_id=row.quote_snapshot_id,
            data_snapshot_ids=dict(row.data_snapshot_ids),
            started_at=row.started_at,
            completed_at=row.completed_at,
            status=SleeveRunStatus(row.status),
            errors=tuple(SleeveRunError.model_validate(error) for error in row.errors),
            members=tuple(
                SleeveRunMember(
                    sleeve_id=member.sleeve_id,
                    status=MemberRunStatus(member.status),
                    started_at=member.started_at,
                    completed_at=member.completed_at,
                    error=(
                        SleeveRunError.model_validate(member.error)
                        if member.error is not None
                        else None
                    ),
                )
                for member in members
            ),
        )

    @contextmanager
    def official_session(
        self,
        cohort_id: str,
        scheduled_for: date,
        *,
        owner_id: str | None = None,
    ) -> Iterator[bool]:
        """Own one official session, with a PostgreSQL advisory lock when available."""
        owner = owner_id or _owner_id()
        token = secrets.token_bytes(32)
        token_hash = hashlib.sha256(token).digest()
        acquired = False
        advisory_connection = None
        advisory_key = _advisory_key(cohort_id, scheduled_for)
        try:
            if self.database.dialect == "postgresql":
                advisory_connection = self.database.engine.connect()
                acquired = bool(
                    advisory_connection.execute(
                        text("SELECT pg_try_advisory_lock(:lock_key)"),
                        {"lock_key": advisory_key},
                    ).scalar_one()
                )
                advisory_connection.commit()
                if not acquired:
                    yield False
                    return
            with self.database.session() as session:
                row = session.scalar(
                    select(OfficialSessionLease)
                    .where(
                        OfficialSessionLease.cohort_id == cohort_id,
                        OfficialSessionLease.scheduled_for == scheduled_for,
                    )
                    .with_for_update()
                )
                now = datetime.now(UTC)
                if row is None:
                    session.add(
                        OfficialSessionLease(
                            cohort_id=cohort_id,
                            scheduled_for=scheduled_for,
                            owner_id=owner,
                            lease_token_hash=token_hash,
                            acquired_at=now,
                            expires_at=now + LEASE_DURATION,
                            released_at=None,
                        )
                    )
                    acquired = True
                elif (
                    self.database.dialect == "postgresql"
                    or row.released_at is not None
                    or _utc(row.expires_at) <= now
                ):
                    row.owner_id = owner
                    row.lease_token_hash = token_hash
                    row.acquired_at = now
                    row.expires_at = now + LEASE_DURATION
                    row.released_at = None
                    acquired = True
                else:
                    acquired = False
            yield acquired
        finally:
            if acquired:
                with self.database.session() as session:
                    row = session.scalar(
                        select(OfficialSessionLease)
                        .where(
                            OfficialSessionLease.cohort_id == cohort_id,
                            OfficialSessionLease.scheduled_for == scheduled_for,
                            OfficialSessionLease.lease_token_hash == token_hash,
                        )
                        .with_for_update()
                    )
                    if row is not None:
                        row.released_at = datetime.now(UTC)
            if advisory_connection is not None:
                try:
                    advisory_connection.execute(
                        text("SELECT pg_advisory_unlock(:lock_key)"),
                        {"lock_key": advisory_key},
                    )
                    advisory_connection.commit()
                finally:
                    advisory_connection.close()
