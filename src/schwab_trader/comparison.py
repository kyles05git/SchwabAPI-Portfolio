"""Read-only matched-cohort comparison service for paper sleeves.

Given the *official daily observations* recorded by :mod:`schwab_trader.evaluation`
(exactly one row per cohort/sleeve/session), this module compares every sleeve and
its benchmark over **identical official sessions**. Nothing here writes state, opens
a network connection, or touches the live-order path - it is pure analytics over an
already-persisted snapshot, so a report is fully reproducible from the stored rows.

Design rules that keep the comparison honest:

- Only ``OFFICIAL`` observations with a real value contribute to a return series. A
  ``MISSING`` or ``PARTIAL`` session is recorded as an explicit :class:`Exclusion`
  with a reason code, never fabricated as a zero return.
- A sleeve is measured against the benchmark over the *matched* dates the two share.
  A sleeve that started later simply uses the declared overlap; it is never padded.
- Every statistic reuses the vetted primitives in :mod:`schwab_trader.metrics`
  rather than recomputing returns, risk, beta, turnover, or cost inline.
- Insufficient shared history yields an explicit :class:`MaturityState` and ``None``
  metrics instead of a misleading point estimate.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum

from schwab_trader import metrics
from schwab_trader.evaluation import ObservationStatus, OfficialDailyObservation
from schwab_trader.metrics import RollingExcessReturn

# Official observations are one-per-session daily points, so annualization defaults to
# the trading-day calendar. Callers on a different cadence pass periods_per_year.
DEFAULT_PERIODS_PER_YEAR = metrics.TRADING_DAYS_PER_YEAR
DEFAULT_ROLLING_WINDOWS: tuple[int, ...] = (20, 60)
# A matched return needs two shared equity points; two returns is the smallest history
# that lets the dispersion-based metrics (volatility, beta, correlation) be defined.
DEFAULT_MIN_HISTORY = 2


class MaturityState(StrEnum):
    """How much matched history a sleeve-versus-benchmark comparison rests on."""

    MATURE = "mature"  # at least ``min_history`` matched returns
    INSUFFICIENT_HISTORY = "insufficient_history"  # some overlap, but below the floor
    NO_OVERLAP = "no_overlap"  # no shared session produced a paired return


class ExclusionReason(StrEnum):
    """Why one recorded observation did not enter the matched comparison."""

    NON_OFFICIAL_STATUS = "non_official_status"  # session was MISSING or PARTIAL
    MISSING_SLEEVE_VALUE = "missing_sleeve_value"  # official row lacked a total value
    MISSING_BENCHMARK_VALUE = "missing_benchmark_value"  # no benchmark for that session
    BENCHMARK_CONFLICT = "benchmark_conflict"  # sleeves disagreed on the benchmark


@dataclass(frozen=True)
class Exclusion:
    """One session dropped from a sleeve's matched series, with its reason."""

    session_date: date
    reason: ExclusionReason


@dataclass(frozen=True)
class SleeveReliability:
    """Coverage and data-readiness reliability for one sleeve's observations."""

    total_observations: int
    official: int
    partial: int
    missing: int
    used: int  # official sessions that also matched the benchmark
    excluded: int
    readiness_ready: int
    readiness_unready: int
    readiness_unknown: int
    reason_codes: tuple[str, ...]  # distinct readiness reasons across the sleeve


