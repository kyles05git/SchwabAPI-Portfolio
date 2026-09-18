"""Non-mutating recovery inspection for one official cohort session.

This module answers a single question — *what actually happened to this cohort's
session, and what is the one safe thing to do next?* — and it answers it by reading
the durable record only.

It is deliberately incapable of repair. There is no code path here that deletes a
run, clears a lease, resets a member, replays an ambiguous member, rewrites a status,
creates an observation or fill, triggers the scheduler, or submits an order. Every
statement it issues is a ``SELECT``.

It also does not invent a state machine. The classification is derived from the
existing contracts — :class:`~schwab_trader.sleeve_runs.SleeveRunStatus`,
:class:`~schwab_trader.sleeve_runs.MemberRunStatus`, the
``official_session_leases`` row, and :func:`schwab_trader.scheduling.evaluate_session`
— so a verdict here can never disagree with what the orchestrator will do.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import inspect, select

from schwab_trader import market_calendar, scheduling
from schwab_trader.sleeve_runs import (
    AWAITING_REAUTH_CODE,
    WAIT_ERROR_CODES,
    MemberRunStatus,
    SleeveRunStatus,
)
from schwab_trader.storage.database import Database
from schwab_trader.storage.schema import (
    CohortRun,
    CohortRunMember,
    OfficialDailyObservation,
    OfficialSessionLease,
)

#: Bump only for a breaking change to the JSON document.
RECOVERY_CONTRACT_VERSION = 1


class RecoveryState(StrEnum):
    """What the durable record says about this cohort session."""

    ACTIVE = "active"
    """A run is executing and a live lease backs it. Nothing to recover."""

    AWAITING_DATA = "awaiting-data"
    """A considered verdict that required data was not ready. Retryable by design."""

    AWAITING_AUTH = "awaiting-auth"
    """Snapshot capture needs Schwab reauthentication. Retryable, but only by a human.

    Split from :attr:`AWAITING_DATA` because the two need opposite actions: one says
    wait, and this one says the session is being held open and will be lost at its
    deadline unless somebody logs in.
    """

    STALE_LEASE = "stale-lease"
    """An unreleased lease has passed its expiry. A runner died holding ownership."""

    INTERRUPTED = "interrupted"
    """A non-terminal run has members stuck mid-flight or explicitly interrupted."""

    PARTIAL = "partial"
    FAILED = "failed"
    LATE = "late"
    """No durable outcome exists and the session's decision time has passed."""

    MISSED = "missed"
    COMPLETED = "completed"
    SKIPPED_CLOSED_SESSION = "skipped-closed-session"
    NOT_DUE = "not-due"
    """The session's decision time has not been reached. Nothing is wrong."""

    UNKNOWN = "unknown"
    """The record could not be read. Never treated as any of the above."""


class ResumeSafety(StrEnum):
    SAFE = "safe"
    AMBIGUOUS = "ambiguous"
    FORBIDDEN = "forbidden"


#: Exit codes for ``storage recover``. Grouped by whether an operator must act.
RECOVERY_EXIT_CODES: dict[RecoveryState, int] = {
    RecoveryState.COMPLETED: 0,
    RecoveryState.ACTIVE: 0,
    RecoveryState.NOT_DUE: 0,
    RecoveryState.SKIPPED_CLOSED_SESSION: 0,
    RecoveryState.AWAITING_DATA: 1,
    RecoveryState.AWAITING_AUTH: 1,
    RecoveryState.STALE_LEASE: 1,
    RecoveryState.INTERRUPTED: 1,
    RecoveryState.PARTIAL: 1,
    RecoveryState.FAILED: 1,
    RecoveryState.LATE: 1,
    RecoveryState.MISSED: 1,
    RecoveryState.UNKNOWN: 3,
}


class LeaseFacts(BaseModel):
    """Sanitized lease evidence.

    The owner id and lease token hash are deliberately absent: neither is needed to
    decide what to do, and the token hash is the value that proves ownership.
    """

    model_config = ConfigDict(frozen=True)

    present: bool = False
    released: bool | None = None
    expired: bool | None = None
    acquired_at: datetime | None = None
    expires_at: datetime | None = None
    age_seconds: int | None = None


