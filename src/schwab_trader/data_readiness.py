"""Data-readiness results for the paper-sleeve provider seam.

A strategy run declares what data it needs as a set of :class:`DataRequirement`
values (which symbols/series, how fresh, whether point-in-time correctness is
required). Given what each capability actually produced - a source that is missing,
disabled, empty, partial, stale, or not vintage-safe - :func:`evaluate_readiness`
returns a :class:`DataReadiness` result with per-requirement coverage, the snapshot
identity that was seen, and machine-readable :class:`ReasonCode` values.

The guiding rules:

- A *price-only* requirement can be ready even when fundamentals or macro are
  entirely unavailable: readiness is judged per declared requirement, and the run is
  ready only when every one of its requirements is ready.
- Missing or disabled capabilities produce a clear unready result with a distinct
  reason, never a silent pass.
- A requirement that demands vintage-correct data fails against latest-revised
  inputs (e.g. default FRED macro), because those are not point-in-time safe.

Freshness has two modes, and picking the wrong one is how an official cohort session
quietly gets the wrong answer:

- **Session coverage** (``required_session`` set). The requirement names the exchange
  session it must be settled through, and each key's coverage is compared against that
  session *identity*. This is the correct mode for settled end-of-day bars.
- **Elapsed time** (``required_session`` unset). The legacy mode, kept for callers with
  no session context, compares ``now - as_of`` against ``max_staleness``.

Elapsed time is wrong for daily bars in both directions. On a Saturday it calls
Friday's correct settled bar stale because forty hours have passed; on a Monday evening
it can call Friday's bar fresh even though the session under judgement is Monday, and
Friday's close says nothing about Monday. Sessions, not hours, decide whether a settled
bar covers the day being recorded.

This module performs no I/O. Callers fetch batches through the capability protocols
in :mod:`schwab_trader.data_contracts` and hand the observed results here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from schwab_trader.data_contracts import BarBatch, DataBatch


class DataKind(StrEnum):
    """The category of data a requirement asks for."""

    DAILY_BARS = "daily_bars"
    FUNDAMENTALS = "fundamentals"
    MACRO = "macro"


# Predictive inputs must carry an explicit point-in-time availability policy.
_PREDICTIVE_KINDS = frozenset({DataKind.FUNDAMENTALS, DataKind.MACRO})


class ReasonCode(StrEnum):
    """Why a requirement is (not) ready. ``OK`` means the requirement is satisfied."""

    OK = "ok"
    SOURCE_MISSING = "source_missing"  # no capability was provided for this kind
    SOURCE_DISABLED = "source_disabled"  # capability present but turned off
    PROVIDER_ERROR = "provider_error"  # the capability raised instead of answering
    NO_DATA = "no_data"  # source produced an empty batch
    MISSING_KEYS = "missing_keys"  # some required symbols/series absent
    STALE = "stale"  # batch older than the allowed elapsed staleness
    SESSION_NOT_COVERED = "session_not_covered"  # settled through an earlier session
    MISSING_AVAILABLE_AT = "missing_available_at"  # predictive batch lacks availability
    NOT_VINTAGE_SAFE = "not_vintage_safe"  # latest-revised data where PIT required


class DataRequirement(BaseModel):
    """What one strategy run needs from a single data kind.

    Set ``required_session`` to the exchange session the data must be settled through.
    Doing so switches freshness from elapsed wall-clock time to session identity, which
    is the only correct test for settled end-of-day bars: it neither accepts Friday's
    close as evidence about Monday nor rejects it on the intervening weekend.
    """

    model_config = ConfigDict(frozen=True)

    kind: DataKind
    keys: tuple[str, ...] = ()
    max_staleness: timedelta = timedelta(days=1)
    require_vintage_safe: bool = False
    required_session: date | None = None


def session_coverage(batch: DataBatch | None) -> tuple[tuple[str, date], ...]:
    """The latest settled session each key in ``batch`` is covered through.

    Derived automatically for :class:`~schwab_trader.data_contracts.BarBatch`, whose
    observations carry an explicit ``session_date``. Any other batch shape reports no
    session coverage, so a caller that wants session-aligned freshness for it must
    supply the mapping itself rather than have one inferred.
    """
    if not isinstance(batch, BarBatch):
        return ()
    latest: dict[str, date] = {}
    for bar in batch.bars:
        seen = latest.get(bar.symbol)
        if seen is None or bar.session_date > seen:
            latest[bar.symbol] = bar.session_date
    return tuple(sorted(latest.items()))


class SourceProbe(BaseModel):
    """What a caller observed for one data kind before evaluating readiness.

    Kept separate from the batch so the *absence* of a capability (missing vs.
    disabled vs. failed vs. produced-nothing) is explicit rather than inferred from a
    null. Use the constructors :meth:`missing`, :meth:`disabled`, :meth:`failed`, and
    :meth:`of` instead of assembling the flags by hand.

    ``covered_sessions`` maps each key to the latest settled exchange session it is
    covered through, and is what :attr:`DataRequirement.required_session` is judged
    against.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    source_present: bool
    source_enabled: bool
    batch: DataBatch | None = None
    covered_sessions: tuple[tuple[str, date], ...] = ()
    error: str | None = None
    """Sanitized reason the capability raised, if it did. Never a provider payload."""

    @classmethod
    def missing(cls) -> SourceProbe:
        """No capability was configured for this kind."""
        return cls(source_present=False, source_enabled=False, batch=None)

    @classmethod
    def disabled(cls) -> SourceProbe:
        """A capability exists but is turned off."""
        return cls(source_present=True, source_enabled=False, batch=None)

    @classmethod
    def failed(cls, error: str) -> SourceProbe:
        """An enabled capability that raised instead of returning a batch.

        Distinct from :meth:`missing` and from an empty batch on purpose: a provider
        that errored is an operational fault to surface, not an absence to shrug at.
        Pass a sanitized reason such as an exception class name — never a payload,
        URL, credential, or account identifier.
        """
        return cls(source_present=True, source_enabled=True, batch=None, error=error)

    @classmethod
    def of(
        cls,
        batch: DataBatch | None,
        *,
        covered_sessions: Mapping[str, date] | Sequence[tuple[str, date]] | None = None,
    ) -> SourceProbe:
        """An enabled capability that returned ``batch`` (possibly ``None``/empty).

        Session coverage is derived from the batch when it is a ``BarBatch`` and
        ``covered_sessions`` is not given explicitly.
        """
        if covered_sessions is None:
            covered = session_coverage(batch)
        else:
            items = (
                covered_sessions.items()
                if isinstance(covered_sessions, Mapping)
                else covered_sessions
            )
            covered = tuple(sorted(items))
        return cls(
            source_present=True,
            source_enabled=True,
            batch=batch,
            covered_sessions=covered,
        )


