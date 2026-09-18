"""The cohort-wide readiness preflight and the ``awaiting-data`` run lifecycle.

Covers the orchestration half of the ``paper-first-2026-07-27`` incident: an official
session must be all-or-nothing, must stay retryable while its data is merely late, and
must become a durable missed result once its scheduling window closes.

Offline and deterministic throughout. The clock is injected, the snapshot provider is a
local stub, and no `.env`, database, broker, or SMTP connection is reachable.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from schwab_trader import (
    cohort_ops,
    cohort_preflight,
    evidence_timing,
    scheduling,
    strategy_registry,
)
from schwab_trader.cohort_alerts import AlertKind, kind_for_run, kind_for_state
from schwab_trader.data_contracts import BarBatch, BarObservation, Provenance, TimingPolicy
from schwab_trader.data_readiness import (
    DataKind,
    DataRequirement,
    ReasonCode,
    SourceProbe,
    evaluate_readiness,
)
from schwab_trader.evaluation import EvaluationStore, ObservationStatus
from schwab_trader.market_data import Quote
from schwab_trader.safety import KillSwitch
from schwab_trader.sleeve_runs import (
    CohortSnapshot,
    MemberRunStatus,
    SleeveRunOrchestrator,
    SleeveRunStatus,
    SleeveRunStore,
    SnapshotMismatchError,
)
from schwab_trader.sleeves import SleeveStore

COHORT = "paper-replacement"
FRIDAY = date(2026, 7, 24)
MONDAY = date(2026, 7, 27)

MONDAY_CLOSE_UTC = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)
MONDAY_EVENING_ET = datetime(2026, 7, 27, 16, 30)
MONDAY_LATE_ET = datetime(2026, 7, 27, 21, 0)
#: Past Tuesday's close, so Monday is superseded and the scheduler calls it missed.
TUESDAY_EVENING_ET = datetime(2026, 7, 28, 16, 30)

MEMBERS = (
    "control-cash",
    "bench-spy",
    "sector-momentum",
    "trend-large",
    "low-vol-large",
    "momentum-large",
    "value-momentum-edgar",
)


def _quote(symbol: str, price: str = "10") -> Quote:
    value = Decimal(price)
    return Quote(
        symbol=symbol,
        bid=value,
        ask=value,
        last=value,
        mark=value,
        previous_close=value,
        quote_time=MONDAY_CLOSE_UTC,
    )


def _bars(session: date, *symbols: str) -> BarBatch:
    return BarBatch(
        provenance=Provenance(
            source="test",
            snapshot_id=f"bars:{session.isoformat()}",
            retrieved_at=MONDAY_CLOSE_UTC,
            as_of=MONDAY_CLOSE_UTC,
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
                volume=1,
            )
            for symbol in symbols
        ),
    )


def _readiness(*, settled: date | None):
    """Ready when ``settled`` reaches Monday; short when it stops at Friday."""
    if settled is None:
        return evaluate_readiness([], now=MONDAY_CLOSE_UTC)
    return evaluate_readiness(
        [
            (
                DataRequirement(kind=DataKind.DAILY_BARS, keys=("AAA",), required_session=MONDAY),
                SourceProbe.of(_bars(settled, "AAA")),
            )
        ],
        now=MONDAY_CLOSE_UTC,
    )


@pytest.fixture
def cohort(tmp_path):
    """Seven immutable members, mirroring the real cohort's shape."""
    store = SleeveStore(tmp_path / "sleeves")
    configs = []
    for name in MEMBERS:
        definition = strategy_registry.make_definition("buy-hold", universe_definition=["AAA"])
        configs.append(
            store.create(
                name,
                strategy="buy-hold",
                universe=["AAA"],
                starting_cash=Decimal("10000.00"),
                max_positions=3,
                max_position_fraction=Decimal("1.0"),
                definition=definition,
                cohort_id=COHORT,
            )
        )
    return store, tuple(configs)


def _digest(prefix: str, payload: object) -> str:
    """Mirror of ``cli._snapshot_digest``: identity is derived from content."""
    encoded = json.dumps(payload, default=str, sort_keys=True, separators=(",", ":"))
    return f"{prefix}:{hashlib.sha256(encoded.encode()).hexdigest()}"


