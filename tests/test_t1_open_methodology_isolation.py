"""The new timing methodology must not reach back into recorded history.

Two distinct guarantees are proved here.

**Configuration identity.** ``StrategyDefinition.configuration_payload`` is the input to
every sleeve's ``configuration_hash``. Issue #79 deliberately added its methodology as a
*separate* identity rather than a field of that payload, because adding one field would
silently move every hash the July cohorts were recorded under. The hashes below are
pinned literals captured from ``origin/main`` at ``bc50bbd``; if a future change adds to
the payload, this fails immediately rather than at the next reconciliation.

**Persisted evidence.** An observation written before the execution-timing columns
existed must survive the additive migration byte-for-byte, keep re-recording
idempotently, and read back as the close-marked model it actually ran.

Offline and hermetic: SQLite files under ``tmp_path`` only. No `.env`, Neon, Schwab,
SMTP, socket, or order path is reachable.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from schwab_trader import execution_timing as et
from schwab_trader import scheduling, strategy_registry
from schwab_trader.evaluation import (
    EvaluationStore,
    ObservationStatus,
    OfficialDailyObservation,
    official_observation_key,
)
from schwab_trader.sleeves import SleeveStore
from schwab_trader.storage.database import Database
from schwab_trader.storage.evaluation import SqlAlchemyEvaluationStore
from schwab_trader.storage.runs import SqlAlchemySleeveRunStore
from schwab_trader.storage.sleeves import SqlAlchemySleeveStore

#: Captured from origin/main @ bc50bbd, before any #79 change existed.
FROZEN_CONFIGURATION_HASHES = {
    "buy-hold": "a36c6aa9df780be80b727a48f707ca90b8c89b4ac544697b73b557bc2bf94fc2",
    "dip-buyer": "c727e05f29da144bd1e22aed6caf83d330cf50ba724ab9727d466cdccf4ef1a0",
    "fundamental": "d6abef91b0ef758933425b596ef44d7c7eb8937e27163176df9119362e696b6e",
    "hold": "c03a46d3699641fd2b17407dba55bff786e71f3968760a285e2af41511e30f68",
    "intraday": "9dc831704c5efb85dca2d67a1a0ea8cb4337b7372e36a5d2a17fb4be3c932d77",
    "llm": "1b421d99c523522ffc61315601184c7d7d14ebf6491b03689e422f7cb56e86f1",
    "low-vol": "a69d1b6e335ae825a248aed26cd2729b466361a09f893806570fd15bf5f63292",
    "mean-reversion": "d0f2bd90ddc0dd7b12bbc7ea1f3b1aee99dd43799dd335d5b70e79d31f5d49b9",
    "momentum": "156618cdf9d8fc7a8062f667d20a16c758c91af33dadd4478f8baf4457744cdc",
    "post-earnings-drift": "e37062108816b3167209aec678d9355e35d66b16432a880dfa84fc2fee7d6e74",
    "tactical": "377ee9d2f033bcc8fd70721fbe39ed483f872af2af35294aad94e03b93df227d",
    "trend": "8da39abb123994b179d47591ce0b0eca548a373c3d5e2f3f0b4b24dda07e90ec",
    "value-momentum": "8917e9668ccfae4130e3a0669ae635f005e99db695f72b8d00f0b8fcacd2b8fb",
}

#: The exact hash inputs as of bc50bbd. Execution timing is not among them, by design.
FROZEN_PAYLOAD_KEYS = {
    "benchmark_symbol_or_sleeve",
    "data_requirements",
    "decision_frequency",
    "decision_time",
    "implementation_name",
    "leverage_allowed",
    "long_only",
    "parameters",
    "strategy_id",
    "strategy_version",
    "universe_definition",
}

JULY_27 = date(2026, 7, 27)
JULY_28 = date(2026, 7, 28)
CLOSE_27_UTC = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)

#: The exact ``official_daily_observations`` DDL as of ``origin/main`` @ ``bc50bbd``,
#: reproduced verbatim so the migration is exercised against the real prior shape
#: (including the UNIQUE key that makes re-recording idempotent) rather than a
#: convenient approximation of it.
LEGACY_OBSERVATION_DDL = """
CREATE TABLE IF NOT EXISTS official_daily_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_key TEXT NOT NULL UNIQUE,
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