class MemberFacts(BaseModel):
    model_config = ConfigDict(frozen=True)

    sleeve_id: str
    status: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_code: str | None = None
    #: The durable official observation this member produced, if any. Presence of an
    #: OFFICIAL observation is the evidence that makes a member safely skippable.
    observation_key: str | None = None
    observation_status: str | None = None


class RecoveryReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    contract_version: int = RECOVERY_CONTRACT_VERSION
    generated_at: datetime
    state: RecoveryState
    exit_code: int
    resume_safety: ResumeSafety
    cohort_id: str
    scheduled_for: str
    session_id: str | None = None
    is_trading_day: bool
    schedule_verdict: str
    run_id: str | None = None
    run_key: str | None = None
    run_status: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    snapshot_id: str | None = None
    quote_snapshot_id: str | None = None
    data_snapshot_ids: tuple[str, ...] = ()
    expected_members: tuple[str, ...] = ()
    completed_members: tuple[str, ...] = ()
    missing_members: tuple[str, ...] = ()
    interrupted_members: tuple[str, ...] = ()
    failed_members: tuple[str, ...] = ()
    other_members: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    members: tuple[MemberFacts, ...] = ()
    official_observations: tuple[str, ...] = ()
    run_error_codes: tuple[str, ...] = ()
    lease: LeaseFacts = LeaseFacts()
    recommended_action: str

    def sanitized_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _lease_facts(
    database: Database, cohort_id: str, scheduled_for: date, now: datetime
) -> LeaseFacts:
    if "official_session_leases" not in set(inspect(database.engine).get_table_names()):
        return LeaseFacts()
    with database.session() as session:
        row = session.execute(
            select(
                OfficialSessionLease.acquired_at,
                OfficialSessionLease.expires_at,
                OfficialSessionLease.released_at,
            ).where(
                OfficialSessionLease.cohort_id == cohort_id,
                OfficialSessionLease.scheduled_for == scheduled_for,
            )
        ).first()
    if row is None:
        return LeaseFacts()
    acquired_at, expires_at, released_at = (_aware(value) for value in row)
    assert acquired_at is not None and expires_at is not None
    return LeaseFacts(
        present=True,
        released=released_at is not None,
        expired=expires_at <= now,
        acquired_at=acquired_at,
        expires_at=expires_at,
        age_seconds=int((now - acquired_at).total_seconds()),
    )


def _observation_index(
    database: Database, run_id: str
) -> dict[str, tuple[str, str]]:
    """``sleeve_id -> (observation_key, status)`` for this run's official record."""
    if "official_daily_observations" not in set(inspect(database.engine).get_table_names()):
        return {}
    with database.session() as session:
        rows = session.execute(
            select(
                OfficialDailyObservation.sleeve_id,
                OfficialDailyObservation.observation_key,
                OfficialDailyObservation.status,
            ).where(OfficialDailyObservation.run_id == run_id)
        ).all()
    return {sleeve_id: (key, status) for sleeve_id, key, status in rows}


def _current_wait_code(errors: object) -> str | None:
    """The wait code of the run's most recent non-terminal verdict, if it has one.

    The persisted trail is ordered by when each distinct verdict was *last* reached
    (see :func:`schwab_trader.sleeve_runs.record_run_error`), so the last row carrying a
    wait code is the wait the run is in now.
    """
    if not isinstance(errors, list):
        return None
    for error in reversed(errors):
        if not isinstance(error, dict):
            continue
        code = error.get("code")
        if isinstance(code, str) and code in WAIT_ERROR_CODES:
            return code
    return None


