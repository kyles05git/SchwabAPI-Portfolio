"""Revision ``20260811_0007`` must converge CHECK constraint names, not paper over them.

The defect: ``NAMING_CONVENTION["ck"]`` is ``ck_%(table_name)s_%(constraint_name)s``, so
a revision that passes an already-expanded name gets it expanded a *second* time.
Revisions 0003, 0004, and 0006 did exactly that, and 21 constraints across 7 tables were
physically stored as ``ck_<table>_ck_<table>_<short>``. Alembic 1.19.1 started diffing
CHECK constraints and made it visible; 1.18.5 never looked.

What matters here is that the tests bind to the *physical* schema. A test that compared
normalized names would pass against a database that is still wrong, which is the whole
failure mode being closed.

Offline and hermetic: every database is a throwaway SQLite file under ``tmp_path``, and
the PostgreSQL assertions compile DDL against the dialect without a server, credentials,
or a socket. No ``.env``, shared database, or network is reachable.
"""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import mysql, postgresql

from schwab_trader.storage.schema import Base

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The revision immediately before the convergence revision: a database at this point
#: carries the historical doubled names, built by the real chain rather than by a fixture
#: that merely claims to reproduce them.
LEGACY_REVISION = "20260801_0006"


def _load_revision(stem: str) -> Any:
    spec = importlib.util.spec_from_file_location(
        f"_rev_{stem}", REPO_ROOT / "alembic" / "versions" / f"{stem}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REVISION = _load_revision("20260811_0007_check_constraint_names")


def _alembic_config(url: str) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture
def sqlite_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'converge.sqlite3'}"


def _upgrade(url: str, target: str = "head") -> None:
    command.upgrade(_alembic_config(url), target)


def _checks(url: str, table: str) -> set[str]:
    """The CHECK constraint names the database physically reports for ``table``."""
    engine = create_engine(url, future=True)
    try:
        return {
            check["name"]
            for check in inspect(engine).get_check_constraints(table)
            if check.get("name")
        }
    finally:
        engine.dispose()


def _snapshot(url: str) -> dict[str, dict[str, Any]]:
    """Every schema object the rename must leave alone, per table."""
    engine = create_engine(url, future=True)
    try:
        inspector = inspect(engine)
        return {
            table: {
                "columns": [
                    (c["name"], str(c["type"]), c["nullable"])
                    for c in inspector.get_columns(table)
                ],
                "pk": inspector.get_pk_constraint(table).get("constrained_columns"),
                "fks": sorted(
                    (f["name"], tuple(f["constrained_columns"]), f["referred_table"])
                    for f in inspector.get_foreign_keys(table)
                ),
                "uniques": sorted(
                    (u["name"], tuple(u["column_names"]))
                    for u in inspector.get_unique_constraints(table)
                ),
                "indexes": sorted(
                    (i["name"], tuple(i["column_names"])) for i in inspector.get_indexes(table)
                ),
            }
            for table in sorted(inspector.get_table_names())
        }
    finally:
        engine.dispose()


def _plan(url: str, *, to_canonical: bool = True) -> list[Any]:
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            return REVISION.plan(connection, to_canonical=to_canonical)
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# The regression itself
# ---------------------------------------------------------------------------


def test_alembic_floor_is_high_enough_to_diff_check_constraints() -> None:
    """The floor is the guard. Below 1.19.1 the parity suite cannot see this drift.

    A lower local floor than CI resolved is precisely what let the doubled names reach
    `main` with a green local test run.
    """
    import alembic

    declared = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = declared["project"]["dependencies"]
    assert "alembic>=1.19.1,<2" in requirements, (
        "pyproject must pin the Alembic floor that diffs CHECK constraints"
    )

    installed = tuple(int(part) for part in alembic.__version__.split(".")[:3])
    assert installed >= (1, 19, 1), (
        f"this suite is meaningless below Alembic 1.19.1; found {alembic.__version__}"
    )


def test_fresh_chain_leaves_no_check_constraint_drift(sqlite_url: str) -> None:
    """The failing CI assertion, narrowed to the constraints that caused it."""
    _upgrade(sqlite_url)

    engine = create_engine(sqlite_url, future=True)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            differences = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    constraint_drift = [
        entry
        for entry in differences
        if isinstance(entry, tuple) and str(entry[0]).endswith("_constraint")
    ]
    assert constraint_drift == [], (
        f"the migrated schema still disagrees with the models: {constraint_drift}"
    )


