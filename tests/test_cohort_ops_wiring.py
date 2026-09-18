"""Wiring tests for the cohort operations commands and the shared alert backend.

The shared-storage cases run against a temporary **local SQLite** file through the
SQLAlchemy adapter, never a real Neon URL. The CLI cases invoke only ``--help``, so no
command body runs, no settings are read, and no network, credential, broker path, or
``.env`` is touched.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from schwab_trader import cohort_ops
from schwab_trader.cli import app
from schwab_trader.cohort_alerts import (
    AlertDelivery,
    AlertKind,
    AlertStatus,
    ClaimOutcome,
    CohortAlertService,
)
from schwab_trader.notify import NotifyMessage
from schwab_trader.sleeve_runs import (
    MemberRunStatus,
    SleeveRun,
    SleeveRunMember,
    SleeveRunStatus,
)
from schwab_trader.storage.alerts import SqlAlchemyCohortAlertStore
from schwab_trader.storage.contracts import CohortAlertRepository
from schwab_trader.storage.database import Database
from schwab_trader.storage.schema import Cohort, StorageNamespace

runner = CliRunner()
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

COHORT = "paper-first-2026-07-27"
SESSION_ID = "XNYS:2026-07-27"
MONDAY = date(2026, 7, 27)


def _help(*args: str) -> str:
    result = runner.invoke(app, [*args, "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    return _ANSI.sub("", result.output).replace("│", " ")


@pytest.fixture
def shared_sqlite(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'shared.sqlite3'}", create_schema=True)
    # The alert row has a cohort foreign key, so the cohort has to exist first.
    with database.session() as session:
        session.add(
            StorageNamespace(
                namespace_id="ns-test",
                name="test",
                kind="cohort",
                source_identity=None,
                created_at=datetime(2026, 7, 27, tzinfo=UTC),
                immutable_metadata={},
            )
        )
        session.flush()
        session.add(
            Cohort(
                cohort_id=COHORT,
                namespace_id="ns-test",
                name=COHORT,
                created_at=datetime(2026, 7, 27, tzinfo=UTC),
                start_session=MONDAY,
                status="active",
                starting_cash_per_sleeve=Decimal("10000.00"),
                settlement_model="t1",
                leverage=Decimal("1"),
                benchmark_sleeve_name="bench-spy",
                manifest_json={},
                manifest_hash="0" * 64,
            )
        )
    try:
        yield database
    finally:
        database.dispose()


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[NotifyMessage] = []

    def send(self, message: NotifyMessage) -> None:
        self.sent.append(message)


def _completed_report() -> cohort_ops.CohortHealthReport:
    """A cohort whose Monday session completed, built without cross-test imports."""
    members = ("bench-spy", "trend")
    started = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)
    run = SleeveRun(
        run_id="run-2026-07-27",
        run_key=f"{COHORT}@{SESSION_ID}",
        cohort_id=COHORT,
        session_id=SESSION_ID,
        scheduled_for=MONDAY,
        expected_members=members,
        completed_members=members,
        started_at=started,
        completed_at=started,
        status=SleeveRunStatus.COMPLETED,
        members=tuple(
            SleeveRunMember(sleeve_id=sleeve, status=MemberRunStatus.COMPLETED)
            for sleeve in members
        ),
    )
    return cohort_ops.assess_cohort(
        COHORT,
        now_et=datetime(2026, 7, 27, 18, 30),
        runs=[run],
        expected_members=members,
        session_date=MONDAY,
    )


# --- Shared-database adapter ------------------------------------------------------


def test_the_shared_alert_store_satisfies_the_repository_contract(shared_sqlite) -> None:
    assert isinstance(SqlAlchemyCohortAlertStore(shared_sqlite), CohortAlertRepository)


def test_the_shared_backend_claims_a_transition_exactly_once(shared_sqlite) -> None:
    store = SqlAlchemyCohortAlertStore(shared_sqlite)
    kwargs = {
        "cohort_id": COHORT,
        "session_id": SESSION_ID,
        "scheduled_for": MONDAY,
        "kind": AlertKind.COMPLETED,
    }
    first = store.claim(**kwargs)  # type: ignore[arg-type]
    assert first.alert is not None and first.alert.delivery is AlertDelivery.PENDING

    # A second claim while the first is still recent is in flight, never a duplicate.
    assert store.claim(**kwargs).outcome is ClaimOutcome.IN_FLIGHT  # type: ignore[arg-type]

    store.mark_sent(first.alert.alert_key)
    assert store.claim(**kwargs).outcome is ClaimOutcome.ALREADY_SENT  # type: ignore[arg-type]
    settled = store.get(first.alert.alert_key)
    assert settled is not None
    assert settled.delivery is AlertDelivery.SENT
    assert settled.delivered_at is not None


def test_the_shared_backend_allows_retry_after_a_recorded_failure(shared_sqlite) -> None:
    store = SqlAlchemyCohortAlertStore(shared_sqlite)
    claimed = store.claim(
        cohort_id=COHORT,
        session_id=SESSION_ID,
        scheduled_for=MONDAY,
        kind=AlertKind.PARTIAL,
    )
    assert claimed.alert is not None
    store.mark_failed(claimed.alert.alert_key, reason="NotifyError")

    retried = store.claim(
        cohort_id=COHORT,
        session_id=SESSION_ID,
        scheduled_for=MONDAY,
        kind=AlertKind.PARTIAL,
    )
    assert retried.outcome is ClaimOutcome.CLAIMED
    assert retried.alert is not None
    assert retried.alert.attempts == 2
    assert retried.alert.failure_reason is None

    records = store.list(cohort_id=COHORT)
    assert [record.kind for record in records] == [AlertKind.PARTIAL]
    assert store.list(cohort_id="another-cohort") == []


def test_the_service_behaves_identically_on_the_shared_backend(shared_sqlite) -> None:
    """The at-most-once guarantee must not depend on which store is configured."""
    notifier = RecordingNotifier()
    service = CohortAlertService(
        SqlAlchemyCohortAlertStore(shared_sqlite),  # type: ignore[arg-type]
        notifier,
        channel_live=True,
    )
    report = _completed_report()

    assert service.emit(AlertKind.COMPLETED, report).status is AlertStatus.SENT
    for _ in range(5):
        assert service.emit(AlertKind.COMPLETED, report).status is AlertStatus.SUPPRESSED
    assert len(notifier.sent) == 1


# --- CLI wiring -------------------------------------------------------------------


def test_the_cohort_group_registers_the_operations_commands() -> None:
    text = _help("cohort")
    assert "health" in text
    assert "readiness" in text
    assert "alerts" in text


def test_cohort_health_exposes_the_json_and_clock_options() -> None:
    text = _help("cohort", "health")
    assert "--json" in text
    assert "--cohort" in text
    assert "--now" in text
    assert "--notify" in text


def test_cohort_readiness_documents_that_it_installs_nothing() -> None:
    text = _help("cohort", "readiness")
    assert "--json" in text
    assert "--show-setup" in text
    lowered = text.lower()
    assert "never" in lowered and "scheduled task" in lowered
