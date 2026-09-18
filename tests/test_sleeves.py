"""Tests for the sleeve registry (offline)."""

from __future__ import annotations

import sqlite3
from datetime import time
from decimal import Decimal

import pytest

from schwab_trader.experiments import StrategyDefinition
from schwab_trader.paper import PaperEngine
from schwab_trader.sleeves import SleeveConfig, SleeveExists, SleeveNameError, SleeveStore


def _definition() -> StrategyDefinition:
    return StrategyDefinition(
        strategy_id="momentum",
        strategy_version="1.2.0",
        implementation_name="momentum_v1",
        parameters={"lookback": 20, "top_n": 5},
        universe_definition={"kind": "static", "symbols": ["AAA", "BBB"]},
        benchmark_symbol_or_sleeve="SPY",
        decision_frequency="daily",
        decision_time=time(15, 45),
        data_requirements=["daily_bars"],
        long_only=True,
        leverage_allowed=False,
    )


def _store(tmp_path) -> SleeveStore:
    return SleeveStore(tmp_path / "sleeves")


def _create(store: SleeveStore, name: str, strategy: str = "buy-hold") -> None:
    store.create(
        name,
        strategy=strategy,
        universe=["AAA", "BBB"],
        starting_cash=Decimal("5000.00"),
        max_positions=10,
        max_position_fraction=Decimal("0.10"),
    )


def test_create_and_get_roundtrip(tmp_path) -> None:
    store = _store(tmp_path)
    _create(store, "basket")
    cfg = store.get("basket")
    assert cfg is not None
    assert cfg.strategy == "buy-hold"
    assert cfg.universe == ["AAA", "BBB"]
    assert cfg.starting_cash == Decimal("5000.00")
    assert cfg.max_position_fraction == Decimal("0.10")


def test_leverage_roundtrips_and_defaults_to_cash(tmp_path) -> None:
    store = _store(tmp_path)
    _create(store, "cash")
    assert store.get("cash").leverage == Decimal(1)  # default: cash account

    store.create(
        "margin",
        strategy="buy-hold",
        universe=["AAA"],
        starting_cash=Decimal("5000.00"),
        max_positions=10,
        max_position_fraction=Decimal("0.10"),
        leverage=Decimal(2),
    )
    cfg = store.get("margin")
    assert cfg is not None
    assert cfg.leverage == Decimal(2)
    # The sleeve's paper engine carries the leverage (2x buying power).
    engine = PaperEngine(
        store.paper_path("margin"), starting_cash=Decimal("5000.00"), leverage=cfg.leverage
    )
    assert engine.buying_power() == Decimal("10000.00")


def test_factor_roundtrips(tmp_path) -> None:
    store = _store(tmp_path)
    store.create(
        "value",
        strategy="fundamental",
        universe=["AAA"],
        starting_cash=Decimal("5000.00"),
        max_positions=10,
        max_position_fraction=Decimal("0.10"),
        factor="book-to-market",
    )
    cfg = store.get("value")
    assert cfg is not None
    assert cfg.strategy == "fundamental"
    assert cfg.factor == "book-to-market"
    # Default (non-fundamental) sleeves have an empty factor.
    _create(store, "plain")
    assert store.get("plain").factor == ""


def test_create_initializes_paper_engine(tmp_path) -> None:
    store = _store(tmp_path)
    _create(store, "ai", strategy="llm")
    # The paper engine exists and starts at the sleeve's cash.
    engine = PaperEngine(store.paper_path("ai"), starting_cash=Decimal("5000.00"))
    assert engine.account().cash == Decimal("5000.00")


def test_duplicate_name_rejected(tmp_path) -> None:
    store = _store(tmp_path)
    _create(store, "dups")
    with pytest.raises(SleeveExists):
        _create(store, "dups")


def test_invalid_name_rejected(tmp_path) -> None:
    store = _store(tmp_path)
    with pytest.raises(SleeveNameError):
        _create(store, "bad name/../x")


