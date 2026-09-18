"""Offline tests for the cohort operations health verdict.

Every case pins an explicit naive Eastern instant. Nothing here opens a database,
reads ``.env``, touches the network, consults the wall clock, or reaches a broker.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from schwab_trader import cohort_ops, scheduling
from schwab_trader.cohort_ops import CohortState
from schwab_trader.evaluation import ObservationStatus, OfficialDailyObservation
from schwab_trader.sleeve_runs import (
    MemberRunStatus,
    SleeveRun,
    SleeveRunError,
    SleeveRunMember,
    SleeveRunStatus,
)

COHORT = "paper-first-2026-07-27"
MEMBERS = ("sleeve-bench", "sleeve-trend", "sleeve-value")

MONDAY = date(2026, 7, 27)  # regular XNYS session, 16:00 ET close
TUESDAY = date(2026, 7, 28)
FRIDAY = date(2026, 7, 31)
SATURDAY = date(2026, 8, 1)
EARLY_CLOSE = date(2026, 11, 27)  # day after Thanksgiving: 13:00 ET close
HOLIDAY = date(2026, 7, 3)  # Independence Day observed


def make_run(
    session: date,
    *,
    status: SleeveRunStatus = SleeveRunStatus.PENDING,
    completed: tuple[str, ...] | None = None,
    completed_at: datetime | None = None,
    errors: tuple[SleeveRunError, ...] = (),
) -> SleeveRun:
    done = MEMBERS if completed is None else completed
    started = datetime.combine(session, datetime.min.time(), tzinfo=UTC)
    terminal = status not in (SleeveRunStatus.PENDING, SleeveRunStatus.RUNNING)
    return SleeveRun(
        run_id=f"run-{session.isoformat()}",
        run_key=scheduling.run_key(COHORT, scheduling.session_for_date(session)),
        cohort_id=COHORT,
        session_id=scheduling.session_for_date(session).session_id,
        scheduled_for=session,
        expected_members=MEMBERS,
        completed_members=() if status is SleeveRunStatus.PENDING else done,
        started_at=started,
        completed_at=(
            completed_at
            if completed_at is not None
            else (started + timedelta(hours=20) if terminal else None)
        ),
        status=status,
        errors=errors,
        members=tuple(
            SleeveRunMember(
                sleeve_id=sleeve,
                status=(
                    MemberRunStatus.COMPLETED if sleeve in done else MemberRunStatus.FAILED
                ),
            )
            for sleeve in MEMBERS
        ),
    )


def make_observation(
    session: date,
    sleeve: str,
    status: ObservationStatus = ObservationStatus.OFFICIAL,
) -> OfficialDailyObservation:
    decision = datetime.combine(session, datetime.min.time(), tzinfo=UTC)
    return OfficialDailyObservation(
        cohort_id=COHORT,
        run_id=f"run-{session.isoformat()}",
        sleeve_id=sleeve,
        strategy="trend",
        strategy_hash="0" * 64,
        session_date=session,
        decision_time=decision,
        valuation_time=decision,
        status=status,
        total_value=Decimal("10000.00"),
        return_pct=Decimal("0.00"),
        num_filled=0,
        num_rejected=0,
        snapshot_ids={},
        readiness_reasons=(),
    )


def assess(
    now_et: datetime,
    *,
    runs: tuple[SleeveRun, ...] = (),
    observations: tuple[OfficialDailyObservation, ...] = (),
    members: tuple[str, ...] = MEMBERS,
    session_date: date | None = None,
) -> cohort_ops.CohortHealthReport:
    return cohort_ops.assess_cohort(
        COHORT,
        now_et=now_et,
        runs=runs,
        observations=observations,
        expected_members=members,
        session_date=session_date,
    )


# --- The clock alone, with no run recorded -----------------------------------


@pytest.mark.parametrize(
    ("now_et", "expected"),
    [
        # The morning of a session: the 16:00 ET close is hours away. Friday's run
        # already completed, so there is nothing owed and today is merely pending.
        (datetime(2026, 7, 28, 9, 28), CohortState.PRE_CLOSE),
        (datetime(2026, 7, 28, 15, 59), CohortState.PRE_CLOSE),
        # At and just after the close the run is due inside the two-hour grace period.
        (datetime(2026, 7, 28, 16, 0), CohortState.DUE),
        (datetime(2026, 7, 28, 17, 59), CohortState.DUE),
        (datetime(2026, 7, 28, 18, 0), CohortState.DUE),
        # Past the grace period, but the next session is not due yet: recoverable.
        (datetime(2026, 7, 28, 18, 1), CohortState.LATE),
        (datetime(2026, 7, 29, 9, 0), CohortState.LATE),
        # Once Wednesday's own close arrives, Tuesday is superseded and unrecoverable.
        (datetime(2026, 7, 29, 16, 0), CohortState.MISSED),
    ],
)
def test_states_follow_the_exchange_close_when_nothing_ran(
    now_et: datetime, expected: CohortState
) -> None:
    monday = make_run(MONDAY, status=SleeveRunStatus.COMPLETED)
    report = assess(now_et, runs=(monday,), session_date=None)
    if expected is CohortState.MISSED:
        # After Wednesday's close the focus moves to Tuesday, the unresolved session.
        assert report.session.session_date == TUESDAY
    assert report.state is expected


def test_pre_close_is_reported_for_today_not_the_prior_session() -> None:
    """A healthy cohort mid-morning reads as pending today, not silent about it."""
    monday = make_run(MONDAY, status=SleeveRunStatus.COMPLETED)
    report = assess(datetime(2026, 7, 28, 10, 0), runs=(monday,))
    assert report.state is CohortState.PRE_CLOSE
    assert report.session.session_date == TUESDAY
    assert report.exit_code == cohort_ops.EXIT_OK
    assert "closes" in report.next_action


def test_an_unresolved_prior_session_outranks_todays_pending_one() -> None:
    """The actionable gap wins: yesterday failed, so do not show today's calm."""
    report = assess(
        datetime(2026, 7, 28, 10, 0),
        runs=(make_run(MONDAY, status=SleeveRunStatus.FAILED, completed=()),),
    )
    assert report.state is CohortState.FAILED
    assert report.session.session_date == MONDAY
    assert report.exit_code == cohort_ops.EXIT_ATTENTION


