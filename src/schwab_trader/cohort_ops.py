"""Read-only operational health for one official paper cohort.

The daily question an operator actually has is *"did today's official run happen, and
if not, what do I do about it?"* Answering it from the raw stores is easy to get wrong
in exactly one way: a calendar date cannot tell a session that has not closed yet from
a session whose run is missing. At 09:28 ET the day's run is correctly absent; at 18:30
ET the same absence is an incident.

This module keeps those apart by reusing the canonical primitives rather than adding a
second market-hours or lateness calculation:

- :mod:`schwab_trader.market_calendar` owns the exchange calendar and early closes.
- :mod:`schwab_trader.scheduling` owns the session identity, the post-close due time,
  the grace period, and the missed deadline.

Everything here is pure. It performs no I/O, never reads the wall clock, and never
touches a broker path: the caller injects ``now_et`` (see
:func:`schwab_trader.market_calendar.eastern_now`) plus the durable runs and official
observations it already read, so the same inputs always produce the same verdict.

Output is deliberately identity-poor. The report names the cohort, the sleeves, and the
*kind* of storage in use — never a connection string, account identifier, token, or raw
provider payload. :func:`health_payload` is the stable JSON contract; the human
rendering lives in the CLI.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any

from schwab_trader import market_calendar as mc
from schwab_trader import scheduling
from schwab_trader.evaluation import ObservationStatus, OfficialDailyObservation
from schwab_trader.sleeve_runs import (
    AWAITING_REAUTH_CODE,
    WAIT_ERROR_CODES,
    SleeveRun,
    SleeveRunStatus,
    active_errors,
)

#: JSON contract version. Bump the minor part for additive fields, the major part for
#: a breaking change to an existing field's meaning or type.
PAYLOAD_SCHEMA = "cohort-health/1"

#: Process exit code: the cohort is on schedule and nothing is owed.
EXIT_OK = 0
#: Process exit code: recorded evidence is incomplete, late, or missing.
EXIT_ATTENTION = 1
#: Process exit code: the question could not be answered (fails closed).
EXIT_UNKNOWN = 2

#: Trading sessions scanned backwards for unresolved gaps. Comfortably more than the
#: 30-session operational review target, so nothing inside a review window is missed.
DEFAULT_HISTORY_SESSIONS = 40


class CohortState(StrEnum):
    """What the cohort's most relevant session is doing right now."""

    PRE_CLOSE = "pre-close"
    """Today is a trading session and its official close has not been reached."""

    DUE = "due"
    """The close has passed and the run is inside the scheduler's grace period."""

    LATE = "late"
    """Past the grace period with no durable outcome, but still runnable."""

    AWAITING_DATA = "awaiting-data"
    """The runner fired, refused to execute, and left the session retryable. Not a
    failure and not a gap: nothing ran, nothing was recorded, and the next invocation
    retries. It still needs attention, because a wait that never resolves becomes a
    missed session at the deadline.

    Covers both non-terminal waits. The run's error code says which — ``awaiting_data``
    resolves itself when the provider publishes, ``awaiting_reauthentication`` never
    resolves until an operator authenticates — and :func:`_next_action` gives the
    matching instruction."""

    MISSED = "missed"
    """Past the deadline; a fresher session supersedes this one. Evidence is lost."""

    COMPLETED = "completed"
    """Every expected member recorded an official observation."""

    PARTIAL = "partial"
    """Some members completed and some did not."""

    FAILED = "failed"
    """The run produced a durable outcome in which no member completed."""

    CLOSED_SESSION = "closed-session"
    """A weekend or exchange holiday; no official run was ever owed."""

    UNKNOWN = "unknown"
    """The cohort, its members, or a trading session could not be resolved."""


#: States where the operator has something to fix. These drive :data:`EXIT_ATTENTION`
#: and are the only states that make a *stale* session more interesting than today's.
ATTENTION_STATES = frozenset(
    {
        CohortState.DUE,
        CohortState.LATE,
        CohortState.AWAITING_DATA,
        CohortState.MISSED,
        CohortState.PARTIAL,
        CohortState.FAILED,
    }
)

