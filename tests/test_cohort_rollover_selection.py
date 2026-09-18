"""Rolling over to a newer cohort is decided by persisted metadata, never by a string.

Issue #84, following PR #81's review finding #4. `default_cohort` took the sorted cohort
ids and returned the first active one. That is correct exactly while one cohort is
active, and silently wrong the moment a second is: with `paper-first-2026-07-28` and a
challenger both collecting, the sorted-first id wins, and whether that is the newer
experiment is a coincidence of how the ids were spelled.

The rule these tests pin:

* Recency comes from the cohort's **persisted immutable start session** and nothing else.
  Not the id (a date inside a string is not a fact), not the display name, not the row or
  insertion order, not the wall clock, not the creation timestamp.
* One active cohort needs no ordering metadata at all — there is nothing to rank.
* Several active cohorts rank newest-first, and the older ones are reported as *still
  owed a lifecycle decision*. Nothing here retires them.
* A tie, or a missing start session, produces no default and says why. A guess that lands
  on the wrong experiment is worse than a screen admitting it cannot tell.
* An explicit `?cohort=` is always authoritative, superseded cohorts included.
* Execution and scheduling stay fail-closed: reporting convenience never resolves a
  cohort that a mutating command would refuse to guess.

Offline and deterministic: SQLite-backed shared storage under ``tmp_path``, real cohort
manifests, an injected Eastern instant, and no ``.env``, network, broker, or Neon access.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from schwab_trader import cli, cohort_lifecycle, dashboard, sleeves, strategy_registry
from schwab_trader.config import Settings
from schwab_trader.storage import factory as storage_factory
from schwab_trader.storage.database import Database
from schwab_trader.storage.sleeves import SqlAlchemySleeveStore

runner = CliRunner()

# The real incumbent and the real superseded cohort it replaced.
JULY_27 = "paper-first-2026-07-27"
JULY_28 = "paper-first-2026-07-28"
JULY_27_START = date(2026, 7, 27)
JULY_28_START = date(2026, 7, 28)

# The challenger of #90/#95, which will coexist with July 28 rather than replace it.
CHALLENGER = "challenger-five-sleeve-2026-08-17"
CHALLENGER_START = date(2026, 8, 17)

# An id carrying no date at all, and one that sorts *after* every id above while having
# started long before them. Either would defeat a rule that reads the string.
PILOT = "paper-pilot-alpha"
PILOT_START = date(2026, 6, 15)
ZEBRA = "zzz-late-alphabetically"
ZEBRA_START = date(2026, 5, 4)

MEMBERS = ("bench-spy", "control-cash")
NOW_ET = datetime(2026, 9, 2, 9, 28)


# --- fixtures -------------------------------------------------------------------


def _settings(tmp_path: Path, url: str) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=SecretStr(url),
        sleeves_dir=tmp_path / "sleeves",
        token_path=tmp_path / "tokens.json",
        state_db_path=tmp_path / "state.sqlite3",
        log_path=tmp_path / "logs" / "schwab_trader.log",
        kill_switch_path=tmp_path / "KILL_SWITCH",
    )


class Registry:
    """A temporary shared registry the tests add cohorts to, one call at a time."""

    def __init__(self, tmp_path: Path) -> None:
        self.url = f"sqlite:///{tmp_path / 'shared.sqlite3'}"
        self.database = Database(self.url, create_schema=True)
        self.store = SqlAlchemySleeveStore(self.database)
        self.settings = _settings(tmp_path, self.url)

    def add(self, cohort_id: str, *, start: date | None) -> None:
        """Persist a cohort's members and, when given, its immutable start session.

        ``start=None`` models a cohort whose manifest never recorded one — the missing
        contract, not a cohort that started at some unknown early date.
        """
        for name in MEMBERS:
            self.store.create(
                f"{name}-{abs(hash(cohort_id)) % 10_000}",
                strategy="buy-hold",
                universe=["SPY"],
                starting_cash=Decimal("10000.00"),
                max_positions=1,
                max_position_fraction=Decimal("1"),
                definition=strategy_registry.make_definition(
                    "buy-hold",
                    universe_definition=["SPY"],
                    benchmark_symbol_or_sleeve="bench-spy",
                ),
                cohort_id=cohort_id,
            )
        if start is not None:
            self.store.upsert_cohort_manifest(
                {"cohort": {"cohort_id": cohort_id, "start_session": start.isoformat()}}
            )

    def activate(self) -> Settings:
        """Close the writer and hand back settings pointing at the same database."""
        self.database.dispose()
        storage_factory._shared_database.cache_clear()
        return self.settings


@pytest.fixture
def registry(tmp_path: Path):
    built = Registry(tmp_path)
    yield built
    storage_factory._shared_database.cache_clear()


def _select(settings: Settings, requested: str | None = None) -> dashboard.CohortSelectionView:
    """Run the real read-only collection and return only its selection block."""
    return dashboard.collect_cohort_dashboard(
        settings,
        requested_cohort=requested,
        benchmark="bench-spy",
        now_et=NOW_ET,
    ).selection


def _fingerprint(settings: Settings) -> str:
    """Every persisted sleeve record plus every cohort manifest, serialized.

    Any write at all — a status flip, a renamed cohort, a backfilled start session —
    changes this. It is the guard on "reporting reads; it never edits".
    """
    store = storage_factory.sleeve_store(settings)
    configs = sorted(store.list(), key=lambda cfg: cfg.identity)
    cohorts = sorted({cfg.cohort_id for cfg in configs if cfg.cohort_id})
    payload = json.dumps(
        {
            "sleeves": [cfg.model_dump(mode="json") for cfg in configs],
            "manifests": {
                cohort_id: [
                    store.cohort_manifest(cohort_id),  # type: ignore[attr-defined]
                    str(store.cohort_start_session(cohort_id)),
                ]
                for cohort_id in cohorts
            },
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- the ranking rule itself (pure) ---------------------------------------------


def test_two_active_cohorts_resolve_to_the_later_start_session() -> None:
    resolution = cohort_lifecycle.resolve_default_cohort(
        [
            cohort_lifecycle.CohortRecency(JULY_28, JULY_28_START),
            cohort_lifecycle.CohortRecency(CHALLENGER, CHALLENGER_START),
        ]
    )

    assert resolution.selected == CHALLENGER
    assert resolution.older_active == (JULY_28,)
    assert resolution.multiple_active is True
    assert resolution.ambiguous is False


def test_ranking_ignores_the_order_the_candidates_arrive_in() -> None:
    """Insertion order is not evidence. Both orders give the same answer."""
    forward = cohort_lifecycle.resolve_default_cohort(
        [
            cohort_lifecycle.CohortRecency(JULY_28, JULY_28_START),
            cohort_lifecycle.CohortRecency(CHALLENGER, CHALLENGER_START),
        ]
    )
    reverse = cohort_lifecycle.resolve_default_cohort(
        [
            cohort_lifecycle.CohortRecency(CHALLENGER, CHALLENGER_START),
            cohort_lifecycle.CohortRecency(JULY_28, JULY_28_START),
        ]
    )

    assert forward == reverse
    assert forward.selected == CHALLENGER


def test_a_cohort_id_that_sorts_last_does_not_thereby_win() -> None:
    """`zzz-...` sorts after everything and started first. The start session decides."""
    resolution = cohort_lifecycle.resolve_default_cohort(
        [
            cohort_lifecycle.CohortRecency(JULY_28, JULY_28_START),
            cohort_lifecycle.CohortRecency(ZEBRA, ZEBRA_START),
        ]
    )

    assert sorted([JULY_28, ZEBRA])[-1] == ZEBRA
    assert resolution.selected == JULY_28


def test_a_cohort_id_with_no_date_in_it_ranks_perfectly_well() -> None:
    """Recency never parses the id, so an id carrying no date is not disadvantaged."""
    resolution = cohort_lifecycle.resolve_default_cohort(
        [
            cohort_lifecycle.CohortRecency(JULY_28, JULY_28_START),
            cohort_lifecycle.CohortRecency(PILOT, date(2026, 12, 1)),
        ]
    )

    assert resolution.selected == PILOT


def test_equal_start_sessions_produce_no_default_and_name_both() -> None:
    resolution = cohort_lifecycle.resolve_default_cohort(
        [
            cohort_lifecycle.CohortRecency(JULY_28, JULY_28_START),
            cohort_lifecycle.CohortRecency(PILOT, JULY_28_START),
        ]
    )

    assert resolution.selected is None
    assert resolution.ambiguous is True
    assert JULY_28 in resolution.problem and PILOT in resolution.problem
    assert "2026-07-28" in resolution.problem
    # It must not read as "nothing is running" — both cohorts are still collecting.
    assert resolution.active == (JULY_28, PILOT) or resolution.active == (PILOT, JULY_28)


def test_a_missing_start_session_produces_no_default_and_names_the_gap() -> None:
    resolution = cohort_lifecycle.resolve_default_cohort(
        [
            cohort_lifecycle.CohortRecency(JULY_28, JULY_28_START),
            cohort_lifecycle.CohortRecency(CHALLENGER, None),
        ]
    )

    assert resolution.selected is None
    assert resolution.ambiguous is True
    assert CHALLENGER in resolution.problem
    assert "start_session" in resolution.problem
    # The precise missing contract, so an operator knows what to record.
    assert "manifest" in resolution.problem


def test_one_active_cohort_needs_no_ordering_metadata() -> None:
    """The local SQLite layout persists no manifest at all; it must still resolve."""
    resolution = cohort_lifecycle.resolve_default_cohort(
        [
            cohort_lifecycle.CohortRecency(JULY_27, None),
            cohort_lifecycle.CohortRecency(JULY_28, None),
        ]
    )

    assert resolution.selected == JULY_28
    assert resolution.ambiguous is False
    assert resolution.older_active == ()


def test_superseded_cohorts_are_not_candidates_however_new_they_look() -> None:
    """A withdrawn experiment does not become the default by having a later start."""
    resolution = cohort_lifecycle.resolve_default_cohort(
        [
            cohort_lifecycle.CohortRecency(JULY_27, date(2027, 1, 1)),
            cohort_lifecycle.CohortRecency(JULY_28, JULY_28_START),
        ]
    )

    assert resolution.selected == JULY_28


def test_only_superseded_cohorts_leaves_no_default_and_no_ambiguity() -> None:
    resolution = cohort_lifecycle.resolve_default_cohort(
        [cohort_lifecycle.CohortRecency(JULY_27, JULY_27_START)]
    )

    assert resolution.selected is None
    assert resolution.ambiguous is False
    assert resolution.problem == ""


# --- through the real dashboard, against a real registry -------------------------


def test_the_july_28_default_is_preserved_while_it_is_the_only_active_cohort(
    registry: Registry,
) -> None:
    """The behaviour #78/#81 established must survive this change unaltered."""
    registry.add(JULY_27, start=JULY_27_START)
    registry.add(JULY_28, start=JULY_28_START)
    settings = registry.activate()

    selection = _select(settings)

    assert selection.selected == JULY_28
    assert selection.multiple_active is False
    assert selection.ambiguous is False
    assert [item.cohort_id for item in selection.historical] == [JULY_27]