def test_a_session_that_became_unrecoverable_is_surfaced_over_a_merely_due_one() -> None:
    """Wednesday being due is a non-action; Tuesday being missed is the incident."""
    report = assess(
        datetime(2026, 7, 29, 16, 30),
        runs=(make_run(MONDAY, status=SleeveRunStatus.COMPLETED),),
    )
    assert report.state is CohortState.MISSED
    assert report.session.session_date == TUESDAY
    assert report.exit_code == cohort_ops.EXIT_ATTENTION


def test_a_cohort_registered_this_morning_has_not_missed_yesterday() -> None:
    """With no run ever recorded there is no schedule to be behind — only today."""
    report = assess(datetime(2026, 7, 27, 10, 0))
    assert report.state is CohortState.PRE_CLOSE
    assert report.session.session_date == MONDAY
    assert report.exit_code == cohort_ops.EXIT_OK

    # After its own close, though, today's absent run is a real gap.
    later = assess(datetime(2026, 7, 27, 19, 0))
    assert later.state is CohortState.LATE
    assert later.session.session_date == MONDAY


def test_an_older_gap_stays_visible_after_it_leaves_the_focus_window() -> None:
    """A missed session must not vanish just because newer ones came and went."""
    runs = (
        make_run(MONDAY, status=SleeveRunStatus.COMPLETED),
        # Tuesday never ran at all.
        make_run(date(2026, 7, 29), status=SleeveRunStatus.COMPLETED),
        make_run(date(2026, 7, 30), status=SleeveRunStatus.COMPLETED),
        make_run(FRIDAY, status=SleeveRunStatus.COMPLETED),
    )
    report = assess(datetime(2026, 8, 3, 10, 0), runs=runs)  # the following Monday

    # The focused verdict is calm: everything in the focus window is resolved.
    assert report.state is CohortState.PRE_CLOSE
    assert report.exit_code == cohort_ops.EXIT_OK

    # But Tuesday's gap is still reported rather than forgotten.
    assert [item.session_date for item in report.unresolved_history] == [TUESDAY]
    assert report.unresolved_history[0].state is CohortState.MISSED
    assert report.has_unresolved_history
    assert report.history_scanned_from == MONDAY


