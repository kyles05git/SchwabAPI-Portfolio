"""Offline tests for the paper-sleeve operational-usefulness gate."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from schwab_trader.comparison import ComparisonReport, compare_sleeves
from schwab_trader.evaluation import ObservationStatus, OfficialDailyObservation
from schwab_trader.experiments import ExperimentCohort, StrategyDefinition
from schwab_trader.operational_gate import (
    AccountingArea,
    AccountingDifference,
    AccountingEvidence,
    GateRule,
    GateStatus,
    OperationalGateConfig,
    OperationalGateResult,
    OperatorAction,
    OperatorDecision,
    assess_operational_usefulness,
)
from schwab_trader.sleeve_runs import (
    MemberRunStatus,
    SleeveRun,
    SleeveRunMember,
    SleeveRunStatus,
)
from schwab_trader.sleeves import SleeveConfig

COHORT_ID = "cohort-operational"
MEMBERS = ("control-cash", "bench-spy", "candidate-alpha", "candidate-beta")
START = date(2026, 5, 1)
STAMP = datetime(2026, 5, 1, 20, tzinfo=UTC)


@dataclass(frozen=True)
class GateBundle:
    cohort: ExperimentCohort
    runs: tuple[SleeveRun, ...]
    observations: tuple[OfficialDailyObservation, ...]
    configs: tuple[SleeveConfig, ...]
    comparison: ComparisonReport
    accounting: AccountingEvidence
    decisions: tuple[OperatorDecision, ...]


def _definition(sleeve_id: str) -> StrategyDefinition:
    return StrategyDefinition(
        strategy_id=sleeve_id,
        strategy_version="1.0.0",
        implementation_name=sleeve_id,
        parameters={"lookback": 20},
        universe_definition={"symbols": ["SPY", "QQQ"]},
        benchmark_symbol_or_sleeve="bench-spy",
        decision_frequency="daily",
        decision_time=time(15, 45),
        data_requirements=("daily_bars",),
        long_only=True,
        leverage_allowed=False,
    )


def _config(sleeve_id: str) -> SleeveConfig:
    definition = _definition(sleeve_id)
    return SleeveConfig(
        name=sleeve_id,
        strategy=sleeve_id,
        universe=["SPY", "QQQ"],
        starting_cash=Decimal("10000"),
        max_positions=5,
        max_position_fraction=Decimal("0.25"),
        created_at=STAMP,
        settlement_t1=True,
        leverage=Decimal(1),
        definition=definition,
        cohort_id=COHORT_ID,
        configuration_hash=definition.configuration_hash,
        decision_frequency=definition.decision_frequency,
        decision_time=definition.decision_time.isoformat(),
    )


def _cohort() -> ExperimentCohort:
    return ExperimentCohort(
        cohort_id=COHORT_ID,
        name="Operational gate test cohort",
        created_at=STAMP,
        start_session=START,
        starting_cash_per_sleeve=Decimal("10000"),
        settlement_model="T+1",
        leverage=Decimal(1),
        benchmark_sleeve="bench-spy",
        decision_schedule="daily-close",
        cost_model_id="paper-cost-v1",
        member_sleeves=MEMBERS,
        status="active",
    )


def _value(sleeve_id: str, index: int, *, profitable: bool) -> Decimal:
    if sleeve_id == "control-cash":
        return Decimal("10000")
    if sleeve_id == "bench-spy":
        return Decimal("10000") + Decimal(index * 8)
    if sleeve_id == "candidate-alpha":
        increment = 20 if profitable else 10 + (index % 3)
        return Decimal("10000") + Decimal(index * increment)
    increment = 15 if profitable else 7 + ((index + 1) % 4)
    return Decimal("10000") + Decimal(index * increment)


def _bundle(session_count: int = 30, *, profitable: bool = False) -> GateBundle:
    cohort = _cohort()
    configs = tuple(_config(member) for member in MEMBERS)
    config_by_id = {config.name: config for config in configs}
    runs: list[SleeveRun] = []
    observations: list[OfficialDailyObservation] = []
    for index in range(session_count):
        session = START + timedelta(days=index)
        run_id = f"run-{index:03d}"
        started = STAMP + timedelta(days=index)
        snapshot_id = f"cohort-snapshot-{index:03d}"
        quote_snapshot_id = f"quote-snapshot-{index:03d}"
        data_snapshot_ids = {"daily_bars": f"bars-{index:03d}"}
        runs.append(
            SleeveRun(
                run_id=run_id,
                run_key=f"{COHORT_ID}|{session.isoformat()}",
                cohort_id=COHORT_ID,
                session_id=session.isoformat(),
                scheduled_for=session,
                expected_members=MEMBERS,
                completed_members=MEMBERS,
                snapshot_id=snapshot_id,
                quote_snapshot_id=quote_snapshot_id,
                data_snapshot_ids=data_snapshot_ids,
                started_at=started,
                completed_at=started + timedelta(minutes=5),
                status=SleeveRunStatus.COMPLETED,
                members=tuple(
                    SleeveRunMember(
                        sleeve_id=member,
                        status=MemberRunStatus.COMPLETED,
                        started_at=started,
                        completed_at=started + timedelta(minutes=5),
                    )
                    for member in MEMBERS
                ),
            )
        )
        benchmark_value = _value("bench-spy", index, profitable=profitable)
        for member in MEMBERS:
            is_alpha = member == "candidate-alpha"
            is_beta = member == "candidate-beta"
            total_value = _value(member, index, profitable=profitable)
            observations.append(
                OfficialDailyObservation(
                    cohort_id=COHORT_ID,
                    run_id=run_id,
                    sleeve_id=member,
                    strategy=member,
                    strategy_hash=config_by_id[member].configuration_hash,
                    session_date=session,
                    decision_time=started,
                    valuation_time=started + timedelta(hours=1),
                    status=ObservationStatus.OFFICIAL,
                    total_value=total_value,
                    return_pct=(total_value / Decimal("10000")) - Decimal(1),
                    benchmark_value=benchmark_value,
                    exposure=(
                        Decimal("0.80")
                        if is_alpha
                        else Decimal("0.25")
                        if is_beta
                        else Decimal("1")
                        if member == "bench-spy"
                        else Decimal(0)
                    ),
                    num_positions=5
                    if is_alpha
                    else 2
                    if is_beta
                    else 1
                    if member == "bench-spy"
                    else 0,
                    turnover=Decimal("100") if is_alpha and index % 2 == 0 else Decimal(0),
                    modeled_cost=Decimal("0.25") if is_alpha and index % 2 == 0 else Decimal(0),
                    num_filled=1 if is_alpha and index % 2 == 0 else 0,
                    quote_coverage=Decimal(1),
                    snapshot_ids={
                        "cohort_snapshot": snapshot_id,
                        "quotes": quote_snapshot_id,
                        **data_snapshot_ids,
                    },
                    readiness_ready=True,
                )
            )

    observation_tuple = tuple(observations)
    accounting = AccountingEvidence(
        checked_observation_keys=frozenset(
            observation.observation_key for observation in observation_tuple
        )
    )
    decisions = (
        OperatorDecision(
            cohort_id=COHORT_ID,
            sleeve_id="candidate-alpha",
            action=OperatorAction.KEEP,
            rationale=(
                "Keep collecting matched evidence because behavior is operationally distinct."
            ),
            recorded_at=STAMP + timedelta(days=session_count),
        ),
    )
    return GateBundle(
        cohort=cohort,
        runs=tuple(runs),
        observations=observation_tuple,
        configs=configs,
        comparison=compare_sleeves(observation_tuple, cohort_id=COHORT_ID),
        accounting=accounting,
        decisions=decisions,
    )


def _with_observations(
    bundle: GateBundle, observations: tuple[OfficialDailyObservation, ...]
) -> GateBundle:
    return replace(
        bundle,
        observations=observations,
        comparison=compare_sleeves(observations, cohort_id=COHORT_ID),
        accounting=AccountingEvidence(
            checked_observation_keys=frozenset(
                observation.observation_key
                for observation in observations
                if observation.status is ObservationStatus.OFFICIAL
            )
        ),
    )


def _assess(
    bundle: GateBundle,
    *,
    comparison: ComparisonReport | None = None,
    accounting: AccountingEvidence | None = None,
    decisions: tuple[OperatorDecision, ...] | None = None,
    config: OperationalGateConfig | None = None,
) -> OperationalGateResult:
    return assess_operational_usefulness(
        cohort=bundle.cohort,
        runs=bundle.runs,
        observations=bundle.observations,
        sleeve_configs=bundle.configs,
        recorded_comparison=bundle.comparison if comparison is None else comparison,
        accounting_evidence=bundle.accounting if accounting is None else accounting,
        operator_decisions=bundle.decisions if decisions is None else decisions,
        config=config,
    )


def test_synthetic_passing_cohort_has_rule_level_evidence() -> None:
    result = _assess(_bundle())

    assert result.status is GateStatus.PASS
    assert result.operationally_useful is True
    assert {rule.rule for rule in result.rules} == set(GateRule)
    assert all(rule.status is GateStatus.PASS for rule in result.rules)
    assert all(rule.reason and rule.evidence for rule in result.rules)


def test_synthetic_failing_cohort() -> None:
    bundle = _bundle()
    # Three unexplained slots reduce 120 expected observations to 97.5%.
    broken = _with_observations(bundle, bundle.observations[3:])

    result = _assess(broken)

    assert result.status is GateStatus.FAIL
    assert result.rule(GateRule.COMPLETION_RATE).status is GateStatus.FAIL


def test_insufficient_history_cohort() -> None:
    result = _assess(_bundle(session_count=10))

    assert result.status is GateStatus.INSUFFICIENT_HISTORY
    assert result.rule(GateRule.SESSION_HISTORY).status is GateStatus.INSUFFICIENT_HISTORY
    assert result.operationally_useful is False


def test_missing_or_ambiguous_evidence_fails_closed() -> None:
    bundle = _bundle()
    ambiguous = AccountingEvidence(
        checked_observation_keys=bundle.accounting.checked_observation_keys,
        differences=(
            AccountingDifference(
                observation_key=bundle.observations[0].observation_key,
                area=AccountingArea.VALUATION,
                summary="Valuation difference was observed but not resolved.",
                explained=None,
            ),
        ),
    )

    result = _assess(bundle, accounting=ambiguous, decisions=())

    assert result.status is GateStatus.FAIL
    assert result.rule(GateRule.ACCOUNTING_STATES).status is GateStatus.FAIL
    assert result.rule(GateRule.OPERATOR_DECISIONS).status is GateStatus.FAIL


def test_duplicate_observations_fail() -> None:
    bundle = _bundle()
    duplicate = replace(bundle, observations=(*bundle.observations, bundle.observations[0]))

    result = _assess(duplicate)

    assert result.status is GateStatus.FAIL
    rule = result.rule(GateRule.DUPLICATE_OBSERVATIONS)
    assert rule.status is GateStatus.FAIL
    assert "duplicate" in rule.reason.casefold()


def test_readiness_violation_fails() -> None:
    bundle = _bundle()
    observations = list(bundle.observations)
    observations[0] = observations[0].model_copy(
        update={
            "readiness_ready": False,
            "readiness_reasons": ("daily_bars:stale",),
        }
    )
    violated = _with_observations(bundle, tuple(observations))

    result = _assess(violated)

    assert result.status is GateStatus.FAIL
    assert result.rule(GateRule.DATA_READINESS).status is GateStatus.FAIL


def test_unexplained_accounting_state_fails() -> None:
    bundle = _bundle()
    accounting = AccountingEvidence(
        checked_observation_keys=bundle.accounting.checked_observation_keys,
        differences=(
            AccountingDifference(
                observation_key=bundle.observations[0].observation_key,
                area=AccountingArea.CASH,
                summary="Cash differs from the expected paper ledger.",
                explained=False,
            ),
        ),
    )

    result = _assess(bundle, accounting=accounting)

    assert result.status is GateStatus.FAIL
    assert result.rule(GateRule.ACCOUNTING_STATES).status is GateStatus.FAIL


def test_failed_reproducibility_fails() -> None:
    bundle = _bundle()
    tampered = replace(bundle.comparison, common_dates=())

    result = _assess(bundle, comparison=tampered)

    assert result.status is GateStatus.FAIL
    assert result.rule(GateRule.REPRODUCIBILITY).status is GateStatus.FAIL


def test_insufficiently_distinct_sleeve_behavior_fails() -> None:
    bundle = _bundle()
    alpha_by_session = {
        observation.session_date: observation
        for observation in bundle.observations
        if observation.sleeve_id == "candidate-alpha"
    }
    observations: list[OfficialDailyObservation] = []
    for observation in bundle.observations:
        if observation.sleeve_id != "candidate-beta":
            observations.append(observation)
            continue
        alpha = alpha_by_session[observation.session_date]
        observations.append(
            observation.model_copy(
                update={
                    "total_value": alpha.total_value,
                    "return_pct": alpha.return_pct,
                    "exposure": alpha.exposure,
                    "num_positions": alpha.num_positions,
                    "turnover": alpha.turnover,
                    "modeled_cost": alpha.modeled_cost,
                    "num_filled": alpha.num_filled,
                }
            )
        )
    indistinct = _with_observations(bundle, tuple(observations))

    result = _assess(indistinct)

    assert result.status is GateStatus.FAIL
    assert result.rule(GateRule.DISTINCT_BEHAVIOR).status is GateStatus.FAIL


def test_missing_operator_decisions_fail() -> None:
    result = _assess(_bundle(), decisions=())

    assert result.status is GateStatus.FAIL
    assert result.rule(GateRule.OPERATOR_DECISIONS).status is GateStatus.FAIL


def test_profitability_alone_cannot_pass() -> None:
    result = _assess(_bundle(session_count=5, profitable=True))

    assert result.status is GateStatus.INSUFFICIENT_HISTORY
    assert result.operationally_useful is False
    assert result.investment_alpha_assessed is False


def test_gate_cannot_authorize_live_trading() -> None:
    result = _assess(_bundle())

    assert result.status is GateStatus.PASS
    assert result.live_trading_authorized is False
    assert result.investment_alpha_assessed is False
    assert "does not authorize live trading" in result.summary


def test_thresholds_are_configurable() -> None:
    result = _assess(
        _bundle(session_count=10),
        config=OperationalGateConfig(
            min_scheduled_sessions=10,
            min_behavior_overlap_sessions=5,
        ),
    )

    assert result.status is GateStatus.PASS
