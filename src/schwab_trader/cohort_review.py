"""Durable, append-only accounting review and operator decisions for a paper cohort.

The operational gate in :mod:`schwab_trader.operational_gate` already knows how to judge
two pieces of *human* evidence: an accounting review of every official observation, and
reasoned keep/modify/pause/retire decisions about the cohort's sleeves. Until now nothing
produced either, so every caller handed it ``None`` and both rules failed closed forever.

This module is the missing producer. It records what an operator actually checked, what
differed, why, and what they decided — durably, and in a form the gate already accepts.

Three properties are deliberate and load-bearing:

**Append-only.** Nothing here ever updates or deletes a recorded row. A correction is a
new row with the next ``revision`` for the same key, and the earlier row stays exactly as
it was written. "Current" is therefore a *query* (highest revision per key), not a state
some writer maintains, so a correction cannot silently rewrite the record it corrects and
a crash mid-correction cannot leave a half-superseded row behind.

**Deterministic identity.** Every record's primary key is a SHA-256 of the identity it
belongs to, including the revision. Repeating the same command is a no-op that returns
the existing record rather than a second copy, and two concurrent writers racing to add
the same revision collide on a unique constraint instead of both winning.

**Validated at the service boundary, not the database.** Cohort membership and official
observations live in the sleeve registry and the evaluation store, and in the local
layout those are not SQL tables at all — so a foreign key cannot express "this sleeve is
an immutable member of this cohort". :class:`CohortReviewService` checks every reference
against a :class:`ReviewContext` built from the authoritative records before it writes,
and refuses the whole call rather than writing part of it.

Nothing in this module runs, re-runs, schedules, or modifies a cohort, a sleeve
definition, a paper position, a strategy parameter, or a promotion state, and nothing
here reaches a broker or order path. A decision recorded here is a *research
disposition*: ``keep`` does not promote a sleeve and ``retire`` does not stop one. The
gate's :attr:`~schwab_trader.operational_gate.OperationalGateResult.investment_alpha_assessed`
and ``live_trading_authorized`` remain hard ``False`` regardless of what is recorded.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from schwab_trader import evidence_timing
from schwab_trader.cohort_phase import DEFAULT_REVIEW_TARGET, assess_cohort_phase
from schwab_trader.evaluation import ObservationStatus, OfficialDailyObservation
from schwab_trader.operational_gate import (
    AccountingArea,
    AccountingDifference,
    AccountingEvidence,
    OperatorAction,
    OperatorDecision,
)
from schwab_trader.sleeve_runs import SleeveRun
from schwab_trader.sleeves import SleeveConfig

#: Every accounting area an observation review must cover before the observation counts
#: as reviewed. The gate asks whether an official observation was checked; a check of one
#: area out of three is not that, so partial coverage is reported as still pending.
REQUIRED_AREAS: tuple[AccountingArea, ...] = (
    AccountingArea.CASH,
    AccountingArea.POSITIONS,
    AccountingArea.VALUATION,
)

#: Default label recorded as the author of a review record. Deliberately a role, not a
#: person, account, or credential: durable review records are read by tooling and shown
#: in the dashboard, and none of those places may carry an identity.
DEFAULT_RECORDED_BY = "operator"

_MAX_TEXT = 4000
_MAX_RECORDED_BY = 64


class ReviewFinding(StrEnum):
    """What an operator concluded about one accounting area of one observation."""

    MATCHED = "matched"
    """The recorded figures reconcile; there is nothing to explain."""

    DIFFERENCE = "difference"
    """A difference was found. It needs a summary, and an explanation to clear it."""


class CohortReviewError(ValueError):
    """Base class for a refused review action. Never raised after a partial write."""


class ReviewValidationError(CohortReviewError):
    """A reference, enum value, or required text failed validation."""


class ReviewConflictError(CohortReviewError):
    """A record already exists with different content and no supersede was authorized."""


class ReviewNotDueError(CohortReviewError):
    """A final operator decision was attempted before the formal review is due."""


# --- durable records --------------------------------------------------------


class AccountingCheck(BaseModel):
    """One durable, immutable accounting-review entry for one observation and area."""

    model_config = ConfigDict(frozen=True)

    entry_id: str
    cohort_id: str
    sleeve_id: str
    observation_key: str
    session_date: date
    area: AccountingArea
    finding: ReviewFinding
    summary: str | None = None
    explanation: str | None = None
    recorded_at: datetime
    recorded_by: str
    revision: int
    supersedes: str | None = None

    @property
    def explained(self) -> bool:
        """Whether this entry leaves nothing outstanding for the gate.

        A matched area is trivially settled. A difference is settled only by a nonblank
        explanation, which is exactly the condition
        :func:`~schwab_trader.operational_gate.assess_operational_usefulness` applies.
        """
        if self.finding is ReviewFinding.MATCHED:
            return True
        return bool(self.explanation and self.explanation.strip())


class ReviewNote(BaseModel):
    """One durable operator note attached to a cohort, sleeve, or observation."""

    model_config = ConfigDict(frozen=True)

    note_id: str
    cohort_id: str
    sleeve_id: str | None = None
    observation_key: str | None = None
    note: str
    recorded_at: datetime
    recorded_by: str


class SleeveDecision(BaseModel):
    """One durable, immutable keep/modify/pause/retire decision about one sleeve."""

    model_config = ConfigDict(frozen=True)

    decision_id: str
    cohort_id: str
    sleeve_id: str
    action: OperatorAction
    rationale: str
    recorded_at: datetime
    recorded_by: str
    revision: int
    supersedes: str | None = None


# --- write intents ----------------------------------------------------------


@dataclass(frozen=True)
class CheckIntent:
    """A fully validated accounting-check write, ready for the store."""

    cohort_id: str
    sleeve_id: str
    observation_key: str
    session_date: date
    area: AccountingArea
    finding: ReviewFinding
    summary: str | None
    explanation: str | None
    recorded_at: datetime
    recorded_by: str

    def matches(self, record: AccountingCheck) -> bool:
        """Whether an existing record already says exactly this.

        ``recorded_at`` is deliberately excluded: re-running the same command a minute
        later is the same finding, not a new one, and treating the clock as content
        would turn every retry into a spurious correction.
        """
        return (
            record.finding is self.finding
            and record.summary == self.summary
            and record.explanation == self.explanation
        )


@dataclass(frozen=True)
class NoteIntent:
    """A fully validated operator-note write."""

    cohort_id: str
    sleeve_id: str | None
    observation_key: str | None
    note: str
    recorded_at: datetime
    recorded_by: str


@dataclass(frozen=True)
class DecisionIntent:
    """A fully validated operator-decision write."""

    cohort_id: str
    sleeve_id: str
    action: OperatorAction
    rationale: str
    recorded_at: datetime
    recorded_by: str

    def matches(self, record: SleeveDecision) -> bool:
        """Whether an existing decision already says exactly this."""
        return record.action is self.action and record.rationale == self.rationale


class WriteStatus(StrEnum):
    """What a write actually did to the durable record."""

    RECORDED = "recorded"
    """A first record for this identity was appended."""

    UNCHANGED = "unchanged"
    """An identical record already existed; nothing was written."""

    SUPERSEDED = "superseded"
    """A correction was appended as the next revision; the prior record is untouched."""


@dataclass(frozen=True)
class CheckOutcome:
    """The result of recording one accounting check."""

    status: WriteStatus
    record: AccountingCheck


@dataclass(frozen=True)
class NoteOutcome:
    """The result of adding one operator note."""

    status: WriteStatus
    record: ReviewNote


@dataclass(frozen=True)
class DecisionOutcome:
    """The result of recording one operator decision."""

    status: WriteStatus
    record: SleeveDecision


# --- identities -------------------------------------------------------------


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def check_entry_id(
    cohort_id: str, observation_key: str, area: AccountingArea, revision: int
) -> str:
    """Stable identity for one revision of one observation/area review entry."""
    return _digest(cohort_id, observation_key, area.value, str(revision))


def decision_entry_id(cohort_id: str, sleeve_id: str, revision: int) -> str:
    """Stable identity for one revision of one sleeve's operator decision."""
    return _digest(cohort_id, sleeve_id, str(revision))


