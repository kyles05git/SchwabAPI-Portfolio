"""End-to-end cohort orchestration for signal-at-T-close, execute-at-T+1-open.

Synthetic sessions, synthetic bars, and disposable stores under ``tmp_path``. Nothing
here reads `.env`, reaches Schwab, Neon, SMTP, or a socket, and no order path exists in
the paper orchestrator at all.

The signal session is Friday 2026-07-24, so the execution session is Monday
2026-07-27 — the weekend is crossed by the canonical calendar rather than by anything
this test asserts about weekday arithmetic.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from schwab_trader import execution_timing as et
from schwab_trader import market_calendar as mc
from schwab_trader import scheduling, strategy_registry
from schwab_trader.agent import AgentRunner, HoldStrategy
from schwab_trader.data_readiness import evaluate_readiness
from schwab_trader.evaluation import EvaluationStore, ObservationStatus
from schwab_trader.market_data import Candle, Quote
from schwab_trader.models import OrderRequest, OrderSide
from schwab_trader.next_open_fill import validate_opening_bar
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

COHORT = "paper-t1-open-2026-07-24"
SIGNAL = date(2026, 7, 24)
EXECUTION = date(2026, 7, 27)

SIGNAL_CLOSE_UTC = datetime(2026, 7, 24, 20, 0, tzinfo=UTC)
EXECUTION_OPEN_UTC = datetime(2026, 7, 27, 13, 30, tzinfo=UTC)
RETRIEVED_UTC = EXECUTION_OPEN_UTC + timedelta(minutes=10)

#: Late enough on the execution session that the signal session's run is LATE but the
#: scheduler's deadline (the execution session's own close) has not passed.
NOW_ET = datetime(2026, 7, 27, 9, 45)
NOW_UTC = datetime(2026, 7, 27, 13, 45, tzinfo=UTC)

CLOSE_PRICE = Decimal("10")
#: A gap down, so a basket sized on the close quote is still affordable at the open.
OPEN_PRICE = Decimal("8")


def _quote(
    symbol: str,
    price: Decimal = CLOSE_PRICE,
    *,
    quote_time: datetime = SIGNAL_CLOSE_UTC,
) -> Quote:
    return Quote(
        symbol=symbol,
        bid=price,
        ask=price,
        last=price,
        mark=price,
        previous_close=price,
        quote_time=quote_time,
    )


def _opening_bar(symbol: str, *, session: date = EXECUTION, price: Decimal = OPEN_PRICE):
    opened = mc.session_bounds_utc(session)[0]
    candle = Candle(
        symbol=symbol,
        date=opened,
        open=price,
        high=price + Decimal("1"),
        low=price - Decimal("1"),
        close=price,
        volume=10_000,
        source="test-intraday",
    )
    retrieved = max(RETRIEVED_UTC, opened + timedelta(minutes=10))
    return validate_opening_bar(symbol, session, [candle], retrieved_at=retrieved).require()


def _create(store, name, *, symbol="AAA", methodology=et.NEXT_OPEN_METHODOLOGY_KEY):
    definition = strategy_registry.make_definition("buy-hold", universe_definition=[symbol])
    return store.create(
        name,
        strategy="buy-hold",
        universe=[symbol],
        starting_cash=Decimal("1000.00"),
        max_positions=3,
        max_position_fraction=Decimal("1.0"),
        definition=definition,
        cohort_id=COHORT,
        execution_methodology=methodology,
    )


def _ready():
    return evaluate_readiness([], now=SIGNAL_CLOSE_UTC)


def _snapshot(
    members,
    *,
    opening_bars=None,
    snapshot_id="snapshot:shared",
    quote_time: datetime = SIGNAL_CLOSE_UTC,
):
    symbols = [symbol for cfg in members for symbol in cfg.universe]
    if opening_bars is None:
        opening_bars = {symbol: _opening_bar(symbol) for symbol in symbols}
    return CohortSnapshot(
        snapshot_id=snapshot_id,
        quote_snapshot_id="quotes:shared",
        captured_at=SIGNAL_CLOSE_UTC,
        quotes={symbol: _quote(symbol, quote_time=quote_time) for symbol in symbols},
        resources=strategy_registry.StrategyResources(),
        readiness_by_member={cfg.name: _ready() for cfg in members},
        data_snapshot_ids={"daily_bars": "bars:shared"},
        opening_bars=opening_bars,
    )


def _harness(tmp_path, members, *, snapshot=None, **kwargs):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    run_store = SleeveRunStore(tmp_path / "runs.sqlite3")
    state = {"snapshot": snapshot if snapshot is not None else _snapshot(members), "calls": []}

    def provider(configs, session, required_snapshot_id):
        state["calls"].append(required_snapshot_id)
        return state["snapshot"]

    orchestrator = SleeveRunOrchestrator(
        run_store=run_store,
        sleeve_store=sleeve_store,
        kill_switch=KillSwitch(tmp_path / "KILL_SWITCH"),
        snapshot_provider=provider,
        universe_resolver=lambda cfg: list(cfg.universe),
        **kwargs,
    )
    return sleeve_store, run_store, orchestrator, state


def _due(now_et: datetime = NOW_ET):
    return scheduling.evaluate_session(COHORT, SIGNAL, now_et)


def _observations(sleeve_store, name):
    return EvaluationStore(sleeve_store.eval_path(name)).official_observations()


def _statuses(run):
    return {member.sleeve_id: member.status for member in run.members}


# --- the happy path -----------------------------------------------------------


def test_the_decision_uses_t_close_and_the_fill_uses_the_t1_open(tmp_path):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    members = (_create(sleeve_store, "one"),)
    _, _, orchestrator, _ = _harness(tmp_path, members)

    run = orchestrator.run(_due(), members, now=NOW_UTC)

    assert run.status is SleeveRunStatus.COMPLETED
    orders = orchestrator.engine_factory(members[0]).recent_orders()
    assert len(orders) == 1
    order = orders[0]
    # Sized on the T close quote (1000 / 10 = 100 shares), filled at the T+1 opening
    # print. Neither number could be produced by the other session's data.
    assert order.quantity == 100
    assert order.status == "FILLED"
    assert order.fill_price is not None
    assert OPEN_PRICE <= order.fill_price < CLOSE_PRICE
    assert order.filled_at == EXECUTION_OPEN_UTC


def test_all_four_timestamps_and_both_sessions_are_persisted(tmp_path):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    members = (_create(sleeve_store, "one"),)
    _, _, orchestrator, state = _harness(tmp_path, members)

    orchestrator.run(_due(), members, now=NOW_UTC)

    observation = _observations(sleeve_store, "one")[0]
    assert observation.status is ObservationStatus.OFFICIAL
    assert observation.execution_methodology == et.NEXT_OPEN_METHODOLOGY_KEY
    # session_date remains the signal session, so the idempotency key and the run key
    # continue to describe the same thing they always did.
    assert observation.session_date == SIGNAL
    assert observation.signal_session_date == SIGNAL
    assert observation.execution_session_date == EXECUTION
    assert observation.signal_time == SIGNAL_CLOSE_UTC
    assert observation.decision_time == SIGNAL_CLOSE_UTC
    assert observation.execution_time == EXECUTION_OPEN_UTC
    assert observation.valuation_time == EXECUTION_OPEN_UTC
    assert observation.decision_time < observation.execution_time
    assert observation.snapshot_ids["execution_methodology"] == (
        et.SIGNAL_T_CLOSE_EXECUTE_T1_OPEN_V1.methodology_hash
    )
    assert observation.snapshot_ids["opening_bar:AAA"] == (
        state["snapshot"].opening_bars["AAA"].evidence_digest
    )
    assert observation.modeled_cost == Decimal("1.00")
    cycle = EvaluationStore(sleeve_store.eval_path("one")).recent_cycles()[0]
    assert cycle.ts == SIGNAL_CLOSE_UTC


def test_the_sleeve_is_marked_at_the_execution_open_not_the_signal_close(tmp_path):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    members = (_create(sleeve_store, "one"),)
    _, _, orchestrator, _ = _harness(tmp_path, members)

    orchestrator.run(_due(), members, now=NOW_UTC)

    observation = _observations(sleeve_store, "one")[0]
    engine = orchestrator.engine_factory(members[0])
    at_open = engine.value({"AAA": OPEN_PRICE}).total_value
    at_close = engine.value({"AAA": CLOSE_PRICE}).total_value
    assert at_open != at_close
    assert observation.total_value == at_open


def test_mark_source_never_leaks_t1_equity_into_the_t_decision_context(tmp_path):
    engine = PaperEngine(tmp_path / "mark-source.sqlite3", starting_cash=Decimal("1000"))
    decision_quote = _quote("AAA")
    engine.place_order(
        OrderRequest(
            side=OrderSide.BUY,
            symbol="AAA",
            quantity=1,
            limit_price=CLOSE_PRICE,
        ),
        decision_quote,
        now=SIGNAL_CLOSE_UTC - timedelta(minutes=1),
    )
    opening_quote = Quote(
        symbol="AAA",
        bid=OPEN_PRICE,
        ask=OPEN_PRICE,
        last=OPEN_PRICE,
        mark=OPEN_PRICE,
        quote_time=EXECUTION_OPEN_UTC,
        trade_time=EXECUTION_OPEN_UTC,
    )

    report = AgentRunner(HoldStrategy(["AAA"]), engine, lambda _: decision_quote).run_cycle(
        now=SIGNAL_CLOSE_UTC,
        mark_source=lambda _: opening_quote,
    )

    assert report.now == SIGNAL_CLOSE_UTC
    assert report.starting_value == Decimal("1000")
    assert report.ending_value == Decimal("998")


def test_the_run_stays_keyed_on_the_signal_session(tmp_path):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    members = (_create(sleeve_store, "one"),)
    _, run_store, orchestrator, _ = _harness(tmp_path, members)

    run = orchestrator.run(_due(), members, now=NOW_UTC)

    assert run.scheduled_for == SIGNAL
    assert run.session_id == "XNYS:2026-07-24"
    assert run.run_key == scheduling.run_key(COHORT, scheduling.session_for_date(SIGNAL))
    assert run_store.completed_run_keys(cohort_id=COHORT) == {run.run_key}
    assert run.data_snapshot_ids["execution_methodology"] == (
        et.SIGNAL_T_CLOSE_EXECUTE_T1_OPEN_V1.methodology_hash
    )
    assert len(run.data_snapshot_ids["opening_bar:AAA"]) == 64


# --- fail-closed on opening evidence ------------------------------------------


def test_absent_opening_evidence_waits_and_mutates_nothing(tmp_path):
    """At the signal session's close the T+1 opening print does not exist yet."""
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    members = (_create(sleeve_store, "one"),)
    _, _, orchestrator, _ = _harness(
        tmp_path, members, snapshot=_snapshot(members, opening_bars={})
    )

    at_signal_close = datetime(2026, 7, 24, 16, 5)
    run = orchestrator.run(_due(at_signal_close), members, now=SIGNAL_CLOSE_UTC)

    assert run.status is SleeveRunStatus.AWAITING_DATA
    assert set(_statuses(run).values()) == {MemberRunStatus.PENDING}
    assert orchestrator.engine_factory(members[0]).recent_orders() == []
    assert orchestrator.engine_factory(members[0]).positions() == []
    assert _observations(sleeve_store, "one") == []
    awaiting = [error for error in run.errors if error.code == "awaiting_data"]
    assert awaiting and awaiting[-1].retryable is True
    assert "opening_bars:missing_keys" in awaiting[-1].reasons