def _snapshot(configs, readiness_by_member, *, quotes=None, bars_id="bars:friday"):
    """A snapshot whose identity is a hash of its contents, as production builds it.

    Using a constant id here is what hid the retry blocker: in production the id moves
    both when the awaited bar lands and when quotes merely tick between polls, so a
    stub with a fixed id asserts the opposite of real behaviour.
    """
    resolved = quotes if quotes is not None else {"AAA": _quote("AAA")}
    quote_id = _digest(
        "quotes",
        {symbol: quote.model_dump(mode="json") for symbol, quote in sorted(resolved.items())},
    )
    data_ids = {"daily_bars": bars_id}
    return CohortSnapshot(
        snapshot_id=_digest(
            "cohort-snapshot", {"quotes": quote_id, "data": dict(sorted(data_ids.items()))}
        ),
        quote_snapshot_id=quote_id,
        captured_at=MONDAY_CLOSE_UTC,
        quotes=resolved,
        resources=strategy_registry.StrategyResources(),
        readiness_by_member=readiness_by_member,
        data_snapshot_ids=data_ids,
    )


def _orchestrator(tmp_path, store, configs, snapshot):
    run_store = SleeveRunStore(tmp_path / "runs.sqlite3")
    return run_store, SleeveRunOrchestrator(
        run_store=run_store,
        sleeve_store=store,
        kill_switch=KillSwitch(tmp_path / "kill.flag"),
        snapshot_provider=lambda cfgs, session, prior: snapshot,
        universe_resolver=lambda cfg: list(cfg.universe),
    )


def _decision(now_et):
    return scheduling.evaluate_session(COHORT, MONDAY, now_et)


def _statuses(run):
    return {member.sleeve_id: member.status for member in run.members}


def _observations(store, configs):
    return {
        cfg.name: EvaluationStore(store.eval_path(cfg.name)).official_observations()
        for cfg in configs
    }


# ---------------------------------------------------------------------------
# The incident scenario
# ---------------------------------------------------------------------------


def test_monday_after_close_with_only_friday_bars_awaits_data_and_runs_nobody(tmp_path, cohort):
    """The exact shape of the incident, with the fixed outcome.

    Two members need no daily history and would previously have completed, stranding
    the session at a terminal `partial (2/7)`.
    """
    store, configs = cohort
    readiness = {
        cfg.name: _readiness(settled=None if index < 2 else FRIDAY)
        for index, cfg in enumerate(configs)
    }
    _run_store, orchestrator = _orchestrator(
        tmp_path, store, configs, _snapshot(configs, readiness)
    )

    run = orchestrator.run(_decision(MONDAY_EVENING_ET), configs, now=MONDAY_CLOSE_UTC)

    assert run.status is SleeveRunStatus.AWAITING_DATA
    assert run.completed_members == ()
    assert run.completed_at is None
    assert set(_statuses(run).values()) == {MemberRunStatus.PENDING}
    # No terminal official observation exists for any member, including the two that
    # were ready. Nothing executed, so nothing is owed and nothing was recorded.
    assert all(obs == [] for obs in _observations(store, configs).values())

    error = next(item for item in run.errors if item.code == "awaiting_data")
    assert error.retryable is True
    assert "daily_bars" in (error.capability or "")
    assert "2026-07-24" in error.message and "2026-07-27" in error.message


def test_later_retry_after_the_settled_bar_appears_completes_all_seven(tmp_path, cohort):
    store, configs = cohort
    behind = {
        cfg.name: _readiness(settled=None if index < 2 else FRIDAY)
        for index, cfg in enumerate(configs)
    }
    _run_store, orchestrator = _orchestrator(tmp_path, store, configs, _snapshot(configs, behind))
    assert (
        orchestrator.run(_decision(MONDAY_EVENING_ET), configs, now=MONDAY_CLOSE_UTC).status
        is SleeveRunStatus.AWAITING_DATA
    )

    caught_up = {
        cfg.name: _readiness(settled=None if index < 2 else MONDAY)
        for index, cfg in enumerate(configs)
    }
    # The settled bar landing changes the daily-bars snapshot id, exactly as it does in
    # production. The retry must accept the new identity, not fail against the frozen one.
    orchestrator.snapshot_provider = lambda cfgs, session, prior: _snapshot(
        configs, caught_up, bars_id="bars:monday"
    )
    run = orchestrator.run(_decision(MONDAY_LATE_ET), configs, now=MONDAY_CLOSE_UTC)

    assert run.status is SleeveRunStatus.COMPLETED
    assert len(run.completed_members) == 7
    assert set(_statuses(run).values()) == {MemberRunStatus.COMPLETED}
    assert run.snapshot_id == _snapshot(configs, caught_up, bars_id="bars:monday").snapshot_id

    snapshots = set()
    for cfg in configs:
        observations = EvaluationStore(store.eval_path(cfg.name)).official_observations()
        assert len(observations) == 1
        assert observations[0].status is ObservationStatus.OFFICIAL
        snapshots.add(observations[0].snapshot_ids["cohort_snapshot"])
    caught_up_id = _snapshot(configs, caught_up, bars_id="bars:monday").snapshot_id
    assert snapshots == {caught_up_id}, "all seven members share one snapshot identity"


