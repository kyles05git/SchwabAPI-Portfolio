"""The signal session and the execution session, kept apart.

An experiment that decides from session ``T``'s settled close and an experiment that
*trades* on session ``T``'s close are different experiments. The first is executable;
the second is not, because the close is the last print of the session. Until now the
paper cohort collapsed the two: one :class:`~schwab_trader.scheduling.ExchangeSession`
supplied the decision instant, the fill reference, and the valuation instant at once.

This module separates them.

- **Signal session** (``T``) — the session whose settled evidence produces the decision.
  It is the session the run is keyed on, so run identity, idempotency, leasing, and the
  scheduler's deadline are all unchanged.
- **Execution session** (``T+1`` under the next-open methodology) — the next *valid*
  exchange session, resolved through :mod:`schwab_trader.market_calendar`. Weekends,
  holidays, and early closes are therefore handled by the one calendar that already
  exists; this module defines no second calendar.

A :class:`SessionPlan` carries both sessions and four separate instants — signal,
decision, execution, and valuation — so every one of them can be persisted rather than
re-derived later from a single date.

Two methodologies are registered:

``mark-to-close/v1``
    What the cohorts recorded before this module existed: decide, fill, and mark all at
    session ``T``'s close. It is named here so historical records can state their
    methodology explicitly, and its behavior is byte-for-byte what it always was.

``signal-t-close-execute-t1-open/v1``
    Decide from ``T``'s close, execute and mark at the ``T+1`` opening print.

They are separate configuration identities on purpose. The new timing is never applied
to an existing official cohort — :func:`ensure_methodology_allowed` refuses outright —
so ``paper-first-2026-07-27`` and ``paper-first-2026-07-28`` keep the meaning, hashes,
and results they were recorded under.

``settlement_t1`` is untouched by all of this. It models settled *cash* (sale proceeds
usable only after T+1) and continues to mean exactly that; it is not a timing model and
this module neither reads nor redefines it.

Everything here is pure: no I/O, no clock reads, no broker path.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from schwab_trader import market_calendar as mc
from schwab_trader import scheduling
from schwab_trader.experiments import deterministic_configuration_hash
from schwab_trader.next_open_fill import (
    OPENING_INTERVAL_MINUTES,
    OpeningBarEvidence,
    OpeningFillPolicy,
    opening_evidence_reasons,
    opening_interval_utc,
)

#: Stable schema identity for the payloads produced here.
PAYLOAD_SCHEMA = "paper-execution-timing/1"


class ExecutionTiming(StrEnum):
    """When a decision derived from session ``T``'s close is actually executed."""

    SESSION_CLOSE = "session-close"
    """Execute and mark at session ``T``'s own close. The historical behavior."""

    NEXT_SESSION_OPEN = "next-session-open"
    """Execute and mark at the opening print of the next valid exchange session."""


class UnknownMethodologyError(ValueError):
    """A methodology key that is not registered. Never resolved to a default."""


class ProtectedCohortError(ValueError):
    """An attempt to apply new timing semantics to an existing official cohort."""


class ClosedSessionError(ValueError):
    """A plan was requested for a date the exchange was closed."""


#: Cohorts whose recorded methodology is frozen. Their observations, definitions, and
#: configuration hashes are historical evidence; re-running them under different timing
#: semantics would silently rewrite what the experiment measured.
PROTECTED_LEGACY_COHORTS = frozenset(
    {
        "paper-first-2026-07-27",
        "paper-first-2026-07-28",
    }
)


