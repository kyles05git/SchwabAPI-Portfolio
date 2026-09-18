"""Tests for shared performance metrics (offline)."""

from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import Decimal

import pytest

from schwab_trader import metrics


def test_simple_returns() -> None:
    r = metrics.simple_returns([Decimal("100"), Decimal("110"), Decimal("99")])
    assert abs(r[0] - 0.10) < 1e-9
    assert abs(r[1] - (-0.10)) < 1e-9


def test_sharpe_needs_variation() -> None:
    assert metrics.sharpe([]) is None
    assert metrics.sharpe([0.01]) is None
    assert metrics.sharpe([0.01, 0.01, 0.01]) is None  # zero stdev
    s = metrics.sharpe([0.01, -0.005, 0.02, 0.0])
    assert s is not None


def test_max_drawdown() -> None:
    # 100 -> 120 (peak) -> 90 (trough) -> 110. Max DD = (120-90)/120 = 25%.
    values = [Decimal(v) for v in ("100", "120", "90", "110")]
    assert metrics.max_drawdown_pct(values) == Decimal("25")


def test_max_drawdown_monotonic_up_is_zero() -> None:
    values = [Decimal(v) for v in ("100", "110", "120")]
    assert metrics.max_drawdown_pct(values) == Decimal("0")


def test_cagr() -> None:
    # Double in exactly one year (252 days) -> +100% CAGR.
    assert metrics.cagr_pct(Decimal("100"), Decimal("200"), 252) == 100.0
    assert metrics.cagr_pct(Decimal("0"), Decimal("100"), 252) is None


def test_sampling_aware_annualization_requires_explicit_frequency() -> None:
    returns = [0.01, -0.005, 0.02, -0.01]

    with pytest.raises(TypeError):
        metrics.annualized_volatility(returns)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        metrics.annualized_sharpe(returns)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        metrics.annualized_sortino(returns)  # type: ignore[call-arg]

    daily_vol = metrics.annualized_volatility(returns, periods_per_year=252)
    monthly_vol = metrics.annualized_volatility(returns, periods_per_year=12)
    daily_sharpe = metrics.annualized_sharpe(returns, periods_per_year=252)
    monthly_sharpe = metrics.annualized_sharpe(returns, periods_per_year=12)
    daily_sortino = metrics.annualized_sortino(returns, periods_per_year=252)
    monthly_sortino = metrics.annualized_sortino(returns, periods_per_year=12)

    assert daily_vol is not None and monthly_vol is not None
    assert daily_sharpe is not None and monthly_sharpe is not None
    assert daily_sortino is not None and monthly_sortino is not None
    scale = math.sqrt(252 / 12)
    assert daily_vol == pytest.approx(monthly_vol * scale)
    assert daily_sharpe == pytest.approx(monthly_sharpe * scale)
    assert daily_sortino == pytest.approx(monthly_sortino * scale)
    assert metrics.sharpe(returns) == daily_sharpe


def test_zero_variance_is_not_fabricated_as_a_ratio() -> None:
    flat = [0.01, 0.01, 0.01]

    assert metrics.annualized_volatility(flat, periods_per_year=252) == 0.0
    assert metrics.annualized_sharpe(flat, periods_per_year=252) is None
    assert metrics.annualized_sortino(flat, periods_per_year=252) is None
    assert metrics.beta([0.01, 0.02, 0.03], flat) is None
    assert metrics.correlation([0.01, 0.02, 0.03], flat) is None
    assert metrics.correlation(flat, [0.01, 0.02, 0.03]) is None


@pytest.mark.parametrize(
    "values",
    [
        [Decimal("100"), Decimal("0"), Decimal("101")],
        [Decimal("100"), Decimal("-1")],
    ],
)
def test_simple_returns_reject_nonpositive_equity(values: list[Decimal]) -> None:
    with pytest.raises(ValueError, match="positive"):
        metrics.simple_returns(values)


def test_missing_dates_align_by_intersection_without_flat_returns() -> None:
    first = date(2026, 1, 2)
    sleeve_only = date(2026, 1, 5)
    benchmark_only = date(2026, 1, 6)
    last = date(2026, 1, 7)
    sleeve = {
        first: Decimal("100"),
        sleeve_only: Decimal("110"),
        last: Decimal("121"),
    }
    benchmark = {
        first: Decimal("100"),
        benchmark_only: Decimal("105"),
        last: Decimal("110"),
    }

    matched = metrics.matched_date_returns(sleeve, benchmark)
    performance = metrics.matched_time_weighted_returns(sleeve, benchmark)

    assert matched.dates == (first, last)
    assert matched.return_dates == (last,)
    assert matched.sample_count == 1
    assert matched.sleeve_returns == pytest.approx((0.21,))
    assert matched.benchmark_returns == pytest.approx((0.10,))
    assert performance.dates == (first, last)
    assert performance.sample_count == 1
    assert performance.sleeve_return == pytest.approx(0.21)
    assert performance.benchmark_return == pytest.approx(0.10)
    assert performance.excess_return == pytest.approx(0.11)