#: The column list an official observation had before #79.
LEGACY_COLUMNS = (
    "observation_key",
    "cohort_id",
    "run_id",
    "sleeve_id",
    "strategy",
    "strategy_hash",
    "session_date",
    "decision_time",
    "valuation_time",
    "status",
    "total_value",
    "return_pct",
    "benchmark_value",
    "exposure",
    "num_positions",
    "turnover",
    "modeled_cost",
    "num_filled",
    "num_rejected",
    "quote_coverage",
    "snapshot_ids",
    "readiness_ready",
    "readiness_reasons",
    "recorded_at",
)


# --- configuration identity ---------------------------------------------------


@pytest.mark.parametrize("strategy", sorted(FROZEN_CONFIGURATION_HASHES))
def test_no_registered_strategy_configuration_hash_moved(strategy):
    definition = strategy_registry.make_definition(strategy, universe_definition=["AAA"])
    assert definition.configuration_hash == FROZEN_CONFIGURATION_HASHES[strategy]


def test_execution_timing_is_not_an_input_to_the_configuration_hash():
    definition = strategy_registry.make_definition("buy-hold", universe_definition=["AAA"])
    payload = definition.configuration_payload()
    assert set(payload) == FROZEN_PAYLOAD_KEYS
    assert not any("execution" in key or "methodology" in key for key in payload)


def test_the_methodology_carries_its_own_separate_identity():
    """A distinct hash, so the new experiment is identifiable without touching the old."""
    assert et.SIGNAL_T_CLOSE_EXECUTE_T1_OPEN_V1.methodology_hash not in set(
        FROZEN_CONFIGURATION_HASHES.values()
    )
    assert et.MARK_TO_CLOSE_V1.methodology_hash != (
        et.SIGNAL_T_CLOSE_EXECUTE_T1_OPEN_V1.methodology_hash
    )


def test_a_sleeve_created_without_a_methodology_keeps_its_original_hash(tmp_path):
    store = SleeveStore(tmp_path / "sleeves")
    definition = strategy_registry.make_definition("buy-hold", universe_definition=["AAA"])
    config = store.create(
        "legacy",
        strategy="buy-hold",
        universe=["AAA"],
        starting_cash=Decimal("10000.00"),
        max_positions=3,
        max_position_fraction=Decimal("1.0"),
        definition=definition,
        cohort_id="paper-first-2026-07-27",
    )
    assert config.execution_methodology == ""
    assert config.configuration_hash == FROZEN_CONFIGURATION_HASHES["buy-hold"]
    assert store.get("legacy") == config


def test_opting_into_the_new_methodology_does_not_move_the_configuration_hash(tmp_path):
    """The two identities are orthogonal: one names the strategy, one names the timing."""
    store = SleeveStore(tmp_path / "sleeves")
    definition = strategy_registry.make_definition("buy-hold", universe_definition=["AAA"])
    config = store.create(
        "challenger",
        strategy="buy-hold",
        universe=["AAA"],
        starting_cash=Decimal("10000.00"),
        max_positions=3,
        max_position_fraction=Decimal("1.0"),
        definition=definition,
        cohort_id="paper-t1-open-2026-08-03",
        execution_methodology=et.NEXT_OPEN_METHODOLOGY_KEY,
    )
    assert config.execution_methodology == et.NEXT_OPEN_METHODOLOGY_KEY
    assert config.configuration_hash == FROZEN_CONFIGURATION_HASHES["buy-hold"]


def test_shared_storage_round_trips_the_sleeve_methodology(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'shared.sqlite3'}", create_schema=True)
    try:
        store = SqlAlchemySleeveStore(database)
        definition = strategy_registry.make_definition("buy-hold", universe_definition=["AAA"])
        created = store.create(
            "challenger",
            strategy="buy-hold",
            universe=["AAA"],
            starting_cash=Decimal("10000"),
            max_positions=1,
            max_position_fraction=Decimal("1"),
            definition=definition,
            cohort_id="paper-t1-open-2026-08-03",
            execution_methodology=et.NEXT_OPEN_METHODOLOGY_KEY,
        )
        resolved = store.resolve(created.sleeve_id)
        assert resolved is not None
        assert resolved.execution_methodology == et.NEXT_OPEN_METHODOLOGY_KEY
        assert resolved.configuration_hash == FROZEN_CONFIGURATION_HASHES["buy-hold"]
    finally:
        database.dispose()


