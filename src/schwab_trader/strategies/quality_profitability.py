"""``quality-profitability-v1``: a point-in-time quality ranking of the large caps.

Frozen by ``docs/architecture/challenger-v1-contract.md`` §5.4 and its typed twin
:data:`schwab_trader.strategies.contract.QUALITY_PROFITABILITY`. Every limit,
weight, universe, and floor below is read from that contract rather than re-typed,
so this module cannot drift from the reviewed specification.

The strategy scores each name in the frozen 74-name ``large-cap`` universe on three
long-documented, non-overlapping profitability measures:

======================= ================================ ===================
component               definition                       source fields
======================= ================================ ===================
``gross-profitability`` gross profit TTM / total assets  Novy-Marx
``return-on-equity``    net income TTM / book equity     classic quality
``net-margin``          net income TTM / revenue TTM     classic quality
======================= ================================ ===================

All three are higher-is-better, equally weighted at one third each, and combined
through *cross-sectional percentile ranks* so that three quantities on wildly
different scales contribute equally. The top ten names are held at 10% each.

Three properties are load-bearing and are what the tests principally assert:

**No price enters the ranking.** All three components are pure fundamental ratios,
so a stale, missing, or wrong quote cannot change *which* names are selected. A
price is needed only to size the order, and #79 supplies the T+1 opening print for
the fill. This is the deliberate advantage over a value factor such as earnings
yield, which would need a point-in-time price to be meaningful.

**Point-in-time discipline.** Facts are read through
:meth:`~schwab_trader.sec_store.SecStore.point_in_time` and
:meth:`~schwab_trader.sec_store.SecStore.facts_as_of` with ``as_of`` set to signal
session T, which consider only filings dated on or before that date. A restatement
filed after T is invisible to T's decision permanently, and Schwab's
current-fundamentals endpoint is never substituted for what was actually known.
The store is *injected*; this module never opens, fetches, or writes anything.

**Missing components are reported, never imputed.** A name missing any one of the
three is ineligible and appears in :attr:`QualityRanking.missing_components`.
Scoring on partial components would let a company rank well on the single ratio it
happened to report, which is a data artifact rather than a quality signal; silently
dropping it would hide the artifact. If fewer than 60% of the universe is eligible
the whole sleeve fails closed for that session and proposes no orders at all.

Deliberately out of scope, and owned by the integration issue (#95): registration
in the strategy registry, the monthly last-XNYS-session calendar that decides *when*
:func:`QualityProfitabilityStrategy.plan` is called, cohort assembly, and runtime
resource wiring. This module owns only *what* to hold, given a session and a store.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from schwab_trader import fundamentals
from schwab_trader.market_data import Quote
from schwab_trader.sec_store import SecStore
from schwab_trader.strategies import contract, sizing

_SPEC = contract.QUALITY_PROFITABILITY

STRATEGY_NAME = _SPEC.name
STRATEGY_VERSION = _SPEC.version

#: The frozen 74-name universe snapshotted into the contract at freeze time. Read
#: from the contract rather than from ``universes.get_preset`` so that a later edit
#: to the mutable preset cannot retroactively redefine a running experiment.
UNIVERSE: tuple[str, ...] = _SPEC.universe

#: Component identifiers, in the fixed order that fixes the composite's summation
#: order. ``tests/test_quality_profitability.py`` pins these against the contract's
#: ``parameters["components"]``.
COMPONENTS: tuple[str, ...] = ("gross-profitability", "return-on-equity", "net-margin")

#: Equal weights, one third each. Held as a count rather than a float so the
#: composite is an exact Decimal mean.
COMPONENT_COUNT = len(COMPONENTS)

MAX_POSITIONS = _SPEC.max_positions
MAX_POSITION_FRACTION = _SPEC.max_position_fraction
COVERAGE_FLOOR = _SPEC.coverage_floor
GROSS_EXPOSURE_CAP = contract.GROSS_EXPOSURE_CAP


@dataclass(frozen=True)
class Components:
    """The three quality components for one company, ``None`` where not computable.

    A component is ``None`` only when a required SEC fact was not yet filed as of the
    signal session, or when its denominator is not strictly positive. A *negative*
    numerator is a real, valid, low reading - a company that lost money has a
    genuinely poor return on equity, not an unknown one - so losses rank last rather
    than being discarded.
    """

    gross_profitability: Decimal | None
    return_on_equity: Decimal | None
    net_margin: Decimal | None

    def value(self, component: str) -> Decimal | None:
        """The named component's value, or ``None`` when it is not computable."""
        if component == "gross-profitability":
            return self.gross_profitability
        if component == "return-on-equity":
            return self.return_on_equity
        if component == "net-margin":
            return self.net_margin
        known = ", ".join(COMPONENTS)
        raise ValueError(f"Unknown component '{component}'. Choose: {known}.")

    @property
    def missing(self) -> tuple[str, ...]:
        """Names of the components that are not computable, in ``COMPONENTS`` order."""
        return tuple(name for name in COMPONENTS if self.value(name) is None)

    @property
    def complete(self) -> bool:
        """True when all three components are computable - the eligibility rule."""
        return not self.missing


