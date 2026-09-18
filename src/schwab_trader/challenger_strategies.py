"""Registry-compatible adapters and the frozen rebalance cadence for challenger-v1.

Issues #92, #93, and #94 delivered three strategy *algorithms*. Each one deliberately
stopped short of three things its own docstring names as belonging here: registration in
:mod:`schwab_trader.strategy_registry`, the calendar that decides *when* it is allowed to
rebalance, and a constructor signature the registry can actually call. This module
supplies exactly those three and nothing else.

**No algorithm is reimplemented.** Every adapter delegates the decision to the merged
module it wraps and only decides whether that decision is allowed to produce orders this
session. If an adapter and its inner strategy ever disagree about a number, the inner
strategy wins, because the inner strategy reads the frozen contract directly.

Three integration problems are solved here:

*The registry calls one constructor shape.*
    :func:`schwab_trader.strategy_registry.build` passes ``history`` **and**
    ``benchmark_history`` to every strategy declaring ``daily-price-history``, and passes
    ``store`` to every strategy declaring ``sec-edgar-facts``. The merged strategies take
    ``history`` only, or take the store positionally and are not
    :class:`~schwab_trader.agent.Strategy` subclasses at all. The adapters normalize that.

*The cohort rebalances on a cadence, not every session.*
    challenger-v1 evaluates and marks every sleeve every session — that is what produces
    a comparable daily equity curve, and it is why ``contract.DECISION_FREQUENCY`` is
    ``"daily"`` for the whole cohort. What differs per sleeve is how often it is allowed
    to *trade*: monthly for dual momentum and quality, every session for mean reversion.
    :class:`CadenceGate` is that distinction, and it is derived from the one XNYS calendar
    in :mod:`schwab_trader.market_calendar` rather than a second calendar.

*A stored definition must not be able to redefine a frozen experiment.*
    Every frozen parameter the registry persists is verified against
    :mod:`schwab_trader.strategies.contract` at construction time. A definition carrying a
    different lookback, threshold, or position limit raises
    :class:`ContractViolationError` instead of quietly running a different experiment
    under a frozen name.

Pure: no I/O, no clock reads, no storage, no network. The session is read from the
decision instant the caller supplies.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from schwab_trader import market_calendar
from schwab_trader.agent import MarketContext, OrderProposal, Strategy
from schwab_trader.market_data import Candle
from schwab_trader.sec_store import SecStore
from schwab_trader.strategies import (
    contract,
    dual_momentum,
    quality_profitability,
    short_term_mean_reversion,
)

__all__ = [
    "CadenceGate",
    "ContractViolationError",
    "DualMomentumV1Strategy",
    "QualityProfitabilityV1Strategy",
    "RebalanceCadence",
    "ShortTermMeanReversionV1Strategy",
    "is_last_trading_session_of_month",
    "signal_session_date",
]


class ContractViolationError(ValueError):
    """A stored parameter disagrees with the frozen ``challenger-v1`` contract.

    Raised rather than tolerated: a sleeve whose persisted definition no longer matches
    the contract is not a mis-typed knob, it is a different experiment wearing a frozen
    name. The official runner turns this into a failed member rather than recording an
    observation that cannot be reproduced.
    """


class RebalanceCadence(StrEnum):
    """How often a sleeve is allowed to propose orders.

    Stored as the ``rebalance_cadence`` parameter of every challenger definition, so the
    cadence is part of the configuration hash rather than an implicit property of the
    code that happens to be running.
    """

    NEVER = "never"
    """Never trades. The accounting control."""

    DAILY = "daily"
    """Every XNYS session."""

    MONTHLY = "monthly"
    """Only on the last XNYS session of a calendar month."""

    BUY_ONCE = "buy-once"
    """Buys on the first session it can, then holds. Enforced by the strategy itself
    (:class:`~schwab_trader.agent.BuyHoldStrategy` proposes nothing once it is fully
    invested), so the gate never blocks it."""


def is_last_trading_session_of_month(day: date) -> bool:
    """True when ``day`` is an XNYS session and the next one falls in a later month.

    Derived entirely from :mod:`schwab_trader.market_calendar`, so weekends, observed
    holidays, and observance shifts are handled by the one calendar the rest of the
    project already uses. An early close is a full session for this purpose — a 13:00 ET
    close still produces an official closing print, so December 24 can be a month end.
    """
    if not market_calendar.is_trading_day(day):
        return False
    following = market_calendar.next_trading_day(day)
    return (following.year, following.month) != (day.year, day.month)


def signal_session_date(now: datetime) -> date | None:
    """The signal session ``now`` belongs to, or ``None`` when it cannot be identified.

    The decision instant is session ``T``'s official close (16:00 ET, or 13:00 ET on an
    early close). Both convert to a UTC instant on the same calendar date in every US
    Eastern offset, so the UTC date *is* the session date. A naive instant cannot be
    placed on a session at all and returns ``None`` rather than being interpreted with a
    guessed timezone — the same rule
    :func:`schwab_trader.strategies.dual_momentum.DualMomentumStrategy.evaluate` applies.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        return None
    return now.astimezone(UTC).date()