#: States that mean the session already delivered a durable outcome.
_RESOLVED_RUN_STATES = {
    SleeveRunStatus.COMPLETED: CohortState.COMPLETED,
    SleeveRunStatus.PARTIAL: CohortState.PARTIAL,
    SleeveRunStatus.FAILED: CohortState.FAILED,
    SleeveRunStatus.MISSED: CohortState.MISSED,
    SleeveRunStatus.SKIPPED_CLOSED_SESSION: CohortState.CLOSED_SESSION,
}


@dataclass(frozen=True)
class SessionView:
    """The exchange session the verdict is about."""

    session_id: str
    session_date: date
    is_trading_day: bool
    is_early_close: bool
    close_et: datetime | None
    due_et: datetime | None

    @property
    def close_utc(self) -> datetime | None:
        return None if self.close_et is None else mc.eastern_to_utc(self.close_et)


@dataclass(frozen=True)
class ScheduleView:
    """The canonical scheduler's verdict and the window it was judged against."""

    verdict: scheduling.RunStatus
    reason: str
    grace_period: timedelta
    elapsed_since_due: timedelta | None = None
    grace_ends_et: datetime | None = None
    deadline_et: datetime | None = None


@dataclass(frozen=True)
class RunError:
    """One sanitized run or member failure, flattened for presentation."""

    code: str
    message: str
    member_id: str | None = None
    context: Mapping[str, object] | None = None


@dataclass(frozen=True)
class RunView:
    """The durable run recorded for a session, if the runner reached one."""

    run_id: str
    status: SleeveRunStatus
    scheduled_for: date
    started_at: datetime
    completed_at: datetime | None
    expected_members: tuple[str, ...]
    completed_members: tuple[str, ...]
    ran_late: bool
    errors: tuple[RunError, ...] = ()

    @property
    def expected_count(self) -> int:
        return len(self.expected_members)

    @property
    def completed_count(self) -> int:
        return len(self.completed_members)

    @property
    def missing_members(self) -> tuple[str, ...]:
        done = set(self.completed_members)
        return tuple(member for member in self.expected_members if member not in done)

    @property
    def awaiting_authentication(self) -> bool:
        """Whether this run is parked on the one wait that a retry cannot clear.

        Mirrors :func:`schwab_trader.sleeve_runs.awaits_reauthentication` over the
        flattened presentation errors: the last row carrying a wait code is the wait the
        run is currently in. False for every terminal run, so a ``failed`` session can
        never be presented as merely needing a login.
        """
        if self.status is not SleeveRunStatus.AWAITING_DATA:
            return False
        for error in reversed(self.errors):
            if error.code in WAIT_ERROR_CODES:
                return error.code == AWAITING_REAUTH_CODE
        return False


@dataclass(frozen=True)
class ObservationsView:
    """Official observations recorded for the assessed session, counted by status."""

    session_date: date
    official: int = 0
    partial: int = 0
    missing: int = 0
    sleeves: tuple[tuple[str, ObservationStatus], ...] = ()

    @property
    def total(self) -> int:
        return self.official + self.partial + self.missing


@dataclass(frozen=True)
class UnresolvedSession:
    """A session other than the focused one that never delivered complete evidence.

    The focused verdict deliberately covers only the last two due sessions, so the
    daily next step stays one action. These carry every *other* unresolved session in
    the scanned window — usually older ones, but also a newer session the focus rule
    passed over — so a gap is never silently forgotten.
    """

    session_date: date
    state: CohortState
    completed: int
    expected: int