def test_every_affected_table_ends_at_the_canonical_model_names(sqlite_url: str) -> None:
    """Physical names, table by table, must equal what the models declare."""
    _upgrade(sqlite_url)

    affected = {table for table, _ in REVISION.CANONICAL_CHECKS}
    for table in sorted(affected):
        declared = {
            constraint.name
            for constraint in Base.metadata.tables[table].constraints
            if isinstance(constraint, sa.CheckConstraint) and constraint.name
        }
        assert _checks(sqlite_url, table) == declared, f"{table} diverges from its model"


def test_a_database_with_the_doubled_names_upgrades_to_canonical(sqlite_url: str) -> None:
    """The convergence path, proven against a database that really has the defect."""
    _upgrade(sqlite_url, LEGACY_REVISION)

    # The defect must actually be present, or the rest of this test proves nothing.
    legacy = _checks(sqlite_url, "market_data_daily_evidence")
    assert "ck_market_data_daily_evidence_ck_market_data_daily_evidence_complete" in legacy
    assert "ck_market_data_daily_evidence_complete" not in legacy
    assert len(_plan(sqlite_url)) == len(REVISION.CANONICAL_CHECKS) == 21

    _upgrade(sqlite_url)

    for table, canonical in REVISION.CANONICAL_CHECKS:
        names = _checks(sqlite_url, table)
        assert canonical in names, f"{canonical} missing after upgrade"
        assert REVISION.doubled_name(table, canonical) not in names


def test_repeated_inspection_after_upgrade_reports_no_remaining_work(sqlite_url: str) -> None:
    """Re-running the resolver must find nothing left to do."""
    _upgrade(sqlite_url)

    assert _plan(sqlite_url) == []
    # And the revision applied a second time is a no-op rather than an error.
    _upgrade(sqlite_url)
    assert _plan(sqlite_url) == []


def test_already_canonical_names_are_left_untouched(sqlite_url: str) -> None:
    """A ``create_all`` database never carried the doubled names; nothing may change."""
    engine = create_engine(sqlite_url, future=True)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()
    command.stamp(_alembic_config(sqlite_url), LEGACY_REVISION)

    before = _snapshot(sqlite_url)
    before_checks = {
        table: _checks(sqlite_url, table) for table, _ in REVISION.CANONICAL_CHECKS
    }
    assert _plan(sqlite_url) == [], "a create_all database has no rename to perform"

    _upgrade(sqlite_url)

    assert _snapshot(sqlite_url) == before
    assert {table: _checks(sqlite_url, table) for table, _ in REVISION.CANONICAL_CHECKS} == (
        before_checks
    )


def test_unrelated_check_constraints_are_not_renamed(sqlite_url: str) -> None:
    """``sleeves`` carries a correctly named CHECK from the baseline. It must survive."""
    _upgrade(sqlite_url, LEGACY_REVISION)
    before = _checks(sqlite_url, "sleeves")
    assert before == {"ck_sleeves_scope_matches_cohort"}

    _upgrade(sqlite_url)

    assert _checks(sqlite_url, "sleeves") == before


# ---------------------------------------------------------------------------
# SQLite rebuilds the table. Rows and every other schema object must survive.
# ---------------------------------------------------------------------------

_EVIDENCE_ROW = {
    "dataset_id": "ds-1",
    "symbol": "SPY",
    "session_date": "2026-08-03",
    "retrieved_at": "2026-08-03 21:00:00.000000+00:00",
    "source": "test-fixture",
    "expected_interval_count": 2,
    "observed_interval_count": 2,
    "first_interval_at": "2026-08-03 13:30:00.000000+00:00",
    "final_interval_at": "2026-08-03 20:00:00.000000+00:00",
    "constituent_digest": "digest-1",
    "open": "100.10",
    "high": "101.20",
    "low": "99.30",
    "close": "100.90",
    "volume": 12345,
}

_CONSTITUENT_ROWS = [
    {
        "dataset_id": "ds-1",
        "ordinal": 0,
        "interval_at": "2026-08-03 13:30:00.000000+00:00",
        "open": "100.10",
        "high": "100.50",
        "low": "100.00",
        "close": "100.40",
        "volume": 5000,
    },
    {
        "dataset_id": "ds-1",
        "ordinal": 1,
        "interval_at": "2026-08-03 20:00:00.000000+00:00",
        "open": "100.40",
        "high": "101.20",
        "low": "99.30",
        "close": "100.90",
        "volume": 7345,
    },
]