def note_entry_id(
    cohort_id: str, sleeve_id: str | None, observation_key: str | None, note: str
) -> str:
    """Stable identity for one note.

    Deliberately excludes ``recorded_at``: an identical note about the same target adds
    no information, so repeating the command is a no-op rather than a second copy. Any
    change to the text is a different note and gets its own record.
    """
    return _digest(cohort_id, sleeve_id or "", observation_key or "", note)


# --- repository protocol ----------------------------------------------------


class CohortReviewRepository(Protocol):
    """Append-only persistence for one cohort's review records.

    Implementations must be atomic per call and must never update or delete a row that
    was already written. ``record_*`` returns the existing record unchanged when the
    intent already matches it, and raises :class:`ReviewConflictError` when the content
    differs and ``allow_supersede`` is false.
    """

    def record_check(self, intent: CheckIntent, *, allow_supersede: bool) -> CheckOutcome: ...

    def add_note(self, intent: NoteIntent) -> NoteOutcome: ...

    def record_decision(
        self, intent: DecisionIntent, *, allow_supersede: bool
    ) -> DecisionOutcome: ...

    def checks(self, cohort_id: str) -> list[AccountingCheck]: ...

    def notes(self, cohort_id: str) -> list[ReviewNote]: ...

    def decisions(self, cohort_id: str) -> list[SleeveDecision]: ...


