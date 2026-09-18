"""A cohort's start comes from the database, not from what it happens to have run.

Issue #72: a freshly created cohort — seven members, zero runs, zero observations, which
is the *correct* state before its first session closes — reported ``start_session=None``
and a "Not scheduled" headline on the dashboard, while ``cohort health`` correctly showed
the session as pre-close. The dashboard inferred the start from recorded sessions, and a
cohort that has not run yet has none to infer from.

That inference is wrong in exactly the window an operator uses to confirm a new cohort is
set up properly, which is when it matters most.

Offline and deterministic: SQLite-backed shared storage under ``tmp_path``, injected
clock, no ``.env``, network, or broker access.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from schwab_trader import dashboard, sleeves, strategy_registry
from schwab_trader.storage.database import Database
from schwab_trader.storage.sleeves import SqlAlchemySleeveStore

COHORT = "paper-replacement-2026-07-28"
START = date(2026, 7, 28)
PRE_CLOSE_ET = datetime(2026, 7, 28, 14, 49)


@pytest.fixture
def shared(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'shared.sqlite3'}", create_schema=True)
    try:
        yield database
    finally:
        database.dispose()


def _create_cohort(store: SqlAlchemySleeveStore, *, names: tuple[str, ...]):
    return [
        store.create(
            name,
            strategy="buy-hold",
            universe=["SPY"],
            starting_cash=Decimal("10000.00"),
            max_positions=1,
            max_position_fraction=Decimal("1"),
            definition=strategy_registry.make_definition(
                "buy-hold", universe_definition=["SPY"]
            ),
            cohort_id=COHORT,
        )
        for name in names
    ]


def test_persisted_start_wins_over_recorded_sessions(shared):
    """The precedence rule, in the one place that now owns it."""
    store = SqlAlchemySleeveStore(shared)
    configs = _create_cohort(store, names=("bench-spy", "control-cash"))
    store.upsert_cohort_manifest(
        {"cohort": {"cohort_id": COHORT, "start_session": START.isoformat()}}
    )

    assert store.cohort_start_session(COHORT) == START
    assert sleeves.resolve_cohort_start(store, COHORT, configs) == START


def test_falls_back_to_member_creation_when_nothing_is_persisted(shared):
    """Still predates any run, so a first session that never ran is not hidden."""
    store = SqlAlchemySleeveStore(shared)
    configs = _create_cohort(store, names=("bench-spy", "control-cash"))

    resolved = sleeves.resolve_cohort_start(store, COHORT, configs)

    assert resolved == min(config.created_at.date() for config in configs)


def test_a_created_but_unrun_cohort_is_not_reported_as_never_scheduled():
    """The reported symptom, at the assembly boundary.

    Zero runs and zero observations is the correct state for a cohort created before
    its first session closes. Supplying the persisted start must make the phase view
    say so instead of "no session scheduled".
    """
    selection = dashboard.CohortSelectionView(requested=COHORT, available=[COHORT])
    configs = _fake_configs()

    view = dashboard.assemble_cohort_view(
        selected=COHORT,
        selection=selection,
        configs=configs,
        runs=[],
        observations=[],
        benchmark="bench-spy",
        now_et=PRE_CLOSE_ET,
        cohort_start=START,
    )

    assert view.phase is not None
    assert view.phase.start_session == START
    assert view.phase.completed_due_sessions == 0
    assert view.phase.review_target == 30


def test_without_a_persisted_start_the_old_inference_still_applies():
    """Backwards compatible: callers that supply nothing behave exactly as before."""
    selection = dashboard.CohortSelectionView(requested=COHORT, available=[COHORT])

    view = dashboard.assemble_cohort_view(
        selected=COHORT,
        selection=selection,
        configs=_fake_configs(),
        runs=[],
        observations=[],
        benchmark="bench-spy",
        now_et=PRE_CLOSE_ET,
    )

    assert view.phase is not None
    assert view.phase.start_session is None


def test_the_identity_view_still_reports_the_recorded_range_not_the_start():
    """`first_session`/`latest_session` are an observed-range pair and must stay so.

    Conflating them with the cohort's start would make a cohort that has not run look
    as though it had recorded a session.
    """
    view = dashboard.assemble_cohort_view(
        selected=COHORT,
        selection=dashboard.CohortSelectionView(requested=COHORT, available=[COHORT]),
        configs=_fake_configs(),
        runs=[],
        observations=[],
        benchmark="bench-spy",
        now_et=PRE_CLOSE_ET,
        cohort_start=START,
    )

    assert view.identity is not None
    assert view.identity.first_session is None, "nothing has been recorded yet"
    assert view.identity.latest_session is None
    assert view.phase is not None and view.phase.start_session == START


def _fake_configs() -> list[sleeves.SleeveConfig]:
    """Two in-memory members; no store is needed at the assembly boundary."""
    created = datetime(2026, 7, 28, 9, 0, tzinfo=UTC)
    return [
        sleeves.SleeveConfig(
            sleeve_id=f"sleeve-{name}",
            name=name,
            original_name=name,
            strategy="buy-hold",
            universe=["SPY"],
            starting_cash=Decimal("10000.00"),
            max_positions=1,
            max_position_fraction=Decimal("1"),
            created_at=created,
            settlement_t1=True,
            leverage=Decimal("1"),
            factor="",
            definition=strategy_registry.make_definition(
                "buy-hold", universe_definition=["SPY"]
            ),
            cohort_id=COHORT,
            configuration_hash="hash",
            decision_frequency="daily",
            decision_time="16:00",
        )
        for name in ("bench-spy", "control-cash")
    ]
