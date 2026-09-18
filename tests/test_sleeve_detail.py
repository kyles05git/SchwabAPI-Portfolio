"""Offline tests for the read-only sleeve-detail record and ``GET /api/sleeve``.

Everything here runs against **temporary injected stores** built in ``tmp_path``: a
SQLite-backed shared database plus synthetic sleeves, positions, orders, cycles,
observations, and runs. Nothing reaches Schwab, Neon, ``.env``, SMTP, a socket, the paper
execution path, or any live-order path — :func:`test_no_network_is_reachable_during_a_detail_read`
pins that explicitly, and ``tests/conftest.py`` fails closed on a non-SQLite database.

The shared (SQLAlchemy) backend is used rather than the local one for a specific reason:
it is the only layout where ``SleeveConfig.identity`` is a stable hash distinct from the
display name, so it is the only layout in which the duplicate-name collision this feature
exists to survive can actually be constructed.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import threading
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr

from schwab_trader import (
    agent,
    dashboard,
    evaluation,
    scheduling,
    sleeve_detail,
    sleeve_runs,
    strategy_registry,
)
from schwab_trader.config import Settings
from schwab_trader.market_data import Quote
from schwab_trader.models import OrderRequest, OrderSide
from schwab_trader.paper import PaperValuation
from schwab_trader.storage import factory as storage_factory
from schwab_trader.storage.database import Database
from schwab_trader.storage.sleeves import SqlAlchemySleeveStore

# The real registry: one superseded cohort and the collection that replaced it. Using the
# real ids is deliberate — the lifecycle labelling under test is keyed off them.
HISTORICAL_COHORT = "paper-first-2026-07-27"
ACTIVE_COHORT = "paper-first-2026-07-28"

#: The display name deliberately present in *both* cohorts.
DUPLICATE_NAME = "trend-large"


# --- Fixtures ---------------------------------------------------------------


def _settings(tmp_path: Path, url: str) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=SecretStr(url),
        sleeves_dir=tmp_path / "sleeves",
        token_path=tmp_path / "tokens.json",
        state_db_path=tmp_path / "state.sqlite3",
        log_path=tmp_path / "logs" / "app.log",
        kill_switch_path=tmp_path / "KILL_SWITCH",
        agent_activity_db_path=tmp_path / "agent_activity.sqlite3",
    )


def _quote(symbol: str, price: str) -> Quote:
    value = Decimal(price)
    return Quote(
        symbol=symbol,
        bid=value,
        ask=value,
        last=value,
        mark=value,
        quote_time=datetime(2026, 7, 28, 20, 0, tzinfo=UTC),
    )


def _cycle_report(
    *,
    now: datetime,
    strategy: str,
    valuation: PaperValuation,
    filled: int = 1,
    rejected: int = 0,
) -> agent.CycleReport:
    """A recorded valuation cycle with the given fill/rejection shape."""
    outcomes = [
        agent.DecisionOutcome(
            proposal=agent.OrderProposal(
                request=OrderRequest(
                    side=OrderSide.BUY,
                    symbol="MSFT",
                    quantity=1,
                    limit_price=Decimal("400.00"),
                ),
                rationale="synthetic fixture proposal",
            ),
            status="FILLED" if index < filled else "REJECTED",
            fill_price=Decimal("400.00") if index < filled else None,
            detail="",
        )
        for index in range(filled + rejected)
    ]
    return agent.CycleReport(
        now=now,
        strategy=strategy,
        outcomes=outcomes,
        starting_value=valuation.starting_cash,
        ending_value=valuation.total_value,
        valuation=valuation,
        missing_quotes=[],
    )


def _observation(
    *,
    cohort_id: str,
    sleeve_id: str,
    strategy: str,
    strategy_hash: str,
    session: date,
    total_value: Decimal | None,
    status: evaluation.ObservationStatus = evaluation.ObservationStatus.OFFICIAL,
    run_id: str | None = None,
) -> evaluation.OfficialDailyObservation:
    stamp = datetime.combine(session, datetime.min.time(), tzinfo=UTC).replace(hour=20)
    official = status is evaluation.ObservationStatus.OFFICIAL
    return evaluation.OfficialDailyObservation(
        cohort_id=cohort_id,
        run_id=run_id or f"run-{cohort_id}-{session.isoformat()}",
        sleeve_id=sleeve_id,
        strategy=strategy,
        strategy_hash=strategy_hash,
        session_date=session,
        decision_time=stamp,
        valuation_time=stamp + timedelta(minutes=5),
        status=status,
        total_value=total_value,
        return_pct=Decimal("0.01") if official else None,
        quote_coverage=Decimal("1") if official else Decimal("0.5"),
        snapshot_ids={"cohort_snapshot": f"snap-{session.isoformat()}"},
        readiness_ready=official,
        readiness_reasons=() if official else ("daily_bars:stale",),
    )


@pytest.fixture
def shared(tmp_path: Path) -> Iterator[Settings]:
    """Two cohorts that share a display name, with distinct recorded state in each.

    The active sleeve holds MSFT and has three sessions of history; the superseded one
    holds XOM and has a single partial session. Any leak between them is therefore
    visible as a wrong symbol, not just a wrong number.
    """
    url = f"sqlite:///{tmp_path / 'shared.sqlite3'}"
    database = Database(url, create_schema=True)
    store = SqlAlchemySleeveStore(database)
    # Cohort members carry a versioned definition, exactly as the real ones do, so the
    # reproducible path (configuration hash, parameters, cadence) has real data.
    for cohort_id in (ACTIVE_COHORT, HISTORICAL_COHORT):
        for name in (DUPLICATE_NAME, "bench-spy"):
            store.create(
                name,
                strategy="buy-hold",
                universe=["MSFT"],
                starting_cash=Decimal("10000.00"),
                max_positions=4,
                max_position_fraction=Decimal("0.5"),
                settlement_t1=True,
                definition=strategy_registry.make_definition(
                    "buy-hold",
                    universe_definition=["MSFT"],
                    benchmark_symbol_or_sleeve="bench-spy",
                ),
                cohort_id=cohort_id,
            )
    # Unassigned sleeves with *no* definition: the pre-cohort legacy shape, which must be
    # labelled non-reproducible rather than shown with invented parameters.
    for name in ("legacy-trend", "standalone-new"):
        store.create(
            name,
            strategy="trend",
            universe=["SPY"],
            starting_cash=Decimal("25000.00"),
            max_positions=4,
            max_position_fraction=Decimal("0.5"),
        )
    database.dispose()
    storage_factory._shared_database.cache_clear()

    settings = _settings(tmp_path, url)

    runs = storage_factory.run_store(settings)

    def populate(
        name: str,
        cohort_id: str,
        symbol: str,
        sessions: list[date],
        *,
        quantity: int = 2,
        status: evaluation.ObservationStatus = evaluation.ObservationStatus.OFFICIAL,
    ) -> str:
        config = _resolve(settings, name, cohort_id)
        engine = storage_factory.paper_engine(settings, config)
        engine.place_order(
            OrderRequest(
                side=OrderSide.BUY,
                symbol=symbol,
                quantity=quantity,
                limit_price=Decimal("500.00"),
            ),
            _quote(symbol, "100.00"),
            now=datetime(2026, 7, 28, 20, 0, tzinfo=UTC),
        )
        # A recorded rejection, so the drawer's rejection-reason path has real data.
        engine.place_order(
            OrderRequest(
                side=OrderSide.SELL, symbol="ZZZZ", quantity=1, limit_price=Decimal("1.00")
            ),
            _quote("ZZZZ", "1.00"),
            now=datetime(2026, 7, 28, 20, 1, tzinfo=UTC),
        )
        evaluations = storage_factory.evaluation_store(settings, config)
        for index, session in enumerate(sessions):
            stamp = datetime.combine(session, datetime.min.time(), tzinfo=UTC).replace(hour=20)
            # An observation's ``run_id`` is a foreign key to a persisted cohort run, so
            # the run has to exist first. That constraint is the point: an official
            # observation is not allowed to float free of the session that produced it.
            run = runs.ensure_run(
                cohort_id=cohort_id,
                session=scheduling.session_for_date(session),
                expected_members=[config.identity],
            )
            valuation = engine.value({symbol: Decimal("100.00")})
            evaluations.record_cycle(
                _cycle_report(
                    now=stamp,
                    strategy=config.strategy,
                    valuation=valuation,
                    filled=1,
                    rejected=1,
                )
            )
            evaluations.record_official_observation(
                _observation(
                    cohort_id=cohort_id,
                    sleeve_id=config.identity,
                    strategy=config.strategy,
                    strategy_hash=config.configuration_hash,
                    session=session,
                    total_value=Decimal("10000.00") + Decimal(index * 25),
                    status=status,
                    run_id=run.run_id,
                )
            )
        return config.identity

    populate(
        DUPLICATE_NAME,
        ACTIVE_COHORT,
        "MSFT",
        [date(2026, 7, 28), date(2026, 7, 29), date(2026, 7, 30)],
    )
    # A different quantity as well as a different symbol, so identical starting capital
    # cannot make the two sleeves' recorded cash coincide and hide a leak.
    populate(
        DUPLICATE_NAME,
        HISTORICAL_COHORT,
        "XOM",
        [date(2026, 7, 27)],
        quantity=7,
        status=evaluation.ObservationStatus.PARTIAL,
    )

    # The legacy sleeve gets cycles but no official observations, which is what makes it
    # legacy rather than standalone.
    legacy = _resolve(settings, "legacy-trend", "")
    legacy_engine = storage_factory.paper_engine(settings, legacy)
    legacy_engine.place_order(
        OrderRequest(side=OrderSide.BUY, symbol="SPY", quantity=3, limit_price=Decimal("600.00")),
        _quote("SPY", "550.00"),
        now=datetime(2026, 6, 1, 20, 0, tzinfo=UTC),
    )
    storage_factory.evaluation_store(settings, legacy).record_cycle(
        _cycle_report(
            now=datetime(2026, 6, 1, 20, 15, tzinfo=UTC),
            strategy=legacy.strategy,
            valuation=legacy_engine.value({"SPY": Decimal("560.00")}),
        )
    )

    yield settings
    storage_factory._shared_database.cache_clear()


def _resolve(settings: Settings, name: str, cohort_id: str) -> object:
    """The one registered config with this name in this scope. Test helper only."""
    configs = [
        config
        for config in storage_factory.sleeve_store(settings).list()
        if config.name == name and config.cohort_id == cohort_id
    ]
    assert len(configs) == 1, f"expected exactly one {name!r} in {cohort_id!r}"
    return configs[0]


def _id(settings: Settings, name: str, cohort_id: str) -> str:
    config = _resolve(settings, name, cohort_id)
    return config.identity  # type: ignore[attr-defined]


# --- Identity and scoping ---------------------------------------------------


def test_active_cohort_sleeve_reports_positions_cash_and_history(shared: Settings) -> None:
    """The headline case: a July 28 cohort member with holdings and recorded history."""
    detail = sleeve_detail.collect_sleeve_detail(
        shared, _id(shared, DUPLICATE_NAME, ACTIVE_COHORT)
    )

    assert detail.identity.name == DUPLICATE_NAME
    assert detail.identity.cohort_id == ACTIVE_COHORT
    assert detail.identity.scope is sleeve_detail.SleeveScope.OFFICIAL_COHORT
    assert detail.identity.lifecycle.historical is False

    assert detail.positions.available is True
    assert [row.symbol for row in detail.positions.rows] == ["MSFT"]
    assert detail.positions.rows[0].quantity == 2
    assert detail.positions.rows[0].cost_basis == Decimal("200.00")

    assert detail.cash.available is True
    assert detail.cash.starting_capital == Decimal("10000.00")
    assert detail.cash.settled_cash is not None

    assert detail.observations.available is True
    assert len(detail.observations.rows) == 3
    assert detail.equity_history.available is True
    assert detail.equity_history.source == "official-observation"
    assert detail.cycles.rows, "recorded valuation cycles must be exposed"
    assert detail.simulated_orders.available is True


def test_duplicate_display_name_resolves_to_the_requested_sleeve_only(
    shared: Settings,
) -> None:
    """Two sleeves share ``trend-large``. Only the requested identity may come back."""
    active_id = _id(shared, DUPLICATE_NAME, ACTIVE_COHORT)
    superseded_id = _id(shared, DUPLICATE_NAME, HISTORICAL_COHORT)
    assert active_id != superseded_id

    active = sleeve_detail.collect_sleeve_detail(shared, active_id)
    superseded = sleeve_detail.collect_sleeve_detail(shared, superseded_id)

    assert active.identity.name == superseded.identity.name == DUPLICATE_NAME
    assert active.identity.sleeve_id == active_id
    assert superseded.identity.sleeve_id == superseded_id
    assert active.identity.cohort_id == ACTIVE_COHORT
    assert superseded.identity.cohort_id == HISTORICAL_COHORT


def test_a_bare_display_name_is_never_resolved(shared: Settings) -> None:
    """Passing the *name* must not open either sleeve, however tempting the shape.

    ``SleeveStore.resolve`` would happily fall back to a global name match here. This
    module must not, because with a duplicated name that fallback is a coin flip.
    """
    with pytest.raises(sleeve_detail.UnknownSleeve):
        sleeve_detail.collect_sleeve_detail(shared, DUPLICATE_NAME)


def test_no_cross_cohort_leakage_of_positions_orders_cycles_or_observations(
    shared: Settings,
) -> None:
    """The two same-named sleeves must share nothing at all."""
    active = sleeve_detail.collect_sleeve_detail(
        shared, _id(shared, DUPLICATE_NAME, ACTIVE_COHORT)
    )
    superseded = sleeve_detail.collect_sleeve_detail(
        shared, _id(shared, DUPLICATE_NAME, HISTORICAL_COHORT)
    )

    assert [row.symbol for row in active.positions.rows] == ["MSFT"]
    assert [row.symbol for row in superseded.positions.rows] == ["XOM"]

    assert {row.symbol for row in active.simulated_orders.rows} == {"MSFT", "ZZZZ"}
    assert {row.symbol for row in superseded.simulated_orders.rows} == {"XOM", "ZZZZ"}

    assert len(active.observations.rows) == 3
    assert len(superseded.observations.rows) == 1
    assert {row.session_date for row in active.observations.rows}.isdisjoint(
        {row.session_date for row in superseded.observations.rows}
    )

    # Cash and cycles are per-sleeve too: identical starting capital must not become
    # identical *recorded* state.
    assert active.cash.settled_cash != superseded.cash.settled_cash
    assert len(active.cycles.rows) != len(superseded.cycles.rows)

    # Every observation must cite a run belonging to *its own* cohort, and the two
    # cohorts' run sets must not intersect. Comparing against the persisted runs rather
    # than a string prefix means a real cross-cohort attribution would be caught.
    runs = storage_factory.run_store(shared)
    active_runs = {run.run_id for run in runs.list(cohort_id=ACTIVE_COHORT)}
    superseded_runs = {run.run_id for run in runs.list(cohort_id=HISTORICAL_COHORT)}
    assert active_runs and superseded_runs
    assert active_runs.isdisjoint(superseded_runs)

    assert {row.run_id for row in active.observations.rows} <= active_runs
    assert {row.run_id for row in superseded.observations.rows} <= superseded_runs
    assert {row.run_id for row in active.runs.rows} <= active_runs
    assert {row.run_id for row in superseded.runs.rows} <= superseded_runs


def test_benchmark_sleeve_in_the_same_cohort_is_a_separate_record(shared: Settings) -> None:
    """Requesting one member never merges in a sibling's state."""
    detail = sleeve_detail.collect_sleeve_detail(shared, _id(shared, "bench-spy", ACTIVE_COHORT))
    assert detail.identity.name == "bench-spy"
    assert detail.positions.rows == []
    assert detail.observations.rows == []


