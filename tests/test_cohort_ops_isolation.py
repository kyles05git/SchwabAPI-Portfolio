"""Proof that cohort operations stay offline, paper-only, and non-destructive.

Three properties are locked in here:

1. The alert path never opens a socket, so no real SMTP server or Neon database can
   be reached from a test (or from an unconfigured machine).
2. A notification failure cannot change, retry, or corrupt a recorded cohort run.
3. The operations modules import no broker order path at all.

``.env`` isolation and the non-SQLite database guard come from ``conftest.py`` and
apply to every test here.
"""

from __future__ import annotations

import socket
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from schwab_trader import cli, cohort_alerts, cohort_ops, cohort_readiness
from schwab_trader.config import Settings
from schwab_trader.notify import NotifyError, NullNotifier, build_notifier
from schwab_trader.sleeve_runs import (
    MemberRunStatus,
    SleeveRun,
    SleeveRunMember,
    SleeveRunStatus,
)
from schwab_trader.sleeves import SleeveStore
from schwab_trader.storage import factory as storage_factory

COHORT = "paper-first-2026-07-27"
MONDAY = date(2026, 7, 27)
SESSION_ID = "XNYS:2026-07-27"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        sleeves_dir=tmp_path / "sleeves",
        log_path=tmp_path / "logs" / "schwab_trader.log",
    )


@pytest.fixture
def members(settings: Settings) -> list[object]:
    store = SleeveStore(settings.sleeves_dir)
    return [
        store.create(
            name,
            strategy="buy-hold",
            universe=["SPY"],
            starting_cash=Decimal("10000.00"),
            max_positions=1,
            max_position_fraction=Decimal("1"),
            settlement_t1=True,
            cohort_id=COHORT,
        )
        for name in ("bench-spy", "trend")
    ]


def make_run(members: list[object], status: SleeveRunStatus) -> SleeveRun:
    ids = tuple(cfg.identity for cfg in members)  # type: ignore[attr-defined]
    started = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)
    return SleeveRun(
        run_id="run-2026-07-27",
        run_key=f"{COHORT}@{SESSION_ID}",
        cohort_id=COHORT,
        session_id=SESSION_ID,
        scheduled_for=MONDAY,
        expected_members=ids,
        completed_members=ids if status is SleeveRunStatus.COMPLETED else (),
        started_at=started,
        completed_at=started,
        status=status,
        members=tuple(
            SleeveRunMember(sleeve_id=sleeve, status=MemberRunStatus.COMPLETED) for sleeve in ids
        ),
    )


# --- No network, ever ------------------------------------------------------------


def test_emitting_an_alert_opens_no_socket(
    settings: Settings, members: list[object], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("cohort operations must not open a network socket")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    run = make_run(members, SleeveRunStatus.COMPLETED)
    # No SMTP is configured, so the channel is dark and nothing is delivered.
    cli._emit_cohort_run_alert(settings, members, run, now_et=datetime(2026, 7, 27, 18, 30))  # type: ignore[arg-type]

    record = storage_factory.alert_store(settings).list(cohort_id=COHORT)
    assert [item.kind for item in record] == [cohort_alerts.AlertKind.COMPLETED]


def test_an_unconfigured_channel_yields_a_null_notifier(settings: Settings) -> None:
    assert not settings.has_smtp
    assert isinstance(build_notifier(settings), NullNotifier)


# --- A delivery failure cannot touch the run outcome -------------------------------


def test_a_broken_notifier_does_not_change_or_repeat_the_run(
    settings: Settings, members: list[object], monkeypatch: pytest.MonkeyPatch
) -> None:
    class Exploding:
        def send(self, _message: object) -> None:
            raise NotifyError("SMTP delivery failed: SMTPAuthenticationError")

    monkeypatch.setattr(
        cli,
        "_alert_service",
        lambda cfg: cohort_alerts.CohortAlertService(
            storage_factory.alert_store(cfg), Exploding(), channel_live=True
        ),
    )

    run = make_run(members, SleeveRunStatus.COMPLETED)
    before = run.model_dump()

    # Must not raise: the runner calls this after the durable result is recorded.
    cli._emit_cohort_run_alert(settings, members, run, now_et=datetime(2026, 7, 27, 18, 30))  # type: ignore[arg-type]

    assert run.model_dump() == before
    assert run.status is SleeveRunStatus.COMPLETED
    stored = storage_factory.alert_store(settings).list(cohort_id=COHORT)
    assert stored[0].delivery is cohort_alerts.AlertDelivery.FAILED
    assert stored[0].failure_reason == "NotifyError"


def test_a_broken_observation_read_does_not_escape_the_alert_path(
    settings: Settings, members: list[object], monkeypatch: pytest.MonkeyPatch
) -> None:
    def exploding(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(cli, "_cohort_observations", exploding)
    run = make_run(members, SleeveRunStatus.PARTIAL)
    cli._emit_cohort_run_alert(settings, members, run, now_et=datetime(2026, 7, 27, 18, 30))  # type: ignore[arg-type]
    assert run.status is SleeveRunStatus.PARTIAL


def test_a_pending_run_produces_no_alert_at_all(
    settings: Settings, members: list[object]
) -> None:
    run = make_run(members, SleeveRunStatus.PENDING)
    cli._emit_cohort_run_alert(settings, members, run, now_et=datetime(2026, 7, 27, 10, 0))  # type: ignore[arg-type]
    assert storage_factory.alert_store(settings).list(cohort_id=COHORT) == []


# --- Paper-only by construction ------------------------------------------------------


@pytest.mark.parametrize("module", [cohort_ops, cohort_alerts, cohort_readiness])
def test_the_operations_modules_import_no_broker_order_path(module: object) -> None:
    """Nothing here may reach orders, the authenticated client, or approvals."""
    source = Path(module.__file__).read_text(encoding="utf-8")  # type: ignore[attr-defined]
    for forbidden in (
        "import orders",
        "from schwab_trader.orders",
        "from schwab_trader import client",
        "from schwab_trader.client",
        "from schwab_trader import approval",
        "place_order",
        "submit_order",
    ):
        assert forbidden not in source, forbidden
