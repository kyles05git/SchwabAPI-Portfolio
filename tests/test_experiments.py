from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest
from pydantic import ValidationError

from schwab_trader.experiments import (
    ExperimentCohort,
    StrategyDefinition,
    canonical_configuration_json,
    deterministic_configuration_hash,
    normalize_configuration,
)


def _strategy_input() -> dict[str, object]:
    return {
        "strategy_id": "momentum-large",
        "strategy_version": "1",
        "implementation_name": "momentum",
        "parameters": {
            "lookbacks": [63, 126, 252],
            "max_position_fraction": 0.075,
            "rebalance": {"weekday": 1, "enabled": True},
        },
        "universe_definition": {
            "kind": "static",
            "symbols": ["AAPL", "MSFT", "SPY"],
        },
        "benchmark_symbol_or_sleeve": "bench-spy",
        "decision_frequency": "weekly",
        "decision_time": "15:55:00",
        "data_requirements": ["price_history", "quotes"],
        "long_only": True,
        "leverage_allowed": False,
    }


def _cohort_input() -> dict[str, object]:
    return {
        "cohort_id": "first-paper-cohort",
        "name": "First paper cohort",
        "created_at": datetime(2026, 7, 21, 15, 0, tzinfo=UTC),
        "start_session": date(2026, 7, 22),
        "starting_cash_per_sleeve": Decimal("10000"),
        "settlement_model": "T+1",
        "leverage": Decimal("1"),
        "benchmark_sleeve": "bench-spy",
        "decision_schedule": "official-daily-close-v1",
        "cost_model_id": "paper-cost-v1",
        "member_sleeves": ["control-cash", "bench-spy", "momentum-large"],
        "status": "active",
    }


def test_configuration_normalization_is_canonical() -> None:
    decomposed_e_acute = "e\u0301"
    normalized = normalize_configuration(
        {
            "z": (-0.0, 2.0, 2.5),
            "a": {"text": decomposed_e_acute},
        }
    )

    assert normalized == {
        "a": {"text": "é"},
        "z": [0, 2, 2.5],
    }
    assert canonical_configuration_json(normalized) == ('{"a":{"text":"é"},"z":[0,2,2.5]}')
    assert deterministic_configuration_hash(normalized) == deterministic_configuration_hash(
        {"a": {"text": "é"}, "z": [0, 2, 2.5]}
    )


@pytest.mark.parametrize(
    "unstable_value, message",
    [
        ({1, 2}, "unordered collection"),
        (float("nan"), "non-finite float"),
        (float("inf"), "non-finite float"),
        (Decimal("0.1"), "unsupported configuration value type Decimal"),
        ({1: "value"}, "non-string object key"),
    ],
)
def test_configuration_normalization_rejects_unstable_values(
    unstable_value: object, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_configuration(unstable_value)


def test_configuration_normalization_rejects_cycles() -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)

    with pytest.raises(ValueError, match="cyclic sequence"):
        normalize_configuration(cyclic)


def test_equivalent_normalized_strategy_definitions_hash_identically() -> None:
    first = StrategyDefinition(**_strategy_input())
    equivalent_input = _strategy_input()
    equivalent_input.update(
        {
            "strategy_id": "  momentum-large  ",
            "parameters": {
                "rebalance": {"enabled": True, "weekday": 1.0},
                "max_position_fraction": 0.075,
                "lookbacks": (63.0, 126, 252),
            },
            "universe_definition": {
                "symbols": ("AAPL", "MSFT", "SPY"),
                "kind": "static",
            },
            "decision_frequency": "  WEEKLY ",
            "decision_time": time(15, 55),
            "data_requirements": [" Quotes ", "PRICE_HISTORY", "quotes"],
        }
    )
    second = StrategyDefinition(**equivalent_input)

    assert first.configuration_hash == second.configuration_hash
    assert second.parameters["lookbacks"] == [63, 126, 252]
    assert second.data_requirements == ("price_history", "quotes")


