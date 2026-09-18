"""Phase and progress assessment for an official paper cohort (read-only).

The operational gate in :mod:`schwab_trader.operational_gate` answers one question:
*may this cohort's evidence be treated as operationally useful?* It fails closed, so
before the experiment has produced anything it reports ``fail``. That is the correct
authorization answer and it is deliberately not relaxed here.

It is the wrong thing to *show* an operator, though. A cohort whose first session is
still in the future has not failed; it has not started. This module layers a separate
presentation contract beside the gate:

- :class:`CohortPhase` says where the experiment is in its life cycle.
- :class:`SessionTimingState` says where the *clock* is relative to the next session's
  official close, which is a different question from how the experiment is going.
- :class:`RulePresentation` says whether a gate rule is genuinely actionable, merely
  awaiting evidence, or healthy.
- :class:`CohortPhaseAssessment` carries the progress numbers and one next action.

A calendar date cannot answer "has this session happened?". A session's official
decision instant is the exchange close, so on the morning of its own start date a
correctly pending run is *upcoming*, not missing. Callers therefore inject an explicit
Eastern ``now_et`` and :mod:`schwab_trader.evidence_timing` splits the runs into
recorded evidence and sessions that are legitimately still outstanding. The older
``as_of`` date parameter is kept for existing callers, but it can only compare
calendar dates and so cannot distinguish a pre-close session from a missed one.

Nothing here authorizes anything. It never inspects an order path, and the gate's
``status``, ``operationally_useful``, ``investment_alpha_assessed``, and
``live_trading_authorized`` values pass through untouched.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from schwab_trader import evidence_timing, scheduling
from schwab_trader.evaluation import ObservationStatus, OfficialDailyObservation
from schwab_trader.operational_gate import (
    GateRule,
    GateStatus,
    OperationalGateResult,
    RuleAssessment,
)
from schwab_trader.sleeve_runs import SleeveRun, SleeveRunStatus, active_errors

#: Due sessions required before the formal 30-session operational review.
DEFAULT_REVIEW_TARGET = 30

#: Run states that mean a *due* session did not deliver complete evidence.
_UNHEALTHY_RUN_STATUSES = frozenset(
    {
        SleeveRunStatus.PARTIAL,
        SleeveRunStatus.FAILED,
        SleeveRunStatus.MISSED,
    }
)

#: Run states that mean a due session produced a full, usable result.
_COMPLETE_RUN_STATUSES = frozenset({SleeveRunStatus.COMPLETED})

#: Rules whose failure always describes corrupted or contradictory recorded evidence
#: rather than evidence that has simply not been produced yet.
INTEGRITY_RULES = frozenset(
    {
        GateRule.DUPLICATE_OBSERVATIONS,
        GateRule.ACCOUNTING_STATES,
        GateRule.DATA_READINESS,
        GateRule.REPRODUCIBILITY,
    }
)


class CohortPhase(StrEnum):
    """Where an official cohort is in its life cycle, for presentation only."""

    SCHEDULED = "scheduled"
    """The start session has not occurred; no due session exists yet."""

    COLLECTING = "collecting"
    """The experiment is running and has fewer than the review target of due sessions."""

    REVIEW_READY = "review-ready"
    """Enough due sessions exist that the formal operational review is owed."""

    PASSED = "passed"
    """The operational gate passed on due evidence."""

    ATTENTION_NEEDED = "attention-needed"
    """Recorded evidence contains a real defect that an operator should act on."""

    FAILED = "failed"
    """The review came due and genuine due evidence failed it."""


class SessionTimingState(StrEnum):
    """Where the clock stands relative to the cohort's next owed session.

    This is deliberately independent of :class:`CohortPhase`: a healthy collecting
    cohort can be *awaiting execution* right after a close, and an unstarted one can be
    *upcoming* all morning. Neither is a defect.
    """

    NO_SESSION_SCHEDULED = "no-session-scheduled"
    """No run is persisted for this cohort at all."""

    UPCOMING = "upcoming"
    """The next session's official close has not happened yet."""

    AWAITING_EXECUTION = "awaiting-execution"
    """A session has closed and the scheduler is still inside its grace period."""

    AWAITING_PROVIDER_DATA = "awaiting-provider-data"
    """The runner executed no member because provider evidence is incomplete; retries
    remain authorized by the scheduler until its actual catch-up deadline."""

    OVERDUE = "overdue"
    """A normal run passed grace, or a provider wait passed the actual deadline."""

    SETTLED = "settled"
    """Every session that has closed produced a durable run outcome."""


