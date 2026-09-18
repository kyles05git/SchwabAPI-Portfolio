"""Is this machine allowed and able to run the scheduled official cohort job?

Automating the daily cohort run introduces exactly one new way to corrupt the
experiment: a second machine deciding it is also the writer. The advisory lock and the
durable ``(cohort_id, scheduled_for)`` identity stop that from producing duplicate
evidence, but they stop it by *failing*, which an operator only notices later. This
module is the check that runs first and answers plainly.

It is a **read-only assessment of already-gathered facts**. It performs no I/O, mutates
nothing, and — by deliberate design — never registers, enables, disables, or removes an
operating-system scheduled task. Installing the task stays an explicit human action;
this only tells the operator whether it would be safe to take.

Every check fails closed. An unset writer flag is not "probably fine, there is only one
machine"; it is a refusal, because the cost of guessing wrong is an unrecoverable split
in the official record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from schwab_trader import cohort_lifecycle

#: JSON contract version for :func:`readiness_payload`.
PAYLOAD_SCHEMA = "cohort-readiness/1"

#: Process exit code: safe to schedule.
EXIT_READY = 0
#: Process exit code: at least one check failed closed.
EXIT_NOT_READY = 1


class CheckStatus(StrEnum):
    """One check's verdict."""

    PASS = "pass"
    WARN = "warn"
    """Degraded but not unsafe — scheduling still produces correct evidence."""

    FAIL = "fail"
    """Unsafe or ambiguous. Do not schedule until it is resolved."""


@dataclass(frozen=True)
class ReadinessCheck:
    """One named condition, its verdict, and the operator's fix."""

    name: str
    status: CheckStatus
    detail: str
    remedy: str = ""

    @property
    def blocking(self) -> bool:
        return self.status is CheckStatus.FAIL


@dataclass(frozen=True)
class ReadinessFacts:
    """Everything the assessment needs, already read by the caller.

    Deliberately identity-poor: ``storage_kind`` is a *category* such as
    ``"shared-postgresql"``, never a connection string, and no credential, account
    identifier, or token appears anywhere in this structure.
    """

    cohort_id: str
    """Cohort the scheduled job would operate on; empty means unresolved."""

    writer_role: bool
    """Whether this machine is explicitly configured as the single writer."""

    shared_database: bool
    """Whether provider-neutral shared persistence is configured."""

    storage_kind: str = "local-sqlite"
    member_count: int = 0
    reproducible_members: int = 0
    """Members with a persisted, hash-matched strategy definition."""

    schema_error: str | None = None
    """Sanitized reason the durable run store could not be read, if any."""

    alert_store_error: str | None = None
    """Sanitized reason the alert transition store could not be read, if any."""

    missing_evidence_tables: tuple[str, ...] = ()
    """Market-data evidence tables the configured backend does not have.

    Table *names* only — never a connection string, schema, or credential.
    """

    evidence_store_error: str | None = None
    """Sanitized reason the market-data evidence tables could not be inspected."""

    notifications_live: bool = False
    kill_switch_engaged: bool = False


@dataclass(frozen=True)
class ReadinessReport:
    """The aggregate verdict plus every individual check."""

    facts: ReadinessFacts
    checks: tuple[ReadinessCheck, ...] = field(default_factory=tuple)

    @property
    def blocking(self) -> tuple[ReadinessCheck, ...]:
        return tuple(check for check in self.checks if check.blocking)

    @property
    def warnings(self) -> tuple[ReadinessCheck, ...]:
        return tuple(check for check in self.checks if check.status is CheckStatus.WARN)

    @property
    def ready(self) -> bool:
        """True only when no check failed. A report with no checks is not ready."""
        return bool(self.checks) and not self.blocking

    @property
    def exit_code(self) -> int:
        return EXIT_READY if self.ready else EXIT_NOT_READY

    @property
    def summary(self) -> str:
        if self.ready:
            warned = len(self.warnings)
            suffix = f" ({warned} warning{'s' if warned != 1 else ''})" if warned else ""
            return f"Ready to schedule on this machine{suffix}."
        return f"Not ready: {len(self.blocking)} blocking check(s)."