class RequirementReadiness(BaseModel):
    """Readiness of a single :class:`DataRequirement`."""

    model_config = ConfigDict(frozen=True)

    kind: DataKind
    ready: bool
    required_keys: tuple[str, ...]
    present_keys: tuple[str, ...]
    missing_keys: tuple[str, ...]
    coverage: float = Field(ge=0.0, le=1.0)
    snapshot_id: str | None
    reasons: tuple[ReasonCode, ...]
    required_session: date | None = None
    """The exchange session this requirement had to be settled through, if any."""

    latest_session: date | None = None
    """The latest session any present key is actually covered through."""

    uncovered_keys: tuple[str, ...] = ()
    """Present keys whose coverage stops before :attr:`required_session`."""

    detail: str = ""
    """One sanitized operator-facing line. Identifiers only, never a payload."""


class DataReadiness(BaseModel):
    """Aggregate readiness across every declared requirement for a run."""

    model_config = ConfigDict(frozen=True)

    ready: bool
    evaluated_at: datetime
    requirements: tuple[RequirementReadiness, ...]

    def unready(self) -> tuple[RequirementReadiness, ...]:
        """Requirements that are not ready."""
        return tuple(req for req in self.requirements if not req.ready)

    @property
    def missing_capabilities(self) -> tuple[DataKind, ...]:
        """Kinds with no usable data (missing/disabled source or an empty batch)."""
        blocking = {ReasonCode.SOURCE_MISSING, ReasonCode.SOURCE_DISABLED, ReasonCode.NO_DATA}
        return tuple(req.kind for req in self.requirements if blocking.intersection(req.reasons))

    @property
    def stale_capabilities(self) -> tuple[DataKind, ...]:
        """Kinds whose data is present but does not reach the required freshness.

        Covers both freshness modes: elapsed-time staleness and a settled session that
        stops short of the one required.
        """
        behind = {ReasonCode.STALE, ReasonCode.SESSION_NOT_COVERED}
        return tuple(req.kind for req in self.requirements if behind.intersection(req.reasons))

    @property
    def failed_capabilities(self) -> tuple[DataKind, ...]:
        """Kinds whose provider raised instead of answering."""
        return tuple(
            req.kind for req in self.requirements if ReasonCode.PROVIDER_ERROR in req.reasons
        )

    @property
    def snapshot_ids(self) -> dict[str, str]:
        """The snapshot id seen for each requirement kind that had one."""
        return {
            req.kind.value: req.snapshot_id
            for req in self.requirements
            if req.snapshot_id is not None
        }


#: How many key names a diagnostic line names before it summarizes the rest. Keeps an
#: operator-facing message readable when a 74-symbol universe is short.
_DETAIL_KEY_SAMPLE = 5


def _sample(keys: Sequence[str]) -> str:
    """Name a few keys and count the rest, so a wide universe stays readable."""
    head = ", ".join(keys[:_DETAIL_KEY_SAMPLE])
    extra = len(keys) - _DETAIL_KEY_SAMPLE
    return f"{head} (+{extra} more)" if extra > 0 else head


