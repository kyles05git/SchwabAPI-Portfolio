"""Offline tests for restart-safe cohort lifecycle alerts.

Nothing here opens a real SMTP connection, reaches Neon, reads ``.env``, or touches a
broker path. The notifier is always a local fake and the store is always a temporary
SQLite file, so "did this resend after a restart?" is answered by re-opening the file
rather than by trusting process memory.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from schwab_trader import cohort_alerts, cohort_ops, scheduling
from schwab_trader.cohort_alerts import (
    AlertDelivery,
    AlertKind,
    AlertStatus,
    CohortAlertService,
    CohortAlertStore,
)
from schwab_trader.notify import NotifyError, NotifyMessage, NullNotifier
from schwab_trader.sleeve_runs import (
    MemberRunStatus,
    SleeveRun,
    SleeveRunError,
    SleeveRunMember,
    SleeveRunStatus,
)

COHORT = "paper-first-2026-07-27"
MEMBERS = tuple(f"sleeve-{index}" for index in range(7))
MONDAY = date(2026, 7, 27)
SESSION_ID = "XNYS:2026-07-27"
AFTER_CLOSE = datetime(2026, 7, 27, 18, 30)  # inside the grace period


class RecordingNotifier:
    """Captures messages instead of sending them; can be told to fail."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[NotifyMessage] = []
        self.fail = fail

    def send(self, message: NotifyMessage) -> None:
        if self.fail:
            raise NotifyError("SMTP delivery failed: SMTPAuthenticationError")
        self.sent.append(message)


def make_run(
    *,
    status: SleeveRunStatus,
    completed: tuple[str, ...] = MEMBERS,
    completed_at: datetime | None = None,
    errors: tuple[SleeveRunError, ...] = (),
) -> SleeveRun:
    started = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)
    return SleeveRun(
        run_id=scheduling.run_fingerprint(COHORT, SESSION_ID),
        run_key=scheduling.run_key(COHORT, SESSION_ID),
        cohort_id=COHORT,
        session_id=SESSION_ID,
        scheduled_for=MONDAY,
        expected_members=MEMBERS,
        completed_members=completed,
        started_at=started,
        completed_at=completed_at or started + timedelta(minutes=4),
        status=status,
        errors=errors,
        members=tuple(
            SleeveRunMember(
                sleeve_id=sleeve,
                status=(
                    MemberRunStatus.COMPLETED if sleeve in completed else MemberRunStatus.FAILED
                ),
            )
            for sleeve in MEMBERS
        ),
    )


def make_report(run: SleeveRun, *, now_et: datetime = AFTER_CLOSE) -> cohort_ops.CohortHealthReport:
    return cohort_ops.assess_cohort(
        COHORT,
        now_et=now_et,
        runs=[run],
        expected_members=MEMBERS,
        session_date=MONDAY,
    )


@pytest.fixture
def store(tmp_path: Path) -> CohortAlertStore:
    return CohortAlertStore(tmp_path / "alerts.sqlite3")


# --- At-most-once delivery ----------------------------------------------------


def test_a_successful_run_alerts_once_however_often_the_scheduler_fires(
    store: CohortAlertStore,
) -> None:
    notifier = RecordingNotifier()
    service = CohortAlertService(store, notifier, channel_live=True)
    report = make_report(make_run(status=SleeveRunStatus.COMPLETED))

    first = service.emit(AlertKind.COMPLETED, report)
    repeats = [service.emit(AlertKind.COMPLETED, report) for _ in range(9)]

    assert first.status is AlertStatus.SENT
    assert all(item.status is AlertStatus.SUPPRESSED for item in repeats)
    assert len(notifier.sent) == 1
    assert "7/7 members" in notifier.sent[0].body


def test_a_process_restart_does_not_resend(tmp_path: Path) -> None:
    """Dedupe lives in the file, not in memory: a fresh store must still suppress."""
    path = tmp_path / "alerts.sqlite3"
    report = make_report(make_run(status=SleeveRunStatus.COMPLETED))

    first_notifier = RecordingNotifier()
    CohortAlertService(CohortAlertStore(path), first_notifier, channel_live=True).emit(
        AlertKind.COMPLETED, report
    )

    # A completely new store object, as after a process restart.
    second_notifier = RecordingNotifier()
    outcome = CohortAlertService(
        CohortAlertStore(path), second_notifier, channel_live=True
    ).emit(AlertKind.COMPLETED, report)

    assert outcome.status is AlertStatus.SUPPRESSED
    assert len(first_notifier.sent) == 1
    assert second_notifier.sent == []


