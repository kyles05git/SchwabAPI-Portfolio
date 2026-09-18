"""When a scheduled cohort session becomes *evidence*, in Eastern time.

A cohort run has an official decision instant — the exchange close of its session —
and a separate operational window in which the scheduler is expected to execute it.
Collapsing both into a single ``as_of`` calendar date makes a run that is merely
*upcoming* indistinguishable from one that is *missing*: at 09:28 ET on the morning of
a session, ``run.scheduled_for <= as_of`` is already true, so a correctly pending run
reads as an unexplained gap, a missing snapshot lineage, and a reproducibility defect.

This module keeps the two apart. It reuses the canonical calendar and scheduler in
:mod:`schwab_trader.market_calendar` and :mod:`schwab_trader.scheduling` — there is no
second market-hours calculation here — and classifies every persisted run into one of
:class:`RunTiming`:

- :attr:`RunTiming.UPCOMING` — the session close has not happened yet.
- :attr:`RunTiming.AWAITING_EXECUTION` — the close has passed and the scheduler is
  still inside its normal grace period. Due, not failed.
- :attr:`RunTiming.AWAITING_PROVIDER_DATA` — a preflight found incomplete provider
  evidence and remains retryable through the scheduler's actual deadline.
- :attr:`RunTiming.OVERDUE` — a normal pending run is past grace, or a provider wait is
  past the actual deadline. A real operational problem.
- :attr:`RunTiming.EXECUTED` — the run produced a durable outcome, whatever that
  outcome was. Completed, partial, failed, and missed runs are all evidence.
- :attr:`RunTiming.CLOSED_SESSION` — scheduled on a date the exchange was closed.

Only :attr:`RunTiming.EXECUTED` and :attr:`RunTiming.OVERDUE` runs are *evidence*.
Everything here is pure: it performs no I/O and never reads the wall clock. Callers
inject ``now_et`` (see :func:`schwab_trader.market_calendar.eastern_now`) so the same
inputs always produce the same classification.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

from schwab_trader import market_calendar as mc
from schwab_trader import scheduling
from schwab_trader.sleeve_runs import SleeveRun, SleeveRunStatus

#: Run states that mean the runner has not yet produced a durable outcome. A run in one
#: of these states is judged by the clock; every other state is recorded evidence.
_UNEXECUTED_RUN_STATUSES = frozenset(
    {
        SleeveRunStatus.PENDING,
        SleeveRunStatus.RUNNING,
        # A cohort awaiting data has deliberately executed nothing. It is clock-bound,
        # but unlike a run that never started it remains explicitly retryable after the
        # preferred grace period and becomes overdue only at the actual deadline.
        SleeveRunStatus.AWAITING_DATA,
        SleeveRunStatus.SKIPPED_CLOSED_SESSION,
    }
)


class RunTiming(StrEnum):
    """Where one persisted run sits relative to its session's official close."""

    UPCOMING = "upcoming"
    """The session's decision time has not been reached. Not evidence, not a defect."""

    AWAITING_EXECUTION = "awaiting-execution"
    """Due at the close and still inside the scheduler's grace period."""

    AWAITING_PROVIDER_DATA = "awaiting-provider-data"
    """The runner executed nothing and remains retryable until the scheduler's actual
    catch-up deadline, including after the preferred grace period.

    Covers both non-terminal waits — incomplete provider evidence, and a snapshot
    capture blocked on Schwab reauthentication. The *timing* is identical (retryable
    through the deadline, overdue after it); which wait it is lives on the run's error
    code, because only that changes what the operator should do."""

    OVERDUE = "overdue"
    """A normal run is past grace, or a provider wait is past its hard deadline."""

    EXECUTED = "executed"
    """The runner recorded a durable outcome for this session."""

    CLOSED_SESSION = "closed-session"
    """Scheduled on a weekend or exchange holiday; no session was ever owed."""


