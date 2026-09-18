"""Offline exercise of the existing official-run recovery path on shared storage.

``tests/test_sleeve_runs.py`` already proves the recovery contract against the local
per-file ``SleeveRunStore``. This module runs the same interruption, restart,
repetition, and concurrency scenarios against the **shared** SQLAlchemy backend —
``SqlAlchemySleeveRunStore``, ``SqlAlchemyPaperEngine``, ``SqlAlchemyEvaluationStore``
— because that is the configuration an official cohort actually runs on, and it is
where the durable uniqueness constraints and the session lease live.

Nothing here is a new state machine. Every assertion is about behavior the
orchestrator already contracts for; the point is to prove that behavior survives a
crash on the backend that stores the real record.

Offline and hermetic: every database is a throwaway SQLite file under ``tmp_path``.
No ``.env``, no shared or remote database, no broker, and no order path.
"""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from schwab_trader import scheduling, strategy_registry
from schwab_trader.data_readiness import evaluate_readiness
from schwab_trader.evaluation import ObservationStatus
from schwab_trader.market_data import Quote
from schwab_trader.safety import KillSwitch
from schwab_trader.sleeve_runs import (
    CohortSnapshot,
    MemberRunStatus,
    SleeveRunOrchestrator,
    SleeveRunStatus,
)
from schwab_trader.storage.database import Database
from schwab_trader.storage.evaluation import SqlAlchemyEvaluationStore
from schwab_trader.storage.paper import SqlAlchemyPaperEngine
from schwab_trader.storage.runs import SqlAlchemySleeveRunStore
from schwab_trader.storage.schema import CohortRun, CohortRunMember, OfficialDailyObservation
from schwab_trader.storage.sleeves import SqlAlchemySleeveStore

COHORT = "official-2026"
SESSION = date(2026, 7, 29)
NOW_ET = datetime(2026, 7, 29, 17, 0)
NOW_UTC = datetime(2026, 7, 29, 21, 0, tzinfo=UTC)


@pytest.fixture
def shared(tmp_path):
    database = Database(
        f"sqlite:///{(tmp_path / 'shared.sqlite3').as_posix()}", create_schema=True
    )
    try:
        yield database
    finally:
        database.dispose()


def _quote(symbol: str) -> Quote:
    value = Decimal("10")
    return Quote(
        symbol=symbol,
        bid=value,
        ask=value,
        last=value,
        mark=value,
        previous_close=value,
        quote_time=NOW_UTC,
    )


def _members(database: Database, *names: str):
    store = SqlAlchemySleeveStore(database)
    return tuple(
        store.create(
            name,
            strategy="buy-hold",
            universe=[symbol],
            starting_cash=Decimal("1000.00"),
            max_positions=3,
            max_position_fraction=Decimal("1.0"),
            definition=strategy_registry.make_definition(
                "buy-hold", universe_definition=[symbol]
            ),
            cohort_id=COHORT,
        )
        for name, symbol in zip(names, ("AAA", "BBB", "CCC"), strict=False)
    )


def _snapshot(configs, *, snapshot_id: str = "snapshot:shared") -> CohortSnapshot:
    return CohortSnapshot(
        snapshot_id=snapshot_id,
        quote_snapshot_id="quotes:shared",
        captured_at=NOW_UTC,
        quotes={symbol: _quote(symbol) for cfg in configs for symbol in cfg.universe},
        resources=strategy_registry.StrategyResources(),
        readiness_by_member={cfg.name: evaluate_readiness([], now=NOW_UTC) for cfg in configs},
        data_snapshot_ids={"daily_bars": "bars:shared"},
    )


def _orchestrator(database: Database, configs, *, run_store=None, **kwargs):
    """An orchestrator wired entirely to the shared SQLAlchemy backend."""
    captured = _snapshot(configs)

    def provider(_configs, _session, _required_snapshot_id):
        return captured

    return SleeveRunOrchestrator(
        run_store=run_store or SqlAlchemySleeveRunStore(database),  # type: ignore[arg-type]
        sleeve_store=SqlAlchemySleeveStore(database),  # type: ignore[arg-type]
        kill_switch=KillSwitch(_kill_path(database)),
        snapshot_provider=provider,
        universe_resolver=lambda cfg: list(cfg.universe),
        engine_factory=lambda cfg: SqlAlchemyPaperEngine(  # type: ignore[arg-type,return-value]
            database,
            cfg.identity,
            starting_cash=cfg.starting_cash,
            settle_t1=cfg.settlement_t1,
            leverage=cfg.leverage,
        ),
        evaluation_factory=kwargs.pop(
            "evaluation_factory",
            lambda cfg: SqlAlchemyEvaluationStore(database, cfg.identity),  # type: ignore[arg-type,return-value]
        ),
        **kwargs,
    )