# --- review context ---------------------------------------------------------


@dataclass(frozen=True)
class ObservationRef:
    """The identity of one official observation a review entry may reference."""

    observation_key: str
    sleeve_id: str
    session_date: date


@dataclass(frozen=True)
class ReviewContext:
    """Everything a write needs to be validated against the authoritative records.

    Built by :func:`build_context` from the sleeve registry, the durable run store, and
    the evaluation store. Holding it as a value makes every validation rule testable
    without a database and makes it impossible for a write path to consult a different
    notion of membership than the read path.
    """

    cohort_id: str
    member_sleeve_ids: frozenset[str]
    observations: Mapping[str, ObservationRef]
    completed_due_sessions: int
    review_target: int

    @property
    def review_due(self) -> bool:
        """Whether the formal 30-session operational review is owed yet."""
        return self.completed_due_sessions >= self.review_target

    def sleeve_of(self, observation_key: str) -> str:
        return self.observations[observation_key].sleeve_id


def _is_due(
    session_date: date,
    as_of: date | None,
    window: evidence_timing.EvidenceWindow | None,
) -> bool:
    """Whether a session has delivered evidence yet, under whichever clock was given.

    Mirrors ``operational_gate._is_due_observation`` exactly. The two must agree: the
    gate decides which observations it judges, and this decides which ones an operator
    may record a review for.
    """
    if window is not None:
        return window.counts_evidence_observation(session_date)
    return as_of is None or session_date <= as_of


