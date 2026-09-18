"""A pre-execution Schwab reauthentication failure must stay retryable (issue #109).

On 2026-07-31 the official seven-member paper cohort reached snapshot capture with a
rejected refresh token. Nothing had executed and no snapshot was bound, yet the runner
marked every member FAILED and finalized the session as terminal FAILED — so the
session could never be recorded even after the operator authenticated successfully.

These tests pin the narrow, future-only contract:

* **before** any member starts and **before** a snapshot is bound, a
  :class:`~schwab_trader.auth.ReauthRequiredError` records a non-terminal, retryable
  wait that executes nobody and observes nothing;
* the same cohort/session invocation completes exactly once after authentication;
* everything else — a started member, a bound snapshot, a snapshot mismatch, an
  unclassified failure — still fails closed;
* an unresolved wait that runs out of session still produces the existing truthful
  ``missed`` outcome rather than a fabricated zero return.

Offline and deterministic. No ``.env``, token cache, broker, database, or SMTP
connection is reachable from this module.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from schwab_trader import cohort_ops, dashboard, scheduling, strategy_registry
from schwab_trader.auth import NotAuthenticatedError, ReauthRequiredError
from schwab_trader.data_readiness import evaluate_readiness
from schwab_trader.evaluation import EvaluationStore, ObservationStatus
from schwab_trader.market_data import Quote
from schwab_trader.safety import KillSwitch
from schwab_trader.sleeve_runs import (
    TERMINAL_RUN_STATUSES,
    CohortSnapshot,
    MemberRunStatus,
    SleeveRunError,
    SleeveRunOrchestrator,
    SleeveRunStatus,
    SleeveRunStore,
    SnapshotMismatchError,
    awaits_reauthentication,
)
from schwab_trader.sleeves import SleeveStore
from schwab_trader.storage.database import Database
from schwab_trader.storage.runs import SqlAlchemySleeveRunStore
from schwab_trader.storage.sleeves import SqlAlchemySleeveStore

COHORT = "paper-first-2026-07-28"
SESSION = date(2026, 7, 31)
NOW_ET = datetime(2026, 7, 31, 16, 30)
NOW_UTC = datetime(2026, 7, 31, 20, 30, tzinfo=UTC)

#: The seven official members, so the cohort under test is the one that failed.
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
        quote_time=NOW_UTC,
    )


def _create(store: SleeveStore, name: str):
    return store.create(
        name,
        strategy="buy-hold",
        universe=["AAA"],
        starting_cash=Decimal("10000.00"),
        max_positions=3,
        max_position_fraction=Decimal("1.0"),
        definition=strategy_registry.make_definition("buy-hold", universe_definition=["AAA"]),
        cohort_id=COHORT,
    )


def _snapshot(snapshot_id: str = "snapshot:authenticated") -> CohortSnapshot:
    return CohortSnapshot(
        snapshot_id=snapshot_id,
        quote_snapshot_id=f"quotes:{snapshot_id}",
        captured_at=NOW_UTC,
        quotes={"AAA": _quote("AAA")},
        resources=strategy_registry.StrategyResources(),
        readiness_by_member={name: evaluate_readiness([], now=NOW_UTC) for name in MEMBERS},
        data_snapshot_ids={"daily_bars": "bars:authenticated"},
    )


class FakeSchwabAuth:
    """A token that is rejected until the operator authenticates. No I/O, ever."""

    def __init__(self) -> None:
        self.authenticated = False
        self.captures = 0

    def provider(self, configs, session, prior_snapshot_id) -> CohortSnapshot:
        if not self.authenticated:
            raise ReauthRequiredError(
                "The refresh token was rejected or has expired; re-run 'auth login'."
            )
        self.captures += 1
        return _snapshot()


@pytest.fixture
def cohort(tmp_path):
    sleeve_store = SleeveStore(tmp_path / "sleeves")
    configs = tuple(_create(sleeve_store, name) for name in MEMBERS)
    run_store = SleeveRunStore(tmp_path / "runs.sqlite3")
    return sleeve_store, run_store, configs


def _orchestrator(tmp_path, sleeve_store, run_store, provider):
    return SleeveRunOrchestrator(
        run_store=run_store,
        sleeve_store=sleeve_store,
        kill_switch=KillSwitch(tmp_path / "KILL_SWITCH"),
        snapshot_provider=provider,
        universe_resolver=lambda cfg: list(cfg.universe),
    )


def _due():
    return scheduling.evaluate_session(COHORT, SESSION, NOW_ET)


def _statuses(run):
    return {member.sleeve_id: member.status for member in run.members}


def _observations(sleeve_store, configs):
    return {
        cfg.name: EvaluationStore(sleeve_store.eval_path(cfg.name)).official_observations()
        for cfg in configs
    }


def test_pre_execution_reauth_keeps_the_session_retryable(tmp_path, cohort):
    """The regression. Nothing started, no snapshot bound, so nothing is terminal.

    This is the exact shape of the 2026-07-31 incident: snapshot capture raised
    ``ReauthRequiredError`` on the first attempt of a fresh run.
    """
    sleeve_store, run_store, configs = cohort
    auth = FakeSchwabAuth()
    orchestrator = _orchestrator(tmp_path, sleeve_store, run_store, auth.provider)

    run = orchestrator.run(_due(), configs, now=NOW_UTC)

    assert run.status is not SleeveRunStatus.FAILED
    assert run.status not in TERMINAL_RUN_STATUSES
    assert run.completed_at is None, "an unauthenticated capture is not a finished session"
    assert set(_statuses(run).values()) == {MemberRunStatus.PENDING}
    assert run.completed_members == ()
    assert run.snapshot_id is None
    assert all(items == [] for items in _observations(sleeve_store, configs).values())
    # The operator needs an actionable reason, and it must never carry provider text.
    error = next(item for item in run.errors if item.retryable)
    assert "authenticat" in error.message.lower()
    assert "refresh token" not in error.message.lower()


def test_the_same_session_completes_once_after_authentication(tmp_path, cohort):
    """Requirement 2 and 3: one durable completion, no duplicate anything."""
    sleeve_store, run_store, configs = cohort
    auth = FakeSchwabAuth()
    orchestrator = _orchestrator(tmp_path, sleeve_store, run_store, auth.provider)

    first = orchestrator.run(_due(), configs, now=NOW_UTC)
    assert first.status is not SleeveRunStatus.FAILED

    auth.authenticated = True
    completed = orchestrator.run(_due(), configs, now=NOW_UTC)
    # And a third invocation must not run anything a second time.
    repeated = orchestrator.run(_due(), configs, now=NOW_UTC)

    assert completed.status is SleeveRunStatus.COMPLETED
    assert set(completed.completed_members) == {cfg.identity for cfg in configs}
    assert repeated.run_id == completed.run_id == first.run_id
    assert repeated.completed_at == completed.completed_at
    assert auth.captures == 1, "the authenticated capture must not repeat"
    assert len(run_store.list(cohort_id=COHORT)) == 1
    for cfg in configs:
        evaluations = EvaluationStore(sleeve_store.eval_path(cfg.name))
        observations = evaluations.official_observations()
        assert len(observations) == 1
        assert observations[0].status is ObservationStatus.OFFICIAL
        assert len(evaluations.recent_cycles()) == 1
        assert len(orchestrator.engine_factory(cfg).recent_orders()) == 1


def test_repeated_unauthenticated_polls_record_one_durable_wait(tmp_path, cohort):
    """Requirement 4: the durable transition is recorded once, not once per poll."""
    sleeve_store, run_store, configs = cohort
    auth = FakeSchwabAuth()
    orchestrator = _orchestrator(tmp_path, sleeve_store, run_store, auth.provider)

    for _ in range(4):
        run = orchestrator.run(_due(), configs, now=NOW_UTC)

    assert run.status is not SleeveRunStatus.FAILED
    retryable = [item for item in run.errors if item.retryable]
    assert len(retryable) == 1, "repeated polls must collapse into one durable row"
    assert set(_statuses(run).values()) == {MemberRunStatus.PENDING}


def test_a_started_member_still_fails_closed_on_reauth(tmp_path, cohort):
    """Requirement 6: paper state may already be bound, so waiting is not safe."""
    sleeve_store, run_store, configs = cohort
    auth = FakeSchwabAuth()
    auth.authenticated = True
    orchestrator = _orchestrator(tmp_path, sleeve_store, run_store, auth.provider)

    # Execute the cohort once so the run carries a bound snapshot, then rebuild the
    # session as an interrupted attempt: one member completed, the rest pending.
    run = run_store.ensure_run(
        cohort_id=COHORT,
        session=scheduling.session_for_date(SESSION),
        expected_members=[cfg.identity for cfg in configs],
        now=NOW_UTC,
    )
    run_store.set_snapshot(
        run.run_id,
        snapshot_id="snapshot:bound",
        quote_snapshot_id="quotes:bound",
        data_snapshot_ids={"daily_bars": "bars:bound"},
    )
    run_store.start_member(run.run_id, configs[0].identity, now=NOW_UTC)
    run_store.finish_member(
        run.run_id, configs[0].identity, status=MemberRunStatus.COMPLETED, now=NOW_UTC
    )
    auth.authenticated = False

    resolved = orchestrator.run(_due(), configs, now=NOW_UTC)

    assert resolved.status in {SleeveRunStatus.FAILED, SleeveRunStatus.PARTIAL}
    assert resolved.completed_at is not None, "a run with executed members is terminal"


def test_a_snapshot_mismatch_still_fails_closed(tmp_path, cohort):
    """Requirement 6: a restart that cannot reproduce its snapshot never waits."""
    sleeve_store, run_store, configs = cohort
    orchestrator = _orchestrator(
        tmp_path, sleeve_store, run_store, lambda *_: _snapshot("snapshot:different")
    )
    run = run_store.ensure_run(
        cohort_id=COHORT,
        session=scheduling.session_for_date(SESSION),
        expected_members=[cfg.identity for cfg in configs],
        now=NOW_UTC,
    )
    run_store.set_snapshot(
        run.run_id,
        snapshot_id="snapshot:bound",
        quote_snapshot_id="quotes:bound",
        data_snapshot_ids={"daily_bars": "bars:bound"},
    )
    run_store.start_member(run.run_id, configs[0].identity, now=NOW_UTC)
    run_store.finish_member(
        run.run_id, configs[0].identity, status=MemberRunStatus.COMPLETED, now=NOW_UTC
    )

    resolved = orchestrator.run(_due(), configs, now=NOW_UTC)

    assert resolved.completed_at is not None
    assert resolved.status in {SleeveRunStatus.FAILED, SleeveRunStatus.PARTIAL}
    assert any(item.code == "snapshot_unavailable" for item in resolved.errors)


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("the provider is broken"),
        NotAuthenticatedError("no stored tokens"),
        SnapshotMismatchError("identity drift"),
        ValueError("structural"),
    ],
)
def test_unclassified_snapshot_failures_still_fail_closed(tmp_path, cohort, failure):
    """Requirement 7: only an explicit reauthentication is retryable pre-execution."""
    sleeve_store, run_store, configs = cohort

    def provider(configs_, session, prior):
        raise failure

    orchestrator = _orchestrator(tmp_path, sleeve_store, run_store, provider)
    run = orchestrator.run(_due(), configs, now=NOW_UTC)

    assert run.status is SleeveRunStatus.FAILED
    assert run.completed_at is not None
    assert set(_statuses(run).values()) == {MemberRunStatus.FAILED}


def test_an_unresolved_wait_expires_into_the_truthful_missed_outcome(tmp_path, cohort):
    """Requirement 5: the deadline still wins, and it never fabricates a zero return."""
    sleeve_store, run_store, configs = cohort
    auth = FakeSchwabAuth()
    orchestrator = _orchestrator(tmp_path, sleeve_store, run_store, auth.provider)

    waiting = orchestrator.run(_due(), configs, now=NOW_UTC)
    assert waiting.status is not SleeveRunStatus.FAILED

    # The next session's decision time supersedes this one; nobody authenticated.
    expired = scheduling.evaluate_session(COHORT, SESSION, datetime(2026, 8, 3, 16, 30))
    assert expired.status is scheduling.RunStatus.MISSED
    missed = orchestrator.run(expired, configs, now=datetime(2026, 8, 3, 20, 30, tzinfo=UTC))

    assert missed.status is SleeveRunStatus.MISSED
    assert missed.completed_at is not None
    for cfg in configs:
        observations = EvaluationStore(sleeve_store.eval_path(cfg.name)).official_observations()
        assert len(observations) == 1
        assert observations[0].status is ObservationStatus.MISSING
        assert observations[0].total_value is None, "a missed session has no return"


# --- Presentation: never mistakable for a terminal failure ---------------------


def test_health_gives_the_one_instruction_that_clears_the_wait(tmp_path, cohort):
    """Requirement 10, health. The session is retryable, and it needs a human."""
    sleeve_store, run_store, configs = cohort
    auth = FakeSchwabAuth()
    orchestrator = _orchestrator(tmp_path, sleeve_store, run_store, auth.provider)
    run = orchestrator.run(_due(), configs, now=NOW_UTC)

    report = cohort_ops.assess_cohort(
        COHORT,
        now_et=NOW_ET,
        runs=[run],
        expected_members=[cfg.identity for cfg in configs],
        session_date=SESSION,
    )

    assert report.state is not cohort_ops.CohortState.FAILED
    assert report.state is cohort_ops.CohortState.AWAITING_DATA
    assert report.run is not None and report.run.awaiting_authentication
    assert "auth login" in report.next_action
    assert "Nothing to do yet" not in report.next_action
    payload = cohort_ops.health_payload(report)
    assert payload["state"] == "awaiting-data"
    codes = [error["code"] for error in payload["run"]["errors"]]
    assert codes == ["awaiting_reauthentication"]


def test_a_provider_wait_keeps_its_quiet_instruction(tmp_path, cohort):
    """The unchanged path: waiting on data still says wait, not "authenticate"."""
    sleeve_store, run_store, configs = cohort
    unready = _snapshot("snapshot:early")
    not_ready = evaluate_readiness([], now=NOW_UTC)
    orchestrator = _orchestrator(
        tmp_path,
        sleeve_store,
        run_store,
        lambda *_: CohortSnapshot(
            snapshot_id=unready.snapshot_id,
            quote_snapshot_id=unready.quote_snapshot_id,
            captured_at=NOW_UTC,
            quotes={},  # no quote for AAA: every member's universe is short
            resources=unready.resources,
            readiness_by_member={name: not_ready for name in MEMBERS},
            data_snapshot_ids=unready.data_snapshot_ids,
        ),
    )
    run = orchestrator.run(_due(), configs, now=NOW_UTC)
    assert run.status is SleeveRunStatus.AWAITING_DATA
    assert not awaits_reauthentication(run)

    report = cohort_ops.assess_cohort(
        COHORT,
        now_et=NOW_ET,
        runs=[run],
        expected_members=[cfg.identity for cfg in configs],
        session_date=SESSION,
    )
    assert "Nothing to do yet" in report.next_action
    assert "auth login" not in report.next_action


def test_the_dashboard_shows_a_retryable_run_not_a_failed_one(tmp_path, cohort):
    """Requirement 10, dashboard. The run payload keeps the distinguishing code."""
    sleeve_store, run_store, configs = cohort
    auth = FakeSchwabAuth()
    orchestrator = _orchestrator(tmp_path, sleeve_store, run_store, auth.provider)
    run = orchestrator.run(_due(), configs, now=NOW_UTC)

    view = dashboard.assemble_cohort_view(
        selected=COHORT,
        selection=dashboard.cohort_selection(COHORT, [COHORT]),
        configs=list(configs),
        runs=[run],
        observations=[],
        benchmark="bench-spy",
        now_et=NOW_ET,
        cohort_start=SESSION,
    )

    rendered = next(item for item in view.run_health.runs if item.run_id == run.run_id)
    assert rendered.status == "awaiting-data"
    assert rendered.completed_at is None
    assert [error.code for error in rendered.errors] == ["awaiting_reauthentication"]
    # The panel reads the last wait row, exactly as the frontend component does.
    assert rendered.errors[-1].code == "awaiting_reauthentication"


def test_no_persisted_or_presented_field_carries_a_secret(tmp_path, cohort):
    """The sanitization rule, against an exception that is full of things to leak."""
    sleeve_store, run_store, configs = cohort
    leaky = ReauthRequiredError(
        "refresh_token=SECRET-TOKEN rejected at "
        "https://api.schwabapi.com/v1/oauth/token?code=SECRET-CODE "
        "for account 12345678"
    )

    def provider(configs_, session, prior):
        raise leaky

    orchestrator = _orchestrator(tmp_path, sleeve_store, run_store, provider)
    run = orchestrator.run(_due(), configs, now=NOW_UTC)

    report = cohort_ops.assess_cohort(
        COHORT,
        now_et=NOW_ET,
        runs=[run],
        expected_members=[cfg.identity for cfg in configs],
        session_date=SESSION,
    )
    surfaces = (
        run.model_dump_json(),
        str(cohort_ops.health_payload(report)),
        report.next_action,
    )
    for surface in surfaces:
        for secret in (
            "SECRET-TOKEN",
            "SECRET-CODE",
            "refresh_token",
            "12345678",
            "api.schwabapi.com",
        ):
            assert secret not in surface
    # The sanitized type name is kept, because that is what the operator can act on.
    assert "ReauthRequiredError" in run.model_dump_json()


# --- Storage parity ------------------------------------------------------------


@pytest.fixture
def shared_database(tmp_path):
    database = Database(
        f"sqlite:///{(tmp_path / 'shared.sqlite3').as_posix()}", create_schema=True
    )
    try:
        yield database
    finally:
        database.dispose()


def _shared_members(database: Database):
    """Members registered in the shared database, as its run store's foreign key needs."""
    store = SqlAlchemySleeveStore(database)
    return tuple(
        store.create(
            name,
            strategy="buy-hold",
            universe=["AAA"],
            starting_cash=Decimal("10000.00"),
            max_positions=3,
            max_position_fraction=Decimal("1.0"),
            definition=strategy_registry.make_definition(
                "buy-hold", universe_definition=["AAA"]
            ),
            cohort_id=COHORT,
        )
        for name in MEMBERS
    )