def test_matched_returns_reject_nonpositive_equity() -> None:
    first = date(2026, 1, 2)
    second = date(2026, 1, 5)
    sleeve = {first: Decimal("100"), second: Decimal("0")}
    benchmark = {first: Decimal("100"), second: Decimal("101")}

    with pytest.raises(ValueError, match="positive"):
        metrics.matched_date_returns(sleeve, benchmark)


def test_insufficient_history_is_explicit() -> None:
    only_date = date(2026, 1, 2)
    sleeve = {only_date: Decimal("100")}
    benchmark = {only_date: Decimal("100")}

    matched = metrics.matched_date_returns(sleeve, benchmark)
    performance = metrics.matched_time_weighted_returns(sleeve, benchmark)

    assert matched.dates == (only_date,)
    assert matched.sample_count == 0
    assert not matched.has_sufficient_history()
    assert performance.sample_count == 0
    assert not performance.sufficient_history
    assert performance.sleeve_return is None
    assert performance.benchmark_return is None
    assert performance.excess_return is None
    assert metrics.time_weighted_return([]) is None
    assert metrics.annualized_volatility([0.01], periods_per_year=252) is None
    assert metrics.annualized_sharpe([0.01], periods_per_year=252) is None
    assert metrics.annualized_sortino([0.01], periods_per_year=252) is None
    assert metrics.beta([0.01, 0.02], [0.02, 0.03], min_samples=3) is None
    assert metrics.correlation([0.01, 0.02], [0.02, 0.03], min_samples=3) is None


def test_rolling_20_and_60_session_excess_returns() -> None:
    start = date(2026, 1, 2)
    dates = [start + timedelta(days=offset) for offset in range(61)]
    sleeve = {day: Decimal("100") * (Decimal("1.01") ** offset) for offset, day in enumerate(dates)}
    benchmark = {
        day: Decimal("100") * (Decimal("1.005") ** offset) for offset, day in enumerate(dates)
    }
    matched = metrics.matched_date_returns(sleeve, benchmark)

    rolling_20 = metrics.rolling_20_session_excess_returns(matched)
    rolling_60 = metrics.rolling_60_session_excess_returns(matched)

    assert len(rolling_20) == 41
    assert rolling_20[0].start_date == dates[0]
    assert rolling_20[0].end_date == dates[20]
    assert rolling_20[0].excess_return == pytest.approx(1.01**20 - 1.005**20)
    assert len(rolling_60) == 1
    assert rolling_60[0].start_date == dates[0]
    assert rolling_60[0].end_date == dates[60]
    assert rolling_60[0].excess_return == pytest.approx(1.01**60 - 1.005**60)
    assert metrics.rolling_excess_returns(matched, window=61) == ()


def test_beta_and_correlation_use_matched_returns() -> None:
    benchmark = [-0.02, -0.01, 0.01, 0.02]
    sleeve = [2 * value for value in benchmark]

    assert metrics.beta(sleeve, benchmark) == pytest.approx(2.0)
    assert metrics.correlation(sleeve, benchmark) == pytest.approx(1.0)

    matched = metrics.MatchedReturns(
        dates=tuple(date(2026, 1, 2) + timedelta(days=i) for i in range(5)),
        sleeve_returns=tuple(sleeve),
        benchmark_returns=tuple(benchmark),
    )
    assert metrics.matched_beta(matched) == pytest.approx(2.0)
    assert metrics.matched_correlation(matched) == pytest.approx(1.0)


def test_unmatched_return_vectors_are_rejected() -> None:
    with pytest.raises(ValueError, match="date-matched"):
        metrics.benchmark_excess_return([0.01], [0.01, 0.02])
    with pytest.raises(ValueError, match="date-matched"):
        metrics.beta([0.01], [0.01, 0.02])
    with pytest.raises(ValueError, match="date-matched"):
        metrics.correlation([0.01], [0.01, 0.02])


def test_turnover_and_modeled_cost_aggregation() -> None:
    turnover = [Decimal("100"), Decimal("50"), Decimal("0")]
    costs = [Decimal("1"), Decimal("2"), Decimal("0")]
    equity = [Decimal("1000"), Decimal("2000")]

    assert metrics.aggregate_turnover(turnover) == Decimal("150")
    assert metrics.aggregate_modeled_cost(costs) == Decimal("3")
    assert metrics.turnover_ratio(turnover, equity) == Decimal("0.1")
    assert metrics.modeled_cost_drag(costs, equity) == Decimal("0.002")


def test_trading_aggregates_distinguish_zero_from_no_history() -> None:
    assert metrics.aggregate_turnover([]) is None
    assert metrics.aggregate_modeled_cost([]) is None
    assert metrics.aggregate_turnover([Decimal("0")]) == Decimal("0")
    assert metrics.aggregate_modeled_cost([Decimal("0")]) == Decimal("0")
    assert metrics.turnover_ratio([], [Decimal("1000")]) is None
    assert metrics.modeled_cost_drag([], [Decimal("1000")]) is None
    with pytest.raises(ValueError, match="nonnegative"):
        metrics.aggregate_turnover([Decimal("-1")])
    with pytest.raises(ValueError, match="nonnegative"):
        metrics.aggregate_modeled_cost([Decimal("-1")])
    with pytest.raises(ValueError, match="positive"):
        metrics.turnover_ratio([Decimal("1")], [Decimal("0")])
