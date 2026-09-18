"""Multi-symbol Opening-Range Breakout (ORB) with a relative-volume filter.

The single-symbol ORB is a poor fit for SPY; the research doc's ORB is a *universe*
strategy - each morning it ranks names by how unusually active they are ("stocks in
play" = elevated opening-period volume vs their own baseline) and trades opening-range
breakouts only on the top names. This module implements that portfolio version on the
local intraday panel.

Each session: for every symbol with enough history, compute relative opening volume
(opening-window volume / trailing-average opening volume); keep those above
``min_rvol``, take the top ``top_n``; run one ORB trade per selected name (break of the
first ``or_minutes`` range, stop at the opposite side, forced flat at the close);
equal-weight their returns into the day's P&L. Offline; reads pre-fetched bars.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from itertools import groupby

from pydantic import BaseModel

from schwab_trader import metrics
from schwab_trader.intraday_backtest import Side, trade_return
from schwab_trader.market_data import Candle

_TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class _SessionData:
    bars: list[Candle]
    open_ts: datetime
    or_high: Decimal
    or_low: Decimal
    opening_volume: int


class UniverseOrbResult(BaseModel):
    universe_size: int
    top_n: int
    min_rvol: float
    or_minutes: int
    minutes: int
    cost_bps: float
    sessions: int  # sessions with at least one selected name
    num_trades: int  # actual breakout trades
    total_return_pct: Decimal
    win_rate_pct: Decimal
    avg_trade_pct: Decimal
    long_trades: int
    short_trades: int
    avg_in_play: float  # avg selected names per traded session
    sharpe: Decimal | None
    max_drawdown_pct: Decimal
    first_day: date | None
    last_day: date | None
    equity_curve: list[tuple[date, Decimal]]
    daily_returns: list[tuple[date, Decimal]]  # per-traded-session return (for walk-forward)


def _session_data(session_bars: list[Candle], or_minutes: int) -> _SessionData | None:
    """Opening-range high/low and opening-window volume for one session, or None."""
    open_ts = session_bars[0].date
    or_high: Decimal | None = None
    or_low: Decimal | None = None
    opening_volume = 0
    for bar in session_bars:
        if (bar.date - open_ts).total_seconds() < or_minutes * 60:
            high = bar.high if bar.high is not None else bar.close
            low = bar.low if bar.low is not None else bar.close
            or_high = high if or_high is None else max(or_high, high)
            or_low = low if or_low is None else min(or_low, low)
            opening_volume += bar.volume
    if or_high is None or or_low is None:
        return None
    return _SessionData(session_bars, open_ts, or_high, or_low, opening_volume)


def _orb_trade(
    sd: _SessionData, or_minutes: int, cost_per_side: Decimal, no_entry_after_min: int
) -> tuple[Side, Decimal] | None:
    """Run one ORB trade over a session's bars; return (side, net return) or None."""
    side: Side | None = None
    entry: Decimal | None = None
    for bar in sd.bars:
        mso = (bar.date - sd.open_ts).total_seconds() / 60
        if mso < or_minutes:
            continue
        if side is None:
            if mso > no_entry_after_min:
                continue
            if bar.close > sd.or_high:
                side, entry = Side.LONG, bar.close
            elif bar.close < sd.or_low:
                side, entry = Side.SHORT, bar.close
        else:
            assert entry is not None
            hit = (side is Side.LONG and bar.close < sd.or_low) or (
                side is Side.SHORT and bar.close > sd.or_high
            )
            if hit:
                return side, trade_return(side, entry, bar.close, cost_per_side)
    if side is not None and entry is not None:  # forced flat at the close
        return side, trade_return(side, entry, sd.bars[-1].close, cost_per_side)
    return None