class CadenceGate:
    """Whether a sleeve may propose orders for a given signal session.

    Deliberately one-way: the gate can only *suppress* a rebalance, never manufacture
    one. A suppressed session is a genuine no-op — the sleeve is still evaluated, still
    marked, and still records an observation — so the equity curve stays continuous and
    the cohort stays comparable.
    """

    def __init__(self, cadence: str | RebalanceCadence) -> None:
        try:
            self.cadence = RebalanceCadence(str(cadence).strip().casefold())
        except ValueError as exc:
            allowed = ", ".join(item.value for item in RebalanceCadence)
            raise ContractViolationError(
                f"'{cadence}' is not a known rebalance cadence. Allowed: {allowed}."
            ) from exc

    def allows(self, session: date | None) -> bool:
        """True when ``session`` is a rebalance session for this cadence.

        A ``None`` session (an unidentifiable decision instant) is refused for every
        cadence: an order proposed against a session that cannot be named is not
        auditable evidence.
        """
        if session is None:
            return False
        if self.cadence is RebalanceCadence.NEVER:
            return False
        if self.cadence is RebalanceCadence.MONTHLY:
            return is_last_trading_session_of_month(session)
        # DAILY and BUY_ONCE both evaluate every session; BUY_ONCE's "once" is a
        # property of the strategy's own state (it holds already), not of the calendar.
        return market_calendar.is_trading_day(session)

    def skip_reason(self, session: date | None) -> str:
        """Why this session is not a rebalance session, for the audit trail."""
        if session is None:
            return "the signal session could not be identified from the decision instant"
        if self.cadence is RebalanceCadence.NEVER:
            return "this sleeve never trades"
        if self.cadence is RebalanceCadence.MONTHLY:
            return (
                f"{session.isoformat()} is not the last XNYS session of "
                f"{session.strftime('%B %Y')}"
            )
        return f"{session.isoformat()} is not an XNYS trading session"


def _expect(name: str, actual: object, expected: object, sleeve: str) -> None:
    """Fail closed when a stored parameter differs from the frozen contract."""
    if actual != expected:
        raise ContractViolationError(
            f"{sleeve}.{name} is {actual!r}, but the frozen challenger-v1 contract "
            f"specifies {expected!r}. Refusing to run a redefined experiment under a "
            f"frozen name."
        )


def _expect_decimal(name: str, actual: object, expected: Decimal, sleeve: str) -> None:
    """``_expect`` for a value the registry may hand back as str/int/Decimal."""
    try:
        coerced = Decimal(str(actual))
    except (ArithmeticError, ValueError) as exc:
        raise ContractViolationError(
            f"{sleeve}.{name} is {actual!r}, which is not a number."
        ) from exc
    if coerced != expected:
        raise ContractViolationError(
            f"{sleeve}.{name} is {coerced}, but the frozen challenger-v1 contract "
            f"specifies {expected}. Refusing to run a redefined experiment under a "
            f"frozen name."
        )