def _classify(
    *,
    run_status: SleeveRunStatus | None,
    member_statuses: dict[str, MemberRunStatus],
    lease: LeaseFacts,
    schedule: scheduling.RunDecision,
    wait_code: str | None = None,
) -> RecoveryState:
    """Derive one state from the durable record, never from an assumption.

    A stale lease is checked first because it blocks the next runner regardless of
    what the run row says, and because clearing one is the single most dangerous
    "obvious fix" an operator can reach for.
    """
    stale_lease = lease.present and lease.released is False and lease.expired is True

    if run_status is None:
        if stale_lease:
            return RecoveryState.STALE_LEASE
        if schedule.status is scheduling.RunStatus.SKIPPED_CLOSED_SESSION:
            return RecoveryState.SKIPPED_CLOSED_SESSION
        if schedule.status is scheduling.RunStatus.MISSED:
            return RecoveryState.MISSED
        if schedule.status is scheduling.RunStatus.PENDING:
            return RecoveryState.NOT_DUE
        # DUE and LATE are one recovery state: the decision time has passed and no
        # durable run exists. The operator action is identical for both.
        return RecoveryState.LATE

    if run_status is SleeveRunStatus.COMPLETED:
        return RecoveryState.COMPLETED
    if run_status is SleeveRunStatus.SKIPPED_CLOSED_SESSION:
        return RecoveryState.SKIPPED_CLOSED_SESSION
    if stale_lease:
        return RecoveryState.STALE_LEASE
    if run_status is SleeveRunStatus.PARTIAL:
        return RecoveryState.PARTIAL
    if run_status is SleeveRunStatus.FAILED:
        return RecoveryState.FAILED
    if run_status is SleeveRunStatus.MISSED:
        return RecoveryState.MISSED

    # Non-terminal from here: pending, running, or awaiting-data.
    mid_flight = {MemberRunStatus.RUNNING, MemberRunStatus.INTERRUPTED}
    if any(status in mid_flight for status in member_statuses.values()):
        # A live lease over a RUNNING run means a healthy runner still owns it.
        if run_status is SleeveRunStatus.RUNNING and lease.present and lease.released is False:
            return RecoveryState.ACTIVE
        return RecoveryState.INTERRUPTED
    if run_status is SleeveRunStatus.AWAITING_DATA:
        if wait_code == AWAITING_REAUTH_CODE:
            return RecoveryState.AWAITING_AUTH
        return RecoveryState.AWAITING_DATA
    if run_status is SleeveRunStatus.RUNNING:
        if lease.present and lease.released is False:
            return RecoveryState.ACTIVE
        return RecoveryState.INTERRUPTED
    if schedule.status is scheduling.RunStatus.PENDING:
        return RecoveryState.NOT_DUE
    if schedule.status is scheduling.RunStatus.MISSED:
        return RecoveryState.MISSED
    return RecoveryState.LATE


def _resume_safety(
    state: RecoveryState, member_statuses: dict[str, MemberRunStatus]
) -> ResumeSafety:
    """Whether re-running this session is safe, ambiguous, or forbidden.

    "Forbidden" mirrors the orchestrator, which refuses to re-execute a run whose
    status is in ``TERMINAL_RUN_STATUSES``. "Ambiguous" is the case the orchestrator
    resolves by *not* replaying: a member checkpointed as started with no proof that
    its official observation landed.
    """
    if state in {
        RecoveryState.COMPLETED,
        RecoveryState.PARTIAL,
        RecoveryState.FAILED,
        RecoveryState.MISSED,
        RecoveryState.SKIPPED_CLOSED_SESSION,
    }:
        return ResumeSafety.FORBIDDEN
    if state in {RecoveryState.ACTIVE, RecoveryState.STALE_LEASE, RecoveryState.UNKNOWN}:
        return ResumeSafety.AMBIGUOUS
    mid_flight = {MemberRunStatus.RUNNING, MemberRunStatus.INTERRUPTED}
    if any(status in mid_flight for status in member_statuses.values()):
        return ResumeSafety.AMBIGUOUS
    return ResumeSafety.SAFE