def components_as_of(store: SecStore, symbol: str, as_of: date) -> Components:
    """The three components for ``symbol`` using only facts filed on or before ``as_of``.

    Flow inputs (gross profit, net income, revenue) are trailing-twelve-month; the
    balance-sheet denominators (assets, equity) are the latest instant known as of
    the signal session. Both go through the existing canonical-field helpers, so this
    module adds no second fundamental-data system.
    """
    gross_profit = fundamentals.ttm_value(store, symbol, "gross_profit", as_of)
    net_income = fundamentals.ttm_value(store, symbol, "net_income", as_of)
    revenue = fundamentals.ttm_value(store, symbol, "revenue", as_of)
    assets = fundamentals.point_in_time_value(store, symbol, "assets", as_of)
    equity = fundamentals.point_in_time_value(store, symbol, "equity", as_of)
    return Components(
        gross_profitability=fundamentals.gross_profitability(gross_profit, assets),
        return_on_equity=fundamentals.return_on_equity(net_income, equity),
        net_margin=fundamentals.net_margin(net_income, revenue),
    )


def percentile_ranks(values: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """Cross-sectional percentile of each value in ``[0, 1]``, highest value -> 1.

    Ties receive the *average* of the ranks they span, so two companies reporting the
    same ratio always receive the same percentile. That is what makes the composite
    independent of the order a data provider happened to return names in: a
    rank-by-position scheme would hand tied names different percentiles depending on
    dictionary insertion order, and the difference would propagate into the selection.
    """
    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda item: item[1])
    count = len(ordered)
    if count == 1:
        return {ordered[0][0]: Decimal(1)}

    ranks: dict[str, Decimal] = {}
    start = 0
    span = Decimal(count - 1)
    while start < count:
        stop = start
        while stop + 1 < count and ordered[stop + 1][1] == ordered[start][1]:
            stop += 1
        # Mean of the 0-based positions this tied group spans, mapped onto [0, 1].
        percentile = (Decimal(start + stop) / 2) / span
        for symbol, _ in ordered[start : stop + 1]:
            ranks[symbol] = percentile
        start = stop + 1
    return ranks


@dataclass(frozen=True)
class QualityRanking:
    """The full, auditable record of one session's ranking decision.

    Everything needed to explain (or reproduce) a selection is here, including the
    names that did *not* qualify and exactly which component each of them was
    missing. ``selected`` is empty whenever :attr:`meets_coverage` is false.
    """

    as_of: date
    universe: tuple[str, ...]
    components: Mapping[str, Components]
    #: component -> symbol -> percentile, over the eligible names only.
    percentiles: Mapping[str, Mapping[str, Decimal]]
    #: symbol -> equally weighted composite, over the eligible names only.
    scores: Mapping[str, Decimal]
    eligible: tuple[str, ...]
    ineligible: tuple[str, ...]
    #: ineligible symbol -> the component names it could not supply.
    missing_components: Mapping[str, tuple[str, ...]]
    coverage: Decimal
    coverage_floor: Decimal
    ranked: tuple[str, ...]
    selected: tuple[str, ...]
    failure_reason: str | None

    @property
    def meets_coverage(self) -> bool:
        return self.coverage >= self.coverage_floor

    def explain(self, symbol: str) -> str:
        """A deterministic one-line rationale for ``symbol``'s composite.

        Shows each component's raw ratio and the percentile it earned, so a reviewer
        can reconstruct the composite from the line itself.
        """
        parts = [
            f"{name} {_fmt(self.components[symbol].value(name))}"
            f" (pctl {_fmt(self.percentiles[name].get(symbol))})"
            for name in COMPONENTS
        ]
        return f"composite {_fmt(self.scores.get(symbol))} = mean({', '.join(parts)})"