def test_list_is_ordered_and_remove_works(tmp_path) -> None:
    store = _store(tmp_path)
    _create(store, "a")
    _create(store, "b")
    assert [c.name for c in store.list()] == ["a", "b"]

    assert store.remove("a") is True
    assert store.get("a") is None
    assert [c.name for c in store.list()] == ["b"]
    assert store.remove("missing") is False


def test_paths_are_isolated_per_sleeve(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.paper_path("x") != store.paper_path("y")
    assert store.eval_path("x") != store.paper_path("x")


def test_definition_roundtrips_exactly(tmp_path) -> None:
    store = _store(tmp_path)
    definition = _definition()
    store.create(
        "momo",
        strategy="momentum",
        universe=["AAA", "BBB"],
        starting_cash=Decimal("5000.00"),
        max_positions=10,
        max_position_fraction=Decimal("0.10"),
        definition=definition,
        cohort_id="cohort-2026-q3",
    )
    cfg = store.get("momo")
    assert cfg is not None
    # The reconstructed definition equals the original, byte-for-byte on its hash.
    assert cfg.reproducible is True
    assert cfg.definition == definition
    assert cfg.configuration_hash == definition.configuration_hash
    assert cfg.cohort_id == "cohort-2026-q3"
    # Cadence is denormalized for querying without parsing the JSON.
    assert cfg.decision_frequency == "daily"
    assert cfg.decision_time == "15:45:00"


def test_definition_survives_store_reopen(tmp_path) -> None:
    definition = _definition()
    store = _store(tmp_path)
    store.create(
        "momo",
        strategy="momentum",
        universe=["AAA"],
        starting_cash=Decimal("5000.00"),
        max_positions=10,
        max_position_fraction=Decimal("0.10"),
        definition=definition,
    )
    # A fresh store instance (new process) must reconstruct the exact definition.
    reopened = SleeveStore(tmp_path / "sleeves")
    cfg = reopened.get("momo")
    assert cfg is not None
    assert cfg.definition == definition


def test_sleeve_without_definition_is_not_reproducible(tmp_path) -> None:
    store = _store(tmp_path)
    _create(store, "plain")
    cfg = store.get("plain")
    assert cfg is not None
    assert cfg.definition is None
    assert cfg.reproducible is False
    assert cfg.configuration_hash == ""
    assert cfg.cohort_id == ""


def _legacy_registry(dir_path) -> None:
    """Create a pre-#29 registry with only the original seven columns and one row."""
    dir_path.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(dir_path / "registry.sqlite3")
    try:
        conn.execute(
            """
            CREATE TABLE sleeves (
                name TEXT PRIMARY KEY,
                strategy TEXT NOT NULL,
                universe_csv TEXT NOT NULL,
                starting_cash TEXT NOT NULL,
                max_positions INTEGER NOT NULL,
                max_position_fraction TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO sleeves (name, strategy, universe_csv, starting_cash, "
            "max_positions, max_position_fraction, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("legacy", "buy-hold", "AAA,BBB", "5000.00", 10, "0.10", "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def test_legacy_registry_migrates_without_data_loss(tmp_path) -> None:
    sleeves_dir = tmp_path / "sleeves"
    _legacy_registry(sleeves_dir)
    # Opening the old registry runs the migration and preserves the existing row.
    store = SleeveStore(sleeves_dir)
    cfg = store.get("legacy")
    assert cfg is not None
    assert cfg.strategy == "buy-hold"
    assert cfg.universe == ["AAA", "BBB"]
    assert cfg.starting_cash == Decimal("5000.00")
    # A migrated legacy row cannot be reconstructed and is labeled, not faked.
    assert cfg.definition is None
    assert cfg.reproducible is False
    # New reproducible sleeves can be added alongside migrated legacy ones.
    store.create(
        "new",
        strategy="momentum",
        universe=["CCC"],
        starting_cash=Decimal("5000.00"),
        max_positions=10,
        max_position_fraction=Decimal("0.10"),
        definition=_definition(),
    )
    assert isinstance(store.get("new"), SleeveConfig)
    assert store.get("new").reproducible is True
