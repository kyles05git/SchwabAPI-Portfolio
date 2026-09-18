"""Single authoritative registry of paper-sleeve strategies.

Before this module the CLI carried three overlapping hard-coded strategy lists
(``_RULE_STRATEGIES``, ``_SLEEVE_STRATEGIES``, ``_LIVE_STRATEGIES``) plus several
special-case construction branches (``agent.build_strategy``, the history-bars
builder, the sleeve builder). Each new strategy had to be threaded through every
one of those places by hand.

This registry replaces that with one table. Every paper strategy is described
once by:

* its stable ``name`` (the value persisted as ``SleeveConfig.strategy``),
* a validated parameter schema with defaults,
* the runtime data capabilities it needs (daily history, SEC EDGAR facts, an
  LLM provider), and
* membership flags for the rule-backtest and live paths.

From that single description the module can:

* enumerate the authoritative strategy lists,
* deterministically reconstruct a :class:`~schwab_trader.agent.Strategy` from a
  stored :class:`~schwab_trader.experiments.StrategyDefinition`, and
* fail closed on unknown parameters or unavailable dependencies.

The module is network-free. Runtime resources (fetched price history, an opened
EDGAR store, a wired LLM builder) are injected by the caller through
:class:`StrategyResources`; the registry never fetches them itself. It builds
**paper** strategies only and has no knowledge of live authorization or risk
gates.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import time
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from schwab_trader import challenger_strategies
from schwab_trader.agent import (
    BuyHoldStrategy,
    DipBuyerStrategy,
    FundamentalStrategy,
    HoldStrategy,
    IntradayReversionStrategy,
    LowVolatilityStrategy,
    MeanReversionStrategy,
    MomentumStrategy,
    PostEarningsDriftStrategy,
    Strategy,
    TacticalRegimeStrategy,
    TrendStrategy,
    ValueMomentumStrategy,
)
from schwab_trader.experiments import ConfigurationValue, StrategyDefinition
from schwab_trader.llm_strategy import LLM_STRATEGY_NAME
from schwab_trader.market_data import Candle
from schwab_trader.sec_store import SecStore

# --- data capabilities ------------------------------------------------------
#
# Stable capability labels. They are the values stored in
# ``StrategyDefinition.data_requirements`` and the keys the injected
# ``StrategyResources`` are matched against. Lower-case and hyphenated so they
# survive the definition's Unicode/casefold normalization unchanged.
CAP_DAILY_HISTORY = "daily-price-history"
CAP_SEC_EDGAR = "sec-edgar-facts"
CAP_LLM_PROVIDER = "llm-provider"


# --- errors -----------------------------------------------------------------


class StrategyRegistryError(Exception):
    """Base class for every registry failure (all fail closed)."""


class UnknownStrategyError(StrategyRegistryError):
    """Raised when a strategy name is not registered."""


class UnknownParameterError(StrategyRegistryError):
    """Raised when stored parameters contain a key outside the schema."""


class ParameterValidationError(StrategyRegistryError):
    """Raised when a stored parameter value cannot be coerced to its type."""


class MissingCapabilityError(StrategyRegistryError):
    """Raised when a required runtime dependency was not supplied."""


class DefinitionMismatchError(StrategyRegistryError):
    """Raised when a stored definition disagrees with the known implementation."""


# --- parameter schema -------------------------------------------------------

LlmBuilder = Callable[[list[str], dict[str, Any]], Strategy]


@dataclass(frozen=True)
class ParameterSpec:
    """One validated, defaulted strategy parameter.

    ``kind`` selects the coercion applied to a stored value so that a parameter
    read back from JSON (where a ``Decimal`` is stored as a string and an
    integral float may have been folded to an ``int``) is restored to the exact
    type the strategy constructor expects.
    """

    name: str
    default: object
    kind: str  # "decimal" | "int" | "float" | "str" | "bool" | "string-sequence"


def _coerce(spec: ParameterSpec, value: object) -> object:
    kind = spec.kind
    if kind == "string-sequence":
        # A frozen challenger parameter can legitimately be a list (the quality
        # sleeve's three components and their three weights). Stored as a JSON array
        # and restored as a tuple, so the reconstructed value is immutable and its
        # order — which fixes the composite's summation order — is preserved exactly.
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise ParameterValidationError(f"{spec.name} must be an array of strings")
        items = cast("list[object] | tuple[object, ...]", value)
        if not all(isinstance(item, str) for item in items):
            raise ParameterValidationError(f"{spec.name} must contain only strings")
        return tuple(cast("list[str] | tuple[str, ...]", items))
    if kind == "decimal":
        if isinstance(value, bool):
            raise ParameterValidationError(f"{spec.name} must be a decimal number, not a boolean")
        if isinstance(value, Decimal):
            return value
        if isinstance(value, (int, float, str)):
            try:
                coerced = Decimal(str(value))
            except InvalidOperation as exc:
                raise ParameterValidationError(f"{spec.name} must be a decimal number") from exc
            if not coerced.is_finite():
                raise ParameterValidationError(f"{spec.name} must be a finite decimal number")
            return coerced
        raise ParameterValidationError(f"{spec.name} must be a decimal number")
    if kind == "int":
        if isinstance(value, bool):
            raise ParameterValidationError(f"{spec.name} must be an integer, not a boolean")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError as exc:
                raise ParameterValidationError(f"{spec.name} must be an integer") from exc
        raise ParameterValidationError(f"{spec.name} must be an integer")
    if kind == "float":
        if isinstance(value, bool):
            raise ParameterValidationError(f"{spec.name} must be a number, not a boolean")
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError as exc:
                raise ParameterValidationError(f"{spec.name} must be a number") from exc
        raise ParameterValidationError(f"{spec.name} must be a number")
    if kind == "str":
        if isinstance(value, str):
            return value
        raise ParameterValidationError(f"{spec.name} must be a string")
    if kind == "bool":
        if isinstance(value, bool):
            return value
        raise ParameterValidationError(f"{spec.name} must be a boolean")
    raise ParameterValidationError(f"{spec.name} has an unsupported parameter kind {kind!r}")


def _to_configuration_value(value: object) -> ConfigurationValue:
    """Render a validated parameter as a JSON-storable configuration value.

    ``Decimal`` values are stored as strings so they survive a JSON round-trip
    without losing precision; ``_coerce`` restores them on reconstruction.
    """

    if isinstance(value, Decimal):
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        items = cast("list[object] | tuple[object, ...]", value)
        if all(isinstance(item, str) for item in items):
            return list(cast("list[str] | tuple[str, ...]", items))
    raise ParameterValidationError(f"cannot store parameter of type {type(value).__name__}")


# --- registry entries -------------------------------------------------------


@dataclass(frozen=True)
class StrategyEntry:
    """Complete, single-source description of one paper strategy."""

    name: str
    implementation_name: str
    implementation: type[Strategy] | None
    parameters: tuple[ParameterSpec, ...]
    capabilities: frozenset[str]
    # The subset of ``parameters`` the sleeve/backtest paths populate from a
    # ``SleeveConfig`` (max positions, max fraction, factor). Everything else
    # keeps its schema default, exactly as the pre-registry code did.
    sleeve_parameters: frozenset[str] = frozenset()
    in_rule_backtest: bool = False
    in_live: bool = False
    is_llm: bool = False

    @property
    def is_simple(self) -> bool:
        """True when the strategy is built from just a universe (no resources)."""
        return not self.capabilities and not self.is_llm

    def parameter(self, name: str) -> ParameterSpec:
        for spec in self.parameters:
            if spec.name == name:
                return spec
        raise UnknownParameterError(f"{self.name} has no parameter '{name}'")


def _dec(text: str) -> Decimal:
    return Decimal(text)


# Registration order below defines the display/enumeration order of the paper
# strategy lists. It mirrors the historical ``_SLEEVE_STRATEGIES`` order.
_ENTRY_LIST: tuple[StrategyEntry, ...] = (
    StrategyEntry(
        name="hold",
        implementation_name="hold",
        implementation=HoldStrategy,
        parameters=(),
        capabilities=frozenset(),
        in_rule_backtest=True,
        in_live=True,
    ),
    StrategyEntry(
        name="buy-hold",
        implementation_name="buy-hold",
        implementation=BuyHoldStrategy,
        parameters=(),
        capabilities=frozenset(),
        in_rule_backtest=True,
        in_live=True,
    ),
    StrategyEntry(
        name="dip-buyer",
        implementation_name="dip-buyer",
        implementation=DipBuyerStrategy,
        parameters=(
            ParameterSpec("dip_pct", _dec("0.01"), "decimal"),
            ParameterSpec("max_positions", 3, "int"),
            ParameterSpec("per_trade_cash", _dec("400.00"), "decimal"),
        ),
        capabilities=frozenset(),
        in_rule_backtest=True,
    ),
    StrategyEntry(
        name="intraday",
        implementation_name="intraday",
        implementation=IntradayReversionStrategy,
        parameters=(
            ParameterSpec("entry_dip", _dec("0.005"), "decimal"),
            ParameterSpec("exit_recover", _dec("0.001"), "decimal"),
            ParameterSpec("stop_pct", _dec("0.02"), "decimal"),
            ParameterSpec("max_positions", 5, "int"),
        ),
        capabilities=frozenset(),
        in_rule_backtest=True,
    ),
    StrategyEntry(
        name="momentum",
        implementation_name="momentum",
        implementation=MomentumStrategy,
        parameters=(
            ParameterSpec("max_positions", 8, "int"),
            ParameterSpec("max_position_fraction", _dec("0.10"), "decimal"),
            ParameterSpec("min_rank", 0.5, "float"),
        ),
        capabilities=frozenset({CAP_DAILY_HISTORY}),
        sleeve_parameters=frozenset({"max_positions", "max_position_fraction"}),
        in_rule_backtest=True,
        in_live=True,
    ),
    StrategyEntry(
        name="trend",
        implementation_name="trend",
        implementation=TrendStrategy,
        parameters=(
            ParameterSpec("max_positions", 10, "int"),
            ParameterSpec("max_position_fraction", _dec("0.10"), "decimal"),
            ParameterSpec("long_ma", 200, "int"),
        ),
        capabilities=frozenset({CAP_DAILY_HISTORY}),
        sleeve_parameters=frozenset({"max_positions", "max_position_fraction"}),
        in_rule_backtest=True,
        in_live=True,
    ),
    StrategyEntry(
        name="mean-reversion",
        implementation_name="mean-reversion",
        implementation=MeanReversionStrategy,
        parameters=(
            ParameterSpec("max_positions", 5, "int"),
            ParameterSpec("max_position_fraction", _dec("0.10"), "decimal"),
            ParameterSpec("short_ma", 20, "int"),
            ParameterSpec("long_ma", 200, "int"),
            ParameterSpec("dip", 0.05, "float"),
        ),
        capabilities=frozenset({CAP_DAILY_HISTORY}),
        sleeve_parameters=frozenset({"max_positions", "max_position_fraction"}),
        in_rule_backtest=True,
        in_live=True,
    ),
    StrategyEntry(
        name="low-vol",
        implementation_name="low-vol",
        implementation=LowVolatilityStrategy,
        parameters=(
            ParameterSpec("max_positions", 10, "int"),
            ParameterSpec("max_position_fraction", _dec("0.10"), "decimal"),
            ParameterSpec("vol_window", 120, "int"),
        ),
        capabilities=frozenset({CAP_DAILY_HISTORY}),
        sleeve_parameters=frozenset({"max_positions", "max_position_fraction"}),
        in_rule_backtest=True,
        in_live=True,
    ),
    StrategyEntry(
        name="tactical",
        implementation_name="tactical",
        implementation=TacticalRegimeStrategy,
        parameters=(ParameterSpec("drift_band", _dec("0.05"), "decimal"),),
        capabilities=frozenset({CAP_DAILY_HISTORY}),
        # The sleeve/backtest paths route only the risk asset and drift default;
        # they do not thread max positions/fraction into the tactical allocator.
        sleeve_parameters=frozenset(),
        in_rule_backtest=True,
    ),
    StrategyEntry(
        name="fundamental",
        implementation_name="fundamental",
        implementation=FundamentalStrategy,
        parameters=(
            ParameterSpec("factor", "book-to-market", "str"),
            ParameterSpec("max_positions", 10, "int"),
            ParameterSpec("max_position_fraction", _dec("0.10"), "decimal"),
            ParameterSpec("use_ttm", True, "bool"),
        ),
        capabilities=frozenset({CAP_SEC_EDGAR}),
        sleeve_parameters=frozenset({"factor", "max_positions", "max_position_fraction"}),
        in_live=True,
    ),
    StrategyEntry(
        name="value-momentum",
        implementation_name="value-momentum",
        implementation=ValueMomentumStrategy,
        parameters=(
            ParameterSpec("factor", "earnings-yield", "str"),
            ParameterSpec("max_positions", 10, "int"),
            ParameterSpec("max_position_fraction", _dec("0.10"), "decimal"),
            ParameterSpec("min_rank", 0.5, "float"),
            ParameterSpec("value_weight", 0.5, "float"),
        ),
        capabilities=frozenset({CAP_DAILY_HISTORY, CAP_SEC_EDGAR}),
        sleeve_parameters=frozenset({"factor", "max_positions", "max_position_fraction"}),
        in_rule_backtest=True,
        in_live=True,
    ),
    StrategyEntry(
        name="post-earnings-drift",
        implementation_name="post-earnings-drift",
        implementation=PostEarningsDriftStrategy,
        parameters=(
            ParameterSpec("max_positions", 10, "int"),
            ParameterSpec("max_position_fraction", _dec("0.10"), "decimal"),
            ParameterSpec("drift_window_days", 65, "int"),
            ParameterSpec("min_sue", 0.0, "float"),
        ),
        capabilities=frozenset({CAP_SEC_EDGAR}),
        sleeve_parameters=frozenset({"max_positions", "max_position_fraction"}),
        in_rule_backtest=True,
        in_live=True,
    ),
    StrategyEntry(
        name=LLM_STRATEGY_NAME,
        implementation_name=LLM_STRATEGY_NAME,
        implementation=None,  # wired via the injected llm_builder resource
        parameters=(
            ParameterSpec("max_positions", 20, "int"),
            ParameterSpec("max_position_fraction", _dec("0.10"), "decimal"),
        ),
        capabilities=frozenset({CAP_LLM_PROVIDER}),
        sleeve_parameters=frozenset({"max_positions", "max_position_fraction"}),
        is_llm=True,
    ),
    # --- challenger-v1 ------------------------------------------------------
    #
    # The three sleeves frozen by ``strategies.contract`` and implemented by #92, #93,
    # and #94. Their schemas list *every* frozen value rather than only the ones that
    # look tunable, so a persisted definition's configuration hash covers the whole
    # experiment: lookback, thresholds, weights, limits, coverage floor, and cadence.
    # The adapters verify each value against the contract at construction, so a stored
    # definition either reproduces the frozen experiment exactly or fails closed — it
    # can never quietly run a different one under a frozen name.
    #
    # ``sleeve_parameters`` is empty for all three deliberately: the generic sleeve
    # knobs must not be able to reach a frozen sleeve. Its limits come from the
    # contract and are asserted against it. For the same reason none of the three is
    # in the rule-backtest or live sets — challenger-v1 is a paper cohort experiment,
    # not a validated live candidate.
    StrategyEntry(
        name=challenger_strategies.DualMomentumV1Strategy.name,
        implementation_name=challenger_strategies.DualMomentumV1Strategy.name,
        implementation=challenger_strategies.DualMomentumV1Strategy,
        parameters=(
            ParameterSpec("rebalance_cadence", "monthly", "str"),
            ParameterSpec("lookback_sessions", 252, "int"),
            ParameterSpec("ranking_metric", "trailing-price-return", "str"),
            ParameterSpec("absolute_gate_metric", "trailing-price-return", "str"),
            ParameterSpec("absolute_gate_minimum", _dec("0"), "decimal"),
            ParameterSpec("tie_break", "frozen universe order", "str"),
            ParameterSpec("max_positions", 1, "int"),
            ParameterSpec("max_position_fraction", _dec("1.00"), "decimal"),
            ParameterSpec("minimum_price_sessions", 253, "int"),
            ParameterSpec("minimum_eligible_fraction", _dec("1"), "decimal"),
        ),
        capabilities=frozenset({CAP_DAILY_HISTORY}),
    ),
    StrategyEntry(
        name=challenger_strategies.QualityProfitabilityV1Strategy.name,
        implementation_name=challenger_strategies.QualityProfitabilityV1Strategy.name,
        implementation=challenger_strategies.QualityProfitabilityV1Strategy,
        parameters=(
            ParameterSpec("rebalance_cadence", "monthly", "str"),
            ParameterSpec(
                "components",
                ("gross-profitability", "return-on-equity", "net-margin"),
                "string-sequence",
            ),
            ParameterSpec("component_weights", ("1/3", "1/3", "1/3"), "string-sequence"),
            ParameterSpec(
                "normalization",
                "cross-sectional percentile rank among eligible names",
                "str",
            ),
            ParameterSpec("eligibility", "all three components computable", "str"),
            ParameterSpec("tie_break", "frozen universe order", "str"),
            ParameterSpec("max_positions", 10, "int"),
            ParameterSpec("max_position_fraction", _dec("0.10"), "decimal"),
            ParameterSpec("minimum_price_sessions", 1, "int"),
            ParameterSpec("minimum_eligible_fraction", _dec("0.60"), "decimal"),
        ),
        capabilities=frozenset({CAP_SEC_EDGAR}),
    ),
    StrategyEntry(
        name=challenger_strategies.ShortTermMeanReversionV1Strategy.name,
        implementation_name=challenger_strategies.ShortTermMeanReversionV1Strategy.name,
        implementation=challenger_strategies.ShortTermMeanReversionV1Strategy,
        parameters=(
            ParameterSpec("rebalance_cadence", "daily", "str"),
            ParameterSpec("short_ma", 20, "int"),
            ParameterSpec("long_ma", 200, "int"),
            ParameterSpec("entry_dip", _dec("0.05"), "decimal"),
            ParameterSpec("exit_recovery_band", _dec("0.01"), "decimal"),
            ParameterSpec("exit_on_long_ma_break", True, "bool"),
            ParameterSpec(
                "ranking_metric",
                "most negative close-to-short-average distance",
                "str",
            ),
            ParameterSpec("tie_break", "frozen universe order", "str"),
            ParameterSpec("max_positions", 5, "int"),
            ParameterSpec("max_position_fraction", _dec("0.20"), "decimal"),
            ParameterSpec("minimum_price_sessions", 201, "int"),
            ParameterSpec("minimum_eligible_fraction", _dec("0.60"), "decimal"),
        ),
        capabilities=frozenset({CAP_DAILY_HISTORY}),
    ),
)

_ENTRIES: dict[str, StrategyEntry] = {entry.name: entry for entry in _ENTRY_LIST}
_BY_IMPLEMENTATION: dict[str, StrategyEntry] = {
    entry.implementation_name: entry for entry in _ENTRY_LIST
}


# --- runtime resources ------------------------------------------------------


@dataclass(frozen=True)
class StrategyResources:
    """Runtime dependencies injected by the caller to satisfy capabilities.

    The registry never fetches these; the caller resolves the ones a strategy
    declares (via :func:`resources_for`) and passes them in. A declared
    capability with no matching resource makes :func:`build` fail closed.
    """

    history: dict[str, list[Candle]] | None = None
    benchmark_history: list[Candle] | None = None
    store: SecStore | None = None
    llm_builder: LlmBuilder | None = None


# --- queries ----------------------------------------------------------------


def entry(name: str) -> StrategyEntry:
    """Return the registry entry for ``name`` or raise :class:`UnknownStrategyError`."""
    try:
        return _ENTRIES[name]
    except KeyError as exc:
        raise UnknownStrategyError(f"Unknown strategy '{name}'.") from exc


def is_registered(name: str) -> bool:
    return name in _ENTRIES


def paper_strategy_names() -> list[str]:
    """Every strategy that can run in a paper sleeve (the authoritative list)."""
    return [e.name for e in _ENTRY_LIST]


def simple_strategy_names() -> list[str]:
    """Strategies buildable from just a universe (no resources, no LLM), sorted."""
    return sorted(e.name for e in _ENTRY_LIST if e.is_simple)


def rule_backtest_strategy_names() -> list[str]:
    """Deterministic rule strategies usable by the historical backtest/validate path."""
    return [e.name for e in _ENTRY_LIST if e.in_rule_backtest]


def live_strategy_names() -> list[str]:
    """Strategies eligible for the assisted live/propose path."""
    return [e.name for e in _ENTRY_LIST if e.in_live]


def history_strategy_names() -> list[str]:
    """Strategies that require daily price history."""
    return [e.name for e in _ENTRY_LIST if CAP_DAILY_HISTORY in e.capabilities]


def edgar_strategy_names() -> list[str]:
    """Strategies that require the SEC EDGAR store."""
    return [e.name for e in _ENTRY_LIST if CAP_SEC_EDGAR in e.capabilities]


def requires_history(name: str) -> bool:
    return CAP_DAILY_HISTORY in entry(name).capabilities


def requires_edgar(name: str) -> bool:
    return CAP_SEC_EDGAR in entry(name).capabilities


# --- parameter validation ---------------------------------------------------


def validated_parameters(
    name: str, parameters: Mapping[str, object] | None = None
) -> dict[str, Any]:
    """Return the full, typed parameter set for ``name``.

    Starts from the schema defaults and applies ``parameters`` on top. Unknown
    keys and un-coercible values fail closed so stored parameters can only change
    behavior through validated fields.
    """
    target = entry(name)
    known = {spec.name: spec for spec in target.parameters}
    values: dict[str, Any] = {spec.name: spec.default for spec in target.parameters}
    for key, raw in (parameters or {}).items():
        spec = known.get(key)
        if spec is None:
            allowed = ", ".join(known) or "(none)"
            raise UnknownParameterError(
                f"Strategy '{name}' has no parameter '{key}'. Allowed: {allowed}."
            )
        values[key] = _coerce(spec, raw)
    return values


def sleeve_parameter_values(
    name: str,
    *,
    max_positions: int,
    max_position_fraction: Decimal,
    factor: str = "",
) -> dict[str, Any]:
    """Map the sleeve/backtest knobs onto the parameters an entry actually accepts.

    A knob is applied only when the entry lists it in ``sleeve_parameters``;
    everything else keeps its schema default. This reproduces the historical
    construction exactly (e.g. ``dip-buyer`` ignores the sleeve max positions and
    ``tactical`` takes neither knob, while ``factor`` only reaches the fundamental
    and value-momentum strategies).
    """
    target = entry(name)
    values: dict[str, Any] = {}
    if "max_positions" in target.sleeve_parameters:
        values["max_positions"] = max_positions
    if "max_position_fraction" in target.sleeve_parameters:
        values["max_position_fraction"] = max_position_fraction
    if "factor" in target.sleeve_parameters and factor:
        values["factor"] = factor
    return values


# --- construction -----------------------------------------------------------


def build(
    name: str,
    universe: list[str],
    *,
    parameters: Mapping[str, object] | None = None,
    resources: StrategyResources | None = None,
) -> Strategy:
    """Construct a paper strategy from validated parameters and injected resources.

    Raises:
        UnknownStrategyError: the name is not registered.
        UnknownParameterError / ParameterValidationError: bad stored parameters.
        MissingCapabilityError: a declared runtime dependency was not supplied.
    """
    target = entry(name)
    resources = resources or StrategyResources()
    values = validated_parameters(name, parameters)
    symbols = list(universe)

    if target.is_llm:
        if resources.llm_builder is None:
            raise MissingCapabilityError(
                f"Strategy '{name}' needs an LLM provider, but none was supplied."
            )
        return resources.llm_builder(symbols, values)

    kwargs: dict[str, Any] = dict(values)
    if CAP_DAILY_HISTORY in target.capabilities:
        if resources.history is None or resources.benchmark_history is None:
            raise MissingCapabilityError(
                f"Strategy '{name}' needs daily price history, which was not provided. "
                "Fetch history for the universe and benchmark first."
            )
        kwargs["history"] = resources.history
        kwargs["benchmark_history"] = resources.benchmark_history
    if CAP_SEC_EDGAR in target.capabilities:
        if resources.store is None:
            raise MissingCapabilityError(
                f"Strategy '{name}' needs SEC EDGAR data, which was not provided. "
                "Run 'schwab-trader edgar fetch' first."
            )
        kwargs["store"] = resources.store

    if target.implementation is None:  # pragma: no cover - defensive
        raise StrategyRegistryError(f"Strategy '{name}' has no constructable implementation.")
    return target.implementation(symbols, **kwargs)


def build_simple(name: str, universe: list[str]) -> Strategy:
    """Construct a universe-only strategy.

    Preserves the historical ``agent.build_strategy`` contract: it raises
    :class:`KeyError` for any name that is not a simple (resource-free) strategy,
    so callers that only support the simple set keep failing the same way.
    """
    target = _ENTRIES.get(name)
    if target is None or not target.is_simple:
        raise KeyError(name)
    return build(name, universe)


# --- definitions ------------------------------------------------------------


def make_definition(
    name: str,
    *,
    universe_definition: ConfigurationValue,
    parameters: Mapping[str, object] | None = None,
    strategy_version: str = "1",
    benchmark_symbol_or_sleeve: str = "SPY",
    decision_frequency: str = "daily",
    decision_time: time = time(16, 0),
    long_only: bool = True,
    leverage_allowed: bool = False,
) -> StrategyDefinition:
    """Build a versioned :class:`StrategyDefinition` for a registered strategy.

    The implementation name and data-requirement capabilities are filled from the
    registry so a definition always agrees with the code that will rebuild it.
    """
    target = entry(name)
    values = validated_parameters(name, parameters)
    stored = {key: _to_configuration_value(value) for key, value in values.items()}
    return StrategyDefinition(
        strategy_id=name,
        strategy_version=strategy_version,
        implementation_name=target.implementation_name,
        parameters=stored,
        universe_definition=universe_definition,
        benchmark_symbol_or_sleeve=benchmark_symbol_or_sleeve,
        decision_frequency=decision_frequency,
        decision_time=decision_time,
        data_requirements=tuple(sorted(target.capabilities)),
        long_only=long_only,
        leverage_allowed=leverage_allowed,
    )


def _entry_for_definition(definition: StrategyDefinition) -> StrategyEntry:
    name = definition.implementation_name
    target = _ENTRIES.get(name) or _BY_IMPLEMENTATION.get(name)
    if target is None:
        raise UnknownStrategyError(
            f"Definition references unknown implementation '{name}'."
        )
    stored = set(definition.data_requirements)
    expected = set(target.capabilities)
    if stored != expected:
        raise DefinitionMismatchError(
            f"Definition for '{target.name}' declares data requirements {sorted(stored)}, "
            f"but the implementation requires {sorted(expected)}."
        )
    return target


def reconstruct(
    definition: StrategyDefinition,
    universe: list[str],
    *,
    resources: StrategyResources | None = None,
) -> Strategy:
    """Deterministically rebuild the strategy described by a stored definition.

    The stored parameters are validated against the current schema (unknown keys
    fail closed) and the declared data requirements are checked against the
    implementation before construction, so a definition can only change behavior
    through fields the implementation actually understands.
    """
    target = _entry_for_definition(definition)
    return build(target.name, universe, parameters=definition.parameters, resources=resources)
