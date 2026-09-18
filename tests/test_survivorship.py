"""Tests for survivorship-bias tooling and point-in-time walk-forward (offline)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from schwab_trader import survivorship
from schwab_trader.agent import BuyHoldStrategy
from schwab_trader.backtest import walk_forward
from schwab_trader.market_data import Candle
from schwab_trader.paper import PaperEngine

START = datetime(2020, 1, 1, tzinfo=UTC)


def _series(symbol: str, closes: list[str], *, offset: int = 0) -> list[Candle]:
    return [
        Candle(
            symbol=symbol,
            date=START + timedelta(days=offset + i),
            open=Decimal(c),
            close=Decimal(c),
        )
        for i, c in enumerate(closes)
    ]


def test_listing_date_is_first_bar() -> None:
    bars = _series("AAA", ["10", "11", "12"], offset=5)
    assert survivorship.listing_date(bars) == START + timedelta(days=5)
    assert survivorship.listing_date([]) is None


def test_eligible_as_of_excludes_not_yet_listed() -> None:
    bars = {
        "OLD": _series("OLD", ["10", "11"], offset=0),  # listed day 0
        "NEW": _series("NEW", ["10", "11"], offset=10),  # listed day 10
    }
    as_of = START + timedelta(days=5)
    assert survivorship.eligible_as_of(bars, as_of) == ["OLD"]  # NEW not listed yet
    later = START + timedelta(days=20)
    assert survivorship.eligible_as_of(bars, later) == ["NEW", "OLD"]  # both listed, sorted


def test_coverage_reports_backfill_footprint() -> None:
    bars = {
        "OLD": _series("OLD", ["10", "11"], offset=0),
        "NEW": _series("NEW", ["10", "11"], offset=10),
    }
    cov = survivorship.coverage_as_of(bars, START + timedelta(days=5), ["OLD", "NEW"])
    assert cov.eligible == 1
    assert cov.total == 2
    assert cov.not_yet_listed == ["NEW"]
    assert cov.coverage_pct == 50.0


def test_walk_forward_point_in_time_drops_unlisted_name(tmp_path) -> None:
    # OLD has full history; NEW only lists inside the final fold's window. With
    # point-in-time on, a fold whose window starts before NEW's listing must not
    # hold NEW (it had not started trading yet).
    old = _series("OLD", [str(100 + i) for i in range(60)], offset=0)
    # NEW lists on day 50 (deep into the series), so early folds exclude it.
    new = _series("NEW", [str(200 + i) for i in range(10)], offset=50)
    bars = {"OLD": old, "NEW": new}

    def factory() -> PaperEngine:
        return PaperEngine(tmp_path / "pit.sqlite3", starting_cash=Decimal("1000"))

    # Capture the universe (fold_bars keys) handed to the fold builder. The fold's
    # window ends at the last bar (day 59) and spans 40 days -> starts ~day 20, before
    # NEW lists (day 50), so point-in-time must hand the builder OLD only.
    def builder(bars_: dict, _bench: list) -> BuyHoldStrategy:
        seen.append(sorted(bars_))
        return BuyHoldStrategy(sorted(bars_))

    seen: list[list[str]] = []
    walk_forward(builder, factory, bars, [], window=40, step=20, folds=1, point_in_time=True)
    assert seen == [["OLD"]]  # NEW filtered out before the strategy is built

    seen.clear()
    walk_forward(builder, factory, bars, [], window=40, step=20, folds=1, point_in_time=False)
    assert seen == [["NEW", "OLD"]]  # without the filter the not-yet-listed name leaks in
