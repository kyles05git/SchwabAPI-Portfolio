"""Tests for the tax-lot ledger and realized-gain calculator (offline, pure)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from schwab_trader.taxlots import LotMethod, TaxLotStore, is_long_term


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


def _store(tmp_path: Path) -> TaxLotStore:
    return TaxLotStore(tmp_path / "taxlots.sqlite3")


def test_is_long_term_boundary() -> None:
    acquired = _dt(2024, 7, 20)
    # Exactly one year later is still short-term; one year + a day is long-term.
    assert is_long_term(acquired, _dt(2025, 7, 20)) is False
    assert is_long_term(acquired, _dt(2025, 7, 21)) is True
    assert is_long_term(acquired, _dt(2025, 1, 20)) is False


def test_leap_day_acquisition_long_term() -> None:
    acquired = _dt(2024, 2, 29)
    # Feb 28 the following year is not yet a full year; Mar 1 is long-term.
    assert is_long_term(acquired, _dt(2025, 2, 28)) is False
    assert is_long_term(acquired, _dt(2025, 3, 1)) is True


def test_fifo_relieves_oldest_first(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_purchase(
        symbol="AAPL",
        quantity=Decimal(10),
        cost_per_share=Decimal(100),
        acquired_at=_dt(2024, 1, 10),
    )
    store.record_purchase(
        symbol="AAPL",
        quantity=Decimal(10),
        cost_per_share=Decimal(120),
        acquired_at=_dt(2024, 6, 10),
    )
    # Sell 5 at 150 the next day: FIFO relieves the $100 lot (short-term).
    result = store.compute_sale(
        symbol="AAPL", quantity=Decimal(5), price=Decimal(150), sold_at=_dt(2024, 6, 11)
    )
    assert len(result.consumptions) == 1
    assert result.consumptions[0].cost_per_share == Decimal(100)
    assert result.proceeds == Decimal(750)
    assert result.cost_basis == Decimal(500)
    assert result.gain == Decimal(250)
    assert result.short_term_gain == Decimal(250)
    assert result.long_term_gain == Decimal(0)
    assert result.fully_covered is True


def test_hifo_minimizes_gain(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_purchase(
        symbol="AAPL",
        quantity=Decimal(10),
        cost_per_share=Decimal(100),
        acquired_at=_dt(2024, 1, 10),
    )
    store.record_purchase(
        symbol="AAPL",
        quantity=Decimal(10),
        cost_per_share=Decimal(120),
        acquired_at=_dt(2024, 6, 10),
    )
    # HIFO relieves the $120 lot first -> smaller gain than FIFO's $100 lot.
    hifo = store.compute_sale(
        symbol="AAPL",
        quantity=Decimal(5),
        price=Decimal(150),
        sold_at=_dt(2024, 6, 11),
        method=LotMethod.HIFO,
    )
    assert hifo.consumptions[0].cost_per_share == Decimal(120)
    assert hifo.gain == Decimal(150)


def test_sale_spans_lots_and_splits_short_long_term(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Old lot (long-term when sold in 2026) and a recent lot (short-term).
    store.record_purchase(
        symbol="MSFT",
        quantity=Decimal(10),
        cost_per_share=Decimal(200),
        acquired_at=_dt(2024, 1, 5),
    )
    store.record_purchase(
        symbol="MSFT",
        quantity=Decimal(10),
        cost_per_share=Decimal(300),
        acquired_at=_dt(2026, 1, 5),
    )
    # Sell 15 at 400 on 2026-02-01: 10 from the old lot (LT), 5 from the new lot (ST).
    result = store.compute_sale(
        symbol="MSFT", quantity=Decimal(15), price=Decimal(400), sold_at=_dt(2026, 2, 1)
    )
    assert result.covered_quantity == Decimal(15)
    assert result.fully_covered is True
    # Long-term piece: 10 * (400-200) = 2000; short-term: 5 * (400-300) = 500.
    assert result.long_term_gain == Decimal(2000)
    assert result.short_term_gain == Decimal(500)
    assert result.gain == Decimal(2500)


def test_partial_coverage_flags_uncovered(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_purchase(
        symbol="NVDA", quantity=Decimal(3), cost_per_share=Decimal(100), acquired_at=_dt(2025, 1, 1)
    )
    result = store.compute_sale(
        symbol="NVDA", quantity=Decimal(5), price=Decimal(150), sold_at=_dt(2025, 6, 1)
    )
    assert result.covered_quantity == Decimal(3)
    assert result.fully_covered is False
    assert result.gain == Decimal(150)  # 3 * (150 - 100)


def test_compute_sale_does_not_mutate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_purchase(
        symbol="AAPL",
        quantity=Decimal(10),
        cost_per_share=Decimal(100),
        acquired_at=_dt(2025, 1, 1),
    )
    store.compute_sale(
        symbol="AAPL", quantity=Decimal(5), price=Decimal(150), sold_at=_dt(2025, 2, 1)
    )
    # Open lot is untouched by a pure computation.
    lots = store.open_lots("AAPL")
    assert len(lots) == 1
    assert lots[0].quantity == Decimal(10)


def test_apply_sale_relieves_lots_and_persists(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_purchase(
        symbol="AAPL",
        quantity=Decimal(10),
        cost_per_share=Decimal(100),
        acquired_at=_dt(2025, 1, 1),
    )
    store.apply_sale(
        symbol="AAPL", quantity=Decimal(4), price=Decimal(150), sold_at=_dt(2025, 2, 1)
    )
    lots = store.open_lots("AAPL")
    assert len(lots) == 1
    assert lots[0].quantity == Decimal(6)  # 10 - 4 relieved

    # A second sale relieves the remaining shares and closes the lot.
    store.apply_sale(
        symbol="AAPL", quantity=Decimal(6), price=Decimal(160), sold_at=_dt(2025, 3, 1)
    )
    assert store.open_lots("AAPL") == []


def test_method_parse_defaults_to_fifo() -> None:
    assert LotMethod.parse("hifo") is LotMethod.HIFO
    assert LotMethod.parse("nonsense") is LotMethod.FIFO


def test_wash_sale_on_buy_flags_recent_loss(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Buy then sell at a loss on 2025-06-01.
    store.record_purchase(
        symbol="AAPL",
        quantity=Decimal(10),
        cost_per_share=Decimal(200),
        acquired_at=_dt(2025, 1, 1),
    )
    store.apply_sale(
        symbol="AAPL", quantity=Decimal(10), price=Decimal(150), sold_at=_dt(2025, 6, 1)
    )
    # Buying back 20 days later triggers a wash-sale warning; 40 days later does not.
    warn = store.wash_sale_on_buy(symbol="AAPL", buy_at=_dt(2025, 6, 21), window_days=30)
    assert warn is not None
    assert warn.side == "BUY"
    assert _dt(2025, 6, 1) in warn.related_dates
    assert store.wash_sale_on_buy(symbol="AAPL", buy_at=_dt(2025, 7, 11), window_days=30) is None


def test_wash_sale_on_buy_ignores_gain_sales(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_purchase(
        symbol="AAPL",
        quantity=Decimal(10),
        cost_per_share=Decimal(100),
        acquired_at=_dt(2025, 1, 1),
    )
    store.apply_sale(  # sold at a gain, not a loss
        symbol="AAPL", quantity=Decimal(10), price=Decimal(150), sold_at=_dt(2025, 6, 1)
    )
    assert store.wash_sale_on_buy(symbol="AAPL", buy_at=_dt(2025, 6, 10), window_days=30) is None


def test_wash_sale_on_sell_flags_recent_purchase(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # A replacement lot bought 10 days before a loss sale triggers the warning.
    store.record_purchase(
        symbol="MSFT",
        quantity=Decimal(5),
        cost_per_share=Decimal(300),
        acquired_at=_dt(2025, 5, 20),
    )
    warn = store.wash_sale_on_sell(
        symbol="MSFT", sold_at=_dt(2025, 5, 30), is_loss=True, window_days=30
    )
    assert warn is not None
    assert warn.side == "SELL"
    assert _dt(2025, 5, 20) in warn.related_dates


def test_wash_sale_on_sell_only_for_losses(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_purchase(
        symbol="MSFT",
        quantity=Decimal(5),
        cost_per_share=Decimal(300),
        acquired_at=_dt(2025, 5, 20),
    )
    # A gain sale is never a wash sale, even with a recent purchase.
    assert (
        store.wash_sale_on_sell(
            symbol="MSFT", sold_at=_dt(2025, 5, 30), is_loss=False, window_days=30
        )
        is None
    )


def test_wash_sale_window_zero_disables(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_purchase(
        symbol="AAPL",
        quantity=Decimal(10),
        cost_per_share=Decimal(200),
        acquired_at=_dt(2025, 1, 1),
    )
    store.apply_sale(
        symbol="AAPL", quantity=Decimal(10), price=Decimal(150), sold_at=_dt(2025, 6, 1)
    )
    assert store.wash_sale_on_buy(symbol="AAPL", buy_at=_dt(2025, 6, 10), window_days=0) is None
