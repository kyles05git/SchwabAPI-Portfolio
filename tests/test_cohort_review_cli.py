"""End-to-end operator flow for `schwab-trader cohort review`, offline.

Everything runs against a real local layout under ``tmp_path``: a real sleeve registry,
a real durable run store, real evaluation stores, and the real local review database the
storage factory builds when no shared database is configured. The CLI commands are
invoked exactly as an operator would type them.

No ``.env`` is read (the autouse isolation fixture in ``conftest`` guarantees it), no
shared database is reachable, and nothing here can run a cohort, submit an order, or
send a notification.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from schwab_trader import (
    dashboard,
    evaluation,
    market_calendar,
    scheduling,
    sleeve_runs,
    sleeves,
    strategy_registry,
)
from schwab_trader.cli import app
from schwab_trader.config import Settings
from schwab_trader.operational_gate import GateRule, GateStatus
from schwab_trader.storage import factory as storage_factory

runner = CliRunner()
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

COHORT = "cohort-review-test"
MEMBERS = ("bench-spy", "candidate")
START = date(2026, 7, 27)

#: Well past the thirtieth session's close and grace period, so every recorded run is
#: due evidence under the real scheduler rule.
NOW_ET = datetime(2026, 9, 30, 18, 0)
NOW_ET_ARG = NOW_ET.isoformat()


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        token_path=tmp_path / "tokens.json",
        state_db_path=tmp_path / "state.sqlite3",
        log_path=tmp_path / "logs" / "app.log",
        sleeves_dir=tmp_path / "sleeves",
        cohort_review_db_path=tmp_path / "cohort_reviews.sqlite3",
        kill_switch_path=tmp_path / "KILL_SWITCH",
        agent_activity_db_path=tmp_path / "agent_activity.sqlite3",
        promotion_db_path=tmp_path / "promotion.sqlite3",
        approval_db_path=tmp_path / "approvals.sqlite3",
        tax_lots_db_path=tmp_path / "taxlots.sqlite3",
        cohort_id=COHORT,
    )


def _sessions(count: int) -> list[date]:
    days: list[date] = []
    cursor = START
    while len(days) < count:
        if market_calendar.is_trading_day(cursor):
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _build_cohort(settings: Settings, *, sessions: int) -> sleeves.SleeveStore:
    """A real local cohort with `sessions` completed runs and official observations."""
    store = sleeves.SleeveStore(settings.sleeves_dir)
    for name in MEMBERS:
        symbol = "SPY" if name == "bench-spy" else "QQQ"
        store.create(
            name,
            strategy="buy-hold",
            universe=[symbol],
            starting_cash=Decimal("1000"),
            max_positions=1,
            max_position_fraction=Decimal("1"),
            definition=strategy_registry.make_definition(
                "buy-hold",
                universe_definition=[symbol],
                benchmark_symbol_or_sleeve="bench-spy",
            ),
            cohort_id=COHORT,
        )

    run_store = sleeve_runs.SleeveRunStore(settings.sleeves_dir / "runs.sqlite3")
    for session in _sessions(sessions):
        recorded_at = datetime.combine(session, datetime.min.time(), tzinfo=UTC).replace(hour=21)
        run = run_store.ensure_run(
            cohort_id=COHORT,
            session=scheduling.session_for_date(session),
            expected_members=list(MEMBERS),
        )
        run_store.set_snapshot(
            run.run_id,
            snapshot_id=f"snapshot-{session.isoformat()}",
            quote_snapshot_id=f"quotes-{session.isoformat()}",
            data_snapshot_ids={},
        )
        for name in MEMBERS:
            run_store.start_member(run.run_id, name, now=recorded_at)
            run_store.finish_member(
                run.run_id,
                name,
                status=sleeve_runs.MemberRunStatus.COMPLETED,
                now=recorded_at,
            )
            config = store.get(name)
            assert config is not None
            evaluation.EvaluationStore(store.eval_path(name)).record_official_observation(
                evaluation.OfficialDailyObservation(
                    cohort_id=COHORT,
                    run_id=run.run_id,
                    sleeve_id=name,
                    strategy=config.strategy,
                    strategy_hash=config.configuration_hash,
                    session_date=session,
                    decision_time=recorded_at,
                    valuation_time=recorded_at,
                    status=evaluation.ObservationStatus.OFFICIAL,
                    total_value=Decimal("1000"),
                    return_pct=Decimal("0"),
                    benchmark_value=Decimal("1000"),
                    quote_coverage=Decimal("1"),
                    snapshot_ids={
                        "cohort_snapshot": f"snapshot-{session.isoformat()}",
                        "quotes": f"quotes-{session.isoformat()}",
                    },
                    readiness_ready=True,
                    readiness_reasons=(),
                )
            )
        run_store.finalize(run.run_id, now=recorded_at)
    return store


@pytest.fixture
def local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """A ready 30-session cohort, with the CLI pointed at it."""
    settings = _settings(tmp_path)
    _build_cohort(settings, sessions=30)
    monkeypatch.setattr("schwab_trader.cli.get_settings", lambda: settings)
    return settings


def _run(*args: str) -> str:
    result = runner.invoke(app, list(args), env={"COLUMNS": "220"})
    assert result.exit_code == 0, result.output
    return _ANSI.sub("", result.output)


def _run_json(*args: str) -> dict:
    return json.loads(_ANSI.sub("", _run(*args, "--json")))


def _fails(*args: str) -> str:
    result = runner.invoke(app, list(args), env={"COLUMNS": "220"})
    assert result.exit_code != 0, result.output
    return _ANSI.sub("", result.output)


def _review(*extra: str) -> list[str]:
    return ["cohort", "review", *extra, "--cohort", COHORT, "--now", NOW_ET_ARG]


def _record_everything(sessions: int = 30) -> None:
    for session in _sessions(sessions):
        for name in MEMBERS:
            for area in ("cash", "positions", "valuation"):
                _run(
                    *_review(
                        "record",
                        "--sleeve",
                        name,
                        "--session",
                        session.isoformat(),
                        "--area",
                        area,
                    )
                )


# --- the commands exist and describe themselves ------------------------------


def test_the_review_command_group_is_registered() -> None:
    output = _ANSI.sub("", runner.invoke(app, ["cohort", "review", "--help"]).output)
    for command in ("pending", "record", "note", "decide", "show"):
        assert command in output


# --- recording ---------------------------------------------------------------


def test_recording_a_check_is_idempotent_through_the_cli(local: Settings) -> None:
    args = _review(
        "record", "--sleeve", "candidate", "--session", START.isoformat(), "--area", "cash"
    )
    first = _run_json(*args)
    second = _run_json(*args)

    assert first["status"] == "recorded"
    assert second["status"] == "unchanged"
    assert second["entry_id"] == first["entry_id"]
    assert second["revision"] == 0


def test_correcting_a_check_requires_supersede_and_keeps_the_original(
    local: Settings,
) -> None:
    base = _review(
        "record", "--sleeve", "candidate", "--session", START.isoformat(), "--area", "cash"
    )
    _run_json(*base)

    refused = _fails(*base, "--finding", "difference", "--summary", "Trailing 0.04.")
    assert "supersede" in refused

    corrected = _run_json(
        *base, "--finding", "difference", "--summary", "Trailing 0.04.", "--supersede"
    )
    assert corrected["status"] == "superseded"
    assert corrected["revision"] == 1

    payload = _run_json(*_review("show"))
    assert len(payload["checks"]) == 1
    assert len(payload["superseded_checks"]) == 1
    assert payload["superseded_checks"][0]["finding"] == "matched"


def test_an_unknown_session_or_area_is_refused(local: Settings) -> None:
    assert "No due official observation" in _fails(
        *_review(
            "record", "--sleeve", "candidate", "--session", "2026-01-05", "--area", "cash"
        )
    )
    assert "accounting area" in _fails(
        *_review(
            "record", "--sleeve", "candidate", "--session", START.isoformat(), "--area", "equity"
        )
    )
    assert "Unknown sleeve" in _fails(
        *_review(
            "record", "--sleeve", "stranger", "--session", START.isoformat(), "--area", "cash"
        )
    )


def test_a_difference_without_an_explanation_is_recorded_and_flagged(
    local: Settings,
) -> None:
    output = _run(
        *_review(
            "record",
            "--sleeve",
            "candidate",
            "--session",
            START.isoformat(),
            "--area",
            "valuation",
            "--finding",
            "difference",
            "--summary",
            "Recorded equity differs by 0.01.",
        )
    )

    assert "no explanation" in output
    payload = _run_json(*_review("show"))
    assert payload["unexplained_difference_count"] == 1


# --- notes -------------------------------------------------------------------


def test_an_identical_note_is_recorded_once_through_the_cli(local: Settings) -> None:
    args = _review("note", "--note", "Traced the settlement timing.")

    assert _run_json(*args)["status"] == "recorded"
    assert _run_json(*args)["status"] == "unchanged"
    assert len(_run_json(*_review("show"))["notes"]) == 1


def test_a_session_scoped_note_requires_its_sleeve(local: Settings) -> None:
    assert "requires --sleeve" in _fails(
        *_review("note", "--note", "About that session.", "--session", START.isoformat())
    )


# --- decisions ---------------------------------------------------------------


def test_a_decision_is_refused_before_the_review_is_due(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    _build_cohort(settings, sessions=3)
    monkeypatch.setattr("schwab_trader.cli.get_settings", lambda: settings)

    output = _fails(
        *_review("decide", "--sleeve", "candidate", "--action", "keep", "--rationale", "Fine.")
    )

    assert "not due" in output
    assert _run_json(*_review("show"))["decisions"] == []


def test_a_decision_records_supersedes_and_never_authorizes_live_trading(
    local: Settings,
) -> None:
    base = _review(
        "decide",
        "--sleeve",
        "candidate",
        "--action",
        "keep",
        "--rationale",
        "Complete and reproducible over thirty sessions.",
    )
    assert _run_json(*base)["status"] == "recorded"
    assert _run_json(*base)["status"] == "unchanged"

    modify = _review(
        "decide",
        "--sleeve",
        "candidate",
        "--action",
        "modify",
        "--rationale",
        "Turnover dominates the modeled result.",
    )
    assert "supersede" in _fails(*modify)

    output = _run(*modify, "--supersede")
    assert "superseded" in output
    # The command has to say, in plain words, that this changed nothing operational.
    assert "never authorizes live trading" in output or "live trading remains" in output

    payload = _run_json(*_review("show"))
    assert [item["action"] for item in payload["decisions"]] == ["modify"]
    assert [item["action"] for item in payload["superseded_decisions"]] == ["keep"]


def test_an_invalid_action_is_refused(local: Settings) -> None:
    assert "operator action" in _fails(
        *_review(
            "decide", "--sleeve", "candidate", "--action", "promote", "--rationale", "No."
        )
    )


# --- pending -----------------------------------------------------------------


def test_pending_reports_outstanding_work_and_exits_nonzero_when_the_review_is_due(
    local: Settings,
) -> None:
    result = runner.invoke(app, _review("pending"), env={"COLUMNS": "220"})

    assert result.exit_code == 1
    output = _ANSI.sub("", result.output)
    assert "Unreviewed observations: 60" in output
    assert "Sleeves without a current decision: 2" in output


def test_pending_is_clean_once_everything_is_recorded(local: Settings) -> None:
    _record_everything()
    for name in MEMBERS:
        _run(
            *_review(
                "decide",
                "--sleeve",
                name,
                "--action",
                "keep",
                "--rationale",
                "Reconciled and reproducible across thirty sessions.",
            )
        )

    result = runner.invoke(app, _review("pending"), env={"COLUMNS": "220"})

    assert result.exit_code == 0
    payload = _run_json(*_review("pending"))
    assert payload["pending_observations"] == []
    assert payload["undecided_sleeves"] == []
    assert payload["reviewed_observations"] == 60


# --- the gate and the dashboard actually consume the records -----------------


def test_a_recorded_review_reaches_the_gate_and_the_dashboard(local: Settings) -> None:
    _record_everything()
    for name in MEMBERS:
        _run(
            *_review(
                "decide",
                "--sleeve",
                name,
                "--action",
                "keep",
                "--rationale",
                "Reconciled and reproducible across thirty sessions.",
            )
        )

    view = dashboard.collect_cohort_dashboard(
        local,
        requested_cohort=COHORT,
        benchmark="bench-spy",
        now_et=NOW_ET,
    )

    gate = view.operational_gate
    rules = {item.rule: item for item in gate.rules}
    # The two rules that could never pass before, because nothing produced their input.
    assert rules[GateRule.ACCOUNTING_STATES.value].status == GateStatus.PASS.value
    assert rules[GateRule.ACCOUNTING_STATES.value].awaiting_evidence is False
    assert rules[GateRule.OPERATOR_DECISIONS.value].status == GateStatus.PASS.value
    # ...and the authorization contract is unchanged by any of it.
    assert gate.investment_alpha_assessed is False
    assert gate.live_trading_authorized is False

    review = view.cohort_review
    assert review.available is True
    assert review.reviewed_observations == 60
    assert review.official_observations == 60
    assert review.recorded_check_count == 180
    assert review.decided_sleeve_count == 2
    assert review.unexplained_difference_count == 0
    assert review.review_due is True


def test_an_unreadable_observation_store_cannot_report_a_clean_accounting_review(
    local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed observation read must never render as a completed, clean review.

    The recorded review and the official observations live in different stores. When the
    evaluation store cannot be read the dashboard has zero official observations, but
    every recorded check is still there — and comparing an empty checked set against an
    empty official set is a vacuous pass, not evidence. "Could not read the evidence" and
    "the evidence was reviewed and was clean" are opposite answers.
    """
    _record_everything()
    for name in MEMBERS:
        _run(
            *_review(
                "decide",
                "--sleeve",
                name,
                "--action",
                "keep",
                "--rationale",
                "Reconciled and reproducible across thirty sessions.",
            )
        )

    def unreadable(*_args: object, **_kwargs: object) -> object:
        raise OSError("simulated evaluation-store fault")

    monkeypatch.setattr(storage_factory, "evaluation_store", unreadable)

    view = dashboard.collect_cohort_dashboard(
        local,
        requested_cohort=COHORT,
        benchmark="bench-spy",
        now_et=NOW_ET,
    )

    accounting = {item.rule: item for item in view.operational_gate.rules}[
        GateRule.ACCOUNTING_STATES.value
    ]
    # Fails closed exactly as it did before any review evidence was wired in.
    assert accounting.status == GateStatus.FAIL.value
    assert accounting.presentation != "healthy"
    assert accounting.awaiting_evidence is True
    assert "reviewed" not in accounting.reason

    # ...and the unreadable read stays its own distinct state, never "nothing has been
    # reviewed": the recorded checks are still served, against zero readable observations.
    assert view.comparison.message == "Official cohort observations are unavailable."
    assert view.cohort_review.available is True
    assert view.cohort_review.recorded_check_count == 180
    assert view.cohort_review.official_observations == 0