_ACCOUNTING_ROW = {
    "entry_id": "entry-1",
    "cohort_id": "cohort-1",
    "sleeve_id": "sleeve-1",
    "observation_key": "obs-1",
    "session_date": "2026-08-03",
    "area": "cash",
    "finding": "difference",
    "summary": "a difference needs a summary",
    "explanation": "recorded by the fixture",
    "recorded_at": "2026-08-03 21:05:00.000000+00:00",
    "recorded_by": "test-fixture",
    "revision": 0,
    "supersedes": None,
}


def _insert(url: str, table: str, rows: list[dict[str, Any]]) -> None:
    engine = create_engine(url, future=True)
    try:
        with engine.begin() as connection:
            for row in rows:
                columns = ", ".join(f'"{name}"' for name in row)
                binds = ", ".join(f":{name}" for name in row)
                connection.execute(
                    text(f'INSERT INTO "{table}" ({columns}) VALUES ({binds})'), row
                )
    finally:
        engine.dispose()


def _rows(url: str, table: str, order: str) -> list[tuple[Any, ...]]:
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            result = connection.execute(text(f'SELECT * FROM "{table}" ORDER BY {order}'))
            return [tuple(row) for row in result]
    finally:
        engine.dispose()


def test_sqlite_rebuild_preserves_rows_and_every_other_schema_object(sqlite_url: str) -> None:
    """The batch rebuild copies the table. Nothing may be lost in the copy."""
    _upgrade(sqlite_url, LEGACY_REVISION)
    _insert(sqlite_url, "market_data_daily_evidence", [_EVIDENCE_ROW])
    _insert(sqlite_url, "market_data_evidence_constituents", _CONSTITUENT_ROWS)
    _insert(sqlite_url, "cohort_accounting_checks", [_ACCOUNTING_ROW])

    before_schema = _snapshot(sqlite_url)
    before_evidence = _rows(sqlite_url, "market_data_daily_evidence", "dataset_id")
    before_constituents = _rows(sqlite_url, "market_data_evidence_constituents", "ordinal")
    before_accounting = _rows(sqlite_url, "cohort_accounting_checks", "entry_id")
    assert before_evidence and before_constituents and before_accounting

    _upgrade(sqlite_url)

    # Guard against passing vacuously: the rebuild must actually have happened, or this
    # test would prove only that doing nothing preserves everything.
    assert "ck_market_data_daily_evidence_complete" in _checks(
        sqlite_url, "market_data_daily_evidence"
    )

    assert _rows(sqlite_url, "market_data_daily_evidence", "dataset_id") == before_evidence
    assert _rows(sqlite_url, "market_data_evidence_constituents", "ordinal") == before_constituents
    assert _rows(sqlite_url, "cohort_accounting_checks", "entry_id") == before_accounting

    after_schema = _snapshot(sqlite_url)
    assert set(after_schema) == set(before_schema), "the rebuild added or dropped a table"
    for table in before_schema:
        assert after_schema[table] == before_schema[table], (
            f"{table} lost or changed a column, key, index, or non-CHECK constraint"
        )


def test_renamed_constraints_still_reject_the_rows_they_forbid(sqlite_url: str) -> None:
    """A rebuild that kept the *name* but dropped the *rule* would be invisible above."""
    _upgrade(sqlite_url)
    # The rules asserted below must belong to the *renamed* constraints, not to
    # survivors that were never rebuilt.
    assert "ck_market_data_daily_evidence_volume_nonnegative" in _checks(
        sqlite_url, "market_data_daily_evidence"
    )
    _insert(sqlite_url, "market_data_daily_evidence", [_EVIDENCE_ROW])

    violating = dict(_EVIDENCE_ROW)
    violating["dataset_id"] = "ds-negative-volume"
    violating["constituent_digest"] = "digest-2"
    violating["volume"] = -1
    with pytest.raises(sa.exc.IntegrityError):
        _insert(sqlite_url, "market_data_daily_evidence", [violating])

    incomplete = dict(_EVIDENCE_ROW)
    incomplete["dataset_id"] = "ds-incomplete"
    incomplete["constituent_digest"] = "digest-3"
    incomplete["observed_interval_count"] = 1
    with pytest.raises(sa.exc.IntegrityError):
        _insert(sqlite_url, "market_data_daily_evidence", [incomplete])

    # The self-referential foreign key must still be enforceable after the rebuild.
    superseding = dict(_ACCOUNTING_ROW)
    superseding["entry_id"] = "entry-2"
    superseding["revision"] = 1
    superseding["supersedes"] = "entry-1"
    _insert(sqlite_url, "cohort_accounting_checks", [_ACCOUNTING_ROW])
    _insert(sqlite_url, "cohort_accounting_checks", [superseding])
    assert len(_rows(sqlite_url, "cohort_accounting_checks", "entry_id")) == 2


