"""Cohort-wide data preflight: does *every* member have what it needs, yet?

An official cohort session is a single unit of evidence. Seven sleeves are compared
against one another and against a benchmark, so a session in which five members ran
against Friday's prices and two did not is not "mostly a session" — it is a session
whose comparison is meaningless, recorded permanently as though it were fine.

That is exactly what happened to ``paper-first-2026-07-27``: readiness was judged
per member, *inside* the execution loop, after two members had already mutated paper
state. See ``docs/incidents/2026-07-27-paper-first-cohort-daily-bars-stale.md``.

So readiness is decided once, for the whole cohort, before any member is allowed to
touch paper cash, positions, fills, cycles, or official observations. The verdict is
one of two things:

- :attr:`PreflightState.READY` — every member's declared data is present and settled
  through the target session. Execution may proceed against the shared snapshot.
- :attr:`PreflightState.AWAITING_DATA` — something is not ready. **No member runs.** The
  session is left retryable, not recorded as a terminal partial result.

``awaiting-data`` is deliberately not a failure. A provider that has not yet published
the day's settled candle at 16:05 ET usually has by 17:30 ET, and the scheduler fires
again in between. Bounding that retry is the scheduler's existing job, not a second
timer here: once :mod:`schwab_trader.scheduling` calls the session missed, the runner
converts the wait into a durable missed result and alerts. Waiting is cheap and
reversible; a wrong terminal record is neither.

Everything here is pure. It performs no I/O, reads no clock, and never touches a
broker path. Output is identity-poor: sleeve ids, data kinds, reason codes, and
session dates only — never a payload, connection string, account identifier, or token.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from schwab_trader.data_readiness import DataKind, DataReadiness, ReasonCode

#: JSON contract version for :func:`preflight_payload`.
PAYLOAD_SCHEMA = "cohort-preflight/1"

#: The data kind reported when the shared quote snapshot does not cover a universe.
#: Quotes are not a :class:`DataKind` — they are captured by the snapshot rather than
#: declared as a strategy requirement — but an operator reads them the same way.
QUOTES_KIND = "quotes"

#: Signal-session quotes are distinct from generic quote coverage under a separated
#: timing model: a present quote stamped in T+1 would be look-ahead, not evidence.
SIGNAL_QUOTES_KIND = "signal_quotes"

#: The data kind reported when a next-open methodology lacks its execution session's
#: opening bar. Like quotes, it is snapshot-supplied rather than strategy-declared, and
#: it is never structural: the T+1 opening print simply has not happened yet at T close.
OPENING_BARS_KIND = "opening_bars"

#: How many identifiers a diagnostic names before summarizing the remainder.
_SAMPLE = 5


class PreflightState(StrEnum):
    """Whether the cohort may execute this session yet."""

    READY = "ready"
    """Every member's required data is present and settled through the session."""

    AWAITING_DATA = "awaiting-data"
    """Required data is not ready *yet*. No member executes; the session stays
    retryable, because waiting is what resolves it."""

    MISCONFIGURED = "misconfigured"
    """Required data can never become ready by waiting. No member executes and the
    session fails immediately with an actionable error.

    An unconfigured, disabled, or non-vintage-safe capability is a wiring problem, not
    a late provider. Sitting in ``awaiting-data`` until the deadline would delay the
    operator's answer by hours and then report the far less useful ``missed``."""


#: Reason codes that waiting cannot fix. A capability that is absent, switched off, or
#: structurally unsuitable will be equally absent at the deadline, so the cohort says so
#: at once instead of burning its whole scheduling window first.
STRUCTURAL_REASONS = frozenset(
    {
        ReasonCode.SOURCE_MISSING.value,
        ReasonCode.SOURCE_DISABLED.value,
        ReasonCode.NOT_VINTAGE_SAFE.value,
        ReasonCode.MISSING_AVAILABLE_AT.value,
        # A member with no readiness verdict at all is a wiring defect, not a delay.
        "not_evaluated",
    }
)