class _ChallengerAdapter(Strategy):
    """Shared cadence plumbing for the three registered challenger sleeves."""

    #: The frozen sleeve this adapter is the registered form of. Set by each subclass.
    sleeve: contract.ChallengerSleeve

    def __init__(self, universe: Sequence[str], cadence: str) -> None:
        super().__init__(list(universe))
        self.gate = CadenceGate(cadence)
        self.rebalance_cadence = self.gate.cadence.value

    def _verify_common(
        self,
        *,
        max_positions: int,
        max_position_fraction: object,
        minimum_price_sessions: int,
        minimum_eligible_fraction: object,
    ) -> None:
        name = self.sleeve.name
        _expect("max_positions", max_positions, self.sleeve.max_positions, name)
        _expect_decimal(
            "max_position_fraction",
            max_position_fraction,
            self.sleeve.max_position_fraction,
            name,
        )
        _expect(
            "minimum_price_sessions",
            minimum_price_sessions,
            self.sleeve.minimum_price_sessions,
            name,
        )
        _expect_decimal(
            "minimum_eligible_fraction",
            minimum_eligible_fraction,
            self.sleeve.coverage_floor,
            name,
        )

    def session_for(self, context: MarketContext) -> date | None:
        return signal_session_date(context.now)

    def rebalances_on(self, session: date | None) -> bool:
        return self.gate.allows(session)


class DualMomentumV1Strategy(_ChallengerAdapter):
    """``dual-momentum-v1`` as the registry can build it, rebalancing monthly.

    Wraps :class:`schwab_trader.strategies.dual_momentum.DualMomentumStrategy` unchanged.
    ``benchmark_history`` is accepted because the registry supplies it to every
    history-backed strategy, and ignored because this sleeve ranks SPY as an ordinary
    member of its own risk universe rather than measuring against it.
    """

    name = dual_momentum.STRATEGY_NAME
    sleeve = contract.DUAL_MOMENTUM

    def __init__(
        self,
        universe: Sequence[str] | None = None,
        *,
        history: Mapping[str, list[Candle]],
        benchmark_history: Sequence[Candle] | None = None,
        rebalance_cadence: str = RebalanceCadence.MONTHLY.value,
        lookback_sessions: int = dual_momentum.LOOKBACK_SESSIONS,
        ranking_metric: str = "trailing-price-return",
        absolute_gate_metric: str = "trailing-price-return",
        absolute_gate_minimum: object = Decimal(0),
        tie_break: str = "frozen universe order",
        max_positions: int = contract.DUAL_MOMENTUM.max_positions,
        max_position_fraction: object = contract.DUAL_MOMENTUM.max_position_fraction,
        minimum_price_sessions: int = contract.DUAL_MOMENTUM.minimum_price_sessions,
        minimum_eligible_fraction: object = contract.DUAL_MOMENTUM.coverage_floor,
    ) -> None:
        del benchmark_history  # see the class docstring
        self._inner = dual_momentum.DualMomentumStrategy(
            list(universe) if universe else None, history=history
        )
        super().__init__(self._inner.universe, rebalance_cadence)

        name = self.sleeve.name
        _expect("lookback_sessions", lookback_sessions, dual_momentum.LOOKBACK_SESSIONS, name)
        _expect("ranking_metric", ranking_metric, self.sleeve.parameters["ranking_metric"], name)
        _expect(
            "absolute_gate_metric",
            absolute_gate_metric,
            self.sleeve.parameters["absolute_gate_metric"],
            name,
        )
        _expect_decimal(
            "absolute_gate_minimum",
            absolute_gate_minimum,
            self.sleeve.parameter_decimal("absolute_gate_minimum"),
            name,
        )
        _expect("tie_break", tie_break, self.sleeve.parameters["tie_break"], name)
        self._verify_common(
            max_positions=max_positions,
            max_position_fraction=max_position_fraction,
            minimum_price_sessions=minimum_price_sessions,
            minimum_eligible_fraction=minimum_eligible_fraction,
        )

    def evaluate(self, context: MarketContext) -> dual_momentum.DualMomentumDecision:
        """The inner sleeve's decision, regardless of cadence, for reporting."""
        return self._inner.evaluate(context)

    def decide(self, context: MarketContext) -> list[OrderProposal]:
        if not self.rebalances_on(self.session_for(context)):
            return []
        return self._inner.decide(context)


