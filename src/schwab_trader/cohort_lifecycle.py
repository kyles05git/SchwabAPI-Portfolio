"""Which persisted cohorts are still being collected, and which are only evidence.

A cohort that failed its shakedown is not deleted, repaired, or replayed — it is
*superseded*. The records stay exactly as they were written, and a new cohort id starts
the collection over. See ``docs/incidents/2026-07-27-paper-first-cohort-daily-bars-stale.md``
and ``docs/operations/replacement-cohort-plan.md``.

That leaves one question the stored data cannot answer on its own: when several cohorts
exist, which one is the operator actually collecting? This module answers it from a
declarative, reviewable registry rather than from the database, for two reasons:

* The stored cohort manifest is immutable by construction
  (:meth:`schwab_trader.storage.sleeves.SharedSleeveStore.upsert_cohort_manifest`
  refuses a conflicting rewrite), and the superseded cohort's rows are incident
  evidence. Marking a cohort superseded must not write to it at all.
* A supersession is an operator judgement about an experiment, recorded in an incident
  document and reviewed in a pull request. A code constant is exactly as durable as the
  decision it encodes, and it is visible in the diff.

Everything here is a pure lookup: no I/O, no clock, no storage, no network. Unknown
cohorts are :attr:`CohortLifecycle.ACTIVE`, because the registry only ever records the
exception — a cohort is not retired by being forgotten about.

Two different questions, answered from two different places:

* *Which cohorts are still collecting?* The reviewed registry below.
* *Which of several active cohorts is the newest?* The persisted immutable start session,
  supplied by the caller and ranked by :func:`resolve_default_cohort`. Nothing here ever
  ranks on a cohort id, a display name, insertion order, or the wall clock — a cohort id
  that merely contains a date is a string, not a fact about the experiment.

Being active is not a lifecycle decision. When two cohorts are collecting at once, the
newer one becomes the reporting default *and the older one is left exactly as it is*,
still running and still listed, until a human retires it in a reviewed change. This
module never promotes, retires, or rewrites anything on the operator's behalf.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

__all__ = [
    "SUPERSEDED_LABEL",
    "CohortLifecycle",
    "CohortRecency",
    "CohortStatus",
    "DefaultCohortResolution",
    "active_cohorts",
    "historical_cohorts",
    "is_historical",
    "resolve_default_cohort",
    "run_refusal",
    "status_for",
]

#: Operator-facing label for a cohort withdrawn after a failed shakedown.
SUPERSEDED_LABEL = "Superseded — partial incident, do not run"


class CohortLifecycle(StrEnum):
    """Whether a cohort is still collecting, or is retained only as a record."""

    ACTIVE = "active"
    """Currently collecting. Eligible to be scheduled, run, and selected by default."""

    SUPERSEDED = "superseded"
    """Withdrawn and replaced. Readable forever; never scheduled, run, or defaulted to."""


@dataclass(frozen=True, slots=True)
class CohortStatus:
    """One cohort's lifecycle, with the operator-facing explanation for it."""

    cohort_id: str
    lifecycle: CohortLifecycle = CohortLifecycle.ACTIVE
    label: str = ""
    """Short badge text. Empty for an active cohort, which needs no qualifier."""

    reason: str = ""
    """One sentence on why the cohort is in this state."""

    superseded_by: str | None = None
    """The cohort that replaced this one, when there is one."""

    reference: str = ""
    """Repository-relative document holding the full record."""

    @property
    def historical(self) -> bool:
        """True when the cohort is retained as evidence rather than collected."""
        return self.lifecycle is not CohortLifecycle.ACTIVE

    @property
    def runnable(self) -> bool:
        """True when the scheduler is allowed to execute a session for this cohort."""
        return not self.historical


