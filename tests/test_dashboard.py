"""Tests for the local read-only web dashboard (offline; no network)."""

from __future__ import annotations

import http.client
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from schwab_trader import (
    accounts,
    approval,
    cli,
    dashboard,
    evaluation,
    operational_gate,
    promotion,
    reconciliation,
    safety,
    scheduling,
    sleeve_runs,
    sleeves,
    state,
    strategy_registry,
    taxlots,
)
from schwab_trader import auth as oauth
from schwab_trader import client as api
from schwab_trader.config import Settings
from schwab_trader.models import OrderRequest, OrderSide
from schwab_trader.promotion import PromotionVerdict


def _settings(tmp_path: Path, *, account: str = "") -> Settings:
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
        account_hash=account,
    )


def _create_cohort(
    settings: Settings,
    *,
    cohort_id: str = "cohort-a",
    members: tuple[str, ...] = ("bench-spy", "candidate"),
) -> sleeves.SleeveStore:
    store = sleeves.SleeveStore(settings.sleeves_dir)
    for index, name in enumerate(members):
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
            cohort_id=cohort_id,
        )
    return store


def _record_observation(
    store: sleeves.SleeveStore,
    sleeve_id: str,
    session: date,
    *,
    total_value: Decimal | None,
    benchmark_value: Decimal | None,
    status: evaluation.ObservationStatus = evaluation.ObservationStatus.OFFICIAL,
    ready: bool | None = True,
    reasons: tuple[str, ...] = (),
    quote_coverage: Decimal | None = Decimal("1"),
) -> None:
    config = store.get(sleeve_id)
    assert config is not None
    observation = evaluation.OfficialDailyObservation(
        cohort_id=config.cohort_id,
        run_id=f"run-{session.isoformat()}",
        sleeve_id=sleeve_id,
        strategy=config.strategy,
        strategy_hash=config.configuration_hash,
        session_date=session,
        decision_time=datetime.combine(session, datetime.min.time(), tzinfo=UTC),
        valuation_time=datetime.combine(session, datetime.min.time(), tzinfo=UTC),
        status=status,
        total_value=total_value,
        return_pct=Decimal("0") if status is evaluation.ObservationStatus.OFFICIAL else None,
        benchmark_value=benchmark_value,
        quote_coverage=quote_coverage,
        snapshot_ids={"cohort_snapshot": f"snapshot-{session.isoformat()}"},
        readiness_ready=ready,
        readiness_reasons=reasons,
    )
    evaluation.EvaluationStore(store.eval_path(sleeve_id)).record_official_observation(observation)


# --- Collectors -------------------------------------------------------------


def test_collect_sleeves_empty(tmp_path: Path) -> None:
    rows, curves = dashboard.collect_sleeves(_settings(tmp_path), "bench-spy")
    assert rows == []
    assert curves == []


