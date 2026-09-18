"""Offline tests for the run-timing partition.

Every case pins an explicit naive Eastern instant. Nothing here opens a database, reads
``.env``, touches the network, or consults the wall clock.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from schwab_trader import evidence_timing, scheduling
from schwab_trader.evidence_timing import RunTiming
from schwab_trader.sleeve_runs import (
    MemberRunStatus,
    SleeveRun,
    SleeveRunMember,
    SleeveRunStatus,
)

COHORT = "paper-first-2026-07-27"
START = date(2026, 7, 27)  # Monday, a regular XNYS session
EARLY_CLOSE = date(2026, 11, 27)  # Friday after Thanksgiving: 13:00 ET close
HOLIDAY = date(2026, 7, 3)  # Independence Day observed (July 4 falls on a Saturday)
SATURDAY = date(2026, 8, 1)
MEMBERS = ("sleeve-bench", "sleeve-trend", "sleeve-value")


def make_run(session: date, *, status: SleeveRunStatus = SleeveRunStatus.PENDING) -> SleeveRun:
    pending = status in (
        SleeveRunStatus.PENDING,
        SleeveRunStatus.RUNNING,
        SleeveRunStatus.AWAITING_DATA,
    )
    started = datetime.combine(session, datetime.min.time(), tzinfo=UTC)
    return SleeveRun(
        run_id=f"run-{session.isoformat()}",
        run_key=f"{COHORT}|{session.isoformat()}",
        cohort_id=COHORT,
        session_id=f"XNYS|{session.isoformat()}",
        scheduled_for=session,
        expected_members=MEMBERS,
        completed_members=() if pending else MEMBERS,
        snapshot_id=None if pending else f"snap-{session.isoformat()}",
        quote_snapshot_id=None if pending else f"quote-{session.isoformat()}",
        started_at=started,
        completed_at=None if pending else started + timedelta(minutes=5),
        status=status,
        members=tuple(
            SleeveRunMember(
                sleeve_id=sleeve,
                status=MemberRunStatus.PENDING if pending else MemberRunStatus.COMPLETED,
            )
            for sleeve in MEMBERS
        ),
    )


def classify(session: date, now_et: datetime, **kwargs: object) -> RunTiming:
    run = make_run(session, **kwargs)  # type: ignore[arg-type]
    return evidence_timing.classify_run(run, now_et).timing


# --- One run against the clock ----------------------------------------------


@pytest.mark.parametrize(
    ("now_et", "expected"),
    [
        # The morning of the session: the 16:00 ET decision time is hours away.
        (datetime(2026, 7, 27, 9, 28), RunTiming.UPCOMING),
        # One minute before the close is still before the close.
        (datetime(2026, 7, 27, 15, 59), RunTiming.UPCOMING),
        (datetime(2026, 7, 27, 16, 0), RunTiming.AWAITING_EXECUTION),
        (datetime(2026, 7, 27, 16, 5), RunTiming.AWAITING_EXECUTION),
        # The default grace period is two hours, so 18:00 ET is the boundary.
        (datetime(2026, 7, 27, 18, 0), RunTiming.AWAITING_EXECUTION),
        (datetime(2026, 7, 27, 18, 1), RunTiming.OVERDUE),
        (datetime(2026, 7, 28, 9, 0), RunTiming.OVERDUE),
    ],
)
def test_pending_run_timing_follows_the_exchange_close(now_et: datetime, expected: RunTiming):
    assert classify(START, now_et) is expected


@pytest.mark.parametrize(
    ("now_et", "expected"),
    [
        (datetime(2026, 11, 27, 12, 59), RunTiming.UPCOMING),
        (datetime(2026, 11, 27, 13, 5), RunTiming.AWAITING_EXECUTION),
        (datetime(2026, 11, 27, 15, 30), RunTiming.OVERDUE),
    ],
)
def test_early_close_session_uses_the_one_oclock_close(now_et: datetime, expected: RunTiming):
    """The day after Thanksgiving closes at 13:00 ET, not 16:00."""
    assert classify(EARLY_CLOSE, now_et) is expected


def test_a_run_scheduled_on_a_holiday_is_a_closed_session():
    assert classify(HOLIDAY, datetime(2026, 7, 6, 12, 0)) is RunTiming.CLOSED_SESSION


def test_a_run_scheduled_on_a_weekend_is_a_closed_session():
    assert classify(SATURDAY, datetime(2026, 8, 3, 12, 0)) is RunTiming.CLOSED_SESSION


@pytest.mark.parametrize(
    "status",
    [
        SleeveRunStatus.COMPLETED,
        SleeveRunStatus.PARTIAL,
        SleeveRunStatus.FAILED,
        SleeveRunStatus.MISSED,
    ],
)
def test_a_recorded_outcome_is_evidence_whatever_it_says(status: SleeveRunStatus):
    assessment = evidence_timing.classify_run(
        make_run(START, status=status), datetime(2026, 7, 27, 16, 20)
    )
    assert assessment.timing is RunTiming.EXECUTED
    assert assessment.is_evidence is True


def test_a_running_run_inside_grace_is_awaiting_not_failed():
    assert (
        classify(START, datetime(2026, 7, 27, 16, 3), status=SleeveRunStatus.RUNNING)
        is RunTiming.AWAITING_EXECUTION
    )


@pytest.mark.parametrize(
    "now_et",
    [
        datetime(2026, 7, 27, 16, 5),
        datetime(2026, 7, 27, 18, 45),
        datetime(2026, 7, 28, 9, 0),
    ],
)
def test_awaiting_provider_data_remains_retryable_until_the_actual_deadline(
    now_et: datetime,
):
    assert (
        classify(START, now_et, status=SleeveRunStatus.AWAITING_DATA)
        is RunTiming.AWAITING_PROVIDER_DATA
    )


def test_awaiting_provider_data_becomes_overdue_at_the_actual_deadline():
    assert (
        classify(START, datetime(2026, 7, 28, 16, 0), status=SleeveRunStatus.AWAITING_DATA)
        is RunTiming.OVERDUE
    )


# --- The window over a whole cohort -----------------------------------------


def test_evidence_cutoff_is_the_last_session_that_actually_closed():
    morning = evidence_timing.assess_runs([], now_et=datetime(2026, 7, 27, 9, 28))
    after = evidence_timing.assess_runs([], now_et=datetime(2026, 7, 27, 16, 5))
    weekend = evidence_timing.assess_runs([], now_et=datetime(2026, 8, 1, 11, 0))

    assert morning.evidence_cutoff == date(2026, 7, 24)  # the previous Friday
    assert after.evidence_cutoff == START
    assert weekend.evidence_cutoff == date(2026, 7, 31)  # Friday


def test_upcoming_and_awaiting_runs_are_not_evidence():
    runs = [make_run(START), make_run(date(2026, 7, 28))]
    window = evidence_timing.assess_runs(runs, now_et=datetime(2026, 7, 27, 16, 5))

    assert [item.timing for item in window.assessments] == [
        RunTiming.AWAITING_EXECUTION,
        RunTiming.UPCOMING,
    ]
    assert window.evidence == ()
    assert window.evidence_runs == ()
    assert window.pending_session_dates == frozenset({START, date(2026, 7, 28)})


def test_an_observation_is_evidence_only_after_its_session_delivered():
    runs = [make_run(START)]
    morning = evidence_timing.assess_runs(runs, now_et=datetime(2026, 7, 27, 9, 28))
    executed = evidence_timing.assess_runs(
        [make_run(START, status=SleeveRunStatus.COMPLETED)],
        now_et=datetime(2026, 7, 27, 16, 20),
    )

    assert morning.counts_evidence_observation(START) is False
    assert executed.counts_evidence_observation(START) is True
    # A session in the future is never evidence, whatever its run says.
    assert executed.counts_evidence_observation(date(2026, 7, 28)) is False


def test_a_custom_policy_moves_the_grace_boundary():
    policy = scheduling.SchedulingPolicy(grace_period=timedelta(minutes=30))
    window = evidence_timing.assess_runs(
        [make_run(START)], now_et=datetime(2026, 7, 27, 16, 45), policy=policy
    )
    assert window.overdue and window.overdue[0].scheduled_for == START
    assert window.overdue[0].elapsed_since_due == timedelta(minutes=45)
