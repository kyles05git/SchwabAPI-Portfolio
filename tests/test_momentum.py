"""Tests for MomentumStrategy (offline; synthetic candles + quotes)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from schwab_trader.agent import MarketContext, MomentumStrategy
from schwab_trader.market_data import Candle, Quote

NOW = datetime(2026, 7, 16, 15, 0, tzinfo=UTC)
START = datetime(2025, 6, 1, tzinfo=UTC)


def _history(symbol: str, closes: list[float]) -> list[Candle]:
    return [
        Candle(symbol=symbol, date=START + timedelta(days=i), close=Decimal(str(c)))
        for i, c in enumerate(closes)
    ]


def _rising(symbol: str, start: float = 100.0, step: float = 0.5, n: int = 300) -> list[Candle]:
    return _history(symbol, [start + i * step for i in range(n)])


def _falling(symbol: str, start: float = 250.0, step: float = 0.5, n: int = 300) -> list[Candle]:
    return _history(symbol, [start - i * step for i in range(n)])


def _quote(symbol: str, price: str) -> Quote:
    p = Decimal(price)
    return Quote(symbol=symbol, bid=p, ask=p, last=p, mark=p, quote_time=NOW)


def _context(
    cash: str, quotes: dict[str, Quote], positions: dict[str, int] | None = None
) -> MarketContext:
    pos = positions or {}
    equity = Decimal(cash) + sum(
        (Decimal(q) * quotes[s].last for s, q in pos.items() if quotes.get(s) and quotes[s].last),
        Decimal(0),
    )
    return MarketContext(now=NOW, cash=Decimal(cash), positions=pos, quotes=quotes, equity=equity)


def _bull_spy() -> list[Candle]:
    return _rising("SPY", start=400.0, step=0.3)


def test_buys_top_ranked_names() -> None:
    history = {"WIN": _rising("WIN"), "LOSE": _falling("LOSE")}
    quotes = {"WIN": _quote("WIN", "250.00"), "LOSE": _quote("LOSE", "100.00")}
    strat = MomentumStrategy(
        ["WIN", "LOSE"], history=history, benchmark_history=_bull_spy(), max_positions=1
    )
    proposals = strat.decide(_context("5000", quotes))
    symbols = [p.request.symbol for p in proposals]
    assert "WIN" in symbols
    assert "LOSE" not in symbols  # weak momentum excluded


def test_no_history_yields_no_trades() -> None:
    strat = MomentumStrategy(["AAA"], history={}, benchmark_history=[])
    assert strat.decide(_context("5000", {"AAA": _quote("AAA", "10")})) == []


def test_exits_names_that_fall_out_of_top_set() -> None:
    # Hold LOSE (now weak); WIN is the only top name -> sell LOSE, buy WIN.
    history = {"WIN": _rising("WIN"), "LOSE": _falling("LOSE")}
    quotes = {"WIN": _quote("WIN", "100.00"), "LOSE": _quote("LOSE", "100.00")}
    strat = MomentumStrategy(
        ["WIN", "LOSE"], history=history, benchmark_history=_bull_spy(), max_positions=1
    )
    proposals = strat.decide(_context("5000", quotes, positions={"LOSE": 5}))
    sides = {p.request.symbol: p.request.side.value for p in proposals}
    assert sides.get("LOSE") == "SELL"  # dropped out of the top set
    assert sides.get("WIN") == "BUY"  # rotated into the top set


def test_regime_scales_exposure_down() -> None:
    # Isolate the regime cap: fraction=1.0 so only the gross-exposure cap scales
    # deployment. A bull tape (SPY uptrend) deploys more than a bear tape.
    names = ["AA", "BB", "CC", "DD", "EE", "FF"]
    history = {n: _rising(n, start=100.0 + i * 3) for i, n in enumerate(names)}
    quotes = {n: _quote(n, "50.00") for n in names}

    def total(spy: list[Candle]) -> int:
        strat = MomentumStrategy(
            names,
            history=history,
            benchmark_history=spy,
            max_positions=6,
            max_position_fraction=Decimal("1.0"),
        )
        return sum(p.request.quantity for p in strat.decide(_context("5000", quotes)))

    bull = total(_rising("SPY", 400, 0.3))
    bear = total(_falling("SPY", 600, 0.3))
    assert bear < bull  # regime cap shrinks deployment in a downtrend


def test_position_fraction_caps_single_name() -> None:
    history = {"ONLY": _rising("ONLY")}
    quotes = {"ONLY": _quote("ONLY", "100.00")}
    # 10% of $5000 equity = $500 cap -> 5 shares at $100, despite full-exposure regime.
    strat = MomentumStrategy(
        ["ONLY"],
        history=history,
        benchmark_history=_bull_spy(),
        max_positions=8,
        max_position_fraction=Decimal("0.10"),
    )
    proposals = strat.decide(_context("5000", quotes))
    assert len(proposals) == 1
    assert proposals[0].request.quantity == 5