def test_a_first_session_the_scheduler_missed_entirely_is_still_caught() -> None:
    """The whole point of the persisted start: no run exists to infer the start from.

    The cohort's first owed session is Monday, but the scheduler never fired that day,
    so Monday leaves no record at all. Only a declared start can recover it.
    """
    runs = (make_run(TUESDAY, status=SleeveRunStatus.COMPLETED),)
    kwargs = {"runs": runs, "expected_members": MEMBERS}

    # Wednesday morning: Monday is still inside the focus window.
    inferred = cohort_ops.assess_cohort(
        COHORT, now_et=datetime(2026, 7, 29, 10, 0), **kwargs  # type: ignore[arg-type]
    )
    declared = cohort_ops.assess_cohort(
        COHORT,
        now_et=datetime(2026, 7, 29, 10, 0),
        cohort_start=MONDAY,
        **kwargs,  # type: ignore[arg-type]
    )
    # Inferring the start from the earliest run puts the boundary at Tuesday, so the
    # missed first day is silently written off as "before the cohort existed".
    assert inferred.state is CohortState.PRE_CLOSE
    assert inferred.exit_code == cohort_ops.EXIT_OK
    # The declared start makes it the headline instead.
    assert declared.state is CohortState.MISSED
    assert declared.session.session_date == MONDAY
    assert declared.exit_code == cohort_ops.EXIT_ATTENTION

    # A week later Monday has left the focus window, but is still reported as history.
    later = cohort_ops.assess_cohort(
        COHORT,
        now_et=datetime(2026, 8, 5, 10, 0),
        cohort_start=MONDAY,
        **kwargs,  # type: ignore[arg-type]
    )
    assert MONDAY in {item.session_date for item in later.unresolved_history}
    assert next(
        item for item in later.unresolved_history if item.session_date == MONDAY
    ).expected == len(MEMBERS)


def test_the_history_scan_is_bounded_and_never_predates_the_cohort() -> None:
    report = cohort_ops.assess_cohort(
        COHORT,
        now_et=datetime(2026, 8, 3, 18, 30),
        runs=[make_run(MONDAY, status=SleeveRunStatus.COMPLETED)],
        expected_members=MEMBERS,
        cohort_start=MONDAY,
        history_sessions=3,
    )
    assert report.history_scanned_from is not None
    assert report.history_scanned_from >= MONDAY
    assert all(item.session_date >= MONDAY for item in report.unresolved_history)
    assert len(report.unresolved_history) <= 3


def test_sessions_before_the_cohort_started_are_never_reported_as_gaps() -> None:
    """A cohort whose first session is Monday owes nothing for the Friday before."""
    report = assess(
        datetime(2026, 7, 27, 18, 30),
        runs=(make_run(MONDAY, status=SleeveRunStatus.COMPLETED),),
    )
    assert report.session.session_date == MONDAY
    assert report.state is CohortState.COMPLETED

    # The same holds when the boundary is stated explicitly rather than inferred.
    explicit = cohort_ops.assess_cohort(
        COHORT,
        now_et=datetime(2026, 7, 27, 18, 30),
        runs=[make_run(MONDAY, status=SleeveRunStatus.COMPLETED)],
        expected_members=MEMBERS,
        cohort_start=MONDAY,
    )
    assert explicit.state is CohortState.COMPLETED


# --- Recorded outcomes beat the clock ----------------------------------------