def test_data_never_arrives_before_the_deadline_and_becomes_a_durable_missed_result(
    tmp_path, cohort
):
    """Bounded retry: the wait ends, it does not continue forever."""
    store, configs = cohort
    behind = {cfg.name: _readiness(settled=FRIDAY) for cfg in configs}
    _run_store, orchestrator = _orchestrator(tmp_path, store, configs, _snapshot(configs, behind))

    for now_et in (MONDAY_EVENING_ET, MONDAY_LATE_ET):
        assert (
            orchestrator.run(_decision(now_et), configs, now=MONDAY_CLOSE_UTC).status
            is SleeveRunStatus.AWAITING_DATA
        )

    # Past Tuesday's close, the scheduler supersedes Monday.
    expired = orchestrator.run(_decision(TUESDAY_EVENING_ET), configs, now=MONDAY_CLOSE_UTC)

    assert expired.status is SleeveRunStatus.MISSED
    assert expired.completed_at is not None, "the deadline outcome is terminal"
    assert set(_statuses(expired).values()) == {MemberRunStatus.DATA_NOT_READY}
    for cfg in configs:
        observations = EvaluationStore(store.eval_path(cfg.name)).official_observations()
        assert len(observations) == 1
        assert observations[0].status is ObservationStatus.MISSING
        assert observations[0].total_value is None, "no return is ever fabricated"
        assert observations[0].readiness_ready is False

    # It stays put; further invocations neither retry nor duplicate.
    again = orchestrator.run(_decision(TUESDAY_EVENING_ET), configs, now=MONDAY_CLOSE_UTC)
    assert again.status is SleeveRunStatus.MISSED
    for cfg in configs:
        assert len(EvaluationStore(store.eval_path(cfg.name)).official_observations()) == 1


# ---------------------------------------------------------------------------
# Snapshot identity across an awaiting-data retry
# ---------------------------------------------------------------------------


def test_awaiting_data_does_not_freeze_a_snapshot_identity(tmp_path, cohort):
    """A run with nothing executed must not bind itself to the snapshot it refused.

    `set_snapshot` exists to stop a *partially executed* run resuming against different
    data. An awaiting-data run has no paper state bound to it, so persisting the
    identity it declined would make every retry a terminal `snapshot_unavailable`
    failure the moment the awaited bar landed — or the moment a quote merely ticked.
    """
    store, configs = cohort
    behind = {cfg.name: _readiness(settled=FRIDAY) for cfg in configs}
    _run_store, orchestrator = _orchestrator(tmp_path, store, configs, _snapshot(configs, behind))

    run = orchestrator.run(_decision(MONDAY_EVENING_ET), configs, now=MONDAY_CLOSE_UTC)

    assert run.status is SleeveRunStatus.AWAITING_DATA
    assert run.snapshot_id is None, "an unexecuted run must not persist a snapshot identity"
    assert run.data_snapshot_ids == {}


def test_three_polls_with_moving_quotes_stay_awaiting_and_record_one_error(
    tmp_path, cohort
):
    """The documented scheduler polls every 30 minutes; quotes move between polls."""
    store, configs = cohort
    behind = {cfg.name: _readiness(settled=FRIDAY) for cfg in configs}
    run_store, orchestrator = _orchestrator(tmp_path, store, configs, _snapshot(configs, behind))

    seen: set[str] = set()
    for price in ("10", "11", "12"):
        snapshot = _snapshot(configs, behind, quotes={"AAA": _quote("AAA", price)})
        seen.add(snapshot.snapshot_id)
        orchestrator.snapshot_provider = lambda cfgs, session, prior, s=snapshot: s
        run = orchestrator.run(_decision(MONDAY_EVENING_ET), configs, now=MONDAY_CLOSE_UTC)
        assert run.status is SleeveRunStatus.AWAITING_DATA

    assert len(seen) == 3, "the stub must produce a distinct identity per poll"
    assert len([item for item in run.errors if item.code == "awaiting_data"]) == 1
    assert len(run_store.list(cohort_id=COHORT)) == 1
    assert all(obs == [] for obs in _observations(store, configs).values())


