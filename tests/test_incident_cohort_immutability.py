"""The failed shakedown cohort is evidence, and evidence does not change.

``paper-first-2026-07-27 / 2026-07-27`` recorded a terminal ``partial (2/7 members)``
and must never be reset, deleted, repaired in place, backfilled, replayed, or
rewritten. See ``docs/incidents/2026-07-27-paper-first-cohort-daily-bars-stale.md``.

This module reconstructs that terminal run in an offline store, drives the *new* runner
against it the way a scheduler would, and proves nothing moved: not the run row, not a
member checkpoint, not an official observation, not a paper account.

Offline and deterministic. No `.env`, database, broker, or SMTP connection is reachable.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from schwab_trader import cohort_lifecycle, dashboard, scheduling, strategy_registry
from schwab_trader.agent import CycleReport
from schwab_trader.data_contracts import BarBatch, BarObservation, Provenance, TimingPolicy
from schwab_trader.data_readiness import (
    DataKind,
    DataRequirement,
    SourceProbe,
    evaluate_readiness,
)
from schwab_trader.evaluation import (
    EvaluationStore,
    ObservationStatus,
    OfficialDailyObservation,
)
from schwab_trader.market_data import Quote
from schwab_trader.models import OrderRequest, OrderSide
from schwab_trader.paper import PaperEngine
from schwab_trader.safety import KillSwitch
from schwab_trader.sleeve_runs import (
    CohortSnapshot,
    MemberRunStatus,
    SleeveRunOrchestrator,
    SleeveRunStatus,
    SleeveRunStore,
)
from schwab_trader.sleeves import SleeveStore

INCIDENT_COHORT = "paper-first-2026-07-27"
INCIDENT_SESSION = date(2026, 7, 27)
FRIDAY = date(2026, 7, 24)
CLOSE_UTC = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)

COMPLETED = ("control-cash", "bench-spy")
PARTIAL = (
    "sector-momentum",
    "trend-large",
    "low-vol-large",
    "momentum-large",
    "value-momentum-edgar",
)
ALL_MEMBERS = COMPLETED + PARTIAL


def _quote(symbol: str) -> Quote:
    value = Decimal("10")
    return Quote(
        symbol=symbol,
        bid=value,
        ask=value,
        last=value,
        mark=value,
        previous_close=value,
        quote_time=CLOSE_UTC,
    )


@pytest.fixture
def incident(tmp_path):
    """Rebuild the terminal `partial (2/7)` run exactly as it was persisted."""
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    configs = []
    for name in ALL_MEMBERS:
        definition = strategy_registry.make_definition("buy-hold", universe_definition=["AAA"])
        configs.append(
            sleeve_store.create(
                name,
                strategy="buy-hold",
                universe=["AAA"],
                starting_cash=Decimal("10000.00"),
                max_positions=3,
                max_position_fraction=Decimal("1.0"),
                definition=definition,
                cohort_id=INCIDENT_COHORT,
            )
        )
    configs = tuple(configs)
    by_name = {cfg.name: cfg for cfg in configs}

    run_store = SleeveRunStore(tmp_path / "runs.sqlite3")
    session = scheduling.session_for_date(INCIDENT_SESSION)
    run = run_store.ensure_run(
        cohort_id=INCIDENT_COHORT,
        session=session,
        expected_members=[cfg.identity for cfg in configs],
        now=CLOSE_UTC,
    )
    run_store.set_snapshot(
        run.run_id,
        snapshot_id="snapshot:incident",
        quote_snapshot_id="quotes:incident",
        data_snapshot_ids={"daily_bars": "bars:incident"},
    )

    for name in COMPLETED:
        cfg = by_name[name]
        run_store.start_member(run.run_id, cfg.identity, now=CLOSE_UTC)
        run_store.finish_member(
            run.run_id, cfg.identity, status=MemberRunStatus.COMPLETED, now=CLOSE_UTC
        )
        # These two members genuinely executed: their paper cash, position, fill, and
        # recorded cycle for that session are real. Seeding them is what makes the
        # immutability assertions meaningful rather than empty-compared-to-empty.
        engine = PaperEngine(
            sleeve_store.paper_path(name),
            starting_cash=cfg.starting_cash,
            settle_t1=cfg.settlement_t1,
        )
        order = engine.place_order(
            OrderRequest(
                side=OrderSide.BUY,
                symbol="AAA",
                quantity=10,
                limit_price=Decimal("12"),
            ),
            _quote("AAA"),
            now=CLOSE_UTC,
        )
        assert order.status == "FILLED", "the fixture must actually change paper state"
        valuation = engine.value({"AAA": Decimal("10")})
        EvaluationStore(sleeve_store.eval_path(name)).record_cycle(
            CycleReport(
                now=CLOSE_UTC,
                strategy=cfg.strategy,
                outcomes=[],
                starting_value=cfg.starting_cash,
                ending_value=valuation.total_value,
                valuation=valuation,
                missing_quotes=[],
            )
        )
        EvaluationStore(sleeve_store.eval_path(name)).record_official_observation(
            OfficialDailyObservation(
                cohort_id=INCIDENT_COHORT,
                run_id=run.run_id,
                sleeve_id=cfg.identity,
                strategy=cfg.strategy,
                strategy_hash=cfg.configuration_hash,
                session_date=INCIDENT_SESSION,
                decision_time=CLOSE_UTC,
                valuation_time=CLOSE_UTC,
                status=ObservationStatus.OFFICIAL,
                total_value=Decimal("10000.00"),
                return_pct=Decimal("0"),
                snapshot_ids={"cohort_snapshot": "snapshot:incident"},
                readiness_ready=True,
            )
        )
    for name in PARTIAL:
        cfg = by_name[name]
        run_store.start_member(run.run_id, cfg.identity, now=CLOSE_UTC)
        run_store.finish_member(
            run.run_id,
            cfg.identity,
            status=MemberRunStatus.DATA_NOT_READY,
            now=CLOSE_UTC,
        )
        EvaluationStore(sleeve_store.eval_path(name)).record_official_observation(
            OfficialDailyObservation(
                cohort_id=INCIDENT_COHORT,
                run_id=run.run_id,
                sleeve_id=cfg.identity,
                strategy=cfg.strategy,
                strategy_hash=cfg.configuration_hash,
                session_date=INCIDENT_SESSION,
                decision_time=CLOSE_UTC,
                valuation_time=CLOSE_UTC,
                status=ObservationStatus.PARTIAL,
                snapshot_ids={"cohort_snapshot": "snapshot:incident"},
                readiness_ready=False,
                # As persisted by the defective code path: the reason twice over.
                readiness_reasons=("daily_bars:stale", "daily_bars:stale"),
            )
        )
    terminal = run_store.finalize(run.run_id, now=CLOSE_UTC)
    assert terminal.status is SleeveRunStatus.PARTIAL
    assert len(terminal.completed_members) == 2
    # Guard the guard: if the fixture ever stops producing real paper state, the
    # immutability assertions below would silently compare empty to empty.
    for name in COMPLETED:
        cfg = by_name[name]
        state = _paper_state(sleeve_store, cfg)
        assert state["positions"], f"{name} must hold a real position"
        assert state["orders"], f"{name} must hold a real fill"
        assert state["cash"] != str(cfg.starting_cash), f"{name} cash must have moved"
        assert EvaluationStore(sleeve_store.eval_path(name)).recent_cycles(), name
    return sleeve_store, run_store, configs, terminal


def _paper_state(sleeve_store, cfg) -> dict[str, object]:
    """Cash, realized P&L, positions, and every order/fill for one sleeve."""
    engine = PaperEngine(
        sleeve_store.paper_path(cfg.name),
        starting_cash=cfg.starting_cash,
        settle_t1=cfg.settlement_t1,
    )
    account = engine.account()
    return {
        "cash": str(account.cash),
        "starting_cash": str(account.starting_cash),
        "realized_pnl": str(account.realized_pnl),
        "positions": [
            {
                "symbol": position.symbol,
                "quantity": position.quantity,
                "avg_cost": str(position.avg_cost),
            }
            for position in engine.positions()
        ],
        "orders": [order.model_dump(mode="json") for order in engine.recent_orders(limit=1000)],
    }


def _fingerprint(run_store, sleeve_store, run_id, configs) -> str:
    """A total serialization of everything the incident session could have touched.

    All eight categories, not five: the run row, every member checkpoint, official
    observations, paper cash, paper positions, paper orders/fills, and evaluation
    cycles. "Unchanged" has to mean unchanged, not "unchanged in the parts we
    happened to look at".
    """
    run = run_store.get(run_id)
    assert run is not None
    observations: dict[str, object] = {}
    cycles: dict[str, object] = {}
    paper: dict[str, object] = {}
    for cfg in configs:
        evaluations = EvaluationStore(sleeve_store.eval_path(cfg.name))
        observations[cfg.name] = [
            item.model_dump(mode="json")
            for item in evaluations.official_observations(limit=1000)
        ]
        cycles[cfg.name] = [
            {
                "ts": str(record.ts),
                "strategy": record.strategy,
                "num_filled": record.num_filled,
                "num_rejected": record.num_rejected,
                "total_value": str(record.total_value),
                "realized_pnl": str(record.realized_pnl),
                "return_pct": str(record.return_pct),
            }
            for record in evaluations.recent_cycles(limit=1000)
        ]
        paper[cfg.name] = _paper_state(sleeve_store, cfg)
    return json.dumps(
        {
            "run": run.model_dump(mode="json"),
            "observations": observations,
            "cycles": cycles,
            "paper": paper,
        },
        sort_keys=True,
        default=str,
    )


def _snapshot(configs, readiness):
    return CohortSnapshot(
        snapshot_id="snapshot:incident",
        quote_snapshot_id="quotes:incident",
        captured_at=CLOSE_UTC,
        quotes={"AAA": _quote("AAA")},
        resources=strategy_registry.StrategyResources(),
        readiness_by_member=readiness,
        data_snapshot_ids={"daily_bars": "bars:incident"},
    )


def _bars(session: date) -> BarBatch:
    return BarBatch(
        provenance=Provenance(
            source="test",
            snapshot_id=f"bars:{session.isoformat()}",
            retrieved_at=CLOSE_UTC,
            as_of=CLOSE_UTC,
            timing=TimingPolicy.SETTLED_EOD,
            vintage_safe=True,
        ),
        bars=(
            BarObservation(
                symbol="AAA",
                session_date=session,
                open=Decimal("10"),
                high=Decimal("10"),
                low=Decimal("10"),
                close=Decimal("10"),
                volume=1,
            ),
        ),
    )


def _orchestrator(tmp_path, sleeve_store, run_store, configs, settled):
    readiness = {
        cfg.name: evaluate_readiness(
            [
                (
                    DataRequirement(
                        kind=DataKind.DAILY_BARS,
                        keys=("AAA",),
                        required_session=INCIDENT_SESSION,
                    ),
                    SourceProbe.of(_bars(settled)),
                )
            ],
            now=CLOSE_UTC,
        )
        for cfg in configs
    }
    return SleeveRunOrchestrator(
        run_store=run_store,
        sleeve_store=sleeve_store,
        kill_switch=KillSwitch(tmp_path / "kill.flag"),
        snapshot_provider=lambda cfgs, session, prior: _snapshot(configs, readiness),
        universe_resolver=lambda cfg: list(cfg.universe),
    )


def test_the_fingerprint_actually_detects_a_paper_mutation(incident):
    """Guard the guard: a fingerprint that cannot fail proves nothing.

    Deliberately mutates paper state the way a replayed run would, and asserts the
    comparison notices. Nothing in the suite does this to the real cohort.
    """
    sleeve_store, run_store, configs, terminal = incident
    before = _fingerprint(run_store, sleeve_store, terminal.run_id, configs)
    cfg = next(c for c in configs if c.name == COMPLETED[0])

    PaperEngine(
        sleeve_store.paper_path(cfg.name),
        starting_cash=cfg.starting_cash,
        settle_t1=cfg.settlement_t1,
    ).place_order(
        OrderRequest(
            side=OrderSide.BUY, symbol="AAA", quantity=1, limit_price=Decimal("12")
        ),
        _quote("AAA"),
        now=CLOSE_UTC,
    )

    assert _fingerprint(run_store, sleeve_store, terminal.run_id, configs) != before


@pytest.mark.parametrize(
    ("now_et", "label"),
    [
        (datetime(2026, 7, 27, 16, 30), "same evening, still due"),
        (datetime(2026, 7, 27, 22, 0), "same evening, late"),
        (datetime(2026, 7, 28, 16, 30), "next session, missed"),
        (datetime(2026, 8, 14, 16, 30), "weeks later"),
    ],
)
@pytest.mark.parametrize("settled", [FRIDAY, INCIDENT_SESSION])
def test_the_terminal_incident_run_is_never_altered_by_the_new_runner(
    tmp_path, incident, now_et, label, settled
):
    """Whatever the clock says and whatever the data now looks like, nothing moves."""
    sleeve_store, run_store, configs, terminal = incident
    before = _fingerprint(run_store, sleeve_store, terminal.run_id, configs)
    orchestrator = _orchestrator(tmp_path, sleeve_store, run_store, configs, settled)

    decision = scheduling.evaluate_session(INCIDENT_COHORT, INCIDENT_SESSION, now_et)
    for _ in range(3):
        result = orchestrator.run(decision, configs, now=CLOSE_UTC)
        assert result.status is SleeveRunStatus.PARTIAL, label

    assert _fingerprint(run_store, sleeve_store, terminal.run_id, configs) == before


def test_a_terminal_partial_can_never_be_downgraded_to_awaiting_data(tmp_path, incident):
    """`awaiting-data` is retryable, but it can never overwrite a finished session."""
    sleeve_store, run_store, configs, terminal = incident
    orchestrator = _orchestrator(tmp_path, sleeve_store, run_store, configs, FRIDAY)

    result = orchestrator.run(
        scheduling.evaluate_session(
            INCIDENT_COHORT, INCIDENT_SESSION, datetime(2026, 7, 27, 16, 30)
        ),
        configs,
        now=CLOSE_UTC,
    )

    assert result.status is SleeveRunStatus.PARTIAL
    assert result.completed_at == terminal.completed_at
    assert not any(error.code == "awaiting_data" for error in result.errors)


def test_the_incident_observations_keep_their_original_content(tmp_path, incident):
    """Including the doubled reason string: the record is not retroactively tidied."""
    sleeve_store, run_store, configs, _terminal = incident
    orchestrator = _orchestrator(tmp_path, sleeve_store, run_store, configs, INCIDENT_SESSION)
    orchestrator.run(
        scheduling.evaluate_session(
            INCIDENT_COHORT, INCIDENT_SESSION, datetime(2026, 7, 28, 16, 30)
        ),
        configs,
        now=CLOSE_UTC,
    )

    for name in PARTIAL:
        observations = EvaluationStore(sleeve_store.eval_path(name)).official_observations()
        assert len(observations) == 1
        assert observations[0].status is ObservationStatus.PARTIAL
        assert observations[0].readiness_reasons == ("daily_bars:stale", "daily_bars:stale")
    for name in COMPLETED:
        observations = EvaluationStore(sleeve_store.eval_path(name)).official_observations()
        assert len(observations) == 1
        assert observations[0].status is ObservationStatus.OFFICIAL


def test_archiving_the_incident_cohort_changes_nothing_it_recorded(tmp_path, incident):
    """Marking July 27 superseded is a label, not a migration.

    The lifecycle registry is a reviewed code constant precisely so that withdrawing a
    cohort cannot write to it. Every consumer of that state — the status lookup, the run
    refusal, the dashboard's default selection, and the assembled view for the cohort
    itself — runs here against the real terminal record, and all eight categories of
    stored evidence are byte-identical afterwards.
    """
    sleeve_store, run_store, configs, terminal = incident
    before = _fingerprint(run_store, sleeve_store, terminal.run_id, configs)

    status = cohort_lifecycle.status_for(INCIDENT_COHORT)
    assert status.lifecycle is cohort_lifecycle.CohortLifecycle.SUPERSEDED
    assert cohort_lifecycle.run_refusal(INCIDENT_COHORT) is not None

    replacement = "paper-first-2026-07-28"
    selection = dashboard.cohort_selection(None, sorted([INCIDENT_COHORT, replacement]))
    assert selection.selected == replacement
    assert [item.cohort_id for item in selection.historical] == [INCIDENT_COHORT]

    # And the record still renders in full when it is the one asked for.
    view = dashboard.assemble_cohort_view(
        selected=INCIDENT_COHORT,
        selection=dashboard.cohort_selection(INCIDENT_COHORT, [INCIDENT_COHORT]),
        configs=list(configs),
        runs=run_store.list(cohort_id=INCIDENT_COHORT),
        observations=[
            item
            for cfg in configs
            for item in EvaluationStore(sleeve_store.eval_path(cfg.name)).official_observations()
        ],
        benchmark="bench-spy",
        now_et=datetime(2026, 8, 12, 9, 28),
    )
    assert view.available is True
    assert view.selection.selected_is_historical is True
    assert view.identity is not None and view.identity.cohort_id == INCIDENT_COHORT
    # The terminal partial is still what the view reports for that session.
    assert view.phase is not None
    assert view.phase.latest_due_run is not None
    assert view.phase.latest_due_run.status == "partial"

    assert _fingerprint(run_store, sleeve_store, terminal.run_id, configs) == before


def test_rolling_over_to_a_third_cohort_leaves_july_27_byte_identical(tmp_path, incident):
    """Issue #84: a challenger starts, and the incident record does not notice.

    The new selection rule ranks active cohorts by their persisted start session. This
    drives that rule through every read path with July 27, July 28, and a newer
    challenger all present — resolved by recency, resolved explicitly, and resolved
    while ambiguous — then re-fingerprints all eight categories of stored evidence.

    A rollover is a reporting decision. It must not supersede, retire, rename, backfill,
    or touch anything, least of all the cohort that is somebody's incident evidence.
    """
    sleeve_store, run_store, configs, terminal = incident
    before = _fingerprint(run_store, sleeve_store, terminal.run_id, configs)

    replacement = "paper-first-2026-07-28"
    challenger = "challenger-five-sleeve-2026-08-17"
    available = sorted([INCIDENT_COHORT, replacement, challenger])
    starts = {
        INCIDENT_COHORT: date(2026, 7, 27),
        replacement: date(2026, 7, 28),
        challenger: date(2026, 8, 17),
    }

    # The newer challenger wins; July 28 is reported as still owed a decision; July 27 is
    # historical and is in neither active list.
    rolled = dashboard.cohort_selection(None, available, start_sessions=starts)
    assert rolled.selected == challenger
    assert rolled.older_active == [replacement]
    assert rolled.multiple_active is True
    assert [item.cohort_id for item in rolled.historical] == [INCIDENT_COHORT]

    # An explicit request still opens the incident cohort in full.
    explicit = dashboard.cohort_selection(INCIDENT_COHORT, available, start_sessions=starts)
    assert explicit.selected == INCIDENT_COHORT
    assert explicit.selected_is_historical is True

    # And the ambiguous path — the one that might tempt an implementation to "repair"
    # the missing metadata it just reported — writes nothing either.
    ambiguous = dashboard.cohort_selection(
        None,
        available,
        start_sessions={**starts, challenger: None},
    )
    assert ambiguous.selected is None
    assert ambiguous.ambiguous is True

    assert cohort_lifecycle.status_for(INCIDENT_COHORT).lifecycle is (
        cohort_lifecycle.CohortLifecycle.SUPERSEDED
    )
    assert _fingerprint(run_store, sleeve_store, terminal.run_id, configs) == before


def test_a_replacement_cohort_shares_no_state_with_the_incident_cohort(tmp_path, incident):
    """A new cohort id is a new experiment: its own runs, its own clean accounts."""
    sleeve_store, run_store, configs, terminal = incident
    replacement = "paper-first-2026-07-29"
    new_session = date(2026, 7, 29)
    new_configs = tuple(
        sleeve_store.create(
            f"{name}-r2",
            strategy="buy-hold",
            universe=["AAA"],
            starting_cash=Decimal("10000.00"),
            max_positions=3,
            max_position_fraction=Decimal("1.0"),
            definition=strategy_registry.make_definition("buy-hold", universe_definition=["AAA"]),
            cohort_id=replacement,
        )
        for name in ALL_MEMBERS
    )
    before = _fingerprint(run_store, sleeve_store, terminal.run_id, configs)

    fresh = run_store.ensure_run(
        cohort_id=replacement,
        session=scheduling.session_for_date(new_session),
        expected_members=[cfg.identity for cfg in new_configs],
        now=CLOSE_UTC,
    )

    assert fresh.run_id != terminal.run_id
    assert fresh.completed_members == ()
    assert len(fresh.expected_members) == 7
    assert run_store.list(cohort_id=replacement) == [fresh]
    assert len(run_store.list(cohort_id=INCIDENT_COHORT)) == 1
    for cfg in new_configs:
        assert EvaluationStore(sleeve_store.eval_path(cfg.name)).official_observations() == []
    assert _fingerprint(run_store, sleeve_store, terminal.run_id, configs) == before