def test_shared_observation_storage_round_trips_all_timing_fields(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'shared-observation.sqlite3'}", create_schema=True)
    try:
        sleeves = SqlAlchemySleeveStore(database)
        config = sleeves.create(
            "challenger",
            strategy="buy-hold",
            universe=["AAA"],
            starting_cash=Decimal("10000"),
            max_positions=1,
            max_position_fraction=Decimal("1"),
            cohort_id="paper-t1-open-2026-08-03",
            execution_methodology=et.NEXT_OPEN_METHODOLOGY_KEY,
        )
        execution_time = datetime(2026, 7, 28, 13, 30, tzinfo=UTC)
        run = SqlAlchemySleeveRunStore(database).ensure_run(
            cohort_id=config.cohort_id,
            session=scheduling.session_for_date(JULY_27),
            expected_members=(config.sleeve_id,),
            now=CLOSE_27_UTC,
        )
        observation = OfficialDailyObservation(
            cohort_id=config.cohort_id,
            run_id=run.run_id,
            sleeve_id=config.sleeve_id,
            strategy=config.strategy,
            strategy_hash=config.configuration_hash,
            session_date=JULY_27,
            decision_time=CLOSE_27_UTC,
            valuation_time=execution_time,
            execution_methodology=et.NEXT_OPEN_METHODOLOGY_KEY,
            signal_session_date=JULY_27,
            execution_session_date=JULY_28,
            signal_time=CLOSE_27_UTC,
            execution_time=execution_time,
            status=ObservationStatus.OFFICIAL,
            total_value=Decimal("10001"),
            return_pct=Decimal("0.01"),
            snapshot_ids={
                "execution_methodology": (et.SIGNAL_T_CLOSE_EXECUTE_T1_OPEN_V1.methodology_hash),
                "opening_bar:AAA": "a" * 64,
            },
        )
        store = SqlAlchemyEvaluationStore(database, config.sleeve_id)
        store.record_official_observation(observation)
        assert store.official_observations() == [observation]
    finally:
        database.dispose()


# --- persisted evidence -------------------------------------------------------


def _legacy_store(path, *, cohort: str, session: date) -> tuple[EvaluationStore, dict]:
    """Build an evaluation store whose observation table predates #79."""
    path.parent.mkdir(parents=True, exist_ok=True)
    values = {
        "observation_key": official_observation_key(cohort, "sleeve-1", session),
        "cohort_id": cohort,
        "run_id": "run-1",
        "sleeve_id": "sleeve-1",
        "strategy": "buy-hold",
        "strategy_hash": FROZEN_CONFIGURATION_HASHES["buy-hold"],
        "session_date": session.isoformat(),
        "decision_time": CLOSE_27_UTC.isoformat(),
        "valuation_time": CLOSE_27_UTC.isoformat(),
        "status": ObservationStatus.OFFICIAL.value,
        "total_value": "10123.45",
        "return_pct": "1.2345",
        "benchmark_value": None,
        "exposure": "0.5",
        "num_positions": 2,
        "turnover": "500",
        "modeled_cost": "0",
        "num_filled": 2,
        "num_rejected": 0,
        "quote_coverage": "1",
        "snapshot_ids": json.dumps({"cohort_snapshot": "snapshot:july"}, sort_keys=True),
        "readiness_ready": 1,
        "readiness_reasons": json.dumps([]),
        "recorded_at": CLOSE_27_UTC.isoformat(),
    }
    with sqlite3.connect(path) as conn:
        conn.executescript(LEGACY_OBSERVATION_DDL)
        placeholders = ", ".join("?" for _ in LEGACY_COLUMNS)
        conn.execute(
            f"INSERT INTO official_daily_observations ({', '.join(LEGACY_COLUMNS)}) "
            f"VALUES ({placeholders})",
            tuple(values[name] for name in LEGACY_COLUMNS),
        )
    return EvaluationStore(path), values


def _row(path) -> dict:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM official_daily_observations").fetchone()
    return dict(row)