def test_a_partially_executed_run_still_must_reproduce_its_snapshot(tmp_path, cohort):
    """The restart protection is unchanged where it actually applies.

    Once any member has executed, paper state is bound to the snapshot it used, so a
    resume against a different identity must still fail closed.
    """
    store, configs = cohort
    ready = {cfg.name: _readiness(settled=MONDAY) for cfg in configs}
    run_store, orchestrator = _orchestrator(tmp_path, store, configs, _snapshot(configs, ready))

    # Execute one member, then leave the rest pending by interrupting the run.
    run = run_store.ensure_run(
        cohort_id=COHORT,
        session=scheduling.session_for_date(MONDAY),
        expected_members=[cfg.identity for cfg in configs],
        now=MONDAY_CLOSE_UTC,
    )
    run_store.set_snapshot(
        run.run_id,
        snapshot_id=_snapshot(configs, ready).snapshot_id,
        quote_snapshot_id=_snapshot(configs, ready).quote_snapshot_id,
        data_snapshot_ids=_snapshot(configs, ready).data_snapshot_ids,
    )
    run_store.start_member(run.run_id, configs[0].identity, now=MONDAY_CLOSE_UTC)
    run_store.finish_member(
        run.run_id,
        configs[0].identity,
        status=MemberRunStatus.COMPLETED,
        now=MONDAY_CLOSE_UTC,
    )

    # A different snapshot identity on the resume must fail the remaining members closed.
    orchestrator.snapshot_provider = lambda cfgs, session, prior: _snapshot(
        configs, ready, bars_id="bars:different"
    )
    resumed = orchestrator.run(_decision(MONDAY_LATE_ET), configs, now=MONDAY_CLOSE_UTC)

    assert resumed.status is SleeveRunStatus.PARTIAL
    assert any(error.code == "snapshot_unavailable" for error in resumed.errors)
    statuses = _statuses(resumed)
    assert statuses[configs[0].identity] is MemberRunStatus.COMPLETED
    assert statuses[configs[1].identity] is MemberRunStatus.FAILED


def test_a_partially_executed_run_resolves_instead_of_going_back_to_waiting(
    tmp_path, cohort
):
    """A crash mid-loop spends the all-or-nothing guarantee; it cannot be reclaimed.

    Such a run must resolve to its durable truth rather than record `awaiting-data` and
    later emit a `missed` run mixing COMPLETED and DATA_NOT_READY members.
    """
    store, configs = cohort
    ready = {cfg.name: _readiness(settled=MONDAY) for cfg in configs}
    snapshot = _snapshot(configs, ready)
    run_store, orchestrator = _orchestrator(tmp_path, store, configs, snapshot)

    run = run_store.ensure_run(
        cohort_id=COHORT,
        session=scheduling.session_for_date(MONDAY),
        expected_members=[cfg.identity for cfg in configs],
        now=MONDAY_CLOSE_UTC,
    )
    run_store.set_snapshot(
        run.run_id,
        snapshot_id=snapshot.snapshot_id,
        quote_snapshot_id=snapshot.quote_snapshot_id,
        data_snapshot_ids=snapshot.data_snapshot_ids,
    )
    run_store.start_member(run.run_id, configs[0].identity, now=MONDAY_CLOSE_UTC)
    run_store.finish_member(
        run.run_id,
        configs[0].identity,
        status=MemberRunStatus.COMPLETED,
        now=MONDAY_CLOSE_UTC,
    )

    # The same snapshot identity, but the data has since gone unready.
    behind = {cfg.name: _readiness(settled=FRIDAY) for cfg in configs}
    orchestrator.snapshot_provider = lambda cfgs, session, prior: _snapshot(configs, behind)
    resolved = orchestrator.run(_decision(MONDAY_EVENING_ET), configs, now=MONDAY_CLOSE_UTC)

    assert resolved.status is SleeveRunStatus.PARTIAL
    assert resolved.completed_at is not None
    statuses = set(_statuses(resolved).values())
    assert statuses == {MemberRunStatus.COMPLETED, MemberRunStatus.DATA_NOT_READY}
    assert not any(error.code == "awaiting_data" for error in resolved.errors)


