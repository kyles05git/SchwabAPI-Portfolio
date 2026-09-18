"""Import intraday bars from vendor CSV/flat files into the intraday panel.

Schwab's intraday history is shallow (~8 months at 5-min); to backtest over years,
buy or download minute bars from a vendor (FirstRate Data, Databento, Polygon,
EODHD, ...) and import them here. The importer tolerates common column layouts and -
critically - **normalizes timestamps to UTC** so imported data aligns with the
UTC bars fetched from Schwab (they dedupe by timestamp and can be mixed).

Vendor intraday timestamps are usually US/Eastern *local* time with no zone; the
default ``tz="eastern"`` converts them to UTC using the DST-aware market calendar.
Timestamps that already carry a zone are honored. Reads the stdlib ``csv`` only.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from schwab_trader import market_calendar
from schwab_trader.market_data import Candle

_TS_ALIASES = ("timestamp", "datetime", "date_time", "date-time")
_DATE_ALIASES = ("date",)
_TIME_ALIASES = ("time",)
_OPEN = ("open", "o")
_HIGH = ("high", "h")
_LOW = ("low", "l")
_CLOSE = ("close", "c", "adj close", "adjclose", "adj_close")
_VOLUME = ("volume", "vol", "v")
_SYMBOL = ("symbol", "ticker")

_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%Y%m%d %H:%M:%S",
)


class ImportError_(Exception):
    """Raised when a CSV cannot be interpreted as intraday bars."""


def _naive_eastern_to_utc(naive: datetime) -> datetime:
    """Interpret a naive datetime as US/Eastern and return an aware UTC datetime."""
    offset = market_calendar.eastern_offset_hours(naive.date())  # -4 (EDT) or -5 (EST)
    return (naive - timedelta(hours=offset)).replace(tzinfo=UTC)


def _parse_timestamp(text: str, tz: str) -> datetime | None:
    text = text.strip()
    if not text:
        return None
    if text.isdigit():  # epoch seconds or milliseconds
        value = int(text)
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, UTC)
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in _TIME_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC)
    return _naive_eastern_to_utc(parsed) if tz == "eastern" else parsed.replace(tzinfo=UTC)


def _dec(text: str | None) -> Decimal | None:
    if text is None or not text.strip():
        return None
    try:
        return Decimal(text.strip())
    except InvalidOperation:
        return None


def _rth(utc_ts: datetime) -> bool:
    """True if the UTC timestamp falls in the regular US session (weekday 9:30-16:00 ET)."""
    offset = market_calendar.eastern_offset_hours(utc_ts.date())
    et = (utc_ts + timedelta(hours=offset)).replace(tzinfo=None)
    if et.weekday() >= 5:
        return False
    return market_calendar.MARKET_OPEN <= et.time() <= market_calendar.MARKET_CLOSE


class ImportSummary:
    def __init__(self) -> None:
        self.parsed = 0
        self.skipped = 0
        self.filtered = 0  # dropped by the RTH filter
        self.symbols: set[str] = set()


def parse_intraday_csv(
    path: Path,
    *,
    symbol: str,
    tz: str = "eastern",
    rth_only: bool = True,
    columns: list[str] | None = None,
) -> tuple[list[Candle], ImportSummary]:
    """Parse a vendor intraday CSV into UTC-normalized Candles plus an import summary.

    ``symbol`` is the default when the file has no symbol column. ``tz`` interprets
    naive timestamps (``eastern`` or ``utc``); zoned timestamps are always honored.
    ``columns`` names the fields positionally for **headerless** files (e.g. FirstRate
    Data: ``["timestamp", "open", "high", "low", "close", "volume"]``); omit it for a
    file that has a header row.
    """
    summary = ImportSummary()
    with path.open(newline="", encoding="utf-8-sig") as handle:
        if columns is not None:
            names = [c.strip().lower() for c in columns]
            records: list[dict[str, str]] = [
                dict(zip(names, row, strict=False)) for row in csv.reader(handle) if row
            ]
        else:
            dict_reader = csv.DictReader(handle)
            if dict_reader.fieldnames is None:
                raise ImportError_(
                    f"{path} has no header row (use --columns for headerless files)."
                )
            names = [name.strip().lower() for name in dict_reader.fieldnames]
            records = [
                {k.strip().lower(): (v or "") for k, v in row.items() if k is not None}
                for row in dict_reader
            ]
        available = set(names)

        def col(aliases: tuple[str, ...]) -> str | None:
            return next((a for a in aliases if a in available), None)

        ts_col = col(_TS_ALIASES)
        date_col = col(_DATE_ALIASES)
        time_col = col(_TIME_ALIASES)
        close_col = col(_CLOSE)
        if close_col is None:
            raise ImportError_(f"{path}: no close column found (columns: {sorted(available)}).")
        if ts_col is None and date_col is None:
            raise ImportError_(f"{path}: no timestamp/date column found.")
        open_col, high_col, low_col = col(_OPEN), col(_HIGH), col(_LOW)
        vol_col, sym_col = col(_VOLUME), col(_SYMBOL)

        candles: list[Candle] = []
        for row in records:
            raw_ts = row.get(ts_col) if ts_col else None
            if raw_ts is None and date_col is not None:
                raw_ts = row.get(date_col)
                if time_col is not None and row.get(time_col):
                    raw_ts = f"{raw_ts} {row[time_col]}"
            utc_ts = _parse_timestamp(raw_ts or "", tz)
            close = _dec(row.get(close_col))
            if utc_ts is None or close is None:
                summary.skipped += 1
                continue
            if rth_only and not _rth(utc_ts):
                summary.filtered += 1
                continue
            row_symbol = (row.get(sym_col) or symbol) if sym_col else symbol
            row_symbol = row_symbol.strip().upper() or symbol.upper()
            summary.symbols.add(row_symbol)
            candles.append(
                Candle(
                    symbol=row_symbol,
                    date=utc_ts,
                    open=_dec(row.get(open_col)) if open_col else None,
                    high=_dec(row.get(high_col)) if high_col else None,
                    low=_dec(row.get(low_col)) if low_col else None,
                    close=close,
                    volume=int(_dec(row.get(vol_col)) or 0) if vol_col else 0,
                )
            )
            summary.parsed += 1
    candles.sort(key=lambda candle: (candle.symbol, candle.date))
    return candles, summary