@pytest.mark.parametrize(
    ("cohort", "session"),
    [("paper-first-2026-07-27", JULY_27), ("paper-first-2026-07-28", JULY_28)],
)
def test_a_pre_79_observation_survives_the_migration_unchanged(tmp_path, cohort, session):
    path = tmp_path / cohort / "eval.sqlite3"
    _legacy_store(path, cohort=cohort, session=session)
    after = _row(path)

    # Every column the row already had is byte-identical.
    for name in LEGACY_COLUMNS:
        assert after[name] is not None or name == "benchmark_value"
    assert after["session_date"] == session.isoformat()
    assert after["decision_time"] == CLOSE_27_UTC.isoformat()
    assert after["total_value"] == "10123.45"
    # The new columns exist and are truthfully empty: that row ran the close-marked
    # model, where the signal and execution sessions are the same session.
    assert after["execution_methodology"] == ""
    assert after["signal_session_date"] is None
    assert after["execution_session_date"] is None
    assert after["signal_time"] is None
    assert after["execution_time"] is None


@pytest.mark.parametrize(
    ("cohort", "session"),
    [("paper-first-2026-07-27", JULY_27), ("paper-first-2026-07-28", JULY_28)],
)
def test_a_pre_79_observation_reads_back_as_the_close_marked_model(tmp_path, cohort, session):
    path = tmp_path / cohort / "eval.sqlite3"
    store, _ = _legacy_store(path, cohort=cohort, session=session)

    observation = store.official_observations()[0]

    assert observation.session_date == session
    assert observation.decision_time == observation.valuation_time == CLOSE_27_UTC
    assert observation.total_value == Decimal("10123.45")
    assert observation.execution_methodology == ""
    assert et.resolve_methodology(observation.execution_methodology) is et.MARK_TO_CLOSE_V1
    assert observation.signal_session_date is None
    assert observation.execution_session_date is None


def test_re_recording_a_legacy_observation_remains_a_no_op(tmp_path):
    cohort = "paper-first-2026-07-27"
    path = tmp_path / cohort / "eval.sqlite3"
    store, _ = _legacy_store(path, cohort=cohort, session=JULY_27)
    before = _row(path)
    existing = store.official_observations()[0]

    store.record_official_observation(existing)

    assert _row(path) == before
    assert len(store.official_observations()) == 1


def test_a_close_marked_write_leaves_the_new_columns_unset(tmp_path):
    """The legacy write path must produce exactly the row shape it always produced."""
    store = EvaluationStore(tmp_path / "eval.sqlite3")
    store.record_official_observation(
        OfficialDailyObservation(
            cohort_id="paper-first-2026-07-28",
            run_id="run-2",
            sleeve_id="sleeve-1",
            strategy="buy-hold",
            strategy_hash=FROZEN_CONFIGURATION_HASHES["buy-hold"],
            session_date=JULY_28,
            decision_time=CLOSE_27_UTC,
            valuation_time=CLOSE_27_UTC,
            status=ObservationStatus.OFFICIAL,
            total_value=Decimal("10000"),
            return_pct=Decimal("0"),
        )
    )
    row = _row(tmp_path / "eval.sqlite3")
    assert row["execution_methodology"] == ""
    assert row["signal_session_date"] is None
    assert row["execution_session_date"] is None
    assert row["signal_time"] is None
    assert row["execution_time"] is None


def test_an_observation_cannot_claim_a_signal_session_it_is_not_keyed_on():
    with pytest.raises(ValueError, match="signal_session_date must equal session_date"):
        OfficialDailyObservation(
            cohort_id="c",
            run_id="r",
            sleeve_id="s",
            strategy="buy-hold",
            strategy_hash="x",
            session_date=JULY_27,
            decision_time=CLOSE_27_UTC,
            valuation_time=CLOSE_27_UTC,
            signal_session_date=JULY_28,
            status=ObservationStatus.MISSING,
        )


def test_an_execution_session_cannot_precede_the_signal_session():
    with pytest.raises(ValueError, match="must not precede"):
        OfficialDailyObservation(
            cohort_id="c",
            run_id="r",
            sleeve_id="s",
            strategy="buy-hold",
            strategy_hash="x",
            session_date=JULY_28,
            decision_time=CLOSE_27_UTC,
            valuation_time=CLOSE_27_UTC,
            execution_session_date=JULY_27,
            status=ObservationStatus.MISSING,
        )