def _fmt(value: Decimal | None) -> str:
    """Render a Decimal for a rationale line: fixed 4 dp, or ``n/a``."""
    if value is None:
        return "n/a"
    return f"{value.quantize(Decimal('0.0001')):f}"


def rank(
    store: SecStore,
    as_of: date,
    *,
    universe: Sequence[str] = UNIVERSE,
    max_positions: int = MAX_POSITIONS,
    coverage_floor: Decimal = COVERAGE_FLOOR,
) -> QualityRanking:
    """Rank ``universe`` by the frozen quality composite as known on ``as_of``.

    ``as_of`` is signal session T's date; nothing filed after it can influence the
    result. The returned ranking reports every ineligible name and its missing
    components, and selects nothing at all when eligible coverage falls below
    ``coverage_floor``.
    """
    symbols = tuple(universe)
    components = {symbol: components_as_of(store, symbol, as_of) for symbol in symbols}

    eligible = tuple(symbol for symbol in symbols if components[symbol].complete)
    ineligible = tuple(symbol for symbol in symbols if not components[symbol].complete)
    missing_components = {symbol: components[symbol].missing for symbol in ineligible}

    coverage = Decimal(len(eligible)) / Decimal(len(symbols)) if symbols else Decimal(0)

    percentiles = {
        name: percentile_ranks(
            {symbol: _value(components[symbol], name) for symbol in eligible}
        )
        for name in COMPONENTS
    }
    scores = {
        symbol: sum(
            (percentiles[name][symbol] for name in COMPONENTS), Decimal(0)
        ) / COMPONENT_COUNT
        for symbol in eligible
    }

    # Rank through the shared helper so the tie-break is the frozen universe order
    # rather than a float's hash or a provider's ordering. The composite's exact
    # Decimal ordering is preserved: percentiles are rationals with denominators
    # bounded by 6*(n-1), far coarser than float precision at this magnitude.
    ranked = tuple(sizing.rank_desc({s: float(v) for s, v in scores.items()}, symbols))

    failure_reason: str | None = None
    if not symbols:
        failure_reason = "the universe is empty"
    elif coverage < coverage_floor:
        failure_reason = (
            f"eligible coverage {_pct(coverage)} is below the {_pct(coverage_floor)} "
            f"floor ({len(eligible)}/{len(symbols)} names have all "
            f"{COMPONENT_COUNT} components); the sleeve fails closed"
        )

    selected = () if failure_reason is not None else ranked[:max_positions]

    return QualityRanking(
        as_of=as_of,
        universe=symbols,
        components=components,
        percentiles=percentiles,
        scores=scores,
        eligible=eligible,
        ineligible=ineligible,
        missing_components=missing_components,
        coverage=coverage,
        coverage_floor=coverage_floor,
        ranked=ranked,
        selected=selected,
        failure_reason=failure_reason,
    )


def _value(components: Components, name: str) -> Decimal:
    """A component known to be present (eligibility guarantees it)."""
    value = components.value(name)
    assert value is not None, f"{name} is missing for an eligible name"
    return value


def _pct(fraction: Decimal) -> str:
    return f"{(fraction * 100).quantize(Decimal('0.1')):f}%"