@dataclass(frozen=True)
class DataGap:
    """One member's one unmet data requirement, in operator-readable terms."""

    member_id: str
    kind: str
    reason: str
    target_session: date
    latest_session: date | None = None
    """The latest session the data is actually settled through, when known."""

    missing_keys: tuple[str, ...] = ()
    """Keys absent from the batch entirely."""

    uncovered_keys: tuple[str, ...] = ()
    """Keys present but settled only through an earlier session."""

    detail: str = ""

    @property
    def code(self) -> str:
        """The stable ``"<kind>:<reason>"`` identifier, e.g. ``daily_bars:stale``."""
        return f"{self.kind}:{self.reason}"

    @property
    def structural(self) -> bool:
        """Whether waiting can never resolve this gap. See :data:`STRUCTURAL_REASONS`."""
        return self.reason in STRUCTURAL_REASONS


def _sample(keys: Sequence[str]) -> str:
    head = ", ".join(keys[:_SAMPLE])
    extra = len(keys) - _SAMPLE
    return f"{head} (+{extra} more)" if extra > 0 else head


@dataclass(frozen=True)
class PreflightResult:
    """The cohort-wide verdict plus every gap that produced it."""

    state: PreflightState
    session_date: date
    members: tuple[str, ...] = ()
    """Every member the preflight assessed, in the order given."""

    gaps: tuple[DataGap, ...] = ()

    @property
    def ready(self) -> bool:
        return self.state is PreflightState.READY

    @property
    def structural(self) -> bool:
        """Whether any gap is one that waiting cannot resolve."""
        return self.state is PreflightState.MISCONFIGURED

    @property
    def structural_gaps(self) -> tuple[DataGap, ...]:
        return tuple(gap for gap in self.gaps if gap.structural)

    @property
    def unready_members(self) -> tuple[str, ...]:
        """Members with at least one gap, deduplicated and ordered by the roster."""
        blocked = {gap.member_id for gap in self.gaps}
        return tuple(member for member in self.members if member in blocked)

    @property
    def kinds(self) -> tuple[str, ...]:
        """Distinct data kinds that blocked the cohort, sorted."""
        return tuple(sorted({gap.kind for gap in self.gaps}))

    @property
    def reason_codes(self) -> tuple[str, ...]:
        """Distinct ``"<kind>:<reason>"`` codes, sorted and recorded exactly once.

        Deduplication is the point: five sleeves short of the same settled bar is one
        condition, and persisting ``daily_bars:session_not_covered`` five times (or,
        as the original defect did, twice per observation) tells an operator nothing
        extra while making the record harder to read.
        """
        return tuple(sorted({gap.code for gap in self.gaps}))

    @property
    def latest_session(self) -> date | None:
        """The furthest session any blocked requirement is actually settled through."""
        known = [gap.latest_session for gap in self.gaps if gap.latest_session is not None]
        return max(known) if known else None

    @property
    def summary(self) -> str:
        """One sanitized line naming what is missing and how far behind it is."""
        if self.ready:
            return (
                f"All {len(self.members)} member(s) have data settled through "
                f"{self.session_date.isoformat()}."
            )
        if self.structural:
            codes = ", ".join(sorted({gap.code for gap in self.structural_gaps}))
            return (
                f"{len(self.unready_members)}/{len(self.members)} member(s) cannot run for "
                f"{self.session_date.isoformat()}: a required capability is missing, "
                f"disabled, or not vintage-safe [{codes}]. Waiting cannot resolve this."
            )
        behind = self.latest_session
        through = "" if behind is None else f" Latest available session: {behind.isoformat()}."
        return (
            f"{len(self.unready_members)}/{len(self.members)} member(s) are awaiting data "
            f"for {self.session_date.isoformat()} [{', '.join(self.reason_codes)}].{through}"
        )

    def diagnostics(self) -> tuple[str, ...]:
        """One actionable, sanitized line per gap, newest concern first."""
        return tuple(
            f"{gap.member_id}: {gap.code} — target {gap.target_session.isoformat()}, "
            f"latest {'none' if gap.latest_session is None else gap.latest_session.isoformat()}"
            + (f", absent: {_sample(gap.missing_keys)}" if gap.missing_keys else "")
            + (f", short: {_sample(gap.uncovered_keys)}" if gap.uncovered_keys else "")
            for gap in self.gaps
        )


