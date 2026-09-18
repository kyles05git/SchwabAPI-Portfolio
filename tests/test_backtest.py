"""Tests for the historical replay backtester (offline; synthetic candles)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from schwab_trader.agent import BuyHoldStrategy, HoldStrategy, MomentumStrategy
from schwab_trader.backtest import (
    BacktestResult,
    buy_hold_return_pct,
    evaluate_gates,
    run_backtest,
    walk_forward,
)
from schwab_trader.market_data import Candle
from schwab_trader.paper import PaperEngine

START = datetime(2026, 1, 2, tzinfo=UTC)


def _series(symbol: str, closes: list[str]) -> list[Candle]:
    # Each bar opens where the prior bar closed (flat overnight). The default
    # backtester fills at the next bar's open, so a flat overnight lets these
    # sanity checks price fills at the prior close - matching the intended math.
    bars = []
    prev = Decimal(closes[0])
    for i, c in enumerate(closes):
        close = Decimal(c)
        bars.append(Candle(symbol=symbol, date=START + timedelta(days=i), open=prev, close=close))
        prev = close
    return bars


def _engine(tmp_path, cash: str = "1000.00") -> PaperEngine:
    return PaperEngine(tmp_path / "bt.sqlite3", starting_cash=Decimal(cash))


def test_buy_hold_captures_price_appreciation(tmp_path) -> None:
    # One name at $100 -> $110. Buy-hold with $1000 buys 10 shares day 1, then
    # rides them to $110 = $1100. Return ~ +10%.
    bars = {"AAA": _series("AAA", ["100", "105", "110"])}
    engine = _engine(tmp_path)
    result = run_backtest(BuyHoldStrategy(["AAA"]), bars, engine)

    assert result.days == 3
    assert result.trades == 1  # bought once on day 1
    assert result.start_value == Decimal("1000.00")
    assert result.end_value == Decimal("1100.00")
    assert result.total_return_pct == Decimal("10.00")
    assert len(result.equity_curve) == 3


def test_next_bar_open_fill_has_no_look_ahead(tmp_path) -> None:
    # Day 1 close = 100: the buy-hold order is decided here (sized 10 shares @ ~100).
    # Day 2 GAPS UP to open 130. The honest model fills at that open, where 10 shares
    # cost 1300 > 1000 cash, so the order is rejected - the strategy cannot trade a
    # price it never saw when it sized the order. The legacy same-bar model instead
    # "fills" 10 @ the day-1 close of 100 and rides to 140 = 1400, capturing a move it
    # could not have executed on. The gap between the two is pure look-ahead.
    bars = {
        "AAA": [
            Candle(symbol="AAA", date=START, open=Decimal("100"), close=Decimal("100")),
            Candle(
                symbol="AAA",
                date=START + timedelta(days=1),
                open=Decimal("130"),
                close=Decimal("140"),
            ),
        ]
    }
    honest = run_backtest(BuyHoldStrategy(["AAA"]), bars, _engine(tmp_path / "h"))
    assert honest.trades == 0  # gap-up open is unaffordable at yesterday's size
    assert honest.end_value == Decimal("1000.00")

    look_ahead = run_backtest(
        BuyHoldStrategy(["AAA"]), bars, _engine(tmp_path / "la"), next_bar_fill=False
    )
    assert look_ahead.trades == 1
    assert look_ahead.end_value == Decimal("1400.00")  # same-bar fill captures the whole move
    assert honest.end_value < look_ahead.end_value


def test_hold_strategy_never_trades(tmp_path) -> None:
    bars = {"AAA": _series("AAA", ["100", "90", "120"])}
    result = run_backtest(HoldStrategy(["AAA"]), bars, _engine(tmp_path))
    assert result.trades == 0
    assert result.end_value == Decimal("1000.00")  # all cash, untouched
    assert result.total_return_pct == Decimal("0.00")


def test_max_drawdown_is_measured(tmp_path) -> None:
    # $100 -> $80 (peak-to-trough -20%) -> $90. Buy 10 shares; equity 1000, 800, 900.
    bars = {"AAA": _series("AAA", ["100", "80", "90"])}
    result = run_backtest(BuyHoldStrategy(["AAA"]), bars, _engine(tmp_path))
    assert result.max_drawdown_pct == Decimal("20.00")
    assert result.total_return_pct == Decimal("-10.00")


def test_equal_weight_across_two_names(tmp_path) -> None:
    # $1000 / 2 names = $500 each. AAA $50 -> 10 shares; BBB $100 -> 5 shares.
    bars = {
        "AAA": _series("AAA", ["50", "60"]),
        "BBB": _series("BBB", ["100", "100"]),
    }
    result = run_backtest(BuyHoldStrategy(["AAA", "BBB"]), bars, _engine(tmp_path))
    assert result.trades == 2
    # end: 10*60 + 5*100 = 600 + 500 = 1100
    assert result.end_value == Decimal("1100.00")


def test_buy_hold_return_helper() -> None:
    assert buy_hold_return_pct(_series("SPY", ["400", "440"])) == Decimal("10.00")
    assert buy_hold_return_pct(_series("SPY", ["400"])) is None  # need >= 2 points


def test_buy_hold_return_adds_dividend_total_return() -> None:
    # 252 sessions flat at 100 -> 0% price return; a 2% annual yield over exactly one
    # year adds ~2.0% total return (yield * sessions/252).
    year = _series("SPY", ["100"] * 252)
    price_only = buy_hold_return_pct(year)
    total = buy_hold_return_pct(year, annual_yield=0.02)
    assert price_only == Decimal("0.00")
    assert total is not None and abs(total - Decimal("2.00")) < Decimal("0.01")


def test_dividend_yields_lift_total_return(tmp_path) -> None:
    # Buy-and-hold a dividend payer: the total-return replay ends richer than the
    # price-only one by roughly the accrued dividend cash, all else equal.
    bars = {"AAA": _series("AAA", ["100"] * 60)}  # flat price -> isolate the dividend
    price_only = run_backtest(BuyHoldStrategy(["AAA"]), bars, _engine(tmp_path / "p"))
    total = run_backtest(
        BuyHoldStrategy(["AAA"]),
        bars,
        _engine(tmp_path / "t"),
        dividend_yields={"AAA": 0.10},  # a fat 10% yield so the drip is visible
    )
    assert price_only.end_value == Decimal("1000.00")  # flat price, no dividends
    assert total.end_value > price_only.end_value  # dividend cash accrued on the held shares


def test_no_dividend_yields_is_price_only(tmp_path) -> None:
    bars = {"AAA": _series("AAA", ["100", "105", "110"])}
    a = run_backtest(BuyHoldStrategy(["AAA"]), bars, _engine(tmp_path / "a"))
    b = run_backtest(BuyHoldStrategy(["AAA"]), bars, _engine(tmp_path / "b"), dividend_yields=None)
    assert a.end_value == b.end_value == Decimal("1100.00")


def test_metrics_populated_and_turnover(tmp_path) -> None:
    bars = {"AAA": _series("AAA", ["100", "105", "110", "108"])}
    result = run_backtest(BuyHoldStrategy(["AAA"]), bars, _engine(tmp_path))
    assert result.sharpe is not None  # varying returns -> defined Sharpe
    assert result.cagr_pct is not None
    # bought ~$1000 of AAA once on a $1000 sleeve -> roughly 1x turnover
    assert result.turnover >= Decimal("0.90")


def test_gates_fail_on_flat_hold(tmp_path) -> None:
    # A hold strategy: 0% return, 0 Sharpe/Calmar -> gates should fail.
    bars = {"AAA": _series("AAA", ["100", "101", "102", "103"])}
    result = run_backtest(HoldStrategy(["AAA"]), bars, _engine(tmp_path))
    gates = evaluate_gates(result)
    assert gates.passed is False
    assert any(not c.passed for c in gates.checks)


def test_window_measures_only_recent_days(tmp_path) -> None:
    # 10 days of history; window=3 measures only the last 3 trading days.
    bars = {"AAA": _series("AAA", [str(100 + i) for i in range(10)])}
    result = run_backtest(HoldStrategy(["AAA"]), bars, _engine(tmp_path), window=3)
    assert result.days == 3


def test_momentum_backtest_runs_with_lookback(tmp_path) -> None:
    # 300 sessions of history so the 253-day momentum lookback is available;
    # measure only the last 20. WIN rises, LOSE falls -> momentum holds WIN.
    win = _series("WIN", [str(100 + i) for i in range(300)])
    lose = _series("LOSE", [str(400 - i) for i in range(300)])
    spy = _series("SPY", [str(400 + i * 0.3) for i in range(300)])
    strat = MomentumStrategy(
        ["WIN", "LOSE"],
        history={"WIN": win, "LOSE": lose},
        benchmark_history=spy,
        max_positions=1,
        max_position_fraction=Decimal("1.0"),
    )
    result = run_backtest(strat, {"WIN": win, "LOSE": lose}, _engine(tmp_path, "10000"), window=20)
    assert result.days == 20
    assert result.trades >= 1  # established a momentum position in the window


def test_cost_bps_reduces_return(tmp_path) -> None:
    # Same buy-hold, with vs without cost. Cost must lower the ending value.
    bars = {"AAA": _series("AAA", ["100", "105", "110"])}
    free = run_backtest(BuyHoldStrategy(["AAA"]), bars, _engine(tmp_path / "a"))
    costed = run_backtest(BuyHoldStrategy(["AAA"]), bars, _engine(tmp_path / "b"), cost_bps=50.0)
    assert costed.end_value < free.end_value  # paid the spread on the buy
    assert costed.total_return_pct < free.total_return_pct


def test_zero_cost_is_frictionless(tmp_path) -> None:
    bars = {"AAA": _series("AAA", ["100", "110"])}
    result = run_backtest(BuyHoldStrategy(["AAA"]), bars, _engine(tmp_path), cost_bps=0.0)
    assert result.end_value == Decimal("1100.00")  # exact, no spread


def test_walk_forward_produces_folds_and_summary(tmp_path) -> None:
    # 400 sessions of a steady uptrend; 3 folds of 100 days stepping by 50.
    bars = {"AAA": _series("AAA", [str(round(100 + i * 0.5, 2)) for i in range(400)])}
    counter = {"n": 0}

    def factory() -> PaperEngine:
        counter["n"] += 1
        return PaperEngine(tmp_path / f"wf-{counter['n']}.sqlite3", starting_cash=Decimal("1000"))

    result = walk_forward(
        lambda _b, _bench: BuyHoldStrategy(["AAA"]),
        factory,
        bars,
        bars["AAA"],  # benchmark = the same rising series
        window=100,
        step=50,
        folds=3,
    )
    assert len(result.folds) == 3
    assert all(f.return_pct > 0 for f in result.folds)  # uptrend -> every fold up
    assert result.mean_return_pct > 0
    assert 0.0 <= result.pass_rate <= 1.0
    # folds are chronological (oldest first)
    assert result.folds[0].end_day < result.folds[-1].end_day


def test_walk_forward_stops_when_history_runs_out(tmp_path) -> None:
    bars = {"AAA": _series("AAA", [str(100 + i) for i in range(120)])}

    def factory() -> PaperEngine:
        return PaperEngine(tmp_path / "wf.sqlite3", starting_cash=Decimal("1000"))

    # window=100, step=50: fold 0 ok (ends at 120), fold 1 would end at day 70 (< window) -> stop.
    result = walk_forward(
        lambda _b, _bench: HoldStrategy(["AAA"]), factory, bars, [], window=100, step=50, folds=5
    )
    assert len(result.folds) == 1


def test_gates_pass_with_strong_synthetic_result() -> None:
    # Construct a strong result directly and check the gate thresholds.
    strong = BacktestResult(
        start_value=Decimal("1000"),
        end_value=Decimal("1200"),
        total_return_pct=Decimal("20"),
        cagr_pct=Decimal("30"),
        sharpe=Decimal("1.50"),
        calmar=Decimal("2.00"),
        max_drawdown_pct=Decimal("10"),
        turnover=Decimal("1.00"),
        trades=5,
        days=100,
        first_day=None,
        last_day=None,
        equity_curve=[],
    )
    assert evaluate_gates(strong).passed is True
