"""Cross-sectional fundamental factor backtest (point-in-time, offline).

Ranks a universe each month by a fundamental factor (value or quality), holds the
top ``n`` equal-weighted, and compounds forward returns - reading prices from the
local :class:`~schwab_trader.pricepanel.PricePanel` and fundamentals *point-in-time*
from the :class:`~schwab_trader.sec_store.SecStore`. No network access; both stores
must be populated first (``panel build`` and ``edgar fetch``).

Because fundamentals are pulled as-of each rebalance date (only what was filed by
then), this avoids look-ahead. It does **not** avoid survivorship bias: the panel
only holds currently-listed names, so results flatter the strategy - treat this as
directional research, not validation.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise

from pydantic import BaseModel

from schwab_trader import fundamentals, metrics
from schwab_trader.fundamentals import FACTORS
from schwab_trader.pricepanel import PricePanel
from schwab_trader.sec_store import SecStore

__all__ = ["FACTORS", "FactorBacktestResult", "run_factor_backtest"]

_MONTHS_PER_YEAR = 12


class FactorBacktestResult(BaseModel):
    factor: str
    top_n: int
    first_day: date
    last_day: date
    months: int
    total_return_pct: Decimal
    cagr_pct: Decimal | None
    sharpe: Decimal | None
    max_drawdown_pct: Decimal
    benchmark_return_pct: Decimal
    excess_pct: Decimal
    avg_names_ranked: float  # avg symbols with a usable factor each month (data coverage)
    equity_curve: list[tuple[date, Decimal]]
    last_holdings: list[str]


def _month_end_dates(start: date, end: date) -> list[date]:
    """Last calendar day of each month from ``start``'s month through ``end``."""
    out: list[date] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        first_next = date(year + (month // 12), (month % 12) + 1, 1)
        last = first_next - timedelta(days=1)
        if start <= last <= end:
            out.append(last)
        year, month = first_next.year, first_next.month
    return out


def _basket_return(panel: PricePanel, symbols: list[str], d0: date, d1: date) -> Decimal | None:
    """Equal-weight simple return of ``symbols`` from ``d0`` to ``d1`` (None if no data)."""
    rets: list[Decimal] = []
    for symbol in symbols:
        p0 = panel.close_on_or_after(symbol, d0)
        p1 = panel.close_on_or_after(symbol, d1)
        if p0 is not None and p1 is not None and p0[1] > 0:
            rets.append(p1[1] / p0[1] - 1)
    if not rets:
        return None
    return sum(rets, Decimal(0)) / len(rets)


def run_factor_backtest(
    panel: PricePanel,
    store: SecStore,
    symbols: list[str],
    *,
    factor: str,
    start: date,
    end: date,
    top_n: int = 10,
    use_ttm: bool = True,
) -> FactorBacktestResult:
    """Backtest a monthly top-``n`` factor portfolio vs an equal-weight benchmark.

    ``use_ttm`` computes flow metrics (earnings) trailing-twelve-month so the factor
    updates quarterly; ``False`` uses the annual 10-K only.
    """
    if factor not in FACTORS:
        msg = f"Unknown factor '{factor}'. Choose: {', '.join(FACTORS)}."
        raise ValueError(msg)
    grid = _month_end_dates(start, end)
    if len(grid) < 2:
        msg = "Need at least two month-ends in the date range."
        raise ValueError(msg)

    universe = [s.strip().upper() for s in symbols if s.strip()]
    equity = Decimal(1)
    benchmark = Decimal(1)
    curve: list[tuple[date, Decimal]] = [(grid[0], equity)]
    bench_curve: list[Decimal] = [benchmark]
    ranked_counts: list[int] = []
    last_holdings: list[str] = []

    for d0, d1 in pairwise(grid):
        scored: list[tuple[Decimal, str]] = []
        priced: list[str] = []
        for symbol in universe:
            price0 = panel.close_on_or_after(symbol, d0)
            if price0 is None:
                continue
            priced.append(symbol)
            value = fundamentals.factor_score(store, factor, symbol, d0, price0[1], use_ttm=use_ttm)
            if value is not None:
                scored.append((value, symbol))

        ranked_counts.append(len(scored))
        if scored:
            scored.sort(key=lambda item: item[0], reverse=True)  # higher factor = better
            selected = [symbol for _, symbol in scored[:top_n]]
            last_holdings = selected
            port_ret = _basket_return(panel, selected, d0, d1)
            if port_ret is not None:
                equity *= Decimal(1) + port_ret
        bench_ret = _basket_return(panel, priced, d0, d1)
        if bench_ret is not None:
            benchmark *= Decimal(1) + bench_ret

        curve.append((d1, equity))
        bench_curve.append(benchmark)

    values = [value for _, value in curve]
    months = len(curve) - 1
    total_return = (equity - 1) * 100
    bench_return = (benchmark - 1) * 100
    cagr = metrics.cagr_pct(Decimal(1), equity, months, periods_per_year=_MONTHS_PER_YEAR)
    sharpe = metrics.sharpe(metrics.simple_returns(values), periods_per_year=_MONTHS_PER_YEAR)
    avg_ranked = (sum(ranked_counts) / len(ranked_counts)) if ranked_counts else 0.0

    return FactorBacktestResult(
        factor=factor,
        top_n=top_n,
        first_day=grid[0],
        last_day=grid[-1],
        months=months,
        total_return_pct=total_return,
        cagr_pct=Decimal(str(round(cagr, 2))) if cagr is not None else None,
        sharpe=Decimal(str(round(sharpe, 2))) if sharpe is not None else None,
        max_drawdown_pct=metrics.max_drawdown_pct(values),
        benchmark_return_pct=bench_return,
        excess_pct=total_return - bench_return,
        avg_names_ranked=round(avg_ranked, 1),
        equity_curve=curve,
        last_holdings=last_holdings,
    )