def test_collect_sleeves_lists_created_sleeves(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = sleeves.SleeveStore(settings.sleeves_dir)
    store.create(
        "bench-spy",
        strategy="buy-hold",
        universe=["SPY"],
        starting_cash=Decimal("5000"),
        max_positions=1,
        max_position_fraction=Decimal("1.0"),
    )
    store.create(
        "momentum",
        strategy="momentum",
        universe=[],
        starting_cash=Decimal("5000"),
        max_positions=8,
        max_position_fraction=Decimal("0.10"),
    )
    rows, curves = dashboard.collect_sleeves(settings, "bench-spy")
    names = {r.name for r in rows}
    assert names == {"bench-spy", "momentum"}
    # No recorded cycles yet -> value falls back to starting cash and no curves.
    assert all(r.value == Decimal("5000") for r in rows)
    assert curves == []
    bench_row = next(r for r in rows if r.name == "bench-spy")
    assert bench_row.is_benchmark is True
    assert bench_row.excess_pct is None  # the benchmark has no excess-vs-itself


def test_collect_safety_defaults_to_clear(tmp_path: Path) -> None:
    view = dashboard.collect_safety(_settings(tmp_path))
    assert view.kill_engaged is False
    assert view.trades_today == 0


def test_collect_safety_reports_engaged_kill_switch(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    safety.KillSwitch(settings.kill_switch_path).engage("daily loss limit breached")
    view = dashboard.collect_safety(settings)
    assert view.kill_engaged is True
    assert view.kill_reason == "daily loss limit breached"


# --- Positions (graceful degradation) ---------------------------------------


def test_collect_positions_no_account(tmp_path: Path) -> None:
    view = dashboard.collect_positions(_settings(tmp_path), lambda: _fake_client([], None))
    assert view.available is False
    assert view.message is not None and "account" in view.message.lower()


def test_collect_positions_handles_client_error(tmp_path: Path) -> None:
    settings = _settings(tmp_path, account="secretCC99")

    @contextmanager
    def failing() -> Iterator[api.SchwabClient]:
        raise oauth.OAuthError("token refresh failed")
        yield  # pragma: no cover

    view = dashboard.collect_positions(settings, failing)
    assert view.available is False
    assert view.message is not None and "unavailable" in view.message.lower()
    assert view.account == "****CC99"  # masked, never the full hash


def test_dashboard_client_factory_fails_soft_when_unauthenticated(tmp_path: Path) -> None:
    """Regression: 'serve' without stored tokens must degrade, not crash the render.

    The serve command hands collectors a factory built on _build_client_soft, which
    raises OAuthError (caught by every collector) rather than typer.Exit (caught by
    none, so it used to abort the whole page render).
    """
    settings = _settings(tmp_path, account="secretCC99")

    def factory() -> api.SchwabClient:
        return cli._build_client_soft(settings)

    with pytest.raises(oauth.OAuthError):
        factory()

    view = dashboard.collect_positions(settings, factory)
    assert view.available is False
    assert view.message is not None and "unavailable" in view.message.lower()


def test_collect_positions_returns_live_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, account="secretCC99")
    holdings = [
        accounts.Position(
            symbol="SOFI",
            asset_type="EQUITY",
            long_quantity=Decimal("30"),
            settled_long_quantity=Decimal("30"),
            average_price=Decimal("17.90"),
            market_value=Decimal("513.45"),
            current_day_profit_loss=Decimal("-4.95"),
        )
    ]
    balances = accounts.Balances(
        account_type="CASH",
        liquidation_value=Decimal("1481.26"),
        cash_available_for_trading=Decimal("13.64"),
        cash_available_for_withdrawal=Decimal("13.64"),
    )
    monkeypatch.setattr(dashboard.accounts, "get_positions", lambda _c, _h: holdings)
    monkeypatch.setattr(dashboard.accounts, "get_balances", lambda _c, _h: balances)

    view = dashboard.collect_positions(settings, lambda: _fake_client(holdings, balances))
    assert view.available is True
    assert len(view.positions) == 1
    assert view.positions[0].symbol == "SOFI"
    assert view.liquidation_value == Decimal("1481.26")


@contextmanager
def _fake_client(_holdings: object, _balances: object) -> Iterator[api.SchwabClient]:
    """A stand-in client context manager; the account calls are monkeypatched."""
    yield object()  # type: ignore[misc]


# --- Cohort API --------------------------------------------------------------


def test_collect_cohort_empty_database(tmp_path: Path) -> None:
    view = dashboard.collect_cohort_dashboard(
        _settings(tmp_path), requested_cohort=None, benchmark="bench-spy"
    )
    assert view.available is False
    assert view.selection.available == []
    assert view.comparison.available is False


def test_collect_cohort_legacy_registry_without_new_columns(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.sleeves_dir.mkdir(parents=True)
    with sqlite3.connect(settings.sleeves_dir / "registry.sqlite3") as conn:
        conn.execute(
            "CREATE TABLE sleeves (name TEXT PRIMARY KEY, strategy TEXT NOT NULL, "
            "universe_csv TEXT NOT NULL, starting_cash TEXT NOT NULL, "
            "max_positions INTEGER NOT NULL, max_position_fraction TEXT NOT NULL, "
            "created_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO sleeves VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("legacy", "buy-hold", "SPY", "1000", 1, "1", datetime.now(UTC).isoformat()),
        )
    view = dashboard.collect_cohort_dashboard(
        settings, requested_cohort=None, benchmark="bench-spy"
    )
    assert view.available is False
    assert view.selection.available == []


def test_collect_cohort_legacy_run_database_without_run_tables(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _create_cohort(settings)
    with sqlite3.connect(settings.sleeves_dir / "runs.sqlite3") as conn:
        conn.execute("CREATE TABLE legacy_metadata (value TEXT)")
    view = dashboard.collect_cohort_dashboard(
        settings, requested_cohort="cohort-a", benchmark="bench-spy"
    )
    assert view.available is True
    assert view.run_health.available is True
    assert view.run_health.total_runs == 0
    assert view.operational_gate.available is True
    assert view.operational_gate.status == "fail"
    assert {item.evidence_status for item in view.readiness} == {"unavailable"}


def test_cohort_insufficient_history_partial_and_stale_readiness(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = _create_cohort(settings)
    for session in (date(2026, 7, 20), date(2026, 7, 21)):
        _record_observation(
            store,
            "bench-spy",
            session,
            total_value=Decimal("1000"),
            benchmark_value=Decimal("1000"),
        )
        _record_observation(
            store,
            "candidate",
            session,
            total_value=Decimal("1000"),
            benchmark_value=Decimal("1000"),
        )
    _record_observation(
        store,
        "candidate",
        date(2026, 7, 22),
        total_value=None,
        benchmark_value=Decimal("1000"),
        status=evaluation.ObservationStatus.PARTIAL,
        ready=False,
        # Qualified, as `summarize_readiness` actually persists it. The bare "stale"
        # this used to pass is a value production never emits, and it was hiding a
        # dashboard check that could not match real data (issue #70).
        reasons=("daily_bars:stale",),
        quote_coverage=Decimal("0"),
    )
    view = dashboard.collect_cohort_dashboard(
        settings, requested_cohort="cohort-a", benchmark="bench-spy"
    )
    candidate = next(row for row in view.comparison.sleeves if row.sleeve_id == "candidate")
    assert candidate.maturity == "insufficient_history"
    assert candidate.reliability.partial == 1
    readiness = next(row for row in view.readiness if row.sleeve_id == "candidate")
    assert readiness.observation_status == "partial"
    assert readiness.evidence_status == "stale"
    assert readiness.quote_coverage == Decimal("0")


def test_mature_comparison_distinguishes_zero_return_from_missing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = _create_cohort(settings, members=("bench-spy", "candidate", "missing"))
    sessions = (date(2026, 7, 20), date(2026, 7, 21), date(2026, 7, 22))
    for session in sessions:
        for sleeve_id in ("bench-spy", "candidate"):
            _record_observation(
                store,
                sleeve_id,
                session,
                total_value=Decimal("1000"),
                benchmark_value=Decimal("1000"),
            )
    _record_observation(
        store,
        "missing",
        sessions[0],
        total_value=Decimal("1000"),
        benchmark_value=Decimal("1000"),
    )
    view = dashboard.collect_cohort_dashboard(
        settings, requested_cohort="cohort-a", benchmark="bench-spy"
    )
    candidate = next(row for row in view.comparison.sleeves if row.sleeve_id == "candidate")
    missing = next(row for row in view.comparison.sleeves if row.sleeve_id == "missing")
    assert candidate.maturity == "mature"
    assert candidate.sleeve_return == 0.0
    assert candidate.excess_return == 0.0
    assert missing.maturity == "no_overlap"
    assert missing.sleeve_return is None


def test_cohort_run_health_exposes_partial_failed_and_interrupted(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _create_cohort(settings)
    run_store = sleeve_runs.SleeveRunStore(settings.sleeves_dir / "runs.sqlite3")
    partial = run_store.ensure_run(
        cohort_id="cohort-a",
        session=scheduling.session_for_date(date(2026, 7, 20)),
        expected_members=("bench-spy", "candidate"),
    )
    run_store.set_snapshot(
        partial.run_id,
        snapshot_id="cohort-snapshot-1",
        quote_snapshot_id="quotes-1",
        data_snapshot_ids={"daily_bars": "bars-1"},
    )
    run_store.finish_member(
        partial.run_id, "bench-spy", status=sleeve_runs.MemberRunStatus.COMPLETED
    )
    run_store.finish_member(
        partial.run_id,
        "candidate",
        status=sleeve_runs.MemberRunStatus.INTERRUPTED,
        error=sleeve_runs.SleeveRunError(
            code="interrupted", message="Synthetic interruption.", member_id="candidate"
        ),
    )
    run_store.finalize(partial.run_id)

    failed = run_store.ensure_run(
        cohort_id="cohort-a",
        session=scheduling.session_for_date(date(2026, 7, 21)),
        expected_members=("bench-spy", "candidate"),
    )
    for member in ("bench-spy", "candidate"):
        run_store.finish_member(
            failed.run_id,
            member,
            status=sleeve_runs.MemberRunStatus.FAILED,
            error=sleeve_runs.SleeveRunError(
                code="synthetic_failure", message="Synthetic failure.", member_id=member
            ),
        )
    run_store.finalize(failed.run_id)

    view = dashboard.collect_cohort_dashboard(
        settings, requested_cohort="cohort-a", benchmark="bench-spy"
    )
    assert view.run_health.latest_status == "failed"
    assert {run.status for run in view.run_health.runs} == {"partial", "failed"}
    partial_view = next(run for run in view.run_health.runs if run.status == "partial")
    assert partial_view.snapshot_id == "cohort-snapshot-1"
    assert partial_view.quote_snapshot_id == "quotes-1"
    assert partial_view.data_snapshot_ids == {"daily_bars": "bars-1"}
    interrupted = next(member for member in partial_view.members if member.sleeve_id == "candidate")
    assert interrupted.status == "interrupted"
    assert interrupted.error is not None and interrupted.error.code == "interrupted"


def test_dashboard_keeps_provider_wait_retryable_until_the_actual_deadline(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _create_cohort(settings)
    run_store = sleeve_runs.SleeveRunStore(settings.sleeves_dir / "runs.sqlite3")
    pending = run_store.ensure_run(
        cohort_id="cohort-a",
        session=scheduling.session_for_date(date(2026, 7, 27)),
        expected_members=("bench-spy", "candidate"),
    )
    context = {
        "market_data": {
            "target_session": "2026-07-27",
            "symbols": [
                {
                    "symbol": "SPY",
                    "state": "incomplete",
                    "latest_official_session": "2026-07-24",
                    "evidence_source": "schwab-intraday-derived-daily",
                    "expected_interval_count": 78,
                    "observed_interval_count": 77,
                }
            ],
        }
    }
    run_store.set_status(
        pending.run_id,
        sleeve_runs.SleeveRunStatus.AWAITING_DATA,
        error=sleeve_runs.SleeveRunError(
            code="awaiting_data",
            message="Required provider evidence is incomplete.",
            retryable=True,
            context=context,
        ),
        terminal=False,
    )

    retryable = dashboard.collect_cohort_dashboard(
        settings,
        requested_cohort="cohort-a",
        benchmark="bench-spy",
        now_et=datetime(2026, 7, 27, 18, 45),
    )
    phase = retryable.phase
    assert phase is not None and phase.timing_state == "awaiting-provider-data"
    assert phase.headline == "Awaiting provider data — retryable"
    assert retryable.run_health.awaiting_provider_data_runs == 1
    assert retryable.run_health.overdue_runs == 0
    run_view = retryable.run_health.runs[0]
    assert run_view.timing == "awaiting-provider-data"
    assert run_view.retry_deadline_et == datetime(2026, 7, 28, 16, 0)
    assert run_view.errors[0].context == context
    reproducibility = next(
        rule for rule in retryable.operational_gate.rules if rule.rule == "reproducibility"
    )
    assert reproducibility.presentation == "awaiting-evidence"

    terminal = dashboard.collect_cohort_dashboard(
        settings,
        requested_cohort="cohort-a",
        benchmark="bench-spy",
        now_et=datetime(2026, 7, 28, 16, 0),
    )
    assert terminal.phase is not None and terminal.phase.timing_state == "overdue"
    assert terminal.run_health.awaiting_provider_data_runs == 0
    assert terminal.run_health.overdue_runs == 1


@pytest.mark.parametrize(
    "status",
    (
        operational_gate.GateStatus.PASS,
        operational_gate.GateStatus.FAIL,
        operational_gate.GateStatus.INSUFFICIENT_HISTORY,
    ),
)
def test_operational_gate_results_and_reasons_are_passed_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: operational_gate.GateStatus,
) -> None:
    settings = _settings(tmp_path)
    _create_cohort(settings)
    result = operational_gate.OperationalGateResult(
        cohort_id="cohort-a",
        status=status,
        summary=f"Synthetic {status.value} summary.",
        rules=(
            operational_gate.RuleAssessment(
                rule=operational_gate.GateRule.SESSION_HISTORY,
                status=status,
                reason="Operator-readable synthetic reason.",
                evidence=("synthetic evidence",),
            ),
        ),
    )
    monkeypatch.setattr(
        dashboard.operational_gate,
        "assess_operational_usefulness",
        lambda **_kwargs: result,
    )
    view = dashboard.collect_cohort_dashboard(
        settings, requested_cohort="cohort-a", benchmark="bench-spy"
    )
    assert view.operational_gate.available is True
    assert view.operational_gate.status == status.value
    assert view.operational_gate.operationally_useful is (
        status is operational_gate.GateStatus.PASS
    )
    assert view.operational_gate.rules[0].reason == "Operator-readable synthetic reason."
    assert view.operational_gate.live_trading_authorized is False


# --- Sparkline + HTML rendering ---------------------------------------------


def test_sparkline_needs_two_points() -> None:
    assert "svg" not in dashboard.sparkline_svg([])
    assert "svg" not in dashboard.sparkline_svg([Decimal("100")])


def test_sparkline_colors_by_direction() -> None:
    up = dashboard.sparkline_svg([Decimal("100"), Decimal("110")])
    down = dashboard.sparkline_svg([Decimal("110"), Decimal("100")])
    assert "#2f9e44" in up  # green when the series ends higher
    assert "#e03131" in down  # red when it ends lower
    assert "<svg" in up and "polyline" in up


def _sample_data() -> dashboard.DashboardData:
    return dashboard.DashboardData(
        generated_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        benchmark="bench-spy",
        sleeves=[
            dashboard.SleeveRow(
                rank=1,
                sleeve_id="sleeve-momentum",
                name="momentum",
                strategy="momentum",
                scope=dashboard.SleeveScope.LEGACY,
                cohort_id=None,
                starting_capital=Decimal("5000"),
                cycles=5,
                trades=3,
                value=Decimal("5200"),
                return_pct=Decimal("4.00"),
                excess_pct=Decimal("1.50"),
                excess_benchmark="bench-spy",
                max_drawdown_pct=Decimal("2.0"),
                sharpe=Decimal("1.20"),
                is_benchmark=False,
            )
        ],
        curves=[
            dashboard.EquitySeries(
                sleeve_id="sleeve-momentum",
                name="momentum",
                points=[Decimal("5000"), Decimal("5200")],
            )
        ],
        safety=dashboard.SafetyView(
            kill_engaged=False,
            kill_since=None,
            kill_reason=None,
            trades_today=0,
            realized_pnl=Decimal("0"),
            start_equity=None,
            capital_cap=Decimal("0"),
            daily_loss_limit=Decimal("0"),
            max_trades_per_day=0,
            max_order_notional=Decimal("100.00"),
        ),
        positions=dashboard.PositionsView(
            account="****CC99",
            available=True,
            positions=[
                dashboard.PositionRow(
                    symbol="SOFI",
                    quantity=Decimal("30"),
                    settled=Decimal("30"),
                    average_price=Decimal("17.90"),
                    market_value=Decimal("513.45"),
                    day_pl=Decimal("-4.95"),
                )
            ],
            liquidation_value=Decimal("1481.26"),
            cash_available_for_trading=Decimal("13.64"),
            cash_available_for_withdrawal=Decimal("13.64"),
        ),
        summary=dashboard.SummaryView(
            account="****CC99",
            liquidation_value=Decimal("1481.26"),
            day_pl=Decimal("-4.95"),
            cash_available=Decimal("13.64"),
            kill_engaged=False,
        ),
    )


def test_render_page_includes_all_panels() -> None:
    page = dashboard.render_page(_sample_data())
    for heading in (
        "Positions (live)",
        "Orders (live)",
        "Sleeve comparison",
        "Strategy validation",
        "Pending approvals",
        "Market regime",
        "Autonomous safety",
        "Reconciliation health",
        "Tax lots",
        "Recent activity (audit)",
    ):
        assert heading in page, f"missing panel: {heading}"
    # Hero summary bar and the engage-only kill form are present.
    assert "Liquidation value" in page
    assert 'action="/kill"' in page


def test_render_page_shows_killbar_when_engaged() -> None:
    data = _sample_data()
    data.safety.kill_engaged = True
    data.summary.kill_engaged = True
    page = dashboard.render_page(data)
    assert "KILL SWITCH ENGAGED" in page
    # When engaged there is no engage form - resume stays on the CLI.
    assert 'action="/kill"' not in page
    assert "safety resume" in page


def test_render_page_includes_sections_and_masks_account() -> None:
    page = dashboard.render_page(_sample_data())
    assert "<!doctype html>" in page
    assert "****CC99" in page
    assert "momentum" in page  # sleeve row
    assert "SOFI" in page  # position row
    assert "read-only" in page
    assert "<svg" in page  # equity sparkline rendered
    # The masked tail is shown but never a fuller account identifier.
    assert "secretCC99" not in page


# --- Server (read-only guarantees) ------------------------------------------


@contextmanager
def _running_server(tmp_path: Path, *, frontend_dir: Path | None = None) -> Iterator[int]:
    # Default to a nonexistent dist dir so tests exercise the legacy fallback
    # deterministically (the repo's real frontend/dist may or may not be built).
    server = dashboard.make_server(
        _settings(tmp_path),
        host="127.0.0.1",
        port=0,
        client_factory=None,
        frontend_dir=frontend_dir or (tmp_path / "no-dist"),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_server_get_index_ok(tmp_path: Path) -> None:
    with _running_server(tmp_path) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
    assert resp.status == 200
    assert "schwab-trader" in body


def test_server_unknown_path_404(tmp_path: Path) -> None:
    with _running_server(tmp_path) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/orders/place")
        resp = conn.getresponse()
        resp.read()
        conn.close()
    assert resp.status == 404


def test_server_rejects_post(tmp_path: Path) -> None:
    with _running_server(tmp_path) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/", body=b"{}")
        resp = conn.getresponse()
        resp.read()
        conn.close()
    assert resp.status == 405  # read-only: no state-changing methods


def test_make_server_refuses_non_loopback_without_force(tmp_path: Path) -> None:
    with pytest.raises(dashboard.NonLoopbackHostError):
        dashboard.make_server(_settings(tmp_path), host="0.0.0.0", port=0)


# --- New panels -------------------------------------------------------------


def _verdict(strategy: str, *, pass_rate: float, excess: str | None) -> PromotionVerdict:
    return PromotionVerdict(
        strategy=strategy,
        universe="large-cap",
        created_at=datetime(2026, 7, 20, tzinfo=UTC),
        folds=6,
        passing_folds=round(pass_rate * 6),
        pass_rate=pass_rate,
        mean_return_pct=Decimal("8"),
        worst_return_pct=Decimal("-1"),
        mean_excess_pct=Decimal(excess) if excess is not None else None,
        min_pass_rate=0.6,
    )


def test_collect_validation_sorts_validated_first(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = promotion.PromotionStore(settings.promotion_db_path)
    store.record(_verdict("laggard", pass_rate=0.83, excess="-2"))  # clears gates but lags
    store.record(_verdict("winner", pass_rate=0.83, excess="3.5"))  # validated
    view = dashboard.collect_validation(settings)
    assert [r.strategy for r in view.rows] == ["winner", "laggard"]
    assert view.rows[0].validated is True
    assert view.rows[1].validated is False  # lags the benchmark -> not validated


def test_collect_approvals_lists_pending(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = approval.ApprovalStore(settings.approval_db_path)
    request = OrderRequest(
        side=OrderSide.BUY, symbol="AAPL", quantity=1, limit_price=Decimal("100")
    )
    store.issue(
        account_hash="HASH1234", request=request, ttl=timedelta(minutes=45), rationale="mom"
    )
    view = dashboard.collect_approvals(settings)
    assert len(view.rows) == 1
    assert view.rows[0].describe.startswith("BUY 1 AAPL")
    assert 0 <= view.rows[0].expires_in_min <= 45


def test_collect_tax_lots_classifies_term(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = taxlots.TaxLotStore(settings.tax_lots_db_path)
    store.record_purchase(
        symbol="AAPL",
        quantity=Decimal("5"),
        cost_per_share=Decimal("100"),
        acquired_at=datetime.now(UTC) - timedelta(days=500),
    )
    view = dashboard.collect_tax_lots(settings)
    assert len(view.rows) == 1
    assert view.rows[0].long_term is True  # held > 1 year


def test_collect_audit_returns_recent(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    state.StateStore(settings.state_db_path).append_audit(
        command="order submit", event="submitted", detail="BUY 1 AAPL"
    )
    view = dashboard.collect_audit(settings)
    assert len(view.rows) == 1
    assert view.rows[0].event == "submitted"


def test_collect_reconciliation_returns_latest_health(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    reconciliation.ReconciliationStore(settings.state_db_path).record_run(
        reconciliation.ReconciliationReport(
            started_at=datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
            completed_at=datetime(2026, 7, 21, 12, 1, tzinfo=UTC),
            orders_seen=4,
            transitions=2,
            fills_applied=1,
            discrepancies=[
                reconciliation.Discrepancy(
                    severity="warning",
                    kind="position_quantity_mismatch",
                    subject="AAPL",
                    detail="test",
                )
            ],
        )
    )
    view = dashboard.collect_reconciliation(settings)
    assert view.orders_seen == 4
    assert view.transitions == 2
    assert view.fills_applied == 1
    assert view.discrepancies == 1


def test_collect_orders_disabled_without_client(tmp_path: Path) -> None:
    view = dashboard.collect_orders(_settings(tmp_path, account="HASH99"), None)
    assert view.available is False
    assert view.message is not None


def test_api_data_returns_json(tmp_path: Path) -> None:
    with _running_server(tmp_path) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/data")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
    assert resp.status == 200
    assert resp.getheader("Content-Type", "").startswith("application/json")
    payload = json.loads(body)
    for key in (
        "summary",
        "sleeves",
        "safety",
        "positions",
        "orders",
        "validation",
        "approvals",
        "regime",
        "tax_lots",
        "audit",
        "reconciliation",
    ):
        assert key in payload, f"missing key: {key}"


def test_api_cohort_selection_is_additive_and_exposes_definitions(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = _create_cohort(settings)
    with _running_server(tmp_path) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/data?cohort=cohort-a")
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()
    assert response.status == 200
    assert payload["api_version"] == "3.5"
    assert payload["cohort"]["contract_version"] == "2.4"
    assert payload["cohort"]["selection"]["selected"] == "cohort-a"
    # ``member_sleeves`` now carries stable identities; names travel alongside.
    assert payload["cohort"]["identity"]["member_names"] == ["bench-spy", "candidate"]
    assert payload["cohort"]["identity"]["member_sleeves"] == [
        store.get("bench-spy").identity,
        store.get("candidate").identity,
    ]
    assert payload["cohort"]["sleeve_names"] == {
        store.get("bench-spy").identity: "bench-spy",
        store.get("candidate").identity: "candidate",
    }
    definitions = payload["cohort"]["sleeve_definitions"]
    assert definitions[0]["configuration_hash"] == store.get("bench-spy").configuration_hash
    assert definitions[0]["sleeve_name"] == "bench-spy"
    assert definitions[0]["definition"] is not None
    # Phase is presentation only; the gate keeps failing closed.
    phase = payload["cohort"]["phase"]
    assert phase["phase"] in {"scheduled", "collecting", "review-ready", "attention-needed"}
    gate = payload["cohort"]["operational_gate"]
    assert gate["investment_alpha_assessed"] is False
    assert gate["live_trading_authorized"] is False
    # 2.4: the review section is served read-only and reports "nothing recorded" rather
    # than being absent, so the UI can tell an unstarted review from an unreadable one.
    review = payload["cohort"]["cohort_review"]
    assert review["available"] is True
    assert review["recorded_check_count"] == 0
    assert review["differences"] == []
    assert review["decisions"] == []
    for existing in ("summary", "sleeves", "orders", "safety", "reconciliation"):
        assert existing in payload


def test_api_unknown_cohort_selection_fails_soft(tmp_path: Path) -> None:
    _create_cohort(_settings(tmp_path))
    with _running_server(tmp_path) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/data?cohort=missing")
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()
    assert response.status == 200
    assert payload["cohort"]["available"] is False
    assert payload["cohort"]["selection"]["requested"] == "missing"
    assert payload["cohort"]["selection"]["selected"] is None


def test_serves_built_frontend_when_present(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>react-app</title>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log('hi')", encoding="utf-8")
    with _running_server(tmp_path, frontend_dir=dist) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.request("GET", "/assets/app.js")
        asset = conn.getresponse()
        asset_type = asset.getheader("Content-Type", "")
        asset.read()
        conn.close()
    assert resp.status == 200
    assert "react-app" in body  # the built SPA, not the legacy page
    assert asset.status == 200
    assert asset_type.startswith("text/javascript")


def test_static_path_traversal_blocked(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("ok", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("do-not-serve", encoding="utf-8")
    with _running_server(tmp_path, frontend_dir=dist) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/../secret.txt")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
    assert resp.status == 404
    assert "do-not-serve" not in body


@pytest.mark.parametrize("method", ("PUT", "PATCH", "DELETE"))
def test_only_post_kill_can_mutate_dashboard(tmp_path: Path, method: str) -> None:
    settings = _settings(tmp_path)
    server = dashboard.make_server(settings, host="127.0.0.1", port=0, client_factory=None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        conn.request(method, "/kill", body="reason=must-not-engage")
        response = conn.getresponse()
        response.read()
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert response.status == 405
    assert safety.KillSwitch(settings.kill_switch_path).status().engaged is False


def test_kill_route_engages_switch(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    server = dashboard.make_server(settings, host="127.0.0.1", port=0, client_factory=None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST",
            "/kill",
            body="reason=panic",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp = conn.getresponse()
        resp.read()
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert resp.status == 303  # redirect back to the dashboard
    ks = safety.KillSwitch(settings.kill_switch_path).status()
    assert ks.engaged is True
    assert ks.reason == "panic"