def evaluate_requirement(
    requirement: DataRequirement, probe: SourceProbe, *, now: datetime
) -> RequirementReadiness:
    """Judge one requirement against what its capability produced.

    Reason codes are deduplicated, so a caller that flattens them into a persisted
    observation records each condition exactly once.
    """
    required = requirement.keys
    target = requirement.required_session

    def unready(reason: ReasonCode, detail: str) -> RequirementReadiness:
        return RequirementReadiness(
            kind=requirement.kind,
            ready=False,
            required_keys=required,
            present_keys=(),
            missing_keys=required,
            coverage=0.0,
            snapshot_id=None,
            reasons=(reason,),
            required_session=target,
            detail=detail,
        )

    kind = requirement.kind.value
    if not probe.source_present:
        return unready(ReasonCode.SOURCE_MISSING, f"{kind}: no capability is configured.")
    if not probe.source_enabled:
        return unready(ReasonCode.SOURCE_DISABLED, f"{kind}: the capability is disabled.")
    if probe.error is not None:
        return unready(
            ReasonCode.PROVIDER_ERROR,
            f"{kind}: the provider failed ({probe.error}); no batch was produced.",
        )
    batch = probe.batch
    if batch is None or batch.is_empty():
        return unready(ReasonCode.NO_DATA, f"{kind}: the provider returned no records.")

    covered = batch.covered_keys
    present = tuple(key for key in required if key in covered)
    missing = tuple(key for key in required if key not in covered)
    coverage = 1.0 if not required else len(present) / len(required)

    reasons: list[ReasonCode] = []
    details: list[str] = []
    if missing:
        reasons.append(ReasonCode.MISSING_KEYS)
        details.append(f"{len(missing)} key(s) absent: {_sample(missing)}")

    provenance = batch.provenance
    coverage_by_key = dict(probe.covered_sessions)
    latest_session = max(coverage_by_key.values(), default=None)
    uncovered: tuple[str, ...] = ()

    if target is not None:
        # Session identity, not elapsed hours. Only keys the batch actually carries are
        # judged here; keys it lacks entirely are already reported as MISSING_KEYS.
        scope = present or tuple(sorted(coverage_by_key))
        if not scope:
            # Nothing to judge means nothing proves the session is covered. Fail closed:
            # a requirement that names no keys against a batch with no session coverage
            # must not read as settled evidence about a session it never mentions.
            reasons.append(ReasonCode.SESSION_NOT_COVERED)
            details.append(
                f"no session coverage was reported, so nothing evidences "
                f"{target.isoformat()}"
            )
        else:
            uncovered = tuple(
                key
                for key in scope
                if (settled := coverage_by_key.get(key)) is None or settled < target
            )
            if uncovered:
                reasons.append(ReasonCode.SESSION_NOT_COVERED)
                through = "nothing" if latest_session is None else latest_session.isoformat()
                details.append(
                    f"settled through {through}, required through {target.isoformat()}; "
                    f"{len(uncovered)} key(s) short: {_sample(uncovered)}"
                )
    elif now - provenance.as_of > requirement.max_staleness:
        reasons.append(ReasonCode.STALE)
        details.append(
            f"as of {provenance.as_of.isoformat()} exceeds the "
            f"{requirement.max_staleness} allowance at {now.isoformat()}"
        )

    if requirement.kind in _PREDICTIVE_KINDS and provenance.available_at is None:
        reasons.append(ReasonCode.MISSING_AVAILABLE_AT)
        details.append("no point-in-time availability was declared")

    if requirement.require_vintage_safe and not provenance.vintage_safe:
        reasons.append(ReasonCode.NOT_VINTAGE_SAFE)
        details.append(f"'{provenance.timing.value}' data is not vintage-safe")

    # Deduplicate while preserving first-seen order so a flattened reason list records
    # each condition exactly once.
    ordered_reasons = tuple(dict.fromkeys(reasons))
    ready = not ordered_reasons
    return RequirementReadiness(
        kind=requirement.kind,
        ready=ready,
        required_keys=required,
        present_keys=present,
        missing_keys=missing,
        coverage=coverage,
        snapshot_id=provenance.snapshot_id,
        reasons=ordered_reasons if ordered_reasons else (ReasonCode.OK,),
        required_session=target,
        latest_session=latest_session,
        uncovered_keys=uncovered,
        detail=f"{kind}: {'; '.join(details)}." if details else f"{kind}: ready.",
    )


def evaluate_readiness(
    pairs: Sequence[tuple[DataRequirement, SourceProbe]], *, now: datetime
) -> DataReadiness:
    """Evaluate every ``(requirement, probe)`` pair into one readiness result.

    The run is ready only when every requirement is ready. An empty set of
    requirements is trivially ready (a run that needs no data).
    """
    results = tuple(
        evaluate_requirement(requirement, probe, now=now) for requirement, probe in pairs
    )
    ready = all(result.ready for result in results)
    return DataReadiness(ready=ready, evaluated_at=now, requirements=results)


__all__ = [
    "DataKind",
    "DataReadiness",
    "DataRequirement",
    "ReasonCode",
    "RequirementReadiness",
    "SourceProbe",
    "evaluate_readiness",
    "evaluate_requirement",
    "session_coverage",
]