@dataclass(frozen=True)
class CohortHealthReport:
    """One deterministic answer to "is the official cohort on schedule?"."""

    cohort_id: str
    now_et: datetime
    state: CohortState
    session: SessionView
    schedule: ScheduleView
    next_action: str
    storage_kind: str
    exchange: str = mc.EXCHANGE_MIC
    expected_members: tuple[str, ...] = ()
    run: RunView | None = None
    observations: ObservationsView | None = None
    latest_run: RunView | None = None
    next_session: SessionView | None = None
    unresolved_history: tuple[UnresolvedSession, ...] = ()
    """Other unresolved sessions in the scanned window, newest first, excluding the
    focused one."""

    history_scanned_from: date | None = None
    """Oldest session the history scan covered, so a caller knows the horizon."""

    @property
    def exit_code(self) -> int:
        """``0`` on schedule, ``1`` needs attention, ``2`` could not be determined."""
        if self.state is CohortState.UNKNOWN:
            return EXIT_UNKNOWN
        return EXIT_ATTENTION if self.state in ATTENTION_STATES else EXIT_OK

    @property
    def needs_attention(self) -> bool:
        return self.exit_code != EXIT_OK

    @property
    def expected_count(self) -> int:
        """Members owed for this session — the run's roster, else the cohort's."""
        return self.run.expected_count if self.run is not None else len(self.expected_members)

    @property
    def completed_count(self) -> int:
        return 0 if self.run is None else self.run.completed_count

    @property
    def has_unresolved_history(self) -> bool:
        return bool(self.unresolved_history)


def _session_view(
    session: scheduling.ExchangeSession,
    policy: scheduling.SchedulingPolicy,
) -> SessionView:
    due = None if session.close_et is None else session.close_et + policy.decision_delay
    return SessionView(
        session_id=session.session_id,
        session_date=session.session_date,
        is_trading_day=session.is_trading_day,
        is_early_close=session.is_early_close,
        close_et=session.close_et,
        due_et=due,
    )


def _run_view(
    run: SleeveRun,
    *,
    due_et: datetime | None,
    policy: scheduling.SchedulingPolicy,
) -> RunView:
    ran_late = False
    if run.completed_at is not None and due_et is not None:
        deadline = mc.eastern_to_utc(due_et) + policy.grace_period
        ran_late = run.completed_at > deadline
    return RunView(
        run_id=run.run_id,
        status=run.status,
        scheduled_for=run.scheduled_for,
        started_at=run.started_at,
        completed_at=run.completed_at,
        expected_members=run.expected_members,
        completed_members=run.completed_members,
        ran_late=ran_late,
        errors=tuple(
            RunError(
                code=error.code,
                message=error.message,
                member_id=error.member_id,
                context=dict(error.context),
            )
            for error in active_errors(run)
        ),
    )


def _observations_view(
    observations: Iterable[OfficialDailyObservation],
    session_date: date,
    cohort_id: str,
) -> ObservationsView:
    selected = sorted(
        (
            obs
            for obs in observations
            if obs.cohort_id == cohort_id and obs.session_date == session_date
        ),
        key=lambda obs: obs.sleeve_id,
    )
    counts = {status: 0 for status in ObservationStatus}
    for obs in selected:
        counts[obs.status] = counts.get(obs.status, 0) + 1
    return ObservationsView(
        session_date=session_date,
        official=counts.get(ObservationStatus.OFFICIAL, 0),
        partial=counts.get(ObservationStatus.PARTIAL, 0),
        missing=counts.get(ObservationStatus.MISSING, 0),
        sleeves=tuple((obs.sleeve_id, obs.status) for obs in selected),
    )


def _state_for(decision: scheduling.RunDecision, run: SleeveRun | None) -> CohortState:
    """Map a scheduler verdict plus the durable run onto one presented state.

    A recorded *terminal* outcome wins over the clock: a completed run is completed
    whether it finished on time or hours late. Only a run that never reached a durable
    outcome (or that does not exist) is judged by the scheduler.

    ``awaiting-data`` is the one recorded state the clock can still overrule. It means
    the runner deliberately did nothing and expects to be retried, so once the
    scheduler calls the session missed, missed is the honest answer even though the
    runner has not yet fired again to record it.
    """
    if decision.status is scheduling.RunStatus.SKIPPED_CLOSED_SESSION:
        return CohortState.CLOSED_SESSION
    if run is not None and (resolved := _RESOLVED_RUN_STATES.get(run.status)) is not None:
        return resolved
    if run is not None and run.status is SleeveRunStatus.AWAITING_DATA:
        return (
            CohortState.MISSED
            if decision.status is scheduling.RunStatus.MISSED
            else CohortState.AWAITING_DATA
        )
    match decision.status:
        case scheduling.RunStatus.PENDING:
            return CohortState.PRE_CLOSE
        case scheduling.RunStatus.DUE:
            return CohortState.DUE
        case scheduling.RunStatus.LATE:
            return CohortState.LATE
        case scheduling.RunStatus.MISSED:
            return CohortState.MISSED
        case scheduling.RunStatus.ALREADY_COMPLETED:
            return CohortState.COMPLETED
    return CohortState.UNKNOWN


