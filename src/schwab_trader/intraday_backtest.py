"""Session-aware intraday backtester for day-trading strategies.

The daily backtester runs one cycle per day; day-trading strategies (SPY noise-area
momentum, ORB) act *within* a session on minute bars, anchored to the open and the
running session VWAP. This engine replays intraday bars grouped by session, tracks a
single all-in position (long/short/flat), forces a flat close at each session end (no
overnight risk), and charges a per-side cost (spread + fees + slippage).

A strategy sees a :class:`BarContext` for every bar (the bar, session open/high/low,
running VWAP, minutes since open, current position) and returns an :class:`Action`.
Stops (e.g. a VWAP stop) are expressed by the strategy returning ``EXIT`` on the bar
where its condition trips - the engine stays simple. Sizing is all-in, unlevered;
per-session returns feed Sharpe/drawdown via :mod:`schwab_trader.metrics`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from itertools import groupby

from pydantic import BaseModel

from schwab_trader import metrics
from schwab_trader.market_data import Candle

_TRADING_DAYS_PER_YEAR = 252


class Side(Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class Action(Enum):
    HOLD = "HOLD"
    ENTER_LONG = "ENTER_LONG"
    ENTER_SHORT = "ENTER_SHORT"
    EXIT = "EXIT"


@dataclass(frozen=True)
class OpenPosition:
    side: Side
    entry_price: Decimal
    entry_ts: datetime
    entry_index: int


@dataclass(frozen=True)
class BarContext:
    """What a strategy sees for one bar."""

    bar: Candle
    session_date: date
    bar_index: int  # 0 = first bar of the session
    minutes_since_open: int
    session_open: Decimal
    session_high: Decimal
    session_low: Decimal
    vwap: Decimal  # running session VWAP through this bar
    position: OpenPosition | None


class IntradayStrategy(ABC):
    """Base class for intraday bar strategies."""

    name: str = "base"

    def on_session_start(self, session_date: date) -> None:  # noqa: B027 (optional hook)
        """Reset any per-session state (called before the first bar each day)."""

    @abstractmethod
    def on_bar(self, ctx: BarContext) -> Action:
        """Return the action for this bar given the session context and position."""


class Trade(BaseModel):
    side: str
    entry_ts: datetime
    entry_price: Decimal
    exit_ts: datetime
    exit_price: Decimal
    return_pct: Decimal  # net of costs, signed for direction
    bars_held: int
    exit_reason: str


class IntradayBacktestResult(BaseModel):
    strategy: str
    minutes: int
    cost_bps: float
    sessions: int
    num_trades: int
    total_return_pct: Decimal
    win_rate_pct: Decimal
    avg_trade_pct: Decimal
    sharpe: Decimal | None  # annualized from per-session returns
    max_drawdown_pct: Decimal
    first_day: date | None
    last_day: date | None
    equity_curve: list[tuple[date, Decimal]]
    trades: list[Trade]


def _typical(bar: Candle) -> Decimal:
    """(High + Low + Close) / 3 for VWAP, falling back to close if H/L missing."""
    if bar.high is not None and bar.low is not None:
        return (bar.high + bar.low + bar.close) / 3
    return bar.close


def trade_return(
    side: Side, entry: Decimal, exit_price: Decimal, cost_per_side: Decimal
) -> Decimal:
    """Signed round-trip return on entry notional, net of a per-side cost.

    A short earns the negative of the long return on the same price move. Shared by
    the single-symbol engine and the universe ORB backtest.
    """
    move = exit_price / entry - 1
    gross = move if side is Side.LONG else -move
    return gross - 2 * cost_per_side


def run_intraday_backtest(
    bars: list[Candle],
    strategy: IntradayStrategy,
    *,
    minutes: int,
    cost_bps: float = 1.0,
) -> IntradayBacktestResult:
    """Replay ``bars`` (oldest-first) session by session and measure ``strategy``.

    ``cost_bps`` is charged per side (so a round trip pays ``2 * cost_bps``). Positions
    are all-in and forced flat at each session close.
    """
    cost_per_side = Decimal(str(cost_bps)) / 10000
    equity = Decimal(1)
    trades: list[Trade] = []
    session_returns: list[Decimal] = []
    equity_curve: list[tuple[date, Decimal]] = []

    for session_date, group in groupby(bars, key=lambda candle: candle.date.date()):
        session_bars = list(group)
        if not session_bars:
            continue
        strategy.on_session_start(session_date)
        session_open = session_bars[0].open or session_bars[0].close
        session_high = session_open
        session_low = session_open
        open_ts = session_bars[0].date
        cum_pv = Decimal(0)
        cum_vol = Decimal(0)
        position: OpenPosition | None = None
        session_return = Decimal(1)
        traded_this_session = False

        for index, bar in enumerate(session_bars):
            if bar.high is not None:
                session_high = max(session_high, bar.high)
            if bar.low is not None:
                session_low = min(session_low, bar.low)
            cum_pv += _typical(bar) * bar.volume
            cum_vol += bar.volume
            vwap = (cum_pv / cum_vol) if cum_vol > 0 else bar.close

            ctx = BarContext(
                bar=bar,
                session_date=session_date,
                bar_index=index,
                minutes_since_open=int((bar.date - open_ts).total_seconds() // 60),
                session_open=session_open,
                session_high=session_high,
                session_low=session_low,
                vwap=vwap,
                position=position,
            )
            action = strategy.on_bar(ctx)

            if position is None and action in (Action.ENTER_LONG, Action.ENTER_SHORT):
                side = Side.LONG if action is Action.ENTER_LONG else Side.SHORT
                position = OpenPosition(side, bar.close, bar.date, index)
            elif position is not None and action is Action.EXIT:
                ret = trade_return(position.side, position.entry_price, bar.close, cost_per_side)
                trades.append(
                    _make_trade(position, bar, ret, index - position.entry_index, "signal")
                )
                session_return *= Decimal(1) + ret
                traded_this_session = True
                position = None

        if position is not None:  # force flat at the session close (no overnight risk)
            last = session_bars[-1]
            ret = trade_return(position.side, position.entry_price, last.close, cost_per_side)
            trades.append(
                _make_trade(
                    position, last, ret, len(session_bars) - 1 - position.entry_index, "eod"
                )
            )
            session_return *= Decimal(1) + ret
            traded_this_session = True

        if traded_this_session:
            equity *= session_return
            session_returns.append(session_return - 1)
        equity_curve.append((session_date, equity))

    return _summarize(strategy, minutes, cost_bps, trades, session_returns, equity, equity_curve)


def _make_trade(
    position: OpenPosition, exit_bar: Candle, ret: Decimal, bars_held: int, reason: str
) -> Trade:
    return Trade(
        side=position.side.value,
        entry_ts=position.entry_ts,
        entry_price=position.entry_price,
        exit_ts=exit_bar.date,
        exit_price=exit_bar.close,
        return_pct=(ret * 100),
        bars_held=bars_held,
        exit_reason=reason,
    )


def _summarize(
    strategy: IntradayStrategy,
    minutes: int,
    cost_bps: float,
    trades: list[Trade],
    session_returns: list[Decimal],
    equity: Decimal,
    equity_curve: list[tuple[date, Decimal]],
) -> IntradayBacktestResult:
    wins = sum(1 for t in trades if t.return_pct > 0)
    num = len(trades)
    avg = (sum((t.return_pct for t in trades), Decimal(0)) / num) if num else Decimal(0)
    win_rate = (Decimal(wins) / num * 100) if num else Decimal(0)
    sharpe = metrics.sharpe(
        [float(r) for r in session_returns], periods_per_year=_TRADING_DAYS_PER_YEAR
    )
    values = [value for _, value in equity_curve] or [Decimal(1)]
    return IntradayBacktestResult(
        strategy=strategy.name,
        minutes=minutes,
        cost_bps=cost_bps,
        sessions=len(equity_curve),
        num_trades=num,
        total_return_pct=(equity - 1) * 100,
        win_rate_pct=win_rate.quantize(Decimal("0.1")),
        avg_trade_pct=avg.quantize(Decimal("0.001")),
        sharpe=Decimal(str(round(sharpe, 2))) if sharpe is not None else None,
        max_drawdown_pct=metrics.max_drawdown_pct(values),
        first_day=equity_curve[0][0] if equity_curve else None,
        last_day=equity_curve[-1][0] if equity_curve else None,
        equity_curve=equity_curve,
        trades=trades,
    )
