"""Which sleeve a benchmark name refers to, once several cohorts share that name.

Every cohort needs its own control sleeve, so a name like ``bench-spy`` is *expected* to
be reused across cohorts. That makes a bare name ambiguous against the whole registry —
but it is rarely ambiguous in intent: the collection still running is the one being
measured. Resolving that used to be decided independently by the dashboard, the
leaderboard, and the emailed digest, and they disagreed. This module is the one place
that decides, so they cannot drift apart again.

Precedence:

1. An exact stable id (``sleeve_id`` or ``identity``) is unique by construction, so it
   wins outright — including inside a superseded cohort, because naming a record exactly
   is how an operator inspects it.
2. An explicit ``cohort_id`` scopes the lookup to that cohort, historical or not.
3. Otherwise, a name matching several sleeves narrows to the cohorts that are still
   active. One survivor resolves. Several is genuinely ambiguous and says so, naming the
   cohorts and the flag that disambiguates.

Everything here is a pure lookup over configs the caller already read: no storage, no
network, no clock.
"""

from __future__ import annotations

from schwab_trader import cohort_lifecycle, sleeves

__all__ = ["BenchmarkScopeError", "candidates", "resolve"]


class BenchmarkScopeError(LookupError):
    """The benchmark name could not be narrowed to exactly one sleeve.

    Subclasses :class:`LookupError` so callers that already funnel
    :class:`~schwab_trader.storage.sleeves.AmbiguousSleeveName` into a clean CLI failure
    keep working unchanged.
    """


def candidates(configs: list[sleeves.SleeveConfig], benchmark: str) -> list[sleeves.SleeveConfig]:
    """The sleeves ``benchmark`` could refer to, by display name or stable identity.

    One place decides what "matches the benchmark" means, so a caller resolving within
    one comparability group and a caller only asking whether the name exists anywhere
    cannot drift apart. Matching several sleeves is a legitimate answer here - deciding
    whether that is ambiguous is the caller's job, and depends on the scope it passed.
    """
    if not benchmark:
        return []
    return [
        config
        for config in configs
        if benchmark in {config.name, config.identity} or benchmark == config.sleeve_id != ""
    ]


def _describe(configs: list[sleeves.SleeveConfig]) -> str:
    """The cohorts a set of matches belongs to, for an actionable error message."""
    return ", ".join(sorted({config.cohort_id or "legacy" for config in configs}))


def resolve(
    configs: list[sleeves.SleeveConfig],
    benchmark: str,
    *,
    cohort_id: str | None = None,
) -> sleeves.SleeveConfig | None:
    """The single sleeve ``benchmark`` names, or ``None`` when nothing matches.

    ``None`` means "no such sleeve", which the caller reports in its own words.
    :class:`BenchmarkScopeError` means "several, and I will not guess" — the operator has
    to name a cohort or a stable id.
    """
    if not benchmark:
        return None
    matches = candidates(configs, benchmark)

    if cohort_id is not None:
        scoped = [config for config in matches if config.cohort_id == cohort_id]
        if not scoped:
            raise BenchmarkScopeError(
                f"No sleeve named {benchmark!r} belongs to cohort {cohort_id!r}."
                + (f" It exists in: {_describe(matches)}." if matches else "")
            )
        if len(scoped) > 1:
            # Two sleeves sharing a name inside one cohort; the registry should prevent
            # this, so report it rather than picking one.
            raise BenchmarkScopeError(
                f"Benchmark {benchmark!r} matches {len(scoped)} sleeves inside cohort "
                f"{cohort_id!r}. Pass a sleeve id."
            )
        return scoped[0]

    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]

    # A stable id is unique, so an id match is never the ambiguous case.
    exact = [config for config in matches if benchmark in {config.identity, config.sleeve_id}]
    if len(exact) == 1:
        return exact[0]

    current = [
        config for config in matches if not cohort_lifecycle.is_historical(config.cohort_id)
    ]
    if len(current) == 1:
        return current[0]
    if not current:
        raise BenchmarkScopeError(
            f"Benchmark {benchmark!r} exists only in superseded cohort(s) "
            f"({_describe(matches)}). Pass --cohort to measure inside one of them, or a "
            "sleeve id."
        )
    raise BenchmarkScopeError(
        f"Benchmark {benchmark!r} matches sleeves in several active cohorts "
        f"({_describe(current)}). Pass --cohort or a sleeve id."
    )