# --- Lifecycle --------------------------------------------------------------


def test_superseded_sleeve_is_inspectable_read_only_and_clearly_labelled(
    shared: Settings,
) -> None:
    detail = sleeve_detail.collect_sleeve_detail(
        shared, _id(shared, DUPLICATE_NAME, HISTORICAL_COHORT)
    )

    assert detail.read_only is True
    assert detail.identity.lifecycle.historical is True
    assert detail.identity.lifecycle.lifecycle == "superseded"
    assert detail.identity.lifecycle.label
    assert detail.identity.lifecycle.superseded_by == ACTIVE_COHORT
    assert detail.identity.lifecycle.actionable is False
    assert any("superseded" in warning.lower() for warning in detail.warnings)

    # Still fully readable: withdrawing the experiment did not withdraw its evidence.
    assert detail.positions.available is True
    assert detail.observations.rows


def test_partial_observation_is_labelled_not_presented_as_complete(shared: Settings) -> None:
    detail = sleeve_detail.collect_sleeve_detail(
        shared, _id(shared, DUPLICATE_NAME, HISTORICAL_COHORT)
    )
    assert detail.recorded_valuation.status == "partial"
    assert detail.recorded_valuation.message is not None
    assert "not a complete official" in detail.recorded_valuation.message


def test_legacy_and_standalone_sleeves_report_their_own_scope(shared: Settings) -> None:
    legacy = sleeve_detail.collect_sleeve_detail(shared, _id(shared, "legacy-trend", ""))
    standalone = sleeve_detail.collect_sleeve_detail(shared, _id(shared, "standalone-new", ""))

    assert legacy.identity.scope is sleeve_detail.SleeveScope.LEGACY
    assert standalone.identity.scope is sleeve_detail.SleeveScope.STANDALONE

    # No cohort, so no cohort runs — stated, not silently empty.
    for detail in (legacy, standalone):
        assert detail.runs.available is False
        assert detail.runs.message is not None
        assert "not a member of an official cohort" in detail.runs.message

    # The legacy sleeve has cycles but no observations, so equity falls back honestly.
    assert legacy.equity_history.available is True
    assert legacy.equity_history.source == "evaluation-cycle"
    assert legacy.equity_history.message is not None
    assert legacy.recorded_valuation.source == "evaluation-cycle"
    assert legacy.recorded_valuation.return_pct_basis == "percent"


