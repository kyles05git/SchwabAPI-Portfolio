"""The hard experiment boundary: replay research evidence is never official evidence.

Issue #80 allows a historical download only if the result cannot leak into the forward
official world. These tests pin that as structure rather than as a convention someone
has to remember:

* replay records use different types, a different identity prefix, and different tables;
* no foreign key crosses between replay storage and cohort/paper/official storage;
* the official evidence store refuses a replay record and vice versa;
* forward cohort readiness cannot be satisfied by replay tables being present;
* ingesting replay evidence writes zero official observations, fills, orders,
  positions, valuations, or cohort runs;
* the replay modules reach no OAuth, notification, or order code path.

Offline and hermetic: SQLite under ``tmp_path``, synthetic candles, no socket.
"""

from __future__ import annotations

import socket
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import inspect

from schwab_trader import (
    historical_replay,
    historical_replay_acquire,
    market_bar_evidence,
    market_calendar,
)
from schwab_trader.config import Settings
from schwab_trader.market_data import Candle
from schwab_trader.storage import factory as storage_factory
from schwab_trader.storage.database import Database
from schwab_trader.storage.historical_replay import SqlAlchemyHistoricalReplayStore
from schwab_trader.storage.market_data import SqlAlchemyMarketDataEvidenceStore
from schwab_trader.storage.schema import Base

UNIVERSE = historical_replay.universe(["AAPL"], label="boundary")
NORMAL = date(2026, 7, 29)
NOW = datetime(2026, 7, 30, 21, 0, tzinfo=UTC)

REPLAY_TABLES = frozenset(storage_factory.HISTORICAL_REPLAY_TABLES)
#: Tables that carry forward official cohort/paper truth.
OFFICIAL_TABLES = frozenset(
    {
        "official_daily_observations",
        "official_session_leases",
        "cohorts",
        "cohort_members",
        "cohort_runs",
        "cohort_run_members",
        "paper_accounts",
        "paper_positions",
        "paper_orders",
        "paper_fills",
        "paper_unsettled_cash",
        "evaluation_cycles",
        "evaluation_decisions",
        "market_data_daily_evidence",
        "market_data_evidence_constituents",
    }
)


def _synthetic(symbol: str, session: date) -> list[Candle]:
    starts = market_calendar.session_interval_starts_utc(session, minutes=5)
    return [
        Candle(
            symbol=symbol,
            date=moment,
            open=Decimal(100 + index),
            high=Decimal(101 + index),
            low=Decimal(99 + index),
            close=Decimal(100 + index),
            volume=1000 + index,
            source="schwab-regular-session-5m",
        )
        for index, moment in enumerate(starts)
    ]


def _replay_evidence():
    return historical_replay.validate_replay_session(
        UNIVERSE,
        "AAPL",
        NORMAL,
        _synthetic("AAPL", NORMAL),
        retrieved_at=NOW,
        source="schwab-regular-session-5m",
    )


def _official_evidence():
    result = market_bar_evidence.validate_regular_session(
        "AAPL",
        NORMAL,
        _synthetic("AAPL", NORMAL),
        retrieved_at=NOW,
    )
    assert result.evidence is not None
    return result.evidence


# --- separate types and identities -------------------------------------------------


def test_replay_and_official_evidence_are_different_types() -> None:
    replay = _replay_evidence()
    official = _official_evidence()

    assert type(replay) is not type(official)
    assert not isinstance(replay, market_bar_evidence.DerivedDailyEvidence)
    assert not isinstance(official, historical_replay.ReplaySessionEvidence)


def test_the_two_identity_namespaces_cannot_collide() -> None:
    replay = _replay_evidence()
    official = _official_evidence()

    assert replay.replay_id.startswith("historical-replay:")
    assert official.dataset_id.startswith("schwab-intraday-derived-daily:")
    assert replay.replay_id != official.dataset_id
    assert not replay.replay_id.startswith(market_bar_evidence.DERIVED_DAILY_SOURCE)
    assert not official.dataset_id.startswith(historical_replay.REPLAY_ID_PREFIX)


def test_identical_bars_still_digest_differently_in_the_two_worlds() -> None:
    """Even the same candles must not produce an interchangeable content address."""
    assert _replay_evidence().normalized_bar_digest != _official_evidence().constituent_digest


