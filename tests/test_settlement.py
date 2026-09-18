"""Tests for T+1 settled-cash accounting in the paper engine (offline)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from schwab_trader.market_data import Quote
from schwab_trader.models import OrderRequest, OrderSide
from schwab_trader.paper import PaperEngine, _next_business_day

MON = datetime(2026, 7, 13, 15, 0, tzinfo=UTC)  # Monday
TUE = datetime(2026, 7, 14, 15, 0, tzinfo=UTC)  # Tuesday
FRI = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)  # Friday


def _quote(symbol: str, price: str) -> Quote:
    p = Decimal(price)
    return Quote(symbol=symbol, bid=p, ask=p, last=p, mark=p, quote_time=MON)


def _buy(symbol: str, qty: int, price: str) -> OrderRequest:
    return OrderRequest(side=OrderSide.BUY, symbol=symbol, quantity=qty, limit_price=Decimal(price))


def _sell(symbol: str, qty: int, price: str) -> OrderRequest:
    return OrderRequest(
        side=OrderSide.SELL, symbol=symbol, quantity=qty, limit_price=Decimal(price)
    )


def _engine(tmp_path, *, settle_t1: bool) -> PaperEngine:
    return PaperEngine(
        tmp_path / "p.sqlite3", starting_cash=Decimal("1000.00"), settle_t1=settle_t1
    )


def test_next_business_day_skips_weekend() -> None:
    assert _next_business_day(FRI.date()).weekday() == 0  # Fri -> Mon
    assert _next_business_day(MON.date()).weekday() == 1  # Mon -> Tue


def test_sale_proceeds_are_unsettled_same_day(tmp_path) -> None:
    eng = _engine(tmp_path, settle_t1=True)
    eng.place_order(_buy("AAA", 5, "100"), _quote("AAA", "100"), now=MON)  # spend $500, settled=500
    eng.place_order(_sell("AAA", 5, "100"), _quote("AAA", "100"), now=MON)  # proceeds unsettled

    acct = eng.account()
    assert acct.cash == Decimal("500")  # only the leftover settled cash
    assert acct.unsettled_cash == Decimal("500")  # sale proceeds pending T+1
    assert acct.total_cash == Decimal("1000")


def test_cannot_rebuy_with_unsettled_proceeds(tmp_path) -> None:
    eng = _engine(tmp_path, settle_t1=True)
    eng.place_order(_buy("AAA", 10, "100"), _quote("AAA", "100"), now=MON)  # settled -> 0
    eng.place_order(_sell("AAA", 10, "100"), _quote("AAA", "100"), now=MON)  # $1000 unsettled

    # Same day: try to rebuy with the (unsettled) proceeds -> rejected.
    order = eng.place_order(_buy("BBB", 5, "100"), _quote("BBB", "100"), now=MON)
    assert order.status == "REJECTED"
    assert "settled" in (order.reason or "")


def test_proceeds_settle_next_business_day(tmp_path) -> None:
    eng = _engine(tmp_path, settle_t1=True)
    eng.place_order(_buy("AAA", 10, "100"), _quote("AAA", "100"), now=MON)
    eng.place_order(_sell("AAA", 10, "100"), _quote("AAA", "100"), now=MON)  # settles Tuesday

    # Tuesday: the proceeds have settled -> the rebuy now succeeds.
    order = eng.place_order(_buy("BBB", 5, "100"), _quote("BBB", "100"), now=TUE)
    assert order.status == "FILLED"
    acct = eng.account()
    assert acct.unsettled_cash == Decimal("0")


def test_without_t1_proceeds_are_instantly_reusable(tmp_path) -> None:
    eng = _engine(tmp_path, settle_t1=False)
    eng.place_order(_buy("AAA", 10, "100"), _quote("AAA", "100"), now=MON)
    eng.place_order(_sell("AAA", 10, "100"), _quote("AAA", "100"), now=MON)
    # Same day rebuy works with instant settlement (original behavior).
    order = eng.place_order(_buy("BBB", 5, "100"), _quote("BBB", "100"), now=MON)
    assert order.status == "FILLED"
    assert eng.account().unsettled_cash == Decimal("0")


def test_total_value_includes_unsettled(tmp_path) -> None:
    eng = _engine(tmp_path, settle_t1=True)
    eng.place_order(_buy("AAA", 5, "100"), _quote("AAA", "100"), now=MON)
    eng.place_order(_sell("AAA", 5, "100"), _quote("AAA", "100"), now=MON)
    val = eng.value({})
    assert val.total_value == Decimal("1000")  # 500 settled + 500 unsettled
    assert val.unsettled_cash == Decimal("500")
