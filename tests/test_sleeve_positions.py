"""Tests for the sleeve positions stats summary rendering (offline)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from schwab_trader import cli, paper, sleeves
from schwab_trader.market_data import Quote
from schwab_trader.models import OrderRequest, OrderSide


def _quote(symbol: str, price: str) -> Quote:
    p = Decimal(price)
    return Quote(
        symbol=symbol, bid=p, ask=p, last=p, mark=p, previous_close=p, quote_time=datetime.now(UTC)
    )


def _buy(symbol: str, qty: int, price: str) -> OrderRequest:
    return OrderRequest(side=OrderSide.BUY, symbol=symbol, quantity=qty, limit_price=Decimal(price))


def _cfg(name: str, *, leverage: str = "1", t1: bool = False) -> sleeves.SleeveConfig:
    return sleeves.SleeveConfig(
        name=name,
        strategy="intraday",
        universe=[],
        starting_cash=Decimal("5000"),
        max_positions=5,
        max_position_fraction=Decimal("0.10"),
        created_at=datetime.now(UTC),
        settlement_t1=t1,
        leverage=Decimal(leverage),
    )


def _render(cfg, engine, marks, *, detailed: bool) -> str:
    with cli.console.capture() as capture:
        cli._render_sleeve_positions(
            cfg, engine, engine.positions(), marks, engine.value(marks), detailed=detailed
        )
    return capture.get()


def test_summary_reports_cost_basis_and_value(tmp_path) -> None:
    eng = paper.PaperEngine(tmp_path / "p.sqlite3", starting_cash=Decimal("5000"))
    eng.place_order(_buy("AAPL", 10, "150"), _quote("AAPL", "150"))  # cost basis 1500
    eng.place_order(_buy("MSFT", 5, "400"), _quote("MSFT", "400"))  # cost basis 2000
    out = _render(
        _cfg("demo"), eng, {"AAPL": Decimal("158"), "MSFT": Decimal("390")}, detailed=True
    )

    assert "Total cost basis" in out
    assert "$3,500.00" in out  # 1500 + 2000
    assert "Total position value" in out
    assert "$3,530.00" in out  # 1580 + 1950
    assert "Unrealized P&L" in out  # +80 - 50 = +30


def test_margin_summary_shows_debit_and_buying_power(tmp_path) -> None:
    eng = paper.PaperEngine(
        tmp_path / "p.sqlite3", starting_cash=Decimal("5000"), leverage=Decimal("2")
    )
    eng.place_order(_buy("AAPL", 40, "150"), _quote("AAPL", "150"))  # $6000 on $5000 -> debit 1000
    out = _render(_cfg("demo2x", leverage="2"), eng, {"AAPL": Decimal("150")}, detailed=False)

    assert "Margin debit" in out
    assert "Buying power" in out
    assert "Leverage" in out


def test_flat_sleeve_still_shows_summary(tmp_path) -> None:
    eng = paper.PaperEngine(tmp_path / "p.sqlite3", starting_cash=Decimal("5000"))
    out = _render(_cfg("flat"), eng, {}, detailed=True)
    assert "no positions" in out
    assert "Equity (total value)" in out
    assert "$5,000.00" in out
