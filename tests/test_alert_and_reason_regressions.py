"""Regressions for three post-merge review findings (issue #70).

Each of these passed against the defective code, which is the point: the whole suite
stayed green through all three because the fixtures used values production never emits
and nobody made the alert store fail. Every test here fails on merged `f8dcd7a`.

Offline and deterministic. No `.env`, database, broker, or SMTP connection is reachable;
the alert store's failures are injected locally.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from schwab_trader import cohort_ops, dashboard, scheduling, strategy_registry
from schwab_trader.cohort_alerts import (
    AlertKind,
    AlertStatus,
    CohortAlertService,
    CohortAlertStore,
)
from schwab_trader.data_contracts import BarBatch, BarObservation, Provenance, TimingPolicy
from schwab_trader.data_readiness import (
    DataKind,
    DataRequirement,
    ReasonCode,
    SourceProbe,
    evaluate_readiness,
)
from schwab_trader.evaluation import (
    EvaluationStore,
    ObservationStatus,
    OfficialDailyObservation,
    summarize_readiness,
)
from schwab_trader.market_data import Quote
from schwab_trader.notify import NotifyMessage
from schwab_trader.safety import KillSwitch
from schwab_trader.sleeve_runs import (
    CohortSnapshot,
    MemberRunStatus,
    SleeveRunOrchestrator,
    SleeveRunStatus,
    SleeveRunStore,
)
from schwab_trader.sleeves import SleeveStore

COHORT = "cohort-regressions"
MONDAY = date(2026, 7, 27)
FRIDAY = date(2026, 7, 24)
CLOSE_UTC = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)
EVENING_ET = datetime(2026, 7, 27, 16, 30)
NEXT_EVENING_ET = datetime(2026, 7, 28, 16, 30)


# ---------------------------------------------------------------------------
# 1. CohortAlertService.emit must never raise, including from bookkeeping
# ---------------------------------------------------------------------------


class _Boom(RuntimeError):
    """A durable-store failure, e.g. a dropped connection mid-command."""


class _BrokenStore(CohortAlertStore):
    """An alert store whose bookkeeping calls fail on demand."""

    def __init__(self, path, *, fail_on: set[str]) -> None:
        super().__init__(path)
        self.fail_on = fail_on

    def mark_sent(self, key: str, *, now: datetime | None = None):
        if "mark_sent" in self.fail_on:
            raise _Boom("mark_sent")
        return super().mark_sent(key, now=now)

    def mark_failed(self, key: str, *, reason: str, now: datetime | None = None):
        if "mark_failed" in self.fail_on:
            raise _Boom("mark_failed")
        return super().mark_failed(key, reason=reason, now=now)


class _Notifier:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[NotifyMessage] = []

    def send(self, message: NotifyMessage) -> None:
        if self.fail:
            raise _Boom("smtp")
        self.sent.append(message)


def _report(state: cohort_ops.CohortState = cohort_ops.CohortState.MISSED):
    return cohort_ops.assess_cohort(
        COHORT,
        now_et=NEXT_EVENING_ET,
        runs=(),
        expected_members=("bench-spy",),
        session_date=MONDAY,
        cohort_start=MONDAY,
    )


def test_emit_does_not_raise_when_recording_a_delivered_alert_fails(tmp_path):
    """The worst case: the operator *was* notified but the record could not say so."""
    store = _BrokenStore(tmp_path / "alerts.sqlite3", fail_on={"mark_sent"})
    notifier = _Notifier()
    service = CohortAlertService(store, notifier, channel_live=True)

    outcome = service.emit(AlertKind.MISSED, _report())

    assert len(notifier.sent) == 1, "the message really did go out"
    assert outcome.status is AlertStatus.SENT, "reporting delivery-failed would be untrue"
    assert "could not be recorded" in outcome.detail


def test_emit_does_not_raise_when_recording_a_failed_send_fails(tmp_path):
    """A failure recording the failure must not mask the send failure."""
    store = _BrokenStore(tmp_path / "alerts.sqlite3", fail_on={"mark_failed"})
    service = CohortAlertService(store, _Notifier(fail=True), channel_live=True)

    outcome = service.emit(AlertKind.MISSED, _report())

    assert outcome.status is AlertStatus.DELIVERY_FAILED
    assert "_Boom" in outcome.detail, "the send failure is the cause worth reporting"


def test_emit_does_not_raise_when_recording_a_dark_channel_fails(tmp_path):
    store = _BrokenStore(tmp_path / "alerts.sqlite3", fail_on={"mark_sent"})
    service = CohortAlertService(store, _Notifier(), channel_live=False)

    outcome = service.emit(AlertKind.MISSED, _report())

    assert outcome.status is AlertStatus.DELIVERY_FAILED
    assert outcome.is_problem


def test_a_delivered_but_unrecorded_alert_stays_visible_to_the_operator(tmp_path):
    """The pending record is the signal; `cohort alerts` already surfaces it."""
    from schwab_trader.cohort_alerts import unresolved

    store = _BrokenStore(tmp_path / "alerts.sqlite3", fail_on={"mark_sent"})
    service = CohortAlertService(store, _Notifier(), channel_live=True)
    service.emit(AlertKind.MISSED, _report())

    assert len(unresolved(store.list(cohort_id=COHORT))) == 1


# ---------------------------------------------------------------------------
# 2. The dashboard must classify the codes production actually persists
# ---------------------------------------------------------------------------


def _observation(reasons: tuple[str, ...]) -> OfficialDailyObservation:
    return OfficialDailyObservation(
        cohort_id=COHORT,
        run_id="run-1",
        sleeve_id="trend-large",
        strategy="buy-hold",
        strategy_hash="hash",
        session_date=MONDAY,
        decision_time=CLOSE_UTC,
        valuation_time=CLOSE_UTC,
        status=ObservationStatus.PARTIAL,
        readiness_ready=False,
        readiness_reasons=reasons,
    )


def test_the_persisted_reason_format_is_qualified_not_bare():
    """Pin the contract the dashboard has to match, straight from the real path."""
    batch = BarBatch(
        provenance=Provenance(
            source="t",
            snapshot_id="s",
            retrieved_at=CLOSE_UTC,
            as_of=CLOSE_UTC,
            timing=TimingPolicy.SETTLED_EOD,
            vintage_safe=True,
        ),
        bars=(
            BarObservation(
                symbol="AAA",
                session_date=FRIDAY,
                open=Decimal("1"),
                high=Decimal("1"),
                low=Decimal("1"),
                close=Decimal("1"),
                volume=1,
            ),
        ),
    )
    readiness = evaluate_readiness(
        [
            (
                DataRequirement(
                    kind=DataKind.DAILY_BARS, keys=("AAA",), required_session=MONDAY
                ),
                SourceProbe.of(batch),
            )
        ],
        now=CLOSE_UTC,
    )
    _ready, reasons, _ids = summarize_readiness(readiness)

    assert reasons == ("daily_bars:session_not_covered",)
    assert ReasonCode.SESSION_NOT_COVERED.value not in reasons, (
        "the bare code is never an element; matching it exactly cannot work"
    )


@pytest.mark.parametrize(
    "reasons",
    [
        ("daily_bars:stale",),
        ("daily_bars:session_not_covered",),
        ("fundamentals:missing_keys", "daily_bars:stale"),
    ],
)
def test_qualified_freshness_codes_render_as_stale(reasons):
    assert dashboard._readiness_status(_observation(reasons)) == "stale"


@pytest.mark.parametrize(
    "reasons",
    [
        ("daily_bars:missing_keys",),
        ("fundamentals:source_missing",),
        (),
    ],
)
def test_other_reasons_still_render_as_not_ready(reasons):
    assert dashboard._readiness_status(_observation(reasons)) == "not_ready"


# ---------------------------------------------------------------------------
# 3. An expired awaiting-data session persists structured codes, not prose
# ---------------------------------------------------------------------------


def _quote(symbol: str) -> Quote:
    value = Decimal("10")
    return Quote(
        symbol=symbol,
        bid=value,
        ask=value,
        last=value,
        mark=value,
        previous_close=value,
        quote_time=CLOSE_UTC,
    )


@pytest.fixture
def cohort(tmp_path):
    store = SleeveStore(tmp_path / "sleeves")
    configs = tuple(
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
        for name in ("control-cash", "trend-large")
    )
    return store, configs


def test_expired_awaiting_data_persists_parseable_reason_codes(tmp_path, cohort):
    """The expiry observation must look like every other observation."""
    store, configs = cohort
    behind = evaluate_readiness(
        [
            (
                DataRequirement(
                    kind=DataKind.DAILY_BARS, keys=("AAA",), required_session=MONDAY
                ),
                SourceProbe.of(
                    BarBatch(
                        provenance=Provenance(
                            source="t",
                            snapshot_id="s",
                            retrieved_at=CLOSE_UTC,
                            as_of=CLOSE_UTC,
                            timing=TimingPolicy.SETTLED_EOD,
                            vintage_safe=True,
                        ),
                        bars=(
                            BarObservation(
                                symbol="AAA",
                                session_date=FRIDAY,
                                open=Decimal("1"),
                                high=Decimal("1"),
                                low=Decimal("1"),
                                close=Decimal("1"),
                                volume=1,
                            ),
                        ),
                    )
                ),
            )
        ],
        now=CLOSE_UTC,
    )
    snapshot = CohortSnapshot(
        snapshot_id="snap",
        quote_snapshot_id="quotes",
        captured_at=CLOSE_UTC,
        quotes={"AAA": _quote("AAA")},
        resources=strategy_registry.StrategyResources(),
        readiness_by_member={cfg.name: behind for cfg in configs},
        data_snapshot_ids={"daily_bars": "bars"},
    )
    run_store = SleeveRunStore(tmp_path / "runs.sqlite3")
    orchestrator = SleeveRunOrchestrator(
        run_store=run_store,
        sleeve_store=store,
        kill_switch=KillSwitch(tmp_path / "kill.flag"),
        snapshot_provider=lambda cfgs, session, prior: snapshot,
        universe_resolver=lambda cfg: list(cfg.universe),
    )

    waiting = orchestrator.run(
        scheduling.evaluate_session(COHORT, MONDAY, EVENING_ET), configs, now=CLOSE_UTC
    )
    assert waiting.status is SleeveRunStatus.AWAITING_DATA
    stored = next(error for error in waiting.errors if error.code == "awaiting_data")
    assert stored.reasons == ("daily_bars:session_not_covered",)

    expired = orchestrator.run(
        scheduling.evaluate_session(COHORT, MONDAY, NEXT_EVENING_ET), configs, now=CLOSE_UTC
    )

    assert expired.status is SleeveRunStatus.MISSED
    assert set(m.status for m in expired.members) == {MemberRunStatus.DATA_NOT_READY}
    for cfg in configs:
        observations = EvaluationStore(store.eval_path(cfg.name)).official_observations()
        assert len(observations) == 1
        reasons = observations[0].readiness_reasons
        assert reasons == ("daily_bars:session_not_covered",), (
            "the expiry observation must carry the same structured codes as every other"
        )
        # No prose: every element must parse as "<kind>:<reason>".
        for reason in reasons:
            assert " " not in reason, f"prose leaked into a reason-code list: {reason!r}"
            assert reason.count(":") == 1

        # And the dashboard must classify it, which is the point of keeping the format.
        assert dashboard._readiness_status(observations[0]) == "stale"