def make_waiting_run(*errors: SleeveRunError) -> SleeveRun:
    """A non-terminal ``awaiting-data`` run: nothing started, nothing recorded."""
    started = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)
    return SleeveRun(
        run_id=scheduling.run_fingerprint(COHORT, SESSION_ID),
        run_key=scheduling.run_key(COHORT, SESSION_ID),
        cohort_id=COHORT,
        session_id=SESSION_ID,
        scheduled_for=MONDAY,
        expected_members=MEMBERS,
        completed_members=(),
        started_at=started,
        completed_at=None,
        status=SleeveRunStatus.AWAITING_DATA,
        errors=errors,
        members=tuple(
            SleeveRunMember(sleeve_id=sleeve, status=MemberRunStatus.PENDING)
            for sleeve in MEMBERS
        ),
    )


REAUTH_ERROR = SleeveRunError(
    code="awaiting_reauthentication",
    message="Schwab reauthentication is required before the cohort snapshot can be captured.",
    capability="authentication",
    retryable=True,
    reasons=("authentication:reauthorization_required",),
    context={"failure": "ReauthRequiredError"},
)

DATA_ERROR = SleeveRunError(
    code="awaiting_data",
    message="daily_bars for 7 members are not settled for 2026-07-27.",
    capability="daily_bars",
    retryable=True,
)


def test_a_reauthentication_wait_alerts_once_and_says_what_to_do(
    store: CohortAlertStore,
) -> None:
    """Issue #109: the one non-terminal wait that must interrupt the operator.

    Nobody can fix a rejected refresh token by waiting, and the session is lost at its
    deadline if nobody logs in — but a scheduler firing every few minutes must still
    mail exactly once.
    """
    run = make_waiting_run(REAUTH_ERROR)
    kind = cohort_alerts.kind_for_run(run, ran_late=False)
    assert kind is AlertKind.AWAITING_AUTH
    assert kind in cohort_alerts.PROBLEM_KINDS

    notifier = RecordingNotifier()
    service = CohortAlertService(store, notifier, channel_live=True)
    report = make_report(run)

    first = service.emit(kind, report)
    repeats = [service.emit(kind, report) for _ in range(9)]

    assert first.status is AlertStatus.SENT
    assert all(item.status is AlertStatus.SUPPRESSED for item in repeats)
    assert len(notifier.sent) == 1
    body = notifier.sent[0].body
    assert "NEEDS AUTHENTICATION" in notifier.sent[0].subject
    assert "auth login" in body
    assert notifier.sent[0].category == "alert"
    # Sanitized: names the operator already owns, never a token or provider payload.
    for secret in ("access_token", "refresh_token", "Bearer", "https://api.schwabapi.com"):
        assert secret not in body


def test_a_plain_provider_wait_still_alerts_nobody() -> None:
    """The quiet retry path is unchanged: waiting on data is not an interruption."""
    assert cohort_alerts.kind_for_run(make_waiting_run(DATA_ERROR), ran_late=False) is None
    assert cohort_alerts.kind_for_run(make_waiting_run(), ran_late=False) is None


def test_the_current_wait_decides_whether_to_alert_not_the_history() -> None:
    """A session that waited on auth and then on data is waiting on data.

    ``errors`` is ordered by when each distinct verdict was last reached, so the last
    wait row wins. Reading any earlier row would keep mailing about a login the
    operator already completed.
    """
    resolved = make_waiting_run(REAUTH_ERROR, DATA_ERROR)
    assert cohort_alerts.kind_for_run(resolved, ran_late=False) is None

    regressed = make_waiting_run(DATA_ERROR, REAUTH_ERROR)
    assert cohort_alerts.kind_for_run(regressed, ran_late=False) is AlertKind.AWAITING_AUTH


def test_a_terminal_run_is_never_presented_as_merely_needing_a_login() -> None:
    """Fail-closed: the reauth code on a FAILED run does not soften its transition."""
    failed = make_run(
        status=SleeveRunStatus.FAILED,
        completed=(),
        errors=(REAUTH_ERROR,),
    )
    assert cohort_alerts.kind_for_run(failed, ran_late=False) is AlertKind.FAILED