class QualityProfitabilityV1Strategy(_ChallengerAdapter):
    """``quality-profitability-v1`` as the registry can build it, rebalancing monthly.

    :class:`schwab_trader.strategies.quality_profitability.QualityProfitabilityStrategy`
    is not a :class:`~schwab_trader.agent.Strategy` — it exposes ``plan(as_of=...)``
    returning a :class:`~schwab_trader.strategies.quality_profitability.QualityPlan`
    rather than ``decide(context)``. This adapter is that mapping and nothing more: the
    ranking, eligibility rule, coverage floor, and sizing all stay in the merged module.

    ``leverage`` is the contract's, never the account's. challenger-v1 is unlevered at a
    pinned 100% gross for every member, so a sleeve run against a margin-configured
    engine still deploys exactly the exposure the experiment specifies.
    """

    name = quality_profitability.STRATEGY_NAME
    sleeve = contract.QUALITY_PROFITABILITY

    def __init__(
        self,
        universe: Sequence[str] | None = None,
        *,
        store: SecStore,
        rebalance_cadence: str = RebalanceCadence.MONTHLY.value,
        components: Sequence[str] | None = None,
        component_weights: Sequence[str] | None = None,
        normalization: str = "cross-sectional percentile rank among eligible names",
        eligibility: str = "all three components computable",
        tie_break: str = "frozen universe order",
        max_positions: int = contract.QUALITY_PROFITABILITY.max_positions,
        max_position_fraction: object = contract.QUALITY_PROFITABILITY.max_position_fraction,
        minimum_price_sessions: int = contract.QUALITY_PROFITABILITY.minimum_price_sessions,
        minimum_eligible_fraction: object = contract.QUALITY_PROFITABILITY.coverage_floor,
    ) -> None:
        symbols = list(universe) if universe else list(quality_profitability.UNIVERSE)
        super().__init__(symbols, rebalance_cadence)

        name = self.sleeve.name
        _expect(
            "components",
            list(components) if components is not None else list(quality_profitability.COMPONENTS),
            list(quality_profitability.COMPONENTS),
            name,
        )
        _expect(
            "component_weights",
            list(component_weights)
            if component_weights is not None
            else list(self.sleeve.parameters["component_weights"]),  # type: ignore[arg-type]
            list(self.sleeve.parameters["component_weights"]),  # type: ignore[arg-type]
            name,
        )
        _expect("normalization", normalization, self.sleeve.parameters["normalization"], name)
        _expect("eligibility", eligibility, self.sleeve.parameters["eligibility"], name)
        _expect("tie_break", tie_break, self.sleeve.parameters["tie_break"], name)
        self._verify_common(
            max_positions=max_positions,
            max_position_fraction=max_position_fraction,
            minimum_price_sessions=minimum_price_sessions,
            minimum_eligible_fraction=minimum_eligible_fraction,
        )

        self._inner = quality_profitability.QualityProfitabilityStrategy(
            store,
            universe=self.universe,
            max_positions=self.sleeve.max_positions,
            max_position_fraction=self.sleeve.max_position_fraction,
            coverage_floor=self.sleeve.coverage_floor,
        )

    def rank(self, as_of: date) -> quality_profitability.QualityRanking:
        """The inner sleeve's ranking, regardless of cadence, for reporting."""
        return self._inner.rank(as_of)

    def plan(self, context: MarketContext) -> quality_profitability.QualityPlan | None:
        """This session's plan, or ``None`` when the cadence suppresses a rebalance."""
        session = self.session_for(context)
        if not self.rebalances_on(session):
            return None
        assert session is not None  # rebalances_on rejects an unidentifiable session
        return self._inner.plan(
            as_of=session,
            positions=context.positions,
            quotes=context.quotes,
            equity=context.equity,
            cash=context.cash,
            spendable=context.spendable,
            leverage=contract.LEVERAGE,
        )

    def decide(self, context: MarketContext) -> list[OrderProposal]:
        planned = self.plan(context)
        if planned is None:
            return []
        return [
            OrderProposal(request=order.request, rationale=order.rationale)
            for order in planned.orders
        ]


