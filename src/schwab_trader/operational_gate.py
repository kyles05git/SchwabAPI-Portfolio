"""Deterministic operational-usefulness assessment for paper-sleeve cohorts.

This module is deliberately read-only.  It consumes the canonical experiment,
runner, evaluation, comparison, and sleeve-definition contracts and returns an
explainable assessment.  It does not rank strategies, prove investment alpha,
promote a sleeve, authorize live trading, or interact with an order path.

The gate always fails closed: :attr:`OperationalGateResult.investment_alpha_assessed`
and :attr:`OperationalGateResult.live_trading_authorized` are hard ``False``, and a
missing piece of required evidence is a failure rather than a pass.

Two additions support honest *presentation* without relaxing that contract:

- An injected clock.  ``now_et`` is naive Eastern wall-clock time and is the preferred
  form: a session becomes evidence only once its exchange close has passed *and* its
  run has either executed or gone past the scheduler's grace period, so a run pending
  on the morning of its own session is upcoming rather than an unexplained gap or a
  missing snapshot lineage.  ``as_of`` is the older date-only clock; it still excludes
  future sessions but cannot tell 09:28 ET from 16:05 ET on the session's own date.
  Either way this is strictly more conservative on the ``min_scheduled_sessions`` floor
  (it lowers the counted history) and cannot turn real negative evidence into a pass.
- :attr:`RuleAssessment.awaiting_evidence` marks a rule whose outcome is not yet
  determined by real due evidence (no accounting review recorded, no operator decision
  recorded, no observations at all).  The rule's ``status`` is unchanged; callers may
  use the flag to distinguish "not evaluated yet" from "genuinely failed".
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from itertools import combinations
from typing import Literal

from schwab_trader import evidence_timing, scheduling
from schwab_trader.comparison import ComparisonReport, compare_sleeves
from schwab_trader.evaluation import ObservationStatus, OfficialDailyObservation
from schwab_trader.experiments import ExperimentCohort
from schwab_trader.sleeve_runs import SleeveRun
from schwab_trader.sleeves import SleeveConfig


class GateStatus(StrEnum):
    """Overall and rule-level gate outcomes."""

    PASS = "pass"
    FAIL = "fail"
    INSUFFICIENT_HISTORY = "insufficient-history"


class GateRule(StrEnum):
    """Stable identifiers for every operational gate rule."""

    SESSION_HISTORY = "session-history"
    COMPLETION_RATE = "completion-rate"
    DUPLICATE_OBSERVATIONS = "duplicate-observations"
    ACCOUNTING_STATES = "accounting-states"
    DATA_READINESS = "data-readiness"
    REPRODUCIBILITY = "reproducibility"
    DISTINCT_BEHAVIOR = "distinct-sleeve-behavior"
    OPERATOR_DECISIONS = "operator-decisions"


# Operator-facing wording for each rule. The enum values stay the stable identifiers.
GATE_RULE_LABELS: dict[GateRule, str] = {
    GateRule.SESSION_HISTORY: "Session history",
    GateRule.COMPLETION_RATE: "Completion reliability",
    GateRule.DUPLICATE_OBSERVATIONS: "Duplicate observations",
    GateRule.ACCOUNTING_STATES: "Paper accounting",
    GateRule.DATA_READINESS: "Data readiness",
    GateRule.REPRODUCIBILITY: "Reproducibility",
    GateRule.DISTINCT_BEHAVIOR: "Distinct strategy behavior",
    GateRule.OPERATOR_DECISIONS: "Operator decisions",
}


class AccountingArea(StrEnum):
    """Accounting domains named by the paper-sleeves plan."""

    CASH = "cash"
    POSITIONS = "positions"
    VALUATION = "valuation"


class OperatorAction(StrEnum):
    """Recorded operator dispositions accepted by the gate."""

    KEEP = "keep"
    MODIFY = "modify"
    PAUSE = "pause"
    RETIRE = "retire"


@dataclass(frozen=True)
class AccountingDifference:
    """One observed accounting difference and its operator explanation state."""

    observation_key: str
    area: AccountingArea
    summary: str
    explained: bool | None
    explanation: str | None = None


@dataclass(frozen=True)
class AccountingEvidence:
    """Positive review coverage plus every discovered accounting difference."""

    checked_observation_keys: frozenset[str]
    differences: tuple[AccountingDifference, ...] = ()


@dataclass(frozen=True)
class OperatorDecision:
    """A durable keep/modify/pause/retire decision about one cohort sleeve."""

    cohort_id: str
    sleeve_id: str
    action: OperatorAction
    rationale: str
    recorded_at: datetime


@dataclass(frozen=True)
class OperationalGateConfig:
    """Conservative, configurable thresholds derived from the paper-sleeves plan."""

    min_scheduled_sessions: int = 30
    min_completion_rate: Decimal = Decimal("0.98")
    min_behavior_overlap_sessions: int = 20
    min_distinct_non_control_sleeves: int = 2
    max_return_correlation: float = 0.95
    min_mean_exposure_difference: Decimal = Decimal("0.05")
    min_mean_position_count_difference: Decimal = Decimal("1")
    min_trade_activity_disagreement_rate: Decimal = Decimal("0.20")
    min_mean_absolute_return_difference: Decimal = Decimal("0.001")
    min_operator_decisions: int = 1
    control_sleeve_ids: frozenset[str] = frozenset({"control-cash"})

    def __post_init__(self) -> None:
        if self.min_scheduled_sessions < 1:
            raise ValueError("min_scheduled_sessions must be positive")
        if not Decimal(0) <= self.min_completion_rate <= Decimal(1):
            raise ValueError("min_completion_rate must be between zero and one")
        if not 1 <= self.min_behavior_overlap_sessions <= self.min_scheduled_sessions:
            raise ValueError(
                "min_behavior_overlap_sessions must be positive and no greater than "
                "min_scheduled_sessions"
            )
        if self.min_distinct_non_control_sleeves < 2:
            raise ValueError("min_distinct_non_control_sleeves must be at least two")
        if not -1.0 <= self.max_return_correlation <= 1.0:
            raise ValueError("max_return_correlation must be between -1 and 1")
        for name, value in (
            ("min_mean_exposure_difference", self.min_mean_exposure_difference),
            ("min_mean_position_count_difference", self.min_mean_position_count_difference),
            (
                "min_trade_activity_disagreement_rate",
                self.min_trade_activity_disagreement_rate,
            ),
            (
                "min_mean_absolute_return_difference",
                self.min_mean_absolute_return_difference,
            ),
        ):
            if value < 0:
                raise ValueError(f"{name} must not be negative")
        if self.min_trade_activity_disagreement_rate > 1:
            raise ValueError("min_trade_activity_disagreement_rate must not exceed one")
        if self.min_operator_decisions < 1:
            raise ValueError("min_operator_decisions must be positive")
        if any(not sleeve_id.strip() for sleeve_id in self.control_sleeve_ids):
            raise ValueError("control_sleeve_ids must not contain empty identifiers")


@dataclass(frozen=True)
class RuleAssessment:
    """One operator-readable rule outcome with concrete supporting evidence."""

    rule: GateRule
    status: GateStatus
    reason: str
    evidence: tuple[str, ...]
    awaiting_evidence: bool = False
    """The outcome is not yet determined by real due evidence.

    ``True`` means the rule could not be evaluated because the evidence it needs has
    not been produced or recorded yet (no due sessions, no observations, no accounting
    review, no operator decision).  It never changes :attr:`status`, which still fails
    closed; it only lets a caller say "awaiting evidence" instead of "failed".
    """

    @property
    def label(self) -> str:
        """Plain-language name for this rule."""

        return GATE_RULE_LABELS[self.rule]


@dataclass(frozen=True)
class OperationalGateResult:
    """Frozen operational result that cannot carry live-trading authorization."""

    cohort_id: str
    status: GateStatus
    summary: str
    rules: tuple[RuleAssessment, ...]

    @property
    def operationally_useful(self) -> bool:
        """Whether every operational rule passed."""

        return self.status is GateStatus.PASS

    @property
    def investment_alpha_assessed(self) -> Literal[False]:
        """The operational gate never assesses or proves investment alpha."""

        return False

    @property
    def live_trading_authorized(self) -> Literal[False]:
        """The operational gate never authorizes live trading."""

        return False

    def rule(self, rule: GateRule) -> RuleAssessment:
        """Return one rule result by its stable identifier."""

        return next(item for item in self.rules if item.rule is rule)


def _rule(
    rule: GateRule,
    status: GateStatus,
    reason: str,
    *evidence: str,
    awaiting_evidence: bool = False,
) -> RuleAssessment:
    return RuleAssessment(
        rule=rule,
        status=status,
        reason=reason,
        evidence=tuple(evidence),
        awaiting_evidence=awaiting_evidence,
    )


def _session_history(runs: Sequence[SleeveRun], config: OperationalGateConfig) -> RuleAssessment:
    sessions = {run.scheduled_for for run in runs}
    count = len(sessions)
    evidence = (
        f"observed scheduled sessions: {count}",
        f"required scheduled sessions: {config.min_scheduled_sessions}",
    )
    if count < config.min_scheduled_sessions:
        return _rule(
            GateRule.SESSION_HISTORY,
            GateStatus.INSUFFICIENT_HISTORY,
            f"The cohort has {count} scheduled sessions; at least "
            f"{config.min_scheduled_sessions} are required.",
            *evidence,
            awaiting_evidence=True,
        )
    return _rule(
        GateRule.SESSION_HISTORY,
        GateStatus.PASS,
        f"The cohort meets the {config.min_scheduled_sessions}-session history floor.",
        *evidence,
    )


def _completion_rate(
    cohort: ExperimentCohort,
    runs: Sequence[SleeveRun],
    observations: Sequence[OfficialDailyObservation],
    config: OperationalGateConfig,
) -> RuleAssessment:
    observations_by_slot: dict[tuple[str, str], list[OfficialDailyObservation]] = {}
    for observation in observations:
        observations_by_slot.setdefault((observation.run_id, observation.sleeve_id), []).append(
            observation
        )

    expected = 0
    accounted_for = 0
    unexplained: list[str] = []
    for run in runs:
        members_by_id = {member.sleeve_id: member for member in run.members}
        for sleeve_id in cohort.member_sleeves:
            expected += 1
            matches = observations_by_slot.get((run.run_id, sleeve_id), [])
            member = members_by_id.get(sleeve_id)
            if len(matches) == 1 and matches[0].session_date == run.scheduled_for:
                observation = matches[0]
                if observation.status is ObservationStatus.OFFICIAL:
                    accounted_for += 1
                    continue
                if observation.readiness_reasons or (
                    member is not None and member.error is not None
                ):
                    accounted_for += 1
                    continue
            elif not matches and member is not None and member.error is not None:
                accounted_for += 1
                continue
            unexplained.append(f"{run.scheduled_for.isoformat()}:{sleeve_id}")

    if expected == 0:
        return _rule(
            GateRule.COMPLETION_RATE,
            GateStatus.INSUFFICIENT_HISTORY,
            "No due cohort-member observations exist yet to measure completion.",
            "expected observation slots: 0",
            "accounted-for observation slots: 0",
            awaiting_evidence=True,
        )

    rate = Decimal(accounted_for) / Decimal(expected)
    evidence = [
        f"accounted-for observation slots: {accounted_for}/{expected}",
        f"completion rate: {rate:.4f}",
        f"required completion rate: {config.min_completion_rate:.4f}",
    ]
    if unexplained:
        evidence.append("unexplained slots: " + ", ".join(sorted(unexplained)))
    if rate < config.min_completion_rate:
        return _rule(
            GateRule.COMPLETION_RATE,
            GateStatus.FAIL,
            "Completed or explicitly explained observations fall below the configured floor.",
            *evidence,
        )
    return _rule(
        GateRule.COMPLETION_RATE,
        GateStatus.PASS,
        "Expected observations meet the configured completion-and-explanation floor.",
        *evidence,
    )


def _duplicate_observations(
    observations: Sequence[OfficialDailyObservation],
) -> RuleAssessment:
    counts = Counter(observation.observation_key for observation in observations)
    duplicates = sorted(key for key, count in counts.items() if count > 1)
    if duplicates:
        return _rule(
            GateRule.DUPLICATE_OBSERVATIONS,
            GateStatus.FAIL,
            "Duplicate official observation identities were supplied; daily evidence is ambiguous.",
            f"duplicate identity count: {len(duplicates)}",
            "duplicate identities: " + ", ".join(duplicates),
        )
    return _rule(
        GateRule.DUPLICATE_OBSERVATIONS,
        GateStatus.PASS,
        "Every cohort/sleeve/session observation identity is unique.",
        f"observations checked: {len(observations)}",
        "duplicate identity count: 0",
    )


def _accounting_states(
    observations: Sequence[OfficialDailyObservation],
    evidence: AccountingEvidence | None,
) -> RuleAssessment:
    official_keys = {
        observation.observation_key
        for observation in observations
        if observation.status is ObservationStatus.OFFICIAL
    }
    if evidence is None:
        return _rule(
            GateRule.ACCOUNTING_STATES,
            GateStatus.FAIL,
            "Accounting review evidence is missing, so the gate fails closed.",
            f"official observations requiring review: {len(official_keys)}",
            "accounting evidence supplied: no",
            awaiting_evidence=True,
        )

    missing_checks = sorted(official_keys - evidence.checked_observation_keys)
    unknown_checks = sorted(evidence.checked_observation_keys - official_keys)
    unexplained = [
        difference
        for difference in evidence.differences
        if difference.explained is not True
        or not difference.explanation
        or not difference.explanation.strip()
    ]
    unknown_differences = [
        difference
        for difference in evidence.differences
        if difference.observation_key not in official_keys
    ]
    details = [
        f"reviewed official observations: "
        f"{len(official_keys) - len(missing_checks)}/{len(official_keys)}",
        f"recorded accounting differences: {len(evidence.differences)}",
        f"unexplained or ambiguous differences: {len(unexplained)}",
    ]
    if missing_checks:
        details.append("unreviewed observation identities: " + ", ".join(missing_checks))
    if unknown_checks:
        details.append("unknown reviewed identities: " + ", ".join(unknown_checks))
    if unexplained:
        details.append(
            "unexplained differences: "
            + ", ".join(f"{item.observation_key}:{item.area.value}" for item in unexplained)
        )
    if missing_checks or unknown_checks or unexplained or unknown_differences:
        return _rule(
            GateRule.ACCOUNTING_STATES,
            GateStatus.FAIL,
            "Accounting evidence is incomplete, ambiguous, or contains unexplained states.",
            *details,
        )
    return _rule(
        GateRule.ACCOUNTING_STATES,
        GateStatus.PASS,
        "All official observations were reviewed and every accounting difference is explained.",
        *details,
        # A clean review of zero official observations is a vacuous pass, not proof.
        awaiting_evidence=not official_keys,
    )


def _data_readiness(
    observations: Sequence[OfficialDailyObservation],
) -> RuleAssessment:
    violations: list[str] = []
    for observation in observations:
        identity = observation.observation_key
        if observation.readiness_ready is None:
            violations.append(f"{identity}:readiness-missing")
        elif observation.status is ObservationStatus.OFFICIAL and (
            not observation.readiness_ready or observation.readiness_reasons
        ):
            violations.append(f"{identity}:unready-data-accepted-as-official")
        elif observation.status is not ObservationStatus.OFFICIAL and (
            observation.readiness_ready or not observation.readiness_reasons
        ):
            violations.append(f"{identity}:incomplete-status-readiness-ambiguous")

    if not observations:
        violations.append("cohort:no-readiness-observations")
    if violations:
        return _rule(
            GateRule.DATA_READINESS,
            GateStatus.FAIL,
            "Required readiness evidence is missing, ambiguous, or accepted unready data.",
            f"observations checked: {len(observations)}",
            f"readiness violations: {len(violations)}",
            "violations: " + ", ".join(sorted(violations)),
            # No observations at all means the check has nothing to judge yet; any
            # other violation is a real readiness defect in recorded evidence.
            awaiting_evidence=not observations,
        )
    return _rule(
        GateRule.DATA_READINESS,
        GateStatus.PASS,
        "Every official result was ready and every incomplete result retained blocking reasons.",
        f"observations checked: {len(observations)}",
        "readiness violations: 0",
    )


def _reproducibility(
    cohort: ExperimentCohort,
    runs: Sequence[SleeveRun],
    observations: Sequence[OfficialDailyObservation],
    sleeve_configs: Sequence[SleeveConfig],
    recorded_comparison: ComparisonReport | None,
) -> RuleAssessment:
    problems: list[str] = []
    member_ids = set(cohort.member_sleeves)

    run_ids = Counter(run.run_id for run in runs)
    run_sessions = Counter(run.scheduled_for for run in runs)
    if any(count > 1 for count in run_ids.values()):
        problems.append("duplicate run identities")
    if any(count > 1 for count in run_sessions.values()):
        problems.append("multiple durable runs for one scheduled session")

    runs_by_id = {run.run_id: run for run in runs}
    for run in runs:
        if tuple(run.expected_members) != cohort.member_sleeves:
            problems.append(f"{run.run_id}:expected membership changed")
        if {member.sleeve_id for member in run.members} != member_ids:
            problems.append(f"{run.run_id}:persisted member checkpoints changed")
        if run.snapshot_id is None or run.quote_snapshot_id is None:
            problems.append(f"{run.run_id}:snapshot lineage missing")

    relevant_configs = [
        config for config in sleeve_configs if config.identity in member_ids
    ]
    config_counts = Counter(config.identity for config in relevant_configs)
    if set(config_counts) != member_ids:
        missing = sorted(member_ids - set(config_counts))
        problems.append("missing sleeve definitions: " + ", ".join(missing))
    if any(count > 1 for count in config_counts.values()):
        problems.append("duplicate sleeve definitions")
    configs_by_id = {config.identity: config for config in relevant_configs}
    for sleeve_id, config in sorted(configs_by_id.items()):
        if config.cohort_id != cohort.cohort_id:
            problems.append(f"{sleeve_id}:cohort assignment mismatch")
        if not config.reproducible or config.definition is None:
            problems.append(f"{sleeve_id}:versioned strategy definition missing")
        elif config.configuration_hash != config.definition.configuration_hash:
            problems.append(f"{sleeve_id}:stored configuration hash mismatch")

    for observation in observations:
        lineage_run = runs_by_id.get(observation.run_id)
        sleeve_config = configs_by_id.get(observation.sleeve_id)
        if lineage_run is None or observation.session_date != lineage_run.scheduled_for:
            problems.append(f"{observation.observation_key}:run lineage mismatch")
        elif lineage_run.snapshot_id is not None and lineage_run.quote_snapshot_id is not None:
            expected_snapshots = {
                "cohort_snapshot": lineage_run.snapshot_id,
                "quotes": lineage_run.quote_snapshot_id,
                **dict(sorted(lineage_run.data_snapshot_ids.items())),
            }
            if observation.snapshot_ids != expected_snapshots:
                problems.append(f"{observation.observation_key}:snapshot lineage mismatch")
        if sleeve_config is None:
            problems.append(f"{observation.observation_key}:sleeve definition missing")
        elif (
            observation.strategy != sleeve_config.strategy
            or observation.strategy_hash != sleeve_config.configuration_hash
        ):
            problems.append(f"{observation.observation_key}:strategy identity changed")

    if recorded_comparison is None:
        problems.append("recorded comparison report missing")
    else:
        try:
            recomputed = compare_sleeves(observations, cohort_id=cohort.cohort_id)
        except ValueError as exc:
            problems.append(f"comparison could not be recomputed: {exc}")
        else:
            if recomputed != recorded_comparison:
                problems.append("recorded comparison differs from persisted observations")

    unique_problems = tuple(dict.fromkeys(problems))
    evidence = [
        f"durable runs checked: {len(runs)}",
        f"strategy definitions checked: {len(relevant_configs)}/{len(member_ids)}",
        f"official observation lineages checked: {len(observations)}",
        f"reproducibility problems: {len(unique_problems)}",
    ]
    if unique_problems:
        evidence.append("problems: " + ", ".join(unique_problems))
        # Before anything has run there is nothing to reproduce, and the only complaint
        # is the absent comparison report. A definition or lineage defect is real now.
        awaiting = (
            not runs
            and not observations
            and unique_problems == ("recorded comparison report missing",)
        )
        return _rule(
            GateRule.REPRODUCIBILITY,
            GateStatus.FAIL,
            "Persisted runs, definitions, observations, or comparison output are not reproducible.",
            *evidence,
            awaiting_evidence=awaiting,
        )
    return _rule(
        GateRule.REPRODUCIBILITY,
        GateStatus.PASS,
        "Definitions reconstruct exactly and the comparison reproduces from persisted records.",
        *evidence,
    )


def _mean_decimal(values: Sequence[Decimal]) -> Decimal | None:
    return None if not values else sum(values, Decimal(0)) / Decimal(len(values))


def _is_active(observation: OfficialDailyObservation) -> bool:
    return observation.num_filled > 0 or (
        observation.turnover is not None and observation.turnover > 0
    )


def _distinct_behavior(
    cohort: ExperimentCohort,
    session_count: int,
    observations: Sequence[OfficialDailyObservation],
    comparison: ComparisonReport | None,
    config: OperationalGateConfig,
) -> RuleAssessment:
    excluded = set(config.control_sleeve_ids) | {cohort.benchmark_sleeve}
    candidates = tuple(sleeve for sleeve in cohort.member_sleeves if sleeve not in excluded)
    if len(candidates) < config.min_distinct_non_control_sleeves:
        return _rule(
            GateRule.DISTINCT_BEHAVIOR,
            GateStatus.FAIL,
            "The cohort does not contain enough non-control sleeves to test distinct behavior.",
            f"eligible non-control sleeves: {len(candidates)}",
            f"required distinct non-control sleeves: {config.min_distinct_non_control_sleeves}",
        )

    by_sleeve: dict[str, dict[date, OfficialDailyObservation]] = {}
    for observation in observations:
        if observation.status is ObservationStatus.OFFICIAL:
            by_sleeve.setdefault(observation.sleeve_id, {})[observation.session_date] = observation

    distinct_ids: set[str] = set()
    pair_evidence: list[str] = []
    max_overlap = 0
    for sleeve_a, sleeve_b in combinations(candidates, 2):
        obs_a = by_sleeve.get(sleeve_a, {})
        obs_b = by_sleeve.get(sleeve_b, {})
        shared = sorted(set(obs_a) & set(obs_b))
        max_overlap = max(max_overlap, len(shared))
        if len(shared) < config.min_behavior_overlap_sessions:
            pair_evidence.append(
                f"{sleeve_a}/{sleeve_b}:overlap={len(shared)} below "
                f"{config.min_behavior_overlap_sessions}"
            )
            continue

        pairs = [(obs_a[day], obs_b[day]) for day in shared]
        signals: list[str] = []
        correlation = None if comparison is None else comparison.correlation(sleeve_a, sleeve_b)
        if correlation is not None and correlation <= config.max_return_correlation:
            signals.append(f"return-correlation={correlation:.4f}")

        exposure_differences = [
            abs(a.exposure - b.exposure)
            for a, b in pairs
            if a.exposure is not None and b.exposure is not None
        ]
        mean_exposure = _mean_decimal(exposure_differences)
        if mean_exposure is not None and mean_exposure >= config.min_mean_exposure_difference:
            signals.append(f"mean-exposure-difference={mean_exposure}")

        position_differences = [
            Decimal(abs(a.num_positions - b.num_positions))
            for a, b in pairs
            if a.num_positions is not None and b.num_positions is not None
        ]
        mean_positions = _mean_decimal(position_differences)
        if (
            mean_positions is not None
            and mean_positions >= config.min_mean_position_count_difference
        ):
            signals.append(f"mean-position-count-difference={mean_positions}")

        activity_disagreement = Decimal(
            sum(_is_active(a) != _is_active(b) for a, b in pairs)
        ) / Decimal(len(pairs))
        if activity_disagreement >= config.min_trade_activity_disagreement_rate:
            signals.append(f"trade-activity-disagreement={activity_disagreement:.4f}")

        return_differences = [
            abs(a.return_pct - b.return_pct)
            for a, b in pairs
            if a.return_pct is not None and b.return_pct is not None
        ]
        mean_return = _mean_decimal(return_differences)
        if mean_return is not None and mean_return >= config.min_mean_absolute_return_difference:
            signals.append(f"mean-absolute-return-difference={mean_return}")

        if signals:
            distinct_ids.update((sleeve_a, sleeve_b))
            pair_evidence.append(
                f"{sleeve_a}/{sleeve_b}:overlap={len(shared)}; " + "; ".join(signals)
            )
        else:
            pair_evidence.append(f"{sleeve_a}/{sleeve_b}:overlap={len(shared)}; no threshold met")

    evidence = [
        f"eligible non-control sleeves: {len(candidates)}",
        f"distinct non-control sleeves: {len(distinct_ids)}",
        f"required distinct non-control sleeves: {config.min_distinct_non_control_sleeves}",
        *pair_evidence,
    ]
    if session_count < config.min_scheduled_sessions:
        return _rule(
            GateRule.DISTINCT_BEHAVIOR,
            GateStatus.INSUFFICIENT_HISTORY,
            "Observed behavior remains preliminary until the cohort reaches the history floor.",
            *evidence,
            awaiting_evidence=True,
        )
    if max_overlap < config.min_behavior_overlap_sessions:
        return _rule(
            GateRule.DISTINCT_BEHAVIOR,
            GateStatus.FAIL,
            "No non-control pair has enough matched official behavior evidence.",
            *evidence,
        )
    if len(distinct_ids) < config.min_distinct_non_control_sleeves:
        return _rule(
            GateRule.DISTINCT_BEHAVIOR,
            GateStatus.FAIL,
            "Non-control sleeves did not meet any configured behavior-distinction threshold.",
            *evidence,
        )
    return _rule(
        GateRule.DISTINCT_BEHAVIOR,
        GateStatus.PASS,
        "At least two non-control sleeves demonstrated meaningfully different behavior.",
        *evidence,
    )


def _operator_decisions(
    cohort: ExperimentCohort,
    decisions: Sequence[OperatorDecision] | None,
    config: OperationalGateConfig,
) -> RuleAssessment:
    if decisions is None:
        return _rule(
            GateRule.OPERATOR_DECISIONS,
            GateStatus.FAIL,
            "Operator decision evidence is missing, so the gate fails closed.",
            "operator decision record supplied: no",
            f"required defensible decisions: {config.min_operator_decisions}",
            awaiting_evidence=True,
        )

    invalid: list[str] = []
    valid: list[OperatorDecision] = []
    member_ids = set(cohort.member_sleeves)
    for decision in decisions:
        if decision.cohort_id != cohort.cohort_id:
            continue
        if decision.sleeve_id not in member_ids:
            invalid.append(f"unknown sleeve {decision.sleeve_id}")
        elif not decision.rationale.strip():
            invalid.append(f"{decision.sleeve_id}:rationale missing")
        elif decision.recorded_at.tzinfo is None or decision.recorded_at.utcoffset() is None:
            invalid.append(f"{decision.sleeve_id}:recorded_at timezone missing")
        else:
            valid.append(decision)

    evidence = [
        f"valid cohort decisions: {len(valid)}",
        f"required defensible decisions: {config.min_operator_decisions}",
        "actions: " + (", ".join(sorted(item.action.value for item in valid)) or "none"),
    ]
    if invalid:
        evidence.append("invalid decision records: " + ", ".join(sorted(invalid)))
    if invalid or len(valid) < config.min_operator_decisions:
        return _rule(
            GateRule.OPERATOR_DECISIONS,
            GateStatus.FAIL,
            "The cohort lacks enough valid, reasoned keep/modify/pause/retire decisions.",
            *evidence,
        )
    return _rule(
        GateRule.OPERATOR_DECISIONS,
        GateStatus.PASS,
        "The experiment produced the configured number of reasoned operator decisions.",
        *evidence,
    )


def _is_due_observation(
    session_date: date,
    as_of: date | None,
    window: evidence_timing.EvidenceWindow | None,
) -> bool:
    """Whether an observation may be judged as recorded evidence under the clock."""

    if window is not None:
        return window.counts_evidence_observation(session_date)
    return as_of is None or session_date <= as_of


def assess_operational_usefulness(
    *,
    cohort: ExperimentCohort,
    runs: Iterable[SleeveRun],
    observations: Iterable[OfficialDailyObservation],
    sleeve_configs: Iterable[SleeveConfig],
    recorded_comparison: ComparisonReport | None,
    accounting_evidence: AccountingEvidence | None,
    operator_decisions: Iterable[OperatorDecision] | None,
    config: OperationalGateConfig | None = None,
    as_of: date | None = None,
    now_et: datetime | None = None,
    policy: scheduling.SchedulingPolicy = scheduling.DEFAULT_POLICY,
) -> OperationalGateResult:
    """Assess one paper cohort without making any investment or live-trading decision.

    Inputs may come directly from ``SleeveRunStore.list()``,
    ``EvaluationStore.official_observations()``, ``SleeveStore.list()``, and the
    stored result of :func:`schwab_trader.comparison.compare_sleeves`. Records for
    other cohorts are ignored by identity; all evidence inside the selected cohort
    is checked deterministically.

    ``now_et`` is the preferred explicit clock: naive Eastern wall-clock time. With it,
    a run counts as evidence only once it has executed or gone past the scheduler's
    grace period, so a session that has closed but is still within its normal execution
    window is neither an unexplained completion gap nor a reproducibility defect.

    ``as_of`` is the legacy date-only clock. It excludes runs and observations dated
    after it, but on a session's own date it treats a still-pending run as due. Prefer
    ``now_et``; when both are supplied ``now_et`` wins. Omitting both preserves the
    historical behavior of treating every persisted run as due.
    """

    thresholds = config or OperationalGateConfig()
    cohort_runs = tuple(run for run in runs if run.cohort_id == cohort.cohort_id)
    # Only sessions that have delivered an outcome can carry execution evidence.
    # Excluding upcoming and still-within-grace sessions lowers the counted history,
    # so the 30-session floor stays conservative.
    window = (
        None
        if now_et is None
        else evidence_timing.assess_runs(cohort_runs, now_et=now_et, policy=policy)
    )
    if window is not None:
        due_runs = window.evidence_runs
    elif as_of is not None:
        due_runs = tuple(run for run in cohort_runs if run.scheduled_for <= as_of)
    else:
        due_runs = cohort_runs
    cohort_observations = tuple(
        observation
        for observation in observations
        if observation.cohort_id == cohort.cohort_id
        and _is_due_observation(observation.session_date, as_of, window)
    )
    configs = tuple(sleeve_configs)
    decisions = None if operator_decisions is None else tuple(operator_decisions)
    session_count = len({run.scheduled_for for run in due_runs})

    rules = (
        _session_history(due_runs, thresholds),
        _completion_rate(cohort, due_runs, cohort_observations, thresholds),
        _duplicate_observations(cohort_observations),
        _accounting_states(cohort_observations, accounting_evidence),
        _data_readiness(cohort_observations),
        _reproducibility(
            cohort,
            due_runs,
            cohort_observations,
            configs,
            recorded_comparison,
        ),
        _distinct_behavior(
            cohort,
            session_count,
            cohort_observations,
            recorded_comparison,
            thresholds,
        ),
        _operator_decisions(cohort, decisions, thresholds),
    )

    if any(rule.status is GateStatus.FAIL for rule in rules):
        status = GateStatus.FAIL
        summary = "The cohort failed one or more operational-usefulness rules."
    elif any(rule.status is GateStatus.INSUFFICIENT_HISTORY for rule in rules):
        status = GateStatus.INSUFFICIENT_HISTORY
        summary = "The cohort evidence is operationally sound but has insufficient history."
    else:
        status = GateStatus.PASS
        summary = (
            "The cohort passed the operational-usefulness gate; this is not evidence of "
            "investment alpha and does not authorize live trading."
        )
    return OperationalGateResult(
        cohort_id=cohort.cohort_id,
        status=status,
        summary=summary,
        rules=rules,
    )


__all__ = [
    "GATE_RULE_LABELS",
    "AccountingArea",
    "AccountingDifference",
    "AccountingEvidence",
    "GateRule",
    "GateStatus",
    "OperationalGateConfig",
    "OperationalGateResult",
    "OperatorAction",
    "OperatorDecision",
    "RuleAssessment",
    "assess_operational_usefulness",
]
