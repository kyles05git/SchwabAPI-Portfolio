"""Revision ``20260730_0004`` builds exactly the replay boundary it claims to.

``test_migration_model_parity`` already proves the whole chain matches ``Base.metadata``
with no drift. These tests add what parity cannot see: the constraint *names* (which
``compare_metadata`` does not diff), the correction/idempotency constraints that make
the boundary meaningful, downgrade behavior, and re-runnability.

Offline and hermetic. Every database is a disposable SQLite file under ``tmp_path``.
No DDL is ever executed against Neon or any user database; the opt-in PostgreSQL run in
``test_migration_model_parity`` is the only place a server dialect is exercised, and it
is skipped unless ``SCHWAB_TEST_DATABASE_URL`` is set.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from schwab_trader.storage.schema import Base

REPO_ROOT = Path(__file__).resolve().parent.parent

REPLAY_TABLES = (
    "historical_replay_universes",
    "historical_replay_sessions",
    "historical_replay_bars",
    "historical_replay_observations",
)

#: PostgreSQL truncates identifiers past this, which would silently make a migrated
#: database and a ``create_all`` one disagree.
POSTGRES_IDENTIFIER_LIMIT = 63


def _config(url: str) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture
def sqlite_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'replay-migrated.sqlite3'}"


def _upgrade(url: str, revision: str = "head") -> None:
    command.upgrade(_config(url), revision)


def test_the_revision_creates_all_four_replay_tables(sqlite_url: str) -> None:
    _upgrade(sqlite_url)

    engine = create_engine(sqlite_url, future=True)
    try:
        names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    for table in REPLAY_TABLES:
        assert table in names


def test_the_replay_tables_are_absent_before_this_revision(sqlite_url: str) -> None:
    _upgrade(sqlite_url, "20260728_0003")

    engine = create_engine(sqlite_url, future=True)
    try:
        names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    for table in REPLAY_TABLES:
        assert table not in names
    # The prior revision's official evidence tables are untouched by this work.
    assert "market_data_daily_evidence" in names


def test_every_replay_constraint_name_survives_postgresql_untruncated() -> None:
    for name in REPLAY_TABLES:
        table = Base.metadata.tables[name]
        declared = [
            *(constraint.name for constraint in table.constraints),
            *(index.name for index in table.indexes),
        ]
        for identifier in declared:
            assert identifier is not None
            assert len(identifier) <= POSTGRES_IDENTIFIER_LIMIT, identifier


def test_replay_foreign_keys_are_named_identically_both_ways(sqlite_url: str) -> None:
    """``compare_metadata`` does not diff constraint names, so pin them explicitly."""
    expected = {
        "historical_replay_sessions": {"fk_historical_replay_sessions_universe"},
        "historical_replay_bars": {"fk_historical_replay_bars_session"},
        "historical_replay_observations": {
            "fk_historical_replay_observations_session",
            "fk_historical_replay_observations_previous",
        },
    }
    for table_name, names in expected.items():
        declared = {
            constraint.name
            for constraint in Base.metadata.tables[table_name].foreign_key_constraints
        }
        assert declared == names, table_name

    _upgrade(sqlite_url)
    engine = create_engine(sqlite_url, future=True)
    try:
        for table_name, names in expected.items():
            migrated = {key["name"] for key in inspect(engine).get_foreign_keys(table_name)}
            assert migrated == names, table_name
    finally:
        engine.dispose()


def test_a_replay_bar_cannot_exist_without_its_session(sqlite_url: str) -> None:
    """The FK is what stops orphan research bars from looking like real evidence."""
    _upgrade(sqlite_url)
    engine = create_engine(sqlite_url, future=True)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys = ON")
            with pytest.raises(Exception, match="FOREIGN KEY"):
                connection.execute(
                    text(
                        "insert into historical_replay_bars "
                        "(replay_id, ordinal, interval_at, open, high, low, close, volume) "
                        "values ('historical-replay:nope', 0, '2026-07-29T13:30:00+00:00', "
                        "'1', '1', '1', '1', 1)"
                    )
                )
    finally:
        engine.dispose()


def test_a_first_observation_may_not_claim_a_predecessor(sqlite_url: str) -> None:
    """Correction history has to be honest: revision 1 replaced nothing."""
    _upgrade(sqlite_url)
    engine = create_engine(sqlite_url, future=True)
    try:
        with engine.begin() as connection:
            _seed_session(connection, "historical-replay:aaa")
            _seed_session(connection, "historical-replay:bbb")
        with engine.begin() as connection, pytest.raises(Exception, match="CHECK constraint"):
            connection.execute(
                text(
                    "insert into historical_replay_observations "
                    "(symbol, session_date, revision, replay_id, previous_replay_id, "
                    "observed_at, outcome) values "
                    "('AAPL', '2026-07-29', 1, 'historical-replay:aaa', "
                    "'historical-replay:bbb', '2026-07-30T21:00:00+00:00', 'recorded')"
                )
            )
    finally:
        engine.dispose()


def test_a_correction_may_not_point_at_itself(sqlite_url: str) -> None:
    _upgrade(sqlite_url)
    engine = create_engine(sqlite_url, future=True)
    try:
        with engine.begin() as connection:
            _seed_session(connection, "historical-replay:aaa")
        with engine.begin() as connection, pytest.raises(Exception, match="CHECK constraint"):
            connection.execute(
                text(
                    "insert into historical_replay_observations "
                    "(symbol, session_date, revision, replay_id, previous_replay_id, "
                    "observed_at, outcome) values "
                    "('AAPL', '2026-07-29', 2, 'historical-replay:aaa', "
                    "'historical-replay:aaa', '2026-07-30T21:00:00+00:00', 'corrected')"
                )
            )
    finally:
        engine.dispose()


def test_an_unknown_status_is_rejected(sqlite_url: str) -> None:
    _upgrade(sqlite_url)
    engine = create_engine(sqlite_url, future=True)
    try:
        with engine.begin() as connection, pytest.raises(Exception, match="CHECK constraint"):
            _seed_session(connection, "historical-replay:aaa", status="promoted-to-official")
    finally:
        engine.dispose()


def _seed_session(connection: object, replay_id: str, *, status: str = "complete") -> None:
    execute = connection.execute  # type: ignore[attr-defined]
    execute(
        text(
            "insert or ignore into historical_replay_universes "
            "(universe_id, label, symbols, created_at) values "
            "('u1', 'test', '[\"AAPL\"]', '2026-07-30T21:00:00+00:00')"
        )
    )
    execute(
        text(
            "insert into historical_replay_sessions "
            "(replay_id, universe_id, symbol, session_date, provider, source, status, "
            "retrieved_at, first_seen_at, last_seen_at, raw_payload_digest, "
            "normalized_bar_digest, expected_bar_count, returned_bar_count, "
            "in_session_bar_count, unique_bar_count, request_params, validation) values "
            f"('{replay_id}', 'u1', 'AAPL', '2026-07-29', 'schwab', 'src', '{status}', "
            "'2026-07-30T21:00:00+00:00', '2026-07-30T21:00:00+00:00', "
            "'2026-07-30T21:00:00+00:00', 'raw', 'norm', 78, 78, 78, 78, '{}', '{}')"
        )
    )


def test_upgrade_is_idempotent_against_a_model_built_database(sqlite_url: str) -> None:
    """Databases already built by ``create_all`` must still accept the chain."""
    engine = create_engine(sqlite_url, future=True)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()

    _upgrade(sqlite_url)  # must not raise "table already exists"

    engine = create_engine(sqlite_url, future=True)
    try:
        names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    for table in REPLAY_TABLES:
        assert table in names


def test_downgrade_removes_only_the_replay_tables(sqlite_url: str) -> None:
    _upgrade(sqlite_url)
    command.downgrade(_config(sqlite_url), "20260728_0003")

    engine = create_engine(sqlite_url, future=True)
    try:
        remaining = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    for table in REPLAY_TABLES:
        assert table not in remaining
    # Nothing official is collateral damage.
    assert "market_data_daily_evidence" in remaining
    assert "cohort_alerts" in remaining
    assert "cohort_runs" in remaining
    assert "official_daily_observations" in remaining


def test_downgrade_then_upgrade_restores_the_replay_boundary(sqlite_url: str) -> None:
    _upgrade(sqlite_url)
    command.downgrade(_config(sqlite_url), "20260728_0003")
    _upgrade(sqlite_url)

    engine = create_engine(sqlite_url, future=True)
    try:
        names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    for table in REPLAY_TABLES:
        assert table in names
