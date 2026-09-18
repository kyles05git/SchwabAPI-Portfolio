"""Tests for the intraday CSV importer (offline)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from schwab_trader.intraday_import import parse_intraday_csv
from schwab_trader.intraday_panel import IntradayPanel


def _write(tmp_path, name: str, text: str):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_eastern_naive_timestamps_normalize_to_utc(tmp_path) -> None:
    # FirstRate-style: naive ET timestamps, standard OHLCV columns.
    csv = _write(
        tmp_path,
        "spy.csv",
        "timestamp,open,high,low,close,volume\n"
        "2026-07-16 09:30:00,600,601,599,600.5,100000\n"
        "2026-07-16 09:31:00,600.5,602,600,601.5,120000\n",
    )
    candles, summary = parse_intraday_csv(csv, symbol="SPY", tz="eastern")
    assert summary.parsed == 2
    # July -> EDT (UTC-4): 09:30 ET == 13:30 UTC.
    assert candles[0].date == datetime(2026, 7, 16, 13, 30, tzinfo=UTC)
    assert candles[0].symbol == "SPY"
    assert candles[0].close == Decimal("600.5")
    assert candles[1].volume == 120000


def test_zoned_timestamps_are_honored(tmp_path) -> None:
    csv = _write(
        tmp_path,
        "z.csv",
        "datetime,close\n2026-01-05T14:30:00+00:00,500\n",  # winter, explicit UTC
    )
    candles, _ = parse_intraday_csv(csv, symbol="AAA", tz="eastern")
    assert candles[0].date == datetime(2026, 1, 5, 14, 30, tzinfo=UTC)


def test_separate_date_time_columns_and_symbol_column(tmp_path) -> None:
    csv = _write(
        tmp_path,
        "multi.csv",
        "symbol,date,time,close,volume\n"
        "aapl,2026-07-16,10:00:00,230,5000\n"
        "msft,2026-07-16,10:00:00,500,4000\n",
    )
    candles, summary = parse_intraday_csv(csv, symbol="IGNORED", tz="eastern")
    assert summary.symbols == {"AAPL", "MSFT"}
    assert {c.symbol for c in candles} == {"AAPL", "MSFT"}


def test_rth_filter_drops_premarket(tmp_path) -> None:
    csv = _write(
        tmp_path,
        "pre.csv",
        "timestamp,close\n"
        "2026-07-16 08:00:00,599\n"  # pre-market ET -> filtered
        "2026-07-16 10:00:00,601\n",  # RTH -> kept
    )
    _, summary = parse_intraday_csv(csv, symbol="SPY", tz="eastern", rth_only=True)
    assert summary.parsed == 1
    assert summary.filtered == 1
    all_hours, _ = parse_intraday_csv(csv, symbol="SPY", tz="eastern", rth_only=False)
    assert len(all_hours) == 2  # all-hours keeps the pre-market bar too


def test_headerless_firstrate_style_with_columns(tmp_path) -> None:
    # FirstRate-style: no header row, datetime + OHLCV, Eastern local time.
    csv = _write(
        tmp_path,
        "SPY_1min.txt",
        "2026-07-16 09:30:00,600.0,601.0,599.0,600.5,100000\n"
        "2026-07-16 09:31:00,600.5,602.0,600.0,601.5,120000\n",
    )
    candles, summary = parse_intraday_csv(
        csv,
        symbol="SPY",
        tz="eastern",
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    assert summary.parsed == 2
    assert candles[0].date == datetime(2026, 7, 16, 13, 30, tzinfo=UTC)  # 9:30 ET -> 13:30 UTC
    assert candles[0].open == Decimal("600.0")
    assert candles[1].volume == 120000


def test_import_dedups_against_existing_panel_bars(tmp_path) -> None:
    csv = _write(
        tmp_path,
        "spy.csv",
        "timestamp,open,high,low,close,volume\n2026-07-16 09:30:00,600,601,599,600.5,100000\n",
    )
    candles, _ = parse_intraday_csv(csv, symbol="SPY", tz="eastern")
    panel = IntradayPanel(tmp_path / "intraday.sqlite3")
    panel.upsert(1, candles)
    panel.upsert(1, candles)  # same UTC timestamp -> idempotent
    assert panel.total_bars() == 1
