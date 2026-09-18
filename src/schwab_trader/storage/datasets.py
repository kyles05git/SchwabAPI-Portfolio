"""Shared daily/intraday price panels and SEC EDGAR repository."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from schwab_trader import client as api
from schwab_trader import market_data
from schwab_trader.intraday_panel import IntradayCoverage
from schwab_trader.market_data import Candle
from schwab_trader.pricepanel import SymbolCoverage
from schwab_trader.sec_edgar import Fact
from schwab_trader.storage.database import Database
from schwab_trader.storage.schema import DailyPriceBar, IntradayPriceBar, SecFact

_TRADING_DAYS_PER_YEAR = 252


class SqlAlchemyPricePanel:
    def __init__(self, database: Database) -> None:
        self.database = database

    def upsert(self, candles: list[Candle]) -> int:
        with self.database.session() as session:
            for candle in candles:
                symbol = candle.symbol.strip().upper()
                day = candle.date.date()
                row = session.get(DailyPriceBar, (symbol, day))
                values = {
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                    "volume": int(candle.volume),
                }
                if row is None:
                    session.add(
                        DailyPriceBar(
                            symbol=symbol,
                            day=day,
                            source_path=None,
                            **values,
                        )
                    )
                else:
                    for key, value in values.items():
                        setattr(row, key, value)
        return len(candles)

    def build(
        self,
        client: api.SchwabClient,
        symbols: list[str],
        *,
        years: int = 20,
        on_symbol: Callable[[str, int], None] | None = None,
    ) -> dict[str, int]:
        counts: dict[str, int] = {}
        days = max(1, years) * _TRADING_DAYS_PER_YEAR
        for raw in symbols:
            symbol = raw.strip().upper()
            if not symbol:
                continue
            counts[symbol] = self.upsert(
                market_data.get_price_history(client, symbol, days=days)
            )
            if on_symbol is not None:
                on_symbol(symbol, counts[symbol])
        return counts

    def closes(
        self,
        symbol: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[tuple[date, Decimal]]:
        statement = select(DailyPriceBar).where(
            DailyPriceBar.symbol == symbol.strip().upper()
        )
        if start is not None:
            statement = statement.where(DailyPriceBar.day >= start)
        if end is not None:
            statement = statement.where(DailyPriceBar.day <= end)
        with self.database.session() as session:
            rows = list(session.scalars(statement.order_by(DailyPriceBar.day)))
        return [(row.day, Decimal(row.close)) for row in rows]

    def close_on_or_after(self, symbol: str, day: date) -> tuple[date, Decimal] | None:
        with self.database.session() as session:
            row = session.scalar(
                select(DailyPriceBar)
                .where(
                    DailyPriceBar.symbol == symbol.strip().upper(),
                    DailyPriceBar.day >= day,
                )
                .order_by(DailyPriceBar.day)
                .limit(1)
            )
        return None if row is None else (row.day, Decimal(row.close))

    def close_on_or_before(self, symbol: str, day: date) -> tuple[date, Decimal] | None:
        with self.database.session() as session:
            row = session.scalar(
                select(DailyPriceBar)
                .where(
                    DailyPriceBar.symbol == symbol.strip().upper(),
                    DailyPriceBar.day <= day,
                )
                .order_by(DailyPriceBar.day.desc())
                .limit(1)
            )
        return None if row is None else (row.day, Decimal(row.close))

    def symbols(self) -> list[str]:
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(DailyPriceBar.symbol).distinct().order_by(DailyPriceBar.symbol)
                )
            )

    def coverage(self) -> list[SymbolCoverage]:
        with self.database.session() as session:
            rows = session.execute(
                select(
                    DailyPriceBar.symbol,
                    func.count(),
                    func.min(DailyPriceBar.day),
                    func.max(DailyPriceBar.day),
                )
                .group_by(DailyPriceBar.symbol)
                .order_by(DailyPriceBar.symbol)
            )
            return [
                SymbolCoverage(
                    symbol=symbol,
                    bars=int(count),
                    first_day=first_day,
                    last_day=last_day,
                )
                for symbol, count, first_day, last_day in rows
            ]

    def total_bars(self) -> int:
        with self.database.session() as session:
            return int(session.scalar(select(func.count()).select_from(DailyPriceBar)) or 0)


class SqlAlchemyIntradayPanel:
    def __init__(self, database: Database) -> None:
        self.database = database

    def upsert(self, minutes: int, candles: list[Candle]) -> int:
        with self.database.session() as session:
            for candle in candles:
                symbol = candle.symbol.strip().upper()
                source_ts = candle.date.isoformat()
                key = (symbol, minutes, source_ts)
                row = session.get(IntradayPriceBar, key)
                observed = (
                    candle.date
                    if candle.date.tzinfo is not None
                    and candle.date.utcoffset() is not None
                    else None
                )
                values = {
                    "observed_at": observed,
                    "source_ts": source_ts,
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                    "volume": int(candle.volume),
                }
                if row is None:
                    session.add(
                        IntradayPriceBar(
                            symbol=symbol,
                            minutes=minutes,
                            timestamp_key=source_ts,
                            source_path=None,
                            **values,
                        )
                    )
                else:
                    for field, value in values.items():
                        setattr(row, field, value)
        return len(candles)

    def build(
        self,
        client: api.SchwabClient,
        symbols: list[str],
        *,
        minutes: int = 5,
        days: int = 250,
    ) -> dict[str, int]:
        counts: dict[str, int] = {}
        for raw in symbols:
            symbol = raw.strip().upper()
            if not symbol:
                continue
            counts[symbol] = self.upsert(
                minutes,
                market_data.get_intraday_history(
                    client, symbol, minutes=minutes, days=days
                ),
            )
        return counts

    def bars(
        self,
        symbol: str,
        minutes: int,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Candle]:
        statement = select(IntradayPriceBar).where(
            IntradayPriceBar.symbol == symbol.strip().upper(),
            IntradayPriceBar.minutes == minutes,
        )
        if start is not None:
            statement = statement.where(
                IntradayPriceBar.timestamp_key >= start.isoformat()
            )
        if end is not None:
            statement = statement.where(
                IntradayPriceBar.timestamp_key <= end.isoformat()
            )
        with self.database.session() as session:
            rows = list(
                session.scalars(statement.order_by(IntradayPriceBar.timestamp_key))
            )
        return [self._domain_candle(row) for row in rows]

    @staticmethod
    def _domain_candle(row: IntradayPriceBar) -> Candle:
        return Candle(
            symbol=row.symbol,
            date=row.observed_at or datetime.fromisoformat(row.source_ts),
            open=Decimal(row.open) if row.open is not None else None,
            high=Decimal(row.high) if row.high is not None else None,
            low=Decimal(row.low) if row.low is not None else None,
            close=Decimal(row.close),
            volume=row.volume,
        )

    def coverage(self) -> list[IntradayCoverage]:
        with self.database.session() as session:
            rows = session.execute(
                select(
                    IntradayPriceBar.symbol,
                    IntradayPriceBar.minutes,
                    func.count(),
                    func.min(IntradayPriceBar.source_ts),
                    func.max(IntradayPriceBar.source_ts),
                )
                .group_by(IntradayPriceBar.symbol, IntradayPriceBar.minutes)
                .order_by(IntradayPriceBar.symbol, IntradayPriceBar.minutes)
            )
            return [
                IntradayCoverage(
                    symbol=symbol,
                    minutes=minutes,
                    bars=int(count),
                    first_ts=datetime.fromisoformat(first_ts) if first_ts else None,
                    last_ts=datetime.fromisoformat(last_ts) if last_ts else None,
                )
                for symbol, minutes, count, first_ts, last_ts in rows
            ]

    def total_bars(self) -> int:
        with self.database.session() as session:
            return int(
                session.scalar(select(func.count()).select_from(IntradayPriceBar)) or 0
            )


class SqlAlchemySecStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def upsert(self, facts: list[Fact]) -> int:
        if not facts:
            return 0
        rows = [
            {
                "ticker": fact.ticker,
                "cik": fact.cik,
                "concept": fact.concept,
                "unit": fact.unit,
                "period_start": fact.period_start,
                "period_end": fact.period_end,
                "value": fact.value,
                "fiscal_year": fact.fiscal_year,
                "fiscal_period": fact.fiscal_period,
                "form": fact.form,
                "filed": fact.filed,
                "accession": fact.accession,
                "frame": fact.frame,
                "source_path": None,
            }
            for fact in facts
        ]
        key = ["ticker", "concept", "unit", "period_end", "filed", "accession"]
        with self.database.session() as session:
            if self.database.dialect == "postgresql":
                session.execute(
                    pg_insert(SecFact)
                    .values(rows)
                    .on_conflict_do_nothing(index_elements=key)
                )
            else:
                session.execute(
                    sqlite_insert(SecFact)
                    .values(rows)
                    .on_conflict_do_nothing(index_elements=key)
                )
        return len(rows)

    @staticmethod
    def _domain_fact(row: SecFact) -> Fact:
        return Fact(
            ticker=row.ticker,
            cik=row.cik,
            concept=row.concept,
            unit=row.unit,
            period_start=row.period_start,
            period_end=row.period_end,
            value=Decimal(row.value),
            fiscal_year=row.fiscal_year,
            fiscal_period=row.fiscal_period,
            form=row.form,
            filed=row.filed,
            accession=row.accession,
            frame=row.frame,
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
        statement = select(SecFact).where(
            SecFact.ticker == ticker.strip().upper(),
            SecFact.concept == concept,
            SecFact.filed <= as_of,
        )
        if unit is not None:
            statement = statement.where(SecFact.unit == unit)
        if form is not None:
            statement = statement.where(SecFact.form == form)
        with self.database.session() as session:
            row = session.scalar(
                statement.order_by(SecFact.period_end.desc(), SecFact.filed.desc()).limit(1)
            )
        return None if row is None else self._domain_fact(row)

    def facts_as_of(
        self,
        ticker: str,
        concept: str,
        as_of: date,
        *,
        unit: str = "USD",
    ) -> list[Fact]:
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(SecFact)
                    .where(
                        SecFact.ticker == ticker.strip().upper(),
                        SecFact.concept == concept,
                        SecFact.filed <= as_of,
                        SecFact.unit == unit,
                    )
                    .order_by(SecFact.period_end.desc(), SecFact.filed.desc())
                )
            )
        return [self._domain_fact(row) for row in rows]

    def facts(self, ticker: str, concept: str, *, limit: int = 20) -> list[Fact]:
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(SecFact)
                    .where(
                        SecFact.ticker == ticker.strip().upper(),
                        SecFact.concept == concept,
                    )
                    .order_by(SecFact.period_end.desc(), SecFact.filed.desc())
                    .limit(limit)
                )
            )
        return [self._domain_fact(row) for row in rows]

    def concepts(self, ticker: str) -> list[tuple[str, int]]:
        with self.database.session() as session:
            rows = session.execute(
                select(SecFact.concept, func.count())
                .where(SecFact.ticker == ticker.strip().upper())
                .group_by(SecFact.concept)
                .order_by(SecFact.concept)
            )
            return [(concept, int(count)) for concept, count in rows]

    def tickers(self) -> list[str]:
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(SecFact.ticker).distinct().order_by(SecFact.ticker)
                )
            )

    def total_facts(self) -> int:
        with self.database.session() as session:
            return int(session.scalar(select(func.count()).select_from(SecFact)) or 0)
