"""The frozen ``dual-momentum-v1`` challenger sleeve (issue #92).

Relative momentum picks, absolute momentum gates. Every session the sleeve is
evaluated, the four risk assets are ranked by their trailing 252-session price
return, and the winner is held at 100% *only if* that return is strictly greater
than zero. Otherwise the sleeve holds the defensive asset. One position, always
fully invested, never short and never levered.

Every number this module uses is read from
:mod:`schwab_trader.strategies.contract` rather than re-typed, and every order it
proposes is built by :func:`schwab_trader.strategies.sizing.plan_rebalance`
rather than sized here. The rationale for each frozen value - why the gate is
``> 0`` rather than a T-bill proxy, why ``IEF`` rather than ``BIL``, why the
coverage floor is 100%, why the cadence is monthly - is in
``docs/architecture/challenger-v1-contract.md`` §5.3 and is deliberately not
re-litigated in code comments.

**Fail closed.** A four-name universe cannot absorb a missing member: dropping
one changes which asset classes are even eligible to win. So all five symbols
(four risk plus the defensive asset) must present complete, current, internally
consistent evidence, or the sleeve proposes **no orders at all** for that session
and reports exactly why. Nothing is imputed, nothing is silently dropped, and a
partial universe is never ranked.

Deliberately **not** here, because they belong to other issues:

* registration, cohort assembly, and the monthly last-XNYS-session calendar (#95);
* signal-T / execute-T+1-open timing, opening fills, and valuation (#79);
* the ``signals.regime_signal`` exposure overlay, which challenger-v1 excludes on
  purpose so that each sleeve tests exactly one idea (contract §2).

The decision logic is the pure function :func:`evaluate`: injected candles in, a
frozen :class:`DualMomentumDecision` out. It performs no I/O, reads no clock,
opens no database, and imports nothing dynamically. :class:`DualMomentumStrategy`
is a thin adapter that lets #95 register it like any other paper strategy.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal

from schwab_trader.agent import MarketContext, OrderProposal, Strategy
from schwab_trader.market_data import Candle
from schwab_trader.strategies import contract, sizing

# --- frozen parameters, read from the contract rather than re-typed ----------

SLEEVE = contract.sleeve("dual-momentum-v1")

STRATEGY_NAME = SLEEVE.name
STRATEGY_VERSION = SLEEVE.version

#: The four risk assets, in the frozen order that also breaks ranking ties.
RISK_UNIVERSE: tuple[str, ...] = SLEEVE.universe
#: Held whenever the absolute-momentum gate fails.
DEFENSIVE_ASSET: str = SLEEVE.defensive_universe[0]
#: Every symbol that must present complete evidence: the coverage floor is 100%,
#: and that includes the defensive asset, which the sleeve may have to buy.
REQUIRED_SYMBOLS: tuple[str, ...] = (*RISK_UNIVERSE, DEFENSIVE_ASSET)

#: 252 sessions of trailing return, which needs 253 closes.
LOOKBACK_SESSIONS: int = int(SLEEVE.parameter_decimal("lookback_sessions"))
MINIMUM_CLOSES: int = SLEEVE.minimum_price_sessions
#: The absolute gate is *strictly* greater than this. Zero, not a T-bill proxy:
#: the bars are price-return only, so a T-bill proxy would be a flat line.
ABSOLUTE_GATE_MINIMUM: float = float(SLEEVE.parameter_decimal("absolute_gate_minimum"))


# --- decision result ---------------------------------------------------------


@dataclass(frozen=True)
class AssetScore:
    """One symbol's trailing price return and the exact evidence behind it."""

    symbol: str
    trailing_return: float
    first_close: Decimal
    last_close: Decimal
    first_session: date
    last_session: date
    sessions: int


@dataclass(frozen=True)
class EvidenceFault:
    """Why one symbol could not contribute evidence to this session's decision.

    Faults are carried on the decision rather than raised, so a caller can log
    the whole picture. They are never a reason to proceed with a smaller
    universe.
    """

    symbol: str
    reason: str
    detail: str

    def __str__(self) -> str:
        return f"{self.symbol} {self.reason} ({self.detail})"