def _writer_check(facts: ReadinessFacts) -> ReadinessCheck:
    """The one-writer rule. Ambiguity here is always a refusal."""
    if not facts.writer_role:
        shared = (
            " Another machine already holds the writer role for the shared database."
            if facts.shared_database
            else ""
        )
        return ReadinessCheck(
            name="one-writer",
            status=CheckStatus.FAIL,
            detail=f"This machine is not configured as the cohort writer.{shared}",
            remedy=(
                "Run the scheduled job only on the single writer machine. Set "
                "SCHWAB_COHORT_WRITER=true in that machine's local .env, and leave it "
                "unset everywhere else so read-only dashboard clients cannot schedule."
            ),
        )
    if not facts.shared_database:
        return ReadinessCheck(
            name="one-writer",
            status=CheckStatus.PASS,
            detail=(
                "This machine is the cohort writer and uses local storage, so it is "
                "the only possible writer."
            ),
        )
    return ReadinessCheck(
        name="one-writer",
        status=CheckStatus.PASS,
        detail=(
            "This machine is the declared cohort writer for the shared database; "
            "official sessions are additionally serialized by an advisory lock."
        ),
    )


def _cohort_check(facts: ReadinessFacts) -> ReadinessCheck:
    if not facts.cohort_id.strip():
        return ReadinessCheck(
            name="cohort-identity",
            status=CheckStatus.FAIL,
            detail="No cohort was resolved, so the scheduled command has no target.",
            remedy=(
                "Set SCHWAB_COHORT_ID in the local .env, or pass --cohort explicitly. "
                "The cohort is never guessed when more than one exists."
            ),
        )
    # Pure lookup against the reviewed lifecycle registry — no storage is touched, which
    # is what keeps this assessment I/O-free.
    status = cohort_lifecycle.status_for(facts.cohort_id)
    if not status.runnable:
        replacement = (
            f"Point the scheduled job and SCHWAB_COHORT_ID at {status.superseded_by}."
            if status.superseded_by
            else "Bootstrap a replacement cohort and target that instead."
        )
        return ReadinessCheck(
            name="cohort-identity",
            status=CheckStatus.FAIL,
            detail=(
                f"Cohort {facts.cohort_id} is {status.lifecycle.value} and must not be "
                f"scheduled. {status.reason}"
            ),
            remedy=replacement,
        )
    return ReadinessCheck(
        name="cohort-identity",
        status=CheckStatus.PASS,
        detail=f"Scheduled runs would target cohort {facts.cohort_id}.",
    )


def _membership_check(facts: ReadinessFacts) -> ReadinessCheck:
    if facts.member_count <= 0:
        return ReadinessCheck(
            name="cohort-membership",
            status=CheckStatus.FAIL,
            detail="The cohort has no registered members, so a run would have nothing to do.",
            remedy="Bootstrap the cohort's sleeves before scheduling the daily job.",
        )
    if facts.reproducible_members < facts.member_count:
        missing = facts.member_count - facts.reproducible_members
        return ReadinessCheck(
            name="cohort-membership",
            status=CheckStatus.FAIL,
            detail=(
                f"{missing} of {facts.member_count} members lack a reproducible strategy "
                "definition; the official runner refuses to run them."
            ),
            remedy=(
                "Re-create the affected sleeves so each one persists a StrategyDefinition "
                "whose configuration hash matches."
            ),
        )
    return ReadinessCheck(
        name="cohort-membership",
        status=CheckStatus.PASS,
        detail=f"{facts.member_count} reproducible member(s) are registered.",
    )


def _schema_check(facts: ReadinessFacts) -> ReadinessCheck:
    if facts.schema_error is not None:
        return ReadinessCheck(
            name="schema-compatibility",
            status=CheckStatus.FAIL,
            detail=f"The durable run store could not be read: {facts.schema_error}.",
            remedy="Run 'schwab-trader storage verify' and resolve the reported difference.",
        )
    return ReadinessCheck(
        name="schema-compatibility",
        status=CheckStatus.PASS,
        detail=f"The durable run store ({facts.storage_kind}) is readable and current.",
    )


#: Where the operator applies a pending revision after review.
_MIGRATION_REMEDY = (
    "Apply the pending revision through the reviewed deployment runbook: run "
    "'alembic upgrade head' against the shared database as described in "
    "docs/migrations/postgresql-runbook.md, then re-run this check."
)


