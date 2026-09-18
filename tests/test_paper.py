"""Tests for the paper-trading engine (simulated fills, cash/share accounting)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from schwab_trader.market_data import Quote
from schwab_trader.models import OrderRequest, OrderSide
from schwab_trader.paper import STATUS_FILLED, STATUS_REJECTED, PaperEngine

NOW = datetime(2026, 7, 14, 15, 0, tzinfo=UTC)


def _engine(tmp_path: Path, cash: str = "100.00") -> PaperEngine:
    return PaperEngine(tmp_path / "paper.sqlite3", starting_cash=Decimal(cash))


def _quote(symbol: str, *, bid: str, ask: str) -> Quote:
    return Quote(
        symbol=symbol,
        bid=Decimal(bid),
        ask=Decimal(ask),
        mark=Decimal(ask),
        quote_time=NOW,
    )


def _order(side: OrderSide, symbol: str, qty: int, limit: str) -> OrderRequest:
    return OrderRequest(side=side, symbol=symbol, quantity=qty, limit_price=Decimal(limit))


# --- fills ------------------------------------------------------------------


def test_marketable_buy_fills_at_ask_and_debits_cash(tmp_path: Path) -> None:
    engine = _engine(tmp_path, "100.00")
    order = engine.place_order(
        _order(OrderSide.BUY, "SOFI", 2, "18.10"), _quote("SOFI", bid="18.00", ask="18.05"), now=NOW
    )
    assert order.status == STATUS_FILLED
    assert order.fill_price == Decimal("18.05")  # filled at the ask
    account = engine.account()
    assert account.cash == Decimal("100.00") - Decimal("18.05") * 2
    positions = engine.positions()
    assert positions[0].symbol == "SOFI"
    assert positions[0].quantity == 2
    assert positions[0].avg_cost == Decimal("18.05")


def test_non_marketable_buy_is_rejected(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    order = engine.place_order(
        _order(OrderSide.BUY, "SOFI", 1, "17.00"), _quote("SOFI", bid="18.00", ask="18.05"), now=NOW
    )
    assert order.status == STATUS_REJECTED
    assert "not marketable" in (order.reason or "")
    assert engine.positions() == []


def test_insufficient_cash_is_rejected(tmp_path: Path) -> None:
    engine = _engine(tmp_path, "10.00")
    order = engine.place_order(
        _order(OrderSide.BUY, "SOFI", 1, "18.10"), _quote("SOFI", bid="18.00", ask="18.05"), now=NOW
    )
    assert order.status == STATUS_REJECTED
    assert "insufficient paper cash" in (order.reason or "")


def test_marketable_sell_fills_at_bid_and_realizes_pnl(tmp_path: Path) -> None:
    engine = _engine(tmp_path, "100.00")
    engine.place_order(
        _order(OrderSide.BUY, "SOFI", 2, "18.10"), _quote("SOFI", bid="18.00", ask="18.00"), now=NOW
    )
    # Sell at a higher bid -> realized profit of (19.00 - 18.00) * 2 = 2.00
    sell = engine.place_order(
        _order(OrderSide.SELL, "SOFI", 2, "18.50"),
        _quote("SOFI", bid="19.00", ask="19.05"),
        now=NOW,
    )
    assert sell.status == STATUS_FILLED
    assert sell.fill_price == Decimal("19.00")
    account = engine.account()
    assert account.realized_pnl == Decimal("2.00")
    assert engine.positions() == []


def test_oversized_sell_is_rejected(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine.place_order(
        _order(OrderSide.BUY, "SOFI", 1, "18.10"), _quote("SOFI", bid="18.00", ask="18.00"), now=NOW
    )
    sell = engine.place_order(
        _order(OrderSide.SELL, "SOFI", 5, "17.00"),
        _quote("SOFI", bid="18.00", ask="18.05"),
        now=NOW,
    )
    assert sell.status == STATUS_REJECTED
    assert "insufficient paper shares" in (sell.reason or "")


def test_average_cost_updates_across_buys(tmp_path: Path) -> None:
    engine = _engine(tmp_path, "1000.00")
    engine.place_order(
        _order(OrderSide.BUY, "AAA", 1, "10.00"), _quote("AAA", bid="9", ask="10.00"), now=NOW
    )
    engine.place_order(
        _order(OrderSide.BUY, "AAA", 1, "20.00"), _quote("AAA", bid="19", ask="20.00"), now=NOW
    )
    position = engine.positions()[0]
    assert position.quantity == 2
    assert position.avg_cost == Decimal("15.0000")


# --- persistence + valuation ------------------------------------------------


def test_state_persists_across_instances(tmp_path: Path) -> None:
    db = tmp_path / "paper.sqlite3"
    PaperEngine(db, starting_cash=Decimal("100.00")).place_order(
        _order(OrderSide.BUY, "SOFI", 1, "18.10"), _quote("SOFI", bid="18", ask="18.00"), now=NOW
    )
    reopened = PaperEngine(db, starting_cash=Decimal("100.00"))
    assert reopened.positions()[0].symbol == "SOFI"
    assert reopened.account().cash == Decimal("82.00")


def test_valuation_marks_to_market(tmp_path: Path) -> None:
    engine = _engine(tmp_path, "100.00")
    engine.place_order(
        _order(OrderSide.BUY, "SOFI", 2, "18.00"), _quote("SOFI", bid="18", ask="18.00"), now=NOW
    )
    valuation = engine.value({"SOFI": Decimal("20.00")})
    assert valuation.cash == Decimal("64.00")
    assert valuation.positions_value == Decimal("40.00")
    assert valuation.total_value == Decimal("104.00")
    assert valuation.unrealized_pnl == Decimal("4.00")
    assert valuation.total_return_pct == Decimal("4.00")


def test_reset_restores_starting_cash(tmp_path: Path) -> None:
    engine = _engine(tmp_path, "100.00")
    engine.place_order(
        _order(OrderSide.BUY, "SOFI", 1, "18.10"), _quote("SOFI", bid="18", ask="18.00"), now=NOW
    )
    engine.reset()
    assert engine.account().cash == Decimal("100.00")
    assert engine.positions() == []
    assert engine.recent_orders() == []
