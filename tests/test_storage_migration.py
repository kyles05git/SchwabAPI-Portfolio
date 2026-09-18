from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from schwab_trader.storage.database import Database
from schwab_trader.storage.inventory import build_inventory, file_sha256
from schwab_trader.storage.migration import (
    MigrationConflictError,
    execute_migration,
    preflight_migration,
    verify_migration,
)
from schwab_trader.storage.schema import (
    CohortRun,
    CohortRunMember,
    MigrationRun,
    PaperAccount,
    PaperFill,
    PaperOrder,
    Sleeve,
)
from schwab_trader.storage.snapshots import create_snapshot_set

REVISION = "a" * 40


def _legacy_sources(root: Path) -> tuple[Path, Path]:
    registry = root / "data" / "sleeves" / "registry.sqlite3"
    paper = root / "data" / "sleeves" / "bench-spy" / "paper.sqlite3"
    registry.parent.mkdir(parents=True)
    paper.parent.mkdir(parents=True)

    with sqlite3.connect(registry) as conn:
        conn.executescript(
            """
            PRAGMA user_version = 1;
            CREATE TABLE sleeves (
                name TEXT PRIMARY KEY,
                strategy TEXT NOT NULL,
                universe_csv TEXT NOT NULL,
                starting_cash TEXT NOT NULL,
                max_positions INTEGER NOT NULL,
                max_position_fraction TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO sleeves VALUES (
                'bench-spy', 'buy-hold', 'SPY', '10000.00', 1, '1',
                '2026-07-17T16:00:00'
            );
            """
        )

    with sqlite3.connect(paper) as conn:
        conn.executescript(
            """
            PRAGMA user_version = 1;
            CREATE TABLE paper_account (
                id INTEGER PRIMARY KEY,
                starting_cash TEXT NOT NULL,
                cash TEXT NOT NULL,
                realized_pnl TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_accrual TEXT
            );
            CREATE TABLE paper_positions (
                symbol TEXT PRIMARY KEY,
                quantity INTEGER NOT NULL,
                avg_cost TEXT NOT NULL
            );
            CREATE TABLE paper_orders (
                id INTEGER PRIMARY KEY,
                side TEXT NOT NULL,
                symbol TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                limit_price TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                fill_price TEXT,
                created_at TEXT NOT NULL,
                filled_at TEXT
            );
            INSERT INTO paper_account VALUES (
                1, '10000.00', '9499.875', '0', '2026-07-17T16:00:00', NULL
            );
            INSERT INTO paper_positions VALUES ('SPY', 1, '500.125');
            INSERT INTO paper_orders VALUES (
                1, 'BUY', 'SPY', 1, '500.125', 'FILLED', NULL, '500.125',
                '2026-07-18T16:00:00', '2026-07-18T16:00:01'
            );
            """
        )
    return registry, paper


def _destination(path: Path) -> Database:
    return Database(f"sqlite:///{path}", create_schema=True)


def _snapshot(root: Path, backup: Path):
    inventory = build_inventory(root)
    snapshot_root, snapshot = create_snapshot_set(
        inventory,
        backup,
        writers_stopped=True,
    )
    return inventory, snapshot_root, snapshot