def test_different_kinds_and_sessions_are_distinct_transitions(
    store: CohortAlertStore,
) -> None:
    notifier = RecordingNotifier()
    service = CohortAlertService(store, notifier, channel_live=True)
    report = make_report(make_run(status=SleeveRunStatus.COMPLETED))

    assert service.emit(AlertKind.COMPLETED, report).status is AlertStatus.SENT
    assert service.emit(AlertKind.LATE, report).status is AlertStatus.SENT
    assert len(notifier.sent) == 2

    other_session = store.claim(
        cohort_id=COHORT,
        session_id="XNYS:2026-07-28",
        scheduled_for=date(2026, 7, 28),
        kind=AlertKind.COMPLETED,
    )
    assert other_session.granted


# --- Delivery failure is recorded, never destructive ---------------------------


def test_delivery_failure_is_reported_and_retryable_without_duplicating(
    store: CohortAlertStore,
) -> None:
    report = make_report(make_run(status=SleeveRunStatus.COMPLETED))
    broken = RecordingNotifier(fail=True)

    failed = CohortAlertService(store, broken, channel_live=True).emit(
        AlertKind.COMPLETED, report
    )
    assert failed.status is AlertStatus.DELIVERY_FAILED
    assert failed.is_problem
    record = store.get(failed.alert_key)
    assert record is not None
    assert record.delivery is AlertDelivery.FAILED
    assert record.failure_reason == "NotifyError"

    # Nothing was delivered, so a later retry cannot duplicate what the operator saw.
    working = RecordingNotifier()
    retried = CohortAlertService(store, working, channel_live=True).emit(
        AlertKind.COMPLETED, report
    )
    assert retried.status is AlertStatus.SENT
    assert len(working.sent) == 1

    settled = store.get(failed.alert_key)
    assert settled is not None
    assert settled.delivery is AlertDelivery.SENT
    assert settled.attempts == 2


def test_emit_never_raises_even_when_the_store_is_broken() -> None:
    """A delivery problem must not be able to fail or repeat a paper run."""

    class BrokenStore:
        def claim(self, **_kwargs: object) -> object:
            raise RuntimeError("database unavailable")

        def get(self, _key: str) -> None:
            return None

    report = make_report(make_run(status=SleeveRunStatus.COMPLETED))
    service = CohortAlertService(
        BrokenStore(),  # type: ignore[arg-type]
        RecordingNotifier(),
        channel_live=True,
    )
    outcome = service.emit(AlertKind.COMPLETED, report)
    assert outcome.status is AlertStatus.DELIVERY_FAILED
    assert "RuntimeError" in outcome.detail


def _abandon(store: CohortAlertStore, kind: AlertKind, *, at: datetime) -> str:
    """Leave a `pending` claim behind, as a process killed mid-send would."""
    claim = store.claim(
        cohort_id=COHORT,
        session_id=SESSION_ID,
        scheduled_for=MONDAY,
        kind=kind,
        now=at,
    )
    assert claim.alert is not None and claim.alert.delivery is AlertDelivery.PENDING
    return claim.alert.alert_key


def test_a_recent_pending_claim_is_left_alone_as_possibly_in_flight(
    store: CohortAlertStore,
) -> None:
    """Another process may be inside a slow SMTP handshake right now."""
    started = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)
    _abandon(store, AlertKind.COMPLETED, at=started)

    notifier = RecordingNotifier()
    outcome = CohortAlertService(store, notifier, channel_live=True).emit(
        AlertKind.COMPLETED,
        make_report(make_run(status=SleeveRunStatus.COMPLETED)),
        now=started + timedelta(minutes=2),
    )
    assert outcome.status is AlertStatus.IN_FLIGHT
    assert notifier.sent == []


def test_an_abandoned_attempt_is_resent_and_marked_as_a_possible_duplicate(
    store: CohortAlertStore,
) -> None:
    """A lost warning is worse than a repeated one, so a stale claim is retried."""
    started = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)
    _abandon(store, AlertKind.FAILED, at=started)

    notifier = RecordingNotifier()
    outcome = CohortAlertService(store, notifier, channel_live=True).emit(
        AlertKind.FAILED,
        make_report(make_run(status=SleeveRunStatus.FAILED, completed=())),
        now=started + timedelta(hours=1),
    )
    assert outcome.status is AlertStatus.RESENT_UNCERTAIN
    assert len(notifier.sent) == 1
    assert "possible duplicate" in notifier.sent[0].subject
    body = notifier.sent[0].body
    assert "may have received it already" in body
    assert "did not run twice" in body


