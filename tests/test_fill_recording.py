"""Tests for recording tax lots from confirmed fills (offline).

Covers the average-execution-price parser and the _record_fill_tax_lot helper that
runs after an order is confirmed FILLED. No network: OrderDetail is constructed
directly and the ledger is a temp SQLite file.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from schwab_trader.cli import _record_fill_tax_lot
from schwab_trader.config import Settings
from schwab_trader.models import OrderDetail, OrderRequest, OrderSide, OrderStatus
from schwab_trader.orders import parse_order_detail
from schwab_trader.taxlots import TaxLotStore


def _noop_audit(_event: str, _detail: str | None = None) -> None:
    return None


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        state_db_path=tmp_path / "state.sqlite3",
        tax_lots_db_path=tmp_path / "taxlots.sqlite3",
        agent_activity_db_path=tmp_path / "activity.sqlite3",
        tax_lot_method="FIFO",
    )


# --- average fill price parsing ---------------------------------------------


def test_parse_weighted_average_fill_price() -> None:
    data = {
        "orderId": 1,
        "status": "FILLED",
        "quantity": 10,
        "filledQuantity": 10,
        "price": 150.0,
        "orderActivityCollection": [
            {"executionLegs": [{"price": 149.0, "quantity": 4}]},
            {"executionLegs": [{"price": 151.0, "quantity": 6}]},
        ],
    }
    detail = parse_order_detail(data, fallback_id="1")
    # (149*4 + 151*6) / 10 = 150.2
    assert detail.average_fill_price == Decimal("150.2")


def test_parse_no_activity_leaves_price_none() -> None:
    detail = parse_order_detail(
        {"orderId": 1, "status": "WORKING", "price": 100.0}, fallback_id="1"
    )
    assert detail.average_fill_price is None


# --- recording from a fill ---------------------------------------------------


def _buy(symbol: str = "AAPL", qty: int = 5, limit: str = "150.00") -> OrderRequest:
    return OrderRequest(side=OrderSide.BUY, symbol=symbol, quantity=qty, limit_price=Decimal(limit))


def _filled(qty: int, *, avg: str | None, limit: str = "150.00") -> OrderDetail:
    return OrderDetail(
        order_id="1",
        status=OrderStatus.FILLED,
        symbol="AAPL",
        side="BUY",
        quantity=Decimal(qty),
        filled_quantity=Decimal(qty),
        limit_price=Decimal(limit),
        average_fill_price=Decimal(avg) if avg is not None else None,
    )


def test_buy_fill_opens_lot_at_average_price(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _record_fill_tax_lot(settings, _buy(qty=5), _filled(5, avg="149.50"), _noop_audit)
    lots = TaxLotStore(settings.tax_lots_db_path).open_lots("AAPL")
    assert len(lots) == 1
    assert lots[0].quantity == Decimal(5)
    assert lots[0].cost_per_share == Decimal("149.50")


def test_buy_fill_without_execution_price_fails_closed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    audited: list[tuple[str, str | None]] = []
    _record_fill_tax_lot(
        settings,
        _buy(qty=2, limit="150.00"),
        _filled(2, avg=None),
        lambda event, detail=None: audited.append((event, detail)),
    )
    lots = TaxLotStore(settings.tax_lots_db_path).open_lots("AAPL")
    assert lots == []
    assert audited[0][0] == "fill_price_unavailable"


def test_sell_fill_relieves_existing_lots(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = TaxLotStore(settings.tax_lots_db_path)
    store.record_purchase(
        symbol="AAPL",
        quantity=Decimal(10),
        cost_per_share=Decimal(100),
        acquired_at=datetime.now(UTC),
    )
    sell = OrderRequest(
        side=OrderSide.SELL, symbol="AAPL", quantity=4, limit_price=Decimal("160.00")
    )
    detail = OrderDetail(
        order_id="2",
        status=OrderStatus.FILLED,
        side="SELL",
        quantity=Decimal(4),
        filled_quantity=Decimal(4),
        limit_price=Decimal("160.00"),
        average_fill_price=Decimal("161.00"),
    )
    _record_fill_tax_lot(settings, sell, detail, _noop_audit)
    lots = store.open_lots("AAPL")
    assert lots[0].quantity == Decimal(6)  # 10 - 4 relieved


def test_non_filled_status_records_nothing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    working = OrderDetail(order_id="3", status=OrderStatus.WORKING, side="BUY")
    _record_fill_tax_lot(settings, _buy(), working, _noop_audit)
    assert TaxLotStore(settings.tax_lots_db_path).open_lots("AAPL") == []