def test_opening_evidence_for_the_wrong_session_is_refused_not_substituted(tmp_path):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    members = (_create(sleeve_store, "one"),)
    stale = {"AAA": _opening_bar("AAA", session=date(2026, 7, 23))}
    _, _, orchestrator, _ = _harness(
        tmp_path, members, snapshot=_snapshot(members, opening_bars=stale)
    )

    run = orchestrator.run(_due(), members, now=NOW_UTC)

    assert run.status is SleeveRunStatus.AWAITING_DATA
    assert orchestrator.engine_factory(members[0]).recent_orders() == []
    awaiting = [error for error in run.errors if error.code == "awaiting_data"]
    assert "opening_bars:session_not_covered" in awaiting[-1].reasons


def test_t1_current_quotes_cannot_replace_the_frozen_signal_snapshot(tmp_path):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    members = (_create(sleeve_store, "one"),)
    snapshot = _snapshot(members, quote_time=EXECUTION_OPEN_UTC)
    _, _, orchestrator, _ = _harness(tmp_path, members, snapshot=snapshot)

    run = orchestrator.run(_due(), members, now=NOW_UTC)

    assert run.status is SleeveRunStatus.AWAITING_DATA
    assert orchestrator.engine_factory(members[0]).recent_orders() == []
    awaiting = [error for error in run.errors if error.code == "awaiting_data"]
    assert "signal_quotes:session_not_covered" in awaiting[-1].reasons


