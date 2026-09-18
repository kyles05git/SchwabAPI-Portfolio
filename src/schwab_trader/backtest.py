"""Historical replay backtester for rule-based strategies.

Validates a *deterministic* strategy (buy-hold basket, dip-buyer, hold) against
past daily prices before it is trusted forward. It reuses the live machinery -
each historical day is one :class:`~schwab_trader.agent.AgentRunner` cycle whose
quotes come from that day's close, filling into a fresh
:class:`~schwab_trader.paper.PaperEngine` - so the same order/fill/cash rules
apply as in paper and live trading.

The LLM strategy is intentionally out of scope: replaying it over hundreds of
days would be slow and expensive, and it stays validated forward via the paper
sleeve. This module performs no network calls; the caller supplies the candles.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel

from schwab_trader import metrics, survivorship
from schwab_trader.agent import AgentRunner, QuoteSource, Strategy
from schwab_trader.market_data import Candle, Quote, QuoteError
from schwab_trader.paper import PaperEngine

_TRADING_DAYS = Decimal(252)  # ~sessions per year, for annualizing dividend yield


class BacktestResult(BaseModel):
    start_value: Decimal
    end_value: Decimal
    total_return_pct: Decimal
    cagr_pct: Decimal | None
    sharpe: Decimal | None
    calmar: Decimal | None
    max_drawdown_pct: Decimal
    turnover: Decimal
    trades: int
    days: int
    first_day: datetime | None
    last_day: datetime | None
    equity_curve: list[tuple[datetime, Decimal]]


class GateCheck(BaseModel):
    name: str
    passed: bool
    detail: str


class GateResult(BaseModel):
    passed: bool
    checks: list[GateCheck]


def _round2(value: float | None) -> Decimal | None:
    return Decimal(str(round(value, 2))) if value is not None else None


def evaluate_gates(
    result: BacktestResult,
    *,
    min_sharpe: Decimal = Decimal("0.75"),
    min_calmar: Decimal = Decimal("0.50"),
    max_drawdown_pct: Decimal = Decimal("25"),
) -> GateResult:
    """Check the research-to-paper promotion gates we can compute.

    Covers the Sharpe / Calmar / max-drawdown gates from the strategy spec. The
    spec's IC, top-decile-spread, and baseline-margin gates need a ranking model
    and are intentionally out of scope here.
    """
    checks = [
        GateCheck(
            name=f"net Sharpe >= {min_sharpe}",
            passed=result.sharpe is not None and result.sharpe >= min_sharpe,
            detail=str(result.sharpe) if result.sharpe is not None else "n/a",
        ),
        GateCheck(
            name=f"Calmar >= {min_calmar}",
            passed=result.calmar is not None and result.calmar >= min_calmar,
            detail=str(result.calmar) if result.calmar is not None else "n/a",
        ),
        GateCheck(
            name=f"max drawdown <= {max_drawdown_pct}%",
            passed=result.max_drawdown_pct <= max_drawdown_pct,
            detail=f"{result.max_drawdown_pct}%",
        ),
    ]
    return GateResult(passed=all(check.passed for check in checks), checks=checks)


def _quote_for(
    symbol: str, candle: Candle, prev_close: Decimal | None, half_spread: Decimal
) -> Quote:
    """A day's fill quote: close +/- ``half_spread`` so buys pay the ask, sells the bid.

    With ``half_spread == 0`` all prices equal the close (frictionless). Otherwise a
    round-trip (buy then sell) pays ``2 * half_spread`` of notional - the modeled
    transaction cost.
    """
    ask = (candle.close * (1 + half_spread)).quantize(Decimal("0.01"))
    bid = (candle.close * (1 - half_spread)).quantize(Decimal("0.01"))
    return Quote(
        symbol=symbol,
        bid=bid,
        ask=ask,
        last=candle.close,
        mark=candle.close,
        previous_close=prev_close,
        quote_time=candle.date,
    )


def _open_quote_for(symbol: str, candle: Candle, half_spread: Decimal) -> Quote:
    """A day's *fill* quote priced off the bar's OPEN (market-on-open execution).

    Used for next-bar-open fills: the strategy decides on the prior close, then the
    order fills here at this bar's open +/- ``half_spread``. Falls back to the close
    when a bar carries no open (e.g. close-only synthetic data).
    """
    ref = candle.open if candle.open is not None else candle.close
    ask = (ref * (1 + half_spread)).quantize(Decimal("0.01"))
    bid = (ref * (1 - half_spread)).quantize(Decimal("0.01"))
    return Quote(
        symbol=symbol,
        bid=bid,
        ask=ask,
        last=ref,
        mark=ref,
        previous_close=candle.close,
        quote_time=candle.date,
    )


def run_backtest(
    strategy: Strategy,
    bars_by_symbol: dict[str, list[Candle]],
    engine: PaperEngine,
    *,
    window: int | None = None,
    cost_bps: float = 0.0,
    next_bar_fill: bool = True,
    dividend_yields: dict[str, float] | None = None,
) -> BacktestResult:
    """Replay ``strategy`` day by day over the given candles into ``engine``.

    ``engine`` must be a fresh (reset) paper engine at the desired starting cash.
    When ``window`` is set, only the final ``window`` trading days are traded and
    measured - earlier candles are still available to the strategy (e.g. for a
    momentum lookback) but are not part of the measured result. This keeps a
    lookback strategy comparable to a simple one over the same window.

    ``cost_bps`` is the modeled round-trip transaction cost (spread + slippage) in
    basis points; each fill pays half of it via a spread around the fill price, so a
    buy-then-sell pays the full amount. ``0`` is frictionless.

    ``next_bar_fill`` (default) removes look-ahead: the strategy decides on the
    prior session's close, the order fills at the current bar's OPEN (market-on-open),
    and equity is marked at the current bar's close - so no single price is used for
    both a decision and its own fill. Set it ``False`` for the legacy same-bar model
    (decide, fill, and mark all on one close) - useful only to quantify the look-ahead.

    ``dividend_yields`` (symbol -> annual yield, e.g. 0.013) turns the price-only
    replay into a total-return one: each measured day credits held positions a daily
    drip of ``price * shares * yield / 252`` as cash (redeployed on the next rebalance).
    This is an approximation - a constant annual yield smeared daily, not real ex-date
    payments - since Schwab price history carries no dividend series. ``None`` is the
    default price-only behavior.
    """
    half_spread = Decimal(str(cost_bps)) / 2 / 10000
    # Index candles by trading day, and collect the sorted set of days.
    by_day: dict[str, dict[date, Candle]] = {}
    day_stamp: dict[date, datetime] = {}
    for symbol, candles in bars_by_symbol.items():
        indexed: dict[date, Candle] = {}
        for candle in candles:
            key = candle.date.date()
            indexed[key] = candle
            day_stamp.setdefault(key, candle.date)
        by_day[symbol] = indexed

    all_days = sorted(day_stamp)
    days = all_days[-window:] if window is not None and window < len(all_days) else all_days
    prev_of = {all_days[i]: all_days[i - 1] for i in range(1, len(all_days))}
    start_value = engine.value({}).total_value
    equity_curve: list[tuple[datetime, Decimal]] = []
    trades = 0
    traded_notional = Decimal(0)

    def _quote_map(day: date) -> dict[str, Quote]:
        """Decision quotes from a day's close (previous_close = the prior session)."""
        out: dict[str, Quote] = {}
        for symbol, indexed in by_day.items():
            bar = indexed.get(day)
            if bar is None:
                continue
            prior = prev_of.get(day)
            prior_bar = indexed.get(prior) if prior is not None else None
            prev_close = prior_bar.close if prior_bar is not None else None
            out[symbol] = _quote_for(symbol, bar, prev_close, half_spread)
        return out

    def _make_source(quotes: dict[str, Quote]) -> QuoteSource:
        def source(symbol: str, _quotes: dict[str, Quote] = quotes) -> Quote:
            quote = _quotes.get(symbol)
            if quote is None:
                raise QuoteError(f"No candle for {symbol} on this day.")
            return quote

        return source

    def _close_marks(day: date) -> dict[str, Decimal | None]:
        return {symbol: indexed[day].close for symbol, indexed in by_day.items() if day in indexed}

    def _credit_dividends(day: date) -> None:
        """Credit one day's dividend drip on held positions (total-return replay)."""
        if not dividend_yields:
            return
        marks = _close_marks(day)
        total = Decimal(0)
        for position in engine.positions():
            annual = dividend_yields.get(position.symbol)
            price = marks.get(position.symbol)
            if annual and price is not None:
                total += price * position.quantity * Decimal(str(annual)) / _TRADING_DAYS
        engine.credit_cash(total.quantize(Decimal("0.01")))

    for day in days:
        if next_bar_fill:
            prior = prev_of.get(day)
            if prior is not None:
                # Decide on the prior close; fill at this bar's open.
                decision = _make_source(_quote_map(prior))
                fill_quotes = {
                    symbol: _open_quote_for(symbol, indexed[day], half_spread)
                    for symbol, indexed in by_day.items()
                    if day in indexed
                }
                report = AgentRunner(strategy, engine, decision).run_cycle(
                    now=day_stamp[prior], fill_source=_make_source(fill_quotes)
                )
                trades += report.num_filled
                for outcome in report.outcomes:
                    if outcome.status == "FILLED" and outcome.fill_price is not None:
                        traded_notional += outcome.fill_price * outcome.proposal.request.quantity
            # Credit the day's dividends, then mark at this bar's close (whether or
            # not we traded).
            _credit_dividends(day)
            equity_curve.append((day_stamp[day], engine.value(_close_marks(day)).total_value))
        else:
            # Legacy same-bar model: decide, fill, and mark all on this close.
            report = AgentRunner(strategy, engine, _make_source(_quote_map(day))).run_cycle(
                now=day_stamp[day]
            )
            trades += report.num_filled
            for outcome in report.outcomes:
                if outcome.status == "FILLED" and outcome.fill_price is not None:
                    traded_notional += outcome.fill_price * outcome.proposal.request.quantity
            _credit_dividends(day)
            equity_curve.append((day_stamp[day], engine.value(_close_marks(day)).total_value))

    end_value = equity_curve[-1][1] if equity_curve else start_value
    total_return = (end_value - start_value) / start_value * 100 if start_value > 0 else Decimal(0)

    values = [start_value, *(v for _, v in equity_curve)]
    max_drawdown = metrics.max_drawdown_pct(values)
    cagr = metrics.cagr_pct(start_value, end_value, len(days))
    sharpe = metrics.sharpe(metrics.simple_returns([v for _, v in equity_curve]))
    calmar = cagr / float(max_drawdown) if cagr is not None and max_drawdown > 0 else None
    turnover = traded_notional / start_value if start_value > 0 else Decimal(0)

    return BacktestResult(
        start_value=start_value,
        end_value=end_value,
        total_return_pct=total_return,
        cagr_pct=_round2(cagr),
        sharpe=_round2(sharpe),
        calmar=_round2(calmar),
        max_drawdown_pct=max_drawdown,
        turnover=turnover.quantize(Decimal("0.01")),
        trades=trades,
        days=len(days),
        first_day=day_stamp[days[0]] if days else None,
        last_day=day_stamp[days[-1]] if days else None,
        equity_curve=equity_curve,
    )