def _unresolved_history(
    due_date: date,
    *,
    started: date,
    exclude: date | None,
    state_of: Callable[[date], CohortState],
    runs_by_date: Mapping[date, SleeveRun],
    expected: int,
    limit: int,
) -> tuple[tuple[UnresolvedSession, ...], date | None]:
    """Scan back over recent trading sessions for gaps outside the focus window.

    Walks *sessions*, not runs, so a day the scheduler never touched at all — which
    leaves no record to iterate — is still found. Bounded by ``limit`` sessions and by
    the cohort's start, and it stops early once it walks past the start.
    """
    found: list[UnresolvedSession] = []
    day = due_date
    oldest: date | None = None
    for _ in range(max(limit, 0)):
        if day < started:
            break
        oldest = day
        if day != exclude and (state := state_of(day)) in ATTENTION_STATES:
            run = runs_by_date.get(day)
            found.append(
                UnresolvedSession(
                    session_date=day,
                    state=state,
                    completed=0 if run is None else len(run.completed_members),
                    expected=expected if run is None else len(run.expected_members),
                )
            )
        previous = mc.previous_trading_day(day)
        if previous >= day:  # defensive: never loop forever on a calendar surprise
            break
        day = previous
    return tuple(found), oldest


def _next_action(state: CohortState, report_session: SessionView, run: RunView | None) -> str:
    """The single concrete thing to do next. Never more than one instruction."""
    session = report_session.session_date.isoformat()
    match state:
        case CohortState.PRE_CLOSE:
            close_et = report_session.close_et
            close = "" if close_et is None else f" ({close_et:%H:%M ET})"
            early = " early-close" if report_session.is_early_close else ""
            return (
                f"Nothing to do. The {session}{early} session closes{close}; "
                "the run is scheduled after it."
            )
        case CohortState.DUE:
            return (
                "Nothing to do yet. The scheduler's next invocation should produce the "
                f"{session} run; it is still inside the grace period."
            )
        case CohortState.LATE:
            return (
                "Run the cohort now on the writer machine: "
                f"'python scripts/run_sleeves.py --cohort <cohort>'. The {session} session is "
                "past its grace period but can still be recorded."
            )
        case CohortState.AWAITING_DATA:
            waiting = (
                [error for error in run.errors if error.code in WAIT_ERROR_CODES]
                if run is not None
                else []
            )
            detail = f" {waiting[-1].message}" if waiting else ""
            if run is not None and run.awaiting_authentication:
                # The one waiting state that never resolves on its own. Saying "nothing
                # to do yet" here is what let the 2026-07-31 session sit unrecorded.
                return (
                    "Authenticate on the runner machine: 'python -m schwab_trader auth login'. "
                    f"The {session} session executed nobody and recorded nothing, and it stays "
                    f"recordable until its deadline once the token works again.{detail}"
                )
            return (
                f"Nothing to do yet. The {session} session is waiting for required data and "
                "no member executed, so nothing was recorded and the next scheduled "
                f"invocation retries it.{detail}"
            )
        case CohortState.MISSED:
            return (
                f"The {session} session cannot be recovered — a fresher session supersedes it. "
                "Investigate why the scheduler did not fire, then confirm the next session runs."
            )
        case CohortState.PARTIAL:
            missing = ", ".join(run.missing_members) if run is not None else "some members"
            return (
                f"Review the {session} run errors for {missing}. Partial evidence is durable and "
                "is not replayed; decide whether the session is usable before the review."
            )
        case CohortState.FAILED:
            return (
                f"Investigate the {session} run errors. No member completed, so the session "
                "recorded no usable evidence and will not be retried automatically."
            )
        case CohortState.CLOSED_SESSION:
            return f"Nothing to do. {session} is not an exchange trading session."
        case CohortState.COMPLETED:
            late = (
                " It finished after the grace period; check scheduler timing."
                if run is not None and run.ran_late
                else ""
            )
            return f"Nothing to do. The {session} session recorded complete evidence.{late}"
    return "Resolve the cohort configuration before relying on scheduled runs."