def test_a_sleeve_without_a_definition_is_labelled_non_reproducible(
    shared: Settings,
) -> None:
    """No captured parameters means say so, not invent them."""
    legacy = sleeve_detail.collect_sleeve_detail(shared, _id(shared, "legacy-trend", ""))

    assert legacy.strategy.available is True
    assert legacy.strategy.reproducible is False
    assert legacy.strategy.message is not None
    assert "non-reproducible" in legacy.strategy.message
    assert legacy.strategy.parameters == {}
    assert legacy.strategy.strategy_id is None
    assert legacy.strategy.configuration_hash is None
    assert legacy.lineage.configuration_hash is None
    # The sleeve's own recorded universe still shows, because that *was* captured.
    assert legacy.strategy.universe == ["SPY"]


def test_a_cohort_member_exposes_its_versioned_definition(shared: Settings) -> None:
    detail = sleeve_detail.collect_sleeve_detail(
        shared, _id(shared, DUPLICATE_NAME, ACTIVE_COHORT)
    )
    assert detail.strategy.reproducible is True
    assert detail.strategy.strategy_id
    assert detail.strategy.strategy_version
    assert detail.strategy.configuration_hash
    assert detail.strategy.decision_frequency
    assert detail.strategy.benchmark_symbol_or_sleeve == "bench-spy"
    assert detail.strategy.long_only is not None