def test_the_shared_store_records_the_same_lifecycle_as_the_local_store(
    tmp_path, cohort, shared_database
):
    """Requirement 8: the backend the writer machine actually runs must agree.

    Drives the same orchestrator twice — once over SQLite, once over the SQLAlchemy
    store — and compares the durable lifecycle, not an assertion about it. Paper and
    evaluation writes stay on local paths in both arms; the run lifecycle is what is
    under comparison.
    """
    sleeve_store, local_store, configs = cohort
    shared_store = SqlAlchemySleeveRunStore(shared_database)

    outcomes = {}
    for label, store, sleeves_root, members in (
        ("local", local_store, sleeve_store, configs),
        (
            "shared",
            shared_store,
            SleeveStore(tmp_path / "sleeves-shared"),
            _shared_members(shared_database),
        ),
    ):
        auth = FakeSchwabAuth()
        orchestrator = _orchestrator(tmp_path, sleeves_root, store, auth.provider)

        waiting = orchestrator.run(_due(), members, now=NOW_UTC)
        # A second unauthenticated poll must not add a row in either backend.
        waiting = orchestrator.run(_due(), members, now=NOW_UTC)
        auth.authenticated = True
        completed = orchestrator.run(_due(), members, now=NOW_UTC)

        outcomes[label] = {
            "waiting_status": waiting.status,
            "waiting_completed_at_is_none": waiting.completed_at is None,
            "waiting_codes": [error.code for error in waiting.errors],
            "waiting_members": sorted({member.status for member in waiting.members}),
            "awaits_auth": awaits_reauthentication(waiting),
            "final_status": completed.status,
            "final_member_count": len(completed.completed_members),
            "final_snapshot": completed.snapshot_id,
            "captures": auth.captures,
        }

    assert outcomes["local"] == outcomes["shared"]
    assert outcomes["local"]["waiting_status"] is SleeveRunStatus.AWAITING_DATA
    assert outcomes["local"]["waiting_codes"] == ["awaiting_reauthentication"]
    assert outcomes["local"]["awaits_auth"] is True
    assert outcomes["local"]["final_status"] is SleeveRunStatus.COMPLETED
    assert outcomes["local"]["captures"] == 1


def test_both_stores_order_the_error_trail_by_when_a_verdict_was_last_reached(
    tmp_path, cohort, shared_database
):
    """The ordering every "current wait" reader depends on, proved in both backends."""
    _sleeve_store, local_store, configs = cohort
    shared_members = _shared_members(shared_database)
    shared_store = SqlAlchemySleeveRunStore(shared_database)

    reauth = SleeveRunError(code="awaiting_reauthentication", message="login", retryable=True)
    data = SleeveRunError(code="awaiting_data", message="bars", retryable=True)

    trails = []
    for store, members in ((local_store, configs), (shared_store, shared_members)):
        run = store.ensure_run(
            cohort_id=COHORT,
            session=scheduling.session_for_date(SESSION),
            expected_members=[cfg.identity for cfg in members],
            now=NOW_UTC,
        )
        for error in (reauth, data, reauth):
            latest = store.set_status(
                run.run_id, SleeveRunStatus.AWAITING_DATA, error=error, now=NOW_UTC
            )
        trails.append([item.code for item in latest.errors])

    assert trails[0] == trails[1]
    assert trails[0] == ["awaiting_data", "awaiting_reauthentication"], (
        "one row per distinct verdict, most recently reached last"
    )
