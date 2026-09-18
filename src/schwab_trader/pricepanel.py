"""Persistent long-history daily price panel (a queryable local dataset).

Unlike :mod:`schwab_trader.history_cache` (a per-day JSON cache that keeps one
recent window per symbol to make repeated strategy runs fast), this is a durable
SQLite store of *long* daily history across a whole universe. It is the training
substrate for quant/ML feature building and the price side of a fundamental
backtest: fundamentals (SEC EDGAR) join to *forward price returns* pulled from
here.

Bars are keyed ``(symbol, date)`` and upserts are idempotent, so rebuilding just
appends new sessions. This module wraps :func:`market_data.get_price_history`;
it performs no other network access.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel

from schwab_trader import client as api
from schwab_trader import market_data
from schwab_trader.market_data import Candle

# Trading days per calendar year (approx), for translating --years into bars.
_TRADING_DAYS_PER_YEAR = 252


class SymbolCoverage(BaseModel):
    symbol: str
    bars: int
    first_day: date | None
    last_day: date | None


class PricePanel:
    """SQLite-backed store of daily OHLCV bars for many symbols."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_bars (
                    symbol TEXT NOT NULL,
                    day TEXT NOT NULL,
                    open TEXT,
                    high TEXT,
                    low TEXT,
                    close TEXT NOT NULL,
                    volume INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (symbol, day)
                )
                """
            )

    # --- writes --------------------------------------------------------------

    def upsert(self, candles: list[Candle]) -> int:
        """Insert or replace daily bars; returns the number of rows written."""
        if not candles:
            return 0
        rows = [
            (
                candle.symbol.strip().upper(),
                candle.date.date().isoformat(),
                str(candle.open) if candle.open is not None else None,
                str(candle.high) if candle.high is not None else None,
                str(candle.low) if candle.low is not None else None,
                str(candle.close),
                int(candle.volume),
            )
            for candle in candles
        ]
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO daily_bars "
                "(symbol, day, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def build(
        self,
        client: api.SchwabClient,
        symbols: list[str],
        *,
        years: int = 20,
        on_symbol: Callable[[str, int], None] | None = None,
    ) -> dict[str, int]:
        """Fetch long daily history for each symbol and upsert it. Returns per-symbol counts.

        ``years`` is translated to a bar count (~252/yr); Schwab caps daily history
        at 20 years. ``on_symbol(symbol, count)`` is called after each symbol so the
        CLI can show progress.
        """
        days = max(1, years) * _TRADING_DAYS_PER_YEAR
        counts: dict[str, int] = {}
        for raw in symbols:
            symbol = raw.strip().upper()
            if not symbol:
                continue
            candles = market_data.get_price_history(client, symbol, days=days)
            counts[symbol] = self.upsert(candles)
            if on_symbol is not None:
                on_symbol(symbol, counts[symbol])
        return counts

    # --- reads ---------------------------------------------------------------

    def closes(
        self, symbol: str, *, start: date | None = None, end: date | None = None
    ) -> list[tuple[date, Decimal]]:
        """Return ``(day, close)`` for a symbol over an optional date range, oldest first."""
        symbol = symbol.strip().upper()
        query = "SELECT day, close FROM daily_bars WHERE symbol = ?"
        params: list[str] = [symbol]
        if start is not None:
            query += " AND day >= ?"
            params.append(start.isoformat())
        if end is not None:
            query += " AND day <= ?"
            params.append(end.isoformat())
        query += " ORDER BY day"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [(date.fromisoformat(row["day"]), Decimal(row["close"])) for row in rows]

    def close_on_or_after(self, symbol: str, day: date) -> tuple[date, Decimal] | None:
        """First available ``(day, close)`` on or after ``day`` (for forward-return joins)."""
        symbol = symbol.strip().upper()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT day, close FROM daily_bars WHERE symbol = ? AND day >= ? "
                "ORDER BY day LIMIT 1",
                (symbol, day.isoformat()),
            ).fetchone()
        if row is None:
            return None
        return date.fromisoformat(row["day"]), Decimal(row["close"])

    def close_on_or_before(self, symbol: str, day: date) -> tuple[date, Decimal] | None:
        """Most recent ``(day, close)`` on or before ``day`` (the price *known* as of a date)."""
        symbol = symbol.strip().upper()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT day, close FROM daily_bars WHERE symbol = ? AND day <= ? "
                "ORDER BY day DESC LIMIT 1",
                (symbol, day.isoformat()),
            ).fetchone()
        if row is None:
            return None
        return date.fromisoformat(row["day"]), Decimal(row["close"])

    def symbols(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT DISTINCT symbol FROM daily_bars ORDER BY symbol").fetchall()
        return [row["symbol"] for row in rows]

    def coverage(self) -> list[SymbolCoverage]:
        """Per-symbol bar count and date range - the panel's 'what do I have' view."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT symbol, COUNT(*) AS n, MIN(day) AS first, MAX(day) AS last "
                "FROM daily_bars GROUP BY symbol ORDER BY symbol"
            ).fetchall()
        return [
            SymbolCoverage(
                symbol=row["symbol"],
                bars=row["n"],
                first_day=date.fromisoformat(row["first"]) if row["first"] else None,
                last_day=date.fromisoformat(row["last"]) if row["last"] else None,
            )
            for row in rows
        ]

    def total_bars(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0])