@dataclass(frozen=True)
class DualMomentumDecision:
    """The complete, inspectable outcome of one evaluation.

    ``target`` is ``None`` exactly when the sleeve failed closed. Both the scores
    and the faults are always populated as far as the evidence allowed, so a
    failed session is as auditable as a successful one.
    """

    session: date
    scores: tuple[AssetScore, ...]
    ranking: tuple[str, ...]
    faults: tuple[EvidenceFault, ...]
    coverage: Decimal
    gate_passed: bool
    winner: str | None
    target: str | None
    rationale: str

    @property
    def ok(self) -> bool:
        """True when the sleeve produced a target rather than failing closed."""
        return self.target is not None

    def score(self, symbol: str) -> AssetScore | None:
        """This session's score for ``symbol``, or None if it was ineligible."""
        return next((s for s in self.scores if s.symbol == symbol), None)


# --- evidence validation -----------------------------------------------------
#
# Reason codes are stable strings so a caller can count failure modes over time
# without parsing prose.
MISSING_HISTORY = "missing-history"
INSUFFICIENT_HISTORY = "insufficient-history"
STALE_EVIDENCE = "stale-evidence"
DUPLICATE_EVIDENCE = "duplicate-evidence"
CONFLICTING_EVIDENCE = "conflicting-evidence"
MALFORMED_TIMESTAMP = "malformed-timestamp"
INVALID_CLOSE = "invalid-close"
NON_FINITE_RETURN = "non-finite-return"
AMBIGUOUS_DECISION_TIME = "ambiguous-decision-timestamp"


def _bar_session(candle: Candle) -> date | None:
    """The UTC session date of a bar, or None if its timestamp is ambiguous.

    A naive timestamp cannot be placed on a session at all, so it is malformed
    evidence rather than something to interpret with a guessed timezone.
    """
    when = candle.date
    if when.tzinfo is None or when.utcoffset() is None:
        return None
    return when.astimezone(UTC).date()


def _session_cutoff(session: date) -> datetime:
    """The latest instant belonging to ``session``, for look-ahead-safe slicing."""
    return datetime.combine(session, time.max, tzinfo=UTC)


def _score_symbol(
    symbol: str, candles: Sequence[Candle], session: date
) -> AssetScore | EvidenceFault:
    """Validate one symbol's evidence and score it, or explain why it cannot be."""
    supplied = list(candles)
    if not supplied:
        return EvidenceFault(symbol, MISSING_HISTORY, "no candles supplied")

    sessions = [_bar_session(candle) for candle in supplied]
    if any(bar is None for bar in sessions):
        return EvidenceFault(
            symbol, MALFORMED_TIMESTAMP, "a candle timestamp has no timezone offset"
        )

    # Look-ahead safety through the shared helper: nothing dated after session T's
    # close may enter session T's decision.
    history = sizing.as_of_history({symbol: supplied}, _session_cutoff(session))[symbol]
    if not history:
        return EvidenceFault(
            symbol, MISSING_HISTORY, f"every candle is dated after session {session}"
        )

    # A session appearing twice is a data-integrity fault, not a gap. The sleeve
    # does not pick one of the two values, and it does not tolerate a redundant
    # copy either: both mean the evidence for this symbol is not trustworthy.
    by_session: dict[date, Decimal] = {}
    for candle in history:
        bar = _bar_session(candle)
        assert bar is not None  # validated above
        previous = by_session.get(bar)
        if previous is not None:
            reason = DUPLICATE_EVIDENCE if previous == candle.close else CONFLICTING_EVIDENCE
            return EvidenceFault(symbol, reason, f"session {bar} appears more than once")
        by_session[bar] = candle.close

    last_session = _bar_session(history[-1])
    assert last_session is not None
    if last_session != session:
        # Staleness tolerance is zero sessions: the latest bar must be session T's.
        return EvidenceFault(
            symbol, STALE_EVIDENCE, f"latest close is {last_session}, not session {session}"
        )

    if len(history) < MINIMUM_CLOSES:
        return EvidenceFault(
            symbol,
            INSUFFICIENT_HISTORY,
            f"{len(history)} closes on or before {session}, need {MINIMUM_CLOSES}",
        )

    window = history[-MINIMUM_CLOSES:]
    for candle in window:
        close = candle.close
        if not close.is_finite() or close <= 0:
            return EvidenceFault(
                symbol, INVALID_CLOSE, f"close {close} on {_bar_session(candle)} is not a price"
            )

    first, last = window[0], window[-1]
    trailing = float(last.close) / float(first.close) - 1.0
    if not math.isfinite(trailing):
        return EvidenceFault(symbol, NON_FINITE_RETURN, "trailing return is not a finite number")

    first_session = _bar_session(first)
    assert first_session is not None
    return AssetScore(
        symbol=symbol,
        trailing_return=trailing,
        first_close=first.close,
        last_close=last.close,
        first_session=first_session,
        last_session=session,
        sessions=LOOKBACK_SESSIONS,
    )