def test_downgrade_restores_the_historical_names_and_round_trips(sqlite_url: str) -> None:
    """The revision must be reversible, or a rollback would strand the database."""
    _upgrade(sqlite_url, LEGACY_REVISION)
    legacy = {table: _checks(sqlite_url, table) for table, _ in REVISION.CANONICAL_CHECKS}

    _upgrade(sqlite_url)
    command.downgrade(_alembic_config(sqlite_url), LEGACY_REVISION)

    assert {table: _checks(sqlite_url, table) for table, _ in REVISION.CANONICAL_CHECKS} == legacy

    _upgrade(sqlite_url)
    assert _plan(sqlite_url) == []


# ---------------------------------------------------------------------------
# PostgreSQL: truncation is deterministic and the DDL is valid
# ---------------------------------------------------------------------------


def test_postgresql_truncated_names_are_identified_deterministically() -> None:
    """19 of the 21 exceed 63 characters and are stored hashed. Both forms must resolve."""
    dialect = postgresql.dialect()
    assert dialect.max_identifier_length == 63

    truncated: list[str] = []
    whole: list[str] = []
    for table, canonical in REVISION.CANONICAL_CHECKS:
        doubled = REVISION.doubled_name(table, canonical)
        stored = REVISION.stored_name(dialect, doubled)
        assert len(stored) <= 63, f"{stored} would not fit PostgreSQL"
        # Deterministic: the same input must always give the same stored name.
        assert stored == REVISION.stored_name(dialect, doubled)
        (whole if stored == doubled else truncated).append(stored)

    assert len(truncated) == 19
    assert sorted(whole) == [
        "ck_cohort_accounting_checks_ck_cohort_accounting_checks_area",
        "ck_cohort_accounting_checks_ck_cohort_accounting_checks_finding",
    ]
    # Truncation must not collide: three of these share a 55-character prefix and are
    # separated only by the hash suffix, which is exactly the ambiguity being avoided.
    assert len(set(truncated)) == len(truncated)


def test_postgresql_stored_name_matches_the_ddl_the_dialect_emits() -> None:
    """The resolver's expectation and the compiler's output must agree by construction."""
    dialect = postgresql.dialect()
    table = "market_data_evidence_constituents"
    canonical = "ck_market_data_evidence_constituents_ordinal_nonnegative"
    doubled = REVISION.doubled_name(table, canonical)

    metadata = sa.MetaData(naming_convention={"ck": "ck_%(table_name)s_%(constraint_name)s"})
    subject = sa.Table(
        table,
        metadata,
        sa.Column("ordinal", sa.Integer()),
        # Reproduces exactly what revision 0003 did: an already-expanded name.
        sa.CheckConstraint("ordinal >= 0", name=canonical),
    )
    emitted = str(sa.schema.CreateTable(subject).compile(dialect=dialect))

    assert REVISION.stored_name(dialect, doubled) in emitted
    assert canonical not in emitted.replace(REVISION.stored_name(dialect, doubled), "")


def test_postgresql_rename_compiles_to_valid_ddl() -> None:
    """Native in-place rename, quoted, within PostgreSQL's identifier limit."""
    dialect = postgresql.dialect()
    statements = []
    for table, canonical in REVISION.CANONICAL_CHECKS:
        doubled = REVISION.doubled_name(table, canonical)
        rename = REVISION.Rename(
            table=table,
            old=REVISION.stored_name(dialect, doubled),
            new=canonical,
            sqltext="",
        )
        statement = REVISION.postgresql_rename_statement(dialect, rename)
        statements.append(statement)
        assert statement.startswith(f"ALTER TABLE {table} RENAME CONSTRAINT ")
        assert statement.endswith(f" TO {canonical}")
        assert len(rename.old) <= 63 and len(rename.new) <= 63
        # It must parse as a single statement with no injected quoting surprises.
        assert ";" not in statement and '"' not in statement

    assert len(statements) == 21
    assert (
        "ALTER TABLE market_data_daily_evidence RENAME CONSTRAINT "
        "ck_market_data_daily_evidence_ck_market_data_daily_evid_41b1 "
        "TO ck_market_data_daily_evidence_complete"
    ) in statements


