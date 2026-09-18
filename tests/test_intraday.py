"""Tests for the intraday high-turnover reversion strategy. Pure, offline."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from schwab_trader.agent import IntradayReversionStrategy, MarketContext
from schwab_trader.market_data import Quote
from schwab_trader.models import OrderSide

NOW = datetime(2026, 7, 14, 15, 0, tzinfo=UTC)


def _quote(symbol: str, *, last: str, prev_close: str) -> Quote:
    p = Decimal(last)
    return Quote(
        symbol=symbol,
        last=p,
        previous_close=Decimal(prev_close),
        ask=p,
        bid=p,
        mark=p,
        quote_time=NOW,
    )


def _context(
    cash: str, quotes: dict[str, Quote], positions: dict[str, int] | None = None
) -> MarketContext:
    return MarketContext(
        now=NOW,
        cash=Decimal(cash),
        positions=positions or {},
        quotes=quotes,
        equity=Decimal(cash),
    )


def test_buys_biggest_intraday_dips() -> None:
    strat = IntradayReversionStrategy(["AAA", "BBB", "CCC"], max_positions=2)
    quotes = {
        "AAA": _quote("AAA", last="99.5", prev_close="100"),  # -0.5% (at threshold)
        "BBB": _quote("BBB", last="97", prev_close="100"),  # -3% (biggest dip)
        "CCC": _quote("CCC", last="100", prev_close="100"),  # flat, no buy
    }
    props = strat.decide(_context("1000", quotes))
    buys = [p for p in props if p.request.side is OrderSide.BUY]
    assert {p.request.symbol for p in buys} == {"AAA", "BBB"}  # CCC not dipping


def test_sells_reverted_position() -> None:
    strat = IntradayReversionStrategy(["AAA"])
    # Held AAA has recovered to prior close -> scalp exit.
    quotes = {"AAA": _quote("AAA", last="100", prev_close="100")}
    props = strat.decide(_context("0", quotes, positions={"AAA": 10}))
    sells = [p for p in props if p.request.side is OrderSide.SELL]
    assert len(sells) == 1
    assert sells[0].request.symbol == "AAA"
    assert sells[0].request.quantity == 10


def test_stops_out_extended_dip() -> None:
    strat = IntradayReversionStrategy(["AAA"], stop_pct=Decimal("0.02"))
    quotes = {"AAA": _quote("AAA", last="97", prev_close="100")}  # -3% < -2% stop
    props = strat.decide(_context("0", quotes, positions={"AAA": 10}))
    assert [p.request.side for p in props] == [OrderSide.SELL]
    assert "stop" in props[0].rationale


def test_holds_position_still_in_the_dip() -> None:
    strat = IntradayReversionStrategy(["AAA"], stop_pct=Decimal("0.05"))
    # Down 1%: past neither the scalp-exit (recovered) nor the 5% stop -> hold.
    quotes = {"AAA": _quote("AAA", last="99", prev_close="100")}
    props = strat.decide(_context("0", quotes, positions={"AAA": 10}))
    assert props == []


def test_no_buys_when_fully_positioned() -> None:
    strat = IntradayReversionStrategy(["AAA", "BBB"], max_positions=1)
    quotes = {
        "AAA": _quote("AAA", last="99", prev_close="100"),  # held, still dipping (hold)
        "BBB": _quote("BBB", last="97", prev_close="100"),  # dip, but no room
    }
    props = strat.decide(_context("1000", quotes, positions={"AAA": 5}))
    assert [p.request.symbol for p in props] == []  # at max_positions, no new buys


def test_registered_in_registry() -> None:
    from schwab_trader.agent import available_strategies, build_strategy

    assert "intraday" in available_strategies()
    assert isinstance(build_strategy("intraday", ["AAA"]), IntradayReversionStrategy)