#: Timings whose runs carry evidence the gate and phase assessment may judge.
EVIDENCE_TIMINGS = frozenset({RunTiming.EXECUTED, RunTiming.OVERDUE})

#: Timings that mean the session simply has not delivered its outcome yet.
PENDING_TIMINGS = frozenset(
    {
        RunTiming.UPCOMING,
        RunTiming.AWAITING_EXECUTION,
        RunTiming.AWAITING_PROVIDER_DATA,
    }
)


@dataclass(frozen=True)
class RunTimingAssessment:
    """One run's timing verdict, with the scheduler evidence behind it."""

    run: SleeveRun
    timing: RunTiming
    due_et: datetime | None
    """The session's official decision instant plus the policy's decision delay."""

    elapsed_since_due: timedelta | None = None
    """How long an unexecuted run has gone past ``due_et``. The scheduler verdict and
    durable run status determine whether that elapsed time is still retryable."""

    reason: str = ""

    @property
    def scheduled_for(self) -> date:
        return self.run.scheduled_for

    @property
    def is_evidence(self) -> bool:
        """Whether this run may be judged as recorded evidence."""
        return self.timing in EVIDENCE_TIMINGS


@dataclass(frozen=True)
class EvidenceWindow:
    """The evidence/pending split for one cohort's runs at a fixed Eastern instant."""

    now_et: datetime
    evidence_cutoff: date | None
    """Latest exchange session whose decision time has passed, or ``None`` when no
    session is found inside the policy's lookback horizon."""

    assessments: tuple[RunTimingAssessment, ...]
    policy: scheduling.SchedulingPolicy = scheduling.DEFAULT_POLICY

    def _with(self, *timings: RunTiming) -> tuple[RunTimingAssessment, ...]:
        selected = set(timings)
        return tuple(item for item in self.assessments if item.timing in selected)

    @property
    def evidence(self) -> tuple[RunTimingAssessment, ...]:
        """Executed runs plus overdue gaps — everything that may be judged."""
        return tuple(item for item in self.assessments if item.is_evidence)

    @property
    def evidence_runs(self) -> tuple[SleeveRun, ...]:
        return tuple(item.run for item in self.evidence)

    @property
    def executed(self) -> tuple[RunTimingAssessment, ...]:
        return self._with(RunTiming.EXECUTED)

    @property
    def awaiting_execution(self) -> tuple[RunTimingAssessment, ...]:
        return self._with(RunTiming.AWAITING_EXECUTION)

    @property
    def awaiting_provider_data(self) -> tuple[RunTimingAssessment, ...]:
        return self._with(RunTiming.AWAITING_PROVIDER_DATA)

    @property
    def upcoming(self) -> tuple[RunTimingAssessment, ...]:
        return self._with(RunTiming.UPCOMING)

    @property
    def overdue(self) -> tuple[RunTimingAssessment, ...]:
        return self._with(RunTiming.OVERDUE)

    @property
    def evidence_session_dates(self) -> frozenset[date]:
        return frozenset(item.scheduled_for for item in self.evidence)

    @property
    def pending_session_dates(self) -> frozenset[date]:
        """Sessions whose outcome is still legitimately outstanding."""
        return frozenset(
            item.scheduled_for
            for item in self.assessments
            if item.timing in PENDING_TIMINGS or item.timing is RunTiming.CLOSED_SESSION
        )

    def timing_for(self, run_id: str) -> RunTiming | None:
        """The timing recorded for one run id, or ``None`` when it was not assessed."""
        return next((item.timing for item in self.assessments if item.run.run_id == run_id), None)

    def counts_evidence_observation(self, session_date: date) -> bool:
        """Whether an observation dated ``session_date`` is due evidence right now.

        A session after the evidence cutoff has not closed, and a session whose run is
        still upcoming or awaiting execution has not delivered its outcome, so neither
        may be judged as recorded evidence.
        """
        if self.evidence_cutoff is None or session_date > self.evidence_cutoff:
            return False
        return session_date not in self.pending_session_dates