def test_the_challenger_coexists_with_july_28_without_hijacking_the_view(
    registry: Registry,
) -> None:
    """#95's cohort starts while July 28 is still collecting.

    The newer experiment is what the operator lands on, July 28 is named as still
    running and still owed a decision, and neither cohort is altered.
    """
    registry.add(JULY_27, start=JULY_27_START)
    registry.add(JULY_28, start=JULY_28_START)
    registry.add(CHALLENGER, start=CHALLENGER_START)
    settings = registry.activate()

    selection = _select(settings)

    assert selection.selected == CHALLENGER
    assert selection.multiple_active is True
    assert selection.older_active == [JULY_28]
    assert selection.ambiguous is False
    # Newest first, and the superseded cohort is in neither list.
    assert selection.active == [CHALLENGER, JULY_28]
    assert [item.cohort_id for item in selection.historical] == [JULY_27]
    assert [item.is_default for item in selection.active_cohorts] == [True, False]
    assert [item.start_session for item in selection.active_cohorts] == [
        CHALLENGER_START,
        JULY_28_START,
    ]


def test_a_challenger_without_a_persisted_start_shows_nothing_rather_than_the_wrong_thing(
    registry: Registry,
) -> None:
    """The failure this issue exists to prevent, in its most likely real form.

    A cohort bootstrapped without a start session cannot be ranked. Defaulting to July 28
    anyway would look right and be indistinguishable from the case where the challenger
    really is older, so the view refuses and names the gap.
    """
    registry.add(JULY_28, start=JULY_28_START)
    registry.add(CHALLENGER, start=None)
    settings = registry.activate()

    view = dashboard.collect_cohort_dashboard(
        settings, requested_cohort=None, benchmark="bench-spy", now_et=NOW_ET
    )

    assert view.selection.selected is None
    assert view.selection.ambiguous is True
    assert view.available is False
    assert view.message == view.selection.ambiguity_reason
    assert CHALLENGER in view.message and "start_session" in view.message


