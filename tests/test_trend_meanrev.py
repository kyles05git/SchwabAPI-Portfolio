"""Tests for TrendStrategy, MeanReversionStrategy, LowVolatilityStrategy (offline)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from schwab_trader.agent import (
    LowVolatilityStrategy,
    MarketContext,
    MeanReversionStrategy,
    TrendStrategy,
)
from schwab_trader.market_data import Candle, Quote

NOW = datetime(2026, 7, 16, 15, 0, tzinfo=UTC)
START = datetime(2025, 5, 1, tzinfo=UTC)


def _series(symbol: str, closes: list[float]) -> list[Candle]:
    return [
        Candle(symbol=symbol, date=START + timedelta(days=i), close=Decimal(str(c)))
        for i, c in enumerate(closes)
    ]


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


def _flat_spy() -> list[Candle]:
    return _series("SPY", [400.0] * 300)  # calm, flat -> regime doesn't gate exposure hard


# --- Trend ------------------------------------------------------------------


def test_trend_buys_uptrend_not_downtrend() -> None:
    up = _series("UP", [100.0 + i * 0.4 for i in range(300)])  # last well above 200MA
    down = _series("DOWN", [200.0 - i * 0.4 for i in range(300)])  # last below 200MA
    quotes = {"UP": _quote("UP", "50.00"), "DOWN": _quote("DOWN", "80.00")}
    strat = TrendStrategy(
        ["UP", "DOWN"], history={"UP": up, "DOWN": down}, benchmark_history=_flat_spy()
    )
    symbols = [p.request.symbol for p in strat.decide(_context("5000", quotes))]
    assert "UP" in symbols
    assert "DOWN" not in symbols


def test_trend_exits_when_below_average() -> None:
    # Held DOWN is now below its 200MA -> should be sold.
    down = _series("DOWN", [200.0 - i * 0.4 for i in range(300)])
    quotes = {"DOWN": _quote("DOWN", "80.00")}
    strat = TrendStrategy(["DOWN"], history={"DOWN": down}, benchmark_history=_flat_spy())
    proposals = strat.decide(_context("1000", quotes, positions={"DOWN": 5}))
    sides = {p.request.symbol: p.request.side.value for p in proposals}
    assert sides.get("DOWN") == "SELL"


# --- Mean reversion ---------------------------------------------------------


def test_mean_reversion_buys_oversold_in_uptrend() -> None:
    # Long uptrend, then a *recent* sharp dip -> price below 20MA but above 200MA.
    # The dip must be recent (few days) so the 20-day average still sits above it.
    closes = [100.0 + i * 0.5 for i in range(295)]  # steady uptrend to ~247
    closes += [closes[-1] * 0.88] * 5  # ~12% drop in the last 5 sessions
    hist = _series("DIP", closes)
    quotes = {"DIP": _quote("DIP", "50.00")}
    strat = MeanReversionStrategy(["DIP"], history={"DIP": hist}, benchmark_history=_flat_spy())
    symbols = [p.request.symbol for p in strat.decide(_context("5000", quotes))]
    assert "DIP" in symbols  # oversold within an uptrend -> buy


def test_mean_reversion_skips_downtrend_knife() -> None:
    # Falling the whole way: below both short and long average -> not a buy.
    hist = _series("KNIFE", [300.0 - i for i in range(300)])
    quotes = {"KNIFE": _quote("KNIFE", "10.00")}
    strat = MeanReversionStrategy(["KNIFE"], history={"KNIFE": hist}, benchmark_history=_flat_spy())
    assert strat.decide(_context("5000", quotes)) == []


def test_mean_reversion_sells_after_recovery() -> None:
    # Held name has fully recovered (above its short average) -> revert/sell.
    closes = [100.0 + i * 0.5 for i in range(300)]  # steadily up, currently at the high
    hist = _series("REC", closes)
    quotes = {"REC": _quote("REC", "50.00")}
    strat = MeanReversionStrategy(["REC"], history={"REC": hist}, benchmark_history=_flat_spy())
    proposals = strat.decide(_context("1000", quotes, positions={"REC": 5}))
    sides = {p.request.symbol: p.request.side.value for p in proposals}
    assert sides.get("REC") == "SELL"  # no longer oversold -> exit


# --- Low volatility ---------------------------------------------------------


def _calm(symbol: str, n: int = 200) -> list[Candle]:
    # Tiny alternating moves -> very low realized volatility.
    return _series(symbol, [100.0 + (0.05 if i % 2 else 0.0) for i in range(n)])


def _wild(symbol: str, n: int = 200) -> list[Candle]:
    # Large alternating moves -> high realized volatility, same average price.
    return _series(symbol, [100.0 + (8.0 if i % 2 else 0.0) for i in range(n)])


def test_low_vol_holds_the_calm_name_not_the_volatile_one() -> None:
    quotes = {"CALM": _quote("CALM", "50.00"), "WILD": _quote("WILD", "50.00")}
    strat = LowVolatilityStrategy(
        ["CALM", "WILD"],
        history={"CALM": _calm("CALM"), "WILD": _wild("WILD")},
        benchmark_history=_flat_spy(),
        max_positions=1,
    )
    symbols = [p.request.symbol for p in strat.decide(_context("5000", quotes))]
    assert "CALM" in symbols  # calmest name is the target
    assert "WILD" not in symbols


def test_low_vol_exits_a_name_no_longer_among_the_calmest() -> None:
    # WILD is held but a calmer name exists and only 1 slot -> WILD should be sold.
    quotes = {"CALM": _quote("CALM", "50.00"), "WILD": _quote("WILD", "50.00")}
    strat = LowVolatilityStrategy(
        ["CALM", "WILD"],
        history={"CALM": _calm("CALM"), "WILD": _wild("WILD")},
        benchmark_history=_flat_spy(),
        max_positions=1,
    )
    proposals = strat.decide(_context("1000", quotes, positions={"WILD": 5}))
    sides = {p.request.symbol: p.request.side.value for p in proposals}
    assert sides.get("WILD") == "SELL"
