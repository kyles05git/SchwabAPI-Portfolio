"""Restart-safe cohort lifecycle alerts.

A scheduler fires the cohort runner many times a day; almost every invocation is a
no-op. Sending an alert on every invocation would train the operator to ignore the
channel, and keeping "already sent" in process memory would resend everything after a
restart. So delivery is keyed on a *durable transition identity* — one
``(cohort, session, kind)`` triple — recorded in the same database as the run it
describes.

The record is written **before** the message is handed to the notifier:

* no record        → claim it, then send;
* record ``sent``  → skip; the operator already heard about this transition;
* record ``failed``→ retry; nothing was delivered, so a retry cannot duplicate;
* record ``pending``→ it depends on how old it is, and this is the deliberate part.

A ``pending`` record means a previous attempt stopped between the claim and the
outcome, so whether the message went out is genuinely unknown. A *fresh* pending record
is most likely a send in flight in another process, so it is left alone. A *stale* one
(older than :data:`DEFAULT_AMBIGUOUS_AFTER`) is re-sent, clearly marked as a possible
duplicate, up to :data:`DEFAULT_MAX_ATTEMPTS` times.

That is the opposite of the rule for order submission, and intentionally so. A
duplicated order moves real money; a duplicated *warning* costs an operator ten seconds
of confusion, while a silently dropped one can hide a failed cohort session for days.
For operational alerts a possible duplicate beats a lost warning. Attempts are still
bounded, and a record that exhausts them stops retrying and is reported as unresolved
rather than retried forever.

Nothing here can change a paper run. :meth:`CohortAlertService.emit` swallows every
notifier failure into a returned :class:`AlertOutcome` — a dark or broken channel is
reported, never raised, so a delivery problem cannot mark a completed cohort run
failed or cause it to repeat.

Message bodies carry cohort, sleeve, session, and status only. No connection string,
account identifier, token, or raw provider payload is ever formatted into an alert.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from schwab_trader.cohort_ops import CohortHealthReport, CohortState
from schwab_trader.logging_config import get_logger
from schwab_trader.notify import Notifier, NotifyMessage
from schwab_trader.sleeve_runs import SleeveRun, SleeveRunStatus, awaits_reauthentication

log = get_logger("schwab_trader.cohort_alerts")

#: How long a ``pending`` claim must sit untouched before it is treated as abandoned
#: rather than in flight. Long enough that a slow SMTP handshake in another process is
#: never stomped on; short enough that a crash is recovered on the next scheduler tick.
DEFAULT_AMBIGUOUS_AFTER = timedelta(minutes=15)

#: Total delivery attempts per transition, including ambiguous re-sends. Bounded so a
#: process that crashes mid-send every time cannot mail the operator indefinitely.
DEFAULT_MAX_ATTEMPTS = 3


class AlertKind(StrEnum):
    """The cohort transition an alert describes. One alert per kind per session."""

    COMPLETED = "completed"
    """Every expected member recorded an official observation."""

    LATE = "late"
    """The session completed, but only after the scheduler's grace period."""

    PARTIAL = "partial"
    """Some members completed and some did not; evidence is incomplete."""

    FAILED = "failed"
    """A durable outcome in which no member completed."""

    MISSED = "missed"
    """The session passed its deadline without a recoverable run."""

    AWAITING_AUTH = "awaiting-auth"
    """The runner needs Schwab reauthentication before it can capture a snapshot.

    The one *non-terminal* transition worth interrupting an operator for. Every other
    wait resolves itself if the scheduler simply fires again; this one cannot, because
    only a human can complete the login, and the session it holds open still expires at
    its scheduling deadline. Delivered once per ``(cohort, session)`` like every other
    transition, so a scheduler polling every few minutes still mails exactly once.
    """


#: Kinds that describe a problem rather than a healthy completion.
PROBLEM_KINDS = frozenset(
    {
        AlertKind.LATE,
        AlertKind.PARTIAL,
        AlertKind.FAILED,
        AlertKind.MISSED,
        AlertKind.AWAITING_AUTH,
    }
)


class AlertDelivery(StrEnum):
    """Durable delivery state of one claimed transition."""

    PENDING = "pending"
    """Claimed, handed to the notifier, outcome not yet recorded."""

    SENT = "sent"
    """The notifier accepted the message."""

    FAILED = "failed"
    """The notifier rejected the message; nothing was delivered."""


