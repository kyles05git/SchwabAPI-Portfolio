"""Tests for margin (leverage) accounting in the paper engine (offline)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from schwab_trader.market_data import Quote
from schwab_trader.models import OrderRequest, OrderSide
from schwab_trader.paper import PaperEngine

MON = datetime(2026, 7, 13, 15, 0, tzinfo=UTC)  # Monday
NEXT_MON = datetime(2026, 7, 20, 15, 0, tzinfo=UTC)  # +7 calendar days


def _quote(symbol: str, price: str) -> Quote:
    p = Decimal(price)
    return Quote(symbol=symbol, bid=p, ask=p, last=p, mark=p, quote_time=MON)


def _buy(symbol: str, qty: int, price: str) -> OrderRequest:
    return OrderRequest(side=OrderSide.BUY, symbol=symbol, quantity=qty, limit_price=Decimal(price))


def _engine(tmp_path, *, leverage: str, **kwargs) -> PaperEngine:
    return PaperEngine(
        tmp_path / "m.sqlite3",
        starting_cash=Decimal("1000.00"),
        leverage=Decimal(leverage),
        **kwargs,
    )


def test_cash_account_buying_power_equals_cash(tmp_path) -> None:
    eng = _engine(tmp_path, leverage="1")
    assert eng.buying_power() == Decimal("1000.00")


def test_margin_doubles_buying_power(tmp_path) -> None:
    eng = _engine(tmp_path, leverage="2")
    assert eng.buying_power() == Decimal("2000.00")  # 2x equity, no positions yet


def test_can_buy_beyond_cash_on_margin(tmp_path) -> None:
    eng = _engine(tmp_path, leverage="2")
    # $1500 of stock on $1000 cash: allowed (within 2x), cash goes to -$500 (a debit).
    order = eng.place_order(_buy("AAA", 15, "100"), _quote("AAA", "100"), now=MON)
    assert order.status == "FILLED"
    assert eng.account().cash == Decimal("-500")
    # Remaining buying power: 2 x equity(1000) - position(1500) = 500.
    assert eng.buying_power() == Decimal("500")


def test_buy_beyond_buying_power_rejected(tmp_path) -> None:
    eng = _engine(tmp_path, leverage="2")
    order = eng.place_order(_buy("AAA", 21, "100"), _quote("AAA", "100"), now=MON)  # $2100 > $2000
    assert order.status == "REJECTED"
    assert "margin buying power" in (order.reason or "")


def test_cash_account_cannot_exceed_cash(tmp_path) -> None:
    eng = _engine(tmp_path, leverage="1")
    order = eng.place_order(_buy("AAA", 11, "100"), _quote("AAA", "100"), now=MON)  # $1100 > $1000
    assert order.status == "REJECTED"
    assert "paper cash" in (order.reason or "")


def test_margin_interest_accrues_on_debit(tmp_path) -> None:
    eng = _engine(tmp_path, leverage="2", margin_rate=Decimal("0.365"))  # 0.1%/day
    eng.place_order(_buy("AAA", 15, "100"), _quote("AAA", "100"), now=MON)  # cash = -500
    eng.accrue(NEXT_MON)  # 7 days later
    # interest = 500 x 0.365 x 7 / 365 = $3.50
    assert eng.account().cash == Decimal("-503.50")


def test_no_interest_when_not_borrowing(tmp_path) -> None:
    eng = _engine(tmp_path, leverage="2", margin_rate=Decimal("0.365"))
    eng.place_order(_buy("AAA", 5, "100"), _quote("AAA", "100"), now=MON)  # cash = +500, no debit
    eng.accrue(NEXT_MON)
    assert eng.account().cash == Decimal("500")


def test_cash_account_never_accrues_interest(tmp_path) -> None:
    eng = _engine(tmp_path, leverage="1", margin_rate=Decimal("0.365"))
    eng.place_order(_buy("AAA", 10, "100"), _quote("AAA", "100"), now=MON)  # cash = 0
    eng.accrue(NEXT_MON)
    assert eng.account().cash == Decimal("0")


def test_margin_call_liquidates_below_maintenance(tmp_path) -> None:
    eng = _engine(tmp_path, leverage="2", maintenance_margin=Decimal("0.30"))
    eng.place_order(_buy("AAA", 15, "100"), _quote("AAA", "100"), now=MON)  # $1500 pos, cash -500
    # Price falls to $40: position now $600, equity = 600 - 500 = 100.
    # 100 / 600 = 16.7% < 30% maintenance -> margin call.
    liquidated = eng.enforce_maintenance({"AAA": Decimal("40")}, now=MON)
    assert len(liquidated) == 1
    assert liquidated[0].reason == "margin-call liquidation"
    assert eng.positions() == []  # forced flat
    assert eng.account().cash == Decimal("100")  # -500 debit + $600 proceeds


def test_no_margin_call_when_healthy(tmp_path) -> None:
    eng = _engine(tmp_path, leverage="2", maintenance_margin=Decimal("0.30"))
    eng.place_order(_buy("AAA", 15, "100"), _quote("AAA", "100"), now=MON)
    # Price holds at $100: equity 1000 / position 1500 = 66% > 30%.
    assert eng.enforce_maintenance({"AAA": Decimal("100")}, now=MON) == []
    assert len(eng.positions()) == 1


def test_cash_account_maintenance_is_noop(tmp_path) -> None:
    eng = _engine(tmp_path, leverage="1")
    eng.place_order(_buy("AAA", 5, "100"), _quote("AAA", "100"), now=MON)
    # Even a crash can't margin-call a cash account (no leverage).
    assert eng.enforce_maintenance({"AAA": Decimal("1")}, now=MON) == []
    assert len(eng.positions()) == 1
