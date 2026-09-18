"""Tests for the single authoritative paper-strategy registry.

Pure and offline: strategies are constructed with empty history / a fresh EDGAR
store (construction never touches the network) and the LLM builder is a stub.
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal
from pathlib import Path

import pytest

from schwab_trader import agent, cli, strategy_registry
from schwab_trader.experiments import StrategyDefinition


def _history_resources() -> strategy_registry.StrategyResources:
    return strategy_registry.StrategyResources(history={}, benchmark_history=[])


def _edgar_resources(tmp_path: Path) -> strategy_registry.StrategyResources:
    from schwab_trader.sec_store import SecStore

    return strategy_registry.StrategyResources(store=SecStore(tmp_path / "sec.sqlite3"))


def _history_and_edgar_resources(tmp_path: Path) -> strategy_registry.StrategyResources:
    from schwab_trader.sec_store import SecStore

    return strategy_registry.StrategyResources(
        history={}, benchmark_history=[], store=SecStore(tmp_path / "sec.sqlite3")
    )


# --- authoritative lists ----------------------------------------------------


def test_paper_list_is_the_single_source_for_the_cli_sleeve_list() -> None:
    assert tuple(strategy_registry.paper_strategy_names()) == cli._SLEEVE_STRATEGIES


def test_rule_backtest_list_matches_cli_constant() -> None:
    assert tuple(strategy_registry.rule_backtest_strategy_names()) == cli._RULE_STRATEGIES


def test_live_names_agree_with_the_untouched_cli_live_list() -> None:
    # The CLI keeps its own literal live list; the registry must agree with it so
    # the two never drift (the live path itself is deliberately not rewired here).
    assert set(strategy_registry.live_strategy_names()) == set(cli._LIVE_STRATEGIES)


def test_every_registered_strategy_is_a_paper_sleeve_strategy() -> None:
    names = strategy_registry.paper_strategy_names()
    assert set(names) == {
        "hold",
        "buy-hold",
        "dip-buyer",
        "intraday",
        "momentum",
        "trend",
        "mean-reversion",
        "low-vol",
        "tactical",
        "fundamental",
        "value-momentum",
        "post-earnings-drift",
        "llm",
        # challenger-v1 (#95). Registered as paper sleeves only: they appear in
        # neither _RULE_STRATEGIES nor _LIVE_STRATEGIES, which the two tests above
        # assert independently.
        "dual-momentum-v1",
        "quality-profitability-v1",
        "short-term-mean-reversion-v1",
    }


def test_simple_strategies_are_universe_only() -> None:
    assert strategy_registry.simple_strategy_names() == [
        "buy-hold",
        "dip-buyer",
        "hold",
        "intraday",
    ]


def test_history_and_edgar_capability_groups() -> None:
    assert set(strategy_registry.history_strategy_names()) == {
        "momentum",
        "trend",
        "mean-reversion",
        "low-vol",
        "tactical",
        "value-momentum",
        "dual-momentum-v1",
        "short-term-mean-reversion-v1",
    }
    assert set(strategy_registry.edgar_strategy_names()) == {
        "fundamental",
        "value-momentum",
        "post-earnings-drift",
        # Ranks purely on point-in-time SEC facts; no price enters the ranking, so it
        # deliberately does not declare daily-price-history.
        "quality-profitability-v1",
    }


# --- construction -----------------------------------------------------------


def test_build_simple_strategy() -> None:
    strat = strategy_registry.build("dip-buyer", ["AAPL", "MSFT"])
    assert isinstance(strat, agent.DipBuyerStrategy)
    assert strat.universe == ["AAPL", "MSFT"]
    # Untouched parameters keep their schema defaults.
    assert strat.max_positions == 3
    assert strat.per_trade_cash == Decimal("400.00")


def test_build_history_strategy_requires_history() -> None:
    with pytest.raises(strategy_registry.MissingCapabilityError):
        strategy_registry.build("momentum", ["AAPL"])
    strat = strategy_registry.build("momentum", ["AAPL"], resources=_history_resources())
    assert isinstance(strat, agent.MomentumStrategy)


def test_build_edgar_strategy_requires_store(tmp_path: Path) -> None:
    with pytest.raises(strategy_registry.MissingCapabilityError):
        strategy_registry.build("fundamental", ["AAPL"])
    strat = strategy_registry.build(
        "fundamental", ["AAPL"], resources=_edgar_resources(tmp_path)
    )
    assert isinstance(strat, agent.FundamentalStrategy)


def test_build_value_momentum_needs_history_and_edgar(tmp_path: Path) -> None:
    with pytest.raises(strategy_registry.MissingCapabilityError):
        strategy_registry.build(
            "value-momentum", ["AAPL"], resources=_history_resources()
        )
    strat = strategy_registry.build(
        "value-momentum", ["AAPL"], resources=_history_and_edgar_resources(tmp_path)
    )
    assert isinstance(strat, agent.ValueMomentumStrategy)


def test_build_llm_requires_provider() -> None:
    with pytest.raises(strategy_registry.MissingCapabilityError):
        strategy_registry.build("llm", ["AAPL"])

    captured: dict[str, object] = {}

    def _builder(universe: list[str], params: dict[str, object]) -> agent.Strategy:
        captured["universe"] = universe
        captured["params"] = params
        return agent.HoldStrategy(universe)

    resources = strategy_registry.StrategyResources(llm_builder=_builder)
    strat = strategy_registry.build("llm", ["AAPL"], resources=resources)
    assert isinstance(strat, agent.HoldStrategy)
    assert captured["universe"] == ["AAPL"]
    assert captured["params"] == {"max_positions": 20, "max_position_fraction": Decimal("0.10")}


def test_unknown_strategy_fails_closed() -> None:
    with pytest.raises(strategy_registry.UnknownStrategyError):
        strategy_registry.build("does-not-exist", ["AAPL"])


# --- parameter validation ---------------------------------------------------


def test_unknown_parameter_fails_closed() -> None:
    with pytest.raises(strategy_registry.UnknownParameterError):
        strategy_registry.validated_parameters("dip-buyer", {"not_a_param": 1})


def test_parameters_change_behavior_only_through_validated_fields() -> None:
    strat = strategy_registry.build(
        "momentum",
        ["AAPL"],
        parameters={"max_positions": "12", "min_rank": "0.25"},
        resources=_history_resources(),
    )
    assert isinstance(strat, agent.MomentumStrategy)
    assert strat.max_positions == 12  # coerced from str to int
    assert strat.min_rank == 0.25  # coerced from str to float


def test_bad_parameter_value_fails_closed() -> None:
    with pytest.raises(strategy_registry.ParameterValidationError):
        strategy_registry.validated_parameters("momentum", {"max_positions": "not-an-int"})


def test_boolean_rejected_for_numeric_parameter() -> None:
    with pytest.raises(strategy_registry.ParameterValidationError):
        strategy_registry.validated_parameters("momentum", {"max_positions": True})


# --- sleeve knob mapping (behavior preservation) ----------------------------


def test_sleeve_parameter_values_only_reach_supporting_strategies() -> None:
    knobs = dict(max_positions=7, max_position_fraction=Decimal("0.2"), factor="roe")

    # Simple strategies ignore the sleeve knobs (dip-buyer keeps default 3).
    assert strategy_registry.sleeve_parameter_values("dip-buyer", **knobs) == {}
    # Tactical takes neither positions/fraction nor factor.
    assert strategy_registry.sleeve_parameter_values("tactical", **knobs) == {}
    # History strategies take positions + fraction, not factor.
    assert strategy_registry.sleeve_parameter_values("momentum", **knobs) == {
        "max_positions": 7,
        "max_position_fraction": Decimal("0.2"),
    }
    # Factor-driven strategies also take the factor.
    assert strategy_registry.sleeve_parameter_values("fundamental", **knobs) == {
        "max_positions": 7,
        "max_position_fraction": Decimal("0.2"),
        "factor": "roe",
    }


def test_empty_factor_falls_back_to_schema_default() -> None:
    values = strategy_registry.sleeve_parameter_values(
        "value-momentum", max_positions=5, max_position_fraction=Decimal("0.1"), factor=""
    )
    assert "factor" not in values  # empty factor omitted -> constructor default applies


# --- definition round trip --------------------------------------------------


def test_make_definition_records_capabilities_and_impl() -> None:
    definition = strategy_registry.make_definition(
        "value-momentum", universe_definition={"preset": "large-cap"}
    )
    assert definition.implementation_name == "value-momentum"
    assert set(definition.data_requirements) == {
        strategy_registry.CAP_DAILY_HISTORY,
        strategy_registry.CAP_SEC_EDGAR,
    }
    # A Decimal parameter is stored JSON-safely as a string.
    assert definition.parameters["max_position_fraction"] == "0.10"


def test_reconstruct_is_deterministic(tmp_path: Path) -> None:
    definition = strategy_registry.make_definition(
        "momentum",
        universe_definition=["AAPL", "MSFT"],
        parameters={"max_positions": 15, "max_position_fraction": Decimal("0.05")},
    )
    strat = strategy_registry.reconstruct(
        definition, ["AAPL", "MSFT"], resources=_history_resources()
    )
    assert isinstance(strat, agent.MomentumStrategy)
    assert strat.max_positions == 15
    assert strat.max_position_fraction == Decimal("0.05")


def test_reconstruct_rejects_tampered_data_requirements() -> None:
    definition = strategy_registry.make_definition("momentum", universe_definition=["AAPL"])
    tampered = definition.model_copy(update={"data_requirements": ("sec-edgar-facts",)})
    with pytest.raises(strategy_registry.DefinitionMismatchError):
        strategy_registry.reconstruct(tampered, ["AAPL"], resources=_history_resources())


def test_reconstruct_rejects_unknown_implementation() -> None:
    definition = StrategyDefinition(
        strategy_id="ghost",
        strategy_version="1",
        implementation_name="ghost-impl",
        parameters={},
        universe_definition=["AAPL"],
        benchmark_symbol_or_sleeve="SPY",
        decision_frequency="daily",
        decision_time=time(16, 0),
        data_requirements=(),
        long_only=True,
        leverage_allowed=False,
    )
    with pytest.raises(strategy_registry.UnknownStrategyError):
        strategy_registry.reconstruct(definition, ["AAPL"])


# --- agent delegation -------------------------------------------------------


def test_agent_build_strategy_delegates_and_rejects_non_simple() -> None:
    assert isinstance(agent.build_strategy("hold", ["AAPL"]), agent.HoldStrategy)
    # Non-simple strategies still raise KeyError through the agent facade.
    with pytest.raises(KeyError):
        agent.build_strategy("momentum", ["AAPL"])


def test_agent_available_strategies_are_the_simple_set() -> None:
    assert agent.available_strategies() == strategy_registry.simple_strategy_names()
