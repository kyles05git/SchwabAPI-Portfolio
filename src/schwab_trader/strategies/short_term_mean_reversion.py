"""``short-term-mean-reversion-v1``: the frozen challenger-v1 sleeve owned by #94.

Short-horizon oversold-inside-an-uptrend entries, ranked by the most negative
close-to-20-session-average distance, held until an explicit exit condition
(recovery to within 1% of the 20-session average, or a close below the
200-session average) fires. Every frozen decision - universe, averages,
thresholds, position limits, coverage floor - is read from
:mod:`schwab_trader.strategies.contract` rather than re-typed here, and the
existing registered ``mean-reversion`` implementation
(:class:`schwab_trader.agent.MeanReversionStrategy`) is not modified: this is a
version-1 rewrite that keeps its entry/ranking mathematics and shared sizing
helper while fixing the gaps the contract's audit (`docs/architecture
/challenger-v1-contract.md`, S6) found non-compliant - the implicit-only exit,
the silent short-history skip, and the ``regime_signal`` overlay.

Deliberately *not* here: registration, cohort assembly, scheduling, and the
CLI/dashboard wiring. Those belong to #95.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from schwab_trader import signals
from schwab_trader.agent import MarketContext, OrderProposal, Strategy
from schwab_trader.market_data import Candle
from schwab_trader.models import OrderSide
from schwab_trader.strategies import contract, sizing

_SLEEVE = contract.SHORT_TERM_MEAN_REVERSION

SHORT_MA: int = _SLEEVE.parameters["short_ma"]  # type: ignore[assignment]
LONG_MA: int = _SLEEVE.parameters["long_ma"]  # type: ignore[assignment]
ENTRY_DIP: Decimal = _SLEEVE.parameter_decimal("entry_dip")
EXIT_RECOVERY_BAND: Decimal = _SLEEVE.parameter_decimal("exit_recovery_band")
MINIMUM_PRICE_SESSIONS: int = _SLEEVE.minimum_price_sessions
MAX_POSITIONS: int = _SLEEVE.max_positions
MAX_POSITION_FRACTION: Decimal = _SLEEVE.max_position_fraction
COVERAGE_FLOOR: Decimal = _SLEEVE.coverage_floor
DEFAULT_UNIVERSE: tuple[str, ...] = _SLEEVE.universe

# Cohort-level, not per-sleeve: challenger-v1 pins gross exposure at 100% for
# every member and forbids a second, independent market-timing overlay such as
# ``signals.regime_signal`` on top of the strategy under test.
GROSS_EXPOSURE_CAP: Decimal = contract.GROSS_EXPOSURE_CAP

# ``signals.sma`` and every distance computed from it are plain ``float``
# (S6 of the contract: "signals.sma is reused by #94"), but the frozen
# thresholds are exact ``Decimal`` percentages. A close constructed to sit
# exactly on a threshold - "exactly 5% below qualifies" - can still land a
# handful of ULPs to the wrong side of it purely from float summation order,
# which is not a real difference in evidence. Thresholds are compared with
# this tolerance so the documented boundary is inclusive in practice, not just
# in the real-number math; it is far below any economically meaningful price
# difference.
_FLOAT_TOLERANCE = 1e-9
_ENTRY_DIP_F = float(ENTRY_DIP)
_EXIT_RECOVERY_BAND_F = float(EXIT_RECOVERY_BAND)


@dataclass(frozen=True)
class SymbolSignal:
    """One eligible symbol's exact session-T evidence, for deterministic rationales."""

    symbol: str
    close: float
    short_average: float
    long_average: float
    distance: float  # close / short_average - 1; negative when below the average


@dataclass(frozen=True)
class Evaluation:
    """Everything session T's decision produced, not just the resulting orders.

    ``decide`` returns only ``proposals`` (the :class:`~schwab_trader.agent.Strategy`
    contract), but the contract also requires ineligible symbols and fail-closed
    reasons to be reported rather than silently swallowed. Callers that need that
    detail - tests, and eventually #95's reporting - use :meth:`evaluate` directly.
    """

    session: datetime
    universe_size: int
    eligible: tuple[str, ...]
    ineligible: tuple[str, ...]
    coverage: Decimal
    ok: bool
    reason: str
    held_before: tuple[str, ...]
    exited: tuple[str, ...]
    retained: tuple[str, ...]
    entered: tuple[str, ...]
    proposals: tuple[OrderProposal, ...] = field(default_factory=tuple)

    @property
    def turnover_orders(self) -> int:
        """Sell-plus-buy order count this session - a cheap turnover diagnostic.

        Not a dollar cost: the frozen 5-bps-per-side / 10-bps-round-trip
        assumption (``contract.COST_BPS_PER_SIDE`` /
        ``contract.COST_BPS_ROUND_TRIP``) is applied downstream at fill time, not
        here. This only counts how many fills a session would generate, which is
        what makes the daily cadence of this sleeve the most cost-sensitive
        member of the cohort by construction (S5.5 of the contract).
        """
        return len(self.proposals)


