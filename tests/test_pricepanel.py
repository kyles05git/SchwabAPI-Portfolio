"""Tests for the persistent price panel store (offline)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from schwab_trader import market_data, pricepanel
from schwab_trader.market_data import Candle


def _candle(symbol: str, day: str, close: str, *, volume: int = 100) -> Candle:
    return Candle(
        symbol=symbol,
        date=datetime.fromisoformat(f"{day}T00:00:00+00:00"),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=volume,
    )


def test_upsert_is_idempotent(tmp_path) -> None:
    panel = pricepanel.PricePanel(tmp_path / "prices.sqlite3")
    bars = [_candle("AAPL", "2026-01-02", "150"), _candle("AAPL", "2026-01-05", "152")]
    panel.upsert(bars)
    panel.upsert(bars)  # same rows again -> no duplicates
    assert panel.total_bars() == 2
    # Re-upsert with a changed close replaces, not duplicates.
    panel.upsert([_candle("AAPL", "2026-01-05", "160")])
    closes = panel.closes("AAPL")
    assert closes == [(date(2026, 1, 2), Decimal("150")), (date(2026, 1, 5), Decimal("160"))]


def test_closes_respects_date_range(tmp_path) -> None:
    panel = pricepanel.PricePanel(tmp_path / "prices.sqlite3")
    panel.upsert(
        [
            _candle("MSFT", "2026-01-02", "400"),
            _candle("MSFT", "2026-02-02", "410"),
            _candle("MSFT", "2026-03-02", "420"),
        ]
    )
    ranged = panel.closes("MSFT", start=date(2026, 2, 1), end=date(2026, 2, 28))
    assert ranged == [(date(2026, 2, 2), Decimal("410"))]


def test_close_on_or_after_for_forward_returns(tmp_path) -> None:
    panel = pricepanel.PricePanel(tmp_path / "prices.sqlite3")
    panel.upsert([_candle("NVDA", "2026-01-05", "120"), _candle("NVDA", "2026-01-12", "130")])
    # A weekend/holiday date resolves forward to the next available session.
    assert panel.close_on_or_after("NVDA", date(2026, 1, 6)) == (date(2026, 1, 12), Decimal("130"))
    assert panel.close_on_or_after("NVDA", date(2026, 2, 1)) is None


def test_close_on_or_before_for_point_in_time_price(tmp_path) -> None:
    panel = pricepanel.PricePanel(tmp_path / "prices.sqlite3")
    panel.upsert([_candle("NVDA", "2026-01-05", "120"), _candle("NVDA", "2026-01-12", "130")])
    # A later date resolves back to the most recent available session.
    assert panel.close_on_or_before("NVDA", date(2026, 1, 20)) == (
        date(2026, 1, 12),
        Decimal("130"),
    )
    assert panel.close_on_or_before("NVDA", date(2026, 1, 5)) == (date(2026, 1, 5), Decimal("120"))
    assert panel.close_on_or_before("NVDA", date(2026, 1, 1)) is None


def test_coverage_and_symbols(tmp_path) -> None:
    panel = pricepanel.PricePanel(tmp_path / "prices.sqlite3")
    panel.upsert([_candle("AAA", "2026-01-02", "10"), _candle("AAA", "2026-01-03", "11")])
    panel.upsert([_candle("BBB", "2026-01-02", "20")])
    assert panel.symbols() == ["AAA", "BBB"]
    cov = {c.symbol: c for c in panel.coverage()}
    assert cov["AAA"].bars == 2
    assert cov["AAA"].first_day == date(2026, 1, 2)
    assert cov["AAA"].last_day == date(2026, 1, 3)


def test_build_fetches_and_stores(tmp_path, monkeypatch) -> None:
    fake = {
        "AAA": [_candle("AAA", "2026-01-02", "10"), _candle("AAA", "2026-01-03", "11")],
        "BBB": [_candle("BBB", "2026-01-02", "20")],
    }

    def fake_history(_client, symbol, *, days):
        return fake.get(symbol.upper(), [])

    monkeypatch.setattr(market_data, "get_price_history", fake_history)
    panel = pricepanel.PricePanel(tmp_path / "prices.sqlite3")
    counts = panel.build(object(), ["aaa", "bbb", "ccc"], years=5)  # type: ignore[arg-type]
    assert counts == {"AAA": 2, "BBB": 1, "CCC": 0}
    assert panel.total_bars() == 3