def assess_preflight(
    readiness_by_member: Mapping[str, DataReadiness],
    *,
    session_date: date,
    members: Sequence[str],
    quote_gaps: Mapping[str, Sequence[str]] | None = None,
    extra_gaps: Sequence[DataGap] = (),
) -> PreflightResult:
    """Decide whether the whole cohort may execute this session.

    ``readiness_by_member`` is keyed by the same member identifiers as ``members``; a
    member with no entry is treated as a gap, because an absent verdict is not a pass.
    ``quote_gaps`` maps a member to the universe symbols its shared quote snapshot did
    not cover — also a data gap, and also a reason nobody runs.

    ``extra_gaps`` lets a caller contribute gaps this module cannot derive from
    readiness alone — currently the execution session's opening evidence under a
    next-open methodology. They are judged by exactly the same all-or-nothing rule, so
    one member short of one opening bar still means no member executes.
    """
    roster = tuple(members)
    quotes = quote_gaps or {}
    gaps: list[DataGap] = []

    for member in roster:
        readiness = readiness_by_member.get(member)
        if readiness is None:
            gaps.append(
                DataGap(
                    member_id=member,
                    kind="readiness",
                    reason="not_evaluated",
                    target_session=session_date,
                    detail="No data-readiness result was captured for this member.",
                )
            )
            continue
        for requirement in readiness.unready():
            for reason in requirement.reasons:
                if reason is ReasonCode.OK:
                    continue
                gaps.append(
                    DataGap(
                        member_id=member,
                        kind=requirement.kind.value,
                        reason=reason.value,
                        target_session=requirement.required_session or session_date,
                        latest_session=requirement.latest_session,
                        missing_keys=requirement.missing_keys,
                        uncovered_keys=requirement.uncovered_keys,
                        detail=requirement.detail,
                    )
                )
        missing_quotes = tuple(sorted(quotes.get(member, ())))
        if missing_quotes:
            gaps.append(
                DataGap(
                    member_id=member,
                    kind=QUOTES_KIND,
                    reason=ReasonCode.MISSING_KEYS.value,
                    target_session=session_date,
                    missing_keys=missing_quotes,
                    detail=(
                        f"{QUOTES_KIND}: the shared snapshot did not cover "
                        f"{len(missing_quotes)} universe symbol(s)."
                    ),
                )
            )

    gaps.extend(extra_gaps)

    if not gaps:
        state = PreflightState.READY
    elif any(gap.structural for gap in gaps):
        state = PreflightState.MISCONFIGURED
    else:
        state = PreflightState.AWAITING_DATA
    return PreflightResult(
        state=state,
        session_date=session_date,
        members=roster,
        gaps=tuple(gaps),
    )


def preflight_payload(result: PreflightResult) -> dict[str, Any]:
    """The stable JSON contract for :class:`PreflightResult`.

    Every value is a JSON primitive. The payload carries sleeve, data-kind, session,
    and reason identity only — no connection string, account identifier, token, or raw
    provider payload can reach it.
    """
    return {
        "schema": PAYLOAD_SCHEMA,
        "state": result.state.value,
        "ready": result.ready,
        "structural": result.structural,
        "session_date": result.session_date.isoformat(),
        "latest_session": (
            None if result.latest_session is None else result.latest_session.isoformat()
        ),
        "summary": result.summary,
        "members": {
            "assessed": list(result.members),
            "unready": list(result.unready_members),
        },
        "reason_codes": list(result.reason_codes),
        "kinds": list(result.kinds),
        "gaps": [
            {
                "member_id": gap.member_id,
                "kind": gap.kind,
                "reason": gap.reason,
                "code": gap.code,
                "target_session": gap.target_session.isoformat(),
                "latest_session": (
                    None if gap.latest_session is None else gap.latest_session.isoformat()
                ),
                "missing_keys": list(gap.missing_keys),
                "uncovered_keys": list(gap.uncovered_keys),
                "structural": gap.structural,
                "detail": gap.detail,
            }
            for gap in result.gaps
        ],
    }


__all__ = [
    "OPENING_BARS_KIND",
    "PAYLOAD_SCHEMA",
    "QUOTES_KIND",
    "SIGNAL_QUOTES_KIND",
    "STRUCTURAL_REASONS",
    "DataGap",
    "DataKind",
    "PreflightResult",
    "PreflightState",
    "assess_preflight",
    "preflight_payload",
]
