"""Offline integration tests for durable paper-cohort orchestration."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from schwab_trader import cli, scheduling, strategy_registry
from schwab_trader.config import Settings
from schwab_trader.data_contracts import BarBatch, BarObservation, Provenance, TimingPolicy
from schwab_trader.data_readiness import DataKind, DataRequirement, SourceProbe, evaluate_readiness
from schwab_trader.evaluation import EvaluationStore, ObservationStatus
from schwab_trader.market_data import Quote
from schwab_trader.safety import KillSwitch
from schwab_trader.sleeve_runs import (
    CohortSnapshot,
    MemberRunStatus,
    SleeveRunOrchestrator,
    SleeveRunStatus,
    SleeveRunStore,
)
from schwab_trader.sleeves import SleeveStore

COHORT = "cohort-a"
SESSION = date(2026, 7, 20)
NOW_ET = datetime(2026, 7, 20, 16, 0)
NOW_UTC = datetime(2026, 7, 20, 20, 0, tzinfo=UTC)


def _quote(symbol: str, price: str = "10") -> Quote:
    value = Decimal(price)
    return Quote(
        symbol=symbol,
        bid=value,
        ask=value,
        last=value,
        mark=value,
        previous_close=value,
        quote_time=NOW_UTC,
    )


def _create(store, name, *, strategy="buy-hold", symbol="AAA"):
    definition = strategy_registry.make_definition(strategy, universe_definition=[symbol])
    return store.create(
        name,
        strategy=strategy,
        universe=[symbol],
        starting_cash=Decimal("1000.00"),
        max_positions=3,
        max_position_fraction=Decimal("1.0"),
        definition=definition,
        cohort_id=COHORT,
    )


def _ready():
    return evaluate_readiness([], now=NOW_UTC)


def _bar_batch(*symbols: str, session: date) -> BarBatch:
    """A settled end-of-day batch covering ``symbols`` through ``session``."""
    return BarBatch(
        provenance=Provenance(
            source="test-bars",
            snapshot_id=f"bars:{session.isoformat()}",
            retrieved_at=NOW_UTC,
            as_of=NOW_UTC,
            timing=TimingPolicy.SETTLED_EOD,
            vintage_safe=True,
        ),
        bars=tuple(
            BarObservation(
                symbol=symbol,
                session_date=session,
                open=Decimal("10"),
                high=Decimal("10"),
                low=Decimal("10"),
                close=Decimal("10"),
                volume=1_000,
            )
            for symbol in symbols
        ),
    )


def _snapshot(members, *, readiness=None, snapshot_id="snapshot:shared"):
    quotes = {symbol: _quote(symbol) for cfg in members for symbol in cfg.universe}
    return CohortSnapshot(
        snapshot_id=snapshot_id,
        quote_snapshot_id="quotes:shared",
        captured_at=NOW_UTC,
        quotes=quotes,
        resources=strategy_registry.StrategyResources(),
        readiness_by_member=readiness or {cfg.name: _ready() for cfg in members},
        data_snapshot_ids={"daily_bars": "bars:shared"},
    )


def _harness(tmp_path, members, *, snapshot=None, kill_switch=None, **kwargs):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    run_store = SleeveRunStore(tmp_path / "runs.sqlite3")
    calls = []
    captured = snapshot or _snapshot(members)

    def provider(configs, session, required_snapshot_id):
        assert configs == members
        calls.append(required_snapshot_id)
        return captured

    orchestrator = SleeveRunOrchestrator(
        run_store=run_store,
        sleeve_store=sleeve_store,
        kill_switch=kill_switch or KillSwitch(tmp_path / "KILL_SWITCH"),
        snapshot_provider=provider,
        universe_resolver=lambda cfg: list(cfg.universe),
        **kwargs,
    )
    return run_store, orchestrator, calls


def _due():
    return scheduling.evaluate_session(COHORT, SESSION, NOW_ET)


def _member_statuses(run):
    return {member.sleeve_id: member.status for member in run.members}


def test_run_store_uses_scheduling_identity_and_rejects_membership_drift(tmp_path):
    store = SleeveRunStore(tmp_path / "runs.sqlite3")
    session = scheduling.session_for_date(SESSION)
    first = store.ensure_run(
        cohort_id=COHORT,
        session=session,
        expected_members=("one", "two"),
        now=NOW_UTC,
    )
    second = store.ensure_run(
        cohort_id=COHORT,
        session=session,
        expected_members=("one", "two"),
        now=NOW_UTC,
    )
    assert first.run_id == scheduling.run_fingerprint(COHORT, session)
    assert first.run_key == scheduling.run_key(COHORT, session)
    assert second.run_id == first.run_id
    with pytest.raises(Exception, match="different expected members"):
        store.ensure_run(
            cohort_id=COHORT,
            session=session,
            expected_members=("one", "three"),
            now=NOW_UTC,
        )


def test_successful_rerun_is_idempotent_for_observations_and_fills(tmp_path):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    members = (_create(sleeve_store, "one"),)
    run_store, orchestrator, calls = _harness(tmp_path, members)

    first = orchestrator.run(_due(), members, now=NOW_UTC)
    second = orchestrator.run(_due(), members, now=NOW_UTC)

    assert first.status is SleeveRunStatus.COMPLETED
    assert second.run_id == first.run_id
    assert run_store.completed_run_keys(cohort_id=COHORT) == {first.run_key}
    assert calls == [None]
    assert len(EvaluationStore(sleeve_store.eval_path("one")).official_observations()) == 1
    assert len(orchestrator.engine_factory(members[0]).recent_orders()) == 1


def test_all_completed_members_share_snapshot_identity(tmp_path):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    members = (
        _create(sleeve_store, "one", symbol="AAA"),
        _create(sleeve_store, "two", symbol="BBB"),
    )
    _, orchestrator, _ = _harness(tmp_path, members)
    run = orchestrator.run(_due(), members, now=NOW_UTC)

    assert run.status is SleeveRunStatus.COMPLETED
    assert run.quote_snapshot_id == "quotes:shared"
    for cfg in members:
        observations = EvaluationStore(sleeve_store.eval_path(cfg.name)).official_observations()
        assert observations[0].snapshot_ids == {
            "cohort_snapshot": "snapshot:shared",
            "daily_bars": "bars:shared",
            "quotes": "quotes:shared",
        }


def test_one_unready_member_holds_the_whole_cohort_and_executes_nobody(tmp_path):
    """An official session is one unit of evidence: all seven members, or none.

    The previous behaviour let a price-only member complete while a fundamental member
    recorded a terminal PARTIAL observation, permanently burning the session on a
    mixture that cannot be compared. See issue #66.
    """
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    price = _create(sleeve_store, "price", strategy="buy-hold", symbol="AAA")
    other = _create(sleeve_store, "needs-bars", strategy="buy-hold", symbol="BBB")
    # A transient gap: the settled bar for the target session has not appeared yet.
    not_ready = evaluate_readiness(
        [
            (
                DataRequirement(
                    kind=DataKind.DAILY_BARS,
                    keys=("BBB",),
                    required_session=date(2026, 7, 20),
                ),
                SourceProbe.of(_bar_batch("BBB", session=date(2026, 7, 17))),
            )
        ],
        now=NOW_UTC,
    )
    members = (price, other)
    captured = _snapshot(
        members,
        readiness={price.name: _ready(), other.name: not_ready},
    )
    _, orchestrator, _ = _harness(tmp_path, members, snapshot=captured)
    run = orchestrator.run(_due(), members, now=NOW_UTC)

    assert run.status is SleeveRunStatus.AWAITING_DATA
    assert run.completed_at is None, "awaiting-data is not a terminal outcome"
    assert _member_statuses(run) == {
        "price": MemberRunStatus.PENDING,
        "needs-bars": MemberRunStatus.PENDING,
    }
    # The ready member must not have executed: no cycle, no fill, no observation.
    for name in ("price", "needs-bars"):
        assert EvaluationStore(sleeve_store.eval_path(name)).official_observations() == []

    # Repeated scheduler invocations retry cleanly and record nothing new.
    repeated = orchestrator.run(_due(), members, now=NOW_UTC)
    assert repeated.status is SleeveRunStatus.AWAITING_DATA
    assert len([e for e in repeated.errors if e.code == "awaiting_data"]) == 1
    for name in ("price", "needs-bars"):
        assert EvaluationStore(sleeve_store.eval_path(name)).official_observations() == []


def test_a_structural_gap_also_stops_the_ready_member_executing(tmp_path):
    """All-or-nothing holds for the fail-now path too, not just the wait path."""
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    price = _create(sleeve_store, "price", strategy="buy-hold", symbol="AAA")
    fundamental = _create(sleeve_store, "fundamental", strategy="fundamental", symbol="BBB")
    unconfigured = evaluate_readiness(
        [
            (
                DataRequirement(
                    kind=DataKind.FUNDAMENTALS, keys=("BBB",), require_vintage_safe=True
                ),
                SourceProbe.missing(),
            )
        ],
        now=NOW_UTC,
    )
    members = (price, fundamental)
    _, orchestrator, _ = _harness(
        tmp_path,
        members,
        snapshot=_snapshot(
            members, readiness={price.name: _ready(), fundamental.name: unconfigured}
        ),
    )
    run = orchestrator.run(_due(), members, now=NOW_UTC)

    assert run.status is SleeveRunStatus.FAILED
    assert run.completed_members == ()
    assert set(_member_statuses(run).values()) == {MemberRunStatus.DATA_NOT_READY}
    for name in ("price", "fundamental"):
        observations = EvaluationStore(sleeve_store.eval_path(name)).official_observations()
        assert len(observations) == 1
        assert observations[0].status is ObservationStatus.MISSING


def test_cohort_completes_in_full_once_the_awaited_data_arrives(tmp_path):
    """The retry after data lands runs every member against one shared snapshot."""
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    names = ("alpha", "beta")
    members = tuple(_create(sleeve_store, name, symbol="AAA") for name in names)
    behind = evaluate_readiness(
        [
            (
                DataRequirement(
                    kind=DataKind.DAILY_BARS,
                    keys=("AAA",),
                    required_session=date(2026, 7, 20),
                ),
                SourceProbe.of(
                    _bar_batch("AAA", session=date(2026, 7, 17)),
                ),
            )
        ],
        now=NOW_UTC,
    )
    _, orchestrator, _ = _harness(
        tmp_path,
        members,
        snapshot=_snapshot(members, readiness={names[0]: _ready(), names[1]: behind}),
    )
    first = orchestrator.run(_due(), members, now=NOW_UTC)
    assert first.status is SleeveRunStatus.AWAITING_DATA

    # The settled bar arrives. Its identity necessarily differs from the one the
    # awaiting-data verdict declined, so the retry must accept the new snapshot rather
    # than fail against a frozen id.
    orchestrator.snapshot_provider = lambda configs, session, prior: _snapshot(
        members,
        readiness={name: _ready() for name in names},
        snapshot_id="snapshot:settled",
    )
    run = orchestrator.run(_due(), members, now=NOW_UTC)

    assert run.status is SleeveRunStatus.COMPLETED
    assert run.snapshot_id == "snapshot:settled"
    assert set(run.completed_members) == set(names)
    seen = set()
    for name in names:
        observations = EvaluationStore(sleeve_store.eval_path(name)).official_observations()
        assert len(observations) == 1
        assert observations[0].status is ObservationStatus.OFFICIAL
        seen.add(observations[0].snapshot_ids["cohort_snapshot"])
    assert len(seen) == 1, "every member must share one immutable snapshot identity"


def test_member_failure_does_not_corrupt_independent_member_or_persist_secret(tmp_path):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    members = (
        _create(sleeve_store, "independent", symbol="AAA"),
        _create(sleeve_store, "provider-fails", symbol="BBB"),
    )

    def builder(cfg, universe, resources):
        if cfg.name == "provider-fails":
            raise RuntimeError("access_token=must-not-be-persisted")
        return strategy_registry.reconstruct(cfg.definition, universe, resources=resources)

    _, orchestrator, _ = _harness(tmp_path, members, strategy_builder=builder)
    run = orchestrator.run(_due(), members, now=NOW_UTC)

    assert run.status is SleeveRunStatus.PARTIAL
    assert _member_statuses(run)["independent"] is MemberRunStatus.COMPLETED
    assert _member_statuses(run)["provider-fails"] is MemberRunStatus.FAILED
    persisted = run.model_dump_json()
    assert "must-not-be-persisted" not in persisted
    assert "access_token" not in persisted


def test_declared_unavailable_macro_requirement_fails_closed(tmp_path):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    definition = strategy_registry.make_definition("buy-hold", universe_definition=["AAA"])
    payload = definition.model_dump()
    payload["data_requirements"] = ("macro",)
    payload["configuration_hash"] = ""
    guarded = type(definition).model_validate(payload)
    member = sleeve_store.create(
        "macro-guarded",
        strategy="buy-hold",
        universe=["AAA"],
        starting_cash=Decimal("1000.00"),
        max_positions=3,
        max_position_fraction=Decimal("1.0"),
        definition=guarded,
        cohort_id=COHORT,
    )
    macro_unready = evaluate_readiness(
        [(DataRequirement(kind=DataKind.MACRO, keys=("GDP",)), SourceProbe.missing())],
        now=NOW_UTC,
    )
    captured = _snapshot((member,), readiness={member.name: macro_unready})
    _, orchestrator, _ = _harness(tmp_path, (member,), snapshot=captured)

    run = orchestrator.run(_due(), (member,), now=NOW_UTC)

    # An unconfigured capability is structural: waiting until the deadline would never
    # configure it, so this fails immediately with an actionable reason rather than
    # sitting in awaiting-data and eventually reporting the less useful `missed`.
    assert run.status is SleeveRunStatus.FAILED
    assert _member_statuses(run)["macro-guarded"] is MemberRunStatus.DATA_NOT_READY
    assert run.errors[0].capability == "macro"
    assert run.errors[0].retryable is False
    # Nothing executed, so the observation is MISSING, not a PARTIAL implying an attempt.
    observations = EvaluationStore(sleeve_store.eval_path("macro-guarded")).official_observations()
    assert len(observations) == 1
    assert observations[0].status is ObservationStatus.MISSING


def test_cli_snapshot_isolates_sec_failure_from_price_only_member(tmp_path, monkeypatch):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    price = _create(sleeve_store, "price", strategy="buy-hold", symbol="AAA")
    fundamental = _create(
        sleeve_store,
        "fundamental",
        strategy="fundamental",
        symbol="BBB",
    )
    settings = Settings(
        _env_file=None,
        sleeves_dir=tmp_path / "sleeves",
        sec_db_path=tmp_path / "sec.sqlite3",
    )
    monkeypatch.setattr(
        cli.market_data,
        "get_quotes",
        lambda client, symbols: {symbol: _quote(symbol) for symbol in symbols},
    )

    def unavailable_store(path):
        raise OSError("provider unavailable")

    monkeypatch.setattr(cli.sec_store, "SecStore", unavailable_store)
    captured = cli._capture_official_sleeve_snapshot(
        settings,
        (price, fundamental),
        scheduling.session_for_date(SESSION),
        None,
        client=object(),
        cache=object(),
        evidence_store=object(),
        spec=None,
        on_usage=lambda usage: None,
    )

    assert captured.readiness_by_member["price"].ready
    assert not captured.readiness_by_member["fundamental"].ready
    assert captured.resources.store is None


def test_cli_kill_switch_records_missed_without_building_client(tmp_path, monkeypatch):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    member = _create(sleeve_store, "one")
    settings = Settings(
        _env_file=None,
        sleeves_dir=tmp_path / "sleeves",
        history_cache_dir=tmp_path / "history",
        research_db_path=tmp_path / "research.sqlite3",
        usage_db_path=tmp_path / "usage.sqlite3",
        kill_switch_path=tmp_path / "KILL_SWITCH",
    )
    KillSwitch(settings.kill_switch_path).engage("operator halt")
    monkeypatch.setattr(cli.market_calendar, "eastern_now", lambda: NOW_ET)

    def forbidden_client(settings):
        raise AssertionError("client must not be built while kill switch is engaged")

    monkeypatch.setattr(cli, "_build_client", forbidden_client)
    with pytest.raises(cli.typer.Exit):
        cli._run_official_sleeve_cohorts(
            settings,
            sleeve_store,
            [member],
            scheduled_for=SESSION,
        )

    runs = SleeveRunStore(settings.sleeves_dir / "runs.sqlite3").list()
    assert runs[0].status is SleeveRunStatus.MISSED
    assert runs[0].errors[0].code == "kill_switch_engaged"


def test_restart_after_official_write_recovers_without_duplicate_fill(tmp_path, monkeypatch):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    members = (
        _create(sleeve_store, "one", symbol="AAA"),
        _create(sleeve_store, "two", symbol="BBB"),
    )
    run_store, orchestrator, calls = _harness(tmp_path, members)
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
    assert calls == [None, "snapshot:shared"]
    assert len(restarted.engine_factory(members[0]).recent_orders()) == 1
    assert len(EvaluationStore(sleeve_store.eval_path("one")).official_observations()) == 1


def test_ambiguous_interrupted_member_is_reported_not_replayed(tmp_path):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    members = (
        _create(sleeve_store, "one", symbol="AAA"),
        _create(sleeve_store, "two", symbol="BBB"),
    )
    fail_cycle = True

    class InterruptingEvaluationStore(EvaluationStore):
        def record_cycle(self, report):
            nonlocal fail_cycle
            if fail_cycle:
                fail_cycle = False
                raise KeyboardInterrupt
            return super().record_cycle(report)

    def eval_factory(cfg):
        return InterruptingEvaluationStore(sleeve_store.eval_path(cfg.name))

    run_store, orchestrator, _ = _harness(
        tmp_path,
        members,
        evaluation_factory=eval_factory,
    )
    with pytest.raises(KeyboardInterrupt):
        orchestrator.run(_due(), members, now=NOW_UTC)

    restarted = SleeveRunOrchestrator(
        run_store=run_store,
        sleeve_store=sleeve_store,
        kill_switch=KillSwitch(tmp_path / "KILL_SWITCH"),
        snapshot_provider=orchestrator.snapshot_provider,
        universe_resolver=lambda cfg: list(cfg.universe),
    )
    run = restarted.run(_due(), members, now=NOW_UTC)

    assert run.status is SleeveRunStatus.PARTIAL
    assert _member_statuses(run) == {
        "one": MemberRunStatus.INTERRUPTED,
        "two": MemberRunStatus.COMPLETED,
    }
    assert len(restarted.engine_factory(members[0]).recent_orders()) == 1
    assert any(error.code == "ambiguous_interrupted_member" for error in run.errors)


def test_kill_switch_creates_structured_missed_run_before_snapshot_or_fills(tmp_path):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    members = (_create(sleeve_store, "one"),)
    kill_switch = KillSwitch(tmp_path / "KILL_SWITCH")
    kill_switch.engage("operator halt")
    _, orchestrator, calls = _harness(tmp_path, members, kill_switch=kill_switch)

    run = orchestrator.run(_due(), members, now=NOW_UTC)

    assert run.status is SleeveRunStatus.MISSED
    assert calls == []
    assert run.errors[0].code == "kill_switch_engaged"
    assert orchestrator.engine_factory(members[0]).recent_orders() == []


@pytest.mark.parametrize("closed", [date(2026, 7, 18), date(2026, 12, 25)])
def test_closed_weekend_and_holiday_are_persisted_without_snapshot(tmp_path, closed):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    members = (_create(sleeve_store, "one"),)
    _, orchestrator, calls = _harness(tmp_path, members)
    decision = scheduling.evaluate_session(COHORT, closed, datetime.combine(closed, NOW_ET.time()))

    run = orchestrator.run(decision, members, now=NOW_UTC)

    assert run.status is SleeveRunStatus.SKIPPED_CLOSED_SESSION
    assert run.scheduled_for == closed
    assert calls == []
    assert run.errors[0].code == "closed_session"


def test_missed_scheduling_decision_is_structured_and_never_executes(tmp_path):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    members = (_create(sleeve_store, "one"),)
    _, orchestrator, calls = _harness(tmp_path, members)
    decision = scheduling.evaluate_session(
        COHORT,
        SESSION,
        datetime(2026, 7, 21, 16, 1),
    )

    run = orchestrator.run(decision, members, now=NOW_UTC)

    assert run.status is SleeveRunStatus.MISSED
    assert calls == []
    assert run.errors[0].code == "missed_session"


def test_early_close_and_late_run_keep_official_scheduled_session(tmp_path):
    early = date(2025, 12, 24)
    now_et = datetime(2025, 12, 24, 18, 0)
    now_utc = datetime(2025, 12, 24, 23, 0, tzinfo=UTC)
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    members = (_create(sleeve_store, "one"),)
    captured = _snapshot(members)
    captured = CohortSnapshot(
        snapshot_id=captured.snapshot_id,
        quote_snapshot_id=captured.quote_snapshot_id,
        captured_at=now_utc,
        quotes={"AAA": _quote("AAA")},
        resources=captured.resources,
        readiness_by_member=captured.readiness_by_member,
        data_snapshot_ids=captured.data_snapshot_ids,
    )
    run_store = SleeveRunStore(tmp_path / "runs.sqlite3")
    orchestrator = SleeveRunOrchestrator(
        run_store=run_store,
        sleeve_store=sleeve_store,
        kill_switch=KillSwitch(tmp_path / "KILL_SWITCH"),
        snapshot_provider=lambda configs, session, prior: captured,
        universe_resolver=lambda cfg: list(cfg.universe),
    )
    decision = scheduling.evaluate_session(COHORT, early, now_et)
    assert decision.status is scheduling.RunStatus.LATE

    run = orchestrator.run(decision, members, now=now_utc)

    observation = EvaluationStore(sleeve_store.eval_path("one")).official_observations()[0]
    assert run.status is SleeveRunStatus.COMPLETED
    assert run.scheduled_for == early
    assert observation.session_date == early
    assert observation.decision_time == datetime(2025, 12, 24, 18, 0, tzinfo=UTC)