def _is_usable_price(close: Decimal) -> bool:
    return close.is_finite() and close > 0


def _duplicate_or_conflicting(
    candles: list[Candle],
) -> tuple[bool, str | None]:
    """Detect two rows for the same calendar date; True + reason if values conflict.

    Two different closes for one symbol/session is a data-integrity fault per the
    contract (S7): the whole sleeve fails closed rather than picking one value. An
    exact duplicate row (same date, same close) is not a conflict but is still
    reported so it never silently passes as ordinary evidence.
    """
    seen: dict[date, Decimal] = {}
    for candle in candles:
        day = candle.date.date()
        prior = seen.get(day)
        if prior is not None and prior != candle.close:
            reason = f"conflicting closes for {candle.symbol} on {day}: {prior} vs {candle.close}"
            return True, reason
        seen[day] = candle.close
    return False, None


def _evaluate_symbol(
    symbol: str, candles: list[Candle], session_date: date
) -> tuple[SymbolSignal | None, str | None]:
    """Return (signal, ineligible_reason). Exactly one is None."""
    if len(candles) < MINIMUM_PRICE_SESSIONS:
        return None, "insufficient history"

    conflicting, reason = _duplicate_or_conflicting(candles)
    if conflicting:
        return None, reason or "conflicting evidence"

    last = candles[-1]
    if last.date.date() != session_date:
        return None, f"stale: last close {last.date.date()} is not session {session_date}"
    if not _is_usable_price(last.close):
        return None, f"non-finite or non-positive price {last.close}"

    closes = [float(c.close) for c in candles]
    short_average = signals.sma(closes, SHORT_MA)
    long_average = signals.sma(closes, LONG_MA)
    if short_average is None or long_average is None or short_average <= 0 or long_average <= 0:
        return None, "moving average unavailable despite sufficient history"

    price = closes[-1]
    distance = price / short_average - 1
    return SymbolSignal(symbol, price, short_average, long_average, distance), None


def _qualifies_entry(signal: SymbolSignal) -> bool:
    """Close at least ``entry_dip`` below the short average, still above the long one."""
    threshold = -_ENTRY_DIP_F + _FLOAT_TOLERANCE
    return signal.close > signal.long_average and signal.distance <= threshold


def _recovered(signal: SymbolSignal) -> bool:
    """Close back to within ``exit_recovery_band`` of the short average (or past it)."""
    return signal.distance >= -_EXIT_RECOVERY_BAND_F - _FLOAT_TOLERANCE


def _broke_long_average(signal: SymbolSignal) -> bool:
    return signal.close <= signal.long_average


def _entry_rationale(signal: SymbolSignal) -> str:
    return (
        f"close {signal.close:.2f} is {-signal.distance:.2%} below its {SHORT_MA}-session "
        f"average {signal.short_average:.2f} (entry threshold {ENTRY_DIP:.0%}) while above its "
        f"{LONG_MA}-session average {signal.long_average:.2f}"
    )


def _exit_rationale(signal: SymbolSignal) -> str:
    recovered = _recovered(signal)
    broke_long = _broke_long_average(signal)
    parts = []
    if recovered:
        parts.append(
            f"recovered to within {EXIT_RECOVERY_BAND:.0%} of its {SHORT_MA}-session average: "
            f"close {signal.close:.2f} vs average {signal.short_average:.2f} "
            f"(distance {signal.distance:+.2%})"
        )
    if broke_long:
        parts.append(
            f"closed {signal.close:.2f} at or below its {LONG_MA}-session average "
            f"{signal.long_average:.2f} (uptrend broke)"
        )
    return "; ".join(parts) if parts else "exit condition no longer holds"


