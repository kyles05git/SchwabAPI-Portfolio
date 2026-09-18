"""Offline tests for the idempotent Paper Sleeves First bootstrap."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from schwab_trader import data_contracts, data_readiness, strategy_registry, universes
from schwab_trader.approval import ApprovalStore
from schwab_trader.client import SchwabClient
from schwab_trader.orders import cancel_order, replace_order, submit_order
from schwab_trader.reconciliation import ReconciliationStore, reconcile_account
from schwab_trader.sec_store import SecStore
from schwab_trader.sleeve_runs import SnapshotCoverage
from schwab_trader.sleeves import SleeveStore
from schwab_trader.storage.database import Database
from schwab_trader.storage.sleeves import SqlAlchemySleeveStore

_SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "bootstrap_paper_cohort.py"
_SPEC = importlib.util.spec_from_file_location("bootstrap_paper_cohort_script", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
bootstrap = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = bootstrap
_SPEC.loader.exec_module(bootstrap)

_START = date(2026, 7, 24)
_NOW = datetime(2026, 7, 22, 16, 0, tzinfo=UTC)
_NOW_ET = datetime(2026, 7, 22, 12, 0)


def _probe(*, ready: bool) -> data_readiness.SourceProbe:
    large_cap = tuple(universes.get_preset("large-cap") or ())
    covered = large_cap if ready else large_cap[:3]
    return data_readiness.SourceProbe.of(
        SnapshotCoverage(
            provenance=data_contracts.Provenance(
                source="fake-edgar",
                snapshot_id="fake-edgar:ready" if ready else "fake-edgar:partial",
                retrieved_at=_NOW,
                as_of=_NOW,
                available_at=_NOW,
                timing=data_contracts.TimingPolicy.POINT_IN_TIME,
                vintage_safe=True,
            ),
            keys=frozenset(covered),
        )
    )


def _plan(
    tmp_path: Path,
    *,
    edgar_ready: bool,
    start_session: date = _START,
    cohort_id: str | None = None,
) -> bootstrap.BootstrapPlan:
    return bootstrap.build_bootstrap_plan(
        start_session=start_session,
        sec_db_path=tmp_path / "sec.sqlite3",
        cohort_id=cohort_id,
        now=_NOW,
        now_et=_NOW_ET,
        edgar_probe=_probe(ready=edgar_ready),
    )


def _fake_edgar_db(path: Path, tickers: tuple[str, ...]) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sec_facts (ticker TEXT NOT NULL, filed TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO sec_facts (ticker, filed) VALUES (?, ?)",
            [(ticker, "2026-07-01") for ticker in tickers],
        )


def test_dry_run_prints_complete_preview_and_performs_zero_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sleeves_dir = tmp_path / "sleeves"
    sec_db = tmp_path / "missing-sec.sqlite3"
    monkeypatch.setattr(
        bootstrap.market_calendar,
        "eastern_now",
        lambda: _NOW_ET,
    )

    result = bootstrap.main(
        [
            "preview",
            "--start-session",
            _START.isoformat(),
            "--sleeves-dir",
            str(sleeves_dir),
            "--sec-db",
            str(sec_db),
        ]
    )

    output = capsys.readouterr().out
    assert result == 0
    assert '"persistent_writes": "none"' in output
    assert "not divided from, reserved in, or linked to the real account" in output
    assert '"configuration_hash"' in output
    assert all(name in output for name in bootstrap.CORE_MEMBER_NAMES)
    assert bootstrap.CONDITIONAL_MEMBER_NAME in output
    assert "will be omitted" in output
    assert "DRY RUN COMPLETE: zero persistent writes were performed." in output
    assert not sleeves_dir.exists()
    assert not sec_db.exists()


def test_readonly_edgar_preview_does_not_change_fake_database(tmp_path: Path) -> None:
    sec_db = tmp_path / "sec.sqlite3"
    tickers = tuple(universes.get_preset("large-cap") or ())
    _fake_edgar_db(sec_db, tickers)
    before = sec_db.read_bytes()

    plan = bootstrap.build_bootstrap_plan(
        start_session=_START,
        sec_db_path=sec_db,
        now=_NOW,
        now_et=_NOW_ET,
    )

    assert plan.readiness.ready is True
    assert plan.manifest.conditional_edgar.status is bootstrap.ConditionalStatus.INCLUDED
    assert sec_db.read_bytes() == before


def test_first_bootstrap_creates_core_members_with_parallel_cash(tmp_path: Path) -> None:
    sleeves_dir = tmp_path / "sleeves"
    plan = _plan(tmp_path, edgar_ready=False)

    result = bootstrap.apply_bootstrap(plan, sleeves_dir=sleeves_dir)
    store = SleeveStore(sleeves_dir)
    configs = store.list()

    assert result.created == bootstrap.CORE_MEMBER_NAMES
    assert [config.name for config in configs] == list(bootstrap.CORE_MEMBER_NAMES)
    assert {config.starting_cash for config in configs} == {Decimal("10000.00")}
    assert all(config.settlement_t1 for config in configs)
    assert {config.leverage for config in configs} == {Decimal("1")}
    assert all(config.cohort_id == plan.manifest.cohort.cohort_id for config in configs)
    assert not any(store.eval_path(config.name).exists() for config in configs)


def test_rerun_is_idempotent_and_creates_no_duplicates(tmp_path: Path) -> None:
    sleeves_dir = tmp_path / "sleeves"
    plan = _plan(tmp_path, edgar_ready=True)

    first = bootstrap.apply_bootstrap(plan, sleeves_dir=sleeves_dir)
    manifest_before = first.manifest_path.read_bytes()
    second = bootstrap.apply_bootstrap(plan, sleeves_dir=sleeves_dir)
    store = SleeveStore(sleeves_dir)

    assert len(first.created) == 7
    assert second.created == ()
    assert second.existing == tuple(spec.name for spec in plan.selected_specs)
    assert len(store.list()) == 7
    assert first.manifest_path.read_bytes() == manifest_before


def test_common_benchmark_schedules_start_and_hashes_persist_and_reconstruct(
    tmp_path: Path,
) -> None:
    sleeves_dir = tmp_path / "sleeves"
    plan = _plan(tmp_path, edgar_ready=True)
    result = bootstrap.apply_bootstrap(plan, sleeves_dir=sleeves_dir)
    stored = bootstrap.load_manifest(sleeves_dir, plan.manifest.cohort.cohort_id)
    assert stored is not None

    assert stored.cohort.start_session == _START
    assert stored.cohort.starting_cash_per_sleeve == Decimal("10000.00")
    assert stored.cohort.settlement_model == "T+1"
    assert stored.cohort.leverage == Decimal("1")
    assert stored.cohort.benchmark_sleeve == "bench-spy"
    assert stored.cohort.decision_schedule == bootstrap.DECISION_SCHEDULE
    assert stored.valuation_schedule == bootstrap.VALUATION_SCHEDULE
    assert stored.cadence == bootstrap.CADENCE

    store = SleeveStore(sleeves_dir)
    edgar_store = SecStore(tmp_path / "offline-sec.sqlite3")
    resources = strategy_registry.StrategyResources(
        history={},
        benchmark_history=[],
        store=edgar_store,
    )
    for config in store.list():
        assert config.definition is not None
        assert config.configuration_hash == config.definition.configuration_hash
        assert stored.configuration_hashes[config.name] == config.configuration_hash
        assert config.definition.benchmark_symbol_or_sleeve == "bench-spy"
        assert config.definition.decision_frequency == "daily"
        assert config.definition.decision_time.isoformat() == "16:00:00"
        rebuilt = strategy_registry.reconstruct(
            config.definition,
            config.universe,
            resources=resources,
        )
        assert rebuilt is not None

    inspected = bootstrap.inspect_cohort(
        sleeves_dir=sleeves_dir,
        cohort_id=stored.cohort.cohort_id,
    )
    assert inspected["manifest_path"] == str(result.manifest_path)
    assert all(record["definition"] for record in inspected["stored_sleeves"])


def test_conditional_edgar_member_is_created_only_when_readiness_passes(
    tmp_path: Path,
) -> None:
    ready_dir = tmp_path / "ready"
    ready_plan = _plan(tmp_path, edgar_ready=True, cohort_id="ready-cohort")
    bootstrap.apply_bootstrap(ready_plan, sleeves_dir=ready_dir)

    omitted_dir = tmp_path / "omitted"
    omitted_plan = _plan(tmp_path, edgar_ready=False, cohort_id="omitted-cohort")
    bootstrap.apply_bootstrap(omitted_plan, sleeves_dir=omitted_dir)

    assert SleeveStore(ready_dir).get(bootstrap.CONDITIONAL_MEMBER_NAME) is not None
    assert ready_plan.manifest.conditional_edgar.status is bootstrap.ConditionalStatus.INCLUDED
    assert SleeveStore(omitted_dir).get(bootstrap.CONDITIONAL_MEMBER_NAME) is None
    assert omitted_plan.manifest.conditional_edgar.status is bootstrap.ConditionalStatus.OMITTED
    assert "No price-only substitute" in omitted_plan.manifest.conditional_edgar.reason


def test_require_edgar_blocks_all_creation_when_readiness_fails(tmp_path: Path) -> None:
    sleeves_dir = tmp_path / "sleeves"
    plan = _plan(tmp_path, edgar_ready=False)

    with pytest.raises(bootstrap.BootstrapConflictError, match="readiness failed"):
        bootstrap.apply_bootstrap(
            plan,
            sleeves_dir=sleeves_dir,
            require_edgar=True,
        )

    assert not sleeves_dir.exists()


def test_conflicting_immutable_configuration_fails_without_mutation(
    tmp_path: Path,
) -> None:
    sleeves_dir = tmp_path / "sleeves"
    original = _plan(
        tmp_path,
        edgar_ready=False,
        start_session=_START,
        cohort_id="immutable-cohort",
    )
    bootstrap.apply_bootstrap(original, sleeves_dir=sleeves_dir)
    before = [config.model_dump(mode="json") for config in SleeveStore(sleeves_dir).list()]
    manifest_before = bootstrap.manifest_path(
        sleeves_dir,
        "immutable-cohort",
    ).read_bytes()
    changed = _plan(
        tmp_path,
        edgar_ready=False,
        start_session=date(2026, 7, 27),
        cohort_id="immutable-cohort",
    )

    with pytest.raises(bootstrap.BootstrapConflictError, match="conflicting immutable"):
        bootstrap.apply_bootstrap(changed, sleeves_dir=sleeves_dir)

    after = [config.model_dump(mode="json") for config in SleeveStore(sleeves_dir).list()]
    assert after == before
    assert bootstrap.manifest_path(sleeves_dir, "immutable-cohort").read_bytes() == manifest_before


def test_bootstrap_creates_no_evaluations_history_or_live_path_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def forbidden(name: str):
        def fail(*args: object, **kwargs: object) -> None:
            calls.append(name)
            raise AssertionError(f"forbidden live path called: {name}")

        return fail

    monkeypatch.setattr(SchwabClient, "__init__", forbidden("broker-client"))
    monkeypatch.setattr(
        sys.modules[submit_order.__module__],
        "submit_order",
        forbidden("submit-order"),
    )
    monkeypatch.setattr(
        sys.modules[replace_order.__module__],
        "replace_order",
        forbidden("replace-order"),
    )
    monkeypatch.setattr(
        sys.modules[cancel_order.__module__],
        "cancel_order",
        forbidden("cancel-order"),
    )
    monkeypatch.setattr(ApprovalStore, "__init__", forbidden("approval"))
    monkeypatch.setattr(ReconciliationStore, "__init__", forbidden("reconciliation-store"))
    monkeypatch.setattr(
        sys.modules[reconcile_account.__module__],
        "reconcile_account",
        forbidden("reconcile-account"),
    )

    sleeves_dir = tmp_path / "sleeves"
    plan = _plan(tmp_path, edgar_ready=False)
    bootstrap.apply_bootstrap(plan, sleeves_dir=sleeves_dir)
    store = SleeveStore(sleeves_dir)

    assert calls == []
    assert not (sleeves_dir / "runs.sqlite3").exists()
    for config in store.list():
        assert not store.eval_path(config.name).exists()
        with sqlite3.connect(store.paper_path(config.name)) as conn:
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            assert "orders" not in tables


@pytest.mark.parametrize(
    "start_session, message",
    [
        (date(2026, 7, 18), "not an XNYS trading session"),
        (date(2026, 7, 21), "must not be historical"),
    ],
)
def test_start_session_must_be_explicit_valid_and_not_historical(
    tmp_path: Path,
    start_session: date,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _plan(tmp_path, edgar_ready=False, start_session=start_session)


def test_inspect_cli_reports_exact_stored_hashes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sleeves_dir = tmp_path / "sleeves"
    plan = _plan(tmp_path, edgar_ready=False)
    bootstrap.apply_bootstrap(plan, sleeves_dir=sleeves_dir)

    result = bootstrap.main(
        [
            "inspect",
            "--cohort-id",
            plan.manifest.cohort.cohort_id,
            "--sleeves-dir",
            str(sleeves_dir),
            "--sec-db",
            str(tmp_path / "unused.sqlite3"),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["manifest"]["configuration_hashes"]
    assert all(item["definition"] for item in payload["stored_sleeves"])


def test_shared_bootstrap_scopes_names_and_persists_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(
        f"sqlite:///{tmp_path / 'shared.sqlite3'}",
        create_schema=True,
    )
    store = SqlAlchemySleeveStore(database)
    legacy = store.create(
        "bench-spy",
        strategy="buy-hold",
        universe=["SPY"],
        starting_cash=Decimal("5000.00"),
        max_positions=1,
        max_position_fraction=Decimal("1"),
    )
    monkeypatch.setattr(
        bootstrap.storage_factory,
        "sleeve_store",
        lambda settings: store,
    )
    plan = _plan(
        tmp_path,
        edgar_ready=False,
        cohort_id="official-shared",
    )
    try:
        result = bootstrap.apply_bootstrap(
            plan,
            sleeves_dir=tmp_path / "unused-sleeves",
        )
        official = store.resolve("bench-spy", cohort_id="official-shared")
        inspected = bootstrap.inspect_cohort(
            sleeves_dir=tmp_path / "unused-sleeves",
            cohort_id="official-shared",
        )

        assert result.created
        assert official is not None
        assert official.sleeve_id != legacy.sleeve_id
        assert store.resolve(legacy.sleeve_id) == legacy
        assert inspected["manifest"]["cohort"]["cohort_id"] == "official-shared"
        assert len(inspected["stored_sleeves"]) == len(
            plan.manifest.cohort.member_sleeves
        )
    finally:
        database.dispose()
