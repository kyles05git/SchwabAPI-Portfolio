"""Quotes and other approved market-data requests.

Phase 6: fetch a single-symbol quote from the Market Data API and expose it as a
typed :class:`Quote` that clearly distinguishes bid/ask/last/mark/previous-close
and records the quote timestamp so callers can reject stale data.

A quote never guarantees execution at that price; risk checks (Phase 8) must
treat it accordingly and refuse to act on missing or stale quotes.
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel

from schwab_trader import client as api
from schwab_trader import market_calendar

QUOTES_PATH = f"{api.MARKETDATA_API}/quotes"
PRICE_HISTORY_PATH = f"{api.MARKETDATA_API}/pricehistory"
INSTRUMENTS_PATH = f"{api.MARKETDATA_API}/instruments"

SCHWAB_PRICE_HISTORY_SOURCE = "schwab-pricehistory"
SCHWAB_DAILY_HISTORY_SOURCE = "schwab-official-daily"
SCHWAB_INTRADAY_HISTORY_SOURCE = "schwab-intraday-minute"
SCHWAB_REGULAR_SESSION_SOURCE = "schwab-regular-session-5m"


class QuoteError(Exception):
    """Raised when a usable quote could not be obtained for a symbol."""


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _ms_to_datetime(value: Any) -> datetime | None:
    """Convert a Schwab epoch-millisecond timestamp to an aware UTC datetime."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _flt(value: Any) -> float | None:
    """Parse a value to float, or None. Treats 0.0 as a real value, not missing."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class Quote(BaseModel):
    """A point-in-time quote. Prices are Decimals; times are aware UTC datetimes."""

    symbol: str
    bid: Decimal | None = None
    ask: Decimal | None = None
    last: Decimal | None = None
    mark: Decimal | None = None
    previous_close: Decimal | None = None
    quote_time: datetime
    trade_time: datetime | None = None
    security_status: str | None = None
    asset_main_type: str | None = None

    def age(self, *, now: datetime | None = None) -> timedelta:
        """How old the quote is relative to ``now`` (default: current UTC time)."""
        return (now or datetime.now(UTC)) - self.quote_time

    def is_stale(self, max_age: timedelta, *, now: datetime | None = None) -> bool:
        """True if the quote is older than ``max_age``."""
        return self.age(now=now) > max_age


class Candle(BaseModel):
    """A single OHLCV bar. ``date`` is an aware UTC datetime.

    ``source`` is explicit because an official daily Schwab candle and a daily candle
    deterministically derived from Schwab intraday constituents are not the same kind
    of evidence.
    """

    symbol: str
    date: datetime
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal
    volume: int = 0
    source: str = SCHWAB_PRICE_HISTORY_SOURCE


def get_price_history(client: api.SchwabClient, symbol: str, *, days: int = 180) -> list[Candle]:
    """Fetch daily candles for a symbol (most recent ``days``), oldest first.

    Uses the Market Data ``pricehistory`` endpoint and returns the last ``days``
    daily bars. The request window scales to ``days`` (a year is only ~252 trading
    sessions, so momentum's ~253-day lookback needs two years). Returns an empty
    list if no data.
    """
    symbol = symbol.strip().upper()
    # ~252 trading days per year; pick the smallest year-period Schwab allows
    # (1, 2, 3, 5, 10, 15, 20) that covers `days`.
    period = next((p for p in (1, 2, 3, 5, 10, 15, 20) if 252 * p >= days), 20)
    params = {
        "symbol": symbol,
        "periodType": "year",
        "period": period,
        "frequencyType": "daily",
        "frequency": 1,
        "needExtendedHoursData": "false",
    }
    data: Any = client.get(PRICE_HISTORY_PATH, params=params)
    candles = _parse_candles(
        symbol,
        data,
        source=SCHWAB_DAILY_HISTORY_SOURCE,
    )
    return candles[-days:] if days > 0 else candles


def _parse_candles(
    symbol: str,
    data: Any,
    *,
    preserve_order: bool = False,
    source: str = SCHWAB_PRICE_HISTORY_SOURCE,
) -> list[Candle]:
    """Parse a ``pricehistory`` response into Candles (empty on no data).

    Most research callers want oldest-first data. Exact-session official evidence must
    instead preserve provider order so duplicate and out-of-order timestamps remain
    detectable; those callers set ``preserve_order=True``.
    """
    raw = data.get("candles") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    candles: list[Candle] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        when = _ms_to_datetime(entry.get("datetime"))
        close = _dec(entry.get("close"))
        if when is None or close is None:
            continue
        candles.append(
            Candle(
                symbol=symbol,
                date=when,
                open=_dec(entry.get("open")),
                high=_dec(entry.get("high")),
                low=_dec(entry.get("low")),
                close=close,
                volume=int(entry.get("volume") or 0),
                source=source,
            )
        )
    if not preserve_order:
        candles.sort(key=lambda candle: candle.date)
    return candles


# Schwab intraday history is shallow: ~8-9 months for 5-min bars, ~6 weeks for 1-min
# (measured). Callers should accumulate forward to build deeper history over time.
_INTRADAY_FREQUENCIES = (1, 5, 10, 15, 30)


def get_intraday_history(
    client: api.SchwabClient,
    symbol: str,
    *,
    minutes: int = 5,
    days: int = 250,
    extended_hours: bool = False,
) -> list[Candle]:
    """Fetch intraday minute bars for a symbol over the last ``days``, oldest first.

    ``minutes`` must be one of 1, 5, 10, 15, 30. ``days`` sets the requested lookback
    via start/end dates; Schwab caps how far back intraday data actually goes (far
    shorter than daily), so the returned range may be shorter than requested. Regular
    trading hours only unless ``extended_hours`` is set.
    """
    symbol = symbol.strip().upper()
    if minutes not in _INTRADAY_FREQUENCIES:
        msg = f"minutes must be one of {_INTRADAY_FREQUENCIES}, got {minutes}."
        raise ValueError(msg)
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - max(1, days) * 86_400_000
    params = {
        "symbol": symbol,
        "periodType": "day",
        "frequencyType": "minute",
        "frequency": minutes,
        "startDate": start_ms,
        "endDate": now_ms,
        "needExtendedHoursData": "true" if extended_hours else "false",
    }
    data: Any = client.get(PRICE_HISTORY_PATH, params=params)
    return _parse_candles(
        symbol,
        data,
        source=SCHWAB_INTRADAY_HISTORY_SOURCE,
    )


def get_regular_session_history(
    client: api.SchwabClient,
    symbol: str,
    session: date,
) -> list[Candle]:
    """Fetch the exact XNYS session as regular-hours five-minute candles.

    The explicit UTC ``startDate`` and ``endDate`` are essential. Schwab's date-free
    ``periodType=day&period=1`` shape can return only the previous session after the
    target close, so it is intentionally not used here. Provider order is preserved
    for the validator rather than sorted into an apparently clean sequence.
    """
    opened, closed = market_calendar.session_bounds_utc(session)
    normalized = symbol.strip().upper()
    params = {
        "symbol": normalized,
        "frequencyType": "minute",
        "frequency": 5,
        "startDate": int(opened.timestamp() * 1000),
        "endDate": int(closed.timestamp() * 1000),
        "needExtendedHoursData": "false",
    }
    data: Any = client.get(PRICE_HISTORY_PATH, params=params)
    return _parse_candles(
        normalized,
        data,
        preserve_order=True,
        source=SCHWAB_REGULAR_SESSION_SOURCE,
    )


class Fundamentals(BaseModel):
    """Current per-symbol fundamentals from Schwab's instruments endpoint.

    Values are point-in-time *today* (Schwab does not expose a history), so these
    are for live screening and context - not for historical backtests. All numeric
    fields are optional; a missing value means the field was absent or unparseable.
    """

    symbol: str
    pe_ratio: float | None = None
    pb_ratio: float | None = None
    peg_ratio: float | None = None
    market_cap: float | None = None
    high_52: float | None = None
    low_52: float | None = None
    dividend_yield: float | None = None
    eps_ttm: float | None = None
    eps_change_pct_ttm: float | None = None
    rev_change_ttm: float | None = None
    gross_margin_ttm: float | None = None
    operating_margin_ttm: float | None = None
    net_profit_margin_ttm: float | None = None
    return_on_equity: float | None = None
    return_on_assets: float | None = None
    total_debt_to_equity: float | None = None
    current_ratio: float | None = None
    avg_3month_volume: float | None = None
    beta: float | None = None

    @property
    def earnings_yield(self) -> float | None:
        """Inverse P/E (a value signal): higher is cheaper."""
        if self.pe_ratio is not None and self.pe_ratio > 0:
            return 1.0 / self.pe_ratio
        return None


def get_fundamentals(client: api.SchwabClient, symbol: str) -> Fundamentals | None:
    """Fetch current fundamentals for a symbol, or None if unavailable.

    Uses the Market Data ``instruments`` endpoint with the ``fundamental``
    projection. Returns None (rather than raising) when no fundamental block is
    present, so callers can skip a symbol gracefully.
    """
    symbol = symbol.strip().upper()
    data: Any = client.get(INSTRUMENTS_PATH, params={"symbol": symbol, "projection": "fundamental"})
    entries = data.get("instruments") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        return None
    entry = entries[0]
    fundamental = entry.get("fundamental") if isinstance(entry, dict) else None
    if not isinstance(fundamental, dict):
        return None
    return Fundamentals(
        symbol=entry.get("symbol", symbol),
        pe_ratio=_flt(fundamental.get("peRatio")),
        pb_ratio=_flt(fundamental.get("pbRatio")),
        peg_ratio=_flt(fundamental.get("pegRatio")),
        market_cap=_flt(fundamental.get("marketCap")),
        high_52=_flt(fundamental.get("high52")),
        low_52=_flt(fundamental.get("low52")),
        dividend_yield=_flt(fundamental.get("dividendYield")),
        eps_ttm=_flt(fundamental.get("epsTTM")),
        eps_change_pct_ttm=_flt(fundamental.get("epsChangePercentTTM")),
        rev_change_ttm=_flt(fundamental.get("revChangeTTM")),
        gross_margin_ttm=_flt(fundamental.get("grossMarginTTM")),
        operating_margin_ttm=_flt(fundamental.get("operatingMarginTTM")),
        net_profit_margin_ttm=_flt(fundamental.get("netProfitMarginTTM")),
        return_on_equity=_flt(fundamental.get("returnOnEquity")),
        return_on_assets=_flt(fundamental.get("returnOnAssets")),
        total_debt_to_equity=_flt(fundamental.get("totalDebtToEquity")),
        current_ratio=_flt(fundamental.get("currentRatio")),
        avg_3month_volume=_flt(fundamental.get("avg3MonthVolume")),
        beta=_flt(fundamental.get("beta")),
    )


def _parse_quote_entry(symbol: str, entry: Any) -> Quote | None:
    """Parse one symbol's entry from a ``/quotes`` response into a Quote, or None.

    Returns None when the entry has no usable quote block or no quote timestamp.
    """
    quote = entry.get("quote") if isinstance(entry, dict) else None
    if not isinstance(quote, dict):
        return None
    quote_time = _ms_to_datetime(quote.get("quoteTime"))
    if quote_time is None:
        return None
    return Quote(
        symbol=entry.get("symbol", symbol) if isinstance(entry, dict) else symbol,
        bid=_dec(quote.get("bidPrice")),
        ask=_dec(quote.get("askPrice")),
        last=_dec(quote.get("lastPrice")),
        mark=_dec(quote.get("mark")),
        previous_close=_dec(quote.get("closePrice")),
        quote_time=quote_time,
        trade_time=_ms_to_datetime(quote.get("tradeTime")),
        security_status=quote.get("securityStatus"),
        asset_main_type=entry.get("assetMainType") if isinstance(entry, dict) else None,
    )


def get_quote(client: api.SchwabClient, symbol: str) -> Quote:
    """Fetch a single-symbol quote.

    Raises:
        QuoteError: if no quote (or no quote timestamp) is returned for ``symbol``.
    """
    symbol = symbol.strip().upper()
    data: Any = client.get(QUOTES_PATH, params={"symbols": symbol, "fields": "quote"})
    entry = data.get(symbol) if isinstance(data, dict) else None
    quote = _parse_quote_entry(symbol, entry)
    if quote is None:
        raise QuoteError(f"No usable quote was returned for {symbol}.")
    return quote


def get_quotes(client: api.SchwabClient, symbols: list[str]) -> dict[str, Quote]:
    """Fetch many symbols in a single request; return only the ones that parsed.

    This is the batched form of :func:`get_quote` - all symbols come back in one
    HTTP call (comma-separated ``symbols``), which is how the watch daemon fetches
    a whole universe cheaply. Symbols without a usable quote are simply omitted.
    """
    wanted = [s.strip().upper() for s in symbols if s.strip()]
    if not wanted:
        return {}
    data: Any = client.get(QUOTES_PATH, params={"symbols": ",".join(wanted), "fields": "quote"})
    quotes: dict[str, Quote] = {}
    if isinstance(data, dict):
        for symbol in wanted:
            quote = _parse_quote_entry(symbol, data.get(symbol))
            if quote is not None:
                quotes[symbol] = quote
    return quotes