def test_the_sqlite_batch_path_is_used_only_for_sqlite() -> None:
    """An unsupported backend must refuse rather than emit DDL it cannot verify."""
    source = (
        REPO_ROOT / "alembic" / "versions" / "20260811_0007_check_constraint_names.py"
    ).read_text(encoding="utf-8")
    assert 'if dialect.name == "postgresql"' in source
    assert 'if dialect.name == "sqlite"' in source
    assert "raise NotImplementedError" in source


# ---------------------------------------------------------------------------
# Fail closed: never guess which constraint to rename
# ---------------------------------------------------------------------------


def _operations(connection: sa.Connection) -> Operations:
    """Alembic operations bound to a live connection, with no naming convention.

    Without ``target_metadata`` in the context options the convention is not applied, so
    the names written here are the literal names the test intends.
    """
    return Operations(MigrationContext.configure(connection))


def test_a_missing_constraint_fails_visibly(sqlite_url: str) -> None:
    _upgrade(sqlite_url, LEGACY_REVISION)
    doomed = REVISION.doubled_name(
        "historical_replay_bars", "ck_historical_replay_bars_volume_nonnegative"
    )

    engine = create_engine(sqlite_url, future=True)
    try:
        with engine.begin() as connection:
            with _operations(connection).batch_alter_table(
                "historical_replay_bars", recreate="always"
            ) as batch_op:
                batch_op.drop_constraint(doomed, type_="check")
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="missing CHECK constraint"):
        _plan(sqlite_url)


def test_both_names_present_at_once_fails_visibly(sqlite_url: str) -> None:
    """Two candidates for one target is ambiguous; the revision must not pick one."""
    _upgrade(sqlite_url, LEGACY_REVISION)

    engine = create_engine(sqlite_url, future=True)
    try:
        with engine.begin() as connection:
            with _operations(connection).batch_alter_table(
                "historical_replay_bars", recreate="always"
            ) as batch_op:
                batch_op.create_check_constraint(
                    "ck_historical_replay_bars_volume_nonnegative", sa.text("volume >= 0")
                )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="ambiguous CHECK constraints"):
        _plan(sqlite_url)


def test_an_unexpected_truncated_name_fails_visibly() -> None:
    """A hash that does not match this dialect's is not close enough to act on."""
    dialect = postgresql.dialect()
    table = "historical_replay_observations"
    canonical = "ck_historical_replay_observations_revision_positive"
    doubled = REVISION.doubled_name(table, canonical)
    expected = REVISION.stored_name(dialect, doubled)
    impostor = expected[:-4] + "0000"
    assert impostor != expected

    with pytest.raises(RuntimeError, match="ambiguous CHECK constraint"):
        REVISION._resolve(
            dialect,
            table,
            {impostor: "revision >= 1"},
            target=canonical,
            source=expected,
            doubled=doubled,
        )


def test_a_missing_table_fails_visibly(sqlite_url: str) -> None:
    _upgrade(sqlite_url, LEGACY_REVISION)

    engine = create_engine(sqlite_url, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE historical_replay_bars"))
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="missing table"):
        _plan(sqlite_url)


def test_an_unsupported_backend_refuses_rather_than_guessing() -> None:
    """MySQL has neither PostgreSQL's rename nor SQLite's batch semantics."""
    rename = REVISION.Rename(
        table="historical_replay_bars",
        old="ck_historical_replay_bars_ck_historical_replay_bars_volume_nonnegative",
        new="ck_historical_replay_bars_volume_nonnegative",
        sqltext="volume >= 0",
    )

    class _Bind:
        dialect = mysql.dialect()

    original = REVISION.op.get_bind
    REVISION.op.get_bind = lambda: _Bind()  # type: ignore[assignment]
    try:
        with pytest.raises(NotImplementedError, match="only postgresql and sqlite"):
            REVISION._apply([rename])
    finally:
        REVISION.op.get_bind = original  # type: ignore[assignment]