def test_a_repeatedly_abandoned_alert_stops_and_is_reported_unresolved(
    store: CohortAlertStore,
) -> None:
    """Bounded: a process crashing mid-send every time must not mail forever.

    A crash leaves the record ``pending`` and never settles it, so this drives the
    store directly — going through ``emit`` would mark each attempt delivered and
    never reach the ceiling.
    """
    started = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)
    kwargs = {
        "cohort_id": COHORT,
        "session_id": SESSION_ID,
        "scheduled_for": MONDAY,
        "kind": AlertKind.FAILED,
    }
    _abandon(store, AlertKind.FAILED, at=started)  # attempt 1, then killed

    outcomes = []
    for index in range(1, 4):
        result = store.claim(**kwargs, now=started + timedelta(hours=index))  # type: ignore[arg-type]
        outcomes.append(result.outcome)

    assert outcomes == [
        cohort_alerts.ClaimOutcome.RECLAIMED_UNCERTAIN,  # attempt 2
        cohort_alerts.ClaimOutcome.RECLAIMED_UNCERTAIN,  # attempt 3
        cohort_alerts.ClaimOutcome.EXHAUSTED,  # ceiling reached; stop retrying
    ]

    # The service reports the ceiling as an operator problem, not as success.
    notifier = RecordingNotifier()
    outcome = CohortAlertService(store, notifier, channel_live=True).emit(
        AlertKind.FAILED,
        make_report(make_run(status=SleeveRunStatus.FAILED, completed=())),
        now=started + timedelta(hours=9),
    )
    assert outcome.status is AlertStatus.UNRESOLVED
    assert outcome.is_problem
    assert notifier.sent == []
    assert "needs an operator" in outcome.detail

    # ...and it stays visible for the health command rather than vanishing.
    assert len(cohort_alerts.unresolved(store.list(cohort_id=COHORT))) == 1


def test_undelivered_records_are_surfaced_for_the_health_command(
    store: CohortAlertStore,
) -> None:
    """A silently lost warning must still be discoverable after the fact."""
    started = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)
    _abandon(store, AlertKind.MISSED, at=started)
    delivered = store.claim(
        cohort_id=COHORT,
        session_id="XNYS:2026-07-28",
        scheduled_for=date(2026, 7, 28),
        kind=AlertKind.COMPLETED,
        now=started,
    )
    assert delivered.alert is not None
    store.mark_sent(delivered.alert.alert_key)

    stuck = cohort_alerts.unresolved(store.list(cohort_id=COHORT))
    assert [record.kind for record in stuck] == [AlertKind.MISSED]

    payload = cohort_alerts.alert_health_payload(store.list(cohort_id=COHORT))
    assert payload["unresolved"] == 1
    assert payload["records"][0]["kind"] == "missed"  # type: ignore[index]


def test_a_dark_channel_records_the_transition_without_a_backlog(
    store: CohortAlertStore,
) -> None:
    """Turning SMTP on later must not replay every alert that happened while it was off."""
    report = make_report(make_run(status=SleeveRunStatus.COMPLETED))
    dark = CohortAlertService(store, NullNotifier(), channel_live=False)
    assert dark.emit(AlertKind.COMPLETED, report).status is AlertStatus.NO_CHANNEL

    notifier = RecordingNotifier()
    live = CohortAlertService(store, notifier, channel_live=True)
    assert live.emit(AlertKind.COMPLETED, report).status is AlertStatus.SUPPRESSED
    assert notifier.sent == []


# --- Which transition a run represents -----------------------------------------


