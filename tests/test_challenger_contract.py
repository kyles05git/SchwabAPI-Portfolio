"""The challenger-v1 contract is frozen, serializes stably, and hashes deterministically.

These tests are the enforcement mechanism for "freeze the contract". Every pinned
digest below is a deliberate tripwire: if a later change alters a universe, a
limit, a cadence, or a cost assumption, the suite fails and the change has to be
justified in review rather than slipping into a running experiment.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from schwab_trader import universes
from schwab_trader.experiments import (
    canonical_configuration_json,
    deterministic_configuration_hash,
)
from schwab_trader.strategies import contract

# Pinned on 2026-07-30 when the contract was frozen for issue #91. Changing the
# contract must change this digest in the same reviewed commit.
FROZEN_CONTRACT_HASH = "47965e0f12dede2e74ba7100276eeafc09d9a7ae8577788d8906f5594f9bd981"

# The large-cap preset is referenced by two sleeves rather than copied into them.
# Pinning its digest means an edit to ``universes.py`` fails here instead of
# silently redefining a frozen experiment.
FROZEN_LARGE_CAP_HASH = "c0dad906e32974030fd3dd8c8e29b38b3eec367cb165f397db304af77cabcadd"


def test_contract_hash_is_frozen() -> None:
    assert contract.contract_hash() == FROZEN_CONTRACT_HASH


def test_large_cap_universe_is_pinned() -> None:
    preset = universes.get_preset("large-cap")
    assert preset is not None
    assert deterministic_configuration_hash(preset) == FROZEN_LARGE_CAP_HASH
    assert tuple(preset) == contract.QUALITY_PROFITABILITY.universe
    assert tuple(preset) == contract.SHORT_TERM_MEAN_REVERSION.universe


def test_contract_hash_is_stable_across_calls() -> None:
    assert contract.contract_hash() == contract.contract_hash()
    assert contract.contract_payload() == contract.contract_payload()


def test_contract_payload_is_json_serializable_and_canonical() -> None:
    payload = contract.contract_payload()
    encoded = canonical_configuration_json(payload)
    # Round-tripping through JSON must not change the digest: the payload holds no
    # Decimal, tuple, set, or other type that survives in memory but not on disk.
    assert deterministic_configuration_hash(json.loads(encoded)) == contract.contract_hash()


def test_contract_hash_changes_when_any_decision_changes() -> None:
    """A digest that ignored a field would silently permit editing that field."""
    baseline = contract.contract_payload()
    for field in ("cost_model_id", "starting_cash_per_sleeve", "gross_exposure_cap", "sleeves"):
        mutated = dict(baseline)
        mutated.pop(field)
        assert deterministic_configuration_hash(mutated) != contract.contract_hash()


# --- membership and capital --------------------------------------------------


def test_exactly_five_named_sleeves() -> None:
    assert contract.SLEEVE_NAMES == (
        "control-cash",
        "bench-spy",
        "dual-momentum-v1",
        "quality-profitability-v1",
        "short-term-mean-reversion-v1",
    )
    assert len(contract.SLEEVES) == contract.SLEEVE_COUNT == 5


def test_each_sleeve_is_independently_funded_with_ten_thousand() -> None:
    assert contract.STARTING_CASH_PER_SLEEVE == Decimal("10000.00")


def test_long_only_unlevered_whole_shares() -> None:
    assert contract.LONG_ONLY is True
    assert contract.LEVERAGE_ALLOWED is False
    assert contract.LEVERAGE == Decimal("1")
    assert contract.WHOLE_SHARES_ONLY is True
    # No regime overlay: each sleeve tests exactly one idea.
    assert contract.GROSS_EXPOSURE_CAP == Decimal("1.00")


def test_position_limits_cannot_exceed_the_sleeve() -> None:
    for sleeve in contract.SLEEVES:
        exposure = sleeve.max_positions * sleeve.max_position_fraction
        assert exposure <= Decimal("1"), f"{sleeve.name} can allocate more than 100%"
        assert Decimal("0") <= sleeve.max_position_fraction <= Decimal("1")
        assert sleeve.max_positions >= 0


def test_numeric_accessors_parse_the_frozen_strings_once() -> None:
    """Thresholds are stored as strings for exact hashing; parsing lives in one place."""
    assert contract.QUALITY_PROFITABILITY.coverage_floor == Decimal("0.60")
    assert contract.DUAL_MOMENTUM.coverage_floor == Decimal("1")
    assert contract.SHORT_TERM_MEAN_REVERSION.parameter_decimal("entry_dip") == Decimal("0.05")
    assert contract.DUAL_MOMENTUM.parameter_decimal("absolute_gate_minimum") == Decimal("0")
    with pytest.raises(TypeError, match="not a numeric parameter"):
        contract.SHORT_TERM_MEAN_REVERSION.parameter_decimal("exit_on_long_ma_break")


def test_numeric_accessors_do_not_change_the_contract_hash() -> None:
    """Convenience accessors are derived, so they must not enter the frozen payload."""
    assert contract.contract_hash() == FROZEN_CONTRACT_HASH


def test_sleeve_lookup_and_unknown_name_fails_closed() -> None:
    assert contract.sleeve("dual-momentum-v1") is contract.DUAL_MOMENTUM
    with pytest.raises(KeyError, match="not a challenger-v1 sleeve"):
        contract.sleeve("momentum")


# --- timing and costs --------------------------------------------------------


def test_execution_depends_on_t1_open() -> None:
    assert contract.REQUIRES_T1_OPEN_EXECUTION is True
    assert "T+1" in contract.EXECUTION_BASIS or "next trading session" in (
        contract.EXECUTION_BASIS
    )
    assert "signal session T" in contract.SIGNAL_BASIS
    assert contract.SETTLEMENT_MODEL == "T+1"


def test_costs_are_explicit_and_uniform() -> None:
    assert contract.COST_BPS_ROUND_TRIP == contract.COST_BPS_PER_SIDE * 2
    assert contract.COST_BPS_PER_SIDE > 0, "a zero-cost model would flatter turnover"
    # One cost model for the whole cohort, benchmark included.
    assert "challenger-v1" in contract.COST_MODEL_ID


def test_stale_and_conflicting_evidence_fail_closed() -> None:
    assert contract.MAX_PRICE_STALENESS_SESSIONS == 0
    assert contract.CONFLICTING_EVIDENCE_FAILS_SLEEVE is True


# --- per-sleeve frozen decisions ---------------------------------------------


def test_dual_momentum_is_frozen() -> None:
    sleeve = contract.DUAL_MOMENTUM
    assert sleeve.universe == ("SPY", "EFA", "EEM", "VNQ")
    assert sleeve.defensive_universe == ("IEF",)
    assert sleeve.max_positions == 1
    assert sleeve.parameters["lookback_sessions"] == 252
    # 252 sessions of return require 253 closes.
    assert sleeve.minimum_price_sessions == 253
    # A four-name universe cannot absorb a missing member.
    assert sleeve.minimum_eligible_fraction == "1"
    assert sleeve.data_requirements == (contract.CAP_DAILY_HISTORY,)


def test_quality_profitability_is_frozen() -> None:
    sleeve = contract.QUALITY_PROFITABILITY
    assert sleeve.max_positions == 10
    assert sleeve.max_position_fraction == Decimal("0.10")
    assert sleeve.parameters["components"] == [
        "gross-profitability",
        "return-on-equity",
        "net-margin",
    ]
    assert sleeve.parameters["eligibility"] == "all three components computable"
    assert sleeve.minimum_eligible_fraction == "0.60"
    assert contract.CAP_SEC_EDGAR in sleeve.data_requirements


def test_short_term_mean_reversion_is_frozen() -> None:
    sleeve = contract.SHORT_TERM_MEAN_REVERSION
    assert sleeve.max_positions == 5
    # 1/5 exactly, so a full book is fully invested rather than half in cash.
    assert sleeve.max_positions * sleeve.max_position_fraction == Decimal("1.00")
    assert sleeve.parameters["short_ma"] == 20
    assert sleeve.parameters["long_ma"] == 200
    assert sleeve.parameters["entry_dip"] == "0.05"
    # Explicit exits, unlike the registered implementation's implicit one.
    assert sleeve.parameters["exit_recovery_band"] == "0.01"
    assert sleeve.parameters["exit_on_long_ma_break"] is True
    assert sleeve.minimum_price_sessions == 201


def test_controls_never_trade_or_rotate() -> None:
    assert contract.CONTROL_CASH.max_positions == 0
    assert contract.CONTROL_CASH.rebalance_cadence == "never"
    assert contract.BENCH_SPY.universe == ("SPY",)
    assert "hold" in contract.BENCH_SPY.rebalance_cadence


def test_every_sleeve_declares_a_deterministic_tie_break() -> None:
    """Float scores tie; without a frozen tie-break the selected set is not reproducible."""
    for sleeve in contract.SLEEVES:
        if sleeve.max_positions <= 1 and not sleeve.parameters:
            continue  # the two controls rank nothing
        assert sleeve.parameters["tie_break"] == "frozen universe order"


# --- ownership boundaries ----------------------------------------------------


def test_each_strategy_sleeve_names_its_owning_issue_and_single_module() -> None:
    owners = {sleeve.name: sleeve.owner for sleeve in contract.SLEEVES}
    assert owners["dual-momentum-v1"] == "#92"
    assert owners["quality-profitability-v1"] == "#93"
    assert owners["short-term-mean-reversion-v1"] == "#94"
    # The two controls reuse already-registered strategies; nobody implements them.
    assert owners["control-cash"] == "#95"
    assert owners["bench-spy"] == "#95"

    modules = [s.module for s in contract.SLEEVES if s.owner in {"#92", "#93", "#94"}]
    assert len(set(modules)) == 3, "two strategy agents would collide on one module"
    for module in modules:
        assert module.startswith("schwab_trader/strategies/")


# --- interpretation ----------------------------------------------------------


def test_contract_states_it_is_not_alpha_evidence() -> None:
    assert "not evidence of alpha" in contract.INTERPRETATION
    assert "no result" in contract.INTERPRETATION.lower()


def test_known_limitations_disclose_survivorship_and_price_return() -> None:
    disclosed = " ".join(contract.KNOWN_LIMITATIONS).lower()
    assert "survivorship" in disclosed
    assert "price return" in disclosed
    assert "dividend" in disclosed


# --- document and code agree --------------------------------------------------

_DOC = (
    Path(contract.__file__).parents[3]
    / "docs"
    / "architecture"
    / "challenger-v1-contract.md"
)


def test_specification_document_exists() -> None:
    assert _DOC.is_file(), "the frozen contract must have a reviewed prose specification"


def test_document_quotes_the_current_contract_hash() -> None:
    """The reviewed document and the executing code must describe one experiment."""
    assert contract.contract_hash() in _DOC.read_text(encoding="utf-8")


def test_document_names_every_sleeve_and_its_owner() -> None:
    text = _DOC.read_text(encoding="utf-8")
    for sleeve in contract.SLEEVES:
        assert sleeve.name in text
    for owner in ("#92", "#93", "#94", "#95", "#79"):
        assert owner in text