#: The exception list. An id absent from here is active by default.
#:
#: `paper-first-2026-07-27` recorded a terminal `partial (2/7 members)` on its first and
#: only session and was replaced rather than repaired. Its rows are untouched.
_REGISTRY: Mapping[str, CohortStatus] = {
    "paper-first-2026-07-27": CohortStatus(
        cohort_id="paper-first-2026-07-27",
        lifecycle=CohortLifecycle.SUPERSEDED,
        label=SUPERSEDED_LABEL,
        reason=(
            "Session 1 recorded a terminal partial (2 of 7 members) on stale daily bars. "
            "The collection restarted as a new cohort; these records are kept unchanged "
            "as incident evidence."
        ),
        superseded_by="paper-first-2026-07-28",
        reference="docs/incidents/2026-07-27-paper-first-cohort-daily-bars-stale.md",
    ),
}


def status_for(cohort_id: str) -> CohortStatus:
    """The lifecycle record for ``cohort_id``; active when it is not registered."""
    known = _REGISTRY.get(cohort_id.strip())
    if known is not None:
        return known
    return CohortStatus(cohort_id=cohort_id.strip())


def is_historical(cohort_id: str) -> bool:
    """True when the cohort is kept as a record rather than collected."""
    return status_for(cohort_id).historical


def active_cohorts(cohort_ids: Iterable[str]) -> list[str]:
    """``cohort_ids`` that are still collecting, in the order given."""
    return [cohort_id for cohort_id in cohort_ids if not is_historical(cohort_id)]


def historical_cohorts(cohort_ids: Iterable[str]) -> list[CohortStatus]:
    """Lifecycle records for the historical members of ``cohort_ids``, in order given."""
    return [status_for(cohort_id) for cohort_id in cohort_ids if is_historical(cohort_id)]


@dataclass(frozen=True, slots=True)
class CohortRecency:
    """A candidate cohort paired with the persisted metadata that orders it.

    ``start_session`` is the cohort's *immutable exchange start session*, read from its
    persisted manifest (``Cohort.start_session``). It is the only field this module will
    rank on, and ``None`` means the manifest does not record one — not "unknown, assume
    old". Deliberately absent: the cohort id, the display name, the row/insert order, and
    any created-at timestamp. An id like ``paper-first-2026-07-28`` only *looks* like a
    date, a name is operator prose, and a creation timestamp records when somebody typed
    the bootstrap command rather than which experiment is newer.
    """

    cohort_id: str
    start_session: date | None = None


@dataclass(frozen=True, slots=True)
class DefaultCohortResolution:
    """Which cohort a read-only view defaults to, and what the operator must be told.

    A resolution is never a silent success. Either ``selected`` names one unambiguously
    newest active cohort — in which case ``older_active`` lists the active cohorts it
    won against, every one of them still owed an explicit lifecycle decision — or
    ``selected`` is ``None`` and ``problem`` says precisely what is missing.
    """

    selected: str | None = None
    active: tuple[str, ...] = ()
    """Every active candidate considered, newest first when ranking succeeded."""

    older_active: tuple[str, ...] = ()
    """Active cohorts that are not the default. Non-empty means the operator has more
    than one live experiment and has not yet retired any of them."""

    ambiguous: bool = False
    """True when active cohorts exist but none could be chosen without guessing."""

    problem: str = ""
    """Operator-facing explanation, set exactly when ``selected`` is ``None``."""

    @property
    def multiple_active(self) -> bool:
        """True when more than one cohort is still collecting."""
        return len(self.active) > 1


def _rankable(candidates: Iterable[CohortRecency]) -> list[CohortRecency]:
    """Active candidates only, de-duplicated by cohort id, in the order given."""
    seen: set[str] = set()
    kept: list[CohortRecency] = []
    for candidate in candidates:
        cohort_id = candidate.cohort_id.strip()
        if not cohort_id or cohort_id in seen or is_historical(cohort_id):
            continue
        seen.add(cohort_id)
        kept.append(candidate)
    return kept