def _add_current_registry_id_and_run(root: Path) -> None:
    registry = root / "data" / "sleeves" / "registry.sqlite3"
    with sqlite3.connect(registry) as conn:
        conn.execute("ALTER TABLE sleeves ADD COLUMN sleeve_id TEXT")
        conn.execute(
            "UPDATE sleeves SET sleeve_id = ? WHERE name = ?",
            ("local-bench-id", "bench-spy"),
        )
    runs = root / "data" / "sleeves" / "runs.sqlite3"
    with sqlite3.connect(runs) as conn:
        conn.executescript(
            """
            CREATE TABLE sleeve_runs (
                run_id TEXT PRIMARY KEY,
                run_key TEXT NOT NULL,
                cohort_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                scheduled_for TEXT NOT NULL,
                expected_members TEXT NOT NULL,
                completed_members TEXT NOT NULL,
                snapshot_id TEXT,
                quote_snapshot_id TEXT,
                data_snapshot_ids TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL,
                errors TEXT NOT NULL
            );
            CREATE TABLE sleeve_run_members (
                run_id TEXT NOT NULL,
                sleeve_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                error TEXT,
                PRIMARY KEY (run_id, sleeve_id)
            );
            INSERT INTO sleeve_runs VALUES (
                'run-one', 'official:current-cohort:2026-07-23',
                'current-cohort', 'XNYS:2026-07-23', '2026-07-23',
                '["local-bench-id"]', '["local-bench-id"]',
                'snapshot-one', 'quotes-one', '{"prices":"prices-one"}',
                '2026-07-23T20:00:00+00:00', '2026-07-23T20:01:00+00:00',
                'completed', '[]'
            );
            INSERT INTO sleeve_run_members VALUES (
                'run-one', 'local-bench-id', 0, 'completed',
                '2026-07-23T20:00:00+00:00', '2026-07-23T20:01:00+00:00',
                NULL
            );
            """
        )