def _kill_path(database: Database):
    from pathlib import Path

    return Path(str(database.engine.url.database)).parent / "KILL_SWITCH"


def _decision(completed=frozenset()):
    return scheduling.evaluate_session(COHORT, SESSION, NOW_ET, completed)


def _member_statuses(run):
    return {member.sleeve_id: member.status for member in run.members}


def _counts(database: Database) -> dict[str, int]:
    """Durable row counts for the identities that must never duplicate."""
    from sqlalchemy import func, select

    from schwab_trader.storage.schema import PaperFill, PaperOrder

    with database.session() as session:
        return {
            "runs": session.scalar(select(func.count()).select_from(CohortRun)) or 0,
            "members": session.scalar(select(func.count()).select_from(CohortRunMember)) or 0,
            "observations": session.scalar(
                select(func.count()).select_from(OfficialDailyObservation)
            )
            or 0,
            "orders": session.scalar(select(func.count()).select_from(PaperOrder)) or 0,
            "fills": session.scalar(select(func.count()).select_from(PaperFill)) or 0,
        }


def _per_sleeve(database: Database) -> dict[str, tuple[int, int, int]]:
    """``sleeve_id -> (orders, fills, official observations)``.

    Duplication is a *per-member* property, which the aggregate counts cannot express:
    an interrupted member legitimately leaves a real fill behind with no observation,
    so only the per-sleeve view distinguishes "not replayed" from "never ran".
    """
    from sqlalchemy import func, select

    from schwab_trader.storage.schema import PaperFill, PaperOrder

    with database.session() as session:
        orders = dict(
            session.execute(
                select(PaperOrder.sleeve_id, func.count()).group_by(PaperOrder.sleeve_id)
            ).all()
        )
        fills = dict(
            session.execute(
                select(PaperOrder.sleeve_id, func.count())
                .join(PaperFill, PaperFill.paper_order_id == PaperOrder.paper_order_id)
                .group_by(PaperOrder.sleeve_id)
            ).all()
        )
        observations = dict(
            session.execute(
                select(OfficialDailyObservation.sleeve_id, func.count()).group_by(
                    OfficialDailyObservation.sleeve_id
                )
            ).all()
        )
    return {
        sleeve_id: (
            orders.get(sleeve_id, 0),
            fills.get(sleeve_id, 0),
            observations.get(sleeve_id, 0),
        )
        for sleeve_id in set(orders) | set(observations)
    }


# --------------------------------------------------------------------------------
# Baseline: a clean run, and repeating it
# --------------------------------------------------------------------------------


def test_repeated_invocation_converges_on_one_run_identity(shared):
    configs = _members(shared, "one", "two")
    orchestrator = _orchestrator(shared, configs)

    first = orchestrator.run(_decision(), configs, now=NOW_UTC)
    second = orchestrator.run(_decision(), configs, now=NOW_UTC)
    third = orchestrator.run(_decision(), configs, now=NOW_UTC)

    assert first.status is SleeveRunStatus.COMPLETED
    assert second.run_id == first.run_id == third.run_id
    assert second.run_key == first.run_key
    counts = _counts(shared)
    assert counts["runs"] == 1
    assert counts["members"] == 2
    assert counts["observations"] == 2
    assert counts["orders"] == 2
    assert counts["fills"] == 2


def test_a_completed_run_is_never_re_executed(shared):
    configs = _members(shared, "one")
    orchestrator = _orchestrator(shared, configs)
    completed = orchestrator.run(_decision(), configs, now=NOW_UTC)
    before = _counts(shared)

    # Both the durable terminal status and the scheduler's completed-key set must
    # independently stop a second execution.
    orchestrator.run(_decision(), configs, now=NOW_UTC)
    orchestrator.run(_decision(frozenset({completed.run_key})), configs, now=NOW_UTC)

    assert _counts(shared) == before


# --------------------------------------------------------------------------------
# Interruption before anything executed
# --------------------------------------------------------------------------------


