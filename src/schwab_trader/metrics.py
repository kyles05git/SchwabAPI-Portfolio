"""Sampling-aware performance and comparison metric primitives.

Sampling-aware annualized metrics require periods_per_year explicitly so
intraday observations cannot silently receive daily annualization. sharpe keeps
its historical 252-period default for compatibility; new callers should migrate
to annualized_sharpe and pass the sampling frequency explicitly.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import pairwise

TRADING_DAYS_PER_YEAR = 252
_TRADING_DAYS_PER_YEAR = TRADING_DAYS_PER_YEAR


@dataclass(frozen=True)
class MatchedReturns:
    """Returns derived solely from shared equity observation dates."""

    dates: tuple[date, ...]
    sleeve_returns: tuple[float, ...]
    benchmark_returns: tuple[float, ...]

    def __post_init__(self) -> None:
        samples = len(self.sleeve_returns)
        if len(self.benchmark_returns) != samples:
            raise ValueError("matched return series must have equal sample counts")
        expected_dates = samples + 1 if samples or self.dates else 0
        if len(self.dates) != expected_dates:
            raise ValueError("matched dates must contain one more observation than returns")

    @property
    def return_dates(self) -> tuple[date, ...]:
        return self.dates[1:]

    @property
    def sample_count(self) -> int:
        return len(self.sleeve_returns)

    def has_sufficient_history(self, min_samples: int = 1) -> bool:
        _validate_min_samples(min_samples)
        return self.sample_count >= min_samples


@dataclass(frozen=True)
class MatchedPerformance:
    """Time-weighted results over the exact matched dates."""

    dates: tuple[date, ...]
    sample_count: int
    sleeve_return: float | None
    benchmark_return: float | None
    excess_return: float | None

    @property
    def sufficient_history(self) -> bool:
        return self.excess_return is not None


@dataclass(frozen=True)
class RollingExcessReturn:
    """One rolling matched-window comparison."""

    start_date: date
    end_date: date
    sleeve_return: float
    benchmark_return: float
    excess_return: float


def _validate_periods_per_year(periods_per_year: int) -> None:
    if isinstance(periods_per_year, bool) or periods_per_year <= 0:
        raise ValueError("periods_per_year must be a positive integer")


def _validate_min_samples(min_samples: int) -> None:
    if isinstance(min_samples, bool) or min_samples <= 0:
        raise ValueError("min_samples must be a positive integer")


def _validated_returns(returns: Sequence[float]) -> tuple[float, ...]:
    values = tuple(float(value) for value in returns)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("returns must contain only finite values")
    return values


def _validated_positive_equity(values: Sequence[Decimal]) -> tuple[Decimal, ...]:
    equities = tuple(values)
    if any(not value.is_finite() or value <= 0 for value in equities):
        raise ValueError("equity values must be finite and positive")
    return equities


def _validated_nonnegative_amounts(
    values: Sequence[Decimal], *, description: str
) -> tuple[Decimal, ...]:
    amounts = tuple(values)
    if any(not value.is_finite() or value < 0 for value in amounts):
        raise ValueError(f"{description} must contain finite, nonnegative values")
    return amounts


def simple_returns(values: Sequence[Decimal]) -> list[float]:
    """Period-over-period returns; reject nonpositive equity rather than skip it."""
    equities = _validated_positive_equity(values)
    return [float(current / previous - 1) for previous, current in pairwise(equities)]


def annualized_volatility(
    returns: Sequence[float], *, periods_per_year: int, min_samples: int = 2
) -> float | None:
    """Annualized sample volatility, or None for insufficient history."""
    _validate_periods_per_year(periods_per_year)
    _validate_min_samples(min_samples)
    values = _validated_returns(returns)
    if len(values) < max(2, min_samples):
        return None
    return statistics.stdev(values) * math.sqrt(periods_per_year)


def volatility(
    returns: Sequence[float], *, periods_per_year: int, min_samples: int = 2
) -> float | None:
    """Explicit-frequency alias for annualized_volatility."""
    return annualized_volatility(
        returns, periods_per_year=periods_per_year, min_samples=min_samples
    )


def annualized_sharpe(
    returns: Sequence[float],
    *,
    periods_per_year: int,
    risk_free_rate_per_period: float = 0.0,
    min_samples: int = 2,
) -> float | None:
    """Annualized Sharpe, or None for insufficient or zero-variance history."""
    _validate_periods_per_year(periods_per_year)
    _validate_min_samples(min_samples)
    if not math.isfinite(risk_free_rate_per_period):
        raise ValueError("risk_free_rate_per_period must be finite")
    values = _validated_returns(returns)
    if len(values) < max(2, min_samples):
        return None
    excess = tuple(value - risk_free_rate_per_period for value in values)
    spread = statistics.stdev(excess)
    if spread == 0:
        return None
    return statistics.fmean(excess) / spread * math.sqrt(periods_per_year)


def sharpe(
    returns: Sequence[float], *, periods_per_year: int = _TRADING_DAYS_PER_YEAR
) -> float | None:
    """Legacy Sharpe with a 252-period compatibility default.

    Sampling-aware and intraday code must use annualized_sharpe.
    """
    return annualized_sharpe(returns, periods_per_year=periods_per_year)


def annualized_sortino(
    returns: Sequence[float],
    *,
    periods_per_year: int,
    minimum_acceptable_return: float = 0.0,
    min_samples: int = 2,
) -> float | None:
    """Annualized Sortino, or None for insufficient or no-downside history."""
    _validate_periods_per_year(periods_per_year)
    _validate_min_samples(min_samples)
    if not math.isfinite(minimum_acceptable_return):
        raise ValueError("minimum_acceptable_return must be finite")
    values = _validated_returns(returns)
    if len(values) < max(2, min_samples):
        return None
    excess = tuple(value - minimum_acceptable_return for value in values)
    downside = math.sqrt(statistics.fmean(min(value, 0.0) ** 2 for value in excess))
    if downside == 0:
        return None
    return statistics.fmean(excess) / downside * math.sqrt(periods_per_year)


def sortino(
    returns: Sequence[float],
    *,
    periods_per_year: int,
    minimum_acceptable_return: float = 0.0,
    min_samples: int = 2,
) -> float | None:
    """Explicit-frequency alias for annualized_sortino."""
    return annualized_sortino(
        returns,
        periods_per_year=periods_per_year,
        minimum_acceptable_return=minimum_acceptable_return,
        min_samples=min_samples,
    )


def matched_date_returns(
    sleeve_equity: Mapping[date, Decimal], benchmark_equity: Mapping[date, Decimal]
) -> MatchedReturns:
    """Build paired returns from the sorted intersection of observation dates.

    Missing dates span the same wider interval in both series and never create a
    zero return. Fewer than two shared dates produce an explicit zero-sample result.
    """
    _validated_positive_equity(tuple(sleeve_equity.values()))
    _validated_positive_equity(tuple(benchmark_equity.values()))
    dates = tuple(sorted(sleeve_equity.keys() & benchmark_equity.keys()))
    if len(dates) < 2:
        return MatchedReturns(dates=dates, sleeve_returns=(), benchmark_returns=())
    return MatchedReturns(
        dates=dates,
        sleeve_returns=tuple(simple_returns(tuple(sleeve_equity[day] for day in dates))),
        benchmark_returns=tuple(simple_returns(tuple(benchmark_equity[day] for day in dates))),
    )


def time_weighted_return(returns: Sequence[float], *, min_samples: int = 1) -> float | None:
    """Geometrically link returns, or None when samples are lacking."""
    _validate_min_samples(min_samples)
    values = _validated_returns(returns)
    if len(values) < min_samples:
        return None
    if any(value < -1 for value in values):
        raise ValueError("a simple return cannot be less than -1")
    return math.prod(1 + value for value in values) - 1


def benchmark_excess_return(
    sleeve_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    *,
    min_samples: int = 1,
) -> float | None:
    """Time-weighted sleeve return minus its matched benchmark return."""
    if len(sleeve_returns) != len(benchmark_returns):
        raise ValueError("sleeve and benchmark returns must be date-matched")
    sleeve_result = time_weighted_return(sleeve_returns, min_samples=min_samples)
    benchmark_result = time_weighted_return(benchmark_returns, min_samples=min_samples)
    if sleeve_result is None or benchmark_result is None:
        return None
    return sleeve_result - benchmark_result


def matched_time_weighted_returns(
    sleeve_equity: Mapping[date, Decimal],
    benchmark_equity: Mapping[date, Decimal],
    *,
    min_samples: int = 1,
) -> MatchedPerformance:
    """Calculate sleeve, benchmark, and excess return over exact shared dates."""
    matched = matched_date_returns(sleeve_equity, benchmark_equity)
    sleeve_result = time_weighted_return(matched.sleeve_returns, min_samples=min_samples)
    benchmark_result = time_weighted_return(matched.benchmark_returns, min_samples=min_samples)
    excess_result = benchmark_excess_return(
        matched.sleeve_returns,
        matched.benchmark_returns,
        min_samples=min_samples,
    )
    return MatchedPerformance(
        dates=matched.dates,
        sample_count=matched.sample_count,
        sleeve_return=sleeve_result,
        benchmark_return=benchmark_result,
        excess_return=excess_result,
    )


def rolling_excess_returns(
    matched: MatchedReturns, *, window: int
) -> tuple[RollingExcessReturn, ...]:
    """Return complete rolling matched-session excess-return windows.

    An empty tuple explicitly represents insufficient history. A 20-session result
    uses 20 returns and therefore requires 21 matched equity observations.
    """
    _validate_min_samples(window)
    if matched.sample_count < window:
        return ()
    results: list[RollingExcessReturn] = []
    for end in range(window, matched.sample_count + 1):
        start = end - window
        sleeve_result = time_weighted_return(matched.sleeve_returns[start:end])
        benchmark_result = time_weighted_return(matched.benchmark_returns[start:end])
        if sleeve_result is None or benchmark_result is None:
            continue
        results.append(
            RollingExcessReturn(
                start_date=matched.dates[start],
                end_date=matched.dates[end],
                sleeve_return=sleeve_result,
                benchmark_return=benchmark_result,
                excess_return=sleeve_result - benchmark_result,
            )
        )
    return tuple(results)


def rolling_20_session_excess_returns(
    matched: MatchedReturns,
) -> tuple[RollingExcessReturn, ...]:
    """Return complete rolling 20-session excess-return windows."""
    return rolling_excess_returns(matched, window=20)


def rolling_60_session_excess_returns(
    matched: MatchedReturns,
) -> tuple[RollingExcessReturn, ...]:
    """Return complete rolling 60-session excess-return windows."""
    return rolling_excess_returns(matched, window=60)


def beta(
    sleeve_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    *,
    min_samples: int = 2,
) -> float | None:
    """Sleeve beta versus matched benchmark returns, or None when undefined."""
    _validate_min_samples(min_samples)
    sleeve = _validated_returns(sleeve_returns)
    benchmark = _validated_returns(benchmark_returns)
    if len(sleeve) != len(benchmark):
        raise ValueError("sleeve and benchmark returns must be date-matched")
    if len(sleeve) < max(2, min_samples):
        return None
    benchmark_variance = statistics.variance(benchmark)
    if benchmark_variance == 0:
        return None
    return statistics.covariance(sleeve, benchmark) / benchmark_variance


def correlation(
    sleeve_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    *,
    min_samples: int = 2,
) -> float | None:
    """Pearson correlation of matched returns, or None when undefined."""
    _validate_min_samples(min_samples)
    sleeve = _validated_returns(sleeve_returns)
    benchmark = _validated_returns(benchmark_returns)
    if len(sleeve) != len(benchmark):
        raise ValueError("sleeve and benchmark returns must be date-matched")
    if len(sleeve) < max(2, min_samples):
        return None
    if statistics.variance(sleeve) == 0 or statistics.variance(benchmark) == 0:
        return None
    return statistics.correlation(sleeve, benchmark)


def matched_beta(matched: MatchedReturns, *, min_samples: int = 2) -> float | None:
    """Convenience beta for an already matched return series."""
    return beta(matched.sleeve_returns, matched.benchmark_returns, min_samples=min_samples)


def matched_correlation(matched: MatchedReturns, *, min_samples: int = 2) -> float | None:
    """Convenience correlation for an already matched return series."""
    return correlation(matched.sleeve_returns, matched.benchmark_returns, min_samples=min_samples)


def aggregate_turnover(turnover_notionals: Sequence[Decimal]) -> Decimal | None:
    """Sum turnover notional, or None when no observations exist."""
    values = _validated_nonnegative_amounts(turnover_notionals, description="turnover notionals")
    return sum(values, start=Decimal(0)) if values else None


def aggregate_modeled_cost(modeled_costs: Sequence[Decimal]) -> Decimal | None:
    """Sum modeled costs, or None when no observations exist."""
    values = _validated_nonnegative_amounts(modeled_costs, description="modeled costs")
    return sum(values, start=Decimal(0)) if values else None


def turnover_ratio(
    turnover_notionals: Sequence[Decimal], equity_values: Sequence[Decimal]
) -> Decimal | None:
    """Total turnover divided by average equity, or None for missing samples."""
    turnover = aggregate_turnover(turnover_notionals)
    equities = _validated_positive_equity(equity_values)
    if turnover is None or not equities:
        return None
    return turnover / (sum(equities, start=Decimal(0)) / len(equities))


def modeled_cost_drag(
    modeled_costs: Sequence[Decimal], equity_values: Sequence[Decimal]
) -> Decimal | None:
    """Total modeled costs divided by average equity, or None for missing samples."""
    cost = aggregate_modeled_cost(modeled_costs)
    equities = _validated_positive_equity(equity_values)
    if cost is None or not equities:
        return None
    return cost / (sum(equities, start=Decimal(0)) / len(equities))


def max_drawdown_pct(values: Sequence[Decimal]) -> Decimal:
    """Largest peak-to-trough decline of an equity series, as a positive percent."""
    peak = None
    worst = Decimal(0)
    for value in values:
        if peak is None or value > peak:
            peak = value
        if peak and peak > 0:
            drawdown = (peak - value) / peak * 100
            worst = max(worst, drawdown)
    return worst


def cagr_pct(
    start: Decimal, end: Decimal, periods: int, *, periods_per_year: int = _TRADING_DAYS_PER_YEAR
) -> float | None:
    """Compound annual growth rate (percent) over ``periods`` samples, or None.

    ``periods_per_year`` converts sample count to years: 252 for trading days (the
    default), 12 for monthly steps.
    """
    s, e = float(start), float(end)
    if s <= 0 or e <= 0 or periods <= 0:
        return None
    growth: float = (e / s) ** (periods_per_year / periods)
    return (growth - 1) * 100