def test_official_observation_return_is_labelled_as_a_ratio(shared: Settings) -> None:
    """The two sources disagree about units, so the unit must travel with the value."""
    detail = sleeve_detail.collect_sleeve_detail(
        shared, _id(shared, DUPLICATE_NAME, ACTIVE_COHORT)
    )
    assert detail.recorded_valuation.source == "official-observation"
    assert detail.recorded_valuation.return_pct_basis == "ratio"


# --- Empty and incomplete records -------------------------------------------


def test_sleeve_with_no_positions_or_cycles_explains_every_empty_section(
    shared: Settings,
) -> None:
    """A registered-but-never-run sleeve. Every empty section must say why it is empty.

    The shared registry opens a paper account when it creates a sleeve, so cash *is*
    readable here and correctly reports untouched starting capital. What must not appear
    is a valuation, a return, or an equity series — because none was ever recorded, and
    reporting those as zero would be a fabrication.
    """
    detail = sleeve_detail.collect_sleeve_detail(shared, _id(shared, "standalone-new", ""))

    assert detail.positions.available is True
    assert detail.positions.rows == []
    assert detail.positions.message == "This sleeve currently holds no paper positions."
    assert detail.positions.total_cost_basis == Decimal(0)

    assert detail.cash.available is True
    assert detail.cash.settled_cash == Decimal("25000.00")
    assert detail.cash.realized_pnl == Decimal(0)

    assert detail.recorded_valuation.available is False
    assert detail.recorded_valuation.total_equity is None
    assert detail.performance.available is False
    assert detail.performance.message is not None
    # An unstarted sleeve is not a zero-return sleeve, and must never read as one.
    assert "not a zero return" in detail.performance.message
    assert detail.performance.total_return_pct is None
    assert detail.equity_history.available is False
    assert detail.equity_history.points == []
    assert detail.cycles.rows == []
    assert detail.simulated_orders.rows == []


def test_viewing_a_sleeve_never_creates_paper_state(shared: Settings) -> None:
    """The probe must not bootstrap an account row for a sleeve that has none.

    Constructing a paper engine would create one, which is a write. Reading a record
    must leave the store exactly as it was.
    """
    from schwab_trader.storage.schema import PaperAccount as PaperAccountRow

    sleeve_id = _id(shared, "standalone-new", "")
    database = storage_factory.database(shared)
    assert database is not None
    with database.session() as session:
        session.query(PaperAccountRow).filter(
            PaperAccountRow.sleeve_id == sleeve_id
        ).delete()

    detail = sleeve_detail.collect_sleeve_detail(shared, sleeve_id)
    assert detail.positions.available is False

    with database.session() as session:
        assert session.get(PaperAccountRow, sleeve_id) is None, (
            "reading a sleeve must not create paper state for it"
        )