class ShortTermMeanReversionV1Strategy(_ChallengerAdapter):
    """``short-term-mean-reversion-v1`` as the registry can build it, rebalancing daily.

    Wraps
    :class:`schwab_trader.strategies.short_term_mean_reversion.ShortTermMeanReversionStrategy`
    unchanged. The cadence gate is a no-op for a daily sleeve and is kept anyway so the
    cadence is an explicit, hashed part of every challenger definition rather than an
    absence that has to be inferred.
    """

    name = short_term_mean_reversion.ShortTermMeanReversionStrategy.name
    sleeve = contract.SHORT_TERM_MEAN_REVERSION

    def __init__(
        self,
        universe: Sequence[str] | None = None,
        *,
        history: Mapping[str, list[Candle]],
        benchmark_history: Sequence[Candle] | None = None,
        rebalance_cadence: str = RebalanceCadence.DAILY.value,
        short_ma: int = short_term_mean_reversion.SHORT_MA,
        long_ma: int = short_term_mean_reversion.LONG_MA,
        entry_dip: object = short_term_mean_reversion.ENTRY_DIP,
        exit_recovery_band: object = short_term_mean_reversion.EXIT_RECOVERY_BAND,
        exit_on_long_ma_break: bool = True,
        ranking_metric: str = "most negative close-to-short-average distance",
        tie_break: str = "frozen universe order",
        max_positions: int = contract.SHORT_TERM_MEAN_REVERSION.max_positions,
        max_position_fraction: object = contract.SHORT_TERM_MEAN_REVERSION.max_position_fraction,
        minimum_price_sessions: int = contract.SHORT_TERM_MEAN_REVERSION.minimum_price_sessions,
        minimum_eligible_fraction: object = contract.SHORT_TERM_MEAN_REVERSION.coverage_floor,
    ) -> None:
        del benchmark_history  # supplied to every history-backed strategy; unused here
        symbols = list(universe) if universe else list(short_term_mean_reversion.DEFAULT_UNIVERSE)
        super().__init__(symbols, rebalance_cadence)

        name = self.sleeve.name
        _expect("short_ma", short_ma, short_term_mean_reversion.SHORT_MA, name)
        _expect("long_ma", long_ma, short_term_mean_reversion.LONG_MA, name)
        _expect_decimal("entry_dip", entry_dip, short_term_mean_reversion.ENTRY_DIP, name)
        _expect_decimal(
            "exit_recovery_band",
            exit_recovery_band,
            short_term_mean_reversion.EXIT_RECOVERY_BAND,
            name,
        )
        _expect(
            "exit_on_long_ma_break",
            exit_on_long_ma_break,
            self.sleeve.parameters["exit_on_long_ma_break"],
            name,
        )
        _expect("ranking_metric", ranking_metric, self.sleeve.parameters["ranking_metric"], name)
        _expect("tie_break", tie_break, self.sleeve.parameters["tie_break"], name)
        self._verify_common(
            max_positions=max_positions,
            max_position_fraction=max_position_fraction,
            minimum_price_sessions=minimum_price_sessions,
            minimum_eligible_fraction=minimum_eligible_fraction,
        )

        self._inner = short_term_mean_reversion.ShortTermMeanReversionStrategy(
            self.universe, history=dict(history)
        )

    def evaluate(self, context: MarketContext) -> short_term_mean_reversion.Evaluation:
        """The inner sleeve's evaluation, regardless of cadence, for reporting."""
        return self._inner.evaluate(context)

    def decide(self, context: MarketContext) -> list[OrderProposal]:
        if not self.rebalances_on(self.session_for(context)):
            return []
        return self._inner.decide(context)


#: Every adapter, keyed by the registered strategy name. Used by the registry entries and
#: by the cohort bootstrap so neither has to re-list them.
ADAPTERS: Mapping[str, type[Strategy]] = {
    DualMomentumV1Strategy.name: DualMomentumV1Strategy,
    QualityProfitabilityV1Strategy.name: QualityProfitabilityV1Strategy,
    ShortTermMeanReversionV1Strategy.name: ShortTermMeanReversionV1Strategy,
}


def frozen_parameters(sleeve: contract.ChallengerSleeve, *, cadence: RebalanceCadence) -> dict[
    str, Any
]:
    """The complete frozen parameter set persisted for ``sleeve``'s definition.

    Everything the contract fixes for this sleeve, plus the cadence, in one mapping. It
    is what makes a challenger definition *reconstructable*: the configuration hash then
    covers every value the strategy will read, so a definition whose hash matches cannot
    describe a different experiment.
    """
    values: dict[str, Any] = dict(sleeve.parameters)
    values["rebalance_cadence"] = cadence.value
    values["max_positions"] = sleeve.max_positions
    values["max_position_fraction"] = sleeve.max_position_fraction
    values["minimum_price_sessions"] = sleeve.minimum_price_sessions
    values["minimum_eligible_fraction"] = sleeve.minimum_eligible_fraction
    return values