def test_an_unreviewed_cohort_still_awaits_evidence_rather_than_passing(
    local: Settings,
) -> None:
    view = dashboard.collect_cohort_dashboard(
        local,
        requested_cohort=COHORT,
        benchmark="bench-spy",
        now_et=NOW_ET,
    )

    rules = {item.rule: item for item in view.operational_gate.rules}
    accounting = rules[GateRule.ACCOUNTING_STATES.value]
    decisions = rules[GateRule.OPERATOR_DECISIONS.value]
    assert accounting.status == GateStatus.FAIL.value
    assert accounting.awaiting_evidence is True
    assert decisions.awaiting_evidence is True

    review = view.cohort_review
    # Readable and empty is a different state from unreadable, and it says so.
    assert review.available is True
    assert review.recorded_check_count == 0
    assert review.message is not None
    assert review.official_observations == 60


def test_an_unexplained_difference_makes_the_gate_actionable(local: Settings) -> None:
    _record_everything()
    _run(
        *_review(
            "record",
            "--sleeve",
            "candidate",
            "--session",
            START.isoformat(),
            "--area",
            "valuation",
            "--finding",
            "difference",
            "--summary",
            "Recorded equity differs from the recomputed valuation by 0.01.",
            "--supersede",
        )
    )
    for name in MEMBERS:
        _run(
            *_review(
                "decide",
                "--sleeve",
                name,
                "--action",
                "keep",
                "--rationale",
                "Reconciled apart from one open item.",
            )
        )

    view = dashboard.collect_cohort_dashboard(
        local,
        requested_cohort=COHORT,
        benchmark="bench-spy",
        now_et=NOW_ET,
    )

    accounting = {item.rule: item for item in view.operational_gate.rules}[
        GateRule.ACCOUNTING_STATES.value
    ]
    assert accounting.status == GateStatus.FAIL.value
    # A recorded, unexplained difference is a real defect, not absent evidence.
    assert accounting.awaiting_evidence is False
    assert accounting.presentation == "needs-attention"

    review = view.cohort_review
    assert review.unexplained_difference_count == 1
    assert len(review.differences) == 1
    assert review.differences[0].explained is False
    assert len(review.superseded_checks) == 1