def test_one_member_without_an_opening_bar_stops_the_whole_cohort(tmp_path):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    members = (
        _create(sleeve_store, "one", symbol="AAA"),
        _create(sleeve_store, "two", symbol="BBB"),
    )
    partial = {"AAA": _opening_bar("AAA")}
    _, _, orchestrator, _ = _harness(
        tmp_path, members, snapshot=_snapshot(members, opening_bars=partial)
    )

    run = orchestrator.run(_due(), members, now=NOW_UTC)

    assert run.status is SleeveRunStatus.AWAITING_DATA
    for cfg in members:
        assert orchestrator.engine_factory(cfg).recent_orders() == []
        assert _observations(sleeve_store, cfg.name) == []


def test_late_opening_data_completes_the_same_session_cleanly(tmp_path):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    members = (
        _create(sleeve_store, "one", symbol="AAA"),
        _create(sleeve_store, "two", symbol="BBB"),
    )
    _, _, orchestrator, state = _harness(
        tmp_path, members, snapshot=_snapshot(members, opening_bars={})
    )

    waiting = orchestrator.run(_due(datetime(2026, 7, 24, 16, 5)), members, now=SIGNAL_CLOSE_UTC)
    assert waiting.status is SleeveRunStatus.AWAITING_DATA

    state["snapshot"] = _snapshot(members, snapshot_id="snapshot:with-opens")
    completed = orchestrator.run(_due(), members, now=NOW_UTC)

    assert completed.run_id == waiting.run_id
    assert completed.status is SleeveRunStatus.COMPLETED
    assert set(_statuses(completed).values()) == {MemberRunStatus.COMPLETED}
    assert completed.snapshot_id == "snapshot:with-opens"
    for cfg in members:
        assert len(_observations(sleeve_store, cfg.name)) == 1