@dataclass(frozen=True)
class QualityPlan:
    """One session's orders plus the ranking and the reason they were (not) produced."""

    ranking: QualityRanking
    orders: tuple[sizing.PlannedOrder, ...]
    #: False when the selected set equals the currently held set - a no-op session.
    selection_changed: bool
    #: Selected names that could not be priced, and so were not sized into an order.
    unpriced: tuple[str, ...]
    reason: str


class QualityProfitabilityStrategy:
    """The ``quality-profitability-v1`` sleeve: rank, select ten, hold at 10% each.

    Construction takes only the injected SEC store; every limit comes from the frozen
    contract. The overridable keyword arguments exist so tests can exercise the rules
    on a small synthetic universe - they are *not* tuning knobs, and the integration
    issue registers this strategy with the contract defaults.
    """

    name = STRATEGY_NAME
    version = STRATEGY_VERSION

    def __init__(
        self,
        store: SecStore,
        *,
        universe: Sequence[str] = UNIVERSE,
        max_positions: int = MAX_POSITIONS,
        max_position_fraction: Decimal = MAX_POSITION_FRACTION,
        coverage_floor: Decimal = COVERAGE_FLOOR,
    ) -> None:
        self._store = store
        self.universe = tuple(symbol.strip().upper() for symbol in universe if symbol.strip())
        self.max_positions = max_positions
        self.max_position_fraction = max_position_fraction
        self.coverage_floor = coverage_floor

    def rank(self, as_of: date) -> QualityRanking:
        """The ranking for signal session ``as_of``, without proposing any orders."""
        return rank(
            self._store,
            as_of,
            universe=self.universe,
            max_positions=self.max_positions,
            coverage_floor=self.coverage_floor,
        )

    def plan(
        self,
        *,
        as_of: date,
        positions: Mapping[str, int],
        quotes: Mapping[str, Quote],
        equity: Decimal,
        cash: Decimal,
        spendable: Decimal | None = None,
        leverage: Decimal = Decimal(1),
    ) -> QualityPlan:
        """Plan session ``as_of``'s orders: long-only, whole-share, 10 names at 10%.

        ``quotes`` are used **only** to size and price the orders - they cannot reach
        the ranking, which is computed before they are consulted. Three outcomes are
        possible and each is reported rather than inferred from an empty order list:
        the sleeve fails closed below the coverage floor, the selected set is
        unchanged from what is already held (a no-op month), or orders are proposed.
        """
        ranking = self.rank(as_of)
        budget = cash if spendable is None else spendable

        if ranking.failure_reason is not None:
            return QualityPlan(
                ranking=ranking,
                orders=(),
                selection_changed=False,
                unpriced=(),
                reason=ranking.failure_reason,
            )

        held = frozenset(symbol for symbol, quantity in positions.items() if quantity > 0)
        if held == frozenset(ranking.selected):
            return QualityPlan(
                ranking=ranking,
                orders=(),
                selection_changed=False,
                unpriced=(),
                reason=(
                    f"selection unchanged ({len(ranking.selected)} names); "
                    "no rebalance this session"
                ),
            )

        planned = sizing.plan_rebalance(
            targets=list(ranking.selected),
            positions=positions,
            quotes=quotes,
            equity=equity,
            cash=cash,
            spendable=budget,
            leverage=leverage,
            gross_cap=GROSS_EXPOSURE_CAP,
            max_position_fraction=self.max_position_fraction,
            exit_reason=f"fell out of the top {self.max_positions} by quality composite",
            enter_reason=f"top {self.max_positions} by quality composite",
        )
        orders = tuple(
            dataclasses.replace(
                order,
                rationale=f"{order.rationale}: {ranking.explain(order.request.symbol)}"
                if order.request.symbol in ranking.scores
                else order.rationale,
            )
            for order in planned
        )

        bought = {order.request.symbol for order in orders}
        unpriced = tuple(
            symbol for symbol in ranking.selected if symbol not in held and symbol not in bought
        )
        return QualityPlan(
            ranking=ranking,
            orders=orders,
            selection_changed=True,
            unpriced=unpriced,
            reason=(
                f"rebalancing to the top {self.max_positions} of "
                f"{len(ranking.eligible)} eligible names "
                f"({_pct(ranking.coverage)} coverage)"
            ),
        )