def build_context(
    *,
    cohort_id: str,
    configs: Sequence[SleeveConfig],
    runs: Sequence[SleeveRun],
    observations: Sequence[OfficialDailyObservation],
    now_et: datetime | None = None,
    as_of: date | None = None,
    start_session: date | None = None,
    review_target: int = DEFAULT_REVIEW_TARGET,
) -> ReviewContext:
    """Build a validation context from persisted cohort records.

    Membership comes from the sleeve registry rather than from whatever identities happen
    to appear in observations, so a stray observation cannot make a non-member reviewable.
    Only observations that are all of: in this cohort, :attr:`ObservationStatus.OFFICIAL`,
    owned by a current member, and *due* under the injected clock are reviewable. The due
    filter is the same :mod:`schwab_trader.evidence_timing` window the operational gate
    applies, which matters in both directions: an operator cannot review a session that
    has not closed yet, and a review of a session the gate is not judging would arrive at
    the gate as an unknown identity and fail it.

    Due-ness reuses :func:`~schwab_trader.cohort_phase.assess_cohort_phase` rather than
    counting sessions here, so "is the review due?" cannot drift from what the dashboard
    and the phase headline say.
    """
    phase = assess_cohort_phase(
        cohort_id=cohort_id,
        runs=runs,
        observations=observations,
        gate=None,
        now_et=now_et,
        as_of=as_of,
        start_session=start_session,
        review_target=review_target,
    )
    cohort_runs = [run for run in runs if run.cohort_id == cohort_id]
    window = (
        None
        if now_et is None
        else evidence_timing.assess_runs(cohort_runs, now_et=now_et)
    )
    members = frozenset(config.identity for config in configs if config.cohort_id == cohort_id)
    reviewable = {
        observation.observation_key: ObservationRef(
            observation_key=observation.observation_key,
            sleeve_id=observation.sleeve_id,
            session_date=observation.session_date,
        )
        for observation in observations
        if observation.cohort_id == cohort_id
        and observation.status is ObservationStatus.OFFICIAL
        and observation.sleeve_id in members
        and _is_due(observation.session_date, as_of, window)
    }
    return ReviewContext(
        cohort_id=cohort_id,
        member_sleeve_ids=members,
        observations=reviewable,
        completed_due_sessions=phase.completed_due_sessions,
        review_target=phase.review_target,
    )


# --- the current review -----------------------------------------------------


class PendingObservation(BaseModel):
    """One official observation whose accounting review is not complete yet."""

    model_config = ConfigDict(frozen=True)

    observation_key: str
    sleeve_id: str
    session_date: date
    missing_areas: tuple[AccountingArea, ...]


class CohortReview(BaseModel):
    """The deterministic current view of one cohort's review, plus its full history."""

    model_config = ConfigDict(frozen=True)

    cohort_id: str
    review_target: int
    completed_due_sessions: int
    review_due: bool
    official_observations: int
    reviewed_observations: int
    pending_observations: tuple[PendingObservation, ...] = ()
    covered_observation_keys: tuple[str, ...] = ()
    """Official observations whose every required accounting area has a current entry.

    Partial coverage deliberately does not appear here: the gate asks whether an
    observation was reviewed, and a cash-only check is not a review of it.
    """

    checks: tuple[AccountingCheck, ...] = ()
    superseded_checks: tuple[AccountingCheck, ...] = ()
    notes: tuple[ReviewNote, ...] = ()
    decisions: tuple[SleeveDecision, ...] = ()
    superseded_decisions: tuple[SleeveDecision, ...] = ()

    @property
    def differences(self) -> tuple[AccountingCheck, ...]:
        """Every current entry that recorded a difference, explained or not."""
        return tuple(item for item in self.checks if item.finding is ReviewFinding.DIFFERENCE)

    @property
    def unexplained_differences(self) -> tuple[AccountingCheck, ...]:
        """Current differences with no usable explanation. These fail the gate."""
        return tuple(item for item in self.differences if not item.explained)

    @property
    def reviewable_observation_keys(self) -> frozenset[str]:
        """Every official, due observation this review was assembled against."""
        return frozenset(self.covered_observation_keys) | frozenset(
            item.observation_key for item in self.pending_observations
        )

    @property
    def decided_sleeves(self) -> frozenset[str]:
        return frozenset(item.sleeve_id for item in self.decisions)

    @property
    def has_records(self) -> bool:
        """Whether anything at all has been recorded for this cohort."""
        return bool(self.checks or self.notes or self.decisions)


def _current_checks(
    records: Sequence[AccountingCheck],
) -> tuple[list[AccountingCheck], list[AccountingCheck]]:
    """Split checks into (current, superseded) by highest revision per identity."""
    latest: dict[tuple[str, str], AccountingCheck] = {}
    for record in records:
        identity = (record.observation_key, record.area.value)
        seen = latest.get(identity)
        if seen is None or record.revision > seen.revision:
            latest[identity] = record
    current_ids = {record.entry_id for record in latest.values()}
    ordering = sorted(
        latest.values(), key=lambda item: (item.session_date, item.sleeve_id, item.area.value)
    )
    superseded = sorted(
        (record for record in records if record.entry_id not in current_ids),
        key=lambda item: (item.session_date, item.sleeve_id, item.area.value, item.revision),
    )
    return ordering, superseded