def test_there_is_no_fallback_to_the_signal_session_close(tmp_path):
    """A cohort with quotes but no opening bars must never fill at the close.

    This is the substitution the whole methodology exists to prevent, so it is
    asserted directly rather than inferred from the run status.
    """
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    members = (_create(sleeve_store, "one"),)
    snapshot = _snapshot(members, opening_bars={})
    assert snapshot.quotes["AAA"].ask == CLOSE_PRICE  # a usable close price exists
    _, _, orchestrator, _ = _harness(tmp_path, members, snapshot=snapshot)

    run = orchestrator.run(_due(), members, now=NOW_UTC)

    assert run.status is SleeveRunStatus.AWAITING_DATA
    assert orchestrator.engine_factory(members[0]).recent_orders() == []


# --- idempotency, restart, locking --------------------------------------------


def test_repeated_invocation_produces_one_fill_and_one_observation(tmp_path):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    members = (_create(sleeve_store, "one"),)
    _, _, orchestrator, state = _harness(tmp_path, members)

    first = orchestrator.run(_due(), members, now=NOW_UTC)
    second = orchestrator.run(_due(), members, now=NOW_UTC)
    third = orchestrator.run(_due(), members, now=NOW_UTC)

    assert first.run_id == second.run_id == third.run_id
    assert third.status is SleeveRunStatus.COMPLETED
    assert len(orchestrator.engine_factory(members[0]).recent_orders()) == 1
    assert len(_observations(sleeve_store, "one")) == 1
    assert state["calls"] == [None]


def test_restart_after_the_official_write_does_not_duplicate_the_fill(tmp_path, monkeypatch):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    members = (
        _create(sleeve_store, "one", symbol="AAA"),
        _create(sleeve_store, "two", symbol="BBB"),
    )
    _, run_store, orchestrator, _ = _harness(tmp_path, members)
    original = run_store.finish_member
    crashed = False

    def crash_after_official(*args, **kwargs):
        nonlocal crashed
        if kwargs.get("status") is MemberRunStatus.COMPLETED and not crashed:
            crashed = True
            raise KeyboardInterrupt
        return original(*args, **kwargs)

    monkeypatch.setattr(run_store, "finish_member", crash_after_official)
    with pytest.raises(KeyboardInterrupt):
        orchestrator.run(_due(), members, now=NOW_UTC)
    monkeypatch.setattr(run_store, "finish_member", original)

    restarted = SleeveRunOrchestrator(
        run_store=run_store,
        sleeve_store=sleeve_store,
        kill_switch=KillSwitch(tmp_path / "KILL_SWITCH"),
        snapshot_provider=orchestrator.snapshot_provider,
        universe_resolver=lambda cfg: list(cfg.universe),
    )
    run = restarted.run(_due(), members, now=NOW_UTC)

    assert run.status is SleeveRunStatus.COMPLETED
    for cfg in members:
        assert len(restarted.engine_factory(cfg).recent_orders()) == 1
        assert len(_observations(sleeve_store, cfg.name)) == 1


