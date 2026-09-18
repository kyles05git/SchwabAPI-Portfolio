"""Tests for the read-only tax breakdown rendered in the order review (offline).

Exercises the private _render_tax_breakdown helper against a seeded local ledger,
capturing the rich console output. Warn-only: it renders information and never
raises or alters an order.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from schwab_trader.cli import _render_tax_breakdown, console
from schwab_trader.config import Settings
from schwab_trader.models import OrderRequest, OrderSide
from schwab_trader.taxlots import TaxLotStore


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        tax_lots_db_path=tmp_path / "taxlots.sqlite3",
        tax_lot_method="FIFO",
        wash_sale_window_days=30,
    )


def _sell(symbol: str = "AAPL", qty: int = 1, price: str = "150.00") -> OrderRequest:
    return OrderRequest(
        side=OrderSide.SELL, symbol=symbol, quantity=qty, limit_price=Decimal(price)
    )


def _render(request: OrderRequest, settings: Settings) -> str:
    with console.capture() as capture:
        _render_tax_breakdown(request, settings)
    return capture.get()


def test_sell_breakdown_shows_gain_split(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = TaxLotStore(settings.tax_lots_db_path)
    # A lot acquired well over a year ago -> long-term gain when sold now.
    store.record_purchase(
        symbol="AAPL",
        quantity=Decimal(10),
        cost_per_share=Decimal(100),
        acquired_at=datetime.now(UTC) - timedelta(days=500),
    )
    output = _render(_sell(qty=1, price="150.00"), settings)
    assert "Estimated tax impact" in output
    assert "realized_gain" in output
    assert "long_term" in output


def test_sell_with_no_lots_notes_empty_ledger(tmp_path: Path) -> None:
    output = _render(_sell(symbol="TSLA"), _settings(tmp_path))
    assert "No tax-lot history on record for TSLA" in output


def test_buy_after_recent_loss_warns_wash_sale(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = TaxLotStore(settings.tax_lots_db_path)
    now = datetime.now(UTC)
    store.record_purchase(
        symbol="AAPL",
        quantity=Decimal(10),
        cost_per_share=Decimal(200),
        acquired_at=now - timedelta(days=20),
    )
    store.apply_sale(  # realized loss 5 days ago
        symbol="AAPL", quantity=Decimal(10), price=Decimal(150), sold_at=now - timedelta(days=5)
    )
    buy = OrderRequest(side=OrderSide.BUY, symbol="AAPL", quantity=1, limit_price=Decimal("155.00"))
    output = _render(buy, settings)
    assert "WASH-SALE WARNING" in output


def test_buy_with_clean_history_is_silent(tmp_path: Path) -> None:
    buy = OrderRequest(side=OrderSide.BUY, symbol="AAPL", quantity=1, limit_price=Decimal("155.00"))
    output = _render(buy, _settings(tmp_path))
    assert "WASH-SALE" not in output