def classify_run(
    run: SleeveRun,
    now_et: datetime,
    policy: scheduling.SchedulingPolicy = scheduling.DEFAULT_POLICY,
    exchange: str = mc.EXCHANGE_MIC,
) -> RunTimingAssessment:
    """Classify one persisted run against the canonical scheduler at ``now_et``.

    A run that already recorded a durable outcome is evidence regardless of the clock.
    An unexecuted run is judged by :func:`schwab_trader.scheduling.evaluate_session`,
    so the exchange calendar, early closes, the post-close decision delay, and the
    grace period all come from the single canonical implementation.
    """
    decision = scheduling.evaluate_session(
        run.cohort_id,
        run.scheduled_for,
        now_et,
        completed_run_keys=frozenset(),
        policy=policy,
        exchange=exchange,
    )
    executed = run.status not in _UNEXECUTED_RUN_STATUSES

    if executed:
        return RunTimingAssessment(
            run=run,
            timing=RunTiming.EXECUTED,
            due_et=decision.due_et,
            reason=f"Run recorded a durable {run.status.value} outcome.",
        )
    if decision.status is scheduling.RunStatus.SKIPPED_CLOSED_SESSION:
        return RunTimingAssessment(
            run=run, timing=RunTiming.CLOSED_SESSION, due_et=None, reason=decision.reason
        )
    if run.status is SleeveRunStatus.SKIPPED_CLOSED_SESSION:
        return RunTimingAssessment(
            run=run,
            timing=RunTiming.CLOSED_SESSION,
            due_et=decision.due_et,
            reason="Run was recorded as a skipped closed session.",
        )
    if decision.status is scheduling.RunStatus.PENDING:
        return RunTimingAssessment(
            run=run, timing=RunTiming.UPCOMING, due_et=decision.due_et, reason=decision.reason
        )
    if run.status is SleeveRunStatus.AWAITING_DATA and decision.status in {
        scheduling.RunStatus.DUE,
        scheduling.RunStatus.LATE,
    }:
        return RunTimingAssessment(
            run=run,
            timing=RunTiming.AWAITING_PROVIDER_DATA,
            due_et=decision.due_et,
            elapsed_since_due=decision.late_by,
            reason=decision.reason,
        )
    if decision.status is scheduling.RunStatus.DUE:
        return RunTimingAssessment(
            run=run,
            timing=RunTiming.AWAITING_EXECUTION,
            due_et=decision.due_et,
            elapsed_since_due=decision.late_by,
            reason=decision.reason,
        )
    return RunTimingAssessment(
        run=run,
        timing=RunTiming.OVERDUE,
        due_et=decision.due_et,
        elapsed_since_due=decision.late_by,
        reason=decision.reason,
    )


def assess_runs(
    runs: Iterable[SleeveRun],
    *,
    now_et: datetime,
    policy: scheduling.SchedulingPolicy = scheduling.DEFAULT_POLICY,
    exchange: str = mc.EXCHANGE_MIC,
) -> EvidenceWindow:
    """Partition a cohort's runs into evidence and legitimately pending sessions.

    ``now_et`` is naive Eastern wall-clock time. Runs are returned in scheduled order
    so the caller can take the latest evidence run without re-sorting.
    """
    ordered = sorted(runs, key=lambda run: (run.scheduled_for, run.run_id))
    return EvidenceWindow(
        now_et=now_et,
        evidence_cutoff=scheduling.most_recent_due_session_date(now_et, policy, exchange),
        assessments=tuple(classify_run(run, now_et, policy, exchange) for run in ordered),
        policy=policy,
    )


__all__ = [
    "EVIDENCE_TIMINGS",
    "PENDING_TIMINGS",
    "EvidenceWindow",
    "RunTiming",
    "RunTimingAssessment",
    "assess_runs",
    "classify_run",
]