class ExecutionMethodology(BaseModel):
    """A complete, versioned statement of when a paper decision becomes a fill."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    methodology_id: str
    methodology_version: str
    timing: ExecutionTiming
    signal_evidence: str
    """What the decision is derived from, e.g. ``session-close``."""

    execution_reference: str
    """The price reference the fill uses, e.g. ``next-session-open``."""

    valuation_reference: str
    """The instant the sleeve is marked, e.g. ``next-session-open``."""

    opening_interval_minutes: int = OPENING_INTERVAL_MINUTES
    fill_policy: OpeningFillPolicy | None = None
    """Cost assumptions for an opening fill. ``None`` for close-marked methodologies,
    which execute against the shared quote snapshot the cohort already captures."""

    methodology_hash: str = ""
    """SHA-256 over every field above. Derived, never supplied."""

    @property
    def key(self) -> str:
        """The stable registry key, ``"<id>/<version>"``."""
        return f"{self.methodology_id}/{self.methodology_version}"

    @property
    def requires_opening_evidence(self) -> bool:
        return self.timing is ExecutionTiming.NEXT_SESSION_OPEN

    def payload(self) -> dict[str, object]:
        """Every decision-relevant field, in its canonical hash representation."""
        policy = self.fill_policy
        return {
            "execution_reference": self.execution_reference,
            "fill_policy": (
                None
                if policy is None
                else {
                    "commission_minimum": str(policy.commission_minimum),
                    "commission_per_order": str(policy.commission_per_order),
                    "commission_per_share": str(policy.commission_per_share),
                    "half_spread_bps": str(policy.half_spread_bps),
                    "policy_id": policy.policy_id,
                    "price_increment": str(policy.price_increment),
                    "slippage_bps": str(policy.slippage_bps),
                    "whole_shares_only": policy.whole_shares_only,
                }
            ),
            "methodology_id": self.methodology_id,
            "methodology_version": self.methodology_version,
            "opening_interval_minutes": self.opening_interval_minutes,
            "signal_evidence": self.signal_evidence,
            "timing": self.timing.value,
            "valuation_reference": self.valuation_reference,
        }

    @model_validator(mode="after")
    def _set_or_verify_hash(self) -> Self:
        expected = deterministic_configuration_hash(self.payload())
        if self.methodology_hash and self.methodology_hash != expected:
            raise ValueError("methodology_hash does not match the normalized methodology")
        if self.requires_opening_evidence and self.fill_policy is None:
            raise ValueError("a next-open methodology must declare its fill policy")
        object.__setattr__(self, "methodology_hash", expected)
        return self


#: The methodology every pre-existing observation was recorded under. Naming it does not
#: change a single stored value: it is the behavior the runner already had.
MARK_TO_CLOSE_V1 = ExecutionMethodology(
    methodology_id="mark-to-close",
    methodology_version="v1",
    timing=ExecutionTiming.SESSION_CLOSE,
    signal_evidence="session-close",
    execution_reference="session-close-quote",
    valuation_reference="session-close",
    fill_policy=None,
)

#: The new experiment introduced by issue #79.
SIGNAL_T_CLOSE_EXECUTE_T1_OPEN_V1 = ExecutionMethodology(
    methodology_id="signal-t-close-execute-t1-open",
    methodology_version="v1",
    timing=ExecutionTiming.NEXT_SESSION_OPEN,
    signal_evidence="session-close",
    execution_reference="next-session-open",
    valuation_reference="next-session-open",
    fill_policy=OpeningFillPolicy(),
)

METHODOLOGIES: Mapping[str, ExecutionMethodology] = {
    MARK_TO_CLOSE_V1.key: MARK_TO_CLOSE_V1,
    SIGNAL_T_CLOSE_EXECUTE_T1_OPEN_V1.key: SIGNAL_T_CLOSE_EXECUTE_T1_OPEN_V1,
}

#: What an empty/absent methodology field means. Every record written before this module
#: existed ran the close-marked model, so resolving absence to it is a statement of fact
#: rather than a default that could mislabel history.
DEFAULT_METHODOLOGY_KEY = MARK_TO_CLOSE_V1.key
NEXT_OPEN_METHODOLOGY_KEY = SIGNAL_T_CLOSE_EXECUTE_T1_OPEN_V1.key


def resolve_methodology(key: str | None) -> ExecutionMethodology:
    """Resolve a persisted methodology key. Unknown keys fail closed."""
    normalized = (key or "").strip()
    if not normalized:
        return MARK_TO_CLOSE_V1
    found = METHODOLOGIES.get(normalized)
    if found is None:
        raise UnknownMethodologyError(
            f"'{normalized}' is not a registered execution methodology; "
            f"known keys are {', '.join(sorted(METHODOLOGIES))}."
        )
    return found


def ensure_methodology_allowed(cohort_id: str, methodology: ExecutionMethodology) -> None:
    """Refuse to apply non-legacy timing to a frozen official cohort."""
    if (
        methodology.timing is not ExecutionTiming.SESSION_CLOSE
        and cohort_id.strip() in PROTECTED_LEGACY_COHORTS
    ):
        raise ProtectedCohortError(
            f"Cohort '{cohort_id}' is an existing official experiment recorded under "
            f"{MARK_TO_CLOSE_V1.key}. Its timing semantics cannot be changed; start a "
            "new cohort for the new methodology."
        )


@dataclass(frozen=True)
class SessionPlan:
    """The signal/execution session pair and the four instants that separate them."""

    methodology: ExecutionMethodology
    exchange: str
    signal_session: scheduling.ExchangeSession
    execution_session: scheduling.ExchangeSession

    @property
    def timing(self) -> ExecutionTiming:
        return self.methodology.timing

    @property
    def signal_session_date(self) -> date:
        return self.signal_session.session_date

    @property
    def execution_session_date(self) -> date:
        return self.execution_session.session_date

    @property
    def signal_et(self) -> datetime:
        """The last instant of evidence the decision may use: session ``T``'s close."""
        assert self.signal_session.close_et is not None
        return self.signal_session.close_et

    @property
    def decision_et(self) -> datetime:
        """When the decision is fixed. Identical to :attr:`signal_et` by construction:
        the decision is a pure function of evidence settled at ``T``'s close."""
        return self.signal_et

    @property
    def execution_et(self) -> datetime:
        """When the simulated fill occurs."""
        if self.timing is ExecutionTiming.NEXT_SESSION_OPEN:
            assert self.execution_session.open_et is not None
            return self.execution_session.open_et
        assert self.execution_session.close_et is not None
        return self.execution_session.close_et

    @property
    def valuation_et(self) -> datetime:
        """When the sleeve is marked. Always the execution instant, so the recorded
        equity series is measured at the same point the fills happened."""
        return self.execution_et

    @property
    def signal_utc(self) -> datetime:
        return mc.eastern_to_utc(self.signal_et)

    @property
    def decision_utc(self) -> datetime:
        return mc.eastern_to_utc(self.decision_et)

    @property
    def execution_utc(self) -> datetime:
        return mc.eastern_to_utc(self.execution_et)

    @property
    def valuation_utc(self) -> datetime:
        return mc.eastern_to_utc(self.valuation_et)

    @property
    def opening_interval_utc(self) -> tuple[datetime, datetime] | None:
        """The execution session's opening interval, when the methodology needs one."""
        if not self.methodology.requires_opening_evidence:
            return None
        return opening_interval_utc(
            self.execution_session_date,
            minutes=self.methodology.opening_interval_minutes,
            exchange=self.exchange,
        )

    @property
    def evidence_ready_at_utc(self) -> datetime:
        """The earliest instant every input this plan needs can exist.

        For the next-open methodology that is the end of the execution session's
        opening interval, not its start: a bar that is still printing is not evidence.
        The scheduler's existing deadline for the signal session (the next session's
        due time) is strictly later, so the wait fits inside the window already
        modeled — no second timer is introduced.
        """
        interval = self.opening_interval_utc
        if interval is None:
            return self.execution_utc
        return interval[1]

    def timestamps(self) -> dict[str, str]:
        """The four instants as ISO-8601 UTC, for persistence and diagnostics."""
        return {
            "signal_time": self.signal_utc.isoformat(),
            "decision_time": self.decision_utc.isoformat(),
            "execution_time": self.execution_utc.isoformat(),
            "valuation_time": self.valuation_utc.isoformat(),
        }

    def payload(self) -> dict[str, object]:
        """Stable, secret-free JSON description of the plan."""
        interval = self.opening_interval_utc
        return {
            "schema": PAYLOAD_SCHEMA,
            "methodology": self.methodology.key,
            "methodology_hash": self.methodology.methodology_hash,
            "timing": self.timing.value,
            "exchange": self.exchange,
            "signal_session": self.signal_session.session_id,
            "execution_session": self.execution_session.session_id,
            "signal_session_is_early_close": self.signal_session.is_early_close,
            "execution_session_is_early_close": self.execution_session.is_early_close,
            "opening_interval_start_at": None if interval is None else interval[0].isoformat(),
            "opening_interval_end_at": None if interval is None else interval[1].isoformat(),
            "evidence_ready_at": self.evidence_ready_at_utc.isoformat(),
            **self.timestamps(),
        }


