"""SQLite store for SEC EDGAR facts, with point-in-time queries.

Persists the flattened :class:`~schwab_trader.sec_edgar.Fact` rows and answers the
question a fundamental backtest actually needs: *what value was known as of date D?*
Because every fact carries its ``filed`` date, :meth:`SecStore.point_in_time` only
considers facts filed on or before the as-of date and returns the most recent
reported period among them - the latest filing wins, so a later restatement is
reflected once (and only once) it was actually public.

Concept names are stored raw (us-gaap taxonomy). Normalizing the many aliases for
"revenue"/"net income"/etc. into canonical fields is a later phase; for now
:meth:`concepts` lets callers discover what a company actually reports.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

from schwab_trader.sec_edgar import Fact


class SecStore:
    """SQLite persistence + point-in-time lookup for EDGAR facts."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        # Reuse one connection: opening a fresh one per lookup dominated backtests that
        # query fundamentals for a whole universe every rebalance day. Reads only here.
        if self._conn is None:
            self._conn = sqlite3.connect(self.path)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sec_facts (
                    ticker TEXT NOT NULL,
                    cik INTEGER NOT NULL,
                    concept TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    period_start TEXT,
                    period_end TEXT NOT NULL,
                    value TEXT NOT NULL,
                    fiscal_year INTEGER,
                    fiscal_period TEXT,
                    form TEXT,
                    filed TEXT NOT NULL,
                    accession TEXT NOT NULL DEFAULT '',
                    frame TEXT,
                    PRIMARY KEY (ticker, concept, unit, period_end, filed, accession)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_facts_lookup ON sec_facts (ticker, concept, filed)"
            )

    # --- writes --------------------------------------------------------------

    def upsert(self, facts: list[Fact]) -> int:
        """Insert facts, ignoring exact duplicates. Returns rows attempted."""
        if not facts:
            return 0
        rows = [
            (
                fact.ticker,
                fact.cik,
                fact.concept,
                fact.unit,
                fact.period_start.isoformat() if fact.period_start else None,
                fact.period_end.isoformat(),
                str(fact.value),
                fact.fiscal_year,
                fact.fiscal_period,
                fact.form,
                fact.filed.isoformat(),
                fact.accession,
                fact.frame,
            )
            for fact in facts
        ]
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO sec_facts (ticker, cik, concept, unit, period_start, "
                "period_end, value, fiscal_year, fiscal_period, form, filed, accession, frame) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    # --- reads ---------------------------------------------------------------

    def _row_to_fact(self, row: sqlite3.Row) -> Fact:
        return Fact(
            ticker=row["ticker"],
            cik=row["cik"],
            concept=row["concept"],
            unit=row["unit"],
            period_start=date.fromisoformat(row["period_start"]) if row["period_start"] else None,
            period_end=date.fromisoformat(row["period_end"]),
            value=Decimal(row["value"]),
            fiscal_year=row["fiscal_year"],
            fiscal_period=row["fiscal_period"],
            form=row["form"],
            filed=date.fromisoformat(row["filed"]),
            accession=row["accession"],
            frame=row["frame"],
        )

    def point_in_time(
        self,
        ticker: str,
        concept: str,
        as_of: date,
        *,
        unit: str | None = None,
        form: str | None = None,
    ) -> Fact | None:
        """The value for ``concept`` known as of ``as_of`` (no look-ahead).

        Considers only facts filed on or before ``as_of``, then takes the most recent
        reported period and, within it, the latest filing. Optionally restrict to a
        ``unit`` (e.g. ``USD``) or ``form`` (e.g. ``10-K`` for annual figures).
        """
        query = "SELECT * FROM sec_facts WHERE ticker = ? AND concept = ? AND filed <= ?"
        params: list[object] = [ticker.strip().upper(), concept, as_of.isoformat()]
        if unit is not None:
            query += " AND unit = ?"
            params.append(unit)
        if form is not None:
            query += " AND form = ?"
            params.append(form)
        query += " ORDER BY period_end DESC, filed DESC LIMIT 1"
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        return self._row_to_fact(row) if row is not None else None

    def facts_as_of(
        self, ticker: str, concept: str, as_of: date, *, unit: str = "USD"
    ) -> list[Fact]:
        """All facts for a ticker/concept filed on or before ``as_of`` (newest period first).

        Unlike :meth:`point_in_time` (one value), this returns the full point-in-time set
        so callers can assemble a trailing-twelve-month figure from quarterly periods.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sec_facts WHERE ticker = ? AND concept = ? AND filed <= ? "
                "AND unit = ? ORDER BY period_end DESC, filed DESC",
                (ticker.strip().upper(), concept, as_of.isoformat(), unit),
            ).fetchall()
        return [self._row_to_fact(row) for row in rows]

    def facts(self, ticker: str, concept: str, *, limit: int = 20) -> list[Fact]:
        """Most recent stored facts for a ticker/concept (newest period first)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sec_facts WHERE ticker = ? AND concept = ? "
                "ORDER BY period_end DESC, filed DESC LIMIT ?",
                (ticker.strip().upper(), concept, limit),
            ).fetchall()
        return [self._row_to_fact(row) for row in rows]

    def concepts(self, ticker: str) -> list[tuple[str, int]]:
        """Distinct concepts stored for a ticker with row counts (for discovery)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT concept, COUNT(*) AS n FROM sec_facts WHERE ticker = ? "
                "GROUP BY concept ORDER BY concept",
                (ticker.strip().upper(),),
            ).fetchall()
        return [(row["concept"], row["n"]) for row in rows]

    def tickers(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT DISTINCT ticker FROM sec_facts ORDER BY ticker").fetchall()
        return [row["ticker"] for row in rows]

    def total_facts(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM sec_facts").fetchone()[0])