def test_equal_starts_through_the_dashboard_show_no_cohort_and_say_why(
    registry: Registry,
) -> None:
    registry.add(JULY_28, start=JULY_28_START)
    registry.add(CHALLENGER, start=JULY_28_START)
    settings = registry.activate()

    view = dashboard.collect_cohort_dashboard(
        settings, requested_cohort=None, benchmark="bench-spy", now_et=NOW_ET
    )

    assert view.selection.selected is None
    assert view.identity is None
    assert "newest persisted start session" in view.message
    # Both remain offered explicitly, so the operator can still inspect either.
    assert sorted(view.selection.active) == sorted([JULY_28, CHALLENGER])


def test_explicit_selection_beats_recency_including_an_older_active_cohort(
    registry: Registry,
) -> None:
    registry.add(JULY_28, start=JULY_28_START)
    registry.add(CHALLENGER, start=CHALLENGER_START)
    settings = registry.activate()

    selection = _select(settings, requested=JULY_28)

    assert selection.selected == JULY_28
    assert selection.selected_is_historical is False
    # The warning does not disappear because a different cohort was asked for.
    assert selection.multiple_active is True


def test_explicit_selection_still_opens_a_superseded_cohort(registry: Registry) -> None:
    registry.add(JULY_27, start=JULY_27_START)
    registry.add(JULY_28, start=JULY_28_START)
    registry.add(CHALLENGER, start=CHALLENGER_START)
    settings = registry.activate()

    selection = _select(settings, requested=JULY_27)

    assert selection.selected == JULY_27
    assert selection.selected_is_historical is True


