"""Offline tests for phase assessment and the operational gate's injected clock.

Every case pins an explicit clock — a naive Eastern ``now_et`` instant for the timing
model, or the legacy ``as_of`` date where that path is still exercised. Nothing here
opens a database, reads ``.env``, touches the network, or consults the wall clock.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from schwab_trader import cohort_phase, operational_gate, scheduling
from schwab_trader.cohort_phase import CohortPhase, RulePresentation, SessionTimingState
from schwab_trader.evaluation import ObservationStatus, OfficialDailyObservation
from schwab_trader.experiments import ExperimentCohort
from schwab_trader.operational_gate import GateRule, GateStatus
from schwab_trader.sleeve_runs import (
    MemberRunStatus,
    SleeveRun,
    SleeveRunError,
    SleeveRunMember,
    SleeveRunStatus,
)

COHORT = "paper-first-2026-07-27"
START = date(2026, 7, 27)  # Monday, a regular XNYS session closing at 16:00 ET
EARLY_CLOSE = date(2026, 11, 27)  # Friday after Thanksgiving: 13:00 ET close
HOLIDAY = date(2026, 7, 3)  # Independence Day observed (July 4 falls on a Saturday)
MEMBERS = ("sleeve-bench", "sleeve-trend", "sleeve-value")

# The instants the pre-close bug report named, plus the grace-period boundary.
MORNING = datetime(2026, 7, 27, 9, 28)
ONE_MINUTE_BEFORE_CLOSE = datetime(2026, 7, 27, 15, 59)
JUST_AFTER_CLOSE = datetime(2026, 7, 27, 16, 5)
PAST_GRACE = datetime(2026, 7, 27, 18, 45)


def make_run(
    session: date,
    *,
    status: SleeveRunStatus = SleeveRunStatus.COMPLETED,
    members: tuple[str, ...] = MEMBERS,
    completed: tuple[str, ...] | None = None,
    errors: tuple[SleeveRunError, ...] = (),
) -> SleeveRun:
    done = members if completed is None else completed
    pending = status in {
        SleeveRunStatus.PENDING,
        SleeveRunStatus.AWAITING_DATA,
    }
    started = datetime.combine(session, datetime.min.time(), tzinfo=UTC)
    return SleeveRun(
        run_id=f"run-{session.isoformat()}",
        run_key=f"{COHORT}|{session.isoformat()}",
        cohort_id=COHORT,
        session_id=f"XNYS|{session.isoformat()}",
        scheduled_for=session,
        expected_members=members,
        completed_members=() if pending else done,
        snapshot_id=None if pending else f"snap-{session.isoformat()}",
        quote_snapshot_id=None if pending else f"quote-{session.isoformat()}",
        started_at=started,
        completed_at=None if pending else started + timedelta(minutes=5),
        status=status,
        errors=errors,
        members=tuple(
            SleeveRunMember(
                sleeve_id=sleeve,
                status=(
                    MemberRunStatus.PENDING
                    if pending
                    else MemberRunStatus.COMPLETED
                    if sleeve in done
                    else MemberRunStatus.FAILED
                ),
            )
            for sleeve in members
        ),
    )


def make_observation(
    sleeve_id: str, session: date, *, status: ObservationStatus = ObservationStatus.OFFICIAL
) -> OfficialDailyObservation:
    official = status is ObservationStatus.OFFICIAL
    stamp = datetime.combine(session, datetime.min.time(), tzinfo=UTC)
    return OfficialDailyObservation(
        cohort_id=COHORT,
        run_id=f"run-{session.isoformat()}",
        sleeve_id=sleeve_id,
        strategy="trend",
        strategy_hash="a" * 64,
        session_date=session,
        decision_time=stamp,
        valuation_time=stamp + timedelta(minutes=5),
        status=status,
        total_value=None if not official else 10_000,
        return_pct=None if not official else 0,
        readiness_ready=official,
        readiness_reasons=() if official else ("quotes:stale",),
    )


def sessions(count: int, *, start: date = START) -> list[date]:
    return [start + timedelta(days=index) for index in range(count)]


# --- Phase assessment -------------------------------------------------------


def test_future_scheduled_run_is_scheduled_not_failed():
    """The real July-27 state: one pending run in the future, nothing observed."""
    assessment = cohort_phase.assess_cohort_phase(
        cohort_id=COHORT,
        runs=[make_run(START, status=SleeveRunStatus.PENDING)],
        observations=[],
        gate=None,
        as_of=date(2026, 7, 24),
    )
    assert assessment.phase is CohortPhase.SCHEDULED
    assert assessment.started is False
    assert assessment.due_sessions == 0
    assert assessment.completed_due_sessions == 0
    # A session that has not happened cannot lower completion reliability.
    assert assessment.completion_reliability is None
    assert assessment.integrity_alerts == ()
    assert assessment.headline == "Starts July 27, 2026"
    assert assessment.next_action == "Run the cohort after the July 27, 2026 market close."


def test_future_observations_are_not_presented_as_current_evidence():
    assessment = cohort_phase.assess_cohort_phase(
        cohort_id=COHORT,
        runs=[make_run(START, status=SleeveRunStatus.PENDING)],
        observations=[make_observation(MEMBERS[0], START)],
        gate=None,
        as_of=date(2026, 7, 24),
        start_session=START,
    )

    assert assessment.phase is CohortPhase.SCHEDULED
    assert assessment.official_observations == 0
    assert assessment.latest_observation_date is None


def test_collecting_counts_only_due_sessions():
    due = sessions(12)
    runs = [make_run(day) for day in due]
    runs.append(make_run(date(2026, 9, 30), status=SleeveRunStatus.PENDING))
    assessment = cohort_phase.assess_cohort_phase(
        cohort_id=COHORT,
        runs=runs,
        observations=[make_observation(MEMBERS[0], day) for day in due],
        gate=None,
        as_of=due[-1],
    )
    assert assessment.phase is CohortPhase.COLLECTING
    assert assessment.due_sessions == 12
    assert assessment.completed_due_sessions == 12
    assert assessment.completion_reliability == 1.0
    assert assessment.sessions_remaining == 18
    assert assessment.next_scheduled_session == date(2026, 9, 30)
    assert "18 more due sessions" in assessment.next_action


def test_partial_due_run_raises_an_integrity_alert():
    due = sessions(5)
    runs = [make_run(day) for day in due[:-1]]
    runs.append(
        make_run(
            due[-1],
            status=SleeveRunStatus.PARTIAL,
            completed=MEMBERS[:2],
            errors=(SleeveRunError(code="data-not-ready", message="quotes stale"),),
        )
    )
    assessment = cohort_phase.assess_cohort_phase(
        cohort_id=COHORT, runs=runs, observations=[], gate=None, as_of=due[-1]
    )
    assert assessment.phase is CohortPhase.ATTENTION_NEEDED
    assert len(assessment.integrity_alerts) == 1
    assert "partial" in assessment.integrity_alerts[0]
    assert assessment.next_action.startswith("Investigate:")


def test_review_ready_at_the_target_without_genuine_failures():
    due = sessions(30)
    assessment = cohort_phase.assess_cohort_phase(
        cohort_id=COHORT,
        runs=[make_run(day) for day in due],
        observations=[],
        gate=None,
        as_of=due[-1],
    )
    assert assessment.phase is CohortPhase.REVIEW_READY
    assert assessment.sessions_remaining == 0
    assert assessment.progress_ratio == 1.0


def test_other_cohorts_are_ignored_by_identity():
    foreign = make_run(START).model_copy(update={"cohort_id": "other-cohort"})
    assessment = cohort_phase.assess_cohort_phase(
        cohort_id=COHORT, runs=[foreign], observations=[], gate=None, as_of=START
    )
    assert assessment.due_sessions == 0
    assert assessment.phase is CohortPhase.SCHEDULED


def test_review_target_must_be_positive():
    with pytest.raises(ValueError, match="review_target must be positive"):
        cohort_phase.assess_cohort_phase(
            cohort_id=COHORT,
            runs=[],
            observations=[],
            gate=None,
            as_of=START,
            review_target=0,
        )


# --- Eastern-time evidence timing -------------------------------------------
#
# A session's official decision time is the exchange close, so a run persisted as
# pending is only "missing" once the close *and* the scheduler's grace period have
# passed. These cases pin the clock either side of both boundaries.


def phase_at(
    now_et: datetime,
    runs: list[SleeveRun],
    observations: list[OfficialDailyObservation] | None = None,
    *,
    gate: operational_gate.OperationalGateResult | None = None,
    policy: scheduling.SchedulingPolicy = scheduling.DEFAULT_POLICY,
) -> cohort_phase.CohortPhaseAssessment:
    return cohort_phase.assess_cohort_phase(
        cohort_id=COHORT,
        runs=runs,
        observations=observations or [],
        gate=gate,
        now_et=now_et,
        policy=policy,
    )


@pytest.mark.parametrize("now_et", [MORNING, ONE_MINUTE_BEFORE_CLOSE])
def test_pre_close_session_is_upcoming_not_due(now_et: datetime):
    """9:28 AM and 3:59 PM ET on the start date: nothing is owed yet."""
    assessment = phase_at(now_et, [make_run(START, status=SleeveRunStatus.PENDING)])

    assert assessment.phase is CohortPhase.SCHEDULED
    assert assessment.timing_state is SessionTimingState.UPCOMING
    assert assessment.awaiting_evidence is True
    assert assessment.due_sessions == 0
    assert assessment.completed_due_sessions == 0
    # The missing seven observations are not unexplained slots.
    assert assessment.completion_reliability is None
    assert assessment.integrity_alerts == ()
    assert assessment.started is False
    # The latest session that actually closed is the previous Friday.
    assert assessment.evidence_cutoff == date(2026, 7, 24)
    assert assessment.headline == "Starts July 27, 2026"
    assert assessment.next_action == "Run the cohort after the July 27, 2026 market close."


def test_just_after_the_close_the_run_is_due_not_failed():
    assessment = phase_at(JUST_AFTER_CLOSE, [make_run(START, status=SleeveRunStatus.PENDING)])

    assert assessment.timing_state is SessionTimingState.AWAITING_EXECUTION
    assert assessment.awaiting_execution_session == START
    assert assessment.overdue_sessions == ()
    # Not yet evidence: the scheduler simply has not run.
    assert assessment.due_sessions == 0
    assert assessment.completion_reliability is None
    assert assessment.integrity_alerts == ()
    assert assessment.phase is not CohortPhase.ATTENTION_NEEDED
    assert assessment.headline == "Run due — awaiting execution"
    assert assessment.next_action == "Run today's cohort for the July 27, 2026 session."


def test_a_pending_run_past_the_grace_period_needs_attention():
    assessment = phase_at(PAST_GRACE, [make_run(START, status=SleeveRunStatus.PENDING)])

    assert assessment.timing_state is SessionTimingState.OVERDUE
    assert assessment.overdue_sessions == (START,)
    # An overdue session *is* a due session that produced nothing.
    assert assessment.due_sessions == 1
    assert assessment.completed_due_sessions == 0
    assert assessment.completion_reliability == 0.0
    assert assessment.phase is CohortPhase.ATTENTION_NEEDED
    assert assessment.headline == "Run overdue"
    assert "never executed" in assessment.integrity_alerts[0]
    assert assessment.next_action.startswith("Investigate:")


def test_awaiting_provider_data_past_grace_is_retryable_until_the_deadline():
    assessment = phase_at(PAST_GRACE, [make_run(START, status=SleeveRunStatus.AWAITING_DATA)])

    assert assessment.timing_state is SessionTimingState.AWAITING_PROVIDER_DATA
    assert assessment.awaiting_execution_session is None
    assert assessment.awaiting_provider_session == START
    assert assessment.overdue_sessions == ()
    assert assessment.awaiting_evidence is True
    assert assessment.due_sessions == 0
    assert assessment.completed_due_sessions == 0
    assert assessment.completion_reliability is None
    assert assessment.phase is CohortPhase.SCHEDULED
    assert assessment.headline == "Awaiting provider data — retryable"
    assert assessment.next_action == "Await provider data for the July 27, 2026 session."


def test_the_grace_period_is_configurable():
    tight = scheduling.SchedulingPolicy(grace_period=timedelta(minutes=15))
    assessment = phase_at(
        JUST_AFTER_CLOSE, [make_run(START, status=SleeveRunStatus.PENDING)], policy=tight
    )
    assert assessment.timing_state is SessionTimingState.AWAITING_EXECUTION

    lapsed = phase_at(
        datetime(2026, 7, 27, 16, 30),
        [make_run(START, status=SleeveRunStatus.PENDING)],
        policy=tight,
    )
    assert lapsed.timing_state is SessionTimingState.OVERDUE


def test_a_completed_post_close_run_is_one_completed_due_session():
    assessment = phase_at(
        datetime(2026, 7, 27, 16, 20),
        [make_run(START)],
        [make_observation(sleeve, START) for sleeve in MEMBERS],
    )

    assert assessment.timing_state is SessionTimingState.SETTLED
    assert assessment.due_sessions == 1
    assert assessment.completed_due_sessions == 1
    assert assessment.completion_reliability == 1.0
    assert assessment.official_observations == 3
    assert assessment.latest_observation_date == START
    assert assessment.phase is CohortPhase.COLLECTING
    assert assessment.integrity_alerts == ()


@pytest.mark.parametrize("status", [SleeveRunStatus.PARTIAL, SleeveRunStatus.FAILED])
def test_an_executed_run_that_did_not_finish_needs_attention(status: SleeveRunStatus):
    assessment = phase_at(
        datetime(2026, 7, 27, 16, 20),
        [make_run(START, status=status, completed=MEMBERS[:1])],
    )

    assert assessment.due_sessions == 1
    assert assessment.completed_due_sessions == 0
    assert assessment.phase is CohortPhase.ATTENTION_NEEDED
    assert status.value in assessment.integrity_alerts[0]


def test_a_completed_run_missing_members_is_contradictory_evidence():
    assessment = phase_at(
        datetime(2026, 7, 27, 16, 20),
        [make_run(START, completed=MEMBERS[:2])],
    )
    assert assessment.phase is CohortPhase.ATTENTION_NEEDED
    assert "recorded complete but" in assessment.integrity_alerts[0]


@pytest.mark.parametrize(
    ("now_et", "expected"),
    [
        (datetime(2026, 11, 27, 12, 59), SessionTimingState.UPCOMING),
        (datetime(2026, 11, 27, 13, 5), SessionTimingState.AWAITING_EXECUTION),
    ],
)
def test_an_early_close_session_pivots_at_one_oclock(
    now_et: datetime, expected: SessionTimingState
):
    assessment = phase_at(now_et, [make_run(EARLY_CLOSE, status=SleeveRunStatus.PENDING)])
    assert assessment.timing_state is expected
    assert assessment.integrity_alerts == ()


def test_a_weekend_clock_does_not_make_mondays_session_due():
    """Saturday morning: Friday was the last close and Monday is still upcoming."""
    assessment = phase_at(
        datetime(2026, 8, 1, 10, 0), [make_run(date(2026, 8, 3), status=SleeveRunStatus.PENDING)]
    )
    assert assessment.timing_state is SessionTimingState.UPCOMING
    assert assessment.evidence_cutoff == date(2026, 7, 31)
    assert assessment.due_sessions == 0


def test_a_run_scheduled_on_a_holiday_is_neither_due_nor_overdue():
    assessment = phase_at(
        datetime(2026, 7, 6, 12, 0), [make_run(HOLIDAY, status=SleeveRunStatus.PENDING)]
    )
    assert assessment.timing_state is SessionTimingState.SETTLED
    assert assessment.due_sessions == 0
    assert assessment.integrity_alerts == ()


def test_future_observations_stay_excluded_under_the_eastern_clock():
    assessment = phase_at(
        MORNING,
        [make_run(START, status=SleeveRunStatus.PENDING)],
        [make_observation(MEMBERS[0], START)],
    )
    assert assessment.official_observations == 0
    assert assessment.latest_observation_date is None


def test_assess_cohort_phase_requires_a_clock():
    with pytest.raises(ValueError, match="requires now_et"):
        cohort_phase.assess_cohort_phase(cohort_id=COHORT, runs=[], observations=[], gate=None)


# --- Rule presentation ------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "awaiting", "phase", "expected"),
    [
        (GateStatus.PASS, False, CohortPhase.SCHEDULED, RulePresentation.HEALTHY),
        (GateStatus.FAIL, True, CohortPhase.SCHEDULED, RulePresentation.AWAITING_EVIDENCE),
        (GateStatus.FAIL, True, CohortPhase.COLLECTING, RulePresentation.AWAITING_EVIDENCE),
        # Once the review is owed, unrecorded evidence becomes actionable.
        (GateStatus.FAIL, True, CohortPhase.REVIEW_READY, RulePresentation.NEEDS_ATTENTION),
        # A real defect is actionable no matter how young the cohort is.
        (GateStatus.FAIL, False, CohortPhase.SCHEDULED, RulePresentation.NEEDS_ATTENTION),
        (
            GateStatus.INSUFFICIENT_HISTORY,
            False,
            CohortPhase.COLLECTING,
            RulePresentation.AWAITING_EVIDENCE,
        ),
    ],
)
def test_presentation_separates_awaiting_evidence_from_failure(
    status: GateStatus, awaiting: bool, phase: CohortPhase, expected: RulePresentation
):
    rule = operational_gate.RuleAssessment(
        rule=GateRule.ACCOUNTING_STATES,
        status=status,
        reason="reason",
        evidence=(),
        awaiting_evidence=awaiting,
    )
    assert cohort_phase.presentation_for(rule, phase) is expected


def test_every_gate_rule_has_a_plain_language_label():
    for rule in GateRule:
        label = operational_gate.GATE_RULE_LABELS[rule]
        assert label and label[0].isupper()
    assert operational_gate.GATE_RULE_LABELS[GateRule.COMPLETION_RATE] == "Completion reliability"
    assert operational_gate.GATE_RULE_LABELS[GateRule.ACCOUNTING_STATES] == "Paper accounting"
    assert (
        operational_gate.GATE_RULE_LABELS[GateRule.DISTINCT_BEHAVIOR]
        == "Distinct strategy behavior"
    )


# --- Injected clock in the gate ---------------------------------------------


def _cohort() -> ExperimentCohort:
    return ExperimentCohort(
        cohort_id=COHORT,
        name=COHORT,
        created_at=datetime(2026, 7, 24, tzinfo=UTC),
        start_session=START,
        starting_cash_per_sleeve=10_000,
        settlement_model="T+1",
        leverage=1,
        benchmark_sleeve=MEMBERS[0],
        decision_schedule="daily@16:10",
        cost_model_id="modeled-v1",
        member_sleeves=MEMBERS,
        status="persisted",
    )


def _assess(runs: list[SleeveRun], *, as_of: date | None):
    return operational_gate.assess_operational_usefulness(
        cohort=_cohort(),
        runs=runs,
        observations=[],
        sleeve_configs=[],
        recorded_comparison=None,
        accounting_evidence=None,
        operator_decisions=None,
        as_of=as_of,
    )


def test_as_of_excludes_future_runs_from_completion_evidence():
    runs = [make_run(START, status=SleeveRunStatus.PENDING)]

    without_clock = _assess(runs, as_of=None)
    with_clock = _assess(runs, as_of=date(2026, 7, 24))

    # Without a clock the pending future session reads as unexplained missing slots.
    assert without_clock.rule(GateRule.COMPLETION_RATE).status is GateStatus.FAIL
    # With the clock injected it is simply not due yet.
    completion = with_clock.rule(GateRule.COMPLETION_RATE)
    assert completion.status is GateStatus.INSUFFICIENT_HISTORY
    assert completion.awaiting_evidence is True


def test_as_of_excludes_future_observations_from_all_gate_evidence():
    future = make_observation(MEMBERS[0], START)

    without_clock = operational_gate.assess_operational_usefulness(
        cohort=_cohort(),
        runs=[make_run(START, status=SleeveRunStatus.PENDING)],
        observations=[future, future],
        sleeve_configs=[],
        recorded_comparison=None,
        accounting_evidence=None,
        operator_decisions=None,
    )
    with_clock = operational_gate.assess_operational_usefulness(
        cohort=_cohort(),
        runs=[make_run(START, status=SleeveRunStatus.PENDING)],
        observations=[future, future],
        sleeve_configs=[],
        recorded_comparison=None,
        accounting_evidence=None,
        operator_decisions=None,
        as_of=date(2026, 7, 24),
    )

    assert without_clock.rule(GateRule.DUPLICATE_OBSERVATIONS).status is GateStatus.FAIL
    duplicates = with_clock.rule(GateRule.DUPLICATE_OBSERVATIONS)
    assert duplicates.status is GateStatus.PASS
    readiness = with_clock.rule(GateRule.DATA_READINESS)
    assert readiness.awaiting_evidence is True
    assert "observations checked: 0" in readiness.evidence


def test_as_of_never_relaxes_the_session_history_floor():
    due = sessions(30)
    runs = [make_run(day) for day in due]
    runs.append(make_run(date(2026, 12, 1), status=SleeveRunStatus.PENDING))

    # Counting the future run would reach 31 sessions; the clock keeps it at 30.
    with_clock = _assess(runs, as_of=due[-1])
    assert "observed scheduled sessions: 30" in with_clock.rule(GateRule.SESSION_HISTORY).evidence

    earlier = _assess(runs, as_of=due[9])
    history = earlier.rule(GateRule.SESSION_HISTORY)
    assert history.status is GateStatus.INSUFFICIENT_HISTORY
    assert history.awaiting_evidence is True


def test_gate_still_fails_closed_for_an_unstarted_cohort():
    result = _assess([make_run(START, status=SleeveRunStatus.PENDING)], as_of=date(2026, 7, 24))
    assert result.status is GateStatus.FAIL
    assert result.operationally_useful is False
    assert result.investment_alpha_assessed is False
    assert result.live_trading_authorized is False
    # ...but no *evidence-bearing* rule rests on real negative evidence. (Reproducibility
    # is excluded here only because this minimal case supplies no sleeve definitions,
    # which is itself a genuine defect; see the fixture drift test for the real shape.)
    evidence_rules = {
        GateRule.SESSION_HISTORY,
        GateRule.COMPLETION_RATE,
        GateRule.ACCOUNTING_STATES,
        GateRule.DATA_READINESS,
        GateRule.DISTINCT_BEHAVIOR,
        GateRule.OPERATOR_DECISIONS,
    }
    assert all(result.rule(rule).awaiting_evidence for rule in evidence_rules)
    assert result.rule(GateRule.DUPLICATE_OBSERVATIONS).status is GateStatus.PASS


def test_missing_accounting_and_operator_evidence_is_marked_awaiting():
    result = _assess([make_run(day) for day in sessions(30)], as_of=sessions(30)[-1])
    assert result.rule(GateRule.ACCOUNTING_STATES).awaiting_evidence is True
    assert result.rule(GateRule.OPERATOR_DECISIONS).awaiting_evidence is True


def _assess_at(
    now_et: datetime,
    runs: list[SleeveRun],
    observations: list[OfficialDailyObservation] | None = None,
):
    return operational_gate.assess_operational_usefulness(
        cohort=_cohort(),
        runs=runs,
        observations=observations or [],
        sleeve_configs=[],
        recorded_comparison=None,
        accounting_evidence=None,
        operator_decisions=None,
        now_et=now_et,
    )


@pytest.mark.parametrize("now_et", [MORNING, ONE_MINUTE_BEFORE_CLOSE, JUST_AFTER_CLOSE])
def test_a_session_that_has_not_delivered_is_never_a_completion_gap(now_et: datetime):
    """Pre-close and inside the grace period, the pending run is not counted at all."""
    result = _assess_at(now_et, [make_run(START, status=SleeveRunStatus.PENDING)])

    completion = result.rule(GateRule.COMPLETION_RATE)
    assert completion.status is GateStatus.INSUFFICIENT_HISTORY
    assert completion.awaiting_evidence is True
    assert "expected observation slots: 0" in completion.evidence
    # No delivered run means no snapshot lineage is owed. (This minimal case supplies
    # no sleeve definitions, which is a genuine defect of its own, so only the lineage
    # complaint is asserted here; the fixture test covers a fully-defined cohort.)
    reproducibility = result.rule(GateRule.REPRODUCIBILITY)
    assert "snapshot lineage missing" not in " ".join(reproducibility.evidence)


def test_provider_wait_is_awaiting_evidence_even_after_the_preferred_grace():
    result = _assess_at(PAST_GRACE, [make_run(START, status=SleeveRunStatus.AWAITING_DATA)])

    completion = result.rule(GateRule.COMPLETION_RATE)
    assert completion.status is GateStatus.INSUFFICIENT_HISTORY
    assert completion.awaiting_evidence is True
    reproducibility = result.rule(GateRule.REPRODUCIBILITY)
    assert "snapshot lineage missing" not in " ".join(reproducibility.evidence)


def test_past_the_grace_period_the_missing_session_becomes_real_negative_evidence():
    result = _assess_at(PAST_GRACE, [make_run(START, status=SleeveRunStatus.PENDING)])
    completion = result.rule(GateRule.COMPLETION_RATE)
    assert completion.status is GateStatus.FAIL
    assert completion.awaiting_evidence is False


def test_a_real_reproducibility_defect_still_fails_under_the_eastern_clock():
    """An executed run with no snapshot lineage is a defect, not missing evidence."""
    broken = make_run(START).model_copy(update={"snapshot_id": None, "quote_snapshot_id": None})
    result = _assess_at(datetime(2026, 7, 27, 16, 20), [broken])

    rule = result.rule(GateRule.REPRODUCIBILITY)
    assert rule.status is GateStatus.FAIL
    assert rule.awaiting_evidence is False
    assert any("snapshot lineage missing" in item for item in rule.evidence)
    assert (
        cohort_phase.presentation_for(rule, CohortPhase.COLLECTING)
        is RulePresentation.NEEDS_ATTENTION
    )


def test_duplicate_observations_still_fail_under_the_eastern_clock():
    duplicate = make_observation(MEMBERS[0], START)
    result = _assess_at(datetime(2026, 7, 27, 16, 20), [make_run(START)], [duplicate, duplicate])
    rule = result.rule(GateRule.DUPLICATE_OBSERVATIONS)
    assert rule.status is GateStatus.FAIL
    assert rule.awaiting_evidence is False


@pytest.mark.parametrize("now_et", [MORNING, JUST_AFTER_CLOSE, PAST_GRACE])
def test_the_gate_never_authorizes_anything_at_any_clock(now_et: datetime):
    """Timing is presentation. It can never become permission to trade."""
    result = _assess_at(now_et, [make_run(START, status=SleeveRunStatus.PENDING)])

    assert result.investment_alpha_assessed is False
    assert result.live_trading_authorized is False
    assert result.operationally_useful is False
    assert result.status is GateStatus.FAIL


def test_duplicate_observations_are_never_merely_awaiting_evidence():
    session = START
    duplicate = make_observation(MEMBERS[0], session)
    result = operational_gate.assess_operational_usefulness(
        cohort=_cohort(),
        runs=[make_run(session)],
        observations=[duplicate, duplicate],
        sleeve_configs=[],
        recorded_comparison=None,
        accounting_evidence=None,
        operator_decisions=None,
        as_of=session,
    )
    rule = result.rule(GateRule.DUPLICATE_OBSERVATIONS)
    assert rule.status is GateStatus.FAIL
    assert rule.awaiting_evidence is False