class ShortTermMeanReversionStrategy(Strategy):
    """``short-term-mean-reversion-v1``: version-1, contract-compliant rewrite.

    Takes injected daily history exactly like the registered ``mean-reversion``
    strategy and evaluates every session (no separate calendar - #95 owns
    scheduling). Unlike the registered strategy this fails closed on
    insufficient/stale/conflicting evidence and below-floor coverage, pins gross
    exposure at 100% with no regime overlay, and exits only on the two named
    conditions rather than implicitly falling out of a recomputed target list.
    """

    name = "short-term-mean-reversion-v1"

    def __init__(
        self,
        universe: list[str] | None = None,
        *,
        history: dict[str, list[Candle]],
    ) -> None:
        super().__init__(list(universe) if universe is not None else list(DEFAULT_UNIVERSE))
        self._history = history

    def evaluate(self, context: MarketContext) -> Evaluation:
        history = sizing.as_of_history(self._history, context.now)
        session_date = context.now.date()
        held_before = tuple(sorted(s for s, q in context.positions.items() if q > 0))

        signals_by_symbol: dict[str, SymbolSignal] = {}
        ineligible: list[str] = []
        for symbol in self.universe:
            candles = history.get(symbol, [])
            signal, reason = _evaluate_symbol(symbol, candles, session_date)
            if signal is None:
                ineligible.append(symbol)
                if reason and reason.startswith("conflicting"):
                    return Evaluation(
                        session=context.now,
                        universe_size=len(self.universe),
                        eligible=(),
                        ineligible=tuple(ineligible),
                        coverage=Decimal(0),
                        ok=False,
                        reason=reason,
                        held_before=held_before,
                        exited=(),
                        retained=held_before,
                        entered=(),
                        proposals=(),
                    )
            else:
                signals_by_symbol[symbol] = signal

        eligible = tuple(s for s in self.universe if s in signals_by_symbol)
        universe_size = len(self.universe)
        coverage = (
            Decimal(len(eligible)) / Decimal(universe_size) if universe_size else Decimal(0)
        )
        if coverage < COVERAGE_FLOOR:
            return Evaluation(
                session=context.now,
                universe_size=universe_size,
                eligible=eligible,
                ineligible=tuple(ineligible),
                coverage=coverage,
                ok=False,
                reason=f"coverage {coverage:.0%} is below the {COVERAGE_FLOOR:.0%} floor",
                held_before=held_before,
                exited=(),
                retained=held_before,
                entered=(),
                proposals=(),
            )

        retained: list[str] = []
        exited: list[str] = []
        exit_rationale: dict[str, str] = {}
        for symbol in held_before:
            signal = signals_by_symbol.get(symbol)
            if signal is None:
                # No usable session-T evidence for a held name: preserve it rather
                # than manufacture an exit the contract never named.
                retained.append(symbol)
                continue
            if _recovered(signal) or _broke_long_average(signal):
                exited.append(symbol)
                exit_rationale[symbol] = _exit_rationale(signal)
            else:
                retained.append(symbol)

        held_set = set(held_before)
        candidates = [s for s in eligible if s not in held_set]
        scores = {
            s: -signals_by_symbol[s].distance
            for s in candidates
            if _qualifies_entry(signals_by_symbol[s])
        }
        ranked = sizing.rank_desc(scores, self.universe)

        room = max(0, MAX_POSITIONS - len(retained))
        entered = ranked[:room]
        entry_rationale = {s: _entry_rationale(signals_by_symbol[s]) for s in entered}

        targets = retained + entered
        planned = sizing.plan_rebalance(
            targets=targets,
            positions=context.positions,
            quotes=context.quotes,
            equity=context.equity,
            cash=context.cash,
            spendable=context.spendable,
            leverage=context.leverage,
            gross_cap=GROSS_EXPOSURE_CAP,
            max_position_fraction=MAX_POSITION_FRACTION,
            exit_reason="",
            enter_reason="",
        )

        proposals: list[OrderProposal] = []
        for order in planned:
            symbol = order.request.symbol
            if order.request.side is OrderSide.SELL:
                rationale = exit_rationale.get(symbol, "explicit exit condition met")
            else:
                rationale = entry_rationale.get(symbol, "entered the target book")
            proposals.append(OrderProposal(request=order.request, rationale=rationale))

        return Evaluation(
            session=context.now,
            universe_size=universe_size,
            eligible=eligible,
            ineligible=tuple(ineligible),
            coverage=coverage,
            ok=True,
            reason="",
            held_before=held_before,
            exited=tuple(exited),
            retained=tuple(retained),
            entered=tuple(entered),
            proposals=tuple(proposals),
        )

    def decide(self, context: MarketContext) -> list[OrderProposal]:
        return list(self.evaluate(context).proposals)
