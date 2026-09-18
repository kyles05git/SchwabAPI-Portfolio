"""Which sleeves a cohort-scoped report is allowed to show.

:mod:`schwab_trader.benchmark_scope` answers "which ``bench-spy`` did the operator
mean?". This module answers the other half of the same question: "and which sleeves is
that benchmark measuring?". They were split apart once — ``--cohort`` picked the right
control sleeve and then ranked it against every sleeve ever persisted — so the two live
side by side to make the pairing obvious.

A cohort is the unit of comparability. Its members share a starting session, starting
capital, configuration, and observation history; sleeves outside it do not. Ranking
across that boundary produces an excess-over-benchmark number that means nothing, so a
report that names a cohort shows that cohort and nothing else.

Scoping is *not* filtering by lifecycle. A superseded cohort asked for by name is shown
in full: these reports are read-only, and reading the July 27 incident evidence is
exactly what an operator does with it. :mod:`schwab_trader.cohort_lifecycle` decides what
may be *run*; this module only decides what a given report covers.

Everything here is a pure lookup over configs the caller already read: no storage, no
network, no clock.
"""

from __future__ import annotations

from schwab_trader import sleeves

__all__ = ["CohortScopeError", "known_cohorts", "members"]


class CohortScopeError(LookupError):
    """The named cohort holds no sleeves in this registry.

    Subclasses :class:`LookupError` so the CLI commands that already funnel a benchmark
    lookup failure into a clean ``Error:`` line handle an unknown cohort the same way,
    without a traceback.
    """


def known_cohorts(configs: list[sleeves.SleeveConfig]) -> list[str]:
    """The cohort ids present in ``configs``, sorted.

    Sleeves with no cohort are omitted: they are legacy or standalone records, not a
    cohort an operator can select.
    """
    return sorted({config.cohort_id for config in configs if config.cohort_id})


def members(
    configs: list[sleeves.SleeveConfig], cohort_id: str
) -> list[sleeves.SleeveConfig]:
    """The sleeves in ``cohort_id``, in the order given, or raise.

    An exact match on ``cohort_id`` is the whole rule — no prefix, no fuzzy match, no
    fallback to "everything" when the cohort is unknown. Silently widening the scope is
    the defect this exists to prevent, so an id that names nothing is an error naming the
    cohorts that do exist.
    """
    wanted = cohort_id.strip()
    scoped = [config for config in configs if config.cohort_id == wanted]
    if not scoped:
        available = known_cohorts(configs)
        known = ", ".join(available) if available else "none"
        raise CohortScopeError(
            f"No sleeves belong to cohort {wanted!r}. Known cohorts: {known}."
        )
    return scoped