def _add_multi_sleeve_observations(root: Path) -> None:
    registry = root / "data" / "sleeves" / "registry.sqlite3"
    with sqlite3.connect(registry) as conn:
        conn.execute(
            """
            INSERT INTO sleeves VALUES (
                'momentum', 'momentum', 'SPY', '10000.00', 1, '1',
                '2026-07-17T16:00:00'
            )
            """
        )
    for sleeve in ("bench-spy", "momentum"):
        evaluation = root / "data" / "sleeves" / sleeve / "eval.sqlite3"
        evaluation.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(evaluation) as conn:
            conn.executescript(
                """
                CREATE TABLE agent_cycles (
                    id INTEGER PRIMARY KEY,
                    ts TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    num_proposals INTEGER NOT NULL,
                    num_filled INTEGER NOT NULL,
                    num_rejected INTEGER NOT NULL,
                    cash TEXT NOT NULL,
                    positions_value TEXT NOT NULL,
                    total_value TEXT NOT NULL,
                    realized_pnl TEXT NOT NULL,
                    unrealized_pnl TEXT NOT NULL,
                    starting_cash TEXT NOT NULL,
                    return_pct TEXT NOT NULL
                );
                CREATE TABLE agent_decisions (
                    id INTEGER PRIMARY KEY,
                    cycle_id INTEGER NOT NULL,
                    side TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    limit_price TEXT NOT NULL,
                    status TEXT NOT NULL,
                    fill_price TEXT,
                    rationale TEXT
                );
                CREATE TABLE official_daily_observations (
                    id INTEGER PRIMARY KEY,
                    observation_key TEXT NOT NULL,
                    cohort_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    sleeve_id TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    strategy_hash TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    decision_time TEXT NOT NULL,
                    valuation_time TEXT NOT NULL,
                    status TEXT NOT NULL,
                    total_value TEXT,
                    return_pct TEXT,
                    benchmark_value TEXT,
                    exposure TEXT,
                    num_positions INTEGER,
                    turnover TEXT,
                    modeled_cost TEXT,
                    num_filled INTEGER NOT NULL,
                    num_rejected INTEGER NOT NULL,
                    quote_coverage TEXT,
                    snapshot_ids TEXT NOT NULL,
                    readiness_ready INTEGER,
                    readiness_reasons TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """
                INSERT INTO official_daily_observations VALUES (
                    1, ?, 'official-cohort', 'shared-run', ?, 'buy-hold',
                    'strategy-hash', '2026-07-23',
                    '2026-07-23T20:00:00+00:00',
                    '2026-07-23T20:00:00+00:00',
                    'official', '10000.00', '0', '10000.00', '1', 1,
                    '0', '0', 0, 0, '1', '{"prices":"snapshot"}', 1, '[]',
                    '2026-07-23T20:01:00+00:00'
                )
                """,
                (f"legacy:{sleeve}:2026-07-23", sleeve),
            )


def test_lossless_legacy_copy_is_idempotent_and_sources_unchanged(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    registry, paper = _legacy_sources(root)
    original_hashes = (file_sha256(registry), file_sha256(paper))
    database = _destination(tmp_path / "destination.sqlite3")
    inventory, snapshot_root, snapshot = _snapshot(root, tmp_path / "backups")
    try:
        preflight = preflight_migration(database, inventory)
        assert preflight.source_rows == 4
        first = execute_migration(
            database,
            snapshot_root,
            snapshot,
            code_revision=REVISION,
        )
        second = execute_migration(
            database,
            snapshot_root,
            snapshot,
            code_revision=REVISION,
        )

        assert first.status == second.status == "completed"
        assert verify_migration(database, snapshot.migration_id) == "passed"
        with database.session() as session:
            account = session.scalar(select(PaperAccount))
            order = session.scalar(select(PaperOrder))
            sleeve = session.scalar(select(Sleeve))
            assert account is not None
            assert order is not None
            assert sleeve is not None
            assert account.starting_cash == Decimal("10000.00")
            assert account.cash == Decimal("9499.875")
            assert sleeve.original_name == "bench-spy"
            assert sleeve.created_at is None
            assert sleeve.source_created_at == "2026-07-17T16:00:00"
            assert session.scalar(select(func.count()).select_from(PaperOrder)) == 1
            assert session.scalar(select(func.count()).select_from(PaperFill)) == 1
    finally:
        database.dispose()

    assert (file_sha256(registry), file_sha256(paper)) == original_hashes


def test_interruption_resumes_without_duplicate_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "source"
    _legacy_sources(root)
    database = _destination(tmp_path / "destination.sqlite3")
    _, snapshot_root, snapshot = _snapshot(root, tmp_path / "backups")
    from schwab_trader.storage import migration as migration_module

    original_import = migration_module._import_source
    interrupted = False

    def interrupt_once(*args, **kwargs):
        nonlocal interrupted
        if kwargs["kind"] == "paper" and not interrupted:
            interrupted = True
            raise RuntimeError("synthetic interruption")
        return original_import(*args, **kwargs)

    monkeypatch.setattr(migration_module, "_import_source", interrupt_once)
    with pytest.raises(MigrationConflictError):
        execute_migration(
            database,
            snapshot_root,
            snapshot,
            code_revision=REVISION,
        )
    monkeypatch.setattr(migration_module, "_import_source", original_import)
    try:
        outcome = execute_migration(
            database,
            snapshot_root,
            snapshot,
            code_revision=REVISION,
        )
        assert outcome.status == "completed"
        with database.session() as session:
            assert session.scalar(select(func.count()).select_from(Sleeve)) == 1
            run = session.get(MigrationRun, snapshot.migration_id)
            assert run is not None
            assert run.status == "completed"
    finally:
        database.dispose()


def test_current_registry_ids_resolve_durable_run_history(tmp_path: Path) -> None:
    root = tmp_path / "source"
    _legacy_sources(root)
    _add_current_registry_id_and_run(root)
    database = _destination(tmp_path / "destination.sqlite3")
    _, snapshot_root, snapshot = _snapshot(root, tmp_path / "backups")
    try:
        execute_migration(
            database,
            snapshot_root,
            snapshot,
            code_revision=REVISION,
        )
        with database.session() as session:
            sleeve = session.scalar(select(Sleeve))
            run = session.get(CohortRun, "run-one")
            member = session.scalar(select(CohortRunMember))
            assert sleeve is not None
            assert run is not None
            assert member is not None
            assert sleeve.source_sleeve_id == "local-bench-id"
            assert run.expected_members == [sleeve.sleeve_id]
            assert run.completed_members == [sleeve.sleeve_id]
            assert member.sleeve_id == sleeve.sleeve_id
            assert member.source_sleeve_id == "local-bench-id"
            assert run.snapshot_id == "snapshot-one"
            assert run.data_snapshot_ids == {"prices": "prices-one"}
    finally:
        database.dispose()


def test_observation_only_history_builds_one_multi_member_run(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    _legacy_sources(root)
    _add_multi_sleeve_observations(root)
    database = _destination(tmp_path / "destination.sqlite3")
    _, snapshot_root, snapshot = _snapshot(root, tmp_path / "backups")
    try:
        execute_migration(
            database,
            snapshot_root,
            snapshot,
            code_revision=REVISION,
        )
        with database.session() as session:
            run = session.get(CohortRun, "shared-run")
            members = list(
                session.scalars(
                    select(CohortRunMember)
                    .where(CohortRunMember.run_id == "shared-run")
                    .order_by(CohortRunMember.ordinal)
                )
            )
            assert run is not None
            assert run.status == "completed"
            assert len(run.expected_members) == len(run.completed_members) == 2
            assert [member.ordinal for member in members] == [0, 1]
            assert {member.sleeve_id for member in members} == set(
                run.expected_members
            )
    finally:
        database.dispose()


def test_preflight_rejects_unexplained_destination_records(tmp_path: Path) -> None:
    root = tmp_path / "source"
    _legacy_sources(root)
    inventory = build_inventory(root)
    database = _destination(tmp_path / "destination.sqlite3")
    try:
        source_path = inventory.eligible_sources[0].relative_path
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                """
                INSERT INTO storage_namespaces (
                    namespace_id, name, kind, source_identity, created_at,
                    immutable_metadata
                ) VALUES (
                    'missing', 'conflict', 'test', NULL,
                    '2026-07-23T12:00:00+00:00', '{}'
                )
                """
            )
            connection.exec_driver_sql(
                """
                INSERT INTO sleeves (
                    sleeve_id, namespace_id, cohort_id, scope_key, name,
                    original_name, source_identity, source_sleeve_id, source_path,
                    strategy, universe,
                    starting_cash, max_positions, max_position_fraction,
                    settlement_t1, leverage, factor, strategy_definition,
                    configuration_hash, decision_frequency, decision_time,
                    created_at, source_created_at
                ) VALUES (
                    'conflict', 'missing', NULL, 'namespace:missing', 'conflict',
                    'conflict', 'conflict', NULL, ?, 'hold', '[]', '1', 1, '1',
                    0, '1', '', NULL, '', '', '', NULL, NULL
                )
                """,
                (source_path,),
            )
        with pytest.raises(MigrationConflictError, match="unexplained"):
            preflight_migration(database, inventory)
    finally:
        database.dispose()


def test_verification_detects_destination_mutation(tmp_path: Path) -> None:
    root = tmp_path / "source"
    _legacy_sources(root)
    database = _destination(tmp_path / "destination.sqlite3")
    _, snapshot_root, snapshot = _snapshot(root, tmp_path / "backups")
    try:
        execute_migration(
            database,
            snapshot_root,
            snapshot,
            code_revision=REVISION,
        )
        with database.session() as session:
            account = session.scalar(select(PaperAccount))
            assert account is not None
            account.cash = Decimal("1.00")
        assert verify_migration(database, snapshot.migration_id) == "failed"
    finally:
        database.dispose()


def test_tampered_snapshot_is_rejected_before_destination_write(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    registry, _ = _legacy_sources(root)
    source_hash = file_sha256(registry)
    database = _destination(tmp_path / "destination.sqlite3")
    _, snapshot_root, snapshot = _snapshot(root, tmp_path / "backups")
    copied_registry = snapshot_root / "sources" / "data" / "sleeves" / "registry.sqlite3"
    with sqlite3.connect(copied_registry) as conn:
        conn.execute(
            "UPDATE sleeves SET starting_cash = ? WHERE name = ?",
            ("1.00", "bench-spy"),
        )
    try:
        with pytest.raises(RuntimeError, match="snapshot integrity"):
            execute_migration(
                database,
                snapshot_root,
                snapshot,
                code_revision=REVISION,
            )
        with database.session() as session:
            assert session.scalar(select(func.count()).select_from(MigrationRun)) == 0
    finally:
        database.dispose()
    assert file_sha256(registry) == source_hash