def test_an_interrupted_member_is_reported_not_replayed(tmp_path):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    members = (
        _create(sleeve_store, "one", symbol="AAA"),
        _create(sleeve_store, "two", symbol="BBB"),
    )
    _, run_store, orchestrator, _ = _harness(tmp_path, members)
    run = run_store.ensure_run(
        cohort_id=COHORT,
        session=scheduling.session_for_date(SIGNAL),
        expected_members=tuple(cfg.identity for cfg in members),
        now=SIGNAL_CLOSE_UTC,
    )
    # A previous attempt checkpointed the member and stopped before its observation.
    assert run_store.start_member(run.run_id, members[0].identity, now=SIGNAL_CLOSE_UTC)

    resumed = orchestrator.run(_due(), members, now=NOW_UTC)

    statuses = _statuses(resumed)
    assert statuses[members[0].identity] is MemberRunStatus.INTERRUPTED
    assert orchestrator.engine_factory(members[0]).recent_orders() == []
    assert any(error.code == "ambiguous_interrupted_member" for error in resumed.errors)


def test_a_second_writer_cannot_execute_while_the_session_lease_is_held(tmp_path):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    members = (_create(sleeve_store, "one"),)
    _, run_store, orchestrator, _ = _harness(tmp_path, members)

    with run_store.official_session(COHORT, SIGNAL) as acquired:
        assert acquired is True
        with pytest.raises(Exception, match="another runner owns"):
            orchestrator.run(_due(), members, now=NOW_UTC)

    assert orchestrator.engine_factory(members[0]).recent_orders() == []
    after = orchestrator.run(_due(), members, now=NOW_UTC)
    assert after.status is SleeveRunStatus.COMPLETED
    assert len(orchestrator.engine_factory(members[0]).recent_orders()) == 1


def test_a_partially_executed_run_resolves_instead_of_waiting_again(tmp_path):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    members = (
        _create(sleeve_store, "one", symbol="AAA"),
        _create(sleeve_store, "two", symbol="BBB"),
    )
    _, run_store, orchestrator, state = _harness(tmp_path, members)
    run = run_store.ensure_run(
        cohort_id=COHORT,
        session=scheduling.session_for_date(SIGNAL),
        expected_members=tuple(cfg.identity for cfg in members),
        now=SIGNAL_CLOSE_UTC,
    )
    run_store.start_member(run.run_id, members[0].identity, now=SIGNAL_CLOSE_UTC)
    run_store.finish_member(
        run.run_id, members[0].identity, status=MemberRunStatus.COMPLETED, now=SIGNAL_CLOSE_UTC
    )
    # The remaining member's opening bar is now missing: the all-or-nothing guarantee
    # was already spent, so the session must resolve rather than go back to waiting.
    state["snapshot"] = _snapshot(members, opening_bars={"AAA": _opening_bar("AAA")})

    resolved = orchestrator.run(_due(), members, now=NOW_UTC)

    assert resolved.status is SleeveRunStatus.PARTIAL
    assert _statuses(resolved)[members[1].identity] is MemberRunStatus.DATA_NOT_READY
    assert orchestrator.engine_factory(members[1]).recent_orders() == []


# --- methodology guards -------------------------------------------------------


def test_a_cohort_with_two_methodologies_fails_closed_and_executes_nobody(tmp_path):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    members = (
        _create(sleeve_store, "one", symbol="AAA"),
        _create(sleeve_store, "two", symbol="BBB", methodology=""),
    )
    _, _, orchestrator, state = _harness(tmp_path, members)

    run = orchestrator.run(_due(), members, now=NOW_UTC)

    assert run.status is SleeveRunStatus.FAILED
    assert state["calls"] == []  # the provider is never even reached
    for cfg in members:
        assert orchestrator.engine_factory(cfg).recent_orders() == []
        observation = _observations(sleeve_store, cfg.name)[0]
        assert observation.status is ObservationStatus.MISSING
        assert observation.readiness_reasons == ("execution_timing:methodology_invalid",)
    assert any(error.code == "execution_methodology_invalid" for error in run.errors)