def _evidence_schema_check(facts: ReadinessFacts) -> ReadinessCheck:
    """Are the intraday-to-daily evidence tables actually there?

    Without them the first session that needs the five-minute fallback fails for *every*
    symbol as ``provider_error``, blocking the whole cohort and naming a provider fault
    that never happened. Readiness has to catch that here, because the store is only
    touched once a fallback is already in progress.
    """
    if facts.evidence_store_error is not None:
        return ReadinessCheck(
            name="market-data-evidence-schema",
            status=CheckStatus.FAIL,
            detail=(
                "The market-data evidence tables could not be inspected: "
                f"{facts.evidence_store_error}."
            ),
            remedy=(
                "Fix shared-database connectivity, then confirm the evidence tables "
                f"exist. {_MIGRATION_REMEDY}"
            ),
        )
    if facts.missing_evidence_tables:
        missing = ", ".join(facts.missing_evidence_tables)
        return ReadinessCheck(
            name="market-data-evidence-schema",
            status=CheckStatus.FAIL,
            detail=(
                f"The shared database is missing {missing}. A session needing the "
                "five-minute daily fallback would fail for every symbol as a provider "
                "error and block the cohort."
            ),
            remedy=_MIGRATION_REMEDY,
        )
    return ReadinessCheck(
        name="market-data-evidence-schema",
        status=CheckStatus.PASS,
        detail="The market-data evidence tables exist, so the daily fallback can persist.",
    )


def _alert_store_check(facts: ReadinessFacts) -> ReadinessCheck:
    if facts.alert_store_error is not None:
        return ReadinessCheck(
            name="alert-record",
            status=CheckStatus.FAIL,
            detail=f"The alert transition store could not be read: {facts.alert_store_error}.",
            remedy=(
                "Alerts deduplicate on a durable record; without it, repeated scheduler "
                "invocations would resend. Fix the store before scheduling."
            ),
        )
    return ReadinessCheck(
        name="alert-record",
        status=CheckStatus.PASS,
        detail="Alert transitions are recorded durably, so repeated runs cannot resend.",
    )


def _notification_check(facts: ReadinessFacts) -> ReadinessCheck:
    if not facts.notifications_live:
        return ReadinessCheck(
            name="notifications",
            status=CheckStatus.WARN,
            detail=(
                "No notification channel is configured. Transitions are still recorded, "
                "but nothing is delivered, so a missed session is silent."
            ),
            remedy=(
                "Configure SCHWAB_SMTP_HOST and SCHWAB_NOTIFY_TO in the local .env to "
                "receive completion and failure alerts."
            ),
        )
    return ReadinessCheck(
        name="notifications",
        status=CheckStatus.PASS,
        detail="A notification channel is configured for cohort lifecycle alerts.",
    )


def _kill_switch_check(facts: ReadinessFacts) -> ReadinessCheck:
    if facts.kill_switch_engaged:
        return ReadinessCheck(
            name="kill-switch",
            status=CheckStatus.FAIL,
            detail=(
                "The global kill switch is engaged. Scheduled runs would record the "
                "session as missed rather than producing evidence."
            ),
            remedy="Resolve why the kill switch was engaged, then 'schwab-trader safety resume'.",
        )
    return ReadinessCheck(
        name="kill-switch",
        status=CheckStatus.PASS,
        detail="The global kill switch is clear.",
    )


def assess_readiness(facts: ReadinessFacts) -> ReadinessReport:
    """Evaluate every scheduler precondition. Pure, non-mutating, fails closed."""
    return ReadinessReport(
        facts=facts,
        checks=(
            _writer_check(facts),
            _cohort_check(facts),
            _membership_check(facts),
            _schema_check(facts),
            _evidence_schema_check(facts),
            _alert_store_check(facts),
            _notification_check(facts),
            _kill_switch_check(facts),
        ),
    )


def readiness_payload(report: ReadinessReport) -> dict[str, Any]:
    """The stable JSON contract for :class:`ReadinessReport`.

    Carries verdicts and remedies only — no connection string, credential, account
    identifier, or token can reach it, because :class:`ReadinessFacts` never holds one.
    """
    return {
        "schema": PAYLOAD_SCHEMA,
        "cohort_id": report.facts.cohort_id,
        "cohort_lifecycle": cohort_lifecycle.status_for(report.facts.cohort_id).lifecycle.value,
        "ready": report.ready,
        "exit_code": report.exit_code,
        "summary": report.summary,
        "storage": report.facts.storage_kind,
        "writer_role": report.facts.writer_role,
        "members": {
            "registered": report.facts.member_count,
            "reproducible": report.facts.reproducible_members,
        },
        "notifications_live": report.facts.notifications_live,
        "checks": [
            {
                "name": check.name,
                "status": check.status.value,
                "detail": check.detail,
                "remedy": check.remedy,
            }
            for check in report.checks
        ],
    }


__all__ = [
    "EXIT_NOT_READY",
    "EXIT_READY",
    "PAYLOAD_SCHEMA",
    "CheckStatus",
    "ReadinessCheck",
    "ReadinessFacts",
    "ReadinessReport",
    "assess_readiness",
    "readiness_payload",
]