def test_unreadable_evaluation_history_fails_soft_without_a_traceback(
    shared: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An incomplete record degrades per section; identity and capital still render."""

    def broken(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("simulated storage fault")

    monkeypatch.setattr(storage_factory, "evaluation_store", broken)
    detail = sleeve_detail.collect_sleeve_detail(
        shared, _id(shared, DUPLICATE_NAME, ACTIVE_COHORT)
    )

    assert detail.identity.name == DUPLICATE_NAME
    assert detail.capital.starting_capital == Decimal("10000.00")
    assert detail.observations.available is False
    assert detail.cycles.available is False
    assert detail.equity_history.available is False
    assert detail.performance.available is False
    assert detail.warnings
    # The sanitized explanation must not carry the internal exception text.
    assert all("simulated storage fault" not in warning for warning in detail.warnings)


def test_unreadable_paper_store_leaves_the_rest_of_the_record_intact(
    shared: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("simulated paper fault")

    monkeypatch.setattr(storage_factory, "paper_engine", broken)
    detail = sleeve_detail.collect_sleeve_detail(
        shared, _id(shared, DUPLICATE_NAME, ACTIVE_COHORT)
    )

    assert detail.positions.available is False
    assert detail.cash.available is False
    assert detail.observations.available is True
    assert detail.observations.rows
    assert all("simulated paper fault" not in warning for warning in detail.warnings)


# --- Marks ------------------------------------------------------------------


def test_positions_never_fabricate_a_current_mark(shared: Settings) -> None:
    """Cost basis is exact; market value needs a quote this view will not fetch."""
    detail = sleeve_detail.collect_sleeve_detail(
        shared, _id(shared, DUPLICATE_NAME, ACTIVE_COHORT)
    )
    row = detail.positions.rows[0]
    assert row.market_value is None
    assert row.unrealized_pnl is None
    assert row.mark_status == "unavailable"
    assert "never requests a quote" in detail.positions.mark_note
    # A single-position sleeve is the whole cost basis, and the weight says so.
    assert row.cost_basis_weight == Decimal(1)


# --- Bounding and pagination ------------------------------------------------


def test_history_is_paginated_from_the_newest_end(shared: Settings) -> None:
    sleeve_id = _id(shared, DUPLICATE_NAME, ACTIVE_COHORT)
    first = sleeve_detail.collect_sleeve_detail(shared, sleeve_id, limit=2, offset=0)
    second = sleeve_detail.collect_sleeve_detail(shared, sleeve_id, limit=2, offset=2)

    assert first.observations.page is not None
    assert first.observations.page.limit == 2
    assert first.observations.page.returned == 2
    assert first.observations.page.available == 3
    assert first.observations.page.has_more is True

    assert second.observations.page is not None
    assert second.observations.page.returned == 1
    assert second.observations.page.has_more is False

    # Offset 0 is the newest window, and the pages do not overlap.
    assert first.observations.rows[0].session_date == date(2026, 7, 30)
    assert second.observations.rows[0].session_date == date(2026, 7, 28)


def test_equity_window_is_chart_ordered_while_paging_from_the_newest_end(
    shared: Settings,
) -> None:
    detail = sleeve_detail.collect_sleeve_detail(
        shared, _id(shared, DUPLICATE_NAME, ACTIVE_COHORT), limit=2, offset=0
    )
    sessions = [point.session_date for point in detail.equity_history.points]
    assert sessions == [date(2026, 7, 29), date(2026, 7, 30)], "points plot oldest-first"


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (None, sleeve_detail.DEFAULT_PAGE_LIMIT),
        (0, 1),
        (-5, 1),
        (10, 10),
        (sleeve_detail.MAX_PAGE_LIMIT, sleeve_detail.MAX_PAGE_LIMIT),
        (sleeve_detail.MAX_PAGE_LIMIT + 1, sleeve_detail.MAX_PAGE_LIMIT),
        (10_000, sleeve_detail.MAX_PAGE_LIMIT),
    ],
)
def test_maximum_page_size_is_enforced(requested: int | None, expected: int) -> None:
    limit, offset = sleeve_detail.normalize_page(requested, None)
    assert limit == expected
    assert offset == 0


def test_negative_offset_is_clamped_rather_than_wrapping(shared: Settings) -> None:
    detail = sleeve_detail.collect_sleeve_detail(
        shared, _id(shared, DUPLICATE_NAME, ACTIVE_COHORT), limit=1, offset=-10
    )
    assert detail.observations.page is not None
    assert detail.observations.page.offset == 0


def test_offset_past_the_end_returns_an_empty_page_not_an_error(shared: Settings) -> None:
    detail = sleeve_detail.collect_sleeve_detail(
        shared, _id(shared, DUPLICATE_NAME, ACTIVE_COHORT), limit=5, offset=500
    )
    assert detail.observations.rows == []
    assert detail.observations.page is not None
    assert detail.observations.page.returned == 0
    assert detail.observations.page.has_more is False


# --- Request validation -----------------------------------------------------


@pytest.mark.parametrize(
    "candidate",
    [
        "",
        "   ",
        "has spaces",
        "semi;colon",
        "slash/path",
        "../../etc/passwd",
        "'; DROP TABLE sleeves;--",
        "a" * 65,
        "unicode\u00e9",
        "new\nline",
    ],
)
def test_malformed_identifiers_are_refused_before_any_lookup(candidate: str) -> None:
    with pytest.raises(sleeve_detail.InvalidSleeveId) as excinfo:
        sleeve_detail.normalize_sleeve_id(candidate)
    assert excinfo.value.code == "invalid-sleeve-id"
    # The refusal must not echo the caller's input back into the response, which is what
    # would turn a rejected identifier into a reflection vector.
    if candidate.strip():
        assert candidate.strip() not in excinfo.value.message


def test_wellformed_but_unregistered_identifier_is_unknown_not_invalid(
    shared: Settings,
) -> None:
    with pytest.raises(sleeve_detail.UnknownSleeve):
        sleeve_detail.collect_sleeve_detail(shared, "0" * 64)


def test_unreadable_registry_is_reported_as_unavailable(
    shared: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("simulated registry fault")

    monkeypatch.setattr(storage_factory, "sleeve_store", broken)
    with pytest.raises(sleeve_detail.RegistryUnavailable) as excinfo:
        sleeve_detail.collect_sleeve_detail(shared, "0" * 64)
    assert "simulated registry fault" not in excinfo.value.message


# --- Runs -------------------------------------------------------------------


def test_runs_are_scoped_to_the_sleeves_own_cohort_and_membership(shared: Settings) -> None:
    """A run for the other cohort, and a run this sleeve was not in, must not appear.

    The fixture already recorded three ``paper-first-2026-07-28`` runs expecting only the
    active ``trend-large``, and one ``paper-first-2026-07-27`` run expecting only the
    superseded one. This adds a fourth active-cohort run that expects a *different*
    member, which is the case a cohort-only filter would wrongly include.
    """
    active_id = _id(shared, DUPLICATE_NAME, ACTIVE_COHORT)
    bench_id = _id(shared, "bench-spy", ACTIVE_COHORT)
    other_id = _id(shared, DUPLICATE_NAME, HISTORICAL_COHORT)
    runs = storage_factory.run_store(shared)

    runs.ensure_run(
        cohort_id=ACTIVE_COHORT,
        session=scheduling.session_for_date(date(2026, 7, 31)),
        expected_members=[bench_id],
    )

    detail = sleeve_detail.collect_sleeve_detail(shared, active_id)
    assert detail.runs.available is True
    assert len(detail.runs.rows) == 3, "the bench-only run must not be attributed here"
    assert [row.scheduled_for for row in detail.runs.rows] == [
        date(2026, 7, 30),
        date(2026, 7, 29),
        date(2026, 7, 28),
    ], "runs are newest session first"

    bench_detail = sleeve_detail.collect_sleeve_detail(shared, bench_id)
    assert [row.scheduled_for for row in bench_detail.runs.rows] == [date(2026, 7, 31)]

    superseded = sleeve_detail.collect_sleeve_detail(shared, other_id)
    assert [row.scheduled_for for row in superseded.runs.rows] == [date(2026, 7, 27)]
    assert {row.run_id for row in superseded.runs.rows}.isdisjoint(
        {row.run_id for row in detail.runs.rows}
    )


def test_member_run_failure_surfaces_its_sanitized_reason(shared: Settings) -> None:
    active_id = _id(shared, DUPLICATE_NAME, ACTIVE_COHORT)
    runs = storage_factory.run_store(shared)
    # Idempotent: the fixture already created this run with the same expected member.
    run = runs.ensure_run(
        cohort_id=ACTIVE_COHORT,
        session=scheduling.session_for_date(date(2026, 7, 29)),
        expected_members=[active_id],
    )
    runs.start_member(run.run_id, active_id)
    runs.finish_member(
        run.run_id,
        active_id,
        status=sleeve_runs.MemberRunStatus.FAILED,
        error=sleeve_runs.SleeveRunError(
            code="data_deadline_exceeded",
            message="daily bars were stale past the retry deadline",
            member_id=active_id,
            retryable=False,
        ),
    )

    detail = sleeve_detail.collect_sleeve_detail(shared, active_id)
    row = next(entry for entry in detail.runs.rows if entry.run_id == run.run_id)
    assert row.member_status == "failed"
    assert row.member_error_code == "data_deadline_exceeded"
    assert row.member_error_message is not None


# --- Lineage ----------------------------------------------------------------


def test_lineage_exposes_the_identities_that_support_the_record(shared: Settings) -> None:
    detail = sleeve_detail.collect_sleeve_detail(
        shared, _id(shared, DUPLICATE_NAME, ACTIVE_COHORT)
    )
    assert detail.lineage.cohort_id == ACTIVE_COHORT
    assert detail.lineage.namespace_id
    assert detail.lineage.configuration_hash
    assert detail.lineage.strategy_hashes
    assert len(detail.lineage.run_ids) == 3
    assert detail.lineage.snapshot_ids
    # An honest note about what storage does not record, rather than silence.
    assert any("rationale" in note for note in detail.lineage.notes)


# --- Sanitization -----------------------------------------------------------


def test_no_secret_or_storage_detail_appears_in_the_serialized_record(
    shared: Settings,
) -> None:
    payload = sleeve_detail.collect_sleeve_detail(
        shared, _id(shared, DUPLICATE_NAME, ACTIVE_COHORT)
    ).model_dump_json()
    for forbidden in (
        "sqlite:///",
        "postgresql://",
        "postgres://",
        "sslmode",
        "password",
        "token",
        "account_hash",
        ".env",
        "Traceback",
    ):
        assert forbidden not in payload, f"{forbidden!r} leaked into the sleeve record"


def test_no_filesystem_path_is_exposed(shared: Settings) -> None:
    payload = json.loads(
        sleeve_detail.collect_sleeve_detail(
            shared, _id(shared, "legacy-trend", "")
        ).model_dump_json()
    )
    flattened = json.dumps(payload)
    assert str(shared.sleeves_dir) not in flattened
    assert "C:\\\\" not in flattened


# --- HTTP contract ----------------------------------------------------------


def _request(settings: Settings, path: str) -> tuple[int, str]:
    server = dashboard.make_server(settings, port=0, client_factory=None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read().decode("utf-8")
        conn.close()
        return response.status, body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_api_sleeve_returns_the_record_for_a_stable_id(shared: Settings) -> None:
    sleeve_id = _id(shared, DUPLICATE_NAME, ACTIVE_COHORT)
    status, body = _request(shared, f"/api/sleeve?sleeve_id={sleeve_id}")
    assert status == 200
    payload = json.loads(body)
    assert payload["identity"]["sleeve_id"] == sleeve_id
    assert payload["read_only"] is True
    assert payload["contract_version"] == sleeve_detail.DETAIL_CONTRACT_VERSION


def test_api_sleeve_honors_the_requested_window(shared: Settings) -> None:
    sleeve_id = _id(shared, DUPLICATE_NAME, ACTIVE_COHORT)
    status, body = _request(shared, f"/api/sleeve?sleeve_id={sleeve_id}&limit=1&offset=1")
    assert status == 200
    page = json.loads(body)["observations"]["page"]
    assert page["limit"] == 1
    assert page["offset"] == 1
    assert page["returned"] == 1


def test_api_sleeve_clamps_an_oversized_limit_and_reports_what_it_used(
    shared: Settings,
) -> None:
    sleeve_id = _id(shared, DUPLICATE_NAME, ACTIVE_COHORT)
    status, body = _request(shared, f"/api/sleeve?sleeve_id={sleeve_id}&limit=99999")
    assert status == 200
    assert json.loads(body)["observations"]["page"]["limit"] == sleeve_detail.MAX_PAGE_LIMIT


def test_api_sleeve_ignores_a_non_numeric_window_rather_than_failing(
    shared: Settings,
) -> None:
    sleeve_id = _id(shared, DUPLICATE_NAME, ACTIVE_COHORT)
    status, body = _request(shared, f"/api/sleeve?sleeve_id={sleeve_id}&limit=abc&offset=xyz")
    assert status == 200
    page = json.loads(body)["observations"]["page"]
    assert page["limit"] == sleeve_detail.DEFAULT_PAGE_LIMIT
    assert page["offset"] == 0


@pytest.mark.parametrize(
    ("query", "expected_status", "expected_code"),
    [
        ("", 400, "invalid-sleeve-id"),
        ("?sleeve_id=", 400, "invalid-sleeve-id"),
        ("?sleeve_id=not%20a%20valid%20id", 400, "invalid-sleeve-id"),
        ("?sleeve_id=" + "0" * 64, 404, "unknown-sleeve"),
        (f"?sleeve_id={DUPLICATE_NAME}", 404, "unknown-sleeve"),
    ],
)
def test_api_sleeve_refusals_carry_a_stable_code_and_no_internals(
    shared: Settings, query: str, expected_status: int, expected_code: str
) -> None:
    status, body = _request(shared, f"/api/sleeve{query}")
    assert status == expected_status
    payload = json.loads(body)
    assert payload["error"]["code"] == expected_code
    assert payload["error"]["message"]
    assert "Traceback" not in body
    assert "sqlite" not in body.lower()


def test_api_sleeve_is_read_only(shared: Settings) -> None:
    """No verb other than GET reaches the detail record."""
    server = dashboard.make_server(shared, port=0, client_factory=None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            conn.request(method, "/api/sleeve?sleeve_id=" + "0" * 64)
            response = conn.getresponse()
            response.read()
            conn.close()
            assert response.status in (404, 405), f"{method} must not reach the record"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_api_data_does_not_carry_the_detail_record(shared: Settings) -> None:
    """The dashboard poll must not get materially heavier because detail now exists.

    This is the whole reason detail is a separate route. If a future change folds any of
    it into the payload polled every 30 seconds, this fails.
    """
    status, body = _request(shared, "/api/data")
    assert status == 200
    payload = json.loads(body)

    assert "sleeve_detail" not in payload
    assert set(payload) == set(dashboard.DashboardData.model_fields)

    # The leaderboard row is still a summary of scalars, not a full record. `cycles`
    # here is a *count* that predates this feature, which is why the shape is pinned
    # exactly rather than by scanning for suspicious-looking names.
    assert payload["sleeves"], "the fixture registers sleeves, so rows are expected"
    row = payload["sleeves"][0]
    assert set(row) == set(dashboard.SleeveRow.model_fields)
    assert not any(isinstance(value, list | dict) for value in row.values()), (
        "a leaderboard row must not carry an embedded history"
    )

    # Per-sleeve histories must not have crept into the cohort section either.
    for definition in payload["cohort"]["sleeve_definitions"]:
        assert set(definition) == set(dashboard.CohortSleeveView.model_fields)

    # The detail contract's own marker fields must be absent from the polled payload.
    for detail_only in ("mark_note", "cost_basis_weight", "return_pct_basis", "read_only"):
        assert detail_only not in body


# --- Isolation --------------------------------------------------------------


def test_no_network_is_reachable_during_a_detail_read(
    shared: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Assembling the record must not open a socket to anything, ever.

    Covers the whole forbidden set at once: Schwab, OAuth, Neon, and SMTP are all
    reachable only through a socket, so refusing every outbound connection refuses all
    of them without needing to name each host.
    """

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("sleeve detail must not open a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    detail = sleeve_detail.collect_sleeve_detail(
        shared, _id(shared, DUPLICATE_NAME, ACTIVE_COHORT)
    )
    assert detail.identity.name == DUPLICATE_NAME


def test_collecting_a_record_mutates_nothing(shared: Settings) -> None:
    """Two reads either side of a read must leave every recorded value identical."""
    sleeve_id = _id(shared, DUPLICATE_NAME, ACTIVE_COHORT)
    before = sleeve_detail.collect_sleeve_detail(shared, sleeve_id)
    sleeve_detail.collect_sleeve_detail(shared, sleeve_id)
    after = sleeve_detail.collect_sleeve_detail(shared, sleeve_id)

    for section in ("positions", "cash", "observations", "cycles", "simulated_orders", "runs"):
        assert getattr(before, section).model_dump(
            exclude={"as_of"}
        ) == getattr(after, section).model_dump(exclude={"as_of"}), (
            f"{section} changed across reads"
        )


def test_scope_values_agree_with_the_dashboard_leaderboard() -> None:
    """One shared enum; a divergence here would classify the same sleeve two ways."""
    assert dashboard.SleeveScope is sleeve_detail.SleeveScope


# --- Fixtures shipped to the frontend ---------------------------------------

_FIXTURE_DIR = Path(__file__).parents[1] / "frontend" / "src" / "fixtures"
_DETAIL_FIXTURES = sorted(_FIXTURE_DIR.glob("sleeve-detail-*.json"))

_REQUIRED_DETAIL_FIXTURES = {
    "sleeve-detail-active-cohort-sleeve",
    "sleeve-detail-superseded-duplicate-name",
    "sleeve-detail-legacy-sleeve",
    "sleeve-detail-standalone-empty-sleeve",
    "sleeve-detail-incomplete-record-sleeve",
}


def test_every_required_sleeve_detail_state_has_a_fixture() -> None:
    assert _REQUIRED_DETAIL_FIXTURES <= {path.stem for path in _DETAIL_FIXTURES}


@pytest.mark.parametrize("path", _DETAIL_FIXTURES, ids=lambda path: path.stem)
def test_committed_detail_fixture_parses_as_the_live_contract(path: Path) -> None:
    detail = sleeve_detail.SleeveDetail.model_validate_json(path.read_text("utf-8"))
    assert detail.read_only is True
    assert detail.contract_version == sleeve_detail.DETAIL_CONTRACT_VERSION
    assert detail.identity.sleeve_id


def test_detail_fixtures_reproduce_the_duplicate_name_collision() -> None:
    """The fixtures must keep depicting the case the design exists to survive."""
    active = sleeve_detail.SleeveDetail.model_validate_json(
        (_FIXTURE_DIR / "sleeve-detail-active-cohort-sleeve.json").read_text("utf-8")
    )
    superseded = sleeve_detail.SleeveDetail.model_validate_json(
        (_FIXTURE_DIR / "sleeve-detail-superseded-duplicate-name.json").read_text("utf-8")
    )

    assert active.identity.name == superseded.identity.name
    assert active.identity.sleeve_id != superseded.identity.sleeve_id
    assert active.identity.lifecycle.historical is False
    assert superseded.identity.lifecycle.historical is True
    assert {row.symbol for row in active.positions.rows}.isdisjoint(
        {row.symbol for row in superseded.positions.rows}
    )


# --- TypeScript contract agreement ------------------------------------------

_TYPES_TS = Path(__file__).parents[1] / "frontend" / "src" / "lib" / "types.ts"

#: Every pydantic model in the detail contract, paired with the TypeScript interface
#: that mirrors it. A field added on one side and not the other is a silent contract
#: break: `tsc` cannot see the Python, and pydantic cannot see the TypeScript.
_MIRRORED_MODELS: dict[str, type] = {
    "PageInfo": sleeve_detail.PageInfo,
    "SleeveLifecycleView": sleeve_detail.SleeveLifecycleView,
    "SleeveIdentityView": sleeve_detail.SleeveIdentityView,
    "SleeveStrategyView": sleeve_detail.SleeveStrategyView,
    "SleeveCapitalView": sleeve_detail.SleeveCapitalView,
    "SleevePositionRow": sleeve_detail.SleevePositionRow,
    "SleevePositionsView": sleeve_detail.SleevePositionsView,
    "SleeveCashView": sleeve_detail.SleeveCashView,
    "RecordedValuationView": sleeve_detail.RecordedValuationView,
    "SleevePerformanceView": sleeve_detail.SleevePerformanceView,
    "EquityPointView": sleeve_detail.EquityPointView,
    "EquityHistoryView": sleeve_detail.EquityHistoryView,
    "CycleRowView": sleeve_detail.CycleRowView,
    "CyclesView": sleeve_detail.CyclesView,
    "SimulatedOrderRowView": sleeve_detail.SimulatedOrderRowView,
    "SimulatedOrdersView": sleeve_detail.SimulatedOrdersView,
    "ObservationRowView": sleeve_detail.ObservationRowView,
    "ObservationsView": sleeve_detail.ObservationsView,
    "RunRowView": sleeve_detail.RunRowView,
    "RunsView": sleeve_detail.RunsView,
    "SleeveLineageView": sleeve_detail.SleeveLineageView,
    "SleeveDetail": sleeve_detail.SleeveDetail,
}


def _typescript_fields(interface: str) -> set[str]:
    """Field names declared in one exported interface in ``types.ts``.

    A deliberately small parser: it reads the interface body, drops comments, and takes
    the identifier before each ``:`` at brace depth zero. That is enough for this file's
    plain-interface style and keeps the test dependency-free.
    """
    source = _TYPES_TS.read_text("utf-8")
    start = source.index(f"export interface {interface} {{") + len(
        f"export interface {interface} {{"
    )
    depth = 0
    end = start
    for index in range(start, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            if depth == 0:
                end = index
                break
            depth -= 1
    body = source[start:end]

    body = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)
    body = re.sub(r"//[^\n]*", "", body)

    fields: set[str] = set()
    depth = 0
    for line in body.splitlines():
        stripped = line.strip()
        if depth == 0:
            match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\??\s*:", stripped)
            if match:
                fields.add(match.group(1))
        depth += stripped.count("{") - stripped.count("}")
    return fields


@pytest.mark.parametrize(("interface", "model"), sorted(_MIRRORED_MODELS.items()))
def test_typescript_interface_matches_the_pydantic_model(interface: str, model: type) -> None:
    expected = set(model.model_fields)  # type: ignore[attr-defined]
    actual = _typescript_fields(interface)
    assert actual == expected, (
        f"{interface} in types.ts has drifted from {model.__name__}: "
        f"missing {sorted(expected - actual)}, extra {sorted(actual - expected)}"
    )


def test_every_committed_detail_fixture_key_is_declared_in_typescript() -> None:
    """The fixtures are what the frontend renders, so they must fit the declared types."""
    declared = _typescript_fields("SleeveDetail")
    for path in _DETAIL_FIXTURES:
        assert set(json.loads(path.read_text("utf-8"))) == declared, (
            f"{path.name} does not match the SleeveDetail interface"
        )


def test_no_detail_fixture_is_actionable_or_leaks_storage_detail() -> None:
    for path in _DETAIL_FIXTURES:
        payload = path.read_text("utf-8")
        detail = sleeve_detail.SleeveDetail.model_validate_json(payload)
        assert detail.read_only is True
        assert detail.identity.lifecycle.actionable is False
        for forbidden in ("sqlite:///", "postgresql://", "password", "sslmode", ".env"):
            assert forbidden not in payload, f"{forbidden!r} leaked into {path.name}"


# --- Frontend contract ------------------------------------------------------
#
# These are **source-level** checks, not rendering tests: this repository has no
# JavaScript test runner, and adding one would mean new npm dependencies that are out of
# scope here (recorded as a follow-up on the issue). They are still worth having, because
# what they pin — the dialog semantics, the state branches, the table semantics, and above
# all the absence of any action control — are the properties that must not silently
# regress. A future frontend runner should replace these with real DOM assertions.

_FRONTEND = Path(__file__).parents[1] / "frontend" / "src"
_DRAWER = _FRONTEND / "components" / "dashboard" / "SleeveDetailDrawer.tsx"
_HOOK = _FRONTEND / "lib" / "useSleeveDetail.ts"


def test_drawer_declares_accessible_dialog_semantics() -> None:
    source = _DRAWER.read_text("utf-8")
    for marker in ("role='dialog'", "aria-modal='true'", "aria-labelledby='sleeve-detail-title'"):
        assert marker in source, f"the drawer must declare {marker}"
    assert "id='sleeve-detail-title'" in source, "the labelling target must exist"
    # Keyboard dismissal and focus handling.
    assert "'Escape'" in source
    assert "panelRef.current?.focus()" in source
    assert "restoreFocusTo" in source, "focus must return to whatever opened the drawer"
    assert "aria-label='Close sleeve detail'" in source


def test_drawer_escapes_the_app_shells_transformed_wrapper() -> None:
    """The drawer must render through a portal, not in place.

    ``AppShell`` wraps its children in ``animate-fade-in``, whose ``forwards`` fill
    leaves a ``transform`` on the wrapper — and a transformed ancestor becomes the
    containing block for ``position: fixed``. Rendered in place, the drawer was trapped
    inside the scrolling ``<main>`` and clipped by the status bar instead of covering the
    viewport. Caught in manual fixture inspection; pinned here so it stays fixed.
    """
    source = _DRAWER.read_text("utf-8")
    assert "createPortal" in source
    assert "document.body" in source
    shell = (_FRONTEND / "components" / "layout" / "AppShell.tsx").read_text("utf-8")
    assert "animate-fade-in" in shell, (
        "the transformed wrapper this portal exists to escape has moved or been removed; "
        "re-check whether the portal is still required"
    )


def test_drawer_tables_are_semantically_labelled() -> None:
    source = _DRAWER.read_text("utf-8")
    assert source.count("<caption") >= 4, "every data table needs a caption"
    assert "scope='col'" in source
    assert "scope='row'" in source
    assert "aria-busy='true'" in source, "the loading state must be announced"
    assert "role='alert'" in source, "the error state must be announced"


def test_drawer_renders_every_load_state() -> None:
    source = _DRAWER.read_text("utf-8")
    for branch in ("'idle'", "'loading'", "'error'", "'ready'"):
        assert branch in source, f"the drawer must handle the {branch} state"
    # Empty and unavailable data are explained rather than rendered as blanks or zeros.
    assert "EmptyState" in source
    assert "Unavailable" in source


def test_drawer_labels_a_superseded_record_and_offers_no_action() -> None:
    source = _DRAWER.read_text("utf-8")
    assert "lifecycle.historical" in source
    assert "Closed record" in source
    assert "read only" in source.lower()


@pytest.mark.parametrize(
    "forbidden",
    [
        "onRun",
        "onSubmit",
        "onBuy",
        "onSell",
        "onReset",
        "onRemove",
        "onReplay",
        "onCancel",
        "onApprove",
        "method='post'",
        "method=\"post\"",
        "fetch('/api/sleeve', {",
        "'POST'",
        '"POST"',
        "'DELETE'",
        "'PUT'",
        "'PATCH'",
    ],
)
def test_sleeve_detail_ui_has_no_action_or_mutation_path(forbidden: str) -> None:
    """No control in this view may run, trade, reset, repair, or replay anything."""
    for path in (_DRAWER, _HOOK):
        assert forbidden not in path.read_text("utf-8"), (
            f"{forbidden!r} appears in {path.name}; this view must stay read-only"
        )


def test_drill_down_navigates_by_stable_id_never_by_display_name() -> None:
    """Every call site must hand the drawer `sleeve_id`. A name would be ambiguous."""
    comparison = (_FRONTEND / "components" / "dashboard" / "CohortComparisonPanel.tsx").read_text(
        "utf-8"
    )
    sleeves_panel = (_FRONTEND / "components" / "dashboard" / "SleevesPanel.tsx").read_text("utf-8")
    app = (_FRONTEND / "App.tsx").read_text("utf-8")

    assert "onOpenSleeve(row.sleeve_id)" in comparison
    assert "onOpenSleeve(row.sleeve_id)" in sleeves_panel
    for source in (comparison, sleeves_panel):
        assert "onOpenSleeve(row.name" not in source
        assert "onOpenSleeve(row.sleeve_name" not in source

    # The URL parameter is the id too, so a shared link reopens the same sleeve even when
    # another cohort has one with the same display name.
    assert 'params.get("sleeve")' in app
    assert 'params.set("sleeve", sleeve)' in app


def test_drawer_requests_the_lazy_route_not_the_polled_payload() -> None:
    hook = _HOOK.read_text("utf-8")
    assert "/api/sleeve?sleeve_id=" in hook
    assert "encodeURIComponent(sleeveId)" in hook, "the id must be encoded into the query"
    assert "/api/data" not in hook, "detail must not ride along with the dashboard poll"