def plan_sessions(
    signal_session_date: date,
    *,
    methodology: ExecutionMethodology = MARK_TO_CLOSE_V1,
    exchange: str = mc.EXCHANGE_MIC,
) -> SessionPlan:
    """Resolve the signal/execution session pair for one methodology.

    The execution session is the next *trading* day per the canonical calendar, so a
    Friday signal executes Monday, a signal before a holiday skips it, and a signal
    before an early close still executes at that session's ordinary 09:30 open — an
    early close shortens the afternoon, never the auction.
    """
    signal = scheduling.session_for_date(signal_session_date, exchange)
    if not signal.is_trading_day:
        raise ClosedSessionError(
            f"{signal_session_date.isoformat()} is not an {exchange} trading session, "
            "so it cannot be a signal session."
        )
    if methodology.timing is ExecutionTiming.NEXT_SESSION_OPEN:
        execution = scheduling.session_for_date(
            mc.next_trading_day(signal_session_date), exchange
        )
    else:
        execution = signal
    return SessionPlan(
        methodology=methodology,
        exchange=exchange,
        signal_session=signal,
        execution_session=execution,
    )


@dataclass(frozen=True)
class OpeningEvidenceAssessment:
    """Whether every symbol has usable opening evidence for the execution session."""

    execution_session: date
    required_symbols: tuple[str, ...]
    missing_symbols: tuple[str, ...] = ()
    """Symbols with no opening evidence at all."""

    mismatched_symbols: tuple[str, ...] = ()
    """Symbols whose evidence describes a different session. Never silently accepted."""

    invalid_symbols: tuple[str, ...] = ()
    """Symbols whose typed evidence fails structural or digest reproduction checks."""

    ambiguous_symbols: tuple[str, ...] = ()
    """Symbols with multiple mapping keys after case normalization."""

    @property
    def ready(self) -> bool:
        return not any(
            (
                self.missing_symbols,
                self.mismatched_symbols,
                self.invalid_symbols,
                self.ambiguous_symbols,
            )
        )

    @property
    def reason(self) -> str:
        """The single stable reason code, most ambiguous condition first."""
        if self.ambiguous_symbols:
            return "ambiguous_keys"
        if self.invalid_symbols:
            return "invalid_evidence"
        if self.mismatched_symbols:
            return "session_not_covered"
        if self.missing_symbols:
            return "missing_keys"
        return "ok"

    @property
    def unready_symbols(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    *self.missing_symbols,
                    *self.mismatched_symbols,
                    *self.invalid_symbols,
                    *self.ambiguous_symbols,
                }
            )
        )

    @property
    def detail(self) -> str:
        return (
            f"opening_bars: {len(self.unready_symbols)} of {len(self.required_symbols)} "
            f"symbol(s) lack usable {self.execution_session.isoformat()} opening evidence."
        )