def _current_decisions(
    records: Sequence[SleeveDecision],
) -> tuple[list[SleeveDecision], list[SleeveDecision]]:
    latest: dict[str, SleeveDecision] = {}
    for record in records:
        seen = latest.get(record.sleeve_id)
        if seen is None or record.revision > seen.revision:
            latest[record.sleeve_id] = record
    current_ids = {record.decision_id for record in latest.values()}
    ordering = sorted(latest.values(), key=lambda item: item.sleeve_id)
    superseded = sorted(
        (record for record in records if record.decision_id not in current_ids),
        key=lambda item: (item.sleeve_id, item.revision),
    )
    return ordering, superseded


def assemble_review(context: ReviewContext, repository: CohortReviewRepository) -> CohortReview:
    """Read every record for the cohort and reduce it to the current review.

    Records referring to observations that are no longer official (or to sleeves no
    longer in the cohort) are kept in the history but never counted as coverage, so a
    review can never claim to have checked evidence the cohort does not have.
    """
    stored_checks = [
        record
        for record in repository.checks(context.cohort_id)
        if record.cohort_id == context.cohort_id
    ]
    current_checks, superseded_checks = _current_checks(stored_checks)
    stored_decisions = [
        record
        for record in repository.decisions(context.cohort_id)
        if record.cohort_id == context.cohort_id
    ]
    current_decisions, superseded_decisions = _current_decisions(stored_decisions)

    by_observation: dict[str, set[AccountingArea]] = {}
    for record in current_checks:
        if record.observation_key in context.observations:
            by_observation.setdefault(record.observation_key, set()).add(record.area)

    pending: list[PendingObservation] = []
    covered: list[str] = []
    for key, reference in sorted(
        context.observations.items(), key=lambda item: (item[1].session_date, item[1].sleeve_id)
    ):
        missing = tuple(
            area for area in REQUIRED_AREAS if area not in by_observation.get(key, set())
        )
        if missing:
            pending.append(
                PendingObservation(
                    observation_key=key,
                    sleeve_id=reference.sleeve_id,
                    session_date=reference.session_date,
                    missing_areas=missing,
                )
            )
        else:
            covered.append(key)

    return CohortReview(
        cohort_id=context.cohort_id,
        review_target=context.review_target,
        completed_due_sessions=context.completed_due_sessions,
        review_due=context.review_due,
        official_observations=len(context.observations),
        reviewed_observations=len(covered),
        pending_observations=tuple(pending),
        covered_observation_keys=tuple(covered),
        checks=tuple(current_checks),
        superseded_checks=tuple(superseded_checks),
        notes=tuple(repository.notes(context.cohort_id)),
        decisions=tuple(current_decisions),
        superseded_decisions=tuple(superseded_decisions),
    )


# --- gate evidence ----------------------------------------------------------


