"""Offline tests for the non-mutating cohort recovery inspector.

Fixtures are built through the real ``SqlAlchemySleeveStore`` and
``SqlAlchemySleeveRunStore`` rather than hand-written rows, so what the inspector
reads is exactly what the orchestrator writes. Every database is a throwaway SQLite
file under ``tmp_path``; nothing here reaches ``.env``, Neon, a broker, or a network.

The load-bearing assertion in this module is :func:`test_inspection_never_mutates`:
the inspector's whole value depends on being safe to run against a database an
operator is worried about.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from schwab_trader import scheduling
from schwab_trader.cli import app
from schwab_trader.sleeve_runs import MemberRunStatus, SleeveRunError, SleeveRunStatus
from schwab_trader.storage import factory as storage_factory
from schwab_trader.storage import recovery
from schwab_trader.storage.database import Database
from schwab_trader.storage.runs import SqlAlchemySleeveRunStore
from schwab_trader.storage.schema import OfficialDailyObservation, OfficialSessionLease
from schwab_trader.storage.sleeves import SqlAlchemySleeveStore

COHORT = "official-2026"
SESSION = date(2026, 7, 29)
NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)

#: Eastern wall-clock instants that place ``SESSION`` at each scheduling verdict.
#: The session closes at 16:00 ET with a two-hour grace period, and is superseded
#: when the next session's decision time arrives.
BEFORE_DUE = datetime(2026, 7, 29, 12, 0)
WITHIN_GRACE = datetime(2026, 7, 29, 17, 0)
PAST_GRACE = datetime(2026, 7, 29, 20, 0)
PAST_DEADLINE = datetime(2026, 7, 31, 9, 0)


@pytest.fixture
def shared(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'shared.sqlite3').as_posix()}", create_schema=True)
    try:
        yield database
    finally:
        database.dispose()


def _members(database: Database, *names: str):
    store = SqlAlchemySleeveStore(database)
    return tuple(
        store.create(
            name,
            strategy="buy-hold",
            universe=["SPY"],
            starting_cash=Decimal("10000.00"),
            max_positions=1,
            max_position_fraction=Decimal("1"),
            cohort_id=COHORT,
        )
        for name in names
    )


def _run(database: Database, *names: str):
    configs = _members(database, *names)
    store = SqlAlchemySleeveRunStore(database)
    run = store.ensure_run(
        cohort_id=COHORT,
        session=scheduling.session_for_date(SESSION),
        expected_members=[config.identity for config in configs],
        now=NOW,
    )
    return store, run, configs


def _inspect(database: Database, *, now_et: datetime = PAST_GRACE, now: datetime = NOW):
    return recovery.inspect_recovery(database, COHORT, SESSION, now=now, now_et=now_et)


def _lease(database: Database, *, acquired_ago: timedelta, expires_in: timedelta, released: bool):
    with database.session() as session:
        session.add(
            OfficialSessionLease(
                cohort_id=COHORT,
                scheduled_for=SESSION,
                owner_id="machine-deadbeefcafe:process-4242",
                lease_token_hash=b"\x11" * 32,
                acquired_at=NOW - acquired_ago,
                expires_at=NOW + expires_in,
                released_at=NOW if released else None,
            )
        )


def _observation(database: Database, run_id: str, sleeve_id: str, *, status: str = "official"):
    with database.session() as session:
        session.add(
            OfficialDailyObservation(
                observation_key=f"{COHORT}:{sleeve_id}:{SESSION.isoformat()}",
                cohort_id=COHORT,
                run_id=run_id,
                sleeve_id=sleeve_id,
                strategy="buy-hold",
                strategy_hash="0" * 64,
                session_date=SESSION,
                decision_time=NOW,
                valuation_time=NOW,
                status=status,
                num_filled=1,
                num_rejected=0,
                snapshot_ids={},
                readiness_reasons=[],
                recorded_at=NOW,
            )
        )


# --------------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------------


def test_completed_run_is_completed_and_resumption_is_forbidden(shared):
    store, run, configs = _run(shared, "one", "two")
    for config in configs:
        store.start_member(run.run_id, config.identity, now=NOW)
        store.finish_member(
            run.run_id, config.identity, status=MemberRunStatus.COMPLETED, now=NOW
        )
        _observation(shared, run.run_id, config.identity)
    store.finalize(run.run_id, now=NOW)

    report = _inspect(shared)

    assert report.state is recovery.RecoveryState.COMPLETED
    assert report.exit_code == 0
    assert report.resume_safety is recovery.ResumeSafety.FORBIDDEN
    assert report.run_status == "completed"
    assert set(report.completed_members) == {config.identity for config in configs}
    assert report.missing_members == ()
    assert len(report.official_observations) == 2
    assert report.recommended_action == "No action required. This session completed in full."


def test_active_run_with_a_live_lease_recommends_waiting(shared):
    store, run, configs = _run(shared, "one", "two")
    store.set_status(run.run_id, SleeveRunStatus.RUNNING, now=NOW)
    store.start_member(run.run_id, configs[0].identity, now=NOW)
    _lease(
        shared, acquired_ago=timedelta(minutes=10), expires_in=timedelta(hours=5), released=False
    )

    report = _inspect(shared)

    assert report.state is recovery.RecoveryState.ACTIVE
    assert report.exit_code == 0
    assert report.resume_safety is recovery.ResumeSafety.AMBIGUOUS
    assert report.lease.present is True
    assert report.lease.released is False
    assert report.lease.expired is False
    assert report.lease.age_seconds == 600
    assert "Do nothing" in report.recommended_action


def test_running_member_without_a_lease_is_interrupted(shared):
    store, run, configs = _run(shared, "one", "two")
    store.set_status(run.run_id, SleeveRunStatus.RUNNING, now=NOW)
    store.start_member(run.run_id, configs[0].identity, now=NOW)
    # The runner released its lease on the way out but never finished the member.
    _lease(shared, acquired_ago=timedelta(hours=1), expires_in=timedelta(hours=5), released=True)

    report = _inspect(shared)

    assert report.state is recovery.RecoveryState.INTERRUPTED
    assert report.exit_code == 1
    assert report.resume_safety is recovery.ResumeSafety.AMBIGUOUS
    assert report.interrupted_members == (configs[0].identity,)
    assert report.missing_members == (configs[1].identity,)
    assert "do not reset a member by hand" in report.recommended_action


def test_stale_lease_is_reported_and_must_not_be_cleared(shared):
    store, run, _configs = _run(shared, "one")
    store.set_status(run.run_id, SleeveRunStatus.RUNNING, now=NOW)
    _lease(
        shared,
        acquired_ago=timedelta(hours=12),
        expires_in=-timedelta(hours=6),
        released=False,
    )

    report = _inspect(shared)

    assert report.state is recovery.RecoveryState.STALE_LEASE
    assert report.exit_code == 1
    assert report.resume_safety is recovery.ResumeSafety.AMBIGUOUS
    assert report.lease.expired is True
    assert report.lease.released is False
    assert "Do not clear the lease" in report.recommended_action


def test_awaiting_data_is_retryable_and_safe_to_resume(shared):
    store, run, _configs = _run(shared, "one", "two")
    store.set_status(
        run.run_id,
        SleeveRunStatus.AWAITING_DATA,
        error=SleeveRunError(
            code="awaiting_data",
            message="daily bars do not yet cover the session",
            retryable=True,
        ),
        now=NOW,
    )

    report = _inspect(shared)

    assert report.state is recovery.RecoveryState.AWAITING_DATA
    assert report.exit_code == 1
    # Nothing executed, so the retry keeps its all-or-nothing guarantee.
    assert report.resume_safety is recovery.ResumeSafety.SAFE
    assert report.run_error_codes == ("awaiting_data",)
    assert report.missing_members == tuple(report.expected_members)
    assert "Re-run the official session" in report.recommended_action


def test_awaiting_reauthentication_is_its_own_state_with_its_own_action(shared):
    """Issue #109: retryable like an awaiting-data wait, but only a human clears it.

    Giving both waits the same "re-run once the awaited data has landed" action would
    leave an operator waiting on a provider that was never the problem.
    """
    store, run, _configs = _run(shared, "one", "two")
    store.set_status(
        run.run_id,
        SleeveRunStatus.AWAITING_DATA,
        error=SleeveRunError(
            code="awaiting_reauthentication",
            message="Schwab reauthentication is required before the snapshot is captured.",
            capability="authentication",
            retryable=True,
            reasons=("authentication:reauthorization_required",),
        ),
        now=NOW,
    )

    report = _inspect(shared)

    assert report.state is recovery.RecoveryState.AWAITING_AUTH
    assert report.state is not recovery.RecoveryState.FAILED
    assert report.exit_code == 1
    # Nothing executed, so the retry keeps its all-or-nothing guarantee.
    assert report.resume_safety is recovery.ResumeSafety.SAFE
    assert report.run_error_codes == ("awaiting_reauthentication",)
    assert report.missing_members == tuple(report.expected_members)
    assert report.snapshot_id is None
    assert "auth login" in report.recommended_action


def test_the_last_wait_row_decides_the_recovery_state(shared):
    """A session that waited on auth and then on data reads as awaiting data."""
    store, run, _configs = _run(shared, "one", "two")
    for code, message in (
        ("awaiting_reauthentication", "authenticate first"),
        ("awaiting_data", "daily bars do not yet cover the session"),
    ):
        store.set_status(
            run.run_id,
            SleeveRunStatus.AWAITING_DATA,
            error=SleeveRunError(code=code, message=message, retryable=True),
            now=NOW,
        )

    assert _inspect(shared).state is recovery.RecoveryState.AWAITING_DATA


def test_a_terminal_failure_carrying_a_reauth_row_is_still_terminal(shared):
    """Fail-closed: the code only softens a run the runner deliberately left open."""
    store, run, configs = _run(shared, "one", "two")
    for config in configs:
        store.finish_member(
            run.run_id,
            config.identity,
            status=MemberRunStatus.FAILED,
            error=SleeveRunError(code="snapshot_unavailable", message="closed", retryable=False),
            now=NOW,
        )
    store.set_status(
        run.run_id,
        SleeveRunStatus.FAILED,
        error=SleeveRunError(
            code="awaiting_reauthentication", message="stale row", retryable=True
        ),
        now=NOW,
        terminal=True,
    )

    report = _inspect(shared)

    assert report.state is recovery.RecoveryState.FAILED
    assert report.resume_safety is recovery.ResumeSafety.FORBIDDEN
    assert "Do not re-run" in report.recommended_action


def test_partial_run_is_terminal_and_must_not_be_rerun(shared):
    store, run, configs = _run(shared, "one", "two")
    store.start_member(run.run_id, configs[0].identity, now=NOW)
    store.finish_member(
        run.run_id, configs[0].identity, status=MemberRunStatus.COMPLETED, now=NOW
    )
    _observation(shared, run.run_id, configs[0].identity)
    store.finish_member(
        run.run_id,
        configs[1].identity,
        status=MemberRunStatus.DATA_NOT_READY,
        error=SleeveRunError(code="data_not_ready", message="not ready", retryable=False),
        now=NOW,
    )
    store.finalize(run.run_id, now=NOW)

    report = _inspect(shared)

    assert report.state is recovery.RecoveryState.PARTIAL
    assert report.exit_code == 1
    assert report.resume_safety is recovery.ResumeSafety.FORBIDDEN
    assert report.completed_members == (configs[0].identity,)
    assert report.other_members["data-not-ready"] == (configs[1].identity,)
    assert report.official_observations == (f"{COHORT}:{configs[0].identity}:2026-07-29",)
    assert "Do not re-run" in report.recommended_action


def test_failed_run_is_terminal(shared):
    store, run, configs = _run(shared, "one")
    store.start_member(run.run_id, configs[0].identity, now=NOW)
    store.finish_member(
        run.run_id,
        configs[0].identity,
        status=MemberRunStatus.FAILED,
        error=SleeveRunError(code="snapshot_unavailable", message="no snapshot"),
        now=NOW,
    )
    store.finalize(run.run_id, now=NOW)

    report = _inspect(shared)

    assert report.state is recovery.RecoveryState.FAILED
    assert report.resume_safety is recovery.ResumeSafety.FORBIDDEN
    assert report.failed_members == (configs[0].identity,)
    assert report.run_error_codes == ("snapshot_unavailable",)


def test_missed_run_must_not_be_backfilled(shared):
    store, run, _configs = _run(shared, "one")
    store.set_status(
        run.run_id,
        SleeveRunStatus.MISSED,
        error=SleeveRunError(code="data_deadline_exceeded", message="superseded"),
        now=NOW,
        terminal=True,
    )

    report = _inspect(shared)

    assert report.state is recovery.RecoveryState.MISSED
    assert report.resume_safety is recovery.ResumeSafety.FORBIDDEN
    assert "Do not backfill" in report.recommended_action


def test_no_run_past_the_grace_period_is_late(shared):
    _members(shared, "one")

    report = _inspect(shared, now_et=PAST_GRACE)

    assert report.state is recovery.RecoveryState.LATE
    assert report.exit_code == 1
    assert report.resume_safety is recovery.ResumeSafety.SAFE
    assert report.run_id is None
    assert report.run_key == scheduling.run_key(COHORT, scheduling.session_for_date(SESSION))
    assert report.schedule_verdict == "late"
    assert "Run the official session" in report.recommended_action


def test_no_run_before_the_decision_time_is_not_due(shared):
    _members(shared, "one")

    report = _inspect(shared, now_et=BEFORE_DUE)

    assert report.state is recovery.RecoveryState.NOT_DUE
    assert report.exit_code == 0
    assert report.schedule_verdict == "pending"
    assert "No action required" in report.recommended_action


def test_no_run_within_the_grace_period_is_still_late_for_recovery_purposes(shared):
    """DUE and LATE collapse to one recovery state: the same action resolves both."""
    _members(shared, "one")

    report = _inspect(shared, now_et=WITHIN_GRACE)

    assert report.schedule_verdict == "due"
    assert report.state is recovery.RecoveryState.LATE


def test_no_run_past_the_deadline_is_missed(shared):
    _members(shared, "one")

    report = _inspect(shared, now_et=PAST_DEADLINE)

    assert report.state is recovery.RecoveryState.MISSED
    assert report.schedule_verdict == "missed"


def test_a_closed_session_is_not_a_finding(shared):
    _members(shared, "one")
    saturday = date(2026, 7, 25)

    report = recovery.inspect_recovery(
        shared, COHORT, saturday, now=NOW, now_et=PAST_DEADLINE
    )

    assert report.state is recovery.RecoveryState.SKIPPED_CLOSED_SESSION
    assert report.exit_code == 0
    assert report.is_trading_day is False
    assert report.resume_safety is recovery.ResumeSafety.FORBIDDEN


def test_stale_lease_outranks_a_completed_run_only_when_the_run_is_unfinished(shared):
    """A completed run with a leftover lease row is completed, not a recovery case.

    The lease matters because it blocks the *next* runner. Once the session itself is
    durably complete, nothing is waiting on it.
    """
    store, run, configs = _run(shared, "one")
    store.start_member(run.run_id, configs[0].identity, now=NOW)
    store.finish_member(run.run_id, configs[0].identity, status=MemberRunStatus.COMPLETED, now=NOW)
    store.finalize(run.run_id, now=NOW)
    _lease(
        shared, acquired_ago=timedelta(hours=12), expires_in=-timedelta(hours=6), released=False
    )

    report = _inspect(shared)

    assert report.state is recovery.RecoveryState.COMPLETED


# --------------------------------------------------------------------------------
# Evidence and sanitization
# --------------------------------------------------------------------------------


def test_snapshot_and_observation_identities_are_reported(shared):
    store, run, configs = _run(shared, "one")
    store.set_snapshot(
        run.run_id,
        snapshot_id="snapshot:abc123",
        quote_snapshot_id="quotes:def456",
        data_snapshot_ids={"daily_bars": "bars:ghi789"},
    )
    store.start_member(run.run_id, configs[0].identity, now=NOW)
    store.finish_member(run.run_id, configs[0].identity, status=MemberRunStatus.COMPLETED, now=NOW)
    _observation(shared, run.run_id, configs[0].identity)
    store.finalize(run.run_id, now=NOW)

    report = _inspect(shared)

    assert report.snapshot_id == "snapshot:abc123"
    assert report.quote_snapshot_id == "quotes:def456"
    assert report.data_snapshot_ids == ("daily_bars",)
    member = report.members[0]
    assert member.observation_key == f"{COHORT}:{configs[0].identity}:2026-07-29"
    assert member.observation_status == "official"


def test_lease_owner_and_token_are_never_serialized(shared):
    _run(shared, "one")
    _lease(shared, acquired_ago=timedelta(hours=1), expires_in=timedelta(hours=5), released=False)

    payload = json.dumps(_inspect(shared).sanitized_payload())

    assert "machine-deadbeefcafe" not in payload
    assert "process-4242" not in payload
    assert "lease_token_hash" not in payload
    assert "owner_id" not in payload


def test_member_error_prose_is_not_re_rendered_only_its_code(shared):
    store, run, configs = _run(shared, "one")
    store.start_member(run.run_id, configs[0].identity, now=NOW)
    store.finish_member(
        run.run_id,
        configs[0].identity,
        status=MemberRunStatus.FAILED,
        error=SleeveRunError(
            code="provider_error",
            message="unexpected operator prose that the inspector has no reason to echo",
        ),
        now=NOW,
    )
    store.finalize(run.run_id, now=NOW)

    payload = json.dumps(_inspect(shared).sanitized_payload())

    assert "provider_error" in payload
    assert "unexpected operator prose" not in payload


def test_empty_cohort_id_is_refused(shared):
    with pytest.raises(ValueError, match="must not be empty"):
        recovery.inspect_recovery(shared, "   ", SESSION, now=NOW, now_et=PAST_GRACE)


# --------------------------------------------------------------------------------
# The inspector must not mutate
# --------------------------------------------------------------------------------


def test_inspection_never_mutates(tmp_path):
    """Run every classification path and prove the database file is byte-identical."""
    path = tmp_path / "shared.sqlite3"
    database = Database(f"sqlite:///{path.as_posix()}", create_schema=True)
    store, run, configs = _run(database, "one", "two")
    store.set_status(run.run_id, SleeveRunStatus.RUNNING, now=NOW)
    store.start_member(run.run_id, configs[0].identity, now=NOW)
    _lease(
        database, acquired_ago=timedelta(hours=12), expires_in=-timedelta(hours=6), released=False
    )
    _observation(database, run.run_id, configs[0].identity)
    database.dispose()

    reader = Database(f"sqlite:///{path.as_posix()}")
    before = path.read_bytes()
    for now_et in (BEFORE_DUE, WITHIN_GRACE, PAST_GRACE, PAST_DEADLINE):
        recovery.inspect_recovery(reader, COHORT, SESSION, now=NOW, now_et=now_et)
        recovery.inspect_recovery(
            reader, "cohort-that-does-not-exist", SESSION, now=NOW, now_et=now_et
        )
    reader.dispose()

    assert path.read_bytes() == before


def test_every_state_has_exactly_one_action_and_one_exit_code():
    """The mapping must be total, or a real session could reach an unhandled branch."""
    for state in recovery.RecoveryState:
        assert state in recovery.RECOVERY_EXIT_CODES
        action = recovery._ACTIONS[state]
        assert action and action[0].isupper() and action.endswith(".")


# --------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------


def _run_cli(monkeypatch, database: Database, *arguments: str):
    # Sleeve identities are 64-character hashes with no break point, so a default
    # 80-column console folds them across lines. Widen it so the comparison is about
    # what the rendering *says*, not about terminal geometry.
    monkeypatch.setenv("COLUMNS", "400")
    monkeypatch.setattr(storage_factory, "database", lambda _settings: database)
    monkeypatch.setattr("schwab_trader.storage.cli.get_settings", lambda: object())
    return CliRunner().invoke(app, ["storage", "recover", *arguments])


def test_cli_human_and_json_agree_and_carry_the_exit_code(shared, monkeypatch):
    store, run, configs = _run(shared, "one", "two")
    store.start_member(run.run_id, configs[0].identity, now=NOW)
    store.finish_member(run.run_id, configs[0].identity, status=MemberRunStatus.COMPLETED, now=NOW)
    store.finish_member(
        run.run_id, configs[1].identity, status=MemberRunStatus.DATA_NOT_READY, now=NOW
    )
    store.finalize(run.run_id, now=NOW)

    human = _run_cli(monkeypatch, shared, "--cohort", COHORT, "--scheduled-for", "2026-07-29")
    document = _run_cli(
        monkeypatch, shared, "--cohort", COHORT, "--scheduled-for", "2026-07-29", "--json"
    )

    assert human.exit_code == document.exit_code == 1
    payload = json.loads(document.stdout)
    assert payload["state"] == "partial"
    assert payload["contract_version"] == recovery.RECOVERY_CONTRACT_VERSION

    plain = " ".join(human.stdout.split())
    assert payload["state"] in plain
    assert payload["resume_safety"] in plain
    assert payload["run_status"] in plain
    for member in payload["members"]:
        assert member["sleeve_id"] in plain
        assert member["status"] in plain


def test_cli_rejects_a_malformed_date(shared, monkeypatch):
    result = _run_cli(monkeypatch, shared, "--cohort", COHORT, "--scheduled-for", "29-07-2026")

    assert result.exit_code == 1
    assert "YYYY-MM-DD" in result.stdout