class ClaimOutcome(StrEnum):
    """Why a store did or did not hand out a delivery slot for a transition."""

    CLAIMED = "claimed"
    """A fresh slot: this transition has never been delivered."""

    RECLAIMED_UNCERTAIN = "reclaimed-uncertain"
    """A stale ``pending`` record was taken over. The send may be a duplicate."""

    ALREADY_SENT = "already-sent"
    IN_FLIGHT = "in-flight"
    """A ``pending`` record is recent enough to be an active send elsewhere."""

    EXHAUSTED = "exhausted"
    """Stale and out of attempts. Stop retrying and surface it to the operator."""


@dataclass(frozen=True)
class ClaimResult:
    """A claim decision plus the record it applies to."""

    outcome: ClaimOutcome
    alert: CohortAlert | None = None

    @property
    def granted(self) -> bool:
        return self.outcome in {ClaimOutcome.CLAIMED, ClaimOutcome.RECLAIMED_UNCERTAIN}

    @property
    def uncertain(self) -> bool:
        """Whether the message this grants must be marked as a possible duplicate."""
        return self.outcome is ClaimOutcome.RECLAIMED_UNCERTAIN


class AlertStatus(StrEnum):
    """What :meth:`CohortAlertService.emit` actually did."""

    SENT = "sent"
    RESENT_UNCERTAIN = "resent-uncertain"
    """Re-sent after an abandoned attempt; the operator may see it twice."""

    SUPPRESSED = "suppressed"
    """The transition was already recorded; no duplicate was sent."""

    IN_FLIGHT = "in-flight"
    """Another attempt is recent enough to still be running; left alone."""

    UNRESOLVED = "unresolved"
    """Attempts are exhausted and delivery was never confirmed. Needs an operator."""

    NO_CHANNEL = "no-channel"
    """No notification channel is configured; the transition was still recorded."""

    DELIVERY_FAILED = "delivery-failed"
    """The channel rejected the message. Recorded, reported, never retried inline."""


class CohortAlert(BaseModel):
    """One durable transition record."""

    model_config = ConfigDict(frozen=True)

    alert_key: str
    cohort_id: str
    session_id: str
    scheduled_for: date
    kind: AlertKind
    delivery: AlertDelivery
    attempts: int
    created_at: datetime
    updated_at: datetime
    delivered_at: datetime | None = None
    detail: str | None = None
    failure_reason: str | None = None


def alert_key(cohort_id: str, session_id: str, kind: AlertKind) -> str:
    """Stable SHA-256 identity for one ``(cohort, session, kind)`` transition."""
    return hashlib.sha256(f"{cohort_id}\x1f{session_id}\x1f{kind.value}".encode()).hexdigest()