#: Timing states that mean an owed session has not delivered its outcome yet, so
#: absent snapshots, observations, and comparison output are expected rather than wrong.
_OUTSTANDING_TIMINGS = frozenset(
    {
        SessionTimingState.UPCOMING,
        SessionTimingState.AWAITING_EXECUTION,
        SessionTimingState.AWAITING_PROVIDER_DATA,
    }
)


class RulePresentation(StrEnum):
    """How an operator should read one gate rule right now."""

    HEALTHY = "healthy"
    AWAITING_EVIDENCE = "awaiting-evidence"
    NEEDS_ATTENTION = "needs-attention"


#: Phases in which the review is owed, so unrecorded evidence becomes actionable.
_REVIEW_DUE_PHASES = frozenset({CohortPhase.REVIEW_READY, CohortPhase.PASSED, CohortPhase.FAILED})


@dataclass(frozen=True)
class RunSummary:
    """The minimum an operator needs about one due run."""

    run_id: str
    scheduled_for: date
    status: str
    completed_members: int
    expected_members: int
    error_summary: str | None = None


@dataclass(frozen=True)
class CohortPhaseAssessment:
    """Deterministic phase, progress, and next action for one official cohort."""

    cohort_id: str
    phase: CohortPhase
    as_of: date
    start_session: date | None
    started: bool
    review_target: int
    due_sessions: int
    completed_due_sessions: int
    total_scheduled_sessions: int
    latest_due_run: RunSummary | None
    next_scheduled_session: date | None
    latest_observation_date: date | None
    official_observations: int
    completion_reliability: float | None
    next_action: str
    headline: str
    integrity_alerts: tuple[str, ...] = ()
    timing_state: SessionTimingState = SessionTimingState.NO_SESSION_SCHEDULED
    now_et: datetime | None = None
    """The injected Eastern wall clock, or ``None`` on the legacy date-only path."""

    evidence_cutoff: date | None = None
    """Latest exchange session whose official close has already occurred. Sessions
    after it cannot have produced evidence yet."""

    awaiting_execution_session: date | None = None
    """A closed session still inside the scheduler's grace period, if any."""

    awaiting_provider_session: date | None = None
    """A closed session whose incomplete provider evidence is safely retryable."""

    overdue_sessions: tuple[date, ...] = ()
    """Sessions whose ordinary grace or provider-specific hard deadline expired."""

    @property
    def awaiting_evidence(self) -> bool:
        """Whether the cohort is simply waiting on a session rather than failing."""

        return self.timing_state in _OUTSTANDING_TIMINGS

    @property
    def sessions_remaining(self) -> int:
        """Due sessions still needed before the operational review comes due."""

        return max(0, self.review_target - self.completed_due_sessions)

    @property
    def progress_ratio(self) -> float:
        """Completed due sessions as a fraction of the review target, capped at one."""

        if self.review_target <= 0:
            return 0.0
        return min(1.0, self.completed_due_sessions / self.review_target)


def presentation_for(rule: RuleAssessment, phase: CohortPhase) -> RulePresentation:
    """Classify one gate rule for display without changing its status.

    A rule that passed on real evidence is healthy. A rule that cannot be evaluated yet
    is *awaiting evidence* while the experiment is still scheduled or collecting, and
    becomes actionable once the review is due. Everything else is a real failure.
    """

    review_due = phase in _REVIEW_DUE_PHASES
    if rule.awaiting_evidence:
        if not review_due:
            return RulePresentation.AWAITING_EVIDENCE
        if rule.status is GateStatus.PASS:
            return RulePresentation.HEALTHY
        return RulePresentation.NEEDS_ATTENTION
    if rule.status is GateStatus.PASS:
        return RulePresentation.HEALTHY
    if rule.status is GateStatus.INSUFFICIENT_HISTORY:
        return RulePresentation.AWAITING_EVIDENCE
    return RulePresentation.NEEDS_ATTENTION