def test_interruption_before_any_member_executes_retries_cleanly(shared, monkeypatch):
    """Nothing durable was written, so the retry keeps its all-or-nothing guarantee."""
    configs = _members(shared, "one", "two")
    run_store = SqlAlchemySleeveRunStore(shared)
    orchestrator = _orchestrator(shared, configs, run_store=run_store)

    original = run_store.start_member
    crashed = False

    def crash_before_first_member(*args, **kwargs):
        nonlocal crashed
        if not crashed:
            crashed = True
            raise KeyboardInterrupt
        return original(*args, **kwargs)

    monkeypatch.setattr(run_store, "start_member", crash_before_first_member)
    with pytest.raises(KeyboardInterrupt):
        orchestrator.run(_decision(), configs, now=NOW_UTC)
    monkeypatch.setattr(run_store, "start_member", original)

    interrupted = run_store.get(
        scheduling.run_fingerprint(COHORT, scheduling.session_for_date(SESSION))
    )
    assert interrupted is not None
    assert set(_member_statuses(interrupted).values()) == {MemberRunStatus.PENDING}
    assert _counts(shared)["observations"] == 0

    run = _orchestrator(shared, configs, run_store=run_store).run(
        _decision(), configs, now=NOW_UTC
    )

    assert run.status is SleeveRunStatus.COMPLETED
    assert set(_member_statuses(run).values()) == {MemberRunStatus.COMPLETED}
    counts = _counts(shared)
    assert counts["runs"] == 1
    assert counts["observations"] == 2
    assert counts["fills"] == 2


# --------------------------------------------------------------------------------
# Interruption after a durable official observation
# --------------------------------------------------------------------------------


def test_restart_after_a_durable_observation_does_not_duplicate_it_or_its_fills(
    shared, monkeypatch
):
    configs = _members(shared, "one", "two")
    run_store = SqlAlchemySleeveRunStore(shared)
    orchestrator = _orchestrator(shared, configs, run_store=run_store)

    original = run_store.finish_member
    crashed = False

    def crash_after_official(*args, **kwargs):
        # The member's observation and fills are already committed; the crash lands
        # between that write and the checkpoint that records it as complete.
        nonlocal crashed
        if kwargs.get("status") is MemberRunStatus.COMPLETED and not crashed:
            crashed = True
            raise KeyboardInterrupt
        return original(*args, **kwargs)

    monkeypatch.setattr(run_store, "finish_member", crash_after_official)
    with pytest.raises(KeyboardInterrupt):
        orchestrator.run(_decision(), configs, now=NOW_UTC)
    monkeypatch.setattr(run_store, "finish_member", original)

    mid = _counts(shared)
    assert mid["observations"] == 1
    assert mid["fills"] == 1

    run = _orchestrator(shared, configs, run_store=run_store).run(
        _decision(), configs, now=NOW_UTC
    )

    assert run.status is SleeveRunStatus.COMPLETED
    assert set(_member_statuses(run).values()) == {MemberRunStatus.COMPLETED}
    counts = _counts(shared)
    assert counts["runs"] == 1
    # The recovered member is adopted from its durable observation, not replayed.
    assert counts["observations"] == 2
    assert counts["orders"] == 2
    assert counts["fills"] == 2

    with shared.session() as session:
        from sqlalchemy import select

        statuses = list(session.scalars(select(OfficialDailyObservation.status)))
    assert statuses == [ObservationStatus.OFFICIAL.value] * 2


def test_pending_members_resume_from_their_durable_checkpoints(shared, monkeypatch):
    """Only the member that never ran is executed on the restart."""
    configs = _members(shared, "one", "two", "three")
    run_store = SqlAlchemySleeveRunStore(shared)
    orchestrator = _orchestrator(shared, configs, run_store=run_store)

    original = run_store.finish_member
    completed_count = 0

    def crash_after_two(*args, **kwargs):
        nonlocal completed_count
        if kwargs.get("status") is MemberRunStatus.COMPLETED:
            completed_count += 1
            if completed_count == 2:
                raise KeyboardInterrupt
        return original(*args, **kwargs)

    monkeypatch.setattr(run_store, "finish_member", crash_after_two)
    with pytest.raises(KeyboardInterrupt):
        orchestrator.run(_decision(), configs, now=NOW_UTC)
    monkeypatch.setattr(run_store, "finish_member", original)

    executed_before = _counts(shared)["observations"]
    assert executed_before == 2

    run = _orchestrator(shared, configs, run_store=run_store).run(
        _decision(), configs, now=NOW_UTC
    )

    assert run.status is SleeveRunStatus.COMPLETED
    counts = _counts(shared)
    assert counts["observations"] == 3
    assert counts["fills"] == 3
    # The restart reproduced the run's already-persisted snapshot identity.
    assert run.snapshot_id == "snapshot:shared"


