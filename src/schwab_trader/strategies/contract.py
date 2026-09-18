"""The frozen ``challenger-v1`` experiment contract, as typed hashable data.

``docs/architecture/challenger-v1-contract.md`` is the authoritative prose
specification and states the rationale for every number here. This module is its
machine-readable twin: the three challenger strategies read their universe,
limits, cadence, lookbacks, and coverage floor from these constants instead of
re-typing them, so the document and the code cannot drift apart.

Everything in this module is frozen. Changing any value changes
:func:`contract_hash`, which is asserted against a pinned digest in
``tests/test_challenger_contract.py`` - so an edit here fails the suite until it
is made deliberately and reviewed. That is the intended friction: challenger-v1
is an experiment whose definition must be fixed *before* it collects evidence,
not adjusted afterwards to suit the evidence.

Deliberately absent: any strategy implementation, any registry entry, any cohort
creation, and any I/O. Those belong to issues #92-#95.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from decimal import Decimal

from schwab_trader import universes
from schwab_trader.experiments import (
    ConfigurationValue,
    deterministic_configuration_hash,
    normalize_configuration,
)

CONTRACT_ID = "challenger-v1"
CONTRACT_VERSION = "1"

# --- cohort-level assumptions ------------------------------------------------
#
# Every sleeve is funded independently. Five sleeves x $10,000 is five separate
# simulations, NOT $50,000 of pooled or real capital, and none of it is reserved
# in, divided from, or linked to the real brokerage account.
STARTING_CASH_PER_SLEEVE = Decimal("10000.00")
SLEEVE_COUNT = 5

LONG_ONLY = True
LEVERAGE_ALLOWED = False
LEVERAGE = Decimal("1")
WHOLE_SHARES_ONLY = True

# No regime/volatility overlay in challenger-v1. The existing paper sleeves scale
# exposure by ``signals.regime_signal``; that is a second, independent timing bet
# layered on top of the strategy being tested. Each challenger sleeve tests
# exactly one idea, so the gross exposure cap is pinned at 100%.
GROSS_EXPOSURE_CAP = Decimal("1.00")

SETTLEMENT_MODEL = "T+1"
BENCHMARK_SLEEVE = "bench-spy"

# --- timing ------------------------------------------------------------------
#
# Signal on session T's close; execute at session T+1's open. Implemented by
# issue #79, which owns the execution-timing engine; challenger-v1 depends on it
# and must not introduce a second one.
SIGNAL_SESSION_TIME = time(16, 0)
SIGNAL_BASIS = "official XNYS close of signal session T (13:00 ET on early closes)"
EXECUTION_BASIS = "official XNYS opening print of the next trading session, T+1"
VALUATION_BASIS = "official XNYS close of every trading session, marked after the close"
DECISION_FREQUENCY = "daily"
REQUIRES_T1_OPEN_EXECUTION = True

# --- transaction costs -------------------------------------------------------
#
# Retail equity/ETF commissions are zero, so the modeled cost is spread plus
# slippage only. 5 bps per side (10 bps round trip) is charged symmetrically as a
# half-spread around the T+1 opening print. It is deliberately conservative for
# mega-cap ETFs (SPY's quoted spread is well under 1 bp) and roughly realistic for
# liquid large caps at the open, which is the least liquid moment of the session.
#
# One uniform cost applies to all five sleeves, including bench-spy. A cheaper
# assumption for the benchmark than for the challengers would flatter the
# benchmark; a cheaper assumption for a high-turnover challenger would flatter the
# challenger. The daily-cadence sleeve is the most cost-sensitive by construction,
# and that is a property of the strategy the experiment is meant to reveal.
COST_MODEL_ID = "challenger-v1-open-fill-10bps-round-trip"
COST_BPS_PER_SIDE = Decimal("5")
COST_BPS_ROUND_TRIP = Decimal("10")

# --- data requirements -------------------------------------------------------

CAP_DAILY_HISTORY = "daily-price-history"
CAP_SEC_EDGAR = "sec-edgar-facts"

# A symbol's most recent close must be session T's close. An older last bar is
# stale evidence, not a usable price, and makes the symbol ineligible.
MAX_PRICE_STALENESS_SESSIONS = 0

# Two different values for the same symbol and session indicate a data-integrity
# fault rather than a gap. The whole sleeve fails closed for that session; it does
# not quietly pick one of the two.
CONFLICTING_EVIDENCE_FAILS_SLEEVE = True


@dataclass(frozen=True)
class ChallengerSleeve:
    """One frozen member of the challenger-v1 cohort.

    ``owner`` names the GitHub issue permitted to implement this sleeve, and
    ``module`` the single module path it may create. Together they are the
    file-ownership contract that lets #92, #93, and #94 run concurrently.
    """

    name: str
    version: str
    role: str
    owner: str
    module: str
    universe_label: str
    universe: tuple[str, ...]
    defensive_universe: tuple[str, ...]
    max_positions: int
    max_position_fraction: Decimal
    rebalance_cadence: str
    minimum_price_sessions: int
    minimum_eligible_fraction: str
    data_requirements: tuple[str, ...]
    parameters: dict[str, ConfigurationValue]

    @property
    def coverage_floor(self) -> Decimal:
        """``minimum_eligible_fraction`` as a number.

        The field is stored as a string so the configuration hash is exact and
        JSON-stable. Parsing it here means the three strategy implementations
        cannot each choose a slightly different conversion.
        """
        return Decimal(self.minimum_eligible_fraction)

    def parameter_decimal(self, name: str) -> Decimal:
        """A frozen numeric parameter as a ``Decimal``.

        Thresholds live in ``parameters`` as strings for the same hashing reason
        as ``minimum_eligible_fraction``; this is the one supported way to read
        them back as numbers.
        """
        value = self.parameters[name]
        # ``bool`` is a subclass of ``int``, so it must be rejected explicitly or
        # ``exit_on_long_ma_break`` would silently read back as Decimal(1).
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise TypeError(f"{self.name}.{name} is not a numeric parameter")
        return Decimal(value)

    def payload(self) -> dict[str, ConfigurationValue]:
        """Every decision-relevant field in its normalized hash representation."""
        normalized = normalize_configuration(
            {
                "data_requirements": list(self.data_requirements),
                "defensive_universe": list(self.defensive_universe),
                "max_position_fraction": str(self.max_position_fraction),
                "max_positions": self.max_positions,
                "minimum_eligible_fraction": self.minimum_eligible_fraction,
                "minimum_price_sessions": self.minimum_price_sessions,
                "name": self.name,
                "parameters": self.parameters,
                "rebalance_cadence": self.rebalance_cadence,
                "universe": list(self.universe),
                "universe_label": self.universe_label,
                "version": self.version,
            }
        )
        assert isinstance(normalized, dict)
        return normalized


# The large-cap preset is referenced rather than copied so the challenger sleeves
# and the existing sleeves trade the same named basket. It is pinned by hash in
# ``tests/test_challenger_contract.py``: editing ``universes.py`` therefore fails
# the suite loudly instead of silently redefining a frozen experiment.
_LARGE_CAP: tuple[str, ...] = tuple(universes.get_preset("large-cap") or ())


CONTROL_CASH = ChallengerSleeve(
    name="control-cash",
    version="1",
    role="accounting and no-trade control",
    owner="#95",
    module="(none - reuses the registered 'hold' strategy)",
    universe_label="SPY",
    universe=("SPY",),
    defensive_universe=(),
    # The 'hold' implementation accepts neither parameter and never holds a
    # position; zeros record that intent rather than a limit that could bind.
    max_positions=0,
    max_position_fraction=Decimal("0"),
    rebalance_cadence="never",
    minimum_price_sessions=0,
    minimum_eligible_fraction="0",
    data_requirements=(),
    parameters={},
)

BENCH_SPY = ChallengerSleeve(
    name="bench-spy",
    version="1",
    role="passive market benchmark",
    owner="#95",
    module="(none - reuses the registered 'buy-hold' strategy)",
    universe_label="SPY",
    universe=("SPY",),
    defensive_universe=(),
    max_positions=1,
    max_position_fraction=Decimal("1.00"),
    rebalance_cadence="buy once on the first execution session, then hold",
    minimum_price_sessions=1,
    minimum_eligible_fraction="1",
    data_requirements=(CAP_DAILY_HISTORY,),
    parameters={},
)

DUAL_MOMENTUM = ChallengerSleeve(
    name="dual-momentum-v1",
    version="1",
    role="multi-asset relative plus absolute momentum",
    owner="#92",
    module="schwab_trader/strategies/dual_momentum.py",
    universe_label="challenger-v1-multi-asset",
    # Four liquid, long-listed ETFs spanning the major directional asset classes.
    universe=("SPY", "EFA", "EEM", "VNQ"),
    # Held whenever the absolute-momentum gate fails. IEF rather than BIL: BIL's
    # return is almost entirely coupon, so on the price-return bars this project
    # actually has, BIL is indistinguishable from a flat line.
    defensive_universe=("IEF",),
    max_positions=1,
    max_position_fraction=Decimal("1.00"),
    rebalance_cadence="monthly, on the last XNYS session of the calendar month",
    # 252 sessions of return needs 253 closes.
    minimum_price_sessions=253,
    # A four-symbol universe cannot absorb a missing member: dropping one changes
    # which asset classes are even eligible to win. All five symbols required.
    minimum_eligible_fraction="1",
    data_requirements=(CAP_DAILY_HISTORY,),
    parameters={
        "lookback_sessions": 252,
        # Relative momentum: rank the risk universe by trailing price return.
        "ranking_metric": "trailing-price-return",
        # Absolute momentum: the winner must also have risen outright. Compared
        # against zero rather than a T-bill proxy, for the reason given above.
        "absolute_gate_metric": "trailing-price-return",
        "absolute_gate_minimum": "0",
        "tie_break": "frozen universe order",
    },
)

QUALITY_PROFITABILITY = ChallengerSleeve(
    name="quality-profitability-v1",
    version="1",
    role="point-in-time quality and profitability ranking",
    owner="#93",
    module="schwab_trader/strategies/quality_profitability.py",
    universe_label="large-cap",
    universe=_LARGE_CAP,
    defensive_universe=(),
    max_positions=10,
    max_position_fraction=Decimal("0.10"),
    rebalance_cadence=(
        "monthly, on the last XNYS session of the calendar month, and only when "
        "the selected set changes"
    ),
    # Ranking needs no price history at all; a price is needed only to size the
    # order, and #79 supplies the T+1 opening print for the fill.
    minimum_price_sessions=1,
    minimum_eligible_fraction="0.60",
    data_requirements=(CAP_DAILY_HISTORY, CAP_SEC_EDGAR),
    parameters={
        # Three equally weighted, higher-is-better components, each computable
        # from canonical SEC facts this repository already maps in
        # ``fundamentals.FIELD_CONCEPTS``. No price enters the ranking, so a
        # stale or missing quote cannot distort it.
        "components": [
            "gross-profitability",  # gross_profit TTM / assets
            "return-on-equity",  # net_income TTM / equity
            "net-margin",  # net_income TTM / revenue TTM
        ],
        "component_weights": ["1/3", "1/3", "1/3"],
        "normalization": "cross-sectional percentile rank among eligible names",
        # A name missing any component is ineligible and reported, never imputed
        # and never silently dropped. Partial-component scoring would let a
        # company rank well on the one ratio it happened to report.
        "eligibility": "all three components computable",
        "tie_break": "frozen universe order",
    },
)

SHORT_TERM_MEAN_REVERSION = ChallengerSleeve(
    name="short-term-mean-reversion-v1",
    version="1",
    role="short-horizon oversold entry inside a longer uptrend",
    owner="#94",
    module="schwab_trader/strategies/short_term_mean_reversion.py",
    universe_label="large-cap",
    universe=_LARGE_CAP,
    defensive_universe=(),
    max_positions=5,
    # 1/5 exactly, so a full book is fully invested. The registered
    # ``mean-reversion`` default of 0.10 would strand half the sleeve in cash and
    # make the comparison against a fully invested benchmark meaningless.
    max_position_fraction=Decimal("0.20"),
    rebalance_cadence="every XNYS session",
    # 200-session average needs 201 closes.
    minimum_price_sessions=201,
    minimum_eligible_fraction="0.60",
    data_requirements=(CAP_DAILY_HISTORY,),
    parameters={
        # The registered ``mean-reversion`` defaults, adopted unchanged. They
        # predate the July 27/28 cohorts and were not selected by looking at any
        # challenger outcome.
        "short_ma": 20,
        "long_ma": 200,
        "entry_dip": "0.05",
        # Explicit, deterministic exits. The registered implementation exits only
        # implicitly, when a name falls out of the target list.
        "exit_recovery_band": "0.01",
        "exit_on_long_ma_break": True,
        "ranking_metric": "most negative close-to-short-average distance",
        "tie_break": "frozen universe order",
    },
)

SLEEVES: tuple[ChallengerSleeve, ...] = (
    CONTROL_CASH,
    BENCH_SPY,
    DUAL_MOMENTUM,
    QUALITY_PROFITABILITY,
    SHORT_TERM_MEAN_REVERSION,
)

SLEEVE_NAMES: tuple[str, ...] = tuple(sleeve.name for sleeve in SLEEVES)

_BY_NAME: dict[str, ChallengerSleeve] = {sleeve.name: sleeve for sleeve in SLEEVES}


def sleeve(name: str) -> ChallengerSleeve:
    """Return the frozen specification for ``name``, or raise :class:`KeyError`."""
    try:
        return _BY_NAME[name]
    except KeyError:
        known = ", ".join(SLEEVE_NAMES)
        raise KeyError(f"'{name}' is not a challenger-v1 sleeve. Known: {known}.") from None


def contract_payload() -> dict[str, ConfigurationValue]:
    """Every frozen decision-relevant value, in canonical hash representation."""
    normalized = normalize_configuration(
        {
            "benchmark_sleeve": BENCHMARK_SLEEVE,
            "conflicting_evidence_fails_sleeve": CONFLICTING_EVIDENCE_FAILS_SLEEVE,
            "contract_id": CONTRACT_ID,
            "contract_version": CONTRACT_VERSION,
            "cost_bps_per_side": str(COST_BPS_PER_SIDE),
            "cost_bps_round_trip": str(COST_BPS_ROUND_TRIP),
            "cost_model_id": COST_MODEL_ID,
            "decision_frequency": DECISION_FREQUENCY,
            "execution_basis": EXECUTION_BASIS,
            "gross_exposure_cap": str(GROSS_EXPOSURE_CAP),
            "leverage": str(LEVERAGE),
            "leverage_allowed": LEVERAGE_ALLOWED,
            "long_only": LONG_ONLY,
            "max_price_staleness_sessions": MAX_PRICE_STALENESS_SESSIONS,
            "requires_t1_open_execution": REQUIRES_T1_OPEN_EXECUTION,
            "settlement_model": SETTLEMENT_MODEL,
            "signal_basis": SIGNAL_BASIS,
            "signal_session_time": SIGNAL_SESSION_TIME.isoformat(timespec="seconds"),
            "sleeve_count": SLEEVE_COUNT,
            "sleeves": [sleeve.payload() for sleeve in SLEEVES],
            "starting_cash_per_sleeve": str(STARTING_CASH_PER_SLEEVE),
            "valuation_basis": VALUATION_BASIS,
            "whole_shares_only": WHOLE_SHARES_ONLY,
        }
    )
    assert isinstance(normalized, dict)
    return normalized


def contract_hash() -> str:
    """SHA-256 digest of the whole frozen contract."""
    return deterministic_configuration_hash(contract_payload())


# --- interpretation ----------------------------------------------------------

INTERPRETATION = (
    "challenger-v1 produces operational and early-behavior evidence: that five "
    "independent sleeves can be defined immutably, run every session without "
    "partial mutation, execute on the T+1 open they claim to, and be reconstructed "
    "from persisted state. It is not evidence of alpha. The observation window is "
    "far too short for statistical significance, the universes carry survivorship "
    "bias, the inputs are price-return rather than total-return, and no result "
    "here authorizes live capital."
)

# Honest, load-bearing limitations. Any report built on challenger-v1 repeats
# these rather than presenting a return number on its own.
KNOWN_LIMITATIONS: tuple[str, ...] = (
    "Survivorship bias: the large-cap preset is a curated list of companies that "
    "are listed and liquid today. Names that failed or were acquired never appear, "
    "so any historical comparison over it is biased upward.",
    "Price return, not total return: the daily bars are unadjusted closes, so "
    "dividends are invisible. This understates every dividend-paying holding and "
    "systematically penalizes the higher-yielding members of the dual-momentum "
    "universe (EFA, EEM, VNQ, IEF) against SPY.",
    "Modeled costs, not real fills: 10 bps round trip is an assumption. Real "
    "opening-auction slippage varies by name, size, and day, and is not observed.",
    "No corporate-action handling is claimed by this contract: splits, spin-offs, "
    "and symbol changes are handled by the underlying bar evidence, not here.",
    "Short window: a cohort observed over weeks reveals operational behavior and "
    "turnover, not skill. Differences between sleeves over this horizon are noise "
    "unless they are differences in behavior rather than return.",
)