# --- the decision ------------------------------------------------------------


def _percent(value: float) -> str:
    return f"{value:+.2%}"


def _describe(scores: Mapping[str, AssetScore], ranking: Sequence[str]) -> str:
    ranked = ", ".join(f"{symbol} {_percent(scores[symbol].trailing_return)}" for symbol in ranking)
    return ranked or "no risk asset scored"


def evaluate(
    history: Mapping[str, Sequence[Candle]],
    *,
    session: date,
) -> DualMomentumDecision:
    """Decide what ``dual-momentum-v1`` holds after the close of ``session``.

    ``history`` supplies daily candles per symbol; only :attr:`REQUIRED_SYMBOLS`
    are read, extra symbols are ignored, and the order the mapping happens to
    iterate in cannot affect the result. Candles dated after ``session`` are
    excluded, so passing a full history and an earlier ``session`` reproduces
    that session's decision exactly.

    Returns a decision whose ``target`` is the symbol to hold at 100%, or ``None``
    when the sleeve failed closed - in which case ``faults`` says why and the
    caller must propose no orders.
    """
    scores: dict[str, AssetScore] = {}
    faults: list[EvidenceFault] = []
    # Iterating the frozen symbol order (not the mapping's) is what makes the
    # result independent of how a provider happened to hand over the data.
    for symbol in REQUIRED_SYMBOLS:
        outcome = _score_symbol(symbol, history.get(symbol) or (), session)
        if isinstance(outcome, EvidenceFault):
            faults.append(outcome)
        else:
            scores[symbol] = outcome

    coverage = Decimal(len(scores)) / Decimal(len(REQUIRED_SYMBOLS))
    # Ranked for the audit trail even when the sleeve fails closed; a partial
    # ranking is never allowed to select anything.
    ranking = tuple(
        sizing.rank_desc(
            {s: scores[s].trailing_return for s in RISK_UNIVERSE if s in scores},
            RISK_UNIVERSE,
        )
    )

    if coverage < SLEEVE.coverage_floor:
        detail = "; ".join(str(fault) for fault in faults)
        return DualMomentumDecision(
            session=session,
            scores=tuple(scores[s] for s in REQUIRED_SYMBOLS if s in scores),
            ranking=ranking,
            faults=tuple(faults),
            coverage=coverage,
            gate_passed=False,
            winner=None,
            target=None,
            rationale=(
                f"{STRATEGY_NAME} {session} failed closed: coverage "
                f"{len(scores)}/{len(REQUIRED_SYMBOLS)} is below the "
                f"{SLEEVE.coverage_floor:.0%} floor; {detail}; proposing no orders"
            ),
        )

    winner = ranking[0]
    winning_return = scores[winner].trailing_return
    # Strictly greater than zero. A flat year is not an uptrend, so it holds IEF.
    gate_passed = winning_return > ABSOLUTE_GATE_MINIMUM
    target = winner if gate_passed else DEFENSIVE_ASSET

    verdict = "passed" if gate_passed else "failed"
    rationale = (
        f"{STRATEGY_NAME} {session}: trailing {LOOKBACK_SESSIONS}-session price returns "
        f"{_describe(scores, ranking)}; defensive {DEFENSIVE_ASSET} "
        f"{_percent(scores[DEFENSIVE_ASSET].trailing_return)}; absolute gate "
        f"{winner} {_percent(winning_return)} > {ABSOLUTE_GATE_MINIMUM:.0%} {verdict}; "
        f"target {target} at {SLEEVE.max_position_fraction:.0%}"
    )

    return DualMomentumDecision(
        session=session,
        scores=tuple(scores[s] for s in REQUIRED_SYMBOLS if s in scores),
        ranking=ranking,
        faults=tuple(faults),
        coverage=coverage,
        gate_passed=gate_passed,
        winner=winner,
        target=target,
        rationale=rationale,
    )