def accounting_evidence(review: CohortReview) -> AccountingEvidence | None:
    """The gate's accounting evidence, or ``None`` when there is nothing to judge.

    There are two null cases and they fail closed identically: nothing has been recorded,
    or the review was assembled against no official observations at all.

    Returning ``None`` rather than an empty record is the whole point of the null case:
    the gate reports "accounting evidence is missing" with ``awaiting_evidence`` set, so
    a cohort that has not been reviewed yet still displays as *awaiting evidence* before
    the review is due and as *actionable* once it is. An empty
    :class:`~schwab_trader.operational_gate.AccountingEvidence` would instead assert that
    a review happened and found nothing, which is a different and false claim.
    """
    if not review.checks:
        return None
    # A review assembled against no observations at all is not evidence about them, and
    # must not be offered as any. The recorded checks and the official observations come
    # from different stores, so a failed observation read leaves every check intact while
    # the official set collapses to empty -- and the gate would then compare an empty
    # checked set against an empty official set, pass, and report that every observation
    # was reviewed and every difference explained, when nothing could even be read.
    #
    # This is gated on the observation count rather than on empty coverage on purpose:
    # genuine partial coverage must keep failing loudly with "unreviewed observation
    # identities" instead of quietly reverting to awaiting-evidence.
    if not review.official_observations:
        return None
    # A record about a session outside the current evidence window is preserved in the
    # review but is not offered to the gate as evidence about that session: the gate
    # treats an identity it is not judging as ambiguous and fails, which would turn a
    # correctly recorded review into a spurious failure every time the clock moved.
    reviewable = review.reviewable_observation_keys
    return AccountingEvidence(
        checked_observation_keys=frozenset(review.covered_observation_keys),
        differences=tuple(
            AccountingDifference(
                observation_key=item.observation_key,
                area=item.area,
                summary=item.summary or "",
                explained=item.explained,
                explanation=item.explanation,
            )
            for item in review.differences
            if item.observation_key in reviewable
        ),
    )


def operator_decisions(review: CohortReview) -> tuple[OperatorDecision, ...] | None:
    """The gate's operator decisions, or ``None`` when none has been recorded.

    Only current decisions are supplied. A superseded decision is preserved for audit but
    must never be counted twice, and the gate counts records.
    """
    if not review.decisions:
        return None
    return tuple(
        OperatorDecision(
            cohort_id=record.cohort_id,
            sleeve_id=record.sleeve_id,
            action=record.action,
            rationale=record.rationale,
            recorded_at=record.recorded_at,
        )
        for record in review.decisions
    )


# --- the service ------------------------------------------------------------


def _clean(value: str | None, *, field: str, required: bool) -> str | None:
    if value is None or not value.strip():
        if required:
            raise ReviewValidationError(f"{field} must not be blank")
        return None
    text = value.strip()
    if len(text) > _MAX_TEXT:
        raise ReviewValidationError(f"{field} must be at most {_MAX_TEXT} characters")
    return text