def test_set_snapshot_allow_replace_semantics(tmp_path):
    """The local store's contract. Mirrored for PostgreSQL in test_storage_postgresql.py.

    Both backends must agree: a run that has executed nothing may adopt a fresh
    identity; without the flag a differing identity always fails closed; an identical
    identity is always accepted.
    """
    run_store = SleeveRunStore(tmp_path / "runs.sqlite3")
    run = run_store.ensure_run(
        cohort_id=COHORT,
        session=scheduling.session_for_date(MONDAY),
        expected_members=["bench-spy"],
        now=MONDAY_CLOSE_UTC,
    )
    run_store.set_snapshot(
        run.run_id,
        snapshot_id="snapshot:first",
        quote_snapshot_id="quotes:first",
        data_snapshot_ids={"daily_bars": "bars:friday"},
    )

    replaced = run_store.set_snapshot(
        run.run_id,
        snapshot_id="snapshot:second",
        quote_snapshot_id="quotes:second",
        data_snapshot_ids={"daily_bars": "bars:monday"},
        allow_replace=True,
    )
    assert replaced.snapshot_id == "snapshot:second"
    assert replaced.data_snapshot_ids == {"daily_bars": "bars:monday"}

    with pytest.raises(SnapshotMismatchError):
        run_store.set_snapshot(
            run.run_id,
            snapshot_id="snapshot:third",
            quote_snapshot_id="quotes:third",
            data_snapshot_ids={"daily_bars": "bars:tuesday"},
        )

    unchanged = run_store.set_snapshot(
        run.run_id,
        snapshot_id="snapshot:second",
        quote_snapshot_id="quotes:second",
        data_snapshot_ids={"daily_bars": "bars:monday"},
    )
    assert unchanged.snapshot_id == "snapshot:second"


def test_the_snapshot_identity_becomes_durable_only_when_the_cohort_executes(
    tmp_path, cohort
):
    store, configs = cohort
    ready = {cfg.name: _readiness(settled=MONDAY) for cfg in configs}
    expected = _snapshot(configs, ready)
    _run_store, orchestrator = _orchestrator(tmp_path, store, configs, expected)

    run = orchestrator.run(_decision(MONDAY_EVENING_ET), configs, now=MONDAY_CLOSE_UTC)

    assert run.status is SleeveRunStatus.COMPLETED
    assert run.snapshot_id == expected.snapshot_id
    assert run.data_snapshot_ids == dict(expected.data_snapshot_ids)


# ---------------------------------------------------------------------------
# Idempotence and concurrency
# ---------------------------------------------------------------------------


def test_repeated_invocations_do_not_duplicate_runs_errors_or_observations(tmp_path, cohort):
    store, configs = cohort
    behind = {cfg.name: _readiness(settled=FRIDAY) for cfg in configs}
    run_store, orchestrator = _orchestrator(tmp_path, store, configs, _snapshot(configs, behind))

    runs = [
        orchestrator.run(_decision(MONDAY_EVENING_ET), configs, now=MONDAY_CLOSE_UTC)
        for _ in range(5)
    ]

    assert {run.run_id for run in runs} == {runs[0].run_id}
    assert len(run_store.list(cohort_id=COHORT)) == 1
    awaiting = [item for item in runs[-1].errors if item.code == "awaiting_data"]
    assert len(awaiting) == 1, "an identical verdict is recorded once, not five times"
    assert all(obs == [] for obs in _observations(store, configs).values())


def test_a_second_writer_is_refused_and_returns_the_same_awaiting_run(tmp_path, cohort):
    """One writer. A concurrent runner cannot start a parallel session."""
    store, configs = cohort
    behind = {cfg.name: _readiness(settled=FRIDAY) for cfg in configs}
    run_store, first = _orchestrator(tmp_path, store, configs, _snapshot(configs, behind))
    second = SleeveRunOrchestrator(
        run_store=run_store,
        sleeve_store=store,
        kill_switch=KillSwitch(tmp_path / "kill.flag"),
        snapshot_provider=lambda cfgs, session, prior: _snapshot(configs, behind),
        universe_resolver=lambda cfg: list(cfg.universe),
    )

    owned = first.run(_decision(MONDAY_EVENING_ET), configs, now=MONDAY_CLOSE_UTC)
    with run_store.official_session(COHORT, MONDAY):
        concurrent = second.run(_decision(MONDAY_EVENING_ET), configs, now=MONDAY_CLOSE_UTC)

    assert concurrent.run_id == owned.run_id
    assert concurrent.status is SleeveRunStatus.AWAITING_DATA
    assert len(run_store.list(cohort_id=COHORT)) == 1


