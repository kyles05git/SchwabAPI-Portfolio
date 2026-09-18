"""Accumulating intraday (minute-bar) store for day-trading research.

The daily :class:`~schwab_trader.pricepanel.PricePanel` can't support intraday
strategies (ORB, SPY noise-area momentum), which need minute bars. Schwab's
intraday history is shallow - about 8-9 months for 5-minute bars and ~6 weeks for
1-minute (measured) - so unlike the daily panel this store is designed to
**accumulate forward**: fetch the recent window regularly and append, and depth
grows over time (the one thing we can't fetch retroactively).

Bars are keyed ``(symbol, minutes, timestamp)`` and upserts are idempotent. This
wraps :func:`market_data.get_intraday_history`; no other network access.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel

from schwab_trader import client as api
from schwab_trader import market_data
from schwab_trader.market_data import Candle


class IntradayCoverage(BaseModel):
    symbol: str
    minutes: int
    bars: int
    first_ts: datetime | None
    last_ts: datetime | None


class IntradayPanel:
    """SQLite-backed store of minute OHLCV bars, one row per (symbol, minutes, ts)."""

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
                CREATE TABLE IF NOT EXISTS intraday_bars (
                    symbol TEXT NOT NULL,
                    minutes INTEGER NOT NULL,
                    ts TEXT NOT NULL,
                    open TEXT,
                    high TEXT,
                    low TEXT,
                    close TEXT NOT NULL,
                    volume INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (symbol, minutes, ts)
                )
                """
            )

    # --- writes --------------------------------------------------------------

    def upsert(self, minutes: int, candles: list[Candle]) -> int:
        """Insert or replace bars at the given ``minutes`` resolution; returns rows written."""
        if not candles:
            return 0
        rows = [
            (
                candle.symbol.strip().upper(),
                minutes,
                candle.date.isoformat(),
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
                "INSERT OR REPLACE INTO intraday_bars "
                "(symbol, minutes, ts, open, high, low, close, volume) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def build(
        self,
        client: api.SchwabClient,
        symbols: list[str],
        *,
        minutes: int = 5,
        days: int = 250,
    ) -> dict[str, int]:
        """Fetch intraday bars for each symbol and append them. Returns per-symbol counts.

        Idempotent: re-running just tops up with any newer bars (and grows history as
        each day passes), so this is the accumulate-forward entry point.
        """
        counts: dict[str, int] = {}
        for raw in symbols:
            symbol = raw.strip().upper()
            if not symbol:
                continue
            candles = market_data.get_intraday_history(client, symbol, minutes=minutes, days=days)
            counts[symbol] = self.upsert(minutes, candles)
        return counts

    # --- reads ---------------------------------------------------------------

    def bars(
        self,
        symbol: str,
        minutes: int,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Candle]:
        """Return oldest-first bars for a symbol/resolution over an optional time range."""
        symbol = symbol.strip().upper()
        query = "SELECT * FROM intraday_bars WHERE symbol = ? AND minutes = ?"
        params: list[object] = [symbol, minutes]
        if start is not None:
            query += " AND ts >= ?"
            params.append(start.isoformat())
        if end is not None:
            query += " AND ts <= ?"
            params.append(end.isoformat())
        query += " ORDER BY ts"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_candle(row) for row in rows]

    def _row_to_candle(self, row: sqlite3.Row) -> Candle:
        def _dec(key: str) -> Decimal | None:
            value = row[key]
            return Decimal(value) if value is not None else None

        return Candle(
            symbol=row["symbol"],
            date=datetime.fromisoformat(row["ts"]),
            open=_dec("open"),
            high=_dec("high"),
            low=_dec("low"),
            close=Decimal(row["close"]),
            volume=int(row["volume"]),
        )

    def coverage(self) -> list[IntradayCoverage]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT symbol, minutes, COUNT(*) AS n, MIN(ts) AS first, MAX(ts) AS last "
                "FROM intraday_bars GROUP BY symbol, minutes ORDER BY symbol, minutes"
            ).fetchall()
        return [
            IntradayCoverage(
                symbol=row["symbol"],
                minutes=row["minutes"],
                bars=row["n"],
                first_ts=datetime.fromisoformat(row["first"]) if row["first"] else None,
                last_ts=datetime.fromisoformat(row["last"]) if row["last"] else None,
            )
            for row in rows
        ]

    def total_bars(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM intraday_bars").fetchone()[0])