def test_the_payload_schema_names_are_distinct() -> None:
    assert historical_replay.RESEARCH_EVIDENCE_SCHEMA != market_bar_evidence.PAYLOAD_SCHEMA
    assert "research" in historical_replay.RESEARCH_EVIDENCE_SCHEMA
    assert _replay_evidence().schema_name == historical_replay.RESEARCH_EVIDENCE_SCHEMA


# --- separate storage contracts ----------------------------------------------------


def test_no_foreign_key_crosses_the_replay_boundary_in_either_direction() -> None:
    for name in REPLAY_TABLES:
        table = Base.metadata.tables[name]
        referenced = {key.column.table.name for key in table.foreign_keys}
        assert referenced <= REPLAY_TABLES, (
            f"{name} references non-replay storage: {sorted(referenced - REPLAY_TABLES)}"
        )

    for name, table in Base.metadata.tables.items():
        if name in REPLAY_TABLES:
            continue
        referenced = {key.column.table.name for key in table.foreign_keys}
        assert not (referenced & REPLAY_TABLES), (
            f"{name} references replay storage: {sorted(referenced & REPLAY_TABLES)}"
        )


def test_every_official_table_is_actually_declared() -> None:
    """Guards the test above: a renamed model must not silently empty the check."""
    assert OFFICIAL_TABLES <= set(Base.metadata.tables)
    assert REPLAY_TABLES <= set(Base.metadata.tables)
    assert not (OFFICIAL_TABLES & REPLAY_TABLES)


def test_the_official_evidence_store_refuses_a_replay_record(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'official.sqlite3'}", create_schema=True)
    official_store = SqlAlchemyMarketDataEvidenceStore(database)

    with pytest.raises(AttributeError):
        official_store.save(_replay_evidence())  # type: ignore[arg-type]


def test_the_replay_store_refuses_an_official_record(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'replay.sqlite3'}", create_schema=True)
    replay_store = SqlAlchemyHistoricalReplayStore(database)

    with pytest.raises(AttributeError):
        replay_store.ingest(_official_evidence(), now=NOW)  # type: ignore[arg-type]


def test_a_replay_id_is_not_resolvable_as_an_official_dataset(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'shared.sqlite3'}", create_schema=True)
    replay_store = SqlAlchemyHistoricalReplayStore(database)
    official_store = SqlAlchemyMarketDataEvidenceStore(database)

    replay_store.register_universe(UNIVERSE, now=NOW)
    evidence = _replay_evidence()
    replay_store.ingest(evidence, now=NOW)

    # Even sharing one physical database, the official reader cannot see it.
    assert official_store.get(evidence.replay_id) is None
    assert official_store.for_session("AAPL", NORMAL) == ()


# --- forward readiness is untouched ------------------------------------------------


def test_readiness_never_inspects_replay_tables() -> None:
    assert not set(storage_factory.MARKET_DATA_EVIDENCE_TABLES) & REPLAY_TABLES


def test_replay_storage_cannot_satisfy_the_evidence_schema_check(tmp_path: Path) -> None:
    """A database with every replay table and no official evidence table stays not-ready."""
    target = Database(f"sqlite:///{tmp_path / 'replay-only.sqlite3'}")
    try:
        Base.metadata.create_all(
            bind=target.engine,
            tables=[Base.metadata.tables[name] for name in sorted(REPLAY_TABLES)],
        )
        present = set(inspect(target.engine).get_table_names())
        assert present == REPLAY_TABLES

        missing = storage_factory.missing_tables(
            target, storage_factory.MARKET_DATA_EVIDENCE_TABLES
        )
        assert missing == storage_factory.MARKET_DATA_EVIDENCE_TABLES
    finally:
        target.dispose()


def test_ingesting_replay_evidence_writes_no_official_or_paper_row(tmp_path: Path) -> None:
    """The decisive check: one shared database, and only replay tables gain rows."""
    database = Database(f"sqlite:///{tmp_path / 'shared.sqlite3'}", create_schema=True)
    store = SqlAlchemyHistoricalReplayStore(database)

    def counts() -> dict[str, int]:
        from sqlalchemy import func, select

        with database.session() as session:
            return {
                name: session.scalar(select(func.count()).select_from(Base.metadata.tables[name]))
                or 0
                for name in sorted(OFFICIAL_TABLES)
            }

    before = counts()
    historical_replay_acquire.acquire(
        lambda symbol, session: _synthetic(symbol, session),
        store,
        UNIVERSE,
        (NORMAL,),
        clock=lambda: NOW,
    )
    after = counts()

    assert before == after
    assert all(value == 0 for value in after.values())
    # ...while the research side really did record something.
    assert store.status_counts(UNIVERSE.universe_id) == {"complete": 1}