def test_completed_session_is_never_superseded_by_a_later_awaiting_verdict(tmp_path, cohort):
    store, configs = cohort
    ready = {cfg.name: _readiness(settled=MONDAY) for cfg in configs}
    _run_store, orchestrator = _orchestrator(tmp_path, store, configs, _snapshot(configs, ready))
    done = orchestrator.run(_decision(MONDAY_EVENING_ET), configs, now=MONDAY_CLOSE_UTC)
    assert done.status is SleeveRunStatus.COMPLETED

    orchestrator.snapshot_provider = lambda cfgs, session, prior: _snapshot(
        configs, {cfg.name: _readiness(settled=FRIDAY) for cfg in configs}
    )
    again = orchestrator.run(_decision(MONDAY_LATE_ET), configs, now=MONDAY_CLOSE_UTC)

    assert again.status is SleeveRunStatus.COMPLETED
    for cfg in configs:
        assert len(EvaluationStore(store.eval_path(cfg.name)).official_observations()) == 1


# ---------------------------------------------------------------------------
# Calendar behaviour is unchanged (task #62 guarantees)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("session_date", "label"),
    [
        (date(2026, 7, 25), "weekend"),
        (date(2026, 12, 25), "holiday"),
    ],
)
def test_closed_sessions_skip_without_reaching_the_preflight(tmp_path, cohort, session_date, label):
    store, configs = cohort
    behind = {cfg.name: _readiness(settled=FRIDAY) for cfg in configs}
    _run_store, orchestrator = _orchestrator(tmp_path, store, configs, _snapshot(configs, behind))
    decision = scheduling.evaluate_session(
        COHORT, session_date, datetime.combine(session_date, datetime.min.time())
    )

    run = orchestrator.run(decision, configs, now=MONDAY_CLOSE_UTC)

    assert run.status is SleeveRunStatus.SKIPPED_CLOSED_SESSION, label
    assert all(obs == [] for obs in _observations(store, configs).values())


def test_normal_weekday_after_close_with_current_bars_completes(tmp_path, cohort):
    store, configs = cohort
    ready = {cfg.name: _readiness(settled=MONDAY) for cfg in configs}
    _run_store, orchestrator = _orchestrator(tmp_path, store, configs, _snapshot(configs, ready))

    run = orchestrator.run(_decision(MONDAY_EVENING_ET), configs, now=MONDAY_CLOSE_UTC)

    assert run.status is SleeveRunStatus.COMPLETED
    assert len(run.completed_members) == 7


def test_early_close_session_is_due_at_thirteen_hundred_and_completes(tmp_path, cohort):
    """2026-11-27 closes at 13:00 ET; a 13:30 ET run is after the close, not pre-close."""
    early = date(2026, 11, 27)
    store, configs = cohort
    ready = {
        cfg.name: evaluate_readiness(
            [
                (
                    DataRequirement(
                        kind=DataKind.DAILY_BARS, keys=("AAA",), required_session=early
                    ),
                    SourceProbe.of(_bars(early, "AAA")),
                )
            ],
            now=MONDAY_CLOSE_UTC,
        )
        for cfg in configs
    }
    _run_store, orchestrator = _orchestrator(tmp_path, store, configs, _snapshot(configs, ready))
    decision = scheduling.evaluate_session(COHORT, early, datetime(2026, 11, 27, 13, 30))
    assert decision.session.is_early_close
    assert decision.status is scheduling.RunStatus.DUE

    run = orchestrator.run(decision, configs, now=MONDAY_CLOSE_UTC)
    assert run.status is SleeveRunStatus.COMPLETED


# ---------------------------------------------------------------------------
# Diagnostics, health, and alerting
# ---------------------------------------------------------------------------


def test_preflight_reports_kind_reason_target_latest_and_missing_coverage():
    readiness = {
        "trend-large": evaluate_readiness(
            [
                (
                    DataRequirement(
                        kind=DataKind.DAILY_BARS,
                        keys=("AAA", "BBB"),
                        required_session=MONDAY,
                    ),
                    SourceProbe.of(_bars(FRIDAY, "AAA", "BBB")),
                )
            ],
            now=MONDAY_CLOSE_UTC,
        )
    }
    result = cohort_preflight.assess_preflight(
        readiness, session_date=MONDAY, members=("trend-large",)
    )

    assert result.state is cohort_preflight.PreflightState.AWAITING_DATA
    assert result.reason_codes == ("daily_bars:session_not_covered",)
    assert result.latest_session == FRIDAY
    gap = result.gaps[0]
    assert gap.kind == "daily_bars"
    assert gap.reason == ReasonCode.SESSION_NOT_COVERED.value
    assert gap.target_session == MONDAY
    assert gap.latest_session == FRIDAY
    assert gap.uncovered_keys == ("AAA", "BBB")

    payload = cohort_preflight.preflight_payload(result)
    assert payload["schema"] == cohort_preflight.PAYLOAD_SCHEMA
    assert payload["session_date"] == "2026-07-27"
    assert payload["latest_session"] == "2026-07-24"
    assert payload["members"]["unready"] == ["trend-large"]