#: One action per state. Deterministic by construction: the mapping is total, and the
#: inspector never composes an action from two branches.
_ACTIONS: dict[RecoveryState, str] = {
    RecoveryState.ACTIVE: (
        "Do nothing. A runner holds a live lease for this session; wait for it to "
        "finish and re-inspect."
    ),
    RecoveryState.AWAITING_DATA: (
        "Re-run the official session for this cohort and date once the awaited data has "
        "landed. Nothing executed, so the retry is still all-or-nothing."
    ),
    RecoveryState.AWAITING_AUTH: (
        "Run 'python -m schwab_trader auth login' on the runner machine, then re-run the "
        "official session for this cohort and date before its deadline. Nothing executed, "
        "so the retry is still all-or-nothing."
    ),
    RecoveryState.STALE_LEASE: (
        "Do not clear the lease. Confirm no runner is alive on the scheduler machine; "
        "the next official run re-acquires an expired lease through the existing "
        "contract without any manual edit."
    ),
    RecoveryState.INTERRUPTED: (
        "Re-run the official session for this cohort and date. The recovery path marks "
        "ambiguous members INTERRUPTED rather than replaying them; do not reset a "
        "member by hand."
    ),
    RecoveryState.PARTIAL: (
        "Do not re-run. This session is terminal and its paper state is real. Review "
        "the interrupted and data-not-ready members, then record the outcome."
    ),
    RecoveryState.FAILED: (
        "Do not re-run. This session is terminal. Fix the recorded cause before the "
        "next scheduled session; the failed record stays as the durable truth."
    ),
    RecoveryState.LATE: (
        "Run the official session for this cohort and date from the designated "
        "scheduler machine, before the next session's deadline supersedes it."
    ),
    RecoveryState.MISSED: (
        "Do not backfill. A missed session is durable evidence; leave it and confirm "
        "the next scheduled session runs on time."
    ),
    RecoveryState.COMPLETED: "No action required. This session completed in full.",
    RecoveryState.SKIPPED_CLOSED_SESSION: (
        "No action required. This date is not a trading session."
    ),
    RecoveryState.NOT_DUE: (
        "No action required. The session's decision time has not been reached."
    ),
    RecoveryState.UNKNOWN: (
        "Stop. The durable record could not be read, so no recovery action is safe. "
        "Run `storage health` and resolve what it reports first."
    ),
}