def buy_hold_return_pct(candles: list[Candle], *, annual_yield: float = 0.0) -> Decimal | None:
    """Buy-and-hold return of a single symbol over the candle window.

    With ``annual_yield`` > 0 the result is a total return: the price return plus the
    dividend contribution ``annual_yield * (sessions / 252)`` over the window - the
    benchmark counterpart to :func:`run_backtest`'s ``dividend_yields``, so an
    excess-vs-benchmark comparison stays apples-to-apples (both total-return or both
    price-only).
    """
    if len(candles) < 2:
        return None
    first, last = candles[0].close, candles[-1].close
    if first <= 0:
        return None
    price_return = (last - first) / first * 100
    if annual_yield:
        price_return += Decimal(str(annual_yield)) * 100 * Decimal(len(candles)) / _TRADING_DAYS
    return price_return


# --- Walk-forward (robustness across multiple windows) ----------------------

# Rebuild a strategy from a fold's truncated history (history strategies hold
# their candles at construction; simple strategies ignore the arguments).
StrategyBuilder = Callable[[dict[str, list[Candle]], list[Candle]], Strategy]


class WalkForwardFold(BaseModel):
    end_day: datetime
    return_pct: Decimal
    sharpe: Decimal | None
    max_drawdown_pct: Decimal
    benchmark_return_pct: Decimal | None
    excess_pct: Decimal | None
    passed: bool


