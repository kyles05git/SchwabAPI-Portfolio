from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr

from schwab_trader import benchmark_scope, dashboard, digest, scheduling
from schwab_trader.agent import AgentRunner, HoldStrategy
from schwab_trader.config import Settings
from schwab_trader.evaluation import EvaluationStore
from schwab_trader.market_data import Quote
from schwab_trader.models import OrderRequest, OrderSide
from schwab_trader.paper import PaperEngine
from schwab_trader.sleeve_runs import SnapshotMismatchError
from schwab_trader.storage import factory as storage_factory
from schwab_trader.storage.contracts import (
    EvaluationRepository,
    OfficialRunRepository,
    PaperRepository,
    SleeveRepository,
)
from schwab_trader.storage.database import Database
from schwab_trader.storage.evaluation import SqlAlchemyEvaluationStore
from schwab_trader.storage.paper import SqlAlchemyPaperEngine
from schwab_trader.storage.runs import SqlAlchemySleeveRunStore
from schwab_trader.storage.sleeves import (
    AmbiguousSleeveName,
    SqlAlchemySleeveStore,
)


@pytest.fixture
def shared_sqlite(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'shared.sqlite3'}", create_schema=True)
    try:
        yield database
    finally:
        database.dispose()


def _create_sleeve(
    store: SqlAlchemySleeveStore,
    name: str,
    *,
    cohort_id: str = "",
):
    return store.create(
        name,
        strategy="buy-hold",
        universe=["SPY"],
        starting_cash=Decimal("10000.00"),
        max_positions=1,
        max_position_fraction=Decimal("1"),
        settlement_t1=True,
        cohort_id=cohort_id,
    )


def test_scoped_names_require_cohort_or_stable_id(shared_sqlite: Database) -> None:
    store = SqlAlchemySleeveStore(shared_sqlite)
    legacy = _create_sleeve(store, "bench-spy")
    official = _create_sleeve(store, "bench-spy", cohort_id="official-2026")

    with pytest.raises(AmbiguousSleeveName, match="ambiguous"):
        store.resolve("bench-spy")

    assert store.resolve("bench-spy", cohort_id="official-2026") == official
    assert store.resolve(legacy.sleeve_id) == legacy
    assert legacy.sleeve_id != official.sleeve_id
    assert legacy.original_name == official.original_name == "bench-spy"


def test_exact_decimal_and_aware_timestamp_round_trip(
    shared_sqlite: Database,
) -> None:
    store = SqlAlchemySleeveStore(shared_sqlite)
    config = _create_sleeve(store, "exact")
    resolved = store.resolve(config.sleeve_id)

    assert resolved is not None
    assert resolved.starting_cash == Decimal("10000.00")
    assert resolved.created_at.tzinfo is not None
    assert resolved.created_at.astimezone(UTC).utcoffset() is not None


def test_only_one_store_owns_an_official_session(
    shared_sqlite: Database,
) -> None:
    sleeves = SqlAlchemySleeveStore(shared_sqlite)
    _create_sleeve(sleeves, "bench-spy", cohort_id="official-2026")
    first = SqlAlchemySleeveRunStore(shared_sqlite)
    second = SqlAlchemySleeveRunStore(shared_sqlite)
    assert isinstance(sleeves, SleeveRepository)
    assert isinstance(first, OfficialRunRepository)

    with first.official_session(
        "official-2026",
        date(2026, 7, 23),
        owner_id="machine-one",
    ) as first_owned:
        with second.official_session(
            "official-2026",
            date(2026, 7, 23),
            owner_id="machine-two",
        ) as second_owned:
            assert (first_owned, second_owned) == (True, False)

    with second.official_session(
        "official-2026",
        date(2026, 7, 23),
        owner_id="machine-two",
    ) as later_owned:
        assert later_owned is True


def test_shared_run_store_persists_parent_before_member_checkpoints(
    shared_sqlite: Database,
) -> None:
    sleeves = SqlAlchemySleeveStore(shared_sqlite)
    first = _create_sleeve(sleeves, "one", cohort_id="official-2026")
    second = _create_sleeve(sleeves, "two", cohort_id="official-2026")
    store = SqlAlchemySleeveRunStore(shared_sqlite)

    run = store.ensure_run(
        cohort_id="official-2026",
        session=scheduling.session_for_date(date(2026, 7, 23)),
        expected_members=(first.sleeve_id, second.sleeve_id),
        now=datetime(2026, 7, 23, 20, 0, tzinfo=UTC),
    )

    assert run.expected_members == (first.sleeve_id, second.sleeve_id)
    assert tuple(member.sleeve_id for member in run.members) == run.expected_members