def _integrity_alerts(
    gate: OperationalGateResult | None,
    due_runs: Sequence[SleeveRun],
    overdue: Sequence[evidence_timing.RunTimingAssessment] = (),
) -> tuple[str, ...]:
    """Defects worth surfacing immediately, regardless of how young the cohort is."""

    alerts: list[str] = []
    # An overdue run leads, because every downstream gate complaint about it (absent
    # snapshots, unexplained slots) is a symptom of the run never having executed.
    for item in overdue:
        late = (
            ""
            if item.elapsed_since_due is None
            else f" It is {scheduling.human_duration(item.elapsed_since_due)} late."
        )
        alerts.append(
            f"Run {item.scheduled_for.isoformat()} never executed and is past its "
            f"execution grace period.{late}"
        )
    if gate is not None:
        for rule in gate.rules:
            if (
                rule.rule in INTEGRITY_RULES
                and rule.status is GateStatus.FAIL
                and not rule.awaiting_evidence
            ):
                alerts.append(f"{rule.label}: {rule.reason}")
    for run in due_runs:
        missing = len(run.expected_members) - len(run.completed_members)
        if run.status in _UNHEALTHY_RUN_STATUSES:
            alerts.append(
                f"Run {run.scheduled_for.isoformat()} is {run.status.value} "
                f"with {missing} of {len(run.expected_members)} members incomplete."
            )
        elif run.status in _COMPLETE_RUN_STATUSES and missing > 0:
            # A completed run that is missing members is contradictory recorded
            # evidence, not a session that simply has not happened yet.
            alerts.append(
                f"Run {run.scheduled_for.isoformat()} is recorded complete but "
                f"{missing} of {len(run.expected_members)} members never finished."
            )
    return tuple(dict.fromkeys(alerts))


def _long_date(value: date) -> str:
    """``July 27, 2026`` without platform-specific ``strftime`` padding flags."""

    return f"{value:%B} {value.day}, {value.year}"


def _run_summary(run: SleeveRun) -> RunSummary:
    live = active_errors(run)
    error = live[0].message if live else None
    if error is None:
        member_error = next(
            (member.error.message for member in run.members if member.error is not None),
            None,
        )
        error = member_error
    return RunSummary(
        run_id=run.run_id,
        scheduled_for=run.scheduled_for,
        status=run.status.value,
        completed_members=len(run.completed_members),
        expected_members=len(run.expected_members),
        error_summary=error,
    )


def _next_action(
    phase: CohortPhase,
    *,
    start_session: date | None,
    next_scheduled_session: date | None,
    sessions_remaining: int,
    latest_due_run: RunSummary | None,
    integrity_alerts: Sequence[str],
    timing: SessionTimingState = SessionTimingState.NO_SESSION_SCHEDULED,
    awaiting_execution_session: date | None = None,
    awaiting_provider_session: date | None = None,
) -> str:
    if integrity_alerts:
        return f"Investigate: {integrity_alerts[0]}"
    if timing is SessionTimingState.AWAITING_PROVIDER_DATA and awaiting_provider_session:
        return f"Await provider data for the {_long_date(awaiting_provider_session)} session."
    if timing is SessionTimingState.AWAITING_EXECUTION and awaiting_execution_session:
        return f"Run today's cohort for the {_long_date(awaiting_execution_session)} session."
    if phase is CohortPhase.SCHEDULED:
        session = next_scheduled_session or start_session
        if session is None:
            return "Schedule the first official cohort session."
        return f"Run the cohort after the {_long_date(session)} market close."
    if phase is CohortPhase.COLLECTING:
        if latest_due_run is not None and latest_due_run.status not in {
            status.value for status in _COMPLETE_RUN_STATUSES
        }:
            return (
                f"Resolve the {_long_date(latest_due_run.scheduled_for)} run, then keep collecting."
            )
        plural = "session" if sessions_remaining == 1 else "sessions"
        return f"Keep running the cohort; {sessions_remaining} more due {plural} before review."
    if phase is CohortPhase.REVIEW_READY:
        return "Record the accounting review and operator decisions for the 30-session review."
    if phase is CohortPhase.PASSED:
        return "Review the passed evidence; this does not authorize live trading."
    if phase is CohortPhase.FAILED:
        return "Address the failed operational-review requirements before collecting further."
    return "Investigate the recorded cohort evidence."


#: Phases in which the *clock* is still the most useful thing to say about the cohort.
_TIMING_HEADLINE_PHASES = frozenset(
    {CohortPhase.SCHEDULED, CohortPhase.COLLECTING, CohortPhase.ATTENTION_NEEDED}
)