# --------------------------------------------------------------------------------
# Ambiguous interruption
# --------------------------------------------------------------------------------


def test_ambiguous_interrupted_member_is_recorded_not_replayed(shared):
    """A member that started with no observation to prove it finished is not retried.

    Replaying it is the mistake that produces a duplicate paper fill, so the contract
    records INTERRUPTED and lets the run resolve to its truthful PARTIAL state.
    """
    configs = _members(shared, "one", "two")
    fail_once = True

    class InterruptingEvaluationStore(SqlAlchemyEvaluationStore):
        def record_cycle(self, report):
            nonlocal fail_once
            if fail_once:
                fail_once = False
                raise KeyboardInterrupt
            return super().record_cycle(report)

    run_store = SqlAlchemySleeveRunStore(shared)
    orchestrator = _orchestrator(
        shared,
        configs,
        run_store=run_store,
        evaluation_factory=lambda cfg: InterruptingEvaluationStore(shared, cfg.identity),
    )
    with pytest.raises(KeyboardInterrupt):
        orchestrator.run(_decision(), configs, now=NOW_UTC)

    run = _orchestrator(shared, configs, run_store=run_store).run(
        _decision(), configs, now=NOW_UTC
    )

    assert run.status is SleeveRunStatus.PARTIAL
    statuses = _member_statuses(run)
    assert statuses[configs[0].identity] is MemberRunStatus.INTERRUPTED
    assert statuses[configs[1].identity] is MemberRunStatus.COMPLETED
    assert any(error.code == "ambiguous_interrupted_member" for error in run.errors)

    # The interrupted member's paper fill from the first attempt is real and durable.
    # What must never happen is a *second* one, which is exactly what replaying it
    # would produce — so the assertion is per-sleeve, not on the aggregate.
    per_sleeve = _per_sleeve(shared)
    assert per_sleeve[configs[0].identity] == (1, 1, 0)
    assert per_sleeve[configs[1].identity] == (1, 1, 1)
    assert _counts(shared)["observations"] == 1


def test_partial_state_stays_truthful_and_is_never_rewritten_as_completed(shared):
    configs = _members(shared, "one", "two")
    fail_once = True

    class InterruptingEvaluationStore(SqlAlchemyEvaluationStore):
        def record_cycle(self, report):
            nonlocal fail_once
            if fail_once:
                fail_once = False
                raise KeyboardInterrupt
            return super().record_cycle(report)

    run_store = SqlAlchemySleeveRunStore(shared)
    with pytest.raises(KeyboardInterrupt):
        _orchestrator(
            shared,
            configs,
            run_store=run_store,
            evaluation_factory=lambda cfg: InterruptingEvaluationStore(shared, cfg.identity),
        ).run(_decision(), configs, now=NOW_UTC)

    partial = _orchestrator(shared, configs, run_store=run_store).run(
        _decision(), configs, now=NOW_UTC
    )
    assert partial.status is SleeveRunStatus.PARTIAL
    before = _counts(shared)

    # Three more invocations must not promote a partial session to completed, and
    # must not add a single durable row.
    for _ in range(3):
        again = _orchestrator(shared, configs, run_store=run_store).run(
            _decision(), configs, now=NOW_UTC
        )
        assert again.status is SleeveRunStatus.PARTIAL
    assert _counts(shared) == before


def test_a_completed_member_can_never_be_downgraded(shared):
    configs = _members(shared, "one")
    run_store = SqlAlchemySleeveRunStore(shared)
    run = _orchestrator(shared, configs, run_store=run_store).run(
        _decision(), configs, now=NOW_UTC
    )

    with pytest.raises(Exception, match="completed member cannot be downgraded"):
        run_store.finish_member(
            run.run_id,
            configs[0].identity,
            status=MemberRunStatus.FAILED,
            now=NOW_UTC,
        )


# --------------------------------------------------------------------------------
# Concurrency
# --------------------------------------------------------------------------------