def _aware(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReviewValidationError(f"{field} must be timezone-aware")
    return value


def _author(value: str) -> str:
    text = value.strip()
    if not text:
        raise ReviewValidationError("recorded_by must not be blank")
    if len(text) > _MAX_RECORDED_BY:
        raise ReviewValidationError(f"recorded_by must be at most {_MAX_RECORDED_BY} characters")
    return text


class CohortReviewService:
    """Validate every review write against the cohort's authoritative records.

    Every rule is checked before the repository is called, so a refused call performs no
    write at all — there is no path that records a difference and then fails to record
    its explanation.
    """

    def __init__(self, repository: CohortReviewRepository) -> None:
        self.repository = repository

    def review(self, context: ReviewContext) -> CohortReview:
        """The current review for one cohort. Read-only."""
        return assemble_review(context, self.repository)

    def record_check(
        self,
        context: ReviewContext,
        *,
        observation_key: str,
        area: AccountingArea,
        finding: ReviewFinding,
        summary: str | None = None,
        explanation: str | None = None,
        recorded_at: datetime,
        recorded_by: str = DEFAULT_RECORDED_BY,
        allow_supersede: bool = False,
    ) -> CheckOutcome:
        """Record one accounting area of one official observation as checked.

        A difference needs a summary saying what differed. An *explained* difference also
        needs a nonblank explanation; without one the entry is recorded honestly as an
        outstanding difference and the gate keeps failing, which is the correct outcome —
        an unexplained difference is exactly the state the review exists to surface.
        """
        reference = context.observations.get(observation_key)
        if reference is None:
            raise ReviewValidationError(
                f"{observation_key!r} is not an official observation in cohort "
                f"{context.cohort_id!r}"
            )
        if area not in REQUIRED_AREAS:
            raise ReviewValidationError(f"unknown accounting area {area!r}")
        if reference.sleeve_id not in context.member_sleeve_ids:
            raise ReviewValidationError(
                f"sleeve {reference.sleeve_id!r} is not a member of cohort {context.cohort_id!r}"
            )
        clean_summary = _clean(
            summary, field="summary", required=finding is ReviewFinding.DIFFERENCE
        )
        clean_explanation = _clean(explanation, field="explanation", required=False)
        if finding is ReviewFinding.MATCHED and clean_explanation is not None:
            raise ReviewValidationError(
                "a matched area has nothing to explain; record it as a difference instead"
            )
        intent = CheckIntent(
            cohort_id=context.cohort_id,
            sleeve_id=reference.sleeve_id,
            observation_key=observation_key,
            session_date=reference.session_date,
            area=area,
            finding=finding,
            summary=clean_summary,
            explanation=clean_explanation,
            recorded_at=_aware(recorded_at, field="recorded_at"),
            recorded_by=_author(recorded_by),
        )
        return self.repository.record_check(intent, allow_supersede=allow_supersede)

    def add_note(
        self,
        context: ReviewContext,
        *,
        note: str,
        sleeve_id: str | None = None,
        observation_key: str | None = None,
        recorded_at: datetime,
        recorded_by: str = DEFAULT_RECORDED_BY,
    ) -> NoteOutcome:
        """Attach one durable note to the cohort, a member sleeve, or an observation."""
        text = _clean(note, field="note", required=True)
        assert text is not None  # _clean raises when required and blank
        if observation_key is not None:
            reference = context.observations.get(observation_key)
            if reference is None:
                raise ReviewValidationError(
                    f"{observation_key!r} is not an official observation in cohort "
                    f"{context.cohort_id!r}"
                )
            if sleeve_id is not None and sleeve_id != reference.sleeve_id:
                raise ReviewValidationError(
                    "the note's sleeve does not own the referenced observation"
                )
            sleeve_id = reference.sleeve_id
        if sleeve_id is not None and sleeve_id not in context.member_sleeve_ids:
            raise ReviewValidationError(
                f"sleeve {sleeve_id!r} is not a member of cohort {context.cohort_id!r}"
            )
        intent = NoteIntent(
            cohort_id=context.cohort_id,
            sleeve_id=sleeve_id,
            observation_key=observation_key,
            note=text,
            recorded_at=_aware(recorded_at, field="recorded_at"),
            recorded_by=_author(recorded_by),
        )
        return self.repository.add_note(intent)

    def record_decision(
        self,
        context: ReviewContext,
        *,
        sleeve_id: str,
        action: OperatorAction,
        rationale: str,
        recorded_at: datetime,
        recorded_by: str = DEFAULT_RECORDED_BY,
        allow_supersede: bool = False,
    ) -> DecisionOutcome:
        """Record the final keep/modify/pause/retire disposition for one sleeve.

        Refused before the formal review is due. The gate treats a recorded decision as
        the operator having judged 30 sessions of evidence; accepting one on day three
        would let a cohort satisfy that rule without the evidence ever existing.

        The action is a *research* disposition. Nothing here promotes, pauses, retires,
        or reconfigures the running sleeve, and nothing here authorizes live trading.
        """
        if sleeve_id not in context.member_sleeve_ids:
            raise ReviewValidationError(
                f"sleeve {sleeve_id!r} is not a member of cohort {context.cohort_id!r}"
            )
        if not context.review_due:
            raise ReviewNotDueError(
                f"the operational review is not due yet: {context.completed_due_sessions} of "
                f"{context.review_target} completed due sessions"
            )
        text = _clean(rationale, field="rationale", required=True)
        assert text is not None  # _clean raises when required and blank
        intent = DecisionIntent(
            cohort_id=context.cohort_id,
            sleeve_id=sleeve_id,
            action=action,
            rationale=text,
            recorded_at=_aware(recorded_at, field="recorded_at"),
            recorded_by=_author(recorded_by),
        )
        return self.repository.record_decision(intent, allow_supersede=allow_supersede)


def parse_area(value: str) -> AccountingArea:
    """Parse an operator-supplied accounting area, failing closed on anything else."""
    try:
        return AccountingArea(value.strip().casefold())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in REQUIRED_AREAS)
        raise ReviewValidationError(f"unknown accounting area {value!r}; choose {allowed}") from exc


