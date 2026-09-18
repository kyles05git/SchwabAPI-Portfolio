"""Offline SQLite contract tests for durable derived market-data evidence."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from schwab_trader import market_bar_evidence, market_calendar, market_data
from schwab_trader.config import Settings
from schwab_trader.storage import factory as storage_factory
from schwab_trader.storage.contracts import MarketDataEvidenceRepository
from schwab_trader.storage.database import Database
from schwab_trader.storage.market_data import (
    EvidenceConflictError,
    SqlAlchemyMarketDataEvidenceStore,
)
from schwab_trader.storage.schema import MarketDataEvidenceConstituent

SESSION = date(2026, 7, 28)


def _evidence(
    *,
    retrieved_at: datetime | None = None,
) -> market_bar_evidence.DerivedDailyEvidence:
    starts = market_calendar.session_interval_starts_utc(SESSION)
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
        for index, stamp in enumerate(starts)
    ]
    result = market_bar_evidence.validate_regular_session(
        "SPY",
        SESSION,
        bars,
        retrieved_at=(
            retrieved_at or market_calendar.session_bounds_utc(SESSION)[1] + timedelta(minutes=1)
        ),
    )
    assert result.evidence is not None
    return result.evidence


@pytest.fixture
def database(tmp_path: Path):
    found = Database(f"sqlite:///{tmp_path / 'evidence.sqlite3'}", create_schema=True)
    try:
        yield found
    finally:
        found.dispose()


def test_persisted_constituents_reproduce_the_same_aggregate(database: Database) -> None:
    store = SqlAlchemyMarketDataEvidenceStore(database)
    original = _evidence()

    persisted = store.save(original)
    reproduced = store.reproduce(original.dataset_id)

    assert isinstance(store, MarketDataEvidenceRepository)
    assert persisted == original
    assert reproduced == original
    assert len(persisted.constituents) == 78
    assert persisted.candle.source == market_bar_evidence.DERIVED_DAILY_SOURCE
    with database.session() as session:
        count = session.scalar(select(func.count()).select_from(MarketDataEvidenceConstituent))
    assert count == 78


def test_identical_retry_is_idempotent_and_preserves_first_retrieval(
    database: Database,
) -> None:
    store = SqlAlchemyMarketDataEvidenceStore(database)
    first = _evidence()
    later = _evidence(retrieved_at=first.retrieved_at + timedelta(hours=1))

    stored_first = store.save(first)
    stored_later = store.save(later)

    assert later.dataset_id == first.dataset_id
    assert stored_later == stored_first
    assert stored_later.retrieved_at == first.retrieved_at
    assert len(store.for_session("spy", SESSION)) == 1


def test_tampered_aggregate_is_rejected_before_persistence(database: Database) -> None:
    store = SqlAlchemyMarketDataEvidenceStore(database)
    original = _evidence()
    tampered = original.model_copy(
        update={
            "candle": original.candle.model_copy(
                update={"close": original.candle.close + Decimal("1")}
            )
        }
    )

    with pytest.raises(ValueError, match="deterministic identity"):
        store.save(tampered)
    assert store.get(original.dataset_id) is None


def test_tampered_persisted_constituent_fails_reproduction(database: Database) -> None:
    store = SqlAlchemyMarketDataEvidenceStore(database)
    original = store.save(_evidence())
    with database.session() as session:
        row = session.get(MarketDataEvidenceConstituent, (original.dataset_id, 10))
        assert row is not None
        row.close = Decimal(row.close) + Decimal("1")

    with pytest.raises(EvidenceConflictError, match="do not reproduce"):
        store.reproduce(original.dataset_id)


@pytest.mark.parametrize("shared", [False, True])
def test_factory_local_and_configured_shared_backends_obey_same_contract(
    tmp_path: Path,
    shared: bool,
) -> None:
    local_path = tmp_path / "local-evidence.sqlite3"
    shared_path = tmp_path / "shared.sqlite3"
    if shared:
        migrated = Database(f"sqlite:///{shared_path}", create_schema=True)
        migrated.dispose()

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        market_data_evidence_db_path=local_path,
        database_url=f"sqlite:///{shared_path}" if shared else "",
    )
    store = storage_factory.market_data_evidence_store(settings)
    original = _evidence()

    persisted = store.save(original)

    assert persisted.dataset_id == original.dataset_id
    assert store.reproduce(original.dataset_id) == persisted
    assert shared_path.exists() if shared else local_path.exists()


def test_read_only_factory_does_not_create_a_missing_local_store(tmp_path: Path) -> None:
    path = tmp_path / "missing-evidence.sqlite3"
    settings = Settings(
        _env_file=None,
        market_data_evidence_db_path=path,
    )

    with pytest.raises(FileNotFoundError, match="does not exist"):
        storage_factory.market_data_evidence_reader(settings)
    assert not path.exists()


def test_read_only_factory_reads_existing_evidence_without_rewriting_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "existing-evidence.sqlite3"
    settings = Settings(
        _env_file=None,
        market_data_evidence_db_path=path,
    )
    writer = storage_factory.market_data_evidence_store(settings)
    evidence = writer.save(_evidence())

    reader = storage_factory.market_data_evidence_reader(settings)

    assert reader.get(evidence.dataset_id) == evidence
    assert reader.for_session("SPY", SESSION) == (evidence,)


def test_two_distinct_provider_datasets_for_one_session_are_preserved(
    database: Database,
) -> None:
    store = SqlAlchemyMarketDataEvidenceStore(database)
    first = _evidence()
    starts = market_calendar.session_interval_starts_utc(SESSION)
    corrected_bars = list(first.constituents)
    corrected_bars[20] = corrected_bars[20].model_copy(
        update={
            "high": corrected_bars[20].high + Decimal("0.01"),
        }
    )
    corrected_result = market_bar_evidence.validate_regular_session(
        "SPY",
        SESSION,
        corrected_bars,
        retrieved_at=first.retrieved_at + timedelta(hours=2),
    )
    assert corrected_result.evidence is not None
    assert corrected_result.evidence.first_interval_at == starts[0]

    store.save(first)
    store.save(corrected_result.evidence)

    versions = store.for_session("SPY", SESSION)
    assert len(versions) == 2
    assert versions[0].dataset_id == first.dataset_id
    assert versions[1].dataset_id == corrected_result.evidence.dataset_id
