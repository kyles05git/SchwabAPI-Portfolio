from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from schwab_trader import (
    execution_timing,
    market_bar_evidence,
    market_calendar,
    market_data,
    scheduling,
)
from schwab_trader.evaluation import ObservationStatus, OfficialDailyObservation
from schwab_trader.sleeve_runs import SnapshotMismatchError
from schwab_trader.storage.database import Database
from schwab_trader.storage.evaluation import SqlAlchemyEvaluationStore
from schwab_trader.storage.market_data import SqlAlchemyMarketDataEvidenceStore
from schwab_trader.storage.runs import SqlAlchemySleeveRunStore
from schwab_trader.storage.sleeves import SqlAlchemySleeveStore

TEST_DATABASE_URL = os.environ.get("SCHWAB_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="SCHWAB_TEST_DATABASE_URL is not configured",
)


@pytest.fixture
def postgres_database():
    parsed = make_url(TEST_DATABASE_URL)
    if "test" not in (parsed.database or "").casefold():
        pytest.fail("SCHWAB_TEST_DATABASE_URL must identify a dedicated test database")
    if parsed.get_backend_name() != "postgresql":
        pytest.fail("SCHWAB_TEST_DATABASE_URL must use PostgreSQL")
    schema = f"schwab_test_{uuid.uuid4().hex}"
    admin_url = parsed.set(drivername="postgresql+psycopg")
    admin_engine = create_engine(admin_url, hide_parameters=True)
    with admin_engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    scoped_url = admin_url.update_query_dict({"options": f"-csearch_path={schema}"})
    database = Database(scoped_url.render_as_string(hide_password=False), create_schema=True)
    try:
        yield database
    finally:
        database.dispose()
        with admin_engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin_engine.dispose()


def test_postgresql_scoped_identity_and_advisory_session_lock(
    postgres_database: Database,
) -> None:
    sleeves = SqlAlchemySleeveStore(postgres_database)
    config = sleeves.create(
        "bench-spy",
        strategy="buy-hold",
        universe=["SPY"],
        starting_cash=Decimal("10000.00"),
        max_positions=1,
        max_position_fraction=Decimal("1"),
        cohort_id="postgres-contract",
    )
    assert sleeves.resolve(config.sleeve_id) == config

    first = SqlAlchemySleeveRunStore(postgres_database)
    second = SqlAlchemySleeveRunStore(postgres_database)
    with first.official_session(
        "postgres-contract",
        date(2026, 7, 23),
        owner_id="machine-one",
    ) as first_owned:
        with second.official_session(
            "postgres-contract",
            date(2026, 7, 23),
            owner_id="machine-two",
        ) as second_owned:
            assert first_owned is True
            assert second_owned is False


def test_postgresql_execution_methodology_and_observation_lineage_round_trip(
    postgres_database: Database,
) -> None:
    sleeves = SqlAlchemySleeveStore(postgres_database)
    config = sleeves.create(
        "t1-open",
        strategy="hold",
        universe=[],
        starting_cash=Decimal("10000"),
        max_positions=0,
        max_position_fraction=Decimal("0"),
        cohort_id="postgres-t1-open",
        execution_methodology=execution_timing.NEXT_OPEN_METHODOLOGY_KEY,
    )
    resolved = sleeves.resolve(config.sleeve_id)
    assert resolved is not None
    assert resolved.execution_methodology == execution_timing.NEXT_OPEN_METHODOLOGY_KEY

    signal_time = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)
    execution_time = datetime(2026, 7, 28, 13, 30, tzinfo=UTC)
    run = SqlAlchemySleeveRunStore(postgres_database).ensure_run(
        cohort_id=config.cohort_id,
        session=scheduling.session_for_date(date(2026, 7, 27)),
        expected_members=(config.sleeve_id,),
        now=signal_time,
    )
    observation = OfficialDailyObservation(
        cohort_id=config.cohort_id,
        run_id=run.run_id,
        sleeve_id=config.sleeve_id,
        strategy=config.strategy,
        strategy_hash=config.configuration_hash,
        session_date=date(2026, 7, 27),
        decision_time=signal_time,
        valuation_time=execution_time,
        execution_methodology=execution_timing.NEXT_OPEN_METHODOLOGY_KEY,
        signal_session_date=date(2026, 7, 27),
        execution_session_date=date(2026, 7, 28),
        signal_time=signal_time,
        execution_time=execution_time,
        status=ObservationStatus.OFFICIAL,
        total_value=Decimal("10000"),
        return_pct=Decimal("0"),
        snapshot_ids={
            "execution_methodology": (
                execution_timing.SIGNAL_T_CLOSE_EXECUTE_T1_OPEN_V1.methodology_hash
            )
        },
    )
    evaluations = SqlAlchemyEvaluationStore(postgres_database, config.sleeve_id)
    evaluations.record_official_observation(observation)
    assert evaluations.official_observations() == [observation]