def run_universe_orb_backtest(
    bars_by_symbol: dict[str, list[Candle]],
    *,
    minutes: int,
    or_minutes: int = 5,
    top_n: int = 5,
    rvol_lookback: int = 20,
    min_rvol: float = 1.5,
    cost_bps: float = 1.0,
    no_entry_after_min: int = 210,
) -> UniverseOrbResult:
    """Backtest the relative-volume ORB portfolio over a universe of intraday bars."""
    cost_per_side = Decimal(str(cost_bps)) / 10000

    # Per symbol: ordered list of (session_date, _SessionData).
    per_symbol: dict[str, list[tuple[date, _SessionData]]] = {}
    all_days: set[date] = set()
    for symbol, bars in bars_by_symbol.items():
        sessions: list[tuple[date, _SessionData]] = []
        for day, group in groupby(bars, key=lambda candle: candle.date.date()):
            sd = _session_data(list(group), or_minutes)
            if sd is not None:
                sessions.append((day, sd))
                all_days.add(day)
        if sessions:
            per_symbol[symbol] = sessions

    # For each symbol, index its sessions by day and keep a trailing opening-volume list.
    by_day: dict[str, dict[date, _SessionData]] = {
        symbol: {day: sd for day, sd in sessions} for symbol, sessions in per_symbol.items()
    }
    ordered_days: dict[str, list[date]] = {
        symbol: [day for day, _ in sessions] for symbol, sessions in per_symbol.items()
    }

    equity = Decimal(1)
    equity_curve: list[tuple[date, Decimal]] = []
    session_returns: list[Decimal] = []
    trade_returns: list[Decimal] = []
    long_trades = 0
    short_trades = 0
    in_play_counts: list[int] = []

    for day in sorted(all_days):
        # Rank symbols in play by relative opening volume vs their own baseline.
        ranked: list[tuple[float, str]] = []
        for symbol, days in ordered_days.items():
            sd = by_day[symbol].get(day)
            if sd is None:
                continue
            idx = days.index(day)
            if idx < rvol_lookback:
                continue  # not enough baseline yet
            baseline = [by_day[symbol][d].opening_volume for d in days[idx - rvol_lookback : idx]]
            avg = sum(baseline) / rvol_lookback
            if avg <= 0:
                continue
            rvol = sd.opening_volume / avg
            if rvol >= min_rvol:
                ranked.append((rvol, symbol))
        if not ranked:
            continue
        ranked.sort(reverse=True)
        selected = [symbol for _, symbol in ranked[:top_n]]
        in_play_counts.append(len(selected))

        day_returns: list[Decimal] = []
        for symbol in selected:
            outcome = _orb_trade(by_day[symbol][day], or_minutes, cost_per_side, no_entry_after_min)
            if outcome is None:
                day_returns.append(Decimal(0))  # selected but no breakout -> flat slot
                continue
            side, ret = outcome
            day_returns.append(ret)
            trade_returns.append(ret)
            if side is Side.LONG:
                long_trades += 1
            else:
                short_trades += 1

        session_return = sum(day_returns, Decimal(0)) / len(day_returns)
        equity *= Decimal(1) + session_return
        session_returns.append(session_return)
        equity_curve.append((day, equity))

    return _summarize(
        bars_by_symbol,
        minutes=minutes,
        or_minutes=or_minutes,
        top_n=top_n,
        min_rvol=min_rvol,
        cost_bps=cost_bps,
        equity=equity,
        equity_curve=equity_curve,
        session_returns=session_returns,
        trade_returns=trade_returns,
        long_trades=long_trades,
        short_trades=short_trades,
        in_play_counts=in_play_counts,
    )