def test_shared_run_store_snapshot_replacement_matches_the_local_store(
    shared_sqlite: Database,
) -> None:
    """The ``allow_replace`` contract, executed against the real ORM implementation.

    The awaiting-data retry depends on it: a run that has executed nothing may adopt a
    fresh snapshot identity, and a run that has executed anything may not. The shared
    backend is what the writer machine actually runs, so its behaviour must match
    :class:`schwab_trader.sleeve_runs.SleeveRunStore` exactly rather than by assertion.
    """
    sleeves = SqlAlchemySleeveStore(shared_sqlite)
    member = _create_sleeve(sleeves, "bench-spy", cohort_id="official-2026")
    store = SqlAlchemySleeveRunStore(shared_sqlite)
    run = store.ensure_run(
        cohort_id="official-2026",
        session=scheduling.session_for_date(date(2026, 7, 23)),
        expected_members=(member.sleeve_id,),
        now=datetime(2026, 7, 23, 20, 0, tzinfo=UTC),
    )
    store.set_snapshot(
        run.run_id,
        snapshot_id="snapshot:first",
        quote_snapshot_id="quotes:first",
        data_snapshot_ids={"daily_bars": "bars:friday"},
    )

    # Nothing has executed: a fresh identity is adopted, as after an awaiting-data wait.
    replaced = store.set_snapshot(
        run.run_id,
        snapshot_id="snapshot:second",
        quote_snapshot_id="quotes:second",
        data_snapshot_ids={"daily_bars": "bars:monday"},
        allow_replace=True,
    )
    assert replaced.snapshot_id == "snapshot:second"
    assert replaced.quote_snapshot_id == "quotes:second"
    assert replaced.data_snapshot_ids == {"daily_bars": "bars:monday"}

    # Without the flag, a differing identity still fails closed.
    with pytest.raises(SnapshotMismatchError):
        store.set_snapshot(
            run.run_id,
            snapshot_id="snapshot:third",
            quote_snapshot_id="quotes:third",
            data_snapshot_ids={"daily_bars": "bars:tuesday"},
        )

    # And an identical identity is accepted, as on a clean restart.
    unchanged = store.set_snapshot(
        run.run_id,
        snapshot_id="snapshot:second",
        quote_snapshot_id="quotes:second",
        data_snapshot_ids={"daily_bars": "bars:monday"},
    )
    assert unchanged.snapshot_id == "snapshot:second"


def _paper_lifecycle(engine: PaperEngine) -> tuple[object, ...]:
    now = datetime(2026, 7, 23, 15, 0, tzinfo=UTC)
    request = OrderRequest(
        side=OrderSide.BUY,
        symbol="SPY",
        quantity=2,
        limit_price=Decimal("50.25"),
    )
    quote = Quote(
        symbol="SPY",
        bid=Decimal("50.00"),
        ask=Decimal("50.125"),
        mark=Decimal("50.125"),
        quote_time=now,
    )
    order = engine.place_order(request, quote, now=now)
    account = engine.account()
    position = engine.positions()[0]
    return (
        order.status,
        order.fill_price,
        account.starting_cash,
        account.cash,
        account.realized_pnl,
        position.symbol,
        position.quantity,
        position.avg_cost,
    )


def test_sqlite_and_shared_backends_obey_same_paper_contract(
    tmp_path: Path,
    shared_sqlite: Database,
) -> None:
    legacy = PaperEngine(
        tmp_path / "legacy-paper.sqlite3",
        starting_cash=Decimal("1000.00"),
    )
    store = SqlAlchemySleeveStore(shared_sqlite)
    config = store.create(
        "paper-contract",
        strategy="buy-hold",
        universe=["SPY"],
        starting_cash=Decimal("1000.00"),
        max_positions=1,
        max_position_fraction=Decimal("1"),
    )
    shared = SqlAlchemyPaperEngine(
        shared_sqlite,
        config.sleeve_id,
        starting_cash=Decimal("1000.00"),
    )

    assert isinstance(legacy, PaperRepository)
    assert isinstance(shared, PaperRepository)
    assert _paper_lifecycle(legacy) == _paper_lifecycle(shared)