def parse_action(value: str) -> OperatorAction:
    """Parse an operator-supplied decision, failing closed on anything else."""
    try:
        return OperatorAction(value.strip().casefold())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in OperatorAction)
        raise ReviewValidationError(f"unknown operator action {value!r}; choose {allowed}") from exc


def parse_finding(value: str) -> ReviewFinding:
    """Parse an operator-supplied finding, failing closed on anything else."""
    try:
        return ReviewFinding(value.strip().casefold())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in ReviewFinding)
        raise ReviewValidationError(f"unknown finding {value!r}; choose {allowed}") from exc


def review_payload(review: CohortReview) -> dict[str, object]:
    """JSON-safe stable contract for the operator CLI and the dashboard API."""

    def check(item: AccountingCheck) -> dict[str, object]:
        return {
            "entry_id": item.entry_id,
            "observation_key": item.observation_key,
            "sleeve_id": item.sleeve_id,
            "session_date": item.session_date.isoformat(),
            "area": item.area.value,
            "finding": item.finding.value,
            "summary": item.summary,
            "explanation": item.explanation,
            "explained": item.explained,
            "recorded_at": item.recorded_at.isoformat(),
            "recorded_by": item.recorded_by,
            "revision": item.revision,
            "supersedes": item.supersedes,
        }

    def decision(item: SleeveDecision) -> dict[str, object]:
        return {
            "decision_id": item.decision_id,
            "sleeve_id": item.sleeve_id,
            "action": item.action.value,
            "rationale": item.rationale,
            "recorded_at": item.recorded_at.isoformat(),
            "recorded_by": item.recorded_by,
            "revision": item.revision,
            "supersedes": item.supersedes,
        }

    return {
        "cohort_id": review.cohort_id,
        "review_target": review.review_target,
        "completed_due_sessions": review.completed_due_sessions,
        "review_due": review.review_due,
        "official_observations": review.official_observations,
        "reviewed_observations": review.reviewed_observations,
        # A count, named as one. Every plural key in this payload is a list; a bare
        # `unexplained_differences` holding an integer is exactly the kind of contract a
        # consumer reads once and gets wrong forever.
        "unexplained_difference_count": len(review.unexplained_differences),
        "pending_observations": [
            {
                "observation_key": item.observation_key,
                "sleeve_id": item.sleeve_id,
                "session_date": item.session_date.isoformat(),
                "missing_areas": [area.value for area in item.missing_areas],
            }
            for item in review.pending_observations
        ],
        "checks": [check(item) for item in review.checks],
        "superseded_checks": [check(item) for item in review.superseded_checks],
        "notes": [
            {
                "note_id": item.note_id,
                "sleeve_id": item.sleeve_id,
                "observation_key": item.observation_key,
                "note": item.note,
                "recorded_at": item.recorded_at.isoformat(),
                "recorded_by": item.recorded_by,
            }
            for item in review.notes
        ],
        "decisions": [decision(item) for item in review.decisions],
        "superseded_decisions": [decision(item) for item in review.superseded_decisions],
    }


__all__ = [
    "DEFAULT_RECORDED_BY",
    "REQUIRED_AREAS",
    "AccountingCheck",
    "CheckIntent",
    "CheckOutcome",
    "CohortReview",
    "CohortReviewError",
    "CohortReviewRepository",
    "CohortReviewService",
    "DecisionIntent",
    "DecisionOutcome",
    "NoteIntent",
    "NoteOutcome",
    "ObservationRef",
    "PendingObservation",
    "ReviewConflictError",
    "ReviewContext",
    "ReviewFinding",
    "ReviewNotDueError",
    "ReviewNote",
    "ReviewValidationError",
    "SleeveDecision",
    "WriteStatus",
    "accounting_evidence",
    "assemble_review",
    "build_context",
    "check_entry_id",
    "decision_entry_id",
    "note_entry_id",
    "operator_decisions",
    "parse_action",
    "parse_area",
    "parse_finding",
    "review_payload",
]
