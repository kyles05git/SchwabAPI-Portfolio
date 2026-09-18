"""Offline tests for restart-safe, side-effect-free scheduling primitives."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from schwab_trader import scheduling as sched

COHORT = "paper-cohort-v1"


# --- Exchange-session identity and official timestamps ---------------------------------


def test_session_for_trading_day_identity_and_timestamps() -> None:
    session = sched.session_for_date(date(2026, 7, 20))  # a normal Monday
    assert session.is_trading_day
    assert not session.is_early_close
    assert session.session_id == "XNYS:2026-07-20"
    assert session.open_et == datetime(2026, 7, 20, 9, 30)
    assert session.close_et == datetime(2026, 7, 20, 16, 0)
    assert session.decision_et == datetime(2026, 7, 20, 16, 0)
    assert session.valuation_et == datetime(2026, 7, 20, 16, 0)
    # 16:00 EDT == 20:00 UTC in July.
    assert session.decision_utc == datetime(2026, 7, 20, 20, 0, tzinfo=UTC)
    assert session.valuation_utc == datetime(2026, 7, 20, 20, 0, tzinfo=UTC)


def test_session_for_early_close_uses_1pm() -> None:
    session = sched.session_for_date(date(2025, 12, 24))  # Christmas Eve early close
    assert session.is_trading_day
    assert session.is_early_close
    assert session.close_et == datetime(2025, 12, 24, 13, 0)
    assert session.decision_et == datetime(2025, 12, 24, 13, 0)
    # 13:00 EST == 18:00 UTC in December.
    assert session.decision_utc == datetime(2025, 12, 24, 18, 0, tzinfo=UTC)


@pytest.mark.parametrize("closed", [date(2026, 7, 18), date(2026, 12, 25)])
def test_session_for_closed_day_has_no_timestamps(closed: date) -> None:
    session = sched.session_for_date(closed)
    assert not session.is_trading_day
    assert session.open_et is None
    assert session.close_et is None
    assert session.decision_et is None
    assert session.decision_utc is None


def test_custom_exchange_mic_flows_into_identity() -> None:
    session = sched.session_for_date(date(2026, 7, 20), exchange="XNAS")
    assert session.session_id == "XNAS:2026-07-20"


# --- Deterministic run keys ------------------------------------------------------------


def test_run_key_is_deterministic_from_cohort_and_session() -> None:
    session = sched.session_for_date(date(2026, 7, 20))
    assert sched.run_key(COHORT, session) == "paper-cohort-v1@XNYS:2026-07-20"
    # Same key whether built from the session or its id string, and stable across calls.
    assert sched.run_key(COHORT, session) == sched.run_key(COHORT, session.session_id)


def test_run_fingerprint_is_stable_and_distinct() -> None:
    session = sched.session_for_date(date(2026, 7, 20))
    other = sched.session_for_date(date(2026, 7, 21))
    fp = sched.run_fingerprint(COHORT, session)
    assert fp == sched.run_fingerprint(COHORT, session)  # stable
    assert len(fp) == 64
    assert fp != sched.run_fingerprint(COHORT, other)
    assert fp != sched.run_fingerprint("other-cohort", session)


# --- evaluate_session: closed, pending, due, late, missed, completed -------------------


def test_evaluate_weekend_is_skipped_closed_session() -> None:
    decision = sched.evaluate_session(COHORT, date(2026, 7, 18), datetime(2026, 7, 18, 16, 0))
    assert decision.status is sched.RunStatus.SKIPPED_CLOSED_SESSION
    assert not decision.should_run
    assert decision.run_key is None
    assert "weekend" in decision.reason


def test_evaluate_holiday_is_skipped_closed_session() -> None:
    decision = sched.evaluate_session(COHORT, date(2026, 12, 25), datetime(2026, 12, 25, 16, 0))
    assert decision.status is sched.RunStatus.SKIPPED_CLOSED_SESSION
    assert "Christmas" in decision.reason


def test_evaluate_before_close_is_pending() -> None:
    decision = sched.evaluate_session(COHORT, date(2026, 7, 20), datetime(2026, 7, 20, 15, 59))
    assert decision.status is sched.RunStatus.PENDING
    assert not decision.should_run
    assert decision.due_et == datetime(2026, 7, 20, 16, 0)


def test_evaluate_at_close_is_due() -> None:
    decision = sched.evaluate_session(COHORT, date(2026, 7, 20), datetime(2026, 7, 20, 16, 0))
    assert decision.status is sched.RunStatus.DUE
    assert decision.should_run
    assert decision.run_key == "paper-cohort-v1@XNYS:2026-07-20"
    assert decision.late_by == timedelta(0)


def test_evaluate_within_grace_is_due() -> None:
    decision = sched.evaluate_session(COHORT, date(2026, 7, 20), datetime(2026, 7, 20, 17, 59))
    assert decision.status is sched.RunStatus.DUE


def test_evaluate_past_grace_is_late_but_runnable() -> None:
    decision = sched.evaluate_session(COHORT, date(2026, 7, 20), datetime(2026, 7, 20, 20, 0))
    assert decision.status is sched.RunStatus.LATE
    assert decision.should_run
    assert decision.late_by == timedelta(hours=4)


def test_evaluate_missed_once_next_session_is_due() -> None:
    # Monday's run is superseded once Tuesday's close (the next session's due time) passes.
    decision = sched.evaluate_session(COHORT, date(2026, 7, 20), datetime(2026, 7, 21, 16, 1))
    assert decision.status is sched.RunStatus.MISSED
    assert not decision.should_run
    # Just before Tuesday's close it is still a runnable late catch-up.
    still_late = sched.evaluate_session(COHORT, date(2026, 7, 20), datetime(2026, 7, 21, 15, 0))
    assert still_late.status is sched.RunStatus.LATE
    assert still_late.should_run


def test_max_catchup_caps_the_late_window() -> None:
    policy = sched.SchedulingPolicy(max_catchup=timedelta(hours=6))
    late = sched.evaluate_session(
        COHORT, date(2026, 7, 20), datetime(2026, 7, 20, 21, 0), policy=policy
    )
    assert late.status is sched.RunStatus.LATE  # 5h after close, within 6h cap
    missed = sched.evaluate_session(
        COHORT, date(2026, 7, 20), datetime(2026, 7, 20, 22, 1), policy=policy
    )
    assert missed.status is sched.RunStatus.MISSED  # past the 6h cap


def test_early_close_shifts_due_time_to_1pm() -> None:
    # 13:30 on an early-close day is already past the 13:00 close (DUE), not PENDING.
    decision = sched.evaluate_session(COHORT, date(2025, 12, 24), datetime(2025, 12, 24, 13, 30))
    assert decision.status is sched.RunStatus.DUE
    # 12:59 is still before the early close.
    pending = sched.evaluate_session(COHORT, date(2025, 12, 24), datetime(2025, 12, 24, 12, 59))
    assert pending.status is sched.RunStatus.PENDING


def test_decision_delay_defers_due_time() -> None:
    policy = sched.SchedulingPolicy(decision_delay=timedelta(minutes=15))
    at_close = sched.evaluate_session(
        COHORT, date(2026, 7, 20), datetime(2026, 7, 20, 16, 0), policy=policy
    )
    assert at_close.status is sched.RunStatus.PENDING
    assert at_close.due_et == datetime(2026, 7, 20, 16, 15)
    after = sched.evaluate_session(
        COHORT, date(2026, 7, 20), datetime(2026, 7, 20, 16, 15), policy=policy
    )
    assert after.status is sched.RunStatus.DUE


# --- Idempotency: duplicate requests and restarts -------------------------------------


def test_completed_run_key_is_already_completed() -> None:
    key = sched.run_key(COHORT, sched.session_for_date(date(2026, 7, 20)))
    decision = sched.evaluate_session(
        COHORT, date(2026, 7, 20), datetime(2026, 7, 20, 16, 0), completed_run_keys={key}
    )
    assert decision.status is sched.RunStatus.ALREADY_COMPLETED
    assert not decision.should_run


def test_duplicate_request_is_idempotent_after_completion() -> None:
    now = datetime(2026, 7, 20, 16, 0)
    first = sched.evaluate_session(COHORT, date(2026, 7, 20), now)
    assert first.should_run
    # Simulate a durable store recording the finished run, then a duplicate request.
    completed = {first.run_key}
    second = sched.evaluate_session(COHORT, date(2026, 7, 20), now, completed_run_keys=completed)
    assert second.status is sched.RunStatus.ALREADY_COMPLETED


def test_restart_reaches_identical_decision() -> None:
    now = datetime(2026, 7, 20, 17, 30)
    completed: set[str] = set()
    before = sched.plan_run(COHORT, now, completed_run_keys=completed)
    # A restarted process rebuilds from the same durable inputs and must not re-run.
    completed.add(before.run_key or "")
    after_restart = sched.plan_run(COHORT, now, completed_run_keys=completed)
    assert before.should_run
    assert after_restart.status is sched.RunStatus.ALREADY_COMPLETED
    assert after_restart.run_key == before.run_key


# --- Resolving the scheduled session from "now" ---------------------------------------


def test_most_recent_due_session_before_close_is_previous_day() -> None:
    # Monday 15:00, before Monday's close -> the most recent due session is Friday.
    resolved = sched.most_recent_due_session_date(datetime(2026, 7, 20, 15, 0))
    assert resolved == date(2026, 7, 17)


def test_most_recent_due_session_after_close_is_today() -> None:
    resolved = sched.most_recent_due_session_date(datetime(2026, 7, 20, 16, 30))
    assert resolved == date(2026, 7, 20)


def test_most_recent_due_session_on_weekend_is_friday() -> None:
    resolved = sched.most_recent_due_session_date(datetime(2026, 7, 19, 12, 0))  # Sunday
    assert resolved == date(2026, 7, 17)


def test_most_recent_due_session_spans_holiday() -> None:
    # Saturday after the July 3 2026 holiday week -> most recent session is July 2.
    resolved = sched.most_recent_due_session_date(datetime(2026, 7, 4, 12, 0))
    assert resolved == date(2026, 7, 2)


def test_plan_run_on_weekend_targets_and_runs_friday_late() -> None:
    # Sunday morning: Friday's session is a runnable late catch-up until Monday's close.
    decision = sched.plan_run(COHORT, datetime(2026, 7, 19, 9, 0))
    assert decision.session.session_date == date(2026, 7, 17)
    assert decision.status is sched.RunStatus.LATE
    assert decision.should_run
    assert decision.run_key == "paper-cohort-v1@XNYS:2026-07-17"


def test_plan_run_late_after_a_missed_evening() -> None:
    # Tuesday morning, Monday's run never happened and is now late but within deadline.
    decision = sched.plan_run(COHORT, datetime(2026, 7, 21, 8, 0))
    assert decision.session.session_date == date(2026, 7, 20)
    assert decision.status is sched.RunStatus.LATE
    assert decision.should_run


# --- Timezone / DST correctness on the scheduling path --------------------------------


def test_due_time_uses_wall_clock_across_dst() -> None:
    # A March EST session and a July EDT session both become due at 16:00 wall-clock,
    # even though their UTC instants differ by an hour.
    est_session = sched.session_for_date(date(2026, 3, 6))  # before DST starts (EST)
    edt_session = sched.session_for_date(date(2026, 7, 6))  # during DST (EDT)
    assert est_session.close_et.time() == edt_session.close_et.time()
    assert est_session.decision_utc == datetime(2026, 3, 6, 21, 0, tzinfo=UTC)  # 16:00 -05
    assert edt_session.decision_utc == datetime(2026, 7, 6, 20, 0, tzinfo=UTC)  # 16:00 -04

    # Evaluated with naive ET, both are DUE exactly at their 16:00 wall-clock close.
    est = sched.evaluate_session(COHORT, date(2026, 3, 6), datetime(2026, 3, 6, 16, 0))
    edt = sched.evaluate_session(COHORT, date(2026, 7, 6), datetime(2026, 7, 6, 16, 0))
    assert est.status is sched.RunStatus.DUE
    assert edt.status is sched.RunStatus.DUE


def test_elapsed_duration_is_human_readable_without_microseconds() -> None:
    value = timedelta(days=2, hours=3, minutes=4, microseconds=567_000)
    assert sched.human_duration(value) == "2d 3h"
    decision = sched.evaluate_session(
        COHORT, date(2026, 7, 20), datetime(2026, 7, 20, 18, 0, 0, 500_000)
    )
    assert "microsecond" not in decision.reason
    assert "2h" in decision.reason


    # Guard against accidental network/broker/db imports in a "pure" module by
    # inspecting the module objects it actually binds, not its prose.
    import types

    imported = {
        value.__name__ for value in vars(sched).values() if isinstance(value, types.ModuleType)
    }
    assert imported <= {"hashlib", "schwab_trader.market_calendar"}
    forbidden = {"httpx", "requests", "sqlite3", "socket", "urllib", "urllib.request"}
    assert imported.isdisjoint(forbidden)