def test_seven_of_seven_completed_reports_complete_evidence() -> None:
    members = tuple(f"sleeve-{index}" for index in range(7))
    run = make_run(MONDAY, status=SleeveRunStatus.COMPLETED)
    run = run.model_copy(update={"expected_members": members, "completed_members": members})
    report = cohort_ops.assess_cohort(
        COHORT,
        now_et=datetime(2026, 7, 27, 18, 30),
        runs=[run],
        observations=[make_observation(MONDAY, sleeve) for sleeve in members],
        expected_members=members,
    )
    assert report.state is CohortState.COMPLETED
    assert (report.completed_count, report.expected_count) == (7, 7)
    assert report.observations is not None
    assert report.observations.official == 7
    assert report.exit_code == cohort_ops.EXIT_OK


def test_partial_run_lists_the_members_that_did_not_complete() -> None:
    run = make_run(
        MONDAY,
        status=SleeveRunStatus.PARTIAL,
        completed=("sleeve-bench",),
        errors=(
            SleeveRunError(
                code="data_not_ready",
                message="Required strategy data failed readiness checks.",
                member_id="sleeve-trend",
            ),
        ),
    )
    report = assess(datetime(2026, 7, 27, 18, 30), runs=(run,))
    assert report.state is CohortState.PARTIAL
    assert report.run is not None
    assert report.run.missing_members == ("sleeve-trend", "sleeve-value")
    assert "sleeve-trend" in report.next_action
    assert report.exit_code == cohort_ops.EXIT_ATTENTION


def test_a_completed_run_stays_completed_however_late_the_clock_is() -> None:
    """A recorded outcome is evidence; lateness is a separate, reported fact."""
    run = make_run(MONDAY, status=SleeveRunStatus.COMPLETED)
    report = assess(datetime(2026, 7, 31, 12, 0), runs=(run,), session_date=MONDAY)
    assert report.state is CohortState.COMPLETED
    assert report.exit_code == cohort_ops.EXIT_OK


def test_a_run_finishing_after_grace_is_flagged_as_late() -> None:
    # Monday closes 16:00 ET = 20:00 UTC; grace ends 22:00 UTC.
    on_time = make_run(
        MONDAY,
        status=SleeveRunStatus.COMPLETED,
        completed_at=datetime(2026, 7, 27, 21, 0, tzinfo=UTC),
    )
    overdue = make_run(
        MONDAY,
        status=SleeveRunStatus.COMPLETED,
        completed_at=datetime(2026, 7, 28, 2, 0, tzinfo=UTC),
    )
    assert assess(datetime(2026, 7, 27, 18, 30), runs=(on_time,)).run is not None
    assert not assess(datetime(2026, 7, 27, 18, 30), runs=(on_time,)).run.ran_late  # type: ignore[union-attr]
    report = assess(datetime(2026, 7, 28, 9, 0), runs=(overdue,), session_date=MONDAY)
    assert report.run is not None and report.run.ran_late
    assert report.state is CohortState.COMPLETED
    assert "grace period" in report.next_action


# --- Calendar edges -----------------------------------------------------------


def test_early_close_moves_the_due_time_to_one_pm() -> None:
    """13:05 ET is after an early close, so the run is due — not still pending."""
    # Nov 26 2026 is Thanksgiving, so the prior session is Nov 25. Both are resolved
    # here; the point of the case is the 13:00 ET close, not a backlog.
    prior = (
        make_run(date(2026, 11, 25), status=SleeveRunStatus.COMPLETED),
        make_run(EARLY_CLOSE, status=SleeveRunStatus.COMPLETED).model_copy(
            update={"status": SleeveRunStatus.PENDING, "completed_members": ()}
        ),
    )
    before = assess(datetime(2026, 11, 27, 12, 55), runs=prior)
    after = assess(datetime(2026, 11, 27, 13, 5), runs=prior)
    assert before.session.session_date == EARLY_CLOSE
    assert before.state is CohortState.PRE_CLOSE
    assert before.session.is_early_close
    assert after.state is CohortState.DUE
    assert after.session.session_date == EARLY_CLOSE


def test_a_weekend_reports_the_last_trading_session() -> None:
    friday = make_run(FRIDAY, status=SleeveRunStatus.COMPLETED)
    report = assess(datetime(2026, 8, 1, 11, 0), runs=(friday,))
    assert report.session.session_date == FRIDAY
    assert report.state is CohortState.COMPLETED
    assert report.next_session is not None
    assert report.next_session.session_date == date(2026, 8, 3)  # Monday