def assess_opening_evidence(
    symbols: Sequence[str],
    evidence: Mapping[str, OpeningBarEvidence],
    *,
    execution_session: date,
    interval_minutes: int = OPENING_INTERVAL_MINUTES,
    exchange: str = mc.EXCHANGE_MIC,
) -> OpeningEvidenceAssessment:
    """Check opening evidence for one member's universe against ``execution_session``.

    Symbol matching is case-normalized because the evidence validator upper-cases what
    it stores; nothing else is normalized, and a session mismatch is reported rather
    than repaired.
    """
    required = tuple(dict.fromkeys(symbol.strip().upper() for symbol in symbols if symbol.strip()))
    available: dict[str, list[OpeningBarEvidence]] = {}
    for key, value in evidence.items():
        available.setdefault(key.strip().upper(), []).append(value)
    missing: list[str] = []
    mismatched: list[str] = []
    invalid: list[str] = []
    ambiguous: list[str] = []
    for symbol in required:
        found = available.get(symbol, [])
        if not found:
            missing.append(symbol)
        elif len(found) > 1:
            ambiguous.append(symbol)
        elif found[0].session_date != execution_session:
            mismatched.append(symbol)
        elif opening_evidence_reasons(
            found[0],
            expected_symbol=symbol,
            expected_session=execution_session,
            expected_minutes=interval_minutes,
            expected_exchange=exchange,
        ):
            invalid.append(symbol)
    return OpeningEvidenceAssessment(
        execution_session=execution_session,
        required_symbols=required,
        missing_symbols=tuple(missing),
        mismatched_symbols=tuple(mismatched),
        invalid_symbols=tuple(invalid),
        ambiguous_symbols=tuple(ambiguous),
    )


__all__ = [
    "DEFAULT_METHODOLOGY_KEY",
    "MARK_TO_CLOSE_V1",
    "METHODOLOGIES",
    "NEXT_OPEN_METHODOLOGY_KEY",
    "PAYLOAD_SCHEMA",
    "PROTECTED_LEGACY_COHORTS",
    "SIGNAL_T_CLOSE_EXECUTE_T1_OPEN_V1",
    "ClosedSessionError",
    "ExecutionMethodology",
    "ExecutionTiming",
    "OpeningEvidenceAssessment",
    "ProtectedCohortError",
    "SessionPlan",
    "UnknownMethodologyError",
    "assess_opening_evidence",
    "ensure_methodology_allowed",
    "plan_sessions",
    "resolve_methodology",
]