def test_postgresql_snapshot_replacement_matches_the_local_store(
    postgres_database: Database,
) -> None:
    """`allow_replace` must behave identically in both backends.

    The awaiting-data retry depends on it: a run that has executed nothing may adopt a
    fresh snapshot identity, and a run that has executed anything may not. A divergence
    here would make the shared writer behave differently from every offline test.
    """
    sleeves = SqlAlchemySleeveStore(postgres_database)
    sleeves.create(
        "bench-spy",
        strategy="buy-hold",
        universe=["SPY"],
        starting_cash=Decimal("10000.00"),
        max_positions=1,
        max_position_fraction=Decimal("1"),
        cohort_id="postgres-snapshot",
    )
    runs = SqlAlchemySleeveRunStore(postgres_database)
    run = runs.ensure_run(
        cohort_id="postgres-snapshot",
        session=scheduling.session_for_date(date(2026, 7, 23)),
        expected_members=["bench-spy"],
    )
    runs.set_snapshot(
        run.run_id,
        snapshot_id="snapshot:first",
        quote_snapshot_id="quotes:first",
        data_snapshot_ids={"daily_bars": "bars:friday"},
    )

    # Nothing has executed: a fresh identity is adopted, as after an awaiting-data wait.
    replaced = runs.set_snapshot(
        run.run_id,
        snapshot_id="snapshot:second",
        quote_snapshot_id="quotes:second",
        data_snapshot_ids={"daily_bars": "bars:monday"},
        allow_replace=True,
    )
    assert replaced.snapshot_id == "snapshot:second"
    assert replaced.data_snapshot_ids == {"daily_bars": "bars:monday"}

    # Without the flag, a differing identity still fails closed.
    with pytest.raises(SnapshotMismatchError):
        runs.set_snapshot(
            run.run_id,
            snapshot_id="snapshot:third",
            quote_snapshot_id="quotes:third",
            data_snapshot_ids={"daily_bars": "bars:tuesday"},
        )

    # And an identical identity is accepted, as on a clean restart.
    unchanged = runs.set_snapshot(
        run.run_id,
        snapshot_id="snapshot:second",
        quote_snapshot_id="quotes:second",
        data_snapshot_ids={"daily_bars": "bars:monday"},
    )
    assert unchanged.snapshot_id == "snapshot:second"


def test_postgresql_market_data_evidence_matches_the_local_contract(
    postgres_database: Database,
) -> None:
    session_date = date(2026, 7, 28)
    bars = [
        market_data.Candle(
            symbol="SPY",
            date=stamp,
            open=Decimal(100 + index),
            high=Decimal(102 + index),
            low=Decimal(99 + index),
            close=Decimal(101 + index),
            volume=index + 1,
            source=market_data.SCHWAB_REGULAR_SESSION_SOURCE,
        )
        for index, stamp in enumerate(market_calendar.session_interval_starts_utc(session_date))
    ]
    result = market_bar_evidence.validate_regular_session(
        "SPY",
        session_date,
        bars,
        retrieved_at=(market_calendar.session_bounds_utc(session_date)[1] + timedelta(minutes=1)),
    )
    assert result.evidence is not None
    store = SqlAlchemyMarketDataEvidenceStore(postgres_database)

    persisted = store.save(result.evidence)

    assert persisted == result.evidence
    assert store.reproduce(persisted.dataset_id) == persisted
    assert len(store.for_session("spy", session_date)[0].constituents) == 78
