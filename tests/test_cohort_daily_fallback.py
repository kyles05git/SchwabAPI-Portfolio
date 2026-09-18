"""Offline cohort snapshot integration for mixed official and derived daily bars."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from schwab_trader import cli, market_calendar, market_data, scheduling, strategy_registry
from schwab_trader.config import Settings
from schwab_trader.market_data import Candle, Quote
from schwab_trader.sleeves import SleeveStore
from schwab_trader.storage import factory as storage_factory

SESSION = date(2026, 7, 20)
RETRIEVED_AT = datetime(2026, 7, 20, 20, 1, tzinfo=UTC)


def _daily(symbol: str, session: date) -> Candle:
    return Candle(
        symbol=symbol,
        date=datetime.combine(session, datetime.min.time(), UTC),
        open=Decimal("99"),
        high=Decimal("102"),
        low=Decimal("98"),
        close=Decimal("100"),
        volume=10_000,
        source=market_data.SCHWAB_DAILY_HISTORY_SOURCE,
    )


def _intraday(symbol: str, session: date) -> list[Candle]:
    bars: list[Candle] = []
    for index, stamp in enumerate(market_calendar.session_interval_starts_utc(session)):
        value = Decimal(100 + index)
        bars.append(
            Candle(
                symbol=symbol,
                date=stamp,
                open=value,
                high=value + 2,
                low=value - 1,
                close=value + 1,
                volume=index + 1,
                source=market_data.SCHWAB_REGULAR_SESSION_SOURCE,
            )
        )
    return bars


def _quote(symbol: str) -> Quote:
    return Quote(
        symbol=symbol,
        bid=Decimal("100"),
        ask=Decimal("100"),
        last=Decimal("100"),
        mark=Decimal("100"),
        previous_close=Decimal("99"),
        quote_time=RETRIEVED_AT,
    )


class _Cache:
    def __init__(self) -> None:
        prior = market_calendar.previous_trading_day(SESSION)
        self.history = {
            "AAPL": [_daily("AAPL", SESSION)],
            "SPY": [_daily("SPY", SESSION)],
            "XLB": [_daily("XLB", prior)],
        }

    def get(
        self,
        client: Any,
        symbol: str,
        *,
        days: int = 180,
        settled_through: date | None = None,
        now: datetime | None = None,
    ) -> list[Candle]:
        del client, days, settled_through, now
        return list(self.history[symbol])


def _member(store: SleeveStore, name: str, symbol: str):
    definition = strategy_registry.make_definition(
        "momentum",
        universe_definition=[symbol],
    )
    return store.create(
        name,
        strategy="momentum",
        universe=[symbol],
        starting_cash=Decimal("1000"),
        max_positions=1,
        max_position_fraction=Decimal("1"),
        definition=definition,
        cohort_id="mixed-bars",
    )


def test_snapshot_mixes_official_and_derived_symbols_without_unneeded_intraday_calls(
    tmp_path,
    monkeypatch,
) -> None:
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    members = (
        _member(sleeve_store, "official", "AAPL"),
        _member(sleeve_store, "derived", "XLB"),
    )
    settings = Settings(
        _env_file=None,
        sleeves_dir=tmp_path / "sleeves",
        market_data_evidence_db_path=tmp_path / "evidence.sqlite3",
    )
    intraday_calls: list[tuple[str, date]] = []
    monkeypatch.setattr(
        cli.market_data,
        "get_quotes",
        lambda client, symbols: {symbol: _quote(symbol) for symbol in symbols},
    )

    def exact_session(client: Any, symbol: str, session: date) -> list[Candle]:
        del client
        intraday_calls.append((symbol, session))
        return _intraday(symbol, session)

    monkeypatch.setattr(market_data, "get_regular_session_history", exact_session)
    evidence_store = storage_factory.market_data_evidence_store(settings)
    snapshot = cli._capture_official_sleeve_snapshot(
        settings,
        members,
        scheduling.session_for_date(SESSION),
        None,
        client=object(),  # type: ignore[arg-type]
        cache=_Cache(),  # type: ignore[arg-type]
        evidence_store=evidence_store,
        spec=None,
        on_usage=lambda usage: None,
    )

    assert all(item.ready for item in snapshot.readiness_by_member.values())
    assert intraday_calls == [("XLB", SESSION)]
    assert snapshot.resources.history is not None
    assert snapshot.resources.history["AAPL"][-1].source == "schwab-official-daily"
    assert snapshot.resources.history["XLB"][-1].source == "schwab-intraday-derived-daily"
    dataset_id = snapshot.data_snapshot_ids["daily_bars_evidence:XLB"]
    assert evidence_store.reproduce(dataset_id) is not None
    assert "daily_bars_evidence:AAPL" not in snapshot.data_snapshot_ids