def assess_cohort(
    cohort_id: str,
    *,
    now_et: datetime,
    runs: Sequence[SleeveRun] = (),
    observations: Iterable[OfficialDailyObservation] = (),
    expected_members: Sequence[str] = (),
    storage_kind: str = "local-sqlite",
    session_date: date | None = None,
    cohort_start: date | None = None,
    history_sessions: int = DEFAULT_HISTORY_SESSIONS,
    policy: scheduling.SchedulingPolicy = scheduling.DEFAULT_POLICY,
    exchange: str = mc.EXCHANGE_MIC,
) -> CohortHealthReport:
    """Assess one cohort's scheduled-run health at a fixed Eastern instant.

    The session under judgement is the most recent one whose outcome is still owed. If
    every due session is resolved and today is a trading day before its close, the
    report is about *today* (:attr:`CohortState.PRE_CLOSE`) — the reassuring answer,
    not a manufactured gap. An unresolved earlier session always wins, because that is
    the one an operator has to act on.

    Pass ``session_date`` to pin the report to one specific session instead. The runner
    needs that after finishing a catch-up run: the session it just recorded is the one
    the alert must describe, even when the clock has already moved on to another.

    ``cohort_start`` bounds how far back a gap may be claimed and should be the
    cohort's *persisted* start session: a session that predates the cohort was never
    owed, but a first day the scheduler missed outright leaves no run to infer from,
    so inferring the boundary from recorded runs alone would hide exactly that case.

    Sessions older than the focus window are still reported, in
    :attr:`CohortHealthReport.unresolved_history`, so a gap that scrolls out of focus
    stays visible. It does not change the exit code: the daily signal stays about
    today's action, and a permanent historical gap must not make every future check
    red. ``history_sessions`` bounds that scan.

    ``runs`` and ``observations`` are the caller's already-read durable records; this
    function performs no I/O.
    """
    cohort = cohort_id.strip()
    if not cohort:
        raise ValueError("cohort_id must not be empty")

    cohort_runs = [run for run in runs if run.cohort_id == cohort]
    runs_by_date = {run.scheduled_for: run for run in cohort_runs}
    completed_keys = frozenset(
        run.run_key for run in cohort_runs if run.status is SleeveRunStatus.COMPLETED
    )
    latest_run = max(cohort_runs, key=lambda run: (run.scheduled_for, run.started_at), default=None)

    today = now_et.date()
    today_session = scheduling.session_for_date(today, exchange)
    pre_close_date: date | None = None
    if today_session.is_trading_day:
        assert today_session.close_et is not None
        if now_et < today_session.close_et + policy.decision_delay:
            pre_close_date = today

    due_date = scheduling.most_recent_due_session_date(now_et, policy, exchange)

    def state_of(day: date) -> CohortState:
        return _state_for(
            scheduling.evaluate_session(cohort, day, now_et, completed_keys, policy, exchange),
            runs_by_date.get(day),
        )

    # A session that predates the cohort was never owed, so it can never be a gap.
    # ``cohort_start`` should be the cohort's *persisted* start session: deriving the
    # boundary from the earliest recorded run would hide a first day the scheduler
    # missed entirely, because a session with no run leaves nothing to derive from.
    # The run-derived value is only a last resort, and with no runs and no declared
    # start the horizon collapses to today, so a cohort registered this morning is
    # not reported as having missed yesterday.
    started = cohort_start or min(
        (run.scheduled_for for run in cohort_runs),
        default=pre_close_date or due_date or now_et.date(),
    )

    history: tuple[UnresolvedSession, ...] = ()
    scanned_from: date | None = None
    focus_date = session_date
    if focus_date is None and due_date is not None:
        # Look back one extra session so a gap is still visible on the day it becomes
        # unrecoverable: at Wednesday's close, Tuesday's missed run is the operator's
        # real problem and Wednesday merely being due is a non-action. Oldest-first
        # keeps the choice deterministic.
        window = [day for day in (mc.previous_trading_day(due_date), due_date) if day >= started]
        unresolved = [day for day in window if state_of(day) in ATTENTION_STATES]
        # With everything owed resolved, report today's still-open session so a
        # healthy cohort reads as "pending", not as an absence of news.
        settled = pre_close_date or due_date
        focus_date = unresolved[0] if unresolved else settled
    elif focus_date is None:
        focus_date = pre_close_date

    if due_date is not None:
        history, scanned_from = _unresolved_history(
            due_date,
            started=started,
            exclude=focus_date,
            state_of=state_of,
            runs_by_date=runs_by_date,
            expected=len(expected_members),
            limit=history_sessions,
        )

    if focus_date is None:
        empty = scheduling.session_for_date(today, exchange)
        return CohortHealthReport(
            cohort_id=cohort,
            now_et=now_et,
            state=CohortState.UNKNOWN,
            session=_session_view(empty, policy),
            schedule=ScheduleView(
                verdict=scheduling.RunStatus.SKIPPED_CLOSED_SESSION,
                reason=(
                    "No exchange trading session was found within "
                    f"{policy.lookback_days} days of {now_et.isoformat()}."
                ),
                grace_period=policy.grace_period,
            ),
            next_action="Check the exchange calendar and the injected Eastern clock.",
            storage_kind=storage_kind,
            exchange=exchange,
            expected_members=tuple(expected_members),
            latest_run=(
                None if latest_run is None else _run_view(latest_run, due_et=None, policy=policy)
            ),
            unresolved_history=history,
            history_scanned_from=scanned_from,
        )

    decision = scheduling.evaluate_session(
        cohort, focus_date, now_et, completed_keys, policy, exchange
    )
    session_view = _session_view(decision.session, policy)
    run = runs_by_date.get(focus_date)
    state = _state_for(decision, run)
    if state is not CohortState.CLOSED_SESSION and not expected_members and run is None:
        # Nothing declares who is owed, so no count can be trusted. Fail closed.
        state = CohortState.UNKNOWN

    run_view = None if run is None else _run_view(run, due_et=session_view.due_et, policy=policy)
    next_session = scheduling.session_for_date(mc.next_trading_day(focus_date), exchange)

    return CohortHealthReport(
        cohort_id=cohort,
        now_et=now_et,
        state=state,
        session=session_view,
        schedule=ScheduleView(
            verdict=decision.status,
            reason=decision.reason,
            grace_period=policy.grace_period,
            elapsed_since_due=decision.late_by,
            grace_ends_et=(
                None if decision.due_et is None else decision.due_et + policy.grace_period
            ),
            deadline_et=scheduling.execution_deadline_et(
                focus_date, policy=policy, exchange=exchange
            ),
        ),
        next_action=(
            "Register the cohort's sleeves before relying on scheduled runs."
            if state is CohortState.UNKNOWN
            else _next_action(state, session_view, run_view)
        ),
        storage_kind=storage_kind,
        exchange=exchange,
        expected_members=tuple(expected_members),
        run=run_view,
        observations=_observations_view(observations, focus_date, cohort),
        latest_run=(
            None if latest_run is None else _run_view(latest_run, due_et=None, policy=policy)
        ),
        next_session=_session_view(next_session, policy),
        unresolved_history=history,
        history_scanned_from=scanned_from,
    )