def _headline(
    phase: CohortPhase,
    start_session: date | None,
    started: bool,
    timing: SessionTimingState = SessionTimingState.NO_SESSION_SCHEDULED,
) -> str:
    if phase in _TIMING_HEADLINE_PHASES:
        if timing is SessionTimingState.OVERDUE:
            return "Run overdue"
        if timing is SessionTimingState.AWAITING_PROVIDER_DATA:
            return "Awaiting provider data — retryable"
        if timing is SessionTimingState.AWAITING_EXECUTION:
            return "Run due — awaiting execution"
    if phase is CohortPhase.SCHEDULED:
        if start_session is None:
            return "Not scheduled"
        return f"Starts {_long_date(start_session)}"
    if phase is CohortPhase.COLLECTING:
        return "Collecting official observations"
    if phase is CohortPhase.REVIEW_READY:
        return "30-session review is due"
    if phase is CohortPhase.PASSED:
        return "Operational review passed"
    if phase is CohortPhase.FAILED:
        return "Operational review failed"
    return (
        "Collecting, but recorded evidence needs attention"
        if started
        else "Recorded evidence needs attention before the first session"
    )


def _timing_state(
    window: evidence_timing.EvidenceWindow | None,
    *,
    upcoming: Sequence[SleeveRun],
    has_runs: bool,
) -> SessionTimingState:
    """Reduce the run partition to one state, worst-outstanding first.

    Without an injected Eastern clock only the coarse date comparison is available, so
    the legacy path reports :attr:`SessionTimingState.UPCOMING` or
    :attr:`SessionTimingState.SETTLED` and never claims a session is overdue.
    """

    if not has_runs:
        return SessionTimingState.NO_SESSION_SCHEDULED
    if window is None:
        return SessionTimingState.UPCOMING if upcoming else SessionTimingState.SETTLED
    if window.overdue:
        return SessionTimingState.OVERDUE
    if window.awaiting_provider_data:
        return SessionTimingState.AWAITING_PROVIDER_DATA
    if window.awaiting_execution:
        return SessionTimingState.AWAITING_EXECUTION
    if window.upcoming:
        return SessionTimingState.UPCOMING
    return SessionTimingState.SETTLED


