"""Tests for the read-only matched-cohort comparison service (offline)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from schwab_trader.comparison import (
    ExclusionReason,
    MaturityState,
    compare_sleeves,
)
from schwab_trader.evaluation import ObservationStatus, OfficialDailyObservation

SESSION0 = date(2026, 7, 6)


def _obs(
    *,
    sleeve_id: str,
    session: date,
    total_value: Decimal | None,
    benchmark_value: Decimal | None,
    status: ObservationStatus = ObservationStatus.OFFICIAL,
    return_pct: Decimal | None = Decimal("0"),
    cohort_id: str = "cohort-a",
    turnover: Decimal | None = Decimal("0"),
    modeled_cost: Decimal | None = Decimal("0"),
    num_filled: int = 0,
    num_rejected: int = 0,
    readiness_ready: bool | None = True,
    readiness_reasons: tuple[str, ...] = (),
) -> OfficialDailyObservation:
    decision = datetime.combine(session, datetime.min.time(), tzinfo=UTC)
    return OfficialDailyObservation(
        cohort_id=cohort_id,
        run_id=f"run-{session.isoformat()}",
        sleeve_id=sleeve_id,
        strategy=sleeve_id,
        strategy_hash="sha256:abc",
        session_date=session,
        decision_time=decision,
        valuation_time=decision + timedelta(hours=16),
        status=status,
        total_value=total_value,
        return_pct=return_pct,
        benchmark_value=benchmark_value,
        turnover=turnover,
        modeled_cost=modeled_cost,
        num_filled=num_filled,
        num_rejected=num_rejected,
        readiness_ready=readiness_ready,
        readiness_reasons=readiness_reasons,
    )


def _series(
    sleeve_id: str,
    sleeve_values: list[str],
    benchmark_values: list[str],
    *,
    start: date = SESSION0,
    **kwargs: object,
) -> list[OfficialDailyObservation]:
    """Build one OFFICIAL observation per consecutive day from parallel value lists."""
    out: list[OfficialDailyObservation] = []
    for i, (sv, bv) in enumerate(zip(sleeve_values, benchmark_values, strict=True)):
        out.append(
            _obs(
                sleeve_id=sleeve_id,
                session=start + timedelta(days=i),
                total_value=Decimal(sv),
                benchmark_value=Decimal(bv),
                **kwargs,  # type: ignore[arg-type]
            )
        )
    return out


def test_excess_return_over_matched_sessions() -> None:
    # Sleeve outperforms: +10% then +10% (100 -> 121); benchmark flat at 50.
    obs = _series("alpha", ["100", "110", "121"], ["50", "50", "50"])
    report = compare_sleeves(obs)

    alpha = report.sleeve("alpha")
    assert alpha is not None
    assert alpha.maturity is MaturityState.MATURE
    assert alpha.sample_count == 2
    assert alpha.sleeve_return == pytest.approx(0.21)
    assert alpha.benchmark_return == pytest.approx(0.0)
    assert alpha.excess_return == pytest.approx(0.21)
    assert alpha.coverage == pytest.approx(1.0)
    assert alpha.exclusions == ()


def test_different_start_dates_use_only_the_overlap() -> None:
    # Benchmark spans four sessions; the sleeve only reports the last three.
    bench_days = [SESSION0 + timedelta(days=i) for i in range(4)]
    benchmark = [
        _obs(
            sleeve_id="beta",
            session=day,
            total_value=Decimal("1000"),
            benchmark_value=Decimal(str(100 + i)),
        )
        for i, day in enumerate(bench_days)
    ]
    # Replace the first with a benchmark-only reference sleeve so overlap starts late.
    late = _series(
        "alpha",
        ["200", "210", "220"],
        ["101", "102", "103"],
        start=bench_days[1],
    )
    report = compare_sleeves([*benchmark, *late])

    alpha = report.sleeve("alpha")
    assert alpha is not None
    # Only the three shared sessions are used; the sleeve's own series never invents
    # a value for the benchmark's first session.
    assert alpha.matched_dates == tuple(bench_days[1:])
    assert alpha.sample_count == 2
    # Benchmark has 4 usable sessions; alpha matched 3 -> coverage 3/4.
    assert alpha.coverage == pytest.approx(0.75)


def test_missing_session_is_excluded_not_zeroed() -> None:
    obs = _series("alpha", ["100", "110", "121"], ["50", "50", "50"])
    # A later session produced no data at all.
    obs.append(
        _obs(
            sleeve_id="alpha",
            session=SESSION0 + timedelta(days=3),
            total_value=None,
            benchmark_value=None,
            status=ObservationStatus.MISSING,
            return_pct=None,
        )
    )
    report = compare_sleeves(obs)
    alpha = report.sleeve("alpha")
    assert alpha is not None
    # The missing session contributes no return and is reported as an exclusion.
    assert alpha.sample_count == 2
    assert alpha.sleeve_return == pytest.approx(0.21)
    assert ExclusionReason.NON_OFFICIAL_STATUS in {e.reason for e in alpha.exclusions}
    assert alpha.reliability.missing == 1
    assert alpha.reliability.used == 3


def test_partial_observation_is_excluded_with_reason() -> None:
    obs = _series("alpha", ["100", "110"], ["50", "50"])
    obs.append(
        _obs(
            sleeve_id="alpha",
            session=SESSION0 + timedelta(days=2),
            total_value=Decimal("120"),  # a value exists, but the session was partial
            benchmark_value=Decimal("50"),
            status=ObservationStatus.PARTIAL,
            readiness_ready=False,
            readiness_reasons=("daily_bars:stale",),
        )
    )
    report = compare_sleeves(obs)
    alpha = report.sleeve("alpha")
    assert alpha is not None
    assert alpha.sample_count == 1  # only the two official sessions matched
    excl = {(e.session_date, e.reason) for e in alpha.exclusions}
    assert (SESSION0 + timedelta(days=2), ExclusionReason.NON_OFFICIAL_STATUS) in excl
    assert alpha.reliability.partial == 1
    assert alpha.reliability.readiness_unready == 1
    assert "daily_bars:stale" in alpha.reliability.reason_codes


def test_missing_benchmark_session_is_excluded() -> None:
    obs = _series("alpha", ["100", "110", "121"], ["50", "50", "50"])
    # An official sleeve session whose benchmark value is absent cannot be matched.
    obs.append(
        _obs(
            sleeve_id="alpha",
            session=SESSION0 + timedelta(days=3),
            total_value=Decimal("130"),
            benchmark_value=None,
        )
    )
    report = compare_sleeves(obs)
    alpha = report.sleeve("alpha")
    assert alpha is not None
    assert alpha.sample_count == 2
    reasons = {e.reason for e in alpha.exclusions}
    assert ExclusionReason.MISSING_BENCHMARK_VALUE in reasons


def test_insufficient_history_states() -> None:
    # A single shared session -> no paired return at all.
    one = _series("alpha", ["100"], ["50"])
    report = compare_sleeves(one)
    alpha = report.sleeve("alpha")
    assert alpha is not None
    assert alpha.maturity is MaturityState.NO_OVERLAP
    assert alpha.sample_count == 0
    assert alpha.excess_return is None
    assert alpha.volatility is None
    assert alpha.beta is None

    # Two shared sessions -> one return: below the default two-return floor.
    two = _series("alpha", ["100", "110"], ["50", "50"])
    thin = compare_sleeves(two).sleeve("alpha")
    assert thin is not None
    assert thin.maturity is MaturityState.INSUFFICIENT_HISTORY
    assert thin.sample_count == 1
    assert thin.excess_return == pytest.approx(0.10)  # return exists...
    assert thin.volatility is None  # ...but dispersion metrics need >= 2 returns


def test_cash_control_sleeve_has_no_fabricated_ratios() -> None:
    # A cash control holds value flat: zero-variance returns must not become a ratio.
    obs = _series("cash", ["1000", "1000", "1000", "1000"], ["50", "55", "52", "58"])
    report = compare_sleeves(obs)
    cash = report.sleeve("cash")
    assert cash is not None
    assert cash.sleeve_return == pytest.approx(0.0)
    assert cash.volatility == pytest.approx(0.0)
    assert cash.sharpe is None  # zero variance -> undefined, not fabricated
    assert cash.beta == pytest.approx(0.0)  # flat sleeve legitimately has zero beta
    assert cash.correlation is None  # sleeve has no variance -> correlation undefined
    assert cash.max_drawdown_pct == Decimal("0")
    # Excess return is purely the negative of the benchmark move over the window.
    assert cash.excess_return is not None
    assert cash.benchmark_return is not None
    assert cash.excess_return == pytest.approx(-cash.benchmark_return)


def test_benchmark_conflict_is_excluded() -> None:
    # Two sleeves disagree on the benchmark for the same session.
    a = _series("alpha", ["100", "110"], ["50", "50"])
    b = _series("beta", ["200", "220"], ["50", "999"])  # day-1 benchmark disagrees
    report = compare_sleeves([*a, *b])

    conflict_day = SESSION0 + timedelta(days=1)
    assert conflict_day not in report.benchmark_dates
    alpha = report.sleeve("alpha")
    assert alpha is not None
    assert ExclusionReason.BENCHMARK_CONFLICT in {e.reason for e in alpha.exclusions}
    # The conflicted session is dropped, leaving too little matched history.
    assert alpha.maturity is MaturityState.NO_OVERLAP


def test_turnover_cost_and_fill_reject_aggregate_over_used_sessions() -> None:
    obs = [
        _obs(
            sleeve_id="alpha",
            session=SESSION0 + timedelta(days=i),
            total_value=Decimal(v),
            benchmark_value=Decimal("50"),
            turnover=Decimal("100"),
            modeled_cost=Decimal("1"),
            num_filled=2,
            num_rejected=1,
        )
        for i, v in enumerate(["100", "110", "121"])
    ]
    report = compare_sleeves(obs)
    alpha = report.sleeve("alpha")
    assert alpha is not None
    assert alpha.total_turnover == Decimal("300")
    assert alpha.total_modeled_cost == Decimal("3")
    assert alpha.num_filled == 6
    assert alpha.num_rejected == 3
    assert alpha.reject_rate == pytest.approx(3 / 9)
    assert alpha.turnover_ratio is not None
    assert alpha.modeled_cost_drag is not None


def test_correlation_matrix_over_pairwise_matched_dates() -> None:
    a = _series("alpha", ["100", "110", "121", "133"], ["50", "50", "50", "50"])
    # beta moves opposite to alpha -> negative correlation.
    b = _series("beta", ["100", "90", "81", "73"], ["50", "50", "50", "50"])
    report = compare_sleeves([*a, *b])

    corr = report.correlation("alpha", "beta")
    assert corr is not None
    assert corr < 0
    assert report.correlation("alpha", "alpha") is None
    assert report.correlation("alpha", "missing") is None
    assert {s.sleeve_id for s in report.sleeves} == {"alpha", "beta"}


def test_common_dates_are_the_intersection_of_all_sleeves() -> None:
    a = _series("alpha", ["100", "110", "121"], ["50", "51", "52"], start=SESSION0)
    # beta starts one day later, so the last two sessions are common to both.
    b = _series(
        "beta",
        ["100", "105", "110"],
        ["51", "52", "53"],
        start=SESSION0 + timedelta(days=1),
    )
    report = compare_sleeves([*a, *b])
    expected = (SESSION0 + timedelta(days=1), SESSION0 + timedelta(days=2))
    assert report.common_dates == expected


def test_multiple_cohorts_require_selection() -> None:
    a = _series("alpha", ["100", "110"], ["50", "50"])
    other = _series("alpha", ["100", "110"], ["50", "50"])
    other = [o.model_copy(update={"cohort_id": "cohort-b"}) for o in other]

    with pytest.raises(ValueError, match="multiple cohorts"):
        compare_sleeves([*a, *other])

    # Selecting one cohort restricts the comparison to that cohort only.
    report = compare_sleeves([*a, *other], cohort_id="cohort-b")
    assert report.cohort_id == "cohort-b"
    assert len(report.sleeves) == 1


def test_empty_observations_are_rejected() -> None:
    with pytest.raises(ValueError, match="no observations"):
        compare_sleeves([])


def test_reproducible_from_stored_observations() -> None:
    obs = _series("alpha", ["100", "110", "121"], ["50", "51", "52"])
    first = compare_sleeves(obs)
    second = compare_sleeves(list(obs))
    assert first == second