def resolve_default_cohort(candidates: Iterable[CohortRecency]) -> DefaultCohortResolution:
    """The cohort a read-only view shows when the caller did not ask for one.

    The rule, in order:

    1. **Only active cohorts are eligible.** A superseded experiment is never a fallback;
       presenting closed evidence as the running collection is the confusion this module
       exists to prevent. When every known cohort is historical the answer is ``None``
       and the caller reports the absence, keeping them explicitly selectable.
    2. **One active cohort is the answer.** Nothing is being ranked, so no ordering
       metadata is required. This is today's state — July 28 alone — and it keeps
       working on the local SQLite layout, which persists no cohort manifest at all.
    3. **Several active cohorts are ranked by persisted start session, newest wins.**
       Never by id, name, insertion order, or the wall clock. A cohort id that sorts
       later is not a cohort that started later, and that coincidence is exactly what
       made the previous rule look correct while it was picking the older experiment.
    4. **A tie, or any candidate with no persisted start session, fails visibly.** Two
       experiments that began on the same session have no newest one, and a missing
       ``start_session`` means the database cannot answer the question. Both return
       ``ambiguous`` with a ``problem`` naming the cohorts and the missing contract,
       because a guess that lands on the wrong experiment is worse than a screen that
       says it cannot tell.

    Pure: no I/O, no clock, no storage. The caller supplies the persisted metadata.
    """
    active = _rankable(candidates)
    if not active:
        return DefaultCohortResolution()
    if len(active) == 1:
        # Unambiguous by construction: there is nothing to rank against.
        return DefaultCohortResolution(selected=active[0].cohort_id, active=(active[0].cohort_id,))

    names = ", ".join(sorted(item.cohort_id for item in active))
    missing = sorted(item.cohort_id for item in active if item.start_session is None)
    if missing:
        return DefaultCohortResolution(
            active=tuple(item.cohort_id for item in active),
            ambiguous=True,
            problem=(
                f"{len(active)} active cohorts exist ({names}) and the newest cannot be "
                f"determined: {', '.join(missing)} {'has' if len(missing) == 1 else 'have'} "
                "no persisted start_session in the cohort manifest. Record the exchange "
                "start session for every active cohort, or select one explicitly with "
                "?cohort=. The default is never guessed."
            ),
        )

    # `missing` was empty, so every start session is present and the ranking is total.
    # Sorting on the date alone keeps it stable for equal starts, which the tie check
    # below rejects anyway rather than letting the caller's order break the deadlock.
    dated: list[tuple[date, str]] = [
        (item.start_session, item.cohort_id)
        for item in active
        if item.start_session is not None
    ]
    ordered = sorted(dated, key=lambda pair: pair[0], reverse=True)
    newest = ordered[0][0]
    tied = sorted(cohort_id for start, cohort_id in ordered if start == newest)
    if len(tied) > 1:
        return DefaultCohortResolution(
            active=tuple(cohort_id for _, cohort_id in ordered),
            ambiguous=True,
            problem=(
                f"{len(active)} active cohorts exist ({names}) and {len(tied)} share the "
                f"newest persisted start session {newest.isoformat()}: {', '.join(tied)}. "
                "There is no newest cohort to default to. Select one explicitly with "
                "?cohort=, or retire one through the reviewed lifecycle decision in "
                "docs/operations/cohort-rollover.md."
            ),
        )

    return DefaultCohortResolution(
        selected=ordered[0][1],
        active=tuple(cohort_id for _, cohort_id in ordered),
        older_active=tuple(cohort_id for _, cohort_id in ordered[1:]),
    )


def run_refusal(cohort_id: str) -> str | None:
    """Why the scheduler must not run ``cohort_id``, or ``None`` when it may.

    Refusing is the whole point: re-running a superseded cohort would append new
    sessions to a record that is closed, and the record is the evidence for an incident.
    """
    status = status_for(cohort_id)
    if status.runnable:
        return None
    replacement = (
        f" Run {status.superseded_by} instead." if status.superseded_by else ""
    )
    reference = f" See {status.reference}." if status.reference else ""
    return (
        f"Cohort {status.cohort_id} is {status.lifecycle.value} and must not be run or "
        f"scheduled. {status.reason}{replacement}{reference}"
    )