def test_only_one_of_two_concurrent_runners_owns_the_official_session(shared):
    _members(shared, "one", "two")
    first = SqlAlchemySleeveRunStore(shared)
    second = SqlAlchemySleeveRunStore(shared)

    with first.official_session(COHORT, SESSION) as owned:
        assert owned is True
        with second.official_session(COHORT, SESSION) as also_owned:
            # SQLite has no advisory lock, so the lease row is the barrier. On
            # PostgreSQL the advisory lock refuses first and this never reaches the row.
            assert also_owned is False

    # Once released, the session can be legitimately acquired again.
    with second.official_session(COHORT, SESSION) as reacquired:
        assert reacquired is True


def test_concurrent_writers_converge_on_one_run_and_one_set_of_observations(shared):
    """Two runners racing the same session must not double-write anything.

    The safety property is convergence, and it holds on both backends — but they get
    there differently, and the loser's *failure mode* differs:

    - **PostgreSQL** (the production backend): ``official_session`` takes
      ``pg_try_advisory_lock`` first, so the loser is told ``False`` before it ever
      reaches the lease row, and the orchestrator returns the existing run.
    - **Shared SQLite**: there is no advisory lock and ``SELECT ... FOR UPDATE`` is a
      no-op, so both runners can see no lease row and both attempt the insert. The
      ``(cohort_id, scheduled_for)`` primary key rejects the loser with an
      ``IntegrityError``.

    That is a rougher error, not an unsafe one: the uniqueness rule is the final
    barrier and it does its job. Shared SQLite with two simultaneous writers is not a
    supported configuration — the runbook designates exactly one scheduler machine —
    so this asserts the invariant that actually matters rather than pinning an
    exception type that is legitimately backend-specific.
    """
    from sqlalchemy.exc import IntegrityError

    from schwab_trader.sleeve_runs import SleeveRunConflictError

    configs = _members(shared, "one", "two")
    barrier = threading.Barrier(2)
    results: list[object] = []
    errors: list[BaseException] = []

    def race() -> None:
        try:
            orchestrator = _orchestrator(shared, configs)
            barrier.wait(timeout=30)
            results.append(orchestrator.run(_decision(), configs, now=NOW_UTC))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=race) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert results, "at least one runner must have owned the session"
    # A loser either returns the existing run, raises the typed conflict, or is
    # rejected by the durable uniqueness rule. It must never execute the session.
    assert all(
        isinstance(error, SleeveRunConflictError | IntegrityError) for error in errors
    ), errors

    counts = _counts(shared)
    assert counts["runs"] == 1
    assert counts["members"] == 2
    assert counts["observations"] == 2
    assert counts["orders"] == 2
    assert counts["fills"] == 2
    assert all(value == (1, 1, 1) for value in _per_sleeve(shared).values())


def test_membership_drift_on_the_same_session_is_refused(shared):
    """One session identity cannot be reused for a different cohort composition."""
    configs = _members(shared, "one", "two")
    store = SqlAlchemySleeveRunStore(shared)
    session = scheduling.session_for_date(SESSION)
    store.ensure_run(
        cohort_id=COHORT,
        session=session,
        expected_members=[cfg.identity for cfg in configs],
        now=NOW_UTC,
    )

    with pytest.raises(Exception, match="different expected members"):
        store.ensure_run(
            cohort_id=COHORT,
            session=session,
            expected_members=[configs[0].identity],
            now=NOW_UTC,
        )


def test_duplicate_official_observations_are_impossible_at_the_database_level(shared):
    """The last barrier: even a buggy writer cannot land two observations."""
    from sqlalchemy.exc import IntegrityError

    configs = _members(shared, "one")
    run = _orchestrator(shared, configs).run(_decision(), configs, now=NOW_UTC)

    with shared.session() as session:
        existing = session.query(OfficialDailyObservation).one()
        duplicate = OfficialDailyObservation(
            observation_key=existing.observation_key,
            cohort_id=existing.cohort_id,
            run_id=run.run_id,
            sleeve_id=existing.sleeve_id,
            strategy=existing.strategy,
            strategy_hash=existing.strategy_hash,
            session_date=existing.session_date,
            decision_time=existing.decision_time,
            valuation_time=existing.valuation_time,
            status=existing.status,
            num_filled=existing.num_filled,
            num_rejected=existing.num_rejected,
            snapshot_ids={},
            readiness_reasons=[],
        )

    with pytest.raises(IntegrityError), shared.session() as session:
        session.add(duplicate)