class WalkForwardResult(BaseModel):
    folds: list[WalkForwardFold]
    mean_return_pct: Decimal
    median_return_pct: Decimal
    worst_return_pct: Decimal
    mean_excess_pct: Decimal | None
    pass_rate: float  # fraction of folds that cleared the promotion gates


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def walk_forward(
    build_strategy: StrategyBuilder,
    engine_factory: Callable[[], PaperEngine],
    bars_by_symbol: dict[str, list[Candle]],
    benchmark_candles: list[Candle],
    *,
    window: int,
    step: int,
    folds: int,
    cost_bps: float = 0.0,
    next_bar_fill: bool = True,
    point_in_time: bool = True,
    dividend_yields: dict[str, float] | None = None,
    benchmark_yield: float = 0.0,
) -> WalkForwardResult:
    """Backtest one strategy over several trailing windows and summarize consistency.

    Fold ``k`` measures the ``window`` trading days ending ``k * step`` days before
    the most recent bar; earlier bars remain available for lookback. Each fold gets
    a fresh strategy (rebuilt from that fold's truncated history) and a fresh paper
    engine. The summary reports mean/median/worst return, mean excess over the
    benchmark, and the fraction of folds that cleared the promotion gates - so a
    single lucky window can't masquerade as a robust edge.

    ``point_in_time`` (default) restricts each fold to names already listed at the
    start of its measured window (first bar <= window start), so a name is never
    held in a fold before it began trading - removing backfill survivorship bias.
    It does not fix inclusion or delisting bias (see :mod:`schwab_trader.survivorship`).

    ``dividend_yields`` / ``benchmark_yield`` make both sides total-return so the
    excess-vs-benchmark stays apples-to-apples (both include dividends, or neither).
    """
    all_days = sorted({candle.date for candles in bars_by_symbol.values() for candle in candles})
    records: list[WalkForwardFold] = []
    for k in range(folds):
        end_idx = len(all_days) - 1 - k * step
        if end_idx < window:
            break  # not enough history for another fold
        end_date = all_days[end_idx]
        fold_bars = {
            symbol: [c for c in candles if c.date <= end_date]
            for symbol, candles in bars_by_symbol.items()
        }
        if point_in_time:
            # Point-in-time universe: drop names not yet listed at the window start.
            window_start = all_days[end_idx - window + 1]
            listed = set(survivorship.eligible_as_of(fold_bars, window_start))
            fold_bars = {s: c for s, c in fold_bars.items() if s in listed}
        fold_bench = [c for c in benchmark_candles if c.date <= end_date]
        strategy = build_strategy(fold_bars, fold_bench)
        result = run_backtest(
            strategy,
            fold_bars,
            engine_factory(),
            window=window,
            cost_bps=cost_bps,
            next_bar_fill=next_bar_fill,
            dividend_yields=dividend_yields,
        )
        bench_ret = (
            buy_hold_return_pct(fold_bench[-window:], annual_yield=benchmark_yield)
            if fold_bench
            else None
        )
        excess = result.total_return_pct - bench_ret if bench_ret is not None else None
        records.append(
            WalkForwardFold(
                end_day=end_date,
                return_pct=result.total_return_pct,
                sharpe=result.sharpe,
                max_drawdown_pct=result.max_drawdown_pct,
                benchmark_return_pct=bench_ret,
                excess_pct=excess,
                passed=evaluate_gates(result).passed,
            )
        )

    records.reverse()  # oldest fold first
    returns = [r.return_pct for r in records]
    excesses = [r.excess_pct for r in records if r.excess_pct is not None]
    passed = sum(1 for r in records if r.passed)
    return WalkForwardResult(
        folds=records,
        mean_return_pct=(sum(returns, Decimal(0)) / len(returns)) if returns else Decimal(0),
        median_return_pct=_median(returns) if returns else Decimal(0),
        worst_return_pct=min(returns) if returns else Decimal(0),
        mean_excess_pct=(sum(excesses, Decimal(0)) / len(excesses)) if excesses else None,
        pass_rate=(passed / len(records)) if records else 0.0,
    )
