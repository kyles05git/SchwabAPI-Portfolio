"""The 2026-07-31 terminal failure is evidence, and evidence does not change.

``paper-first-2026-07-28 / 2026-07-31`` recorded a terminal ``failed (0/7 members)``
after snapshot capture met a rejected Schwab refresh token. That record is internally
consistent — zero completed members, no bound snapshot, no official observations,
resumption forbidden — and it is the operational evidence for issue #109. The fix is
future-only: it must not repair, reclassify, retry, backfill, or delete this session,
and it must not disturb the completed sessions recorded around it.

This module reconstructs that run in an offline store alongside a genuinely completed
session, drives the *fixed* runner against both the way a scheduler would — including
with a provider that now authenticates successfully, which is exactly the condition
that would tempt a retry — and proves nothing moved: not a run row, not a member
checkpoint, not an official observation, not a paper account.

Offline and deterministic. No ``.env``, token cache, database, broker, or SMTP
connection is reachable from this module.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from schwab_trader import cohort_ops, scheduling, strategy_registry
from schwab_trader.agent import CycleReport
from schwab_trader.data_readiness import evaluate_readiness
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
    TERMINAL_RUN_STATUSES,
    CohortSnapshot,
    MemberRunStatus,
    SleeveRunError,
    SleeveRunOrchestrator,
    SleeveRunStatus,
    SleeveRunStore,
    awaits_reauthentication,
)
from schwab_trader.sleeves import SleeveStore

COHORT = "paper-first-2026-07-28"
#: The session that completed normally, two days before the failure.
GOOD_SESSION = date(2026, 7, 30)
#: The session that met the rejected refresh token.
FAILED_SESSION = date(2026, 7, 31)

GOOD_UTC = datetime(2026, 7, 30, 20, 0, tzinfo=UTC)
FAILED_UTC = datetime(2026, 7, 31, 20, 30, tzinfo=UTC)

MEMBERS = (
    "control-cash",
    "bench-spy",
    "sector-momentum",
    "trend-large",
    "low-vol-large",
    "momentum-large",
    "value-momentum-edgar",
)


def _quote(symbol: str) -> Quote:
    value = Decimal("10")
    return Quote(
        symbol=symbol,
        bid=value,
        ask=value,
        last=value,
        mark=value,
        previous_close=value,
        quote_time=GOOD_UTC,
    )


def _snapshot(snapshot_id: str, captured_at: datetime) -> CohortSnapshot:
    return CohortSnapshot(
        snapshot_id=snapshot_id,
        quote_snapshot_id=f"quotes:{snapshot_id}",
        captured_at=captured_at,
        quotes={"AAA": _quote("AAA")},
        resources=strategy_registry.StrategyResources(),
        readiness_by_member={name: evaluate_readiness([], now=captured_at) for name in MEMBERS},
        data_snapshot_ids={"daily_bars": f"bars:{snapshot_id}"},
    )


@pytest.fixture
def incident(tmp_path):
    """Rebuild the completed July 30 session and the terminal July 31 failure."""
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    configs = tuple(
        sleeve_store.create(
            name,
            strategy="buy-hold",
            universe=["AAA"],
            starting_cash=Decimal("10000.00"),
            max_positions=3,
            max_position_fraction=Decimal("1.0"),
            definition=strategy_registry.make_definition("buy-hold", universe_definition=["AAA"]),
            cohort_id=COHORT,
        )
        for name in MEMBERS
    )
    run_store = SleeveRunStore(tmp_path / "runs.sqlite3")

    # --- July 30: a real, complete session, with real paper state behind it. ---
    good = run_store.ensure_run(
        cohort_id=COHORT,
        session=scheduling.session_for_date(GOOD_SESSION),
        expected_members=[cfg.identity for cfg in configs],
        now=GOOD_UTC,
    )
    run_store.set_snapshot(
        good.run_id,
        snapshot_id="snapshot:july-30",
        quote_snapshot_id="quotes:july-30",
        data_snapshot_ids={"daily_bars": "bars:july-30"},
    )
    for cfg in configs:
        run_store.start_member(good.run_id, cfg.identity, now=GOOD_UTC)
        engine = PaperEngine(
            sleeve_store.paper_path(cfg.name),
            starting_cash=cfg.starting_cash,
            settle_t1=cfg.settlement_t1,
        )
        order = engine.place_order(
            OrderRequest(
                side=OrderSide.BUY, symbol="AAA", quantity=10, limit_price=Decimal("12")
            ),
            _quote("AAA"),
            now=GOOD_UTC,
        )
        assert order.status == "FILLED", "the fixture must actually change paper state"
        valuation = engine.value({"AAA": Decimal("10")})
        evaluations = EvaluationStore(sleeve_store.eval_path(cfg.name))
        evaluations.record_cycle(
            CycleReport(
                now=GOOD_UTC,
                strategy=cfg.strategy,
                outcomes=[],
                starting_value=cfg.starting_cash,
                ending_value=valuation.total_value,
                valuation=valuation,
                missing_quotes=[],
            )
        )
        evaluations.record_official_observation(
            OfficialDailyObservation(
                cohort_id=COHORT,
                run_id=good.run_id,
                sleeve_id=cfg.identity,
                strategy=cfg.strategy,
                strategy_hash=cfg.configuration_hash,
                session_date=GOOD_SESSION,
                decision_time=GOOD_UTC,
                valuation_time=GOOD_UTC,
                status=ObservationStatus.OFFICIAL,
                total_value=valuation.total_value,
                return_pct=Decimal("0"),
                snapshot_ids={"cohort_snapshot": "snapshot:july-30"},
                readiness_ready=True,
            )
        )
        run_store.finish_member(
            good.run_id, cfg.identity, status=MemberRunStatus.COMPLETED, now=GOOD_UTC
        )
    completed = run_store.finalize(good.run_id, now=GOOD_UTC)
    assert completed.status is SleeveRunStatus.COMPLETED

    # --- July 31: exactly as the defective path persisted it. ---
    failed = run_store.ensure_run(
        cohort_id=COHORT,
        session=scheduling.session_for_date(FAILED_SESSION),
        expected_members=[cfg.identity for cfg in configs],
        now=FAILED_UTC,
    )
    run_store.set_status(failed.run_id, SleeveRunStatus.RUNNING, now=FAILED_UTC)
    error = SleeveRunError(
        code="snapshot_unavailable",
        message="ReauthRequiredError: cohort snapshot could not be captured safely.",
        retryable=True,
    )
    for cfg in configs:
        run_store.finish_member(
            failed.run_id,
            cfg.identity,
            status=MemberRunStatus.FAILED,
            error=error.model_copy(update={"member_id": cfg.identity}),
            now=FAILED_UTC,
        )
    terminal = run_store.finalize(failed.run_id, now=FAILED_UTC)

    # Guard the fixture: this must be the record the incident describes, or the
    # immutability assertions below prove nothing.
    assert terminal.status is SleeveRunStatus.FAILED
    assert terminal.completed_members == ()
    assert terminal.snapshot_id is None
    assert terminal.completed_at is not None
    for cfg in configs:
        observations = EvaluationStore(sleeve_store.eval_path(cfg.name)).official_observations()
        assert [item.session_date for item in observations] == [GOOD_SESSION]
    return sleeve_store, run_store, configs, terminal


def _paper_state(sleeve_store, cfg) -> dict[str, object]:
    engine = PaperEngine(
        sleeve_store.paper_path(cfg.name),
        starting_cash=cfg.starting_cash,
        settle_t1=cfg.settlement_t1,
    )
    account = engine.account()
    return {
        "cash": str(account.cash),
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


def _fingerprint(run_store, sleeve_store, configs) -> str:
    """A total serialization of everything either session could have touched.

    Both runs, every member checkpoint, every official observation, every evaluation
    cycle, and every paper account. "Unchanged" has to mean unchanged, not "unchanged
    in the parts we happened to look at".
    """
    observations: dict[str, object] = {}
    cycles: dict[str, object] = {}
    paper: dict[str, object] = {}
    for cfg in configs:
        evaluations = EvaluationStore(sleeve_store.eval_path(cfg.name))
        observations[cfg.name] = [
            item.model_dump(mode="json") for item in evaluations.official_observations(limit=1000)
        ]
        cycles[cfg.name] = [
            {
                "ts": str(record.ts),
                "strategy": record.strategy,
                "num_filled": record.num_filled,
                "total_value": str(record.total_value),
                "return_pct": str(record.return_pct),
            }
            for record in evaluations.recent_cycles(limit=1000)
        ]
        paper[cfg.name] = _paper_state(sleeve_store, cfg)
    return json.dumps(
        {
            "runs": [run.model_dump(mode="json") for run in run_store.list(cohort_id=COHORT)],
            "observations": observations,
            "cycles": cycles,
            "paper": paper,
        },
        sort_keys=True,
        default=str,
    )


def _orchestrator(tmp_path, sleeve_store, run_store, *, authenticated: bool = True):
    """The fixed runner, with a provider that authenticates exactly as it now would."""

    def provider(configs, session, prior_snapshot_id):
        if not authenticated:
            from schwab_trader.auth import ReauthRequiredError

            raise ReauthRequiredError("the refresh token was rejected")
        captured = _snapshot(f"snapshot:{session.session_date.isoformat()}", FAILED_UTC)
        return captured

    return SleeveRunOrchestrator(
        run_store=run_store,
        sleeve_store=sleeve_store,
        kill_switch=KillSwitch(tmp_path / "KILL_SWITCH"),
        snapshot_provider=provider,
        universe_resolver=lambda cfg: list(cfg.universe),
    )


def test_the_fingerprint_actually_detects_a_mutation(incident):
    """Guard the guard: a fingerprint that cannot fail proves nothing."""
    sleeve_store, run_store, configs, _terminal = incident
    before = _fingerprint(run_store, sleeve_store, configs)

    PaperEngine(
        sleeve_store.paper_path(configs[0].name),
        starting_cash=configs[0].starting_cash,
        settle_t1=configs[0].settlement_t1,
    ).place_order(
        OrderRequest(side=OrderSide.BUY, symbol="AAA", quantity=1, limit_price=Decimal("12")),
        _quote("AAA"),
        now=FAILED_UTC,
    )

    assert _fingerprint(run_store, sleeve_store, configs) != before


@pytest.mark.parametrize(
    ("now_et", "label"),
    [
        (datetime(2026, 7, 31, 16, 30), "same evening, still due"),
        (datetime(2026, 7, 31, 22, 0), "same evening, past grace"),
        (datetime(2026, 8, 3, 16, 30), "next session, missed"),
        (datetime(2026, 9, 15, 16, 30), "weeks later"),
    ],
)
def test_the_terminal_failed_run_is_never_retried_by_the_fixed_runner(
    tmp_path, incident, now_et, label
):
    """The load-bearing test for issue #109's "future sessions only" boundary.

    The provider now authenticates, which is precisely the condition under which a
    careless fix would "helpfully" re-run the failed session. It must not.
    """
    sleeve_store, run_store, configs, terminal = incident
    before = _fingerprint(run_store, sleeve_store, configs)
    orchestrator = _orchestrator(tmp_path, sleeve_store, run_store)

    decision = scheduling.evaluate_session(COHORT, FAILED_SESSION, now_et)
    for _ in range(3):
        result = orchestrator.run(decision, configs, now=FAILED_UTC)
        assert result.status is SleeveRunStatus.FAILED, label
        assert result.status in TERMINAL_RUN_STATUSES, label
        assert result.completed_at == terminal.completed_at, label
        assert result.completed_members == (), label

    assert _fingerprint(run_store, sleeve_store, configs) == before, label


def test_the_terminal_failure_is_never_downgraded_to_a_retryable_wait(tmp_path, incident):
    """The new non-terminal state must never be written over a finished session."""
    sleeve_store, run_store, configs, _terminal = incident
    orchestrator = _orchestrator(tmp_path, sleeve_store, run_store, authenticated=False)

    result = orchestrator.run(
        scheduling.evaluate_session(COHORT, FAILED_SESSION, datetime(2026, 7, 31, 16, 30)),
        configs,
        now=FAILED_UTC,
    )

    assert result.status is SleeveRunStatus.FAILED
    assert not awaits_reauthentication(result)
    assert not any(error.code == "awaiting_reauthentication" for error in result.errors)


def test_the_completed_session_beside_it_is_untouched(tmp_path, incident):
    """A fix aimed at July 31 must not disturb the sessions recorded around it."""
    sleeve_store, run_store, configs, _terminal = incident
    before = _fingerprint(run_store, sleeve_store, configs)
    orchestrator = _orchestrator(tmp_path, sleeve_store, run_store)

    for session in (GOOD_SESSION, FAILED_SESSION):
        orchestrator.run(
            scheduling.evaluate_session(COHORT, session, datetime(2026, 8, 3, 16, 30)),
            configs,
            now=FAILED_UTC,
        )

    good = next(
        run for run in run_store.list(cohort_id=COHORT) if run.scheduled_for == GOOD_SESSION
    )
    assert good.status is SleeveRunStatus.COMPLETED
    assert len(good.completed_members) == 7
    for cfg in configs:
        observations = EvaluationStore(sleeve_store.eval_path(cfg.name)).official_observations()
        assert [item.session_date for item in observations] == [GOOD_SESSION]
        assert observations[0].status is ObservationStatus.OFFICIAL
    assert _fingerprint(run_store, sleeve_store, configs) == before


def test_health_still_reports_the_failed_session_as_terminal(incident):
    """The presentation change must not soften a record that really is terminal."""
    sleeve_store, run_store, configs, terminal = incident

    report = cohort_ops.assess_cohort(
        COHORT,
        now_et=datetime(2026, 7, 31, 22, 0),
        runs=run_store.list(cohort_id=COHORT),
        observations=[
            item
            for cfg in configs
            for item in EvaluationStore(sleeve_store.eval_path(cfg.name)).official_observations()
        ],
        expected_members=[cfg.identity for cfg in configs],
        session_date=FAILED_SESSION,
    )

    assert report.state is cohort_ops.CohortState.FAILED
    assert report.run is not None
    assert report.run.awaiting_authentication is False
    assert "will not be retried automatically" in report.next_action
    assert terminal.run_id == report.run.run_id
