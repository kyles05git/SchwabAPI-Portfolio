"""The migration chain must build exactly what the models declare.

This is the test that was missing. `CohortAlert` shipped in task #62 with no migration,
and nothing failed: the offline suites build schema with
``Database(..., create_schema=True)``, so a fresh database always picks up whatever the
models say, and the baseline revision calls ``Base.metadata.create_all`` rather than
declaring DDL. The result was a shared PostgreSQL database missing ``cohort_alerts``
while ``alembic upgrade head`` reported success and applied nothing.

So the check here is deliberately *not* "does ``cohort_alerts`` exist" — that would
close this hole and leave the next one open. It runs the real migration chain on an
empty database and asserts Alembic can find **no difference** against
``Base.metadata``. Any future model added without a migration fails this immediately.

Offline and hermetic: every database is a throwaway SQLite file under ``tmp_path``.
No ``.env`` is read, and no shared database, broker, or network is reachable. The
opt-in PostgreSQL variant is skipped unless ``SCHWAB_TEST_DATABASE_URL`` is set.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateColumn, CreateTable

from schwab_trader.storage.schema import Base

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Differences Alembic reports that are not real schema drift. SQLite cannot express
#: several constructs the models use, so comparing them would produce noise rather
#: than signal. Nothing here can hide a *missing table* or a *missing column*.
_SQLITE_NOISE = {"modify_nullable", "modify_default", "modify_type"}


def _load_revision(stem: str):
    """Import a revision file by path; ``alembic/versions`` is not an importable package."""
    spec = importlib.util.spec_from_file_location(
        f"_rev_{stem}", REPO_ROOT / "alembic" / "versions" / f"{stem}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _alembic_config(url: str) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture
def sqlite_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'migrated.sqlite3'}"


def _upgrade(url: str) -> None:
    """Run the real chain to head, with env.py reading the injected URL.

    ``alembic/env.py`` normally resolves the URL from ``Settings``. Passing it through
    the config and letting env.py prefer it keeps this test away from ``.env`` entirely.
    """
    command.upgrade(_alembic_config(url), "head")


def _diff(url: str, *, ignore: set[str] = frozenset()) -> list:
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            raw = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()
    return [entry for entry in raw if _kind(entry) not in ignore]


def _kind(entry: object) -> str:
    """Alembic yields either a tuple whose first item is the change kind, or a list."""
    if isinstance(entry, list):
        return "" if not entry else str(entry[0][0])
    return str(entry[0]) if isinstance(entry, tuple) else str(entry)


def test_migration_chain_builds_every_declared_model(sqlite_url: str) -> None:
    """The regression guard. Fails for ANY model added without a migration."""
    _upgrade(sqlite_url)

    engine = create_engine(sqlite_url, future=True)
    try:
        built = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    declared = set(Base.metadata.tables)
    missing = sorted(declared - built)
    assert not missing, (
        f"the migration chain does not create {missing}; "
        "add an explicit Alembic revision for each new model"
    )


def test_migrated_schema_has_no_drift_from_the_models(sqlite_url: str) -> None:
    """No added/removed table or column between the chain and the models."""
    _upgrade(sqlite_url)

    differences = _diff(sqlite_url, ignore=_SQLITE_NOISE)

    assert differences == [], f"migration chain diverges from the models: {differences}"


def test_cohort_alerts_is_created_with_its_dedup_constraint(sqlite_url: str) -> None:
    """The specific table this revision exists for, with the constraint that matters."""
    _upgrade(sqlite_url)

    engine = create_engine(sqlite_url, future=True)
    try:
        inspector = inspect(engine)
        assert "cohort_alerts" in inspector.get_table_names()
        columns = {column["name"] for column in inspector.get_columns("cohort_alerts")}
        uniques = [
            tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("cohort_alerts")
        ]
    finally:
        engine.dispose()

    assert columns == {
        "alert_key",
        "cohort_id",
        "session_id",
        "scheduled_for",
        "kind",
        "delivery",
        "attempts",
        "created_at",
        "updated_at",
        "delivered_at",
        "detail",
        "failure_reason",
    }
    # Without this, repeated scheduler invocations would resend every alert.
    assert ("cohort_id", "session_id", "kind") in uniques


def test_market_data_evidence_tables_preserve_exact_constituents(sqlite_url: str) -> None:
    _upgrade(sqlite_url)

    engine = create_engine(sqlite_url, future=True)
    try:
        inspector = inspect(engine)
        names = set(inspector.get_table_names())
        evidence_columns = {
            column["name"] for column in inspector.get_columns("market_data_daily_evidence")
        }
        constituent_columns = {
            column["name"] for column in inspector.get_columns("market_data_evidence_constituents")
        }
        evidence_uniques = {
            tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("market_data_daily_evidence")
        }
    finally:
        engine.dispose()

    assert "market_data_daily_evidence" in names
    assert "market_data_evidence_constituents" in names
    assert evidence_columns == {
        "dataset_id",
        "symbol",
        "session_date",
        "retrieved_at",
        "source",
        "expected_interval_count",
        "observed_interval_count",
        "first_interval_at",
        "final_interval_at",
        "constituent_digest",
        "open",
        "high",
        "low",
        "close",
        "volume",
    }
    assert constituent_columns == {
        "dataset_id",
        "ordinal",
        "interval_at",
        "open",
        "high",
        "low",
        "close",
        "volume",
    }
    assert ("symbol", "session_date", "constituent_digest") in evidence_uniques


def test_execution_timing_revision_covers_sleeves_and_observations(
    sqlite_url: str,
) -> None:
    _upgrade(sqlite_url)

    engine = create_engine(sqlite_url, future=True)
    try:
        inspector = inspect(engine)
        sleeve_columns = {column["name"] for column in inspector.get_columns("sleeves")}
        observation_columns = {
            column["name"] for column in inspector.get_columns("official_daily_observations")
        }
    finally:
        engine.dispose()

    assert "execution_methodology" in sleeve_columns
    assert {
        "execution_methodology",
        "signal_session_date",
        "execution_session_date",
        "signal_time",
        "execution_time",
    } <= observation_columns


def test_execution_timing_revision_downgrades_to_0004_cleanly(
    sqlite_url: str,
) -> None:
    _upgrade(sqlite_url)
    command.downgrade(_alembic_config(sqlite_url), "20260730_0004")

    engine = create_engine(sqlite_url, future=True)
    try:
        inspector = inspect(engine)
        sleeve_columns = {column["name"] for column in inspector.get_columns("sleeves")}
        observation_columns = {
            column["name"] for column in inspector.get_columns("official_daily_observations")
        }
    finally:
        engine.dispose()

    assert "execution_methodology" not in sleeve_columns
    assert {
        "execution_methodology",
        "signal_session_date",
        "execution_session_date",
        "signal_time",
        "execution_time",
    }.isdisjoint(observation_columns)
    assert "configuration_hash" in sleeve_columns
    assert "valuation_time" in observation_columns


def test_execution_timing_revision_and_models_compile_for_postgresql() -> None:
    """Dialect verification without a server, credentials, socket, or `.env`."""
    dialect = postgresql.dialect()
    revision = _load_revision("20260730_0005_execution_timing")

    revision_ddl = {
        column.name: str(CreateColumn(column).compile(dialect=dialect))
        for _, column in (
            *revision._SLEEVE_COLUMNS,
            *revision._OBSERVATION_COLUMNS,
        )
    }
    assert "execution_methodology" in revision_ddl
    assert "TIMESTAMP WITH TIME ZONE" in revision_ddl["signal_time"]
    assert "TIMESTAMP WITH TIME ZONE" in revision_ddl["execution_time"]

    sleeve_ddl = str(CreateTable(Base.metadata.tables["sleeves"]).compile(dialect=dialect))
    observation_ddl = str(
        CreateTable(Base.metadata.tables["official_daily_observations"]).compile(dialect=dialect)
    )
    assert "execution_methodology" in sleeve_ddl
    assert "execution_methodology" in observation_ddl
    assert observation_ddl.count("TIMESTAMP WITH TIME ZONE") >= 5


def test_evidence_constituent_foreign_key_is_named_identically_both_ways(
    sqlite_url: str,
) -> None:
    """A migrated database and a ``create_all`` one must agree on the FK's name.

    ``compare_metadata`` does not diff constraint names, so this divergence is invisible
    to the drift test above. Unnamed, the convention generates 74 characters that
    PostgreSQL truncates to a hash suffix, while the migration's unnamed FK gets a
    server-assigned ``..._fkey`` — two databases that are genuinely not the same.
    """
    expected = "fk_market_data_evidence_constituents_dataset"
    assert len(expected) <= 63, "must survive PostgreSQL's identifier limit untruncated"

    table = Base.metadata.tables["market_data_evidence_constituents"]
    declared = {
        constraint.name
        for constraint in table.foreign_key_constraints
        if "dataset_id" in constraint.column_keys
    }
    assert declared == {expected}

    _upgrade(sqlite_url)
    engine = create_engine(sqlite_url, future=True)
    try:
        migrated = {
            key["name"]
            for key in inspect(engine).get_foreign_keys("market_data_evidence_constituents")
        }
    finally:
        engine.dispose()
    assert migrated == {expected}


def test_the_chain_is_linear_and_ends_at_one_head() -> None:
    """A branched or multi-head chain would make `upgrade head` ambiguous."""
    script = ScriptDirectory.from_config(_alembic_config("sqlite://"))

    assert len(script.get_heads()) == 1, f"expected one head, found {script.get_heads()}"


def test_the_baseline_revision_is_pinned_not_a_view_of_the_models() -> None:
    """The defect that hid the missing table: a revision that means "whatever the
    models say right now".

    A revision has to mean the same thing forever. While the baseline called
    ``create_all`` with no table list, adding a model silently changed what it would
    build on a fresh database and left every stamped database behind — invisibly,
    because ``upgrade head`` had nothing to apply.
    """
    baseline = _load_revision("20260723_0001_shared_storage")

    assert len(baseline.BASELINE_TABLES) == 24
    assert "market_data_daily_evidence" not in baseline.BASELINE_TABLES
    assert "market_data_evidence_constituents" not in baseline.BASELINE_TABLES

    assert "cohort_alerts" not in baseline.BASELINE_TABLES, (
        "cohort_alerts postdates the baseline; it belongs to revision 20260728_0002"
    )
    # Every pinned name must still resolve, so a model rename cannot leave the
    # baseline silently creating nothing.
    for name in baseline.BASELINE_TABLES:
        assert name in Base.metadata.tables, f"baseline names a table no model declares: {name}"


def test_upgrade_is_idempotent_against_a_model_built_database(sqlite_url: str) -> None:
    """A database created from the models must still accept the chain.

    This is the real shape of the deployed world: databases exist that were built by
    `create_all` and already have `cohort_alerts`. Stamping and upgrading them must not
    fail on "table already exists".
    """
    engine = create_engine(sqlite_url, future=True)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()

    _upgrade(sqlite_url)  # must not raise

    assert _diff(sqlite_url, ignore=_SQLITE_NOISE) == []


def test_downgrade_evidence_revision_preserves_prior_tables(sqlite_url: str) -> None:
    _upgrade(sqlite_url)
    command.downgrade(_alembic_config(sqlite_url), "20260728_0002")

    engine = create_engine(sqlite_url, future=True)
    try:
        remaining = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert "market_data_daily_evidence" not in remaining
    assert "market_data_evidence_constituents" not in remaining
    assert "cohort_alerts" in remaining
    assert "cohort_runs" in remaining


def test_downgrade_to_baseline_removes_all_later_tables(sqlite_url: str) -> None:
    _upgrade(sqlite_url)
    command.downgrade(_alembic_config(sqlite_url), "20260723_0001")

    engine = create_engine(sqlite_url, future=True)
    try:
        remaining = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert "market_data_daily_evidence" not in remaining
    assert "market_data_evidence_constituents" not in remaining

    assert "cohort_alerts" not in remaining
    assert "cohort_runs" in remaining, "the baseline tables must survive the downgrade"


# ---------------------------------------------------------------------------
# Opt-in PostgreSQL: dialect-specific types the SQLite run cannot exercise
# ---------------------------------------------------------------------------

TEST_DATABASE_URL = os.environ.get("SCHWAB_TEST_DATABASE_URL", "").strip()


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="SCHWAB_TEST_DATABASE_URL is not configured")
def test_postgresql_migration_chain_matches_the_models() -> None:
    """The dialect that actually matters: AwareTimestamp becomes TIMESTAMPTZ here."""
    parsed = make_url(TEST_DATABASE_URL)
    if "test" not in (parsed.database or "").casefold():
        pytest.fail("SCHWAB_TEST_DATABASE_URL must identify a dedicated test database")
    schema = f"schwab_mig_{uuid.uuid4().hex}"
    admin_url = parsed.set(drivername="postgresql+psycopg")
    admin_engine = create_engine(admin_url, hide_parameters=True)
    with admin_engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    scoped = admin_url.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(
        hide_password=False
    )
    try:
        _upgrade(scoped)
        engine = create_engine(scoped, future=True)
        try:
            tables = set(inspect(engine).get_table_names())
            assert "cohort_alerts" in tables
            assert "market_data_evidence_constituents" in tables
        finally:
            engine.dispose()
        assert _diff(scoped) == []
    finally:
        with admin_engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin_engine.dispose()