@pytest.mark.parametrize(
    ("status", "ran_late", "expected"),
    [
        (SleeveRunStatus.COMPLETED, False, AlertKind.COMPLETED),
        (SleeveRunStatus.COMPLETED, True, AlertKind.LATE),
        (SleeveRunStatus.PARTIAL, False, AlertKind.PARTIAL),
        (SleeveRunStatus.FAILED, False, AlertKind.FAILED),
        (SleeveRunStatus.MISSED, False, AlertKind.MISSED),
        # Not transitions: nothing has happened worth interrupting an operator for.
        (SleeveRunStatus.PENDING, False, None),
        (SleeveRunStatus.RUNNING, False, None),
        (SleeveRunStatus.SKIPPED_CLOSED_SESSION, False, None),
    ],
)
def test_kind_for_run_maps_each_durable_outcome(
    status: SleeveRunStatus, ran_late: bool, expected: AlertKind | None
) -> None:
    run = make_run(status=status, completed=MEMBERS if status is SleeveRunStatus.COMPLETED else ())
    assert cohort_alerts.kind_for_run(run, ran_late=ran_late) is expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (cohort_ops.CohortState.MISSED, AlertKind.MISSED),
        (cohort_ops.CohortState.PARTIAL, AlertKind.PARTIAL),
        (cohort_ops.CohortState.FAILED, AlertKind.FAILED),
        # Quiet states: pre-close and due are normal, and late is still recoverable
        # by the next scheduler invocation, so it must not alert repeatedly.
        (cohort_ops.CohortState.PRE_CLOSE, None),
        (cohort_ops.CohortState.DUE, None),
        (cohort_ops.CohortState.LATE, None),
        (cohort_ops.CohortState.COMPLETED, None),
        (cohort_ops.CohortState.CLOSED_SESSION, None),
    ],
)
def test_kind_for_state_only_alerts_on_terminal_problems(
    state: cohort_ops.CohortState, expected: AlertKind | None
) -> None:
    assert cohort_alerts.kind_for_state(state) is expected


# --- Message content ------------------------------------------------------------


def test_a_partial_alert_names_the_missing_members_and_the_next_step() -> None:
    run = make_run(
        status=SleeveRunStatus.PARTIAL,
        completed=MEMBERS[:5],
        errors=(
            SleeveRunError(
                code="data_not_ready",
                message="Required strategy data failed readiness checks.",
                member_id="sleeve-5",
            ),
        ),
    )
    message = cohort_alerts.build_message(AlertKind.PARTIAL, make_report(run))
    assert "PARTIAL" in message.subject
    assert "5/7 members" in message.body
    assert "sleeve-5" in message.body
    assert "data_not_ready" in message.body
    assert "Next step:" in message.body
    assert message.category == "alert"


def test_a_completion_alert_is_informational_not_an_alarm() -> None:
    message = cohort_alerts.build_message(
        AlertKind.COMPLETED, make_report(make_run(status=SleeveRunStatus.COMPLETED))
    )
    assert message.category == "info"
    assert "7/7 members" in message.body
    assert "No live order" in message.body


def test_no_alert_body_can_leak_storage_or_account_identity() -> None:
    run = make_run(
        status=SleeveRunStatus.FAILED,
        completed=(),
        errors=(
            SleeveRunError(
                code="snapshot_unavailable",
                message="QuoteError: cohort snapshot could not be captured safely.",
            ),
        ),
    )
    report = cohort_ops.assess_cohort(
        COHORT,
        now_et=AFTER_CLOSE,
        runs=[run],
        expected_members=MEMBERS,
        session_date=MONDAY,
        storage_kind="shared-postgresql",
    )
    body = cohort_alerts.build_message(AlertKind.FAILED, report).body.lower()
    for forbidden in ("postgresql://", "sqlite:///", "password", "secret", "token", "sslmode"):
        assert forbidden not in body, forbidden


# --- Store semantics -------------------------------------------------------------


def test_the_transition_key_is_stable_and_kind_specific() -> None:
    first = cohort_alerts.alert_key(COHORT, SESSION_ID, AlertKind.COMPLETED)
    assert first == cohort_alerts.alert_key(COHORT, SESSION_ID, AlertKind.COMPLETED)
    assert first != cohort_alerts.alert_key(COHORT, SESSION_ID, AlertKind.PARTIAL)
    assert first != cohort_alerts.alert_key("other", SESSION_ID, AlertKind.COMPLETED)


def test_listing_records_survives_reopening_the_store(tmp_path: Path) -> None:
    path = tmp_path / "alerts.sqlite3"
    store = CohortAlertStore(path)
    claimed = store.claim(
        cohort_id=COHORT,
        session_id=SESSION_ID,
        scheduled_for=MONDAY,
        kind=AlertKind.MISSED,
        detail="missed 0/7 members",
    )
    assert claimed.alert is not None
    store.mark_sent(claimed.alert.alert_key)

    records = CohortAlertStore(path).list(cohort_id=COHORT)
    assert [record.kind for record in records] == [AlertKind.MISSED]
    assert records[0].delivery is AlertDelivery.SENT
    assert records[0].detail == "missed 0/7 members"
    assert CohortAlertStore(path).list(cohort_id="another-cohort") == []