def _iso(value: datetime | date | None) -> str | None:
    return None if value is None else value.isoformat()


def _seconds(value: timedelta | None) -> float | None:
    return None if value is None else value.total_seconds()


def _session_payload(view: SessionView | None) -> dict[str, Any] | None:
    if view is None:
        return None
    return {
        "session_id": view.session_id,
        "date": view.session_date.isoformat(),
        "is_trading_day": view.is_trading_day,
        "is_early_close": view.is_early_close,
        "close_et": _iso(view.close_et),
        "close_utc": _iso(view.close_utc),
        "due_et": _iso(view.due_et),
    }


def _run_payload(view: RunView | None) -> dict[str, Any] | None:
    if view is None:
        return None
    return {
        "run_id": view.run_id,
        "status": view.status.value,
        "scheduled_for": view.scheduled_for.isoformat(),
        "started_at": _iso(view.started_at),
        "completed_at": _iso(view.completed_at),
        "expected_members": list(view.expected_members),
        "completed_members": list(view.completed_members),
        "missing_members": list(view.missing_members),
        "expected_count": view.expected_count,
        "completed_count": view.completed_count,
        "ran_late": view.ran_late,
        "errors": [
            {
                "code": error.code,
                "message": error.message,
                "member_id": error.member_id,
                "context": dict(error.context or {}),
            }
            for error in view.errors
        ],
    }