def inspect_recovery(
    database: Database,
    cohort_id: str,
    scheduled_for: date,
    *,
    now: datetime | None = None,
    now_et: datetime | None = None,
) -> RecoveryReport:
    """Read one cohort session and report its state. Writes nothing, ever."""
    stamp = now or datetime.now(UTC)
    eastern = now_et or market_calendar.eastern_now()
    cohort = cohort_id.strip()
    if not cohort:
        raise ValueError("cohort_id must not be empty")

    schedule = scheduling.evaluate_session(cohort, scheduled_for, eastern)
    session_key = scheduling.run_key(cohort, schedule.session)

    lease = _lease_facts(database, cohort, scheduled_for, stamp)

    with database.session() as session:
        run_row = session.execute(
            select(
                CohortRun.run_id,
                CohortRun.run_key,
                CohortRun.session_id,
                CohortRun.status,
                CohortRun.started_at,
                CohortRun.completed_at,
                CohortRun.snapshot_id,
                CohortRun.quote_snapshot_id,
                CohortRun.data_snapshot_ids,
                CohortRun.expected_members,
                CohortRun.completed_members,
                CohortRun.errors,
            ).where(CohortRun.run_key == session_key)
        ).first()

    if run_row is None:
        state = _classify(
            run_status=None, member_statuses={}, lease=lease, schedule=schedule
        )
        return RecoveryReport(
            generated_at=stamp,
            state=state,
            exit_code=RECOVERY_EXIT_CODES[state],
            resume_safety=_resume_safety(state, {}),
            cohort_id=cohort,
            scheduled_for=scheduled_for.isoformat(),
            session_id=schedule.session.session_id,
            is_trading_day=schedule.session.is_trading_day,
            schedule_verdict=schedule.status.value,
            run_key=session_key,
            lease=lease,
            recommended_action=_ACTIONS[state],
        )

    (
        run_id,
        run_key,
        session_id,
        run_status_value,
        started_at,
        completed_at,
        snapshot_id,
        quote_snapshot_id,
        data_snapshot_ids,
        expected_members,
        completed_members,
        errors,
    ) = run_row

    with database.session() as session:
        member_rows = session.execute(
            select(
                CohortRunMember.sleeve_id,
                CohortRunMember.status,
                CohortRunMember.started_at,
                CohortRunMember.completed_at,
                CohortRunMember.error,
            )
            .where(CohortRunMember.run_id == run_id)
            .order_by(CohortRunMember.ordinal)
        ).all()

    observations = _observation_index(database, run_id)

    members: list[MemberFacts] = []
    member_statuses: dict[str, MemberRunStatus] = {}
    for sleeve_id, status_value, member_started, member_completed, error in member_rows:
        status = MemberRunStatus(status_value)
        member_statuses[sleeve_id] = status
        observation = observations.get(sleeve_id)
        members.append(
            MemberFacts(
                sleeve_id=sleeve_id,
                status=status.value,
                started_at=_aware(member_started),
                completed_at=_aware(member_completed),
                # Only the error *code* is carried. Messages are operator prose the
                # inspector has no reason to re-render, and prose is where an
                # unsanitized value would hide.
                error_code=(error or {}).get("code") if isinstance(error, dict) else None,
                observation_key=observation[0] if observation else None,
                observation_status=observation[1] if observation else None,
            )
        )

    def identities(*wanted: MemberRunStatus) -> tuple[str, ...]:
        return tuple(
            sleeve_id
            for sleeve_id, status in member_statuses.items()
            if status in wanted
        )

    accounted = {
        MemberRunStatus.COMPLETED,
        MemberRunStatus.PENDING,
        MemberRunStatus.RUNNING,
        MemberRunStatus.INTERRUPTED,
        MemberRunStatus.FAILED,
    }
    other: dict[str, tuple[str, ...]] = {}
    for status in MemberRunStatus:
        if status in accounted:
            continue
        found = identities(status)
        if found:
            other[status.value] = found

    run_status = SleeveRunStatus(run_status_value)
    state = _classify(
        run_status=run_status,
        member_statuses=member_statuses,
        lease=lease,
        schedule=schedule,
        wait_code=_current_wait_code(errors),
    )
    return RecoveryReport(
        generated_at=stamp,
        state=state,
        exit_code=RECOVERY_EXIT_CODES[state],
        resume_safety=_resume_safety(state, member_statuses),
        cohort_id=cohort,
        scheduled_for=scheduled_for.isoformat(),
        session_id=session_id,
        is_trading_day=schedule.session.is_trading_day,
        schedule_verdict=schedule.status.value,
        run_id=run_id,
        run_key=run_key,
        run_status=run_status.value,
        started_at=_aware(started_at),
        completed_at=_aware(completed_at),
        snapshot_id=snapshot_id,
        quote_snapshot_id=quote_snapshot_id,
        data_snapshot_ids=tuple(sorted(data_snapshot_ids or {})),
        expected_members=tuple(expected_members or ()),
        completed_members=tuple(completed_members or ()),
        missing_members=identities(MemberRunStatus.PENDING),
        interrupted_members=identities(MemberRunStatus.INTERRUPTED, MemberRunStatus.RUNNING),
        failed_members=identities(MemberRunStatus.FAILED),
        other_members=other,
        members=tuple(members),
        official_observations=tuple(sorted(key for key, _ in observations.values())),
        run_error_codes=tuple(
            dict.fromkeys(
                error["code"]
                for error in (errors or [])
                if isinstance(error, dict) and isinstance(error.get("code"), str)
            )
        ),
        lease=lease,
        recommended_action=_ACTIONS[state],
    )


__all__ = [
    "RECOVERY_CONTRACT_VERSION",
    "RECOVERY_EXIT_CODES",
    "LeaseFacts",
    "MemberFacts",
    "RecoveryReport",
    "RecoveryState",
    "ResumeSafety",
    "inspect_recovery",
]