def _utc(value: datetime | None = None) -> datetime:
    stamp = value or datetime.now(UTC)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        return stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS cohort_alerts (
    alert_key TEXT PRIMARY KEY,
    cohort_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    kind TEXT NOT NULL,
    delivery TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    delivered_at TEXT,
    detail TEXT,
    failure_reason TEXT,
    UNIQUE (cohort_id, session_id, kind)
);
"""


class CohortAlertStore:
    """SQLite persistence for cohort alert transitions (local, single-writer)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def claim(
        self,
        *,
        cohort_id: str,
        session_id: str,
        scheduled_for: date,
        kind: AlertKind,
        detail: str | None = None,
        now: datetime | None = None,
        ambiguous_after: timedelta = DEFAULT_AMBIGUOUS_AFTER,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> ClaimResult:
        """Reserve a delivery slot for this transition and say why.

        See the module docstring for the ``pending`` policy: fresh means in flight and
        is left alone; stale means abandoned and is re-sent as a possible duplicate
        until ``max_attempts`` is reached.
        """
        key = alert_key(cohort_id, session_id, kind)
        stamp = _utc(now)
        outcome = ClaimOutcome.CLAIMED
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT delivery, attempts, updated_at FROM cohort_alerts WHERE alert_key = ?",
                (key,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO cohort_alerts (alert_key, cohort_id, session_id, "
                    "scheduled_for, kind, delivery, attempts, created_at, updated_at, "
                    "delivered_at, detail, failure_reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, NULL, ?, NULL)",
                    (
                        key,
                        cohort_id,
                        session_id,
                        scheduled_for.isoformat(),
                        kind.value,
                        AlertDelivery.PENDING.value,
                        stamp.isoformat(),
                        stamp.isoformat(),
                        detail,
                    ),
                )
            else:
                delivery = AlertDelivery(row["delivery"])
                attempts = int(row["attempts"])
                if delivery is AlertDelivery.SENT:
                    return ClaimResult(ClaimOutcome.ALREADY_SENT, self.get(key))
                if delivery is AlertDelivery.PENDING:
                    age = stamp - _utc(datetime.fromisoformat(row["updated_at"]))
                    if age < ambiguous_after:
                        return ClaimResult(ClaimOutcome.IN_FLIGHT, self.get(key))
                    if attempts >= max_attempts:
                        return ClaimResult(ClaimOutcome.EXHAUSTED, self.get(key))
                    outcome = ClaimOutcome.RECLAIMED_UNCERTAIN
                elif attempts >= max_attempts:
                    return ClaimResult(ClaimOutcome.EXHAUSTED, self.get(key))
                conn.execute(
                    "UPDATE cohort_alerts SET delivery = ?, attempts = ?, updated_at = ?, "
                    "detail = ?, failure_reason = NULL WHERE alert_key = ?",
                    (
                        AlertDelivery.PENDING.value,
                        attempts + 1,
                        stamp.isoformat(),
                        detail,
                        key,
                    ),
                )
        return ClaimResult(outcome, self.get(key))

    def mark_sent(self, key: str, *, now: datetime | None = None) -> CohortAlert | None:
        stamp = _utc(now)
        with self._connect() as conn:
            conn.execute(
                "UPDATE cohort_alerts SET delivery = ?, delivered_at = ?, updated_at = ?, "
                "failure_reason = NULL WHERE alert_key = ?",
                (AlertDelivery.SENT.value, stamp.isoformat(), stamp.isoformat(), key),
            )
        return self.get(key)

    def mark_failed(
        self, key: str, *, reason: str, now: datetime | None = None
    ) -> CohortAlert | None:
        stamp = _utc(now)
        with self._connect() as conn:
            conn.execute(
                "UPDATE cohort_alerts SET delivery = ?, updated_at = ?, failure_reason = ? "
                "WHERE alert_key = ?",
                (AlertDelivery.FAILED.value, stamp.isoformat(), reason, key),
            )
        return self.get(key)

    def get(self, key: str) -> CohortAlert | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM cohort_alerts WHERE alert_key = ?", (key,)
            ).fetchone()
        return None if row is None else self._domain(row)

    def list(self, *, cohort_id: str | None = None, limit: int = 100) -> list[CohortAlert]:
        query = "SELECT * FROM cohort_alerts"
        params: list[object] = []
        if cohort_id is not None:
            query += " WHERE cohort_id = ?"
            params.append(cohort_id)
        query += " ORDER BY scheduled_for DESC, created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._domain(row) for row in rows]

    @staticmethod
    def _domain(row: sqlite3.Row) -> CohortAlert:
        return CohortAlert(
            alert_key=row["alert_key"],
            cohort_id=row["cohort_id"],
            session_id=row["session_id"],
            scheduled_for=date.fromisoformat(row["scheduled_for"]),
            kind=AlertKind(row["kind"]),
            delivery=AlertDelivery(row["delivery"]),
            attempts=int(row["attempts"]),
            created_at=_utc(datetime.fromisoformat(row["created_at"])),
            updated_at=_utc(datetime.fromisoformat(row["updated_at"])),
            delivered_at=(
                _utc(datetime.fromisoformat(row["delivered_at"]))
                if row["delivered_at"]
                else None
            ),
            detail=row["detail"],
            failure_reason=row["failure_reason"],
        )


class AlertOutcome(BaseModel):
    """What one :meth:`CohortAlertService.emit` call did. Never an exception."""

    model_config = ConfigDict(frozen=True)

    kind: AlertKind
    alert_key: str
    status: AlertStatus
    detail: str

    @property
    def delivered(self) -> bool:
        return self.status is AlertStatus.SENT

    @property
    def is_problem(self) -> bool:
        """Whether the operator should look at the channel itself."""
        return self.status in {AlertStatus.DELIVERY_FAILED, AlertStatus.UNRESOLVED}


def kind_for_run(run: SleeveRun, *, ran_late: bool) -> AlertKind | None:
    """The single transition a finished run represents, or ``None`` for a no-op.

    A run that is still pending, running, awaiting data, or was skipped as a closed
    session has not transitioned to anything worth interrupting an operator for.
    ``awaiting-data`` in particular is the normal quiet retry path: the scheduler fires
    again shortly, and alerting each time would mail the operator every half hour about
    a condition that usually resolves itself. If it never resolves, the session reaches
    its deadline and alerts once as :attr:`AlertKind.MISSED`.

    A wait on *authentication* is the exception, and it is why this reads the run's wait
    code rather than its status alone. Retrying cannot fix a rejected refresh token, so
    staying quiet would hold the session open in silence until the deadline turned it
    into an unrecoverable ``missed`` — which is exactly what happened on 2026-07-31.
    """
    match run.status:
        case SleeveRunStatus.COMPLETED:
            return AlertKind.LATE if ran_late else AlertKind.COMPLETED
        case SleeveRunStatus.PARTIAL:
            return AlertKind.PARTIAL
        case SleeveRunStatus.FAILED:
            return AlertKind.FAILED
        case SleeveRunStatus.MISSED:
            return AlertKind.MISSED
        case SleeveRunStatus.AWAITING_DATA if awaits_reauthentication(run):
            return AlertKind.AWAITING_AUTH
    return None


def kind_for_state(state: CohortState) -> AlertKind | None:
    """The transition a health verdict represents, for scheduler-side alerting.

    Only terminal, actionable states alert. ``pre-close`` and ``due`` are the normal
    quiet path, and ``late`` and ``awaiting-data`` are deliberately excluded here: both
    are still recoverable by the next scheduler invocation, so alerting on them would
    fire repeatedly for a condition that usually resolves itself. A wait that never
    resolves becomes ``missed`` at the deadline and alerts exactly once there.

    :attr:`AlertKind.AWAITING_AUTH` is deliberately absent too. A health *verdict* is a
    state, and the state cannot tell the two waits apart — only the run's error code
    can. :func:`kind_for_run` owns that transition, so the runner that actually met the
    rejected token is the one thing that mails about it, exactly once.
    """
    match state:
        case CohortState.MISSED:
            return AlertKind.MISSED
        case CohortState.PARTIAL:
            return AlertKind.PARTIAL
        case CohortState.FAILED:
            return AlertKind.FAILED
    return None


#: How a refused claim is reported. Every one of these leaves the run untouched.
_DENIED: dict[ClaimOutcome, tuple[AlertStatus, str]] = {
    ClaimOutcome.ALREADY_SENT: (
        AlertStatus.SUPPRESSED,
        "This transition was already notified.",
    ),
    ClaimOutcome.IN_FLIGHT: (
        AlertStatus.IN_FLIGHT,
        "Another attempt claimed this transition recently and may still be sending; "
        "it will be re-sent later if it never completes.",
    ),
    ClaimOutcome.EXHAUSTED: (
        AlertStatus.UNRESOLVED,
        "Delivery was never confirmed after the maximum attempts. The alert is "
        "recorded as unresolved and needs an operator; check 'cohort alerts'.",
    ),
}


def _member_line(report: CohortHealthReport) -> str:
    return f"{report.completed_count}/{report.expected_count} members"


def build_message(
    kind: AlertKind,
    report: CohortHealthReport,
    *,
    uncertain: bool = False,
) -> NotifyMessage:
    """Render one sanitized alert. Pure — the testable core of the channel.

    Only names the operator already owns appear: the cohort id, sleeve ids, the
    exchange session, statuses, and sanitized run error codes.

    ``uncertain`` marks a re-send after an abandoned attempt. Saying so in the message
    is what makes a possible duplicate cheap: the reader knows immediately why they
    may be seeing it twice, instead of wondering whether the session ran twice.
    """
    session = report.session.session_date.isoformat()
    headline = {
        AlertKind.COMPLETED: f"cohort {report.cohort_id} completed {session}",
        AlertKind.LATE: f"cohort {report.cohort_id} completed {session} LATE",
        AlertKind.PARTIAL: f"cohort {report.cohort_id} PARTIAL for {session}",
        AlertKind.FAILED: f"cohort {report.cohort_id} FAILED for {session}",
        AlertKind.MISSED: f"cohort {report.cohort_id} MISSED {session}",
        AlertKind.AWAITING_AUTH: (
            f"cohort {report.cohort_id} NEEDS AUTHENTICATION for {session}"
        ),
    }[kind]
    if uncertain:
        headline = f"[possible duplicate] {headline}"

    lines = []
    if uncertain:
        lines.extend(
            [
                "NOTE: a previous attempt to send this alert did not record whether it "
                "was delivered, so you may have received it already. It describes the "
                "same single cohort session; the session did not run twice.",
                "",
            ]
        )
    lines += [
        f"Cohort:   {report.cohort_id}",
        f"Session:  {report.session.session_id}"
        + (" (early close)" if report.session.is_early_close else ""),
        f"Close:    {report.session.close_et:%Y-%m-%d %H:%M ET}"
        if report.session.close_et is not None
        else "Close:    n/a",
        f"State:    {report.state.value}",
        f"Members:  {_member_line(report)}",
    ]
    if report.observations is not None:
        obs = report.observations
        lines.append(
            f"Evidence: {obs.official} official, {obs.partial} partial, {obs.missing} missing"
        )
    if report.run is not None:
        if report.run.missing_members:
            lines.append(f"Missing:  {', '.join(report.run.missing_members)}")
        for error in report.run.errors[:10]:
            member = f" [{error.member_id}]" if error.member_id else ""
            lines.append(f"Error:    {error.code}{member}: {error.message}")
    lines.append("")
    lines.append(f"Next step: {report.next_action}")
    lines.append("")
    lines.append("Paper cohort operations only. No live order was placed or affected.")

    return NotifyMessage(
        subject=headline,
        body="\n".join(lines),
        category="alert" if kind in PROBLEM_KINDS or uncertain else "info",
    )


class CohortAlertService:
    """Deliver at most one notification per cohort transition, ever.

    ``channel_live`` reflects whether a real backend is configured. When it is False
    the transition is still claimed and marked delivered, so turning the channel on
    later does not replay a backlog of stale alerts.

    "Ever" has one deliberate exception: an attempt abandoned mid-send is retried and
    marked as a possible duplicate, because a lost warning is worse than a repeated
    one. See the module docstring.
    """

    def __init__(
        self,
        store: CohortAlertStore,
        notifier: Notifier,
        *,
        channel_live: bool,
        ambiguous_after: timedelta = DEFAULT_AMBIGUOUS_AFTER,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self.store = store
        self.notifier = notifier
        self.channel_live = channel_live
        self.ambiguous_after = ambiguous_after
        self.max_attempts = max_attempts

    @staticmethod
    def _record(operation: Callable[..., object], *args: object, **kwargs: object) -> bool:
        """Run one durable bookkeeping call, reporting failure instead of raising.

        Returns whether the record was written. The caller decides what an unwritten
        record means, because it differs: before a send it means nothing went out,
        while after one it means the operator was notified but the record does not
        say so.
        """
        try:
            operation(*args, **kwargs)
        except Exception as exc:
            log.warning("Cohort alert transition could not be recorded: %s", type(exc).__name__)
            return False
        return True

    def emit(
        self,
        kind: AlertKind,
        report: CohortHealthReport,
        *,
        now: datetime | None = None,
    ) -> AlertOutcome:
        """Send the alert if this exact transition has never been delivered.

        Never raises: every failure path returns an :class:`AlertOutcome`, so a
        caller in the runner's tail cannot have its run outcome changed by the
        notification channel. That includes the *bookkeeping* calls, not only the
        claim and the send - a store that fails while recording an outcome is still
        the notification channel failing, and it must not reach the run.
        """
        session_id = report.session.session_id
        key = alert_key(report.cohort_id, session_id, kind)
        detail = f"{report.state.value} {_member_line(report)}"

        try:
            claim = self.store.claim(
                cohort_id=report.cohort_id,
                session_id=session_id,
                scheduled_for=report.session.session_date,
                kind=kind,
                detail=detail,
                now=now,
                ambiguous_after=self.ambiguous_after,
                max_attempts=self.max_attempts,
            )
        except Exception as exc:  # durable store problem: report, never propagate
            log.warning("Cohort alert could not be recorded: %s", type(exc).__name__)
            return AlertOutcome(
                kind=kind,
                alert_key=key,
                status=AlertStatus.DELIVERY_FAILED,
                detail=f"Alert record unavailable ({type(exc).__name__}); nothing was sent.",
            )

        if not claim.granted:
            status, why = _DENIED[claim.outcome]
            return AlertOutcome(kind=kind, alert_key=key, status=status, detail=why)

        if not self.channel_live:
            if not self._record(self.store.mark_sent, key, now=now):
                return AlertOutcome(
                    kind=kind,
                    alert_key=key,
                    status=AlertStatus.DELIVERY_FAILED,
                    detail=(
                        "No notification channel is configured and the transition could "
                        "not be recorded; nothing was sent."
                    ),
                )
            return AlertOutcome(
                kind=kind,
                alert_key=key,
                status=AlertStatus.NO_CHANNEL,
                detail="No notification channel is configured; the transition was recorded.",
            )

        try:
            self.notifier.send(build_message(kind, report, uncertain=claim.uncertain))
        except Exception as exc:
            reason = type(exc).__name__
            # A failure while recording the failure must not replace it: the send is
            # the cause the operator needs, and neither may escape into the run's tail.
            self._record(self.store.mark_failed, key, reason=reason, now=now)
            log.warning("Cohort alert delivery failed: %s", reason)
            return AlertOutcome(
                kind=kind,
                alert_key=key,
                status=AlertStatus.DELIVERY_FAILED,
                detail=(
                    f"Delivery failed ({reason}). The run outcome is unaffected; the alert "
                    "will be retried on the next invocation."
                ),
            )

        if not self._record(self.store.mark_sent, key, now=now):
            # The message *was* delivered; only the bookkeeping failed. Reporting
            # "delivery failed" would be untrue, so the status stays honest. The record
            # is left `pending`, which `unresolved()` already surfaces in
            # `cohort alerts` and which a later invocation may re-send once, clearly
            # marked as a possible duplicate. A repeated warning beats a lost one.
            return AlertOutcome(
                kind=kind,
                alert_key=key,
                status=AlertStatus.SENT,
                detail=(
                    "Alert delivered, but the transition could not be recorded. It is "
                    "listed as unresolved in 'cohort alerts' and may be re-sent once, "
                    "marked as a possible duplicate."
                ),
            )
        if claim.uncertain:
            return AlertOutcome(
                kind=kind,
                alert_key=key,
                status=AlertStatus.RESENT_UNCERTAIN,
                detail=(
                    "Re-sent after an abandoned attempt and marked as a possible "
                    "duplicate. A repeated warning beats a lost one."
                ),
            )
        return AlertOutcome(
            kind=kind,
            alert_key=key,
            status=AlertStatus.SENT,
            detail="Alert delivered.",
        )


def unresolved(records: Iterable[CohortAlert]) -> tuple[CohortAlert, ...]:
    """Alerts whose delivery was never confirmed, newest session first.

    A record sitting here means the operator may never have been told about a cohort
    problem, which is itself a problem — so the health command shows these up front
    rather than leaving them to be discovered.
    """
    return tuple(
        sorted(
            (record for record in records if record.delivery is not AlertDelivery.SENT),
            key=lambda record: (record.scheduled_for, record.kind.value),
            reverse=True,
        )
    )


def alert_health_payload(records: Iterable[CohortAlert]) -> dict[str, object]:
    """JSON-safe summary of undelivered alert transitions for the health contract."""
    stuck = unresolved(records)
    return {
        "unresolved": len(stuck),
        "records": [
            {
                "session_date": record.scheduled_for.isoformat(),
                "kind": record.kind.value,
                "delivery": record.delivery.value,
                "attempts": record.attempts,
                "failure_reason": record.failure_reason,
                "detail": record.detail,
            }
            for record in stuck
        ],
    }


def outcome_payload(outcome: AlertOutcome) -> dict[str, str]:
    """JSON-safe view of one outcome for the health command's stable contract."""
    return {
        "kind": outcome.kind.value,
        "alert_key": outcome.alert_key,
        "status": outcome.status.value,
        "detail": outcome.detail,
    }


def outcomes_json(outcomes: Sequence[AlertOutcome]) -> str:
    return json.dumps([outcome_payload(item) for item in outcomes], indent=2, sort_keys=True)


__all__ = [
    "DEFAULT_AMBIGUOUS_AFTER",
    "DEFAULT_MAX_ATTEMPTS",
    "PROBLEM_KINDS",
    "AlertDelivery",
    "AlertKind",
    "AlertOutcome",
    "AlertStatus",
    "ClaimOutcome",
    "ClaimResult",
    "CohortAlert",
    "CohortAlertService",
    "CohortAlertStore",
    "alert_health_payload",
    "alert_key",
    "build_message",
    "kind_for_run",
    "kind_for_state",
    "outcome_payload",
    "outcomes_json",
    "unresolved",
]