def _summarize(
    bars_by_symbol: dict[str, list[Candle]],
    *,
    minutes: int,
    or_minutes: int,
    top_n: int,
    min_rvol: float,
    cost_bps: float,
    equity: Decimal,
    equity_curve: list[tuple[date, Decimal]],
    session_returns: list[Decimal],
    trade_returns: list[Decimal],
    long_trades: int,
    short_trades: int,
    in_play_counts: list[int],
) -> UniverseOrbResult:
    num = len(trade_returns)
    wins = sum(1 for r in trade_returns if r > 0)
    avg = (sum(trade_returns, Decimal(0)) / num) if num else Decimal(0)
    win_rate = (Decimal(wins) / num * 100) if num else Decimal(0)
    sharpe = metrics.sharpe(
        [float(r) for r in session_returns], periods_per_year=_TRADING_DAYS_PER_YEAR
    )
    values = [value for _, value in equity_curve] or [Decimal(1)]
    avg_in_play = (sum(in_play_counts) / len(in_play_counts)) if in_play_counts else 0.0
    return UniverseOrbResult(
        universe_size=len(bars_by_symbol),
        top_n=top_n,
        min_rvol=min_rvol,
        or_minutes=or_minutes,
        minutes=minutes,
        cost_bps=cost_bps,
        sessions=len(equity_curve),
        num_trades=num,
        total_return_pct=(equity - 1) * 100,
        win_rate_pct=win_rate.quantize(Decimal("0.1")),
        avg_trade_pct=(avg * 100).quantize(Decimal("0.0001")),
        long_trades=long_trades,
        short_trades=short_trades,
        avg_in_play=round(avg_in_play, 1),
        sharpe=Decimal(str(round(sharpe, 2))) if sharpe is not None else None,
        max_drawdown_pct=metrics.max_drawdown_pct(values),
        first_day=equity_curve[0][0] if equity_curve else None,
        last_day=equity_curve[-1][0] if equity_curve else None,
        equity_curve=equity_curve,
        daily_returns=[
            (day, ret) for (day, _), ret in zip(equity_curve, session_returns, strict=True)
        ],
    )


class OrbFold(BaseModel):
    index: int
    first_day: date
    last_day: date
    sessions: int
    total_return_pct: Decimal
    sharpe: Decimal | None


class OrbWalkForwardResult(BaseModel):
    folds: list[OrbFold]
    mean_return_pct: Decimal
    median_return_pct: Decimal
    worst_return_pct: Decimal
    positive_folds: int
    full_return_pct: Decimal


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def walk_forward_orb(
    bars_by_symbol: dict[str, list[Candle]], *, folds: int, **params: object
) -> OrbWalkForwardResult:
    """Run the universe ORB once, then split its session returns into ``folds`` sequential
    windows and report per-fold consistency.

    This is a temporal-stability check (is the edge uniform across sub-periods, or one
    lucky stretch?) - not out-of-sample parameter selection, and not multi-regime unless
    the data spans multiple regimes. ``params`` are passed through to
    :func:`run_universe_orb_backtest`.
    """
    result = run_universe_orb_backtest(bars_by_symbol, **params)  # type: ignore[arg-type]
    series = result.daily_returns
    if len(series) < folds:
        msg = f"Not enough traded sessions ({len(series)}) for {folds} folds."
        raise ValueError(msg)

    chunk = len(series) // folds
    fold_models: list[OrbFold] = []
    returns_pct: list[Decimal] = []
    for k in range(folds):
        start = k * chunk
        end = len(series) if k == folds - 1 else (k + 1) * chunk
        segment = series[start:end]
        rs = [r for _, r in segment]
        compounded = Decimal(1)
        for r in rs:
            compounded *= Decimal(1) + r
        total = (compounded - 1) * 100
        returns_pct.append(total)
        sharpe = metrics.sharpe([float(r) for r in rs], periods_per_year=_TRADING_DAYS_PER_YEAR)
        fold_models.append(
            OrbFold(
                index=k + 1,
                first_day=segment[0][0],
                last_day=segment[-1][0],
                sessions=len(segment),
                total_return_pct=total,
                sharpe=Decimal(str(round(sharpe, 2))) if sharpe is not None else None,
            )
        )

    return OrbWalkForwardResult(
        folds=fold_models,
        mean_return_pct=sum(returns_pct, Decimal(0)) / len(returns_pct),
        median_return_pct=_median(returns_pct),
        worst_return_pct=min(returns_pct),
        positive_folds=sum(1 for r in returns_pct if r > 0),
        full_return_pct=result.total_return_pct,
    )