def test_sqlite_and_shared_backends_return_same_evaluation_history(
    tmp_path: Path,
    shared_sqlite: Database,
) -> None:
    def missing_quote(symbol: str) -> Quote:
        raise KeyError(symbol)

    engine = PaperEngine(
        tmp_path / "evaluation-paper.sqlite3",
        starting_cash=Decimal("1000.00"),
    )
    report = AgentRunner(
        HoldStrategy([]),
        engine,
        missing_quote,
    ).run_cycle(now=datetime(2026, 7, 23, 15, 0, tzinfo=UTC))
    legacy = EvaluationStore(tmp_path / "evaluation.sqlite3")
    sleeves = SqlAlchemySleeveStore(shared_sqlite)
    config = _create_sleeve(sleeves, "evaluation-contract")
    shared = SqlAlchemyEvaluationStore(shared_sqlite, config.sleeve_id)

    assert isinstance(legacy, EvaluationRepository)
    assert isinstance(shared, EvaluationRepository)
    legacy.record_cycle(report)
    shared.record_cycle(report)

    assert legacy.recent_cycles() == shared.recent_cycles()
    assert legacy.summary() == shared.summary()
    assert legacy.equity_curve() == shared.equity_curve()


def test_dashboard_and_digest_require_unambiguous_benchmark(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'shared-dashboard.sqlite3'}"
    schema_database = Database(url, create_schema=True)
    store = SqlAlchemySleeveStore(schema_database)
    legacy = _create_sleeve(store, "bench-spy")
    official = _create_sleeve(store, "bench-spy", cohort_id="official-2026")
    settings = Settings(database_url=SecretStr(url))
    storage_factory._shared_database.cache_clear()
    try:
        # The digest still ranks every sleeve in one list, so a globally duplicated
        # benchmark name remains ambiguous there and must fail closed. Two *non-retired*
        # scopes (a legacy sleeve and a live cohort) is exactly the case the lifecycle
        # narrowing in `benchmark_scope` cannot resolve, so it refuses rather than guess.
        # The error is now raised by that module rather than by the sleeve store; both
        # are LookupErrors, and the CLI turns either into a clean failure.
        with pytest.raises(benchmark_scope.BenchmarkScopeError, match="several active cohorts"):
            digest.collect_digest_rows(settings, "bench-spy")

        # The dashboard now ranks inside a comparability group, so the same name is
        # unambiguous *within* each scope: the legacy row and the cohort row each
        # resolve to their own benchmark instead of refusing to render.
        rows, _ = dashboard.collect_sleeves(settings, "bench-spy")
        by_id = {row.sleeve_id: row for row in rows}
        assert by_id[legacy.identity].scope is not dashboard.SleeveScope.OFFICIAL_COHORT
        assert by_id[official.identity].scope is dashboard.SleeveScope.OFFICIAL_COHORT
        assert by_id[legacy.identity].cohort_id is None
        assert by_id[official.identity].cohort_id == "official-2026"
        # Both are their own group's benchmark, so neither reports an excess against
        # the other, and the bare duplicate name is never used to join them.
        assert all(row.is_benchmark for row in rows)
        assert all(row.excess_pct is None for row in rows)
        assert {row.name for row in rows} == {"bench-spy"}
        assert len({row.sleeve_id for row in rows}) == 2

        # A stable identity still names one specific sleeve.
        rows, _ = dashboard.collect_sleeves(settings, legacy.identity)
        by_id = {row.sleeve_id: row for row in rows}
        assert by_id[legacy.identity].is_benchmark is True
        assert by_id[official.identity].is_benchmark is False

        digest_rows = digest.collect_digest_rows(settings, legacy.sleeve_id)
        assert {row.name for row in digest_rows} == {
            "bench-spy [legacy]",
            "bench-spy [official-2026]",
        }
    finally:
        shared_database = storage_factory.database(settings)
        if shared_database is not None:
            shared_database.dispose()
        storage_factory._shared_database.cache_clear()
        schema_database.dispose()