def test_the_local_replay_store_is_a_separate_file_from_official_evidence(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        token_path=tmp_path / "tokens.json",
        state_db_path=tmp_path / "state.sqlite3",
        log_path=tmp_path / "logs" / "app.log",
        market_data_evidence_db_path=tmp_path / "evidence.sqlite3",
        historical_replay_db_path=tmp_path / "replay.sqlite3",
    )
    assert settings.historical_replay_db_path != settings.market_data_evidence_db_path

    storage_factory._local_historical_replay_databases.clear()
    try:
        store = storage_factory.historical_replay_store(settings)
        store.register_universe(UNIVERSE, now=NOW)
        assert (tmp_path / "replay.sqlite3").is_file()
        # Building replay storage must not conjure the official evidence database.
        assert not (tmp_path / "evidence.sqlite3").exists()

        built = set(inspect(store.database.engine).get_table_names())
        assert built == REPLAY_TABLES
    finally:
        for database in storage_factory._local_historical_replay_databases.values():
            database.dispose()
        storage_factory._local_historical_replay_databases.clear()


# --- no unsafe path is reachable ---------------------------------------------------


def test_the_replay_modules_import_no_auth_order_or_notification_module() -> None:
    """A structural check on the dependency graph, not on runtime behavior alone."""
    import ast

    forbidden = {
        "schwab_trader.auth",
        "schwab_trader.token_store",
        "schwab_trader.orders",
        "schwab_trader.approval",
        "schwab_trader.notify",
        "schwab_trader.emailfmt",
        "schwab_trader.paper",
        "schwab_trader.agent",
        "schwab_trader.cohort_lifecycle",
        "schwab_trader.cohort_readiness",
        "schwab_trader.cohort_ops",
        "schwab_trader.evaluation",
        "schwab_trader.sleeve_runs",
        "schwab_trader.storage.paper",
        "schwab_trader.storage.evaluation",
        "schwab_trader.storage.runs",
        "schwab_trader.storage.market_data",
    }
    roots = ("historical_replay", "historical_replay_acquire", "storage/historical_replay")
    source_dir = Path(historical_replay.__file__).parent
    for root in roots:
        tree = ast.parse((source_dir / f"{root}.py").read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
                imported.update(f"{node.module}.{alias.name}" for alias in node.names)
        assert not (imported & forbidden), f"{root} imports {sorted(imported & forbidden)}"


def test_the_whole_offline_workflow_opens_no_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the replay workflow must not open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    database = Database(f"sqlite:///{tmp_path / 'replay.sqlite3'}", create_schema=True)
    store = SqlAlchemyHistoricalReplayStore(database)
    plan = historical_replay_acquire.preflight(
        UNIVERSE, historical_replay.completed_sessions(NOW, count=5)
    )
    assert len(plan.requests) == 5

    report = historical_replay_acquire.acquire(
        lambda symbol, session: _synthetic(symbol, session),
        store,
        UNIVERSE,
        (NORMAL,),
        clock=lambda: NOW,
    )
    assert len(report.complete) == 1


def test_preflight_needs_no_settings_credentials_or_storage() -> None:
    """It is pure calendar arithmetic, so it can never be the thing that leaks."""
    plan = historical_replay_acquire.preflight(
        UNIVERSE, historical_replay.completed_sessions(NOW, count=30)
    )
    payload = historical_replay_acquire.preflight_payload(plan)

    assert payload["session_count"] == 30
    assert payload["mode"] == "preflight"
    text = str(payload).lower()
    for forbidden in ("token", "bearer", "authorization", "password", "postgresql://", "sqlite:"):
        assert forbidden not in text, forbidden


def test_replay_evidence_carries_no_cohort_or_sleeve_reference() -> None:
    evidence = _replay_evidence()
    fields = set(type(evidence).model_fields)

    assert not {"cohort_id", "sleeve_id", "run_id", "session_id"} & fields
    text = historical_replay.evidence_payload(evidence)
    assert "cohort" not in str(text).lower()
    # The two protected forward cohorts can never appear in a replay record.
    for protected in ("paper-first-2026-07-27", "paper-first-2026-07-28"):
        assert protected not in str(text)