def test_an_unknown_methodology_key_fails_closed(tmp_path):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    members = (_create(sleeve_store, "one", methodology="not-a-real-methodology/v9"),)
    _, _, orchestrator, _ = _harness(tmp_path, members)

    run = orchestrator.run(_due(), members, now=NOW_UTC)

    assert run.status is SleeveRunStatus.FAILED
    assert orchestrator.engine_factory(members[0]).recent_orders() == []


@pytest.mark.parametrize("protected", sorted(et.PROTECTED_LEGACY_COHORTS))
def test_the_frozen_july_cohorts_refuse_the_new_timing(tmp_path, protected):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    definition = strategy_registry.make_definition("buy-hold", universe_definition=["AAA"])
    member = sleeve_store.create(
        "one",
        strategy="buy-hold",
        universe=["AAA"],
        starting_cash=Decimal("1000.00"),
        max_positions=3,
        max_position_fraction=Decimal("1.0"),
        definition=definition,
        cohort_id=protected,
        execution_methodology=et.NEXT_OPEN_METHODOLOGY_KEY,
    )
    members = (member,)
    _, _, orchestrator, state = _harness(tmp_path, members)
    decision = scheduling.evaluate_session(protected, SIGNAL, NOW_ET)

    run = orchestrator.run(decision, members, now=NOW_UTC)

    assert run.status is SleeveRunStatus.FAILED
    assert state["calls"] == []
    assert orchestrator.engine_factory(member).recent_orders() == []


def test_a_commissioned_methodology_is_refused_until_the_engine_models_it(tmp_path):
    """Charging a commission the paper engine cannot book would understate costs."""
    from schwab_trader.next_open_fill import OpeningFillPolicy

    commissioned = et.ExecutionMethodology(
        methodology_id="signal-t-close-execute-t1-open",
        methodology_version="v-commissioned-test",
        timing=et.ExecutionTiming.NEXT_SESSION_OPEN,
        signal_evidence="session-close",
        execution_reference="next-session-open",
        valuation_reference="next-session-open",
        fill_policy=OpeningFillPolicy(commission_per_share=Decimal("0.01")),
    )
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    members = (_create(sleeve_store, "one"),)
    _, _, orchestrator, state = _harness(
        tmp_path, members, methodology_resolver=lambda cfg: commissioned
    )

    run = orchestrator.run(_due(), members, now=NOW_UTC)

    assert run.status is SleeveRunStatus.FAILED
    assert state["calls"] == []
    assert orchestrator.engine_factory(members[0]).recent_orders() == []


# --- the close-marked path is untouched ---------------------------------------


def test_a_close_marked_cohort_ignores_opening_bars_entirely(tmp_path):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    members = (_create(sleeve_store, "one", methodology=""),)
    # No opening bars at all: a close-marked cohort must neither need nor consult them.
    _, _, orchestrator, _ = _harness(
        tmp_path, members, snapshot=_snapshot(members, opening_bars={})
    )

    run = orchestrator.run(_due(), members, now=NOW_UTC)

    assert run.status is SleeveRunStatus.COMPLETED
    order = orchestrator.engine_factory(members[0]).recent_orders()[0]
    assert order.fill_price == CLOSE_PRICE
    observation = _observations(sleeve_store, "one")[0]
    assert observation.execution_methodology == ""
    assert observation.signal_session_date is None
    assert observation.execution_session_date is None
    assert observation.signal_time is None
    assert observation.execution_time is None
    assert observation.decision_time == observation.valuation_time == SIGNAL_CLOSE_UTC


def test_a_gap_up_beyond_the_budget_rejects_rather_than_overspending(tmp_path):
    """Sizing at T close and filling at T+1 open can leave an order unaffordable.

    That is a real property of the methodology, not a defect, and the paper engine
    must refuse rather than let the sleeve spend cash it does not have.
    """
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    members = (_create(sleeve_store, "one"),)
    gap_up = {"AAA": _opening_bar("AAA", price=Decimal("25"))}
    _, _, orchestrator, _ = _harness(
        tmp_path, members, snapshot=_snapshot(members, opening_bars=gap_up)
    )

    run = orchestrator.run(_due(), members, now=NOW_UTC)

    assert run.status is SleeveRunStatus.COMPLETED
    order = orchestrator.engine_factory(members[0]).recent_orders()[0]
    assert order.status == "REJECTED"
    assert order.fill_price is None
    assert orchestrator.engine_factory(members[0]).positions() == []
    assert _observations(sleeve_store, "one")[0].num_filled == 0