def test_no_review_action_touches_cohort_membership_or_paper_state(
    local: Settings,
) -> None:
    """The prohibition the Issue is most explicit about, asserted end to end."""
    store = sleeves.SleeveStore(local.sleeves_dir)
    before = {config.name: config.model_dump_json() for config in store.list()}
    run_store = sleeve_runs.SleeveRunStore(local.sleeves_dir / "runs.sqlite3")
    runs_before = [run.model_dump_json() for run in run_store.list(cohort_id=COHORT, limit=100)]
    observations_before = [
        item.model_dump_json()
        for name in MEMBERS
        for item in evaluation.EvaluationStore(store.eval_path(name)).official_observations(
            limit=1000
        )
    ]

    _record_everything(sessions=2)
    _run(*_review("note", "--note", "Checked the first two sessions."))
    _run(
        *_review(
            "decide",
            "--sleeve",
            "candidate",
            "--action",
            "retire",
            "--rationale",
            "Research disposition: not worth carrying into the next cohort.",
        )
    )

    after_store = sleeves.SleeveStore(local.sleeves_dir)
    assert {config.name: config.model_dump_json() for config in after_store.list()} == before
    assert [
        run.model_dump_json() for run in run_store.list(cohort_id=COHORT, limit=100)
    ] == runs_before
    assert [
        item.model_dump_json()
        for name in MEMBERS
        for item in evaluation.EvaluationStore(after_store.eval_path(name)).official_observations(
            limit=1000
        )
    ] == observations_before
    # A `retire` decision retires nothing: the sleeve is still a cohort member.
    assert sorted(config.name for config in after_store.list()) == sorted(MEMBERS)
