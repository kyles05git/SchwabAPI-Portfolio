"""An unapplied evidence migration must be reported, not discovered mid-session.

The shared PostgreSQL evidence store does not create its own tables. If revision
``20260728_0003`` was never applied, the first session that needs the five-minute daily
fallback fails for *every* symbol as ``state: "provider_error"``, blocks all members, and
names a provider fault that never happened — while ``cohort readiness`` still reports
ready. These tests pin the readiness check that closes that gap.

The last section covers the same module's cache of local evidence databases, which holds
live connection pools and so must dispose what it evicts.

Offline: SQLite under ``tmp_path`` only. Nothing here connects to the user's shared
database, executes DDL against it, or runs a cohort.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schwab_trader import cohort_readiness
from schwab_trader.cohort_readiness import CheckStatus, ReadinessFacts, assess_readiness
from schwab_trader.config import Settings
from schwab_trader.storage import factory as storage_factory
from schwab_trader.storage.database import Database
from schwab_trader.storage.schema import Base

EVIDENCE_TABLES = storage_factory.MARKET_DATA_EVIDENCE_TABLES


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        token_path=tmp_path / "tokens.json",
        state_db_path=tmp_path / "state.sqlite3",
        log_path=tmp_path / "logs" / "app.log",
        sleeves_dir=tmp_path / "sleeves",
        market_data_evidence_db_path=tmp_path / "evidence.sqlite3",
    )


def _database_without_revision(tmp_path: Path) -> Database:
    """A shared database migrated to a revision *before* the evidence tables existed."""
    target = Database(f"sqlite:///{tmp_path / 'behind.sqlite3'}")
    Base.metadata.create_all(
        bind=target.engine,
        tables=[
            table for table in Base.metadata.sorted_tables if table.name not in EVIDENCE_TABLES
        ],
    )
    return target


def test_probe_names_every_missing_evidence_table(tmp_path: Path) -> None:
    target = _database_without_revision(tmp_path)
    try:
        assert storage_factory.missing_tables(target, EVIDENCE_TABLES) == EVIDENCE_TABLES
    finally:
        target.dispose()


def test_probe_is_clean_once_the_revision_is_applied(tmp_path: Path) -> None:
    target = Database(f"sqlite:///{tmp_path / 'current.sqlite3'}", create_schema=True)
    try:
        assert storage_factory.missing_tables(target, EVIDENCE_TABLES) == ()
    finally:
        target.dispose()


def test_shared_backend_behind_its_migration_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _database_without_revision(tmp_path)
    monkeypatch.setattr(storage_factory, "database", lambda _settings: target)
    try:
        missing = storage_factory.missing_market_data_evidence_tables(_settings(tmp_path))
    finally:
        target.dispose()
    assert missing == EVIDENCE_TABLES


def test_local_sqlite_can_never_be_behind_a_migration(tmp_path: Path) -> None:
    """It builds its own schema when opened, so there is nothing to be behind."""
    settings = _settings(tmp_path)
    assert storage_factory.database(settings) is None
    assert storage_factory.missing_market_data_evidence_tables(settings) == ()


def _facts(**overrides: object) -> ReadinessFacts:
    # Must be an *active* cohort: these tests assert the evidence-schema check is the
    # only thing standing between the facts and `ready`, and a superseded cohort id
    # fails `cohort-identity` on its own (see `cohort_lifecycle`).
    base: dict[str, object] = {
        "cohort_id": "paper-first-2026-07-28",
        "writer_role": True,
        "shared_database": True,
        "storage_kind": "shared-postgresql",
        "member_count": 7,
        "reproducible_members": 7,
        "notifications_live": True,
    }
    base.update(overrides)
    return ReadinessFacts(**base)  # type: ignore[arg-type]


def test_missing_evidence_tables_block_scheduling_with_the_migration_remedy() -> None:
    report = assess_readiness(_facts(missing_evidence_tables=EVIDENCE_TABLES))
    assert not report.ready
    assert report.exit_code == cohort_readiness.EXIT_NOT_READY
    check = next(item for item in report.checks if item.name == "market-data-evidence-schema")
    assert check.status is CheckStatus.FAIL
    assert [blocking.name for blocking in report.blocking] == ["market-data-evidence-schema"]
    for table in EVIDENCE_TABLES:
        assert table in check.detail
    # The operator is told the command and where the reviewed procedure lives.
    assert "alembic upgrade head" in check.remedy
    assert "docs/migrations/postgresql-runbook.md" in check.remedy


def test_an_uninspectable_evidence_store_also_fails_closed() -> None:
    """Not knowing is not the same as being fine."""
    report = assess_readiness(_facts(evidence_store_error="OperationalError"))
    assert not report.ready
    check = next(item for item in report.checks if item.name == "market-data-evidence-schema")
    assert check.status is CheckStatus.FAIL
    assert "OperationalError" in check.detail
    assert "alembic upgrade head" in check.remedy


def test_present_evidence_tables_pass() -> None:
    report = assess_readiness(_facts())
    assert report.ready
    check = next(item for item in report.checks if item.name == "market-data-evidence-schema")
    assert check.status is CheckStatus.PASS


def test_readiness_payload_names_no_table_contents_or_credentials() -> None:
    import json

    report = assess_readiness(_facts(missing_evidence_tables=EVIDENCE_TABLES))
    text = json.dumps(cohort_readiness.readiness_payload(report)).lower()
    for forbidden in ("postgresql://", "postgres://", "sqlite:///", "password", "sslmode"):
        assert forbidden not in text, forbidden
    assert "market_data_daily_evidence" in text  # the table name is safe to state


# --- The local evidence database cache owns live connection pools -----------------


def test_evicting_a_cached_local_database_disposes_its_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cache holds live connection pools, so eviction must dispose, not just drop."""
    storage_factory._local_market_data_databases.clear()
    limit = storage_factory._MAX_LOCAL_MARKET_DATA_DATABASES
    opened = [
        storage_factory._local_market_data_database(str(tmp_path / f"evidence-{index}.sqlite3"))
        for index in range(limit)
    ]
    first = opened[0]
    disposed: list[Database] = []
    original = first.dispose

    def record() -> None:
        disposed.append(first)
        original()

    monkeypatch.setattr(first, "dispose", record)
    try:
        # One more distinct path evicts the least recently used entry.
        storage_factory._local_market_data_database(str(tmp_path / "evidence-extra.sqlite3"))
        assert disposed == [first]
        assert len(storage_factory._local_market_data_databases) == limit
    finally:
        for database in storage_factory._local_market_data_databases.values():
            database.dispose()
        storage_factory._local_market_data_databases.clear()


def test_the_same_path_reuses_one_engine_regardless_of_spelling(tmp_path: Path) -> None:
    storage_factory._local_market_data_databases.clear()
    direct = tmp_path / "evidence.sqlite3"
    indirect = tmp_path / "sub" / ".." / "evidence.sqlite3"
    try:
        assert storage_factory._local_market_data_database(
            str(direct)
        ) is storage_factory._local_market_data_database(str(indirect))
        assert len(storage_factory._local_market_data_databases) == 1
    finally:
        for database in storage_factory._local_market_data_databases.values():
            database.dispose()
        storage_factory._local_market_data_databases.clear()