def test_preflight_reason_codes_are_deduplicated_across_members():
    """Five sleeves short of the same bar is one condition, recorded once."""
    short = evaluate_readiness(
        [
            (
                DataRequirement(kind=DataKind.DAILY_BARS, keys=("AAA",), required_session=MONDAY),
                SourceProbe.of(_bars(FRIDAY, "AAA")),
            )
        ],
        now=MONDAY_CLOSE_UTC,
    )
    members = MEMBERS[2:]
    result = cohort_preflight.assess_preflight(
        {name: short for name in members}, session_date=MONDAY, members=members
    )

    assert result.reason_codes == ("daily_bars:session_not_covered",)
    assert len(result.unready_members) == 5
    assert len(result.diagnostics()) == 5, "per-member detail is still available"


def test_a_member_with_no_readiness_verdict_is_a_gap_not_a_pass():
    result = cohort_preflight.assess_preflight({}, session_date=MONDAY, members=("bench-spy",))
    assert not result.ready
    assert result.reason_codes == ("readiness:not_evaluated",)
    assert result.structural, "a missing verdict is a wiring defect, not a late provider"


@pytest.mark.parametrize(
    ("probe", "reason"),
    [
        (SourceProbe.missing(), "source_missing"),
        (SourceProbe.disabled(), "source_disabled"),
    ],
)
def test_a_capability_that_waiting_cannot_configure_is_structural(probe, reason):
    readiness = evaluate_readiness(
        [
            (
                DataRequirement(
                    kind=DataKind.DAILY_BARS, keys=("AAA",), required_session=MONDAY
                ),
                probe,
            )
        ],
        now=MONDAY_CLOSE_UTC,
    )
    result = cohort_preflight.assess_preflight(
        {"trend-large": readiness}, session_date=MONDAY, members=("trend-large",)
    )

    assert result.state is cohort_preflight.PreflightState.MISCONFIGURED
    assert result.structural
    assert result.reason_codes == (f"daily_bars:{reason}",)
    assert "Waiting cannot resolve this" in result.summary


@pytest.mark.parametrize(
    "probe",
    [
        SourceProbe.of(_bars(FRIDAY, "AAA")),  # settled through the wrong session
        SourceProbe.failed("ConnectTimeout"),  # a provider that may recover
        SourceProbe.of(None),  # published nothing yet
    ],
)
def test_a_gap_that_waiting_can_resolve_stays_awaiting(probe):
    readiness = evaluate_readiness(
        [
            (
                DataRequirement(
                    kind=DataKind.DAILY_BARS, keys=("AAA",), required_session=MONDAY
                ),
                probe,
            )
        ],
        now=MONDAY_CLOSE_UTC,
    )
    result = cohort_preflight.assess_preflight(
        {"trend-large": readiness}, session_date=MONDAY, members=("trend-large",)
    )

    assert result.state is cohort_preflight.PreflightState.AWAITING_DATA
    assert not result.structural


def test_a_structural_gap_fails_the_cohort_immediately_without_waiting(tmp_path, cohort):
    """No member executes, and the operator gets the answer now, not at the deadline."""
    store, configs = cohort
    unconfigured = evaluate_readiness(
        [(DataRequirement(kind=DataKind.MACRO, keys=("GDP",)), SourceProbe.missing())],
        now=MONDAY_CLOSE_UTC,
    )
    readiness = {cfg.name: unconfigured for cfg in configs}
    _run_store, orchestrator = _orchestrator(
        tmp_path, store, configs, _snapshot(configs, readiness)
    )

    run = orchestrator.run(_decision(MONDAY_EVENING_ET), configs, now=MONDAY_CLOSE_UTC)

    assert run.status is SleeveRunStatus.FAILED
    assert run.completed_members == ()
    assert set(_statuses(run).values()) == {MemberRunStatus.DATA_NOT_READY}
    assert any(error.code == "data_misconfigured" for error in run.errors)
    assert all(error.retryable is False for error in run.errors)
    for cfg in configs:
        observations = EvaluationStore(store.eval_path(cfg.name)).official_observations()
        assert len(observations) == 1
        assert observations[0].status is ObservationStatus.MISSING
        assert observations[0].total_value is None


