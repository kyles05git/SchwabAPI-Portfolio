"""Offline tests for the scheduler readiness check.

The assessment is pure, so these tests only construct facts. Nothing here reads
``.env``, opens a database, reaches the network, or — the point of the module —
registers, enables, disables, or removes an operating-system scheduled task.
"""

from __future__ import annotations

import json

import pytest

from schwab_trader import cohort_readiness
from schwab_trader.cohort_readiness import CheckStatus, ReadinessFacts, assess_readiness

COHORT = "paper-first-2026-07-28"
SUPERSEDED_COHORT = "paper-first-2026-07-27"


def facts(**overrides: object) -> ReadinessFacts:
    """A machine that is fully ready, so each test states only what it changes."""
    base: dict[str, object] = {
        "cohort_id": COHORT,
        "writer_role": True,
        "shared_database": True,
        "storage_kind": "shared-postgresql",
        "member_count": 7,
        "reproducible_members": 7,
        "schema_error": None,
        "alert_store_error": None,
        "notifications_live": True,
        "kill_switch_engaged": False,
    }
    base.update(overrides)
    return ReadinessFacts(**base)  # type: ignore[arg-type]


def status_of(report: cohort_readiness.ReadinessReport, name: str) -> CheckStatus:
    return next(check.status for check in report.checks if check.name == name)


def test_a_fully_configured_writer_machine_is_ready() -> None:
    report = assess_readiness(facts())
    assert report.ready
    assert report.exit_code == cohort_readiness.EXIT_READY
    assert report.blocking == ()
    assert all(check.status is CheckStatus.PASS for check in report.checks)


# --- The one-writer rule fails closed ------------------------------------------


def test_an_unset_writer_flag_is_a_refusal_not_an_assumption() -> None:
    """Silence about the writer role must never be read as "probably this machine"."""
    report = assess_readiness(facts(writer_role=False))
    assert not report.ready
    assert status_of(report, "one-writer") is CheckStatus.FAIL
    remedy = next(check.remedy for check in report.checks if check.name == "one-writer")
    assert "SCHWAB_COHORT_WRITER" in remedy


def test_a_read_only_dashboard_client_is_told_why_it_may_not_schedule() -> None:
    report = assess_readiness(facts(writer_role=False, shared_database=True))
    detail = next(check.detail for check in report.checks if check.name == "one-writer")
    assert "not configured as the cohort writer" in detail
    assert "shared database" in detail


def test_a_local_only_writer_is_trivially_the_single_writer() -> None:
    report = assess_readiness(
        facts(shared_database=False, storage_kind="local-sqlite")
    )
    assert report.ready
    assert status_of(report, "one-writer") is CheckStatus.PASS


# --- Each remaining precondition blocks on its own -------------------------------


@pytest.mark.parametrize(
    ("overrides", "failing_check"),
    [
        ({"cohort_id": ""}, "cohort-identity"),
        ({"cohort_id": "   "}, "cohort-identity"),
        ({"cohort_id": SUPERSEDED_COHORT}, "cohort-identity"),
        ({"member_count": 0, "reproducible_members": 0}, "cohort-membership"),
        ({"reproducible_members": 5}, "cohort-membership"),
        ({"schema_error": "OperationalError"}, "schema-compatibility"),
        (
            {"missing_evidence_tables": ("market_data_daily_evidence",)},
            "market-data-evidence-schema",
        ),
        ({"evidence_store_error": "ProgrammingError"}, "market-data-evidence-schema"),
        ({"alert_store_error": "ProgrammingError"}, "alert-record"),
        ({"kill_switch_engaged": True}, "kill-switch"),
    ],
)
def test_each_ambiguous_or_broken_precondition_blocks(
    overrides: dict[str, object], failing_check: str
) -> None:
    report = assess_readiness(facts(**overrides))
    assert not report.ready
    assert report.exit_code == cohort_readiness.EXIT_NOT_READY
    assert status_of(report, failing_check) is CheckStatus.FAIL
    assert [check.name for check in report.blocking] == [failing_check]


def test_a_dark_notification_channel_warns_but_does_not_block() -> None:
    """Scheduling still produces correct evidence; the operator just hears nothing."""
    report = assess_readiness(facts(notifications_live=False))
    assert report.ready
    assert status_of(report, "notifications") is CheckStatus.WARN
    assert len(report.warnings) == 1
    assert "1 warning" in report.summary


def test_a_superseded_cohort_may_never_be_scheduled() -> None:
    """A withdrawn experiment is not a scheduling target, however ready the machine is.

    Everything else here is green: the writer flag, the members, the stores, the kill
    switch. The refusal comes from the cohort's lifecycle alone, and it names the
    replacement so the operator's next step is unambiguous.
    """
    report = assess_readiness(facts(cohort_id=SUPERSEDED_COHORT))

    assert not report.ready
    assert report.exit_code == cohort_readiness.EXIT_NOT_READY
    check = next(item for item in report.checks if item.name == "cohort-identity")
    assert check.status is CheckStatus.FAIL
    assert "superseded" in check.detail
    assert COHORT in check.remedy
    # No other precondition was blamed for it.
    assert {item.name for item in report.blocking} == {"cohort-identity"}


def test_the_readiness_payload_reports_the_cohort_lifecycle() -> None:
    active = cohort_readiness.readiness_payload(assess_readiness(facts()))
    retired = cohort_readiness.readiness_payload(
        assess_readiness(facts(cohort_id=SUPERSEDED_COHORT))
    )
    assert active["cohort_lifecycle"] == "active"
    assert retired["cohort_lifecycle"] == "superseded"


def test_several_failures_are_all_reported_not_just_the_first() -> None:
    report = assess_readiness(
        facts(writer_role=False, cohort_id="", kill_switch_engaged=True)
    )
    assert {check.name for check in report.blocking} == {
        "one-writer",
        "cohort-identity",
        "kill-switch",
    }
    assert "3 blocking" in report.summary


def test_a_report_with_no_checks_is_never_ready() -> None:
    """Fail closed: an empty checklist is absence of evidence, not evidence of safety."""
    empty = cohort_readiness.ReadinessReport(facts=facts())
    assert not empty.ready
    assert empty.exit_code == cohort_readiness.EXIT_NOT_READY


# --- The JSON contract -----------------------------------------------------------


def test_payload_is_json_serializable_and_states_every_verdict() -> None:
    report = assess_readiness(facts(notifications_live=False))
    restored = json.loads(json.dumps(cohort_readiness.readiness_payload(report), sort_keys=True))

    assert restored["schema"] == cohort_readiness.PAYLOAD_SCHEMA
    assert restored["ready"] is True
    assert restored["exit_code"] == 0
    assert restored["cohort_id"] == COHORT
    assert restored["members"] == {"registered": 7, "reproducible": 7}
    names = [check["name"] for check in restored["checks"]]
    assert names == [
        "one-writer",
        "cohort-identity",
        "cohort-membership",
        "schema-compatibility",
        "market-data-evidence-schema",
        "alert-record",
        "notifications",
        "kill-switch",
    ]


def test_payload_exposes_no_connection_string_or_credential() -> None:
    report = assess_readiness(facts(schema_error="OperationalError"))
    text = json.dumps(cohort_readiness.readiness_payload(report)).lower()
    for forbidden in ("postgresql://", "postgres://", "sqlite:///", "password", "token", "sslmode"):
        assert forbidden not in text, forbidden
    assert "shared-postgresql" in text  # the category, never the URL