# --- adapter -----------------------------------------------------------------


def _resolve_universe(universe: Sequence[str] | None) -> tuple[str, ...]:
    """Accept only the frozen universe, so a stored list cannot redefine ``-v1``.

    ``dual-momentum-v1`` always means what the contract says it means. A caller
    may pass the four risk assets or all five symbols (both are spellings of the
    same frozen set) or nothing at all; anything else fails closed rather than
    quietly running a different experiment under a frozen name.
    """
    if universe is None:
        return REQUIRED_SYMBOLS
    supplied = tuple(symbol.strip().upper() for symbol in universe if symbol.strip())
    if supplied in (RISK_UNIVERSE, REQUIRED_SYMBOLS):
        return REQUIRED_SYMBOLS
    raise ValueError(
        f"{STRATEGY_NAME} has a frozen universe {RISK_UNIVERSE} plus defensive "
        f"{DEFENSIVE_ASSET}; refusing to run it over {supplied}"
    )


class DualMomentumStrategy(Strategy):
    """The registrable form of :func:`evaluate`.

    ``universe`` is all five symbols so the runner fetches a quote for the
    defensive asset too - the sleeve may need to buy it - while ranking still
    happens over the four risk assets only.

    Cadence is **not** enforced here. The contract evaluates this sleeve on the
    last XNYS session of each month, and that calendar belongs to #95; calling
    :meth:`decide` on any other session returns the decision for that session.
    Because the target rarely changes, an off-cadence call usually proposes
    nothing anyway.
    """

    name = STRATEGY_NAME

    def __init__(
        self,
        universe: Sequence[str] | None = None,
        *,
        history: Mapping[str, list[Candle]],
    ) -> None:
        super().__init__(list(_resolve_universe(universe)))
        self._history = {
            symbol.strip().upper(): list(candles) for symbol, candles in history.items()
        }
        self.risk_universe = RISK_UNIVERSE
        self.defensive_asset = DEFENSIVE_ASSET
        self.max_positions = SLEEVE.max_positions
        self.max_position_fraction = SLEEVE.max_position_fraction

    def evaluate(self, context: MarketContext) -> DualMomentumDecision:
        """This cycle's decision, without proposing any orders."""
        now = context.now
        if now.tzinfo is None or now.utcoffset() is None:
            fault = EvidenceFault(
                STRATEGY_NAME, AMBIGUOUS_DECISION_TIME, "context.now has no timezone offset"
            )
            return DualMomentumDecision(
                session=now.date(),
                scores=(),
                ranking=(),
                faults=(fault,),
                coverage=Decimal(0),
                gate_passed=False,
                winner=None,
                target=None,
                rationale=(
                    f"{STRATEGY_NAME} failed closed: {fault}; the signal session "
                    f"cannot be identified; proposing no orders"
                ),
            )
        # Sliced against the decision instant as well as the session date, so a
        # bar stamped later on session T cannot leak into a decision made earlier.
        history = sizing.as_of_history(self._history, now)
        return evaluate(history, session=now.astimezone(UTC).date())

    def decide(self, context: MarketContext) -> list[OrderProposal]:
        decision = self.evaluate(context)
        if decision.target is None:
            return []  # failed closed: no orders, not a partial book
        planned = sizing.plan_rebalance(
            targets=[decision.target],
            positions=context.positions,
            quotes=context.quotes,
            equity=context.equity,
            cash=context.cash,
            spendable=context.spendable,
            # The contract's own leverage and gross cap, never the account's:
            # challenger-v1 is unlevered at a pinned 100% gross with no regime
            # overlay, whatever the sleeve is run against.
            leverage=contract.LEVERAGE,
            gross_cap=contract.GROSS_EXPOSURE_CAP,
            max_position_fraction=SLEEVE.max_position_fraction,
            exit_reason=f"{decision.rationale}; selling a holding that is not the target",
            enter_reason=f"{decision.rationale}; buying the target",
        )
        return [
            OrderProposal(request=order.request, rationale=order.rationale) for order in planned
        ]