def test_an_explicit_cohort_resolves_even_while_the_default_is_ambiguous(
    registry: Registry,
) -> None:
    """Ambiguity blocks the *guess*, not the operator who names what they want."""
    registry.add(JULY_28, start=JULY_28_START)
    registry.add(CHALLENGER, start=None)
    settings = registry.activate()

    view = dashboard.collect_cohort_dashboard(
        settings, requested_cohort=JULY_28, benchmark="bench-spy", now_et=NOW_ET
    )

    assert view.selection.selected == JULY_28
    assert view.identity is not None
    assert view.selection.ambiguous is True


def test_selection_is_identical_across_the_order_cohorts_were_persisted_in(
    tmp_path: Path,
) -> None:
    """Restart independence: the same records inserted in either order agree.

    Two separate registries, the same three cohorts, opposite insertion order. Row order
    is the thing a `store.list()`-driven rule silently depends on.
    """
    results = []
    for index, order in enumerate(
        [
            (JULY_28, CHALLENGER, PILOT),
            (PILOT, CHALLENGER, JULY_28),
        ]
    ):
        starts = {
            JULY_28: JULY_28_START,
            CHALLENGER: CHALLENGER_START,
            PILOT: PILOT_START,
        }
        root = tmp_path / f"run-{index}"
        root.mkdir()
        built = Registry(root)
        for cohort_id in order:
            built.add(cohort_id, start=starts[cohort_id])
        settings = built.activate()
        selection = _select(settings)
        results.append((selection.selected, tuple(selection.active), tuple(selection.older_active)))
        storage_factory._shared_database.cache_clear()

    assert results[0] == results[1]
    assert results[0][0] == CHALLENGER
    assert results[0][1] == (CHALLENGER, JULY_28, PILOT)


def test_reading_the_dashboard_never_writes_to_any_cohort(registry: Registry) -> None:
    """No supersede, retire, rename, backfill, or status flip happens on read.

    Including the ambiguous path, which is the one that might tempt an implementation to
    "fix" the missing metadata it just complained about.
    """
    registry.add(JULY_27, start=JULY_27_START)
    registry.add(JULY_28, start=JULY_28_START)
    registry.add(CHALLENGER, start=None)
    settings = registry.activate()
    before = _fingerprint(settings)

    for requested in (None, JULY_27, JULY_28, CHALLENGER, "no-such-cohort"):
        dashboard.collect_cohort_dashboard(
            settings, requested_cohort=requested, benchmark="bench-spy", now_et=NOW_ET
        )

    assert _fingerprint(settings) == before