def assess_cohort_phase(
    *,
    cohort_id: str,
    runs: Iterable[SleeveRun],
    observations: Iterable[OfficialDailyObservation],
    gate: OperationalGateResult | None,
    as_of: date | None = None,
    now_et: datetime | None = None,
    start_session: date | None = None,
    review_target: int = DEFAULT_REVIEW_TARGET,
    policy: scheduling.SchedulingPolicy = scheduling.DEFAULT_POLICY,
) -> CohortPhaseAssessment:
    """Assess one cohort's phase and progress against an explicitly injected clock.

    Pass ``now_et``: naive Eastern wall-clock time (see
    :func:`schwab_trader.market_calendar.eastern_now`). A session is evidence only once
    its exchange close has passed and it has either executed or become genuinely
    overdue. An awaiting-data run stays outside evidence through the scheduler's actual
    retry deadline, so grace-period lateness alone is not a reproducibility defect.

    ``as_of`` is the legacy date-only clock. It still works and still excludes future
    sessions, but a date cannot tell 09:28 ET from 16:05 ET, so it treats a same-day
    pending run as already due. Prefer ``now_et``; when both are given ``now_et`` wins.

    Only records belonging to ``cohort_id`` are considered.
    """

    if review_target < 1:
        raise ValueError("review_target must be positive")
    if now_et is None and as_of is None:
        raise ValueError("assess_cohort_phase requires now_et (preferred) or as_of")
    resolved_as_of = now_et.date() if now_et is not None else as_of
    assert resolved_as_of is not None  # guaranteed by the check above

    cohort_runs = sorted(
        (run for run in runs if run.cohort_id == cohort_id),
        key=lambda run: (run.scheduled_for, run.run_id),
    )
    window = (
        None
        if now_et is None
        else evidence_timing.assess_runs(cohort_runs, now_et=now_et, policy=policy)
    )

    if window is None:
        due_runs = [run for run in cohort_runs if run.scheduled_for <= resolved_as_of]
        upcoming = [run for run in cohort_runs if run.scheduled_for > resolved_as_of]
        overdue: tuple[evidence_timing.RunTimingAssessment, ...] = ()
        evidence_cutoff: date | None = resolved_as_of
        awaiting_session: date | None = None
        provider_session: date | None = None
        cohort_observations = [
            observation
            for observation in observations
            if observation.cohort_id == cohort_id and observation.session_date <= resolved_as_of
        ]
    else:
        due_runs = list(window.evidence_runs)
        outstanding = window.awaiting_execution + window.awaiting_provider_data + window.upcoming
        upcoming = [item.run for item in sorted(outstanding, key=lambda i: i.scheduled_for)]
        overdue = window.overdue
        evidence_cutoff = window.evidence_cutoff
        awaiting_session = (
            window.awaiting_execution[0].scheduled_for if window.awaiting_execution else None
        )
        provider_session = (
            window.awaiting_provider_data[0].scheduled_for
            if window.awaiting_provider_data
            else None
        )
        cohort_observations = [
            observation
            for observation in observations
            if observation.cohort_id == cohort_id
            and window.counts_evidence_observation(observation.session_date)
        ]

    timing = _timing_state(window, upcoming=upcoming, has_runs=bool(cohort_runs))

    due_sessions = len({run.scheduled_for for run in due_runs})
    completed_due_sessions = len(
        {run.scheduled_for for run in due_runs if run.status in _COMPLETE_RUN_STATUSES}
    )
    resolved_start = start_session or (cohort_runs[0].scheduled_for if cohort_runs else None)
    started = due_sessions > 0

    official = [item for item in cohort_observations if item.status is ObservationStatus.OFFICIAL]
    latest_observation = max((item.session_date for item in official), default=None)

    reliability: float | None = None
    if due_sessions > 0:
        reliability = completed_due_sessions / due_sessions

    alerts = _integrity_alerts(gate, due_runs, overdue)
    gate_passed = gate is not None and gate.status is GateStatus.PASS

    if completed_due_sessions >= review_target:
        if gate_passed:
            phase = CohortPhase.PASSED
        elif gate is not None and any(
            rule.status is GateStatus.FAIL and not rule.awaiting_evidence for rule in gate.rules
        ):
            phase = CohortPhase.FAILED
        else:
            phase = CohortPhase.REVIEW_READY
    elif alerts:
        phase = CohortPhase.ATTENTION_NEEDED
    elif started:
        phase = CohortPhase.COLLECTING
    else:
        phase = CohortPhase.SCHEDULED

    provider_run = (
        None
        if window is None or not window.awaiting_provider_data
        else window.awaiting_provider_data[-1].run
    )
    latest_for_display = due_runs[-1] if due_runs else provider_run
    latest_due_run = None if latest_for_display is None else _run_summary(latest_for_display)
    next_scheduled = upcoming[0].scheduled_for if upcoming else None
    sessions_remaining = max(0, review_target - completed_due_sessions)

    return CohortPhaseAssessment(
        cohort_id=cohort_id,
        phase=phase,
        as_of=resolved_as_of,
        start_session=resolved_start,
        started=started,
        review_target=review_target,
        due_sessions=due_sessions,
        completed_due_sessions=completed_due_sessions,
        total_scheduled_sessions=len({run.scheduled_for for run in cohort_runs}),
        latest_due_run=latest_due_run,
        next_scheduled_session=next_scheduled,
        latest_observation_date=latest_observation,
        official_observations=len(official),
        completion_reliability=reliability,
        next_action=_next_action(
            phase,
            start_session=resolved_start,
            next_scheduled_session=next_scheduled,
            sessions_remaining=sessions_remaining,
            latest_due_run=latest_due_run,
            integrity_alerts=alerts,
            timing=timing,
            awaiting_execution_session=awaiting_session,
            awaiting_provider_session=provider_session,
        ),
        headline=_headline(phase, resolved_start, started, timing),
        integrity_alerts=alerts,
        timing_state=timing,
        now_et=now_et,
        evidence_cutoff=evidence_cutoff,
        awaiting_execution_session=awaiting_session,
        awaiting_provider_session=provider_session,
        overdue_sessions=tuple(item.scheduled_for for item in overdue),
    )


__all__ = [
    "DEFAULT_REVIEW_TARGET",
    "INTEGRITY_RULES",
    "CohortPhase",
    "CohortPhaseAssessment",
    "RulePresentation",
    "RunSummary",
    "SessionTimingState",
    "assess_cohort_phase",
    "presentation_for",
]