def health_payload(report: CohortHealthReport) -> dict[str, Any]:
    """The stable JSON contract for :class:`CohortHealthReport`.

    Every value is a JSON primitive, so ``json.dumps`` needs no custom encoder. The
    payload carries cohort, sleeve, and session identity only — no connection string,
    account identifier, token, or raw provider payload ever reaches it.
    """
    observations = report.observations
    return {
        "schema": PAYLOAD_SCHEMA,
        "cohort_id": report.cohort_id,
        "exchange": report.exchange,
        "now_et": report.now_et.isoformat(),
        "state": report.state.value,
        "exit_code": report.exit_code,
        "needs_attention": report.needs_attention,
        "storage": report.storage_kind,
        "session": _session_payload(report.session),
        "next_session": _session_payload(report.next_session),
        "schedule": {
            "verdict": report.schedule.verdict.value,
            "reason": report.schedule.reason,
            "grace_seconds": report.schedule.grace_period.total_seconds(),
            "elapsed_since_due_seconds": _seconds(report.schedule.elapsed_since_due),
            "grace_ends_et": _iso(report.schedule.grace_ends_et),
            "retry_deadline_et": _iso(report.schedule.deadline_et),
        },
        "members": {
            "expected": report.expected_count,
            "completed": report.completed_count,
            "cohort_roster": list(report.expected_members),
        },
        "run": _run_payload(report.run),
        "latest_run": _run_payload(report.latest_run),
        "observations": (
            None
            if observations is None
            else {
                "session_date": observations.session_date.isoformat(),
                "official": observations.official,
                "partial": observations.partial,
                "missing": observations.missing,
                "total": observations.total,
                "sleeves": [
                    {"sleeve_id": sleeve_id, "status": status.value}
                    for sleeve_id, status in observations.sleeves
                ],
            }
        ),
        "history": {
            # Reported for visibility, deliberately not folded into exit_code: a
            # permanent past gap must not make every future daily check red.
            "scanned_from": _iso(report.history_scanned_from),
            "unresolved": len(report.unresolved_history),
            "sessions": [
                {
                    "date": item.session_date.isoformat(),
                    "state": item.state.value,
                    "completed": item.completed,
                    "expected": item.expected,
                }
                for item in report.unresolved_history
            ],
        },
        "next_action": report.next_action,
    }


__all__ = [
    "ATTENTION_STATES",
    "DEFAULT_HISTORY_SESSIONS",
    "EXIT_ATTENTION",
    "EXIT_OK",
    "EXIT_UNKNOWN",
    "PAYLOAD_SCHEMA",
    "CohortHealthReport",
    "CohortState",
    "ObservationsView",
    "RunError",
    "RunView",
    "ScheduleView",
    "SessionView",
    "UnresolvedSession",
    "assess_cohort",
    "health_payload",
]