def test_uncovered_quote_symbols_block_the_cohort_too():
    """Quotes are required data; excluding them would leave the same hole open."""
    result = cohort_preflight.assess_preflight(
        {"bench-spy": evaluate_readiness([], now=MONDAY_CLOSE_UTC)},
        session_date=MONDAY,
        members=("bench-spy",),
        quote_gaps={"bench-spy": ("SPY",)},
    )
    assert not result.ready
    assert result.reason_codes == ("quotes:missing_keys",)


def test_preflight_output_carries_no_secret_shaped_content():
    result = cohort_preflight.assess_preflight({}, session_date=MONDAY, members=("bench-spy",))
    rendered = repr(cohort_preflight.preflight_payload(result)) + result.summary
    for forbidden in ("postgres://", "postgresql://", "Bearer", "access_token", "@neon"):
        assert forbidden not in rendered


def test_awaiting_data_is_visible_in_health_but_never_alerts(tmp_path, cohort):
    """It needs an operator's awareness, not an email every thirty minutes."""
    store, configs = cohort
    behind = {cfg.name: _readiness(settled=FRIDAY) for cfg in configs}
    _run_store, orchestrator = _orchestrator(tmp_path, store, configs, _snapshot(configs, behind))
    run = orchestrator.run(_decision(MONDAY_EVENING_ET), configs, now=MONDAY_CLOSE_UTC)

    report = cohort_ops.assess_cohort(
        COHORT,
        now_et=MONDAY_EVENING_ET,
        runs=[run],
        expected_members=MEMBERS,
        cohort_start=MONDAY,
    )

    assert report.state is cohort_ops.CohortState.AWAITING_DATA
    assert report.needs_attention
    assert "retries" in report.next_action
    assert kind_for_run(run, ran_late=False) is None
    assert kind_for_state(cohort_ops.CohortState.AWAITING_DATA) is None


def test_health_reports_missed_once_an_awaiting_session_passes_its_deadline(tmp_path, cohort):
    store, configs = cohort
    behind = {cfg.name: _readiness(settled=FRIDAY) for cfg in configs}
    _run_store, orchestrator = _orchestrator(tmp_path, store, configs, _snapshot(configs, behind))
    run = orchestrator.run(_decision(MONDAY_EVENING_ET), configs, now=MONDAY_CLOSE_UTC)

    report = cohort_ops.assess_cohort(
        COHORT,
        now_et=TUESDAY_EVENING_ET,
        runs=[run],
        expected_members=MEMBERS,
        session_date=MONDAY,
        cohort_start=MONDAY,
    )

    assert report.state is cohort_ops.CohortState.MISSED
    assert kind_for_state(report.state) is AlertKind.MISSED


def test_the_runner_exposes_per_member_diagnostics_for_display(tmp_path, cohort):
    """What the operator actually sees: kind, reason, target, latest, and what is short."""
    store, configs = cohort
    behind = {cfg.name: _readiness(settled=FRIDAY) for cfg in configs}
    _run_store, orchestrator = _orchestrator(tmp_path, store, configs, _snapshot(configs, behind))
    orchestrator.run(_decision(MONDAY_EVENING_ET), configs, now=MONDAY_CLOSE_UTC)

    preflight = orchestrator.last_preflight
    assert preflight is not None
    lines = preflight.diagnostics()
    assert len(lines) == 7
    for line in lines:
        assert "daily_bars:session_not_covered" in line
        assert "target 2026-07-27" in line
        assert "latest 2026-07-24" in line
        assert "short: AAA" in line


def test_awaiting_data_is_judged_by_the_clock_like_an_unexecuted_run(tmp_path, cohort):
    store, configs = cohort
    behind = {cfg.name: _readiness(settled=FRIDAY) for cfg in configs}
    _run_store, orchestrator = _orchestrator(tmp_path, store, configs, _snapshot(configs, behind))
    run = orchestrator.run(_decision(MONDAY_EVENING_ET), configs, now=MONDAY_CLOSE_UTC)

    timing = evidence_timing.classify_run(run, now_et=MONDAY_EVENING_ET)

    assert timing.timing is not evidence_timing.RunTiming.EXECUTED
