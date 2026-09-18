"""Storage and migration tests for the cohort review tables.

Two questions this file exists to answer, neither of which the domain tests can:

1. Does the *migration chain* build exactly these tables, with the constraints that make
   the append-only guarantee real, and does it roll back cleanly?
2. Does the database itself refuse a bad row, rather than relying on the service having
   been called?

Offline and hermetic: every database is a throwaway SQLite file under ``tmp_path``. No
``.env`` is read and no shared database, broker, or network is reachable. The opt-in
PostgreSQL variant is skipped unless ``SCHWAB_TEST_DATABASE_URL`` is set, and even then
it builds an isolated throwaway schema and drops it.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable

from schwab_trader.cohort_review import (
    CheckIntent,
    DecisionIntent,
    NoteIntent,
    ReviewConflictError,
    ReviewFinding,
    WriteStatus,
)
from schwab_trader.operational_gate import AccountingArea, OperatorAction
from schwab_trader.storage.cohort_reviews import SqlAlchemyCohortReviewStore
from schwab_trader.storage.database import Database
from schwab_trader.storage.schema import Base

REPO_ROOT = Path(__file__).resolve().parent.parent

REVIEW_TABLES = (
    "cohort_accounting_checks",
    "cohort_review_notes",
    "cohort_operator_decisions",
)

COHORT = "paper-test-2026-07-27"
SLEEVE = "trend-large"
SESSION = date(2026, 7, 27)
OBSERVATION = f"{COHORT}|{SLEEVE}|{SESSION.isoformat()}"
AT = datetime(2026, 9, 4, 22, 30, tzinfo=UTC)


def _alembic_config(url: str) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture
def sqlite_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'migrated.sqlite3'}"


@pytest.fixture
def store(tmp_path: Path) -> SqlAlchemyCohortReviewStore:
    database = Database(f"sqlite:///{tmp_path / 'reviews.sqlite3'}", create_schema=True)
    return SqlAlchemyCohortReviewStore(database)


def _check(**overrides: object) -> CheckIntent:
    values: dict[str, object] = {
        "cohort_id": COHORT,
        "sleeve_id": SLEEVE,
        "observation_key": OBSERVATION,
        "session_date": SESSION,
        "area": AccountingArea.CASH,
        "finding": ReviewFinding.MATCHED,
        "summary": None,
        "explanation": None,
        "recorded_at": AT,
        "recorded_by": "operator",
    }
    values.update(overrides)
    return CheckIntent(**values)  # type: ignore[arg-type]


def _decision(**overrides: object) -> DecisionIntent:
    values: dict[str, object] = {
        "cohort_id": COHORT,
        "sleeve_id": SLEEVE,
        "action": OperatorAction.KEEP,
        "rationale": "Operationally sound over thirty sessions.",
        "recorded_at": AT,
        "recorded_by": "operator",
    }
    values.update(overrides)
    return DecisionIntent(**values)  # type: ignore[arg-type]


# --- migration --------------------------------------------------------------


def test_the_chain_creates_the_review_tables_with_their_constraints(
    sqlite_url: str,
) -> None:
    command.upgrade(_alembic_config(sqlite_url), "head")

    engine = create_engine(sqlite_url, future=True)
    try:
        inspector = inspect(engine)
        names = set(inspector.get_table_names())
        check_columns = {c["name"] for c in inspector.get_columns("cohort_accounting_checks")}
        check_uniques = {
            tuple(c["column_names"])
            for c in inspector.get_unique_constraints("cohort_accounting_checks")
        }
        decision_uniques = {
            tuple(c["column_names"])
            for c in inspector.get_unique_constraints("cohort_operator_decisions")
        }
        check_indexes = {i["name"] for i in inspector.get_indexes("cohort_accounting_checks")}
    finally:
        engine.dispose()

    assert set(REVIEW_TABLES) <= names
    assert check_columns == {
        "entry_id",
        "cohort_id",
        "sleeve_id",
        "observation_key",
        "session_date",
        "area",
        "finding",
        "summary",
        "explanation",
        "recorded_at",
        "recorded_by",
        "revision",
        "supersedes",
    }
    # The append-only guarantee: two writers cannot both append the same revision, so a
    # correction can never be silently lost to a concurrent one.
    assert ("cohort_id", "observation_key", "area", "revision") in check_uniques
    assert ("cohort_id", "sleeve_id", "revision") in decision_uniques
    assert "ix_cohort_accounting_checks_cohort_id" in check_indexes


def test_the_review_revision_downgrades_cleanly_to_0005(sqlite_url: str) -> None:
    command.upgrade(_alembic_config(sqlite_url), "head")
    command.downgrade(_alembic_config(sqlite_url), "20260730_0005")

    engine = create_engine(sqlite_url, future=True)
    try:
        remaining = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert set(REVIEW_TABLES).isdisjoint(remaining)
    # Everything the earlier revisions built is untouched. A review is additive; rolling
    # it back must not take cohort evidence with it.
    assert {"cohorts", "cohort_runs", "official_daily_observations", "cohort_alerts"} <= remaining


def test_the_review_revision_round_trips(sqlite_url: str) -> None:
    config = _alembic_config(sqlite_url)
    command.upgrade(config, "head")
    command.downgrade(config, "20260730_0005")
    command.upgrade(config, "head")  # must not fail on "table already exists"

    engine = create_engine(sqlite_url, future=True)
    try:
        remaining = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert set(REVIEW_TABLES) <= remaining


def test_the_upgrade_is_a_no_op_on_a_model_built_database(sqlite_url: str) -> None:
    """The deployed shape: a database created by ``create_all`` already has the tables."""
    engine = create_engine(sqlite_url, future=True)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()

    command.upgrade(_alembic_config(sqlite_url), "head")  # must not raise


def test_the_review_tables_compile_for_postgresql() -> None:
    """Dialect verification without a server, credentials, socket, or ``.env``."""
    dialect = postgresql.dialect()
    ddl = {
        name: str(CreateTable(Base.metadata.tables[name]).compile(dialect=dialect))
        for name in REVIEW_TABLES
    }

    assert "TIMESTAMP WITH TIME ZONE" in ddl["cohort_accounting_checks"]
    assert "TIMESTAMP WITH TIME ZONE" in ddl["cohort_operator_decisions"]
    # PostgreSQL truncates an identifier past 63 characters to a hash suffix, which would
    # make a migrated database and a create_all one genuinely different objects.
    for table in REVIEW_TABLES:
        for constraint in Base.metadata.tables[table].constraints:
            if constraint.name is not None:
                assert len(str(constraint.name)) <= 63, constraint.name
        for index in Base.metadata.tables[table].indexes:
            assert index.name is not None and len(index.name) <= 63, index.name


# --- database-level refusals ------------------------------------------------


def _raw_insert(store: SqlAlchemyCohortReviewStore, statement: str) -> None:
    with store.database.session() as session:
        session.execute(text(statement))


def test_the_database_refuses_an_unknown_accounting_area(
    store: SqlAlchemyCohortReviewStore,
) -> None:
    """Defence in depth: a caller bypassing the service still cannot write nonsense."""
    with pytest.raises(IntegrityError):
        _raw_insert(
            store,
            "INSERT INTO cohort_accounting_checks (entry_id, cohort_id, sleeve_id, "
            "observation_key, session_date, area, finding, recorded_at, recorded_by, "
            "revision) VALUES ('x', 'c', 's', 'o', '2026-07-27', 'equity', 'matched', "
            "'2026-09-04 22:30:00', 'operator', 0)",
        )


def test_the_database_refuses_a_difference_with_no_summary(
    store: SqlAlchemyCohortReviewStore,
) -> None:
    with pytest.raises(IntegrityError):
        _raw_insert(
            store,
            "INSERT INTO cohort_accounting_checks (entry_id, cohort_id, sleeve_id, "
            "observation_key, session_date, area, finding, recorded_at, recorded_by, "
            "revision) VALUES ('x', 'c', 's', 'o', '2026-07-27', 'cash', 'difference', "
            "'2026-09-04 22:30:00', 'operator', 0)",
        )


def test_the_database_refuses_a_decision_with_a_blank_rationale(
    store: SqlAlchemyCohortReviewStore,
) -> None:
    with pytest.raises(IntegrityError):
        _raw_insert(
            store,
            "INSERT INTO cohort_operator_decisions (decision_id, cohort_id, sleeve_id, "
            "action, rationale, recorded_at, recorded_by, revision) VALUES "
            "('x', 'c', 's', 'keep', '', '2026-09-04 22:30:00', 'operator', 0)",
        )


def test_the_database_refuses_an_unknown_operator_action(
    store: SqlAlchemyCohortReviewStore,
) -> None:
    with pytest.raises(IntegrityError):
        _raw_insert(
            store,
            "INSERT INTO cohort_operator_decisions (decision_id, cohort_id, sleeve_id, "
            "action, rationale, recorded_at, recorded_by, revision) VALUES "
            "('x', 'c', 's', 'promote', 'because', '2026-09-04 22:30:00', 'operator', 0)",
        )


# --- adapter semantics ------------------------------------------------------


def test_the_adapter_is_idempotent_and_appends_on_correction(
    store: SqlAlchemyCohortReviewStore,
) -> None:
    first = store.record_check(_check(), allow_supersede=False)
    repeat = store.record_check(_check(), allow_supersede=False)
    corrected = store.record_check(
        _check(finding=ReviewFinding.DIFFERENCE, summary="Trailing 0.04."),
        allow_supersede=True,
    )

    assert first.status is WriteStatus.RECORDED
    assert repeat.status is WriteStatus.UNCHANGED
    assert corrected.status is WriteStatus.SUPERSEDED
    assert corrected.record.revision == 1
    assert corrected.record.supersedes == first.record.entry_id

    stored = store.checks(COHORT)
    assert [item.revision for item in stored] == [0, 1]
    assert stored[0].finding is ReviewFinding.MATCHED  # the original is untouched


def test_the_adapter_refuses_a_silent_overwrite(store: SqlAlchemyCohortReviewStore) -> None:
    store.record_check(_check(), allow_supersede=False)

    with pytest.raises(ReviewConflictError):
        store.record_check(
            _check(finding=ReviewFinding.DIFFERENCE, summary="Trailing 0.04."),
            allow_supersede=False,
        )
    assert len(store.checks(COHORT)) == 1


def test_stored_timestamps_come_back_timezone_aware(
    store: SqlAlchemyCohortReviewStore,
) -> None:
    """SQLite has no native timezone; the adapter must not hand back a naive instant."""
    store.record_check(_check(), allow_supersede=False)
    store.record_decision(_decision(), allow_supersede=False)
    store.add_note(
        NoteIntent(
            cohort_id=COHORT,
            sleeve_id=None,
            observation_key=None,
            note="Reviewed.",
            recorded_at=AT,
            recorded_by="operator",
        )
    )

    for stamp in (
        store.checks(COHORT)[0].recorded_at,
        store.decisions(COHORT)[0].recorded_at,
        store.notes(COHORT)[0].recorded_at,
    ):
        assert stamp.tzinfo is not None
        assert stamp.utcoffset() is not None
        assert stamp == AT


def test_records_are_isolated_by_cohort(store: SqlAlchemyCohortReviewStore) -> None:
    store.record_check(_check(), allow_supersede=False)
    store.record_check(
        _check(cohort_id="paper-other", observation_key="paper-other|s|2026-07-27"),
        allow_supersede=False,
    )

    assert len(store.checks(COHORT)) == 1
    assert len(store.checks("paper-other")) == 1


def test_a_note_is_keyed_on_its_content_not_its_clock(
    store: SqlAlchemyCohortReviewStore,
) -> None:
    def note(text_value: str, when: datetime) -> NoteIntent:
        return NoteIntent(
            cohort_id=COHORT,
            sleeve_id=None,
            observation_key=None,
            note=text_value,
            recorded_at=when,
            recorded_by="operator",
        )

    first = store.add_note(note("Traced it.", AT))
    same = store.add_note(note("Traced it.", datetime(2026, 9, 5, 9, 0, tzinfo=UTC)))
    other = store.add_note(note("Traced it again.", AT))

    assert first.status is WriteStatus.RECORDED
    assert same.status is WriteStatus.UNCHANGED
    assert same.record.note_id == first.record.note_id
    assert other.status is WriteStatus.RECORDED
    assert len(store.notes(COHORT)) == 2


# ---------------------------------------------------------------------------
# Opt-in PostgreSQL: the dialect the shared database actually runs
# ---------------------------------------------------------------------------

TEST_DATABASE_URL = os.environ.get("SCHWAB_TEST_DATABASE_URL", "").strip()


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="SCHWAB_TEST_DATABASE_URL is not configured")
def test_postgresql_migration_and_append_only_semantics() -> None:
    """The same guarantees on PostgreSQL, in a throwaway schema that is then dropped."""
    parsed = make_url(TEST_DATABASE_URL)
    if "test" not in (parsed.database or "").casefold():
        pytest.fail("SCHWAB_TEST_DATABASE_URL must identify a dedicated test database")
    schema = f"schwab_review_{uuid.uuid4().hex}"
    admin_url = parsed.set(drivername="postgresql+psycopg")
    admin_engine = create_engine(admin_url, hide_parameters=True)
    with admin_engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    scoped = admin_url.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(
        hide_password=False
    )
    try:
        command.upgrade(_alembic_config(scoped), "head")
        database = Database(scoped)
        try:
            postgres_store = SqlAlchemyCohortReviewStore(database)
            assert postgres_store.record_check(_check(), allow_supersede=False).status is (
                WriteStatus.RECORDED
            )
            assert postgres_store.record_check(_check(), allow_supersede=False).status is (
                WriteStatus.UNCHANGED
            )
            corrected = postgres_store.record_check(
                _check(finding=ReviewFinding.DIFFERENCE, summary="Trailing 0.04."),
                allow_supersede=True,
            )
            assert corrected.record.revision == 1
            assert [item.revision for item in postgres_store.checks(COHORT)] == [0, 1]
            assert postgres_store.checks(COHORT)[0].recorded_at == AT
        finally:
            database.dispose()
    finally:
        with admin_engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin_engine.dispose()
