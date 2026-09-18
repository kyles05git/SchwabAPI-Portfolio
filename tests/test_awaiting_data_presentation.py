"""A resolved provider wait must stop being presented as a live failure.

``SleeveRun.errors`` is append-only because the audit trail requires it, so it holds
every attempt's verdict at once. Every presentation boundary therefore has to ask which
of those rows still describes the run, and a wait that a later attempt resolved does
not. These tests drive the real ``SleeveRunStore`` through the exact
``awaiting_data`` -> ``completed`` transition the cohort runner performs.

Offline: SQLite under ``tmp_path`` only. No network, provider, broker, or ``.env``.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from schwab_trader import (
    cohort_ops,
    cohort_phase,
    dashboard,
    scheduling,
    sleeve_runs,
    sleeves,
    strategy_registry,
)
from schwab_trader.config import Settings

COHORT = "cohort-a"
MEMBERS = ("bench-spy", "candidate")
SESSION = date(2026, 7, 27)
#: After the 16:00 ET close, inside the retry deadline the wait was measured against.
NOW_ET = datetime(2026, 7, 27, 18, 45)
WAITED_AT = datetime(2026, 7, 27, 20, 30, tzinfo=UTC)
RESOLVED_AT = datetime(2026, 7, 27, 22, 30, tzinfo=UTC)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        token_path=tmp_path / "tokens.json",
        state_db_path=tmp_path / "state.sqlite3",
        log_path=tmp_path / "logs" / "app.log",
        sleeves_dir=tmp_path / "sleeves",
        kill_switch_path=tmp_path / "KILL_SWITCH",
        agent_activity_db_path=tmp_path / "agent_activity.sqlite3",
        promotion_db_path=tmp_path / "promotion.sqlite3",
        approval_db_path=tmp_path / "approvals.sqlite3",
        tax_lots_db_path=tmp_path / "taxlots.sqlite3",
    )


def _create_cohort(settings: Settings) -> sleeves.SleeveStore:
    store = sleeves.SleeveStore(settings.sleeves_dir)
    for index, name in enumerate(MEMBERS):
        symbol = "SPY" if name == "bench-spy" else f"T{index}"
        definition = strategy_registry.make_definition(
            "buy-hold",
            universe_definition=[symbol],
            benchmark_symbol_or_sleeve="bench-spy",
        )
        store.create(
            name,
            strategy="buy-hold",
            universe=[symbol],
            starting_cash=Decimal("1000"),
            max_positions=1,
            max_position_fraction=Decimal("1"),
            definition=definition,
            cohort_id=COHORT,
        )
    return store


def _coverage(observed: int, *, symbols: tuple[str, ...] = ("SPY",)) -> dict[str, object]:
    """The sanitized operator context one awaiting-data attempt records."""
    return {
        "market_data": {
            "target_session": SESSION.isoformat(),
            "symbols": [
                {
                    "symbol": symbol,
                    "state": "incomplete",
                    "latest_official_session": "2026-07-24",
                    "evidence_source": "schwab-intraday-derived-daily",
                    "expected_interval_count": 78,
                    "observed_interval_count": observed,
                    "first_interval_at": "2026-07-27T09:30:00-04:00",
                    "final_interval_at": "2026-07-27T15:55:00-04:00",
                    "aggregation_safe": False,
                    "dataset_id": None,
                    "error": None,
                }
                for symbol in symbols
            ],
        }
    }


def _wait(observed: int) -> sleeve_runs.SleeveRunError:
    return sleeve_runs.SleeveRunError(
        code="awaiting_data",
        message=f"Required provider evidence is incomplete ({observed}/78).",
        capability="daily_bars",
        retryable=True,
        reasons=("daily_bars:session_not_covered",),
        context=_coverage(observed),
    )


def _await_then_complete(run_store: sleeve_runs.SleeveRunStore) -> sleeve_runs.SleeveRun:
    """Wait on provider data, then let a later attempt execute every member."""
    run = run_store.ensure_run(
        cohort_id=COHORT,
        session=scheduling.session_for_date(SESSION),
        expected_members=MEMBERS,
    )
    run_store.set_status(
        run.run_id,
        sleeve_runs.SleeveRunStatus.AWAITING_DATA,
        error=_wait(60),
        now=WAITED_AT,
        terminal=False,
    )
    for sleeve_id in MEMBERS:
        assert run_store.start_member(run.run_id, sleeve_id, now=RESOLVED_AT)
        run_store.finish_member(
            run.run_id,
            sleeve_id,
            status=sleeve_runs.MemberRunStatus.COMPLETED,
            now=RESOLVED_AT,
        )
    return run_store.finalize(run.run_id, now=RESOLVED_AT)


def test_resolved_wait_is_history_not_a_live_failure(tmp_path: Path) -> None:
    """A COMPLETED run that once waited on provider data reports no current error."""
    settings = _settings(tmp_path)
    _create_cohort(settings)
    run_store = sleeve_runs.SleeveRunStore(settings.sleeves_dir / "runs.sqlite3")

    run = _await_then_complete(run_store)
    assert run.status is sleeve_runs.SleeveRunStatus.COMPLETED
    assert set(run.completed_members) == set(MEMBERS)

    # The presentation boundary: nothing about this run is still failing.
    assert sleeve_runs.active_errors(run) == ()

    assessment = cohort_phase.assess_cohort_phase(
        cohort_id=COHORT,
        runs=[run],
        observations=[],
        gate=None,
        now_et=NOW_ET,
        start_session=SESSION,
    )
    assert assessment.latest_due_run is not None
    assert assessment.latest_due_run.status == "completed"
    assert assessment.latest_due_run.error_summary is None

    payload = cohort_ops.health_payload(
        cohort_ops.assess_cohort(
            COHORT,
            now_et=NOW_ET,
            runs=[run],
            expected_members=MEMBERS,
            session_date=SESSION,
            cohort_start=SESSION,
        )
    )
    assert payload["run"] is not None
    assert payload["run"]["errors"] == []
    assert payload["run"]["status"] == "completed"

    # ...and the durable audit trail still records that the wait happened.
    reloaded = run_store.get(run.run_id)
    assert reloaded is not None
    assert [error.code for error in reloaded.errors] == ["awaiting_data"]
    assert reloaded.errors[0].retryable is True
    assert reloaded.errors[0].reasons == ("daily_bars:session_not_covered",)


def test_unsuperseded_terminal_error_stays_visible(tmp_path: Path) -> None:
    """``active_errors`` only filters COMPLETED runs; a MISSED wait was never resolved."""
    settings = _settings(tmp_path)
    _create_cohort(settings)
    run_store = sleeve_runs.SleeveRunStore(settings.sleeves_dir / "runs.sqlite3")
    run = run_store.ensure_run(
        cohort_id=COHORT,
        session=scheduling.session_for_date(SESSION),
        expected_members=MEMBERS,
    )
    run_store.set_status(
        run.run_id,
        sleeve_runs.SleeveRunStatus.AWAITING_DATA,
        error=_wait(60),
        now=WAITED_AT,
        terminal=False,
    )
    missed = run_store.set_status(
        run.run_id,
        sleeve_runs.SleeveRunStatus.MISSED,
        error=sleeve_runs.SleeveRunError(
            code="data_deadline_exceeded",
            message="Required data never became ready before the session deadline.",
        ),
        now=RESOLVED_AT,
        terminal=True,
    )

    assert [error.code for error in sleeve_runs.active_errors(missed)] == [
        "awaiting_data",
        "data_deadline_exceeded",
    ]


def test_dashboard_shows_the_current_wait_not_the_first(tmp_path: Path) -> None:
    """Successive waits with advancing coverage surface the later context."""
    settings = _settings(tmp_path)
    _create_cohort(settings)
    run_store = sleeve_runs.SleeveRunStore(settings.sleeves_dir / "runs.sqlite3")
    run = run_store.ensure_run(
        cohort_id=COHORT,
        session=scheduling.session_for_date(SESSION),
        expected_members=MEMBERS,
    )
    for observed in (60, 77):
        run_store.set_status(
            run.run_id,
            sleeve_runs.SleeveRunStatus.AWAITING_DATA,
            error=_wait(observed),
            now=WAITED_AT,
            terminal=False,
        )

    view = dashboard.collect_cohort_dashboard(
        settings,
        requested_cohort=COHORT,
        benchmark="bench-spy",
        now_et=NOW_ET,
    )
    run_view = view.run_health.runs[0]
    assert run_view.timing == "awaiting-provider-data"
    waits = [error for error in run_view.errors if error.code == "awaiting_data"]
    assert waits, "the wait must reach the dashboard payload"

    # The panel reads the *last* awaiting_data row, so that row must carry the current
    # numbers. Presenting attempt 0's 60/78 for the whole wait is the defect.
    market_data = waits[-1].context["market_data"]
    assert isinstance(market_data, dict)
    symbols = market_data["symbols"]
    assert isinstance(symbols, list)
    assert symbols[0]["observed_interval_count"] == 77
    assert "77/78" in waits[-1].message

    # Only the current wait carries coverage; the superseded row is still recorded.
    assert waits[0].message.endswith("(60/78).")
    assert "market_data" not in waits[0].context


def test_awaiting_data_payload_stays_bounded_across_many_retries(tmp_path: Path) -> None:
    """A long wait must not grow the errors blob by one full coverage list per retry.

    Measured before this bound: 75 symbols over 12 retries wrote 353,378 bytes into the
    single ``sleeve_runs.errors`` TEXT column, and every byte reached the health JSON and
    the dashboard payload.
    """
    settings = _settings(tmp_path)
    _create_cohort(settings)
    run_store = sleeve_runs.SleeveRunStore(settings.sleeves_dir / "runs.sqlite3")
    run = run_store.ensure_run(
        cohort_id=COHORT,
        session=scheduling.session_for_date(SESSION),
        expected_members=MEMBERS,
    )
    universe = tuple(f"S{index:03d}" for index in range(75))

    latest = run
    for observed in range(60, 72):  # twelve retries with advancing coverage
        latest = run_store.set_status(
            run.run_id,
            sleeve_runs.SleeveRunStatus.AWAITING_DATA,
            error=sleeve_runs.SleeveRunError(
                code="awaiting_data",
                message=f"Required provider evidence is incomplete ({observed}/78).",
                retryable=True,
                context=_coverage(observed, symbols=universe),
            ),
            now=WAITED_AT,
            terminal=False,
        )

    waits = [error for error in latest.errors if error.code == "awaiting_data"]
    assert len(waits) == 12, "every distinct verdict stays in the durable audit trail"
    # Exactly one row keeps the per-symbol coverage, and it is the current one.
    with_coverage = [index for index, error in enumerate(waits) if "market_data" in error.context]
    assert with_coverage == [11]
    assert waits[-1].message.endswith("(71/78).")

    report = cohort_ops.assess_cohort(
        COHORT,
        now_et=NOW_ET,
        runs=[latest],
        expected_members=MEMBERS,
        session_date=SESSION,
        cohort_start=SESSION,
    )
    encoded = json.dumps(cohort_ops.health_payload(report))
    single = len(json.dumps(_coverage(71, symbols=universe)))
    # `run` and `latest_run` are the same session here, so the contract embeds the run
    # view twice. One coverage list per view is the bound; per *retry* is the defect.
    assert encoded.count('"market_data"') == 2
    assert len(encoded) < 3 * single, (
        f"health payload is {len(encoded)} bytes; one coverage list is {single}, so it "
        "is still carrying a copy per retry"
    )
