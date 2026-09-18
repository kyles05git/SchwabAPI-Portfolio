"""Tests for the value+momentum blend strategy (offline)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from schwab_trader.agent import MarketContext, ValueMomentumStrategy, _percentile_ranks
from schwab_trader.market_data import Candle, Quote
from schwab_trader.models import OrderSide
from schwab_trader.sec_edgar import Fact
from schwab_trader.sec_store import SecStore

START = datetime(2024, 1, 1, tzinfo=UTC)


def _series(symbol: str, closes: list[float]) -> list[Candle]:
    bars = []
    for i, close in enumerate(closes):
        c = Decimal(str(close))
        bars.append(
            Candle(symbol=symbol, date=START + timedelta(days=i), open=c, high=c, low=c, close=c)
        )
    return bars


def _fact(ticker: str, concept: str, val: float, *, unit: str = "USD") -> Fact:
    return Fact(
        ticker=ticker,
        cik=1,
        concept=concept,
        unit=unit,
        period_start=None,
        period_end=datetime(2023, 12, 31).date(),
        value=Decimal(str(val)),
        fiscal_year=2023,
        fiscal_period="FY",
        form="10-K",
        filed=datetime(2024, 2, 1).date(),
        accession="a",
        frame=None,
    )


def test_percentile_ranks() -> None:
    ranks = _percentile_ranks({"low": Decimal("1"), "mid": Decimal("5"), "high": Decimal("9")})
    assert ranks == {"low": 0.0, "mid": 0.5, "high": 1.0}
    assert _percentile_ranks({}) == {}
    assert _percentile_ranks({"solo": Decimal("3")}) == {"solo": 1.0}


def test_blend_prefers_name_strong_on_both_factors(tmp_path) -> None:
    n = 260
    # WINNER: strong uptrend (high momentum). LAGGARD: flat (low momentum).
    winner = _series("WINNER", [100 + i * 0.5 for i in range(n)])
    laggard = _series("LAGGARD", [100.0] * n)
    history = {"WINNER": winner, "LAGGARD": laggard}
    now = winner[-1].date

    store = SecStore(tmp_path / "sec.sqlite3")
    store.upsert(
        [
            # WINNER also cheap: high net income vs price -> high earnings yield.
            _fact("WINNER", "NetIncomeLoss", 2000),
            _fact("WINNER", "CommonStockSharesOutstanding", 100),
            # LAGGARD expensive: tiny net income -> low earnings yield.
            _fact("LAGGARD", "NetIncomeLoss", 10),
            _fact("LAGGARD", "CommonStockSharesOutstanding", 100),
        ]
    )

    def _q(sym: str, price: str) -> Quote:
        p = Decimal(price)
        return Quote(symbol=sym, bid=p, ask=p, last=p, mark=p, previous_close=p, quote_time=now)

    strat = ValueMomentumStrategy(
        ["WINNER", "LAGGARD"],
        history=history,
        benchmark_history=[],
        store=store,
        factor="earnings-yield",
        max_positions=1,
    )
    context = MarketContext(
        now=now,
        cash=Decimal("5000"),
        positions={},
        quotes={"WINNER": _q("WINNER", winner[-1].close), "LAGGARD": _q("LAGGARD", "100")},
        equity=Decimal("5000"),
    )
    proposals = strat.decide(context)
    buys = [p.request.symbol for p in proposals if p.request.side is OrderSide.BUY]
    assert buys == ["WINNER"]  # best on BOTH momentum and value


def test_multi_factor_blend_value_plus_quality(tmp_path) -> None:
    n = 260
    winner = _series("WINNER", [100 + i * 0.5 for i in range(n)])
    laggard = _series("LAGGARD", [100.0] * n)
    now = winner[-1].date

    store = SecStore(tmp_path / "sec.sqlite3")
    store.upsert(
        [
            _fact("WINNER", "NetIncomeLoss", 2000),  # cheap (high E/P) AND high ROE
            _fact("WINNER", "CommonStockSharesOutstanding", 100),
            _fact("WINNER", "StockholdersEquity", 500),
            _fact("LAGGARD", "NetIncomeLoss", 10),  # expensive AND low ROE
            _fact("LAGGARD", "CommonStockSharesOutstanding", 100),
            _fact("LAGGARD", "StockholdersEquity", 500),
        ]
    )

    def _q(sym: str, price: str) -> Quote:
        p = Decimal(price)
        return Quote(symbol=sym, bid=p, ask=p, last=p, mark=p, previous_close=p, quote_time=now)

    strat = ValueMomentumStrategy(
        ["WINNER", "LAGGARD"],
        history={"WINNER": winner, "LAGGARD": laggard},
        benchmark_history=[],
        store=store,
        factor="earnings-yield,roe",  # value + quality blend
        max_positions=1,
    )
    assert strat.factor == "earnings-yield,roe"
    context = MarketContext(
        now=now,
        cash=Decimal("5000"),
        positions={},
        quotes={"WINNER": _q("WINNER", winner[-1].close), "LAGGARD": _q("LAGGARD", "100")},
        equity=Decimal("5000"),
    )
    buys = [p.request.symbol for p in strat.decide(context) if p.request.side is OrderSide.BUY]
    assert buys == ["WINNER"]  # best on momentum + value + quality


def test_needs_both_momentum_and_value(tmp_path) -> None:
    # A name with momentum history but NO fundamentals is excluded from the blend.
    winner = _series("WINNER", [100 + i * 0.5 for i in range(260)])
    store = SecStore(tmp_path / "sec.sqlite3")  # empty -> no value scores
    strat = ValueMomentumStrategy(
        ["WINNER"], history={"WINNER": winner}, benchmark_history=[], store=store
    )
    q = Quote(
        symbol="WINNER",
        bid=Decimal("200"),
        ask=Decimal("200"),
        last=Decimal("200"),
        mark=Decimal("200"),
        previous_close=Decimal("200"),
        quote_time=winner[-1].date,
    )
    context = MarketContext(
        now=winner[-1].date,
        cash=Decimal("5000"),
        positions={},
        quotes={"WINNER": q},
        equity=Decimal("5000"),
    )
    assert strat.decide(context) == []  # no fundamentals -> not eligible