@pytest.mark.parametrize(
    ("field", "changed_value"),
    [
        ("strategy_id", "momentum-small"),
        ("strategy_version", "2"),
        ("implementation_name", "trend"),
        ("parameters", {"lookbacks": [21], "max_position_fraction": 0.075}),
        ("universe_definition", {"kind": "static", "symbols": ["SPY"]}),
        ("benchmark_symbol_or_sleeve", "control-cash"),
        ("decision_frequency", "daily"),
        ("decision_time", "15:54:00"),
        ("data_requirements", ["price_history", "quotes", "fundamentals"]),
        ("long_only", False),
        ("leverage_allowed", True),
    ],
)
def test_every_decision_relevant_change_changes_strategy_hash(
    field: str, changed_value: object
) -> None:
    original = StrategyDefinition(**_strategy_input())
    changed_input = _strategy_input()
    changed_input[field] = changed_value
    changed = StrategyDefinition(**changed_input)

    assert changed.configuration_hash != original.configuration_hash


def test_strategy_definition_rejects_unstable_parameters() -> None:
    invalid_input = _strategy_input()
    invalid_input["parameters"] = {"symbols": {"SPY", "QQQ"}}

    with pytest.raises(ValidationError, match="unordered collection"):
        StrategyDefinition(**invalid_input)


def test_strategy_definition_rejects_a_stale_or_tampered_hash() -> None:
    invalid_input = _strategy_input()
    invalid_input["configuration_hash"] = "0" * 64

    with pytest.raises(ValidationError, match="does not match"):
        StrategyDefinition(**invalid_input)


def test_strategy_definition_json_round_trip_preserves_hash() -> None:
    original = StrategyDefinition(**_strategy_input())

    reloaded = StrategyDefinition.model_validate_json(original.model_dump_json())

    assert reloaded == original
    assert reloaded.configuration_payload() == original.configuration_payload()


def test_experiment_cohort_normalizes_and_round_trips_json() -> None:
    cohort_input = _cohort_input()
    cohort_input["settlement_model"] = " t + 1 "
    cohort_input["benchmark_sleeve"] = "BENCH-SPY"
    cohort_input["status"] = " ACTIVE "
    cohort = ExperimentCohort(**cohort_input)

    assert cohort.starting_cash_per_sleeve == Decimal("10000.00")
    assert cohort.settlement_model == "T+1"
    assert cohort.benchmark_sleeve == "bench-spy"
    assert cohort.status == "active"
    assert cohort.created_at.tzinfo is UTC
    assert ExperimentCohort.model_validate_json(cohort.model_dump_json()) == cohort


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        (
            {"member_sleeves": ["bench-spy", " BENCH-SPY "]},
            "duplicate cohort member",
        ),
        (
            {"member_sleeves": ["control-cash", "candidate"]},
            "benchmark_sleeve must also appear",
        ),
        ({"member_sleeves": ["bench-spy"]}, "at least two distinct sleeves"),
        ({"starting_cash_per_sleeve": 0}, "must be greater than zero"),
        ({"starting_cash_per_sleeve": "10000.001"}, "fractions of a cent"),
        ({"settlement_model": "cash"}, "form 'T\\+N'"),
        ({"leverage": 0}, "leverage must be greater than zero"),
        ({"created_at": datetime(2026, 7, 21, 15, 0)}, "must include a timezone"),
        ({"status": " "}, "status must not be empty"),
    ],
)
def test_invalid_cohort_definitions_fail_closed(updates: dict[str, object], message: str) -> None:
    invalid_input = _cohort_input()
    invalid_input.update(updates)

    with pytest.raises(ValidationError, match=message):
        ExperimentCohort(**invalid_input)


def test_cohort_validates_member_capital_settlement_and_leverage_assumptions() -> None:
    cohort = ExperimentCohort(**_cohort_input())

    cohort.validate_member_assumptions(
        starting_cash_per_sleeve="10000.00",
        settlement_model="t + 1",
        leverage="1.0",
    )

    with pytest.raises(ValueError, match="starting cash is incompatible"):
        cohort.validate_member_assumptions(
            starting_cash_per_sleeve="9000",
            settlement_model="T+1",
            leverage="1",
        )
    with pytest.raises(ValueError, match="settlement model is incompatible"):
        cohort.validate_member_assumptions(
            starting_cash_per_sleeve="10000",
            settlement_model="T+2",
            leverage="1",
        )
    with pytest.raises(ValueError, match="leverage is incompatible"):
        cohort.validate_member_assumptions(
            starting_cash_per_sleeve="10000",
            settlement_model="T+1",
            leverage="2",
        )
