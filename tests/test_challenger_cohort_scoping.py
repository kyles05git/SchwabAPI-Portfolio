"""Two active cohorts stay separate through every reporting and execution path.

``paper-first-2026-07-28`` and the challenger cohort collect at the same time. They have
different start sessions, different members, different execution methodologies, and
different capital histories, so a number computed across both is not a number. These
tests assert the boundary holds in the places it could leak: cohort scoping, the
dashboard's default selection, the digest, the leaderboard, the CLI's cohort resolution,
and the durable run store.

Offline and synthetic throughout: disposable ``tmp_path`` SQLite stores, no ``.env``, no
socket, no Schwab, Neon, SMTP, or broker access.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from schwab_trader import challenger_cohort as cc
from schwab_trader import (
    cohort_lifecycle,
    cohort_scope,
    dashboard,
    digest,
    scheduling,
    strategy_registry,
)
from schwab_trader.config import Settings
from schwab_trader.storage.database import Database
from schwab_trader.storage.sleeves import SqlAlchemySleeveStore

JULY = "paper-first-2026-07-28"
JULY_START = date(2026, 7, 28)
CHALLENGER_START = date(2026, 8, 17)

PLAN_NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
PLAN_NOW_ET = datetime(2026, 8, 1, 8, 0)

#: The two sleeve names both cohorts use. Every "does it leak?" question is really a
#: question about these two.
SHARED_NAMES = ("control-cash", "bench-spy")


@pytest.fixture
def plan() -> cc.ChallengerPlan:
    return cc.build_plan(start_session=CHALLENGER_START, now=PLAN_NOW, now_et=PLAN_NOW_ET)


@pytest.fixture
def both_cohorts(tmp_path: Path, plan: cc.ChallengerPlan) -> SqlAlchemySleeveStore:
    """A shared registry holding July 28 and the challenger cohort side by side."""
    database = Database(f"sqlite:///{tmp_path / 'shared.sqlite3'}", create_schema=True)
    store = SqlAlchemySleeveStore(database)
    for name, strategy in (("control-cash", "hold"), ("bench-spy", "buy-hold")):
        store.create(
            name,
            strategy=strategy,
            universe=["SPY"],
            starting_cash=Decimal("10000.00"),
            max_positions=1,
            max_position_fraction=Decimal("1"),
            definition=strategy_registry.make_definition(
                strategy, universe_definition=["SPY"]
            ),
            cohort_id=JULY,
        )
    store.upsert_cohort_manifest(
        {
            "cohort": {
                "cohort_id": JULY,
                "name": "Paper Sleeves First",
                "created_at": "2026-07-28T12:00:00+00:00",
                "start_session": JULY_START.isoformat(),
                "status": "active",
                "starting_cash_per_sleeve": "10000.00",
                "settlement_model": "T+1",
                "leverage": "1",
                "benchmark_sleeve": "bench-spy",
                "decision_schedule": "XNYS session close",
                "cost_model_id": "paper-engine-v1-no-modeled-cost",
            }
        }
    )
    cc.create(plan, store=store)
    return store


# --- membership never leaks ---------------------------------------------------


def test_each_cohort_reports_only_its_own_members(
    both_cohorts: SqlAlchemySleeveStore, plan: cc.ChallengerPlan
) -> None:
    configs = both_cohorts.list()

    july_members = cohort_scope.members(configs, JULY)
    challenger_members = cohort_scope.members(configs, plan.cohort_id)

    assert len(july_members) == 2
    assert len(challenger_members) == 5
    # Seven records, not five: the two shared names are four distinct sleeves.
    assert len(configs) == 7
    assert {cfg.sleeve_id for cfg in july_members}.isdisjoint(
        cfg.sleeve_id for cfg in challenger_members
    )
    for name in SHARED_NAMES:
        july_one = next(cfg for cfg in july_members if cfg.name == name)
        challenger_one = next(cfg for cfg in challenger_members if cfg.name == name)
        assert july_one.sleeve_id != challenger_one.sleeve_id
        assert july_one.cohort_id == JULY
        assert challenger_one.cohort_id == plan.cohort_id


def test_an_unknown_cohort_is_an_error_rather_than_a_silent_widening(
    both_cohorts: SqlAlchemySleeveStore,
) -> None:
    with pytest.raises(cohort_scope.CohortScopeError, match="No sleeves belong to cohort"):
        cohort_scope.members(both_cohorts.list(), "challenger-v1-not-created")


def test_both_cohorts_are_active_and_neither_retires_the_other(
    both_cohorts: SqlAlchemySleeveStore, plan: cc.ChallengerPlan
) -> None:
    known = cohort_scope.known_cohorts(both_cohorts.list())

    assert set(cohort_lifecycle.active_cohorts(known)) == {JULY, plan.cohort_id}
    assert cohort_lifecycle.historical_cohorts(known) == []
    # Both remain runnable; creating the challenger did not supersede anything.
    assert cohort_lifecycle.run_refusal(JULY) is None
    assert cohort_lifecycle.run_refusal(plan.cohort_id) is None


# --- the dashboard default ----------------------------------------------------


def test_the_dashboard_defaults_to_the_newer_cohort_and_lists_the_older_one(
    both_cohorts: SqlAlchemySleeveStore, plan: cc.ChallengerPlan
) -> None:
    available = cohort_scope.known_cohorts(both_cohorts.list())
    starts = {
        cohort_id: both_cohorts.cohort_start_session(cohort_id) for cohort_id in available
    }

    selection = dashboard.cohort_selection(None, available, start_sessions=starts)

    assert starts == {JULY: JULY_START, plan.cohort_id: CHALLENGER_START}
    assert selection.selected == plan.cohort_id
    # The older experiment is still offered, not hidden: both are live collections.
    assert set(selection.available) == {JULY, plan.cohort_id}


def test_an_explicit_request_always_wins_over_the_default(
    both_cohorts: SqlAlchemySleeveStore, plan: cc.ChallengerPlan
) -> None:
    available = cohort_scope.known_cohorts(both_cohorts.list())
    starts = {
        cohort_id: both_cohorts.cohort_start_session(cohort_id) for cohort_id in available
    }

    selection = dashboard.cohort_selection(JULY, available, start_sessions=starts)

    assert selection.selected == JULY


def test_without_ordering_metadata_the_dashboard_refuses_to_guess(
    both_cohorts: SqlAlchemySleeveStore, plan: cc.ChallengerPlan
) -> None:
    """The case a guess would silently put the wrong experiment on screen."""
    available = cohort_scope.known_cohorts(both_cohorts.list())

    selection = dashboard.cohort_selection(None, available, start_sessions={})

    assert selection.selected is None
    assert selection.ambiguous


# --- the digest and the leaderboard ------------------------------------------


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        sleeves_dir=tmp_path / "sleeves",
        database_url=f"sqlite:///{tmp_path / 'shared.sqlite3'}",  # type: ignore[arg-type]
        token_path=tmp_path / "tokens.json",
        state_db_path=tmp_path / "state.sqlite3",
        log_path=tmp_path / "logs" / "app.log",
    )


def test_the_digest_scopes_its_rows_to_the_named_cohort(
    tmp_path: Path, both_cohorts: SqlAlchemySleeveStore, plan: cc.ChallengerPlan
) -> None:
    settings = _settings(tmp_path)

    july_rows = digest.collect_digest_rows(settings, "bench-spy", cohort_id=JULY)
    challenger_rows = digest.collect_digest_rows(
        settings, "bench-spy", cohort_id=plan.cohort_id
    )

    assert len(july_rows) == 2
    assert len(challenger_rows) == 5
    # Neither report's population includes the other's members.
    assert {row.strategy for row in july_rows} == {"hold", "buy-hold"}
    assert "dual-momentum-v1" in {row.strategy for row in challenger_rows}


def test_a_shared_sleeve_name_is_qualified_by_its_cohort(
    tmp_path: Path, both_cohorts: SqlAlchemySleeveStore, plan: cc.ChallengerPlan
) -> None:
    """Two sleeves called ``control-cash`` must be distinguishable in a report."""
    settings = _settings(tmp_path)

    rows = digest.collect_digest_rows(settings, "bench-spy", cohort_id=plan.cohort_id)

    labels = {row.name for row in rows}
    assert f"control-cash [{plan.cohort_id}]" in labels
    assert f"bench-spy [{plan.cohort_id}]" in labels
    # The unshared challenger names need no qualifier.
    assert "dual-momentum-v1" in labels


def test_the_digest_names_its_cohort_in_the_subject_and_body(
    tmp_path: Path, both_cohorts: SqlAlchemySleeveStore, plan: cc.ChallengerPlan
) -> None:
    settings = _settings(tmp_path)

    message = digest.build_daily_digest(
        settings, benchmark="bench-spy", cohort_id=plan.cohort_id
    )

    assert plan.cohort_id in message.subject
    assert plan.cohort_id in message.body
    assert "this cohort's members only" in message.body


def test_the_digest_body_survives_rich_rendering_intact(
    tmp_path: Path, both_cohorts: SqlAlchemySleeveStore, plan: cc.ChallengerPlan
) -> None:
    """Regression: Rich reads ``[challenger-v1-...]`` as a style tag and deletes it.

    The qualifier only appears once two cohorts share a sleeve name, which is exactly
    what creating the challenger cohort causes — so printing the body as markup would
    have shown two identically labelled ``control-cash`` rows the moment this cohort
    existed. ``schwab-trader sleeve digest`` prints with ``markup=False`` for that
    reason; this asserts the difference is real rather than theoretical.
    """
    import io

    from rich.console import Console

    settings = _settings(tmp_path)
    body = digest.build_daily_digest(
        settings, benchmark="bench-spy", cohort_id=plan.cohort_id
    ).body
    assert f"control-cash [{plan.cohort_id}]" in body

    as_markup = io.StringIO()
    Console(file=as_markup, width=200, no_color=True).print(body)
    as_text = io.StringIO()
    Console(file=as_text, width=200, no_color=True).print(body, markup=False)

    qualified = f"control-cash [{plan.cohort_id}]"
    # Rich deletes the bracketed qualifier from the row, leaving a bare 'control-cash'
    # that no longer says which cohort it is. (The unbracketed 'Cohort:' scope line
    # survives either way, which is why this asserts on the row, not the whole body.)
    assert qualified not in as_markup.getvalue()
    assert qualified in as_text.getvalue()


# --- execution is always explicitly scoped ------------------------------------


def test_each_cohort_gets_its_own_run_identity_for_the_same_session(
    plan: cc.ChallengerPlan,
) -> None:
    """Two cohorts running the same session must never share a run key or lease."""
    session = scheduling.session_for_date(date(2026, 8, 31))

    july_key = scheduling.run_key(JULY, session)
    challenger_key = scheduling.run_key(plan.cohort_id, session)

    assert july_key != challenger_key


def test_the_run_store_lists_only_the_named_cohorts_runs(
    tmp_path: Path, plan: cc.ChallengerPlan
) -> None:
    from schwab_trader.sleeve_runs import SleeveRunStore

    store = SleeveRunStore(tmp_path / "runs.sqlite3")
    session = scheduling.session_for_date(date(2026, 8, 31))
    store.ensure_run(cohort_id=JULY, session=session, expected_members=("a",))
    store.ensure_run(cohort_id=plan.cohort_id, session=session, expected_members=("b",))

    july_runs = store.list(cohort_id=JULY)
    challenger_runs = store.list(cohort_id=plan.cohort_id)

    assert len(july_runs) == 1
    assert len(challenger_runs) == 1
    assert july_runs[0].cohort_id == JULY
    assert challenger_runs[0].cohort_id == plan.cohort_id
    assert july_runs[0].run_id != challenger_runs[0].run_id


def test_the_cli_refuses_to_guess_between_two_active_cohorts(
    tmp_path: Path, both_cohorts: SqlAlchemySleeveStore, plan: cc.ChallengerPlan
) -> None:
    """Reporting may default; execution must be explicit."""
    from schwab_trader import cli

    settings = _settings(tmp_path)

    chosen, members, problem = cli._find_cohort(settings, both_cohorts, "")

    assert chosen == ""
    assert members == []
    assert "never guessed" in problem
    assert JULY in problem and plan.cohort_id in problem


def test_naming_the_cohort_resolves_it_unambiguously(
    tmp_path: Path, both_cohorts: SqlAlchemySleeveStore, plan: cc.ChallengerPlan
) -> None:
    from schwab_trader import cli

    settings = _settings(tmp_path)

    chosen, members, problem = cli._find_cohort(settings, both_cohorts, plan.cohort_id)

    assert problem == ""
    assert chosen == plan.cohort_id
    assert len(members) == 5


def test_a_configured_default_cohort_resolves_without_a_flag(
    tmp_path: Path, both_cohorts: SqlAlchemySleeveStore, plan: cc.ChallengerPlan
) -> None:
    from schwab_trader import cli

    settings = _settings(tmp_path).model_copy(update={"cohort_id": plan.cohort_id})

    chosen, members, problem = cli._find_cohort(settings, both_cohorts, "")

    assert problem == ""
    assert chosen == plan.cohort_id
    assert len(members) == 5