@dataclass(frozen=True)
class SleeveComparison:
    """Matched-cohort metrics for a single sleeve versus the benchmark."""

    sleeve_id: str
    strategy: str
    maturity: MaturityState
    sample_count: int  # number of matched returns (matched equity points minus one)
    matched_dates: tuple[date, ...]  # shared equity observation dates, sorted
    sleeve_return: float | None
    benchmark_return: float | None
    excess_return: float | None
    rolling_excess: Mapping[int, tuple[RollingExcessReturn, ...]]
    max_drawdown_pct: Decimal
    volatility: float | None
    sharpe: float | None
    sortino: float | None
    beta: float | None
    correlation: float | None
    total_turnover: Decimal | None
    turnover_ratio: Decimal | None
    total_modeled_cost: Decimal | None
    modeled_cost_drag: Decimal | None
    num_filled: int
    num_rejected: int
    reject_rate: float | None
    coverage: float  # matched official sessions / usable benchmark sessions
    reliability: SleeveReliability
    exclusions: tuple[Exclusion, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SleeveCorrelation:
    """Return correlation between two sleeves over their own matched dates."""

    sleeve_a: str
    sleeve_b: str
    sample_count: int
    correlation: float | None


@dataclass(frozen=True)
class ComparisonReport:
    """The full matched-cohort comparison for one cohort."""

    cohort_id: str
    benchmark_dates: tuple[date, ...]  # sessions with a single, usable benchmark value
    common_dates: tuple[date, ...]  # sessions shared by every sleeve and the benchmark
    sleeves: tuple[SleeveComparison, ...]
    correlations: tuple[SleeveCorrelation, ...]

    def sleeve(self, sleeve_id: str) -> SleeveComparison | None:
        """Return the comparison for ``sleeve_id`` if present."""
        return next((s for s in self.sleeves if s.sleeve_id == sleeve_id), None)

    def correlation(self, sleeve_a: str, sleeve_b: str) -> float | None:
        """Return the matched return correlation for an unordered sleeve pair."""
        if sleeve_a == sleeve_b:
            return None
        pair = frozenset({sleeve_a, sleeve_b})
        for entry in self.correlations:
            if frozenset({entry.sleeve_a, entry.sleeve_b}) == pair:
                return entry.correlation
        return None


def _resolve_cohort(
    observations: Sequence[OfficialDailyObservation], cohort_id: str | None
) -> str:
    """Pick the single cohort to compare, rejecting an ambiguous cross-cohort set."""
    cohorts = {obs.cohort_id for obs in observations}
    if cohort_id is not None:
        return cohort_id
    if not cohorts:
        raise ValueError("no observations to compare; pass observations or a cohort_id")
    if len(cohorts) > 1:
        raise ValueError(
            "observations span multiple cohorts; pass cohort_id to select one: "
            + ", ".join(sorted(cohorts))
        )
    return next(iter(cohorts))


def _benchmark_equity(
    observations: Sequence[OfficialDailyObservation],
) -> tuple[dict[date, Decimal], set[date]]:
    """Build the benchmark equity series and the set of conflicted sessions.

    A benchmark value is taken only from ``OFFICIAL`` observations. When two sleeves
    disagree on the benchmark for a session, that session is conflicted: it is left
    out of the usable series and reported per sleeve as ``BENCHMARK_CONFLICT`` rather
    than silently resolved to one value.
    """
    seen: dict[date, set[Decimal]] = {}
    for obs in observations:
        if obs.status is not ObservationStatus.OFFICIAL or obs.benchmark_value is None:
            continue
        seen.setdefault(obs.session_date, set()).add(obs.benchmark_value)
    equity = {day: values.pop() for day, values in seen.items() if len(values) == 1}
    conflicts = {day for day, values in seen.items() if len(values) > 1}
    return equity, conflicts


def _sleeve_equity(
    observations: Sequence[OfficialDailyObservation],
) -> dict[date, Decimal]:
    """Map each official session with a real value to that sleeve's equity."""
    return {
        obs.session_date: obs.total_value
        for obs in observations
        if obs.status is ObservationStatus.OFFICIAL and obs.total_value is not None
    }


def _exclusions_for_sleeve(
    observations: Sequence[OfficialDailyObservation],
    benchmark_equity: Mapping[date, Decimal],
    benchmark_conflicts: set[date],
    used_sessions: set[date],
) -> tuple[Exclusion, ...]:
    """Explain every observation that did not enter the matched series."""
    exclusions: list[Exclusion] = []
    for obs in sorted(observations, key=lambda o: o.session_date):
        if obs.session_date in used_sessions:
            continue
        if obs.status is not ObservationStatus.OFFICIAL:
            reason = ExclusionReason.NON_OFFICIAL_STATUS
        elif obs.total_value is None:
            reason = ExclusionReason.MISSING_SLEEVE_VALUE
        elif obs.session_date in benchmark_conflicts:
            reason = ExclusionReason.BENCHMARK_CONFLICT
        elif obs.session_date not in benchmark_equity:
            reason = ExclusionReason.MISSING_BENCHMARK_VALUE
        else:
            # Value is present and the benchmark matched: it is used, not excluded.
            continue
        exclusions.append(Exclusion(session_date=obs.session_date, reason=reason))
    return tuple(exclusions)


def _reliability(
    observations: Sequence[OfficialDailyObservation], used_sessions: set[date]
) -> SleeveReliability:
    """Summarize per-status counts and readiness reliability for a sleeve."""
    official = sum(1 for o in observations if o.status is ObservationStatus.OFFICIAL)
    partial = sum(1 for o in observations if o.status is ObservationStatus.PARTIAL)
    missing = sum(1 for o in observations if o.status is ObservationStatus.MISSING)
    ready = sum(1 for o in observations if o.readiness_ready is True)
    unready = sum(1 for o in observations if o.readiness_ready is False)
    unknown = sum(1 for o in observations if o.readiness_ready is None)
    reasons = sorted({reason for o in observations for reason in o.readiness_reasons})
    used = len(used_sessions)
    return SleeveReliability(
        total_observations=len(observations),
        official=official,
        partial=partial,
        missing=missing,
        used=used,
        excluded=len(observations) - used,
        readiness_ready=ready,
        readiness_unready=unready,
        readiness_unknown=unknown,
        reason_codes=tuple(reasons),
    )


def _maturity(sample_count: int, min_history: int) -> MaturityState:
    if sample_count <= 0:
        return MaturityState.NO_OVERLAP
    if sample_count < min_history:
        return MaturityState.INSUFFICIENT_HISTORY
    return MaturityState.MATURE


def _compare_one_sleeve(
    sleeve_id: str,
    observations: Sequence[OfficialDailyObservation],
    benchmark_equity: Mapping[date, Decimal],
    benchmark_conflicts: set[date],
    *,
    periods_per_year: int,
    rolling_windows: Sequence[int],
    min_history: int,
) -> SleeveComparison:
    sleeve_equity = _sleeve_equity(observations)
    matched = metrics.matched_date_returns(sleeve_equity, dict(benchmark_equity))
    used_sessions = set(matched.dates)

    performance = metrics.matched_time_weighted_returns(sleeve_equity, dict(benchmark_equity))
    rolling: dict[int, tuple[RollingExcessReturn, ...]] = {
        window: metrics.rolling_excess_returns(matched, window=window)
        for window in rolling_windows
    }

    # Equity, turnover, cost, and fill counts drawn only from the used sessions so the
    # aggregates line up exactly with the matched return series.
    obs_by_session = {o.session_date: o for o in observations}
    used_equity = [sleeve_equity[day] for day in matched.dates]
    used_obs = [obs_by_session[day] for day in matched.dates]
    turnovers = [o.turnover for o in used_obs if o.turnover is not None]
    costs = [o.modeled_cost for o in used_obs if o.modeled_cost is not None]
    num_filled = sum(o.num_filled for o in used_obs)
    num_rejected = sum(o.num_rejected for o in used_obs)
    trades = num_filled + num_rejected

    strategy = observations[0].strategy if observations else sleeve_id
    return SleeveComparison(
        sleeve_id=sleeve_id,
        strategy=strategy,
        maturity=_maturity(matched.sample_count, min_history),
        sample_count=matched.sample_count,
        matched_dates=matched.dates,
        sleeve_return=performance.sleeve_return,
        benchmark_return=performance.benchmark_return,
        excess_return=performance.excess_return,
        rolling_excess=rolling,
        max_drawdown_pct=metrics.max_drawdown_pct(used_equity),
        volatility=metrics.volatility(matched.sleeve_returns, periods_per_year=periods_per_year),
        sharpe=metrics.annualized_sharpe(
            matched.sleeve_returns, periods_per_year=periods_per_year
        ),
        sortino=metrics.annualized_sortino(
            matched.sleeve_returns, periods_per_year=periods_per_year
        ),
        beta=metrics.matched_beta(matched),
        correlation=metrics.matched_correlation(matched),
        total_turnover=metrics.aggregate_turnover(turnovers),
        turnover_ratio=metrics.turnover_ratio(turnovers, used_equity) if used_equity else None,
        total_modeled_cost=metrics.aggregate_modeled_cost(costs),
        modeled_cost_drag=metrics.modeled_cost_drag(costs, used_equity) if used_equity else None,
        num_filled=num_filled,
        num_rejected=num_rejected,
        reject_rate=(num_rejected / trades) if trades else None,
        coverage=(len(used_sessions) / len(benchmark_equity)) if benchmark_equity else 0.0,
        reliability=_reliability(observations, used_sessions),
        exclusions=_exclusions_for_sleeve(
            observations, benchmark_equity, benchmark_conflicts, used_sessions
        ),
    )


def _correlation_matrix(
    sleeve_equities: Mapping[str, Mapping[date, Decimal]],
) -> tuple[SleeveCorrelation, ...]:
    """Pairwise return correlation over each pair's own matched dates."""
    sleeve_ids = sorted(sleeve_equities)
    results: list[SleeveCorrelation] = []
    for i, sleeve_a in enumerate(sleeve_ids):
        for sleeve_b in sleeve_ids[i + 1 :]:
            matched = metrics.matched_date_returns(
                dict(sleeve_equities[sleeve_a]), dict(sleeve_equities[sleeve_b])
            )
            results.append(
                SleeveCorrelation(
                    sleeve_a=sleeve_a,
                    sleeve_b=sleeve_b,
                    sample_count=matched.sample_count,
                    correlation=metrics.matched_correlation(matched),
                )
            )
    return tuple(results)


def compare_sleeves(
    observations: Iterable[OfficialDailyObservation],
    *,
    cohort_id: str | None = None,
    periods_per_year: int = DEFAULT_PERIODS_PER_YEAR,
    rolling_windows: Sequence[int] = DEFAULT_ROLLING_WINDOWS,
    min_history: int = DEFAULT_MIN_HISTORY,
) -> ComparisonReport:
    """Compare every sleeve in one cohort against its benchmark over matched sessions.

    ``observations`` is any iterable of stored :class:`OfficialDailyObservation` rows
    - typically ``EvaluationStore.official_observations()``. When they span more than
    one cohort, ``cohort_id`` must select one; otherwise the mixed set is rejected so
    two unrelated experiments are never blended into one comparison.
    """
    all_obs = list(observations)
    cohort = _resolve_cohort(all_obs, cohort_id)
    cohort_obs = [obs for obs in all_obs if obs.cohort_id == cohort]

    benchmark_equity, benchmark_conflicts = _benchmark_equity(cohort_obs)

    by_sleeve: dict[str, list[OfficialDailyObservation]] = {}
    for obs in cohort_obs:
        by_sleeve.setdefault(obs.sleeve_id, []).append(obs)

    sleeves = tuple(
        _compare_one_sleeve(
            sleeve_id,
            sleeve_obs,
            benchmark_equity,
            benchmark_conflicts,
            periods_per_year=periods_per_year,
            rolling_windows=rolling_windows,
            min_history=min_history,
        )
        for sleeve_id, sleeve_obs in sorted(by_sleeve.items())
    )

    sleeve_equities = {sid: _sleeve_equity(obs) for sid, obs in by_sleeve.items()}
    correlations = _correlation_matrix(sleeve_equities)

    # The fully matched cohort: sessions shared by the benchmark and every sleeve.
    common: set[date] = set(benchmark_equity)
    for equity in sleeve_equities.values():
        common &= set(equity)

    return ComparisonReport(
        cohort_id=cohort,
        benchmark_dates=tuple(sorted(benchmark_equity)),
        common_dates=tuple(sorted(common)),
        sleeves=sleeves,
        correlations=correlations,
    )


__all__ = [
    "ComparisonReport",
    "Exclusion",
    "ExclusionReason",
    "MaturityState",
    "SleeveComparison",
    "SleeveCorrelation",
    "SleeveReliability",
    "compare_sleeves",
]
