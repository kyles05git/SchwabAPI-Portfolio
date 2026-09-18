"""Tests for PostEarningsDriftStrategy.decide() (offline)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from schwab_trader import pead
from schwab_trader.agent import MarketContext, PostEarningsDriftStrategy
from schwab_trader.market_data import Quote
from schwab_trader.models import OrderSide
from schwab_trader.sec_edgar import Fact
from schwab_trader.sec_store import SecStore

_AS_OF = datetime(2025, 8, 20, 15, 0, tzinfo=UTC)


def _quarter_ends(last_end: date, n: int) -> list[date]:
    """n consecutive quarter-ends ending at last_end (oldest first)."""
    ends = [last_end]
    for _ in range(n - 1):
        d = ends[-1]
        # step back ~one quarter to the prior quarter-end
        month = d.month - 3
        year = d.year
        if month <= 0:
            month += 12
            year -= 1
        # normalize to a quarter-end day
        end_day = {3: 31, 6: 30, 9: 30, 12: 31}[month]
        ends.append(date(year, month, end_day))
    return list(reversed(ends))


def _seed(
    store: SecStore, ticker: str, values: list[int], last_end: date, last_filed: date
) -> None:
    ends = _quarter_ends(last_end, len(values))
    facts = []
    for end, val in zip(ends, values, strict=True):
        filed = last_filed if end == last_end else end + timedelta(days=40)
        facts.append(
            Fact(
                ticker=ticker,
                cik=1,
                concept="NetIncomeLoss",
                unit="USD",
                period_start=end - timedelta(days=89),
                period_end=end,
                value=Decimal(val),
                fiscal_year=end.year,
                fiscal_period="Q",
                form="10-Q",
                filed=filed,
                accession=f"{ticker}-{end.isoformat()}",
                frame=None,
            )
        )
    store.upsert(facts)


def _quote(symbol: str, price: str) -> Quote:
    p = Decimal(price)
    return Quote(
        symbol=symbol,
        bid=p,
        ask=p,
        last=p,
        mark=p,
        previous_close=p,
        quote_time=_AS_OF,
    )


_ACCEL = [100, 110, 120, 130, 200, 220, 240, 260, 400, 440]  # strong positive SUE
_DECLINE = [100, 120, 140, 160, 220, 250, 280, 310, 200, 180]  # negative latest SUE


def _context(quotes: dict[str, Quote]) -> MarketContext:
    return MarketContext(
        now=_AS_OF,
        cash=Decimal("10000"),
        positions={},
        quotes=quotes,
        equity=Decimal("10000"),
    )


def test_buys_fresh_positive_surprise(tmp_path: Path) -> None:
    store = SecStore(tmp_path / "sec.sqlite3")
    # UP: strong surprise, filed 19 days ago (inside the drift window).
    _seed(store, "UP", _ACCEL, date(2025, 6, 30), date(2025, 8, 1))
    strat = PostEarningsDriftStrategy(["UP"], store=store, max_positions=5)
    proposals = strat.decide(_context({"UP": _quote("UP", "100.00")}))
    assert len(proposals) == 1
    assert proposals[0].request.side is OrderSide.BUY
    assert proposals[0].request.symbol == "UP"


def test_skips_stale_announcement(tmp_path: Path) -> None:
    store = SecStore(tmp_path / "sec.sqlite3")
    # Same strong surprise, but the latest filing is ~7 months old (drift is over).
    _seed(store, "OLD", _ACCEL, date(2024, 12, 31), date(2025, 1, 20))
    strat = PostEarningsDriftStrategy(["OLD"], store=store, max_positions=5)
    assert strat.decide(_context({"OLD": _quote("OLD", "100.00")})) == []


def test_skips_negative_surprise(tmp_path: Path) -> None:
    store = SecStore(tmp_path / "sec.sqlite3")
    # Fresh filing, but the latest quarter fell year-over-year -> negative SUE -> long-only skip.
    _seed(store, "DN", _DECLINE, date(2025, 6, 30), date(2025, 8, 1))
    strat = PostEarningsDriftStrategy(["DN"], store=store, max_positions=5)
    assert strat.decide(_context({"DN": _quote("DN", "100.00")})) == []


def test_ranks_and_caps_to_top_n(tmp_path: Path) -> None:
    store = SecStore(tmp_path / "sec.sqlite3")
    _seed(store, "AAA", _ACCEL, date(2025, 6, 30), date(2025, 8, 1))
    _seed(
        store,
        "BBB",
        [100, 110, 120, 130, 150, 160, 170, 180, 210, 215],
        date(2025, 6, 30),
        date(2025, 8, 1),
    )
    # SUE is standardized by each name's own surprise volatility, so the winner is the
    # higher-SUE name (not necessarily the larger absolute surprise). Assert the strategy
    # picks whichever the signal actually ranks first under max_positions=1.
    as_of = _AS_OF.date()
    sue_a = pead.pead_signal(store, "AAA", as_of)
    sue_b = pead.pead_signal(store, "BBB", as_of)
    assert sue_a is not None and sue_b is not None
    winner = "AAA" if sue_a.sue >= sue_b.sue else "BBB"

    strat = PostEarningsDriftStrategy(["AAA", "BBB"], store=store, max_positions=1)
    quotes = {"AAA": _quote("AAA", "100.00"), "BBB": _quote("BBB", "100.00")}
    proposals = strat.decide(_context(quotes))
    assert [p.request.symbol for p in proposals] == [winner]