def test_the_immutability_guard_can_actually_fail(registry: Registry) -> None:
    """Proof the fingerprint above is load-bearing rather than always-equal."""
    registry.add(JULY_28, start=JULY_28_START)
    settings = registry.activate()
    before = _fingerprint(settings)

    storage_factory.sleeve_store(settings).create(
        "intruder",
        strategy="buy-hold",
        universe=["SPY"],
        starting_cash=Decimal("10000.00"),
        max_positions=1,
        max_position_fraction=Decimal("1"),
        cohort_id=JULY_28,
    )

    assert _fingerprint(settings) != before


# --- execution and scheduling stay fail-closed -----------------------------------


def test_a_mutating_command_refuses_to_pick_between_two_active_cohorts(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dashboard now has a default. The scheduler must still not have one.

    Reporting picks the newest because being wrong costs a confusing screen. Running the
    wrong cohort writes observations into the wrong experiment, so it stays a refusal.
    """
    registry.add(JULY_28, start=JULY_28_START)
    registry.add(CHALLENGER, start=CHALLENGER_START)
    settings = registry.activate()
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    store = storage_factory.sleeve_store(settings)
    chosen, members, problem = cli._find_cohort(settings, store, "")

    assert chosen == ""
    assert members == []
    assert "Several active cohorts exist" in problem
    assert "never guessed" in problem


def test_the_newest_cohort_is_not_quietly_adopted_as_the_execution_default(
    registry: Registry,
) -> None:
    """Specifically: the recency rule must not have leaked into the CLI resolver."""
    registry.add(JULY_28, start=JULY_28_START)
    registry.add(CHALLENGER, start=CHALLENGER_START)
    settings = registry.activate()

    store = storage_factory.sleeve_store(settings)
    chosen, _, problem = cli._find_cohort(settings, store, "")

    assert chosen != CHALLENGER
    assert problem != ""


def test_an_explicitly_named_cohort_still_resolves_for_execution(
    registry: Registry,
) -> None:
    registry.add(JULY_28, start=JULY_28_START)
    registry.add(CHALLENGER, start=CHALLENGER_START)
    settings = registry.activate()

    store = storage_factory.sleeve_store(settings)
    chosen, members, problem = cli._find_cohort(settings, store, CHALLENGER)

    assert chosen == CHALLENGER
    assert problem == ""
    assert {cfg.cohort_id for cfg in members} == {CHALLENGER}


def test_a_single_active_cohort_still_resolves_for_execution(registry: Registry) -> None:
    """The unambiguous case keeps working; this change adds no new refusal."""
    registry.add(JULY_27, start=JULY_27_START)
    registry.add(JULY_28, start=JULY_28_START)
    settings = registry.activate()

    store = storage_factory.sleeve_store(settings)
    chosen, _, problem = cli._find_cohort(settings, store, "")

    assert chosen == JULY_28
    assert problem == ""


def test_running_a_superseded_cohort_is_still_refused(registry: Registry) -> None:
    registry.add(JULY_27, start=JULY_27_START)
    settings = registry.activate()
    del settings

    refusal = cohort_lifecycle.run_refusal(JULY_27)

    assert refusal is not None
    assert "must not be run or scheduled" in refusal


# --- the resolver's own contract -------------------------------------------------


def test_resolve_cohort_start_keeps_its_created_at_fallback(registry: Registry) -> None:
    """Ranking and "when was this cohort first owed a run" are different questions.

    `resolve_cohort_start` still falls back to member creation, which is right for phase
    reporting. Recency must not adopt that fallback: a creation timestamp records when
    somebody typed the bootstrap command.
    """
    registry.add(CHALLENGER, start=None)
    settings = registry.activate()
    store = storage_factory.sleeve_store(settings)
    configs = [cfg for cfg in store.list() if cfg.cohort_id == CHALLENGER]

    assert store.cohort_start_session(CHALLENGER) is None
    assert sleeves.resolve_cohort_start(store, CHALLENGER, configs) is not None


def test_blank_and_duplicate_candidates_do_not_create_phantom_ambiguity() -> None:
    resolution = cohort_lifecycle.resolve_default_cohort(
        [
            cohort_lifecycle.CohortRecency(JULY_28, JULY_28_START),
            cohort_lifecycle.CohortRecency(JULY_28, JULY_28_START),
            cohort_lifecycle.CohortRecency("   ", None),
        ]
    )

    assert resolution.selected == JULY_28
    assert resolution.multiple_active is False