def test_a_holiday_is_never_owed_a_run() -> None:
    """July 3 2026 is the observed Independence Day; nothing is expected."""
    report = assess(datetime(2026, 7, 3, 11, 0), session_date=HOLIDAY)
    assert report.state is CohortState.CLOSED_SESSION
    assert not report.session.is_trading_day
    assert report.exit_code == cohort_ops.EXIT_OK


def test_a_session_skipped_as_closed_is_not_a_failure() -> None:
    run = make_run(SATURDAY, status=SleeveRunStatus.SKIPPED_CLOSED_SESSION, completed=())
    report = assess(datetime(2026, 8, 1, 20, 0), runs=(run,), session_date=SATURDAY)
    assert report.state is CohortState.CLOSED_SESSION
    assert report.exit_code == cohort_ops.EXIT_OK


# --- Fail-closed behavior ------------------------------------------------------


def test_no_roster_and_no_run_is_unknown_rather_than_healthy() -> None:
    report = assess(datetime(2026, 7, 28, 10, 0), members=())
    assert report.state is CohortState.UNKNOWN
    assert report.exit_code == cohort_ops.EXIT_UNKNOWN


def test_an_empty_cohort_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="cohort_id"):
        cohort_ops.assess_cohort("  ", now_et=datetime(2026, 7, 28, 10, 0))


def test_runs_from_another_cohort_are_ignored() -> None:
    foreign = make_run(MONDAY, status=SleeveRunStatus.COMPLETED).model_copy(
        update={"cohort_id": "other-cohort"}
    )
    # 18:30 ET is past the two-hour grace period, so with nothing of our own recorded
    # the session reads as late — a foreign cohort's evidence never counts as ours.
    report = assess(datetime(2026, 7, 27, 18, 30), runs=(foreign,), session_date=MONDAY)
    assert report.run is None
    assert report.state is CohortState.LATE


# --- The JSON contract ---------------------------------------------------------


def test_payload_is_json_serializable_and_carries_the_verdict() -> None:
    run = make_run(MONDAY, status=SleeveRunStatus.COMPLETED)
    report = assess(
        datetime(2026, 7, 27, 18, 30),
        runs=(run,),
        observations=tuple(make_observation(MONDAY, sleeve) for sleeve in MEMBERS),
    )
    payload = cohort_ops.health_payload(report)
    text = json.dumps(payload, sort_keys=True)  # must not need a custom encoder
    restored = json.loads(text)

    assert restored["schema"] == cohort_ops.PAYLOAD_SCHEMA
    assert restored["state"] == "completed"
    assert restored["exit_code"] == 0
    assert restored["needs_attention"] is False
    assert restored["session"]["date"] == MONDAY.isoformat()
    assert restored["session"]["close_et"].endswith("16:00:00")
    assert restored["members"] == {
        "expected": 3,
        "completed": 3,
        "cohort_roster": list(MEMBERS),
    }
    assert restored["observations"]["official"] == 3
    assert restored["next_action"]


def test_payload_exposes_no_secret_or_connection_identity() -> None:
    """Only cohort, sleeve, session, and status names may appear in the contract."""
    report = cohort_ops.assess_cohort(
        COHORT,
        now_et=datetime(2026, 7, 27, 18, 30),
        runs=[make_run(MONDAY, status=SleeveRunStatus.PARTIAL, completed=("sleeve-bench",))],
        observations=[make_observation(MONDAY, "sleeve-bench")],
        expected_members=MEMBERS,
        storage_kind="shared-postgresql",
    )
    text = json.dumps(cohort_ops.health_payload(report)).lower()
    for forbidden in (
        "postgresql://",
        "postgres://",
        "sqlite:///",
        "password",
        "secret",
        "token",
        "@",  # no host/user identity or email address
        "sslmode",
        "account",
    ):
        assert forbidden not in text, forbidden
    assert "shared-postgresql" in text  # the category is allowed, the URL is not
