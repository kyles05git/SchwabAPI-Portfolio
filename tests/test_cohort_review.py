"""Offline tests for durable cohort accounting reviews and operator decisions.

Every database here is a throwaway SQLite file under ``tmp_path`` built from the models.
Nothing reads ``.env``, opens a shared database, reaches a network, or consults the wall
clock: each case pins an explicit naive Eastern ``now_et`` and an explicit aware
``recorded_at``.

The properties under test are the ones the record exists for: append-only history,
idempotent retries, validation that fails closed with no partial write, and evidence
that reaches the operational gate only when it was genuinely recorded.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from schwab_trader import cohort_review
from schwab_trader.cohort_review import (
    CohortReviewService,
    ReviewConflictError,
    ReviewFinding,
    ReviewNotDueError,
    ReviewValidationError,
    WriteStatus,
)
from schwab_trader.evaluation import (
    ObservationStatus,
    OfficialDailyObservation,
    official_observation_key,
)
from schwab_trader.experiments import ExperimentCohort, StrategyDefinition
from schwab_trader.operational_gate import (
    AccountingArea,
    GateRule,
    GateStatus,
    OperatorAction,
    assess_operational_usefulness,
)
from schwab_trader.sleeve_runs import (
    MemberRunStatus,
    SleeveRun,
    SleeveRunMember,
    SleeveRunStatus,
)
from schwab_trader.sleeves import SleeveConfig
from schwab_trader.storage.cohort_reviews import SqlAlchemyCohortReviewStore
from schwab_trader.storage.database import Database

COHORT = "paper-test-2026-07-27"
OTHER_COHORT = "paper-other-2026-07-27"
START = date(2026, 7, 27)
MEMBERS = ("bench-spy", "trend-large", "value-edgar")
RECORDED_AT = datetime(2026, 9, 4, 22, 30, tzinfo=UTC)

#: Well past thirty completed sessions, so the review is due under the real clock rule.
THIRTY_IN = datetime(2026, 9, 4, 18, 0)
#: Two sessions in: enough evidence to review an observation, far too little to decide.
TWO_IN = datetime(2026, 7, 28, 18, 0)


# --- builders ---------------------------------------------------------------


def _definition(name: str) -> StrategyDefinition:
    return StrategyDefinition(
        strategy_id="trend-v1",
        implementation_name="trend",
        strategy_version="1.0.0",
        parameters={"sleeve": name},
        universe_definition={"preset": "mega-cap"},
        decision_frequency="daily",
        decision_time="16:10",
        benchmark_symbol_or_sleeve="bench-spy",
        data_requirements=["daily-bars"],
        long_only=True,
        leverage_allowed=False,
    )


def _config(name: str, *, cohort_id: str = COHORT) -> SleeveConfig:
    definition = _definition(name)
    return SleeveConfig(
        name=name,
        strategy="trend",
        universe=[],
        starting_cash=Decimal(10_000),
        max_positions=10,
        max_position_fraction=Decimal("0.2"),
        created_at=datetime(2026, 7, 24, 15, 0, tzinfo=UTC),
        definition=definition,
        cohort_id=cohort_id,
        configuration_hash=definition.configuration_hash,
        decision_frequency="daily",
        decision_time="16:10",
    )


def _configs(cohort_id: str = COHORT) -> list[SleeveConfig]:
    return [_config(name, cohort_id=cohort_id) for name in MEMBERS]


def _sessions(count: int) -> list[date]:
    from schwab_trader import market_calendar as mc

    days: list[date] = []
    cursor = START
    while len(days) < count:
        if mc.is_trading_day(cursor):
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _run(session: date, cohort_id: str = COHORT) -> SleeveRun:
    started = datetime.combine(session, datetime.min.time(), tzinfo=UTC).replace(hour=20)
    return SleeveRun(
        run_id=f"run-{cohort_id}-{session.isoformat()}",
        run_key=f"{cohort_id}|{session.isoformat()}",
        cohort_id=cohort_id,
        session_id=f"XNYS|{session.isoformat()}",
        scheduled_for=session,
        expected_members=MEMBERS,
        completed_members=MEMBERS,
        snapshot_id=f"snap-{session.isoformat()}",
        quote_snapshot_id=f"quote-{session.isoformat()}",
        started_at=started,
        completed_at=started + timedelta(minutes=4),
        status=SleeveRunStatus.COMPLETED,
        members=tuple(
            SleeveRunMember(
                sleeve_id=name,
                status=MemberRunStatus.COMPLETED,
                started_at=started,
                completed_at=started + timedelta(minutes=3),
            )
            for name in MEMBERS
        ),
    )


def _observation(
    name: str,
    session: date,
    *,
    cohort_id: str = COHORT,
    status: ObservationStatus = ObservationStatus.OFFICIAL,
) -> OfficialDailyObservation:
    official = status is ObservationStatus.OFFICIAL
    decision = datetime.combine(session, datetime.min.time(), tzinfo=UTC).replace(hour=20)
    return OfficialDailyObservation(
        cohort_id=cohort_id,
        run_id=f"run-{cohort_id}-{session.isoformat()}",
        sleeve_id=name,
        strategy="trend",
        strategy_hash=_definition(name).configuration_hash,
        session_date=session,
        decision_time=decision,
        valuation_time=decision + timedelta(minutes=5),
        status=status,
        total_value=Decimal("10050.00") if official else None,
        return_pct=Decimal("0.005") if official else None,
        exposure=Decimal("0.90"),
        num_positions=6,
        num_filled=2,
        num_rejected=0,
        snapshot_ids={
            "cohort_snapshot": f"snap-{session.isoformat()}",
            "quotes": f"quote-{session.isoformat()}",
        },
        readiness_ready=official,
        readiness_reasons=() if official else ("quotes:stale",),
    )


def _records(
    sessions: int, *, cohort_id: str = COHORT
) -> tuple[list[SleeveRun], list[OfficialDailyObservation]]:
    days = _sessions(sessions)
    runs = [_run(day, cohort_id) for day in days]
    observations = [
        _observation(name, day, cohort_id=cohort_id) for day in days for name in MEMBERS
    ]
    return runs, observations


def _context(
    sessions: int = 30,
    *,
    now_et: datetime = THIRTY_IN,
    cohort_id: str = COHORT,
    configs: list[SleeveConfig] | None = None,
    observations: list[OfficialDailyObservation] | None = None,
) -> cohort_review.ReviewContext:
    runs, built = _records(sessions, cohort_id=cohort_id)
    return cohort_review.build_context(
        cohort_id=cohort_id,
        configs=configs if configs is not None else _configs(cohort_id),
        runs=runs,
        observations=built if observations is None else observations,
        now_et=now_et,
        start_session=START,
    )


@pytest.fixture
def store(tmp_path: Path) -> SqlAlchemyCohortReviewStore:
    """A disposable SQLite review store built from the models."""
    database = Database(f"sqlite:///{tmp_path / 'reviews.sqlite3'}", create_schema=True)
    return SqlAlchemyCohortReviewStore(database)


@pytest.fixture
def service(store: SqlAlchemyCohortReviewStore) -> CohortReviewService:
    return CohortReviewService(store)


def _key(name: str, session: date, cohort_id: str = COHORT) -> str:
    return official_observation_key(cohort_id, name, session)


def _review_everything(
    service: CohortReviewService, context: cohort_review.ReviewContext
) -> None:
    """Record a clean matched check for every area of every official observation."""
    for key in sorted(context.observations):
        for area in cohort_review.REQUIRED_AREAS:
            service.record_check(
                context,
                observation_key=key,
                area=area,
                finding=ReviewFinding.MATCHED,
                recorded_at=RECORDED_AT,
            )


# --- context ----------------------------------------------------------------


def test_context_only_admits_official_due_observations_of_member_sleeves() -> None:
    days = _sessions(2)
    runs = [_run(day) for day in days]
    observations = [
        _observation(MEMBERS[0], days[0]),
        # Not official: an incomplete session is not evidence and is not reviewable.
        _observation(MEMBERS[1], days[0], status=ObservationStatus.PARTIAL),
        # Not a member of this cohort.
        _observation("stranger", days[0]),
        # Another cohort's record entirely.
        _observation(MEMBERS[0], days[0], cohort_id=OTHER_COHORT),
        _observation(MEMBERS[0], days[1]),
    ]
    context = cohort_review.build_context(
        cohort_id=COHORT,
        configs=_configs(),
        runs=runs,
        observations=observations,
        now_et=TWO_IN,
        start_session=START,
    )

    assert set(context.observations) == {
        _key(MEMBERS[0], days[0]),
        _key(MEMBERS[0], days[1]),
    }
    assert context.member_sleeve_ids == set(MEMBERS)


def test_context_excludes_a_session_whose_close_has_not_happened() -> None:
    days = _sessions(2)
    runs = [_run(day) for day in days]
    observations = [_observation(MEMBERS[0], day) for day in days]
    # 09:28 ET on the second session: its 16:00 close has not occurred, so it cannot
    # have produced evidence and must not be reviewable.
    morning_of_second = datetime.combine(days[1], datetime.min.time()).replace(hour=9, minute=28)

    context = cohort_review.build_context(
        cohort_id=COHORT,
        configs=_configs(),
        runs=runs,
        observations=observations,
        now_et=morning_of_second,
        start_session=START,
    )

    assert set(context.observations) == {_key(MEMBERS[0], days[0])}


def test_review_is_due_only_at_the_thirty_session_target() -> None:
    assert _context(29).review_due is False
    assert _context(30).review_due is True
    assert _context(30).review_target == 30


# --- recording accounting checks --------------------------------------------


def test_recording_a_check_is_idempotent(service: CohortReviewService) -> None:
    context = _context(2, now_et=TWO_IN)
    key = _key(MEMBERS[0], _sessions(2)[0])

    first = service.record_check(
        context,
        observation_key=key,
        area=AccountingArea.CASH,
        finding=ReviewFinding.MATCHED,
        recorded_at=RECORDED_AT,
    )
    # A later clock must not turn a retry into a correction: the finding is the content.
    second = service.record_check(
        context,
        observation_key=key,
        area=AccountingArea.CASH,
        finding=ReviewFinding.MATCHED,
        recorded_at=RECORDED_AT + timedelta(hours=3),
    )

    assert first.status is WriteStatus.RECORDED
    assert second.status is WriteStatus.UNCHANGED
    assert second.record.entry_id == first.record.entry_id
    assert second.record.revision == 0
    assert len(service.repository.checks(COHORT)) == 1


def test_changing_a_check_requires_an_explicit_supersede(
    service: CohortReviewService,
) -> None:
    context = _context(2, now_et=TWO_IN)
    key = _key(MEMBERS[0], _sessions(2)[0])
    service.record_check(
        context,
        observation_key=key,
        area=AccountingArea.CASH,
        finding=ReviewFinding.MATCHED,
        recorded_at=RECORDED_AT,
    )

    with pytest.raises(ReviewConflictError):
        service.record_check(
            context,
            observation_key=key,
            area=AccountingArea.CASH,
            finding=ReviewFinding.DIFFERENCE,
            summary="Settled cash trailed by 0.04.",
            recorded_at=RECORDED_AT,
        )

    # The refusal wrote nothing: the original entry is still the only record.
    stored = service.repository.checks(COHORT)
    assert len(stored) == 1
    assert stored[0].finding is ReviewFinding.MATCHED


def test_a_correction_appends_and_preserves_the_original(
    service: CohortReviewService,
) -> None:
    context = _context(2, now_et=TWO_IN)
    key = _key(MEMBERS[0], _sessions(2)[0])
    original = service.record_check(
        context,
        observation_key=key,
        area=AccountingArea.CASH,
        finding=ReviewFinding.DIFFERENCE,
        summary="Settled cash trailed by 0.04.",
        recorded_at=RECORDED_AT,
    ).record

    corrected = service.record_check(
        context,
        observation_key=key,
        area=AccountingArea.CASH,
        finding=ReviewFinding.DIFFERENCE,
        summary="Settled cash trailed by 0.04.",
        explanation="T+1 settlement released it on the next session, as recorded.",
        recorded_at=RECORDED_AT + timedelta(hours=1),
        allow_supersede=True,
    )

    assert corrected.status is WriteStatus.SUPERSEDED
    assert corrected.record.revision == 1
    assert corrected.record.supersedes == original.entry_id

    stored = {item.entry_id: item for item in service.repository.checks(COHORT)}
    assert len(stored) == 2
    # The original row is byte-for-byte what it was, including its missing explanation.
    assert stored[original.entry_id].explanation is None
    assert stored[original.entry_id].summary == "Settled cash trailed by 0.04."

    review = service.review(context)
    assert [item.entry_id for item in review.checks] == [corrected.record.entry_id]
    assert [item.entry_id for item in review.superseded_checks] == [original.entry_id]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"observation_key": "nope"}, "official observation"),
        ({"finding": ReviewFinding.DIFFERENCE, "summary": "   "}, "summary"),
        ({"explanation": "why"}, "matched area"),
        ({"recorded_by": "  "}, "recorded_by"),
    ],
)
def test_invalid_checks_fail_closed_without_writing(
    service: CohortReviewService, kwargs: dict[str, object], message: str
) -> None:
    context = _context(2, now_et=TWO_IN)
    call: dict[str, object] = {
        "observation_key": _key(MEMBERS[0], _sessions(2)[0]),
        "area": AccountingArea.CASH,
        "finding": ReviewFinding.MATCHED,
        "recorded_at": RECORDED_AT,
    }
    call.update(kwargs)

    with pytest.raises(ReviewValidationError, match=message):
        service.record_check(context, **call)  # type: ignore[arg-type]
    assert service.repository.checks(COHORT) == []


def test_a_naive_recorded_at_is_refused(service: CohortReviewService) -> None:
    context = _context(2, now_et=TWO_IN)

    with pytest.raises(ReviewValidationError, match="timezone-aware"):
        service.record_check(
            context,
            observation_key=_key(MEMBERS[0], _sessions(2)[0]),
            area=AccountingArea.CASH,
            finding=ReviewFinding.MATCHED,
            recorded_at=datetime(2026, 9, 4, 22, 30),
        )
    assert service.repository.checks(COHORT) == []


@pytest.mark.parametrize("value", ["equity", "CASHFLOW", "", "position", "cash,positions"])
def test_unknown_accounting_areas_are_refused(value: str) -> None:
    with pytest.raises(ReviewValidationError, match="accounting area"):
        cohort_review.parse_area(value)


@pytest.mark.parametrize("value", ["promote", "delete", "", "keep!"])
def test_unknown_operator_actions_are_refused(value: str) -> None:
    with pytest.raises(ReviewValidationError, match="operator action"):
        cohort_review.parse_action(value)


def test_known_areas_and_actions_parse_case_insensitively() -> None:
    assert cohort_review.parse_area(" CASH ") is AccountingArea.CASH
    assert cohort_review.parse_action("Retire") is OperatorAction.RETIRE
    assert cohort_review.parse_finding("DIFFERENCE") is ReviewFinding.DIFFERENCE


# --- coverage ---------------------------------------------------------------


def test_an_observation_counts_as_reviewed_only_when_every_area_is_checked(
    service: CohortReviewService,
) -> None:
    context = _context(1, now_et=datetime(2026, 7, 27, 18, 0))
    key = _key(MEMBERS[0], START)
    for area in (AccountingArea.CASH, AccountingArea.POSITIONS):
        service.record_check(
            context,
            observation_key=key,
            area=area,
            finding=ReviewFinding.MATCHED,
            recorded_at=RECORDED_AT,
        )

    partial = service.review(context)
    pending = {item.observation_key: item for item in partial.pending_observations}
    assert key in pending
    assert pending[key].missing_areas == (AccountingArea.VALUATION,)
    assert key not in partial.covered_observation_keys

    service.record_check(
        context,
        observation_key=key,
        area=AccountingArea.VALUATION,
        finding=ReviewFinding.MATCHED,
        recorded_at=RECORDED_AT,
    )
    complete = service.review(context)
    assert key in complete.covered_observation_keys
    assert all(item.observation_key != key for item in complete.pending_observations)


# --- notes ------------------------------------------------------------------


def test_an_identical_note_is_recorded_once(service: CohortReviewService) -> None:
    context = _context(2, now_et=TWO_IN)

    first = service.add_note(context, note="Traced the difference.", recorded_at=RECORDED_AT)
    second = service.add_note(
        context, note="Traced the difference.", recorded_at=RECORDED_AT + timedelta(days=1)
    )
    third = service.add_note(context, note="Traced it again.", recorded_at=RECORDED_AT)

    assert first.status is WriteStatus.RECORDED
    assert second.status is WriteStatus.UNCHANGED
    assert third.status is WriteStatus.RECORDED
    assert len(service.repository.notes(COHORT)) == 2


def test_a_note_cannot_name_a_sleeve_outside_the_cohort(
    service: CohortReviewService,
) -> None:
    context = _context(2, now_et=TWO_IN)

    with pytest.raises(ReviewValidationError, match="not a member"):
        service.add_note(
            context, note="About a stranger.", sleeve_id="stranger", recorded_at=RECORDED_AT
        )
    assert service.repository.notes(COHORT) == []


def test_a_blank_note_is_refused(service: CohortReviewService) -> None:
    context = _context(2, now_et=TWO_IN)

    with pytest.raises(ReviewValidationError, match="note"):
        service.add_note(context, note="   ", recorded_at=RECORDED_AT)
    assert service.repository.notes(COHORT) == []


# --- operator decisions -----------------------------------------------------


def test_a_decision_before_the_review_is_due_is_refused(
    service: CohortReviewService,
) -> None:
    context = _context(2, now_et=TWO_IN)

    with pytest.raises(ReviewNotDueError, match="not due"):
        service.record_decision(
            context,
            sleeve_id=MEMBERS[1],
            action=OperatorAction.KEEP,
            rationale="Looks fine so far.",
            recorded_at=RECORDED_AT,
        )
    assert service.repository.decisions(COHORT) == []


def test_a_decision_needs_a_rationale_and_a_member_sleeve(
    service: CohortReviewService,
) -> None:
    context = _context(30)

    with pytest.raises(ReviewValidationError, match="rationale"):
        service.record_decision(
            context,
            sleeve_id=MEMBERS[1],
            action=OperatorAction.KEEP,
            rationale="   ",
            recorded_at=RECORDED_AT,
        )
    with pytest.raises(ReviewValidationError, match="not a member"):
        service.record_decision(
            context,
            sleeve_id="stranger",
            action=OperatorAction.KEEP,
            rationale="Reasoned.",
            recorded_at=RECORDED_AT,
        )
    assert service.repository.decisions(COHORT) == []


def test_a_decision_correction_supersedes_without_deleting(
    service: CohortReviewService,
) -> None:
    context = _context(30)
    first = service.record_decision(
        context,
        sleeve_id=MEMBERS[1],
        action=OperatorAction.KEEP,
        rationale="Operationally sound over thirty sessions.",
        recorded_at=RECORDED_AT,
    ).record

    with pytest.raises(ReviewConflictError):
        service.record_decision(
            context,
            sleeve_id=MEMBERS[1],
            action=OperatorAction.MODIFY,
            rationale="Turnover dominates the result.",
            recorded_at=RECORDED_AT,
        )

    second = service.record_decision(
        context,
        sleeve_id=MEMBERS[1],
        action=OperatorAction.MODIFY,
        rationale="Turnover dominates the result.",
        recorded_at=RECORDED_AT + timedelta(hours=1),
        allow_supersede=True,
    )

    assert second.status is WriteStatus.SUPERSEDED
    assert second.record.revision == 1
    assert second.record.supersedes == first.decision_id

    review = service.review(context)
    assert [item.action for item in review.decisions] == [OperatorAction.MODIFY]
    assert [item.action for item in review.superseded_decisions] == [OperatorAction.KEEP]
    # The gate must count the current decision once, never both.
    supplied = cohort_review.operator_decisions(review)
    assert supplied is not None
    assert len(supplied) == 1
    assert supplied[0].action is OperatorAction.MODIFY


def test_records_are_scoped_to_their_cohort(service: CohortReviewService) -> None:
    here = _context(30)
    elsewhere = _context(30, cohort_id=OTHER_COHORT)
    service.record_decision(
        here,
        sleeve_id=MEMBERS[0],
        action=OperatorAction.KEEP,
        rationale="This cohort only.",
        recorded_at=RECORDED_AT,
    )

    assert service.review(elsewhere).decisions == ()
    assert len(service.review(here).decisions) == 1


# --- gate evidence ----------------------------------------------------------


def _cohort_contract() -> ExperimentCohort:
    return ExperimentCohort(
        cohort_id=COHORT,
        name=COHORT,
        created_at=datetime(2026, 7, 24, 15, 0, tzinfo=UTC),
        start_session=START,
        starting_cash_per_sleeve=Decimal(10_000),
        settlement_model="T+0",
        leverage=Decimal(1),
        benchmark_sleeve=MEMBERS[0],
        decision_schedule="daily@16:10",
        cost_model_id="test",
        member_sleeves=MEMBERS,
        status="active",
    )


def _assess(review: cohort_review.CohortReview | None, sessions: int = 30):
    runs, observations = _records(sessions)
    return assess_operational_usefulness(
        cohort=_cohort_contract(),
        runs=runs,
        observations=observations,
        sleeve_configs=_configs(),
        recorded_comparison=None,
        accounting_evidence=None if review is None else cohort_review.accounting_evidence(review),
        operator_decisions=None if review is None else cohort_review.operator_decisions(review),
        now_et=THIRTY_IN,
    )


def test_an_empty_review_supplies_no_evidence_so_the_gate_still_awaits_it(
    service: CohortReviewService,
) -> None:
    """The null case. An empty review must not read as "reviewed and found clean"."""
    review = service.review(_context(30))

    assert cohort_review.accounting_evidence(review) is None
    assert cohort_review.operator_decisions(review) is None

    result = _assess(review)
    accounting = result.rule(GateRule.ACCOUNTING_STATES)
    decisions = result.rule(GateRule.OPERATOR_DECISIONS)
    assert accounting.status is GateStatus.FAIL
    assert accounting.awaiting_evidence is True
    assert decisions.status is GateStatus.FAIL
    assert decisions.awaiting_evidence is True


def test_a_complete_clean_review_satisfies_both_human_evidence_rules(
    service: CohortReviewService,
) -> None:
    context = _context(30)
    _review_everything(service, context)
    service.record_decision(
        context,
        sleeve_id=MEMBERS[1],
        action=OperatorAction.KEEP,
        rationale="Complete, reproducible, and reconciled over thirty sessions.",
        recorded_at=RECORDED_AT,
    )

    result = _assess(service.review(context))

    assert result.rule(GateRule.ACCOUNTING_STATES).status is GateStatus.PASS
    assert result.rule(GateRule.ACCOUNTING_STATES).awaiting_evidence is False
    assert result.rule(GateRule.OPERATOR_DECISIONS).status is GateStatus.PASS
    # Recording evidence never relaxes the authorization contract.
    assert result.investment_alpha_assessed is False
    assert result.live_trading_authorized is False


def test_an_unexplained_difference_keeps_the_gate_failing(
    service: CohortReviewService,
) -> None:
    context = _context(30)
    _review_everything(service, context)
    key = sorted(context.observations)[0]
    service.record_check(
        context,
        observation_key=key,
        area=AccountingArea.VALUATION,
        finding=ReviewFinding.DIFFERENCE,
        summary="Recorded equity differs from the recomputed valuation by 0.01.",
        recorded_at=RECORDED_AT,
        allow_supersede=True,
    )

    review = service.review(context)
    assert len(review.unexplained_differences) == 1

    accounting = _assess(review).rule(GateRule.ACCOUNTING_STATES)
    assert accounting.status is GateStatus.FAIL
    # A recorded, unexplained difference is a real defect, not missing evidence.
    assert accounting.awaiting_evidence is False


def test_explaining_the_difference_clears_it(service: CohortReviewService) -> None:
    context = _context(30)
    _review_everything(service, context)
    key = sorted(context.observations)[0]
    service.record_check(
        context,
        observation_key=key,
        area=AccountingArea.VALUATION,
        finding=ReviewFinding.DIFFERENCE,
        summary="Recorded equity differs from the recomputed valuation by 0.01.",
        recorded_at=RECORDED_AT,
        allow_supersede=True,
    )
    service.record_check(
        context,
        observation_key=key,
        area=AccountingArea.VALUATION,
        finding=ReviewFinding.DIFFERENCE,
        summary="Recorded equity differs from the recomputed valuation by 0.01.",
        explanation="Rounding in the recorded quote; the snapshot agrees to the cent.",
        recorded_at=RECORDED_AT + timedelta(hours=1),
        allow_supersede=True,
    )
    service.record_decision(
        context,
        sleeve_id=MEMBERS[1],
        action=OperatorAction.KEEP,
        rationale="Reconciled; the one difference is understood and documented.",
        recorded_at=RECORDED_AT,
    )

    review = service.review(context)
    assert review.unexplained_differences == ()
    assert _assess(review).rule(GateRule.ACCOUNTING_STATES).status is GateStatus.PASS


def test_partial_coverage_fails_the_gate_rather_than_passing_vacuously(
    service: CohortReviewService,
) -> None:
    context = _context(30)
    key = sorted(context.observations)[0]
    for area in cohort_review.REQUIRED_AREAS:
        service.record_check(
            context,
            observation_key=key,
            area=area,
            finding=ReviewFinding.MATCHED,
            recorded_at=RECORDED_AT,
        )

    accounting = _assess(service.review(context)).rule(GateRule.ACCOUNTING_STATES)
    assert accounting.status is GateStatus.FAIL
    assert any("unreviewed observation identities" in item for item in accounting.evidence)


# --- payload ----------------------------------------------------------------


def test_the_json_payload_carries_history_and_never_a_recorded_identity(
    service: CohortReviewService,
) -> None:
    context = _context(30)
    key = sorted(context.observations)[0]
    service.record_check(
        context,
        observation_key=key,
        area=AccountingArea.CASH,
        finding=ReviewFinding.DIFFERENCE,
        summary="Trailing 0.04.",
        recorded_at=RECORDED_AT,
    )
    service.record_check(
        context,
        observation_key=key,
        area=AccountingArea.CASH,
        finding=ReviewFinding.DIFFERENCE,
        summary="Trailing 0.04.",
        explanation="Settled next session.",
        recorded_at=RECORDED_AT + timedelta(hours=1),
        allow_supersede=True,
    )

    payload = cohort_review.review_payload(service.review(context))

    assert payload["review_due"] is True
    assert payload["unexplained_difference_count"] == 0
    assert len(payload["checks"]) == 1
    assert len(payload["superseded_checks"]) == 1
    assert payload["checks"][0]["revision"] == 1
    assert payload["superseded_checks"][0]["explanation"] is None
    assert payload["checks"][0]["recorded_by"] == cohort_review.DEFAULT_RECORDED_BY
