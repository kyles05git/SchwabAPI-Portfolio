"""Offline tests for the deterministic dashboard fixtures.

The fixtures are the frontend's only source of truth for manual UI inspection, so they
must stay valid against the live API contract and must keep asserting the behavior the
redesign exists to guarantee. Everything here builds view models in memory: no storage
engine is opened, no ``.env`` is read, no network call is made, and every scenario pins
an explicit Eastern instant instead of using the wall clock.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest

from schwab_trader.dashboard import DashboardData, SleeveScope

_ROOT = Path(__file__).parents[1]
_SCRIPT_PATH = _ROOT / "scripts" / "dashboard_fixtures.py"
_SPEC = importlib.util.spec_from_file_location("dashboard_fixtures_script", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
fixtures = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fixtures)

FIXTURE_DIR = _ROOT / "frontend" / "src" / "fixtures"
NAMES = sorted(fixtures.FIXTURES)

REQUIRED_STATES = {
    "no-cohort",
    "scheduled",
    "pre-close",
    "awaiting-execution",
    "run-late",
    "first-session-complete",
    "collecting",
    "collecting-partial",
    "review-ready",
    "review-recorded",
    "passed",
    "failed",
    "multiple-cohorts",
    "ambiguous-cohorts",
    "historical-cohorts",
    "historical-cohort-selected",
    "duplicate-names",
}


def load(name: str) -> DashboardData:
    """Parse a committed fixture back through the live pydantic contract."""
    return DashboardData.model_validate_json((FIXTURE_DIR / f"{name}.json").read_text("utf-8"))


def test_every_required_cohort_state_has_a_fixture():
    assert REQUIRED_STATES <= set(NAMES)


@pytest.mark.parametrize("name", NAMES)
def test_committed_fixture_matches_the_current_contract(name: str):
    """The committed JSON has not drifted from what the generator produces today."""
    path = FIXTURE_DIR / f"{name}.json"
    assert path.is_file(), f"missing fixture {name}; run scripts/dashboard_fixtures.py"
    assert path.read_text("utf-8") == fixtures.build_all()[name], (
        f"fixture {name} is stale; re-run scripts/dashboard_fixtures.py"
    )


@pytest.mark.parametrize("name", NAMES)
def test_fixture_parses_as_dashboard_data(name: str):
    data = load(name)
    assert data.api_version == "3.5"
    assert data.live_enabled is False
    assert data.cohort.contract_version == "2.4"


@pytest.mark.parametrize("name", NAMES)
def test_no_fixture_ever_reports_authorization(name: str):
    """The gate's authorization contract is invariant across every rendered state."""
    gate = load(name).cohort.operational_gate
    assert gate.investment_alpha_assessed is False
    assert gate.live_trading_authorized is False


# The two states that deliberately render no cohort at all: nothing is persisted, and
# nothing could be chosen without guessing. Neither has sleeves to name.
_NO_COHORT_RENDERED = {"no-cohort", "ambiguous-cohorts"}


@pytest.mark.parametrize("name", sorted(REQUIRED_STATES - _NO_COHORT_RENDERED))
def test_every_rendered_cohort_serves_the_review_section(name: str):
    """The review is part of the contract in every state, not only once it is recorded.

    ``available`` false is reserved for a store that could not be read. A cohort with
    nothing recorded must still say so through a readable, empty section — conflating
    "unreadable" with "unreviewed" is exactly how an operator ends up trusting a review
    that never happened.
    """
    review = load(name).cohort.cohort_review
    assert review.available is True
    assert review.review_target > 0


def test_the_recorded_review_fixture_renders_every_state_the_panel_distinguishes():
    """One fixture must exercise all four, or the panel's other branches are untested."""
    review = load("review-recorded").cohort.cohort_review

    assert review.review_due is True
    assert review.reviewed_observations == review.official_observations > 0
    assert review.recorded_check_count > review.official_observations  # three areas each
    # An explained difference, an unexplained one, a note, and a superseded decision.
    explained = [item for item in review.differences if item.explained]
    unexplained = [item for item in review.differences if not item.explained]
    assert explained and unexplained
    assert review.unexplained_difference_count == len(unexplained)
    assert review.notes
    assert review.superseded_decisions
    assert review.decisions
    # The superseded decision is preserved beside the one that replaced it, for the same
    # sleeve, at a lower revision.
    superseded = review.superseded_decisions[0]
    current = next(item for item in review.decisions if item.sleeve_id == superseded.sleeve_id)
    assert current.revision > superseded.revision
    assert current.action != superseded.action


def test_a_recorded_review_still_reports_no_authorization():
    """Recording evidence must not be able to move the authorization contract."""
    cohort = load("review-recorded").cohort
    rules = {rule.rule: rule for rule in cohort.operational_gate.rules}

    # The two rules only a human can satisfy now read real recorded evidence...
    assert rules["operator-decisions"].status == "pass"
    assert rules["operator-decisions"].awaiting_evidence is False
    # ...and the unexplained difference keeps the accounting rule actionable, not absent.
    assert rules["accounting-states"].status == "fail"
    assert rules["accounting-states"].awaiting_evidence is False
    assert rules["accounting-states"].presentation == "needs-attention"

    assert cohort.operational_gate.investment_alpha_assessed is False
    assert cohort.operational_gate.live_trading_authorized is False


@pytest.mark.parametrize("name", sorted(REQUIRED_STATES - _NO_COHORT_RENDERED))
def test_human_readable_names_accompany_every_stable_id(name: str):
    cohort = load(name).cohort
    assert cohort.sleeve_names
    for definition in cohort.sleeve_definitions:
        assert definition.sleeve_name
        assert definition.sleeve_name != definition.sleeve_id
    for row in cohort.readiness:
        assert row.sleeve_name and row.sleeve_name != row.sleeve_id
    for row in cohort.comparison.sleeves:
        assert row.sleeve_name and row.sleeve_name != row.sleeve_id
    for run in cohort.run_health.runs:
        for member in run.members:
            assert member.sleeve_name and member.sleeve_name != member.sleeve_id


def test_scheduled_cohort_reads_as_scheduled_not_failed():
    """The real July-27 state must never present as a failure."""
    cohort = load("scheduled").cohort
    phase = cohort.phase
    assert phase is not None
    assert phase.phase == "scheduled"
    assert phase.started is False
    assert phase.due_sessions == 0
    assert phase.completed_due_sessions == 0
    assert phase.official_observations == 0
    # A future session is not a missing observation.
    assert phase.completion_reliability is None
    assert phase.integrity_alerts == []
    assert phase.headline == "Starts July 27, 2026"
    assert phase.next_action == "Run the cohort after the July 27, 2026 market close."

    gate = cohort.operational_gate
    # The gate itself still fails closed...
    assert gate.status == "fail"
    assert gate.operationally_useful is False
    # ...but nothing is presented to the operator as an actionable failure yet.
    assert [rule.rule for rule in gate.rules if rule.presentation == "needs-attention"] == []
    assert all(
        rule.presentation in {"healthy", "awaiting-evidence"} for rule in gate.rules
    )


def test_pre_close_start_day_is_not_a_failure():
    """9:28 AM ET on the cohort's own start date: the exact reported bug state."""
    cohort = load("pre-close").cohort
    phase = cohort.phase
    assert phase is not None
    assert phase.phase == "scheduled"
    assert phase.timing_state == "upcoming"
    assert phase.due_sessions == 0
    # The seven un-produced observations are not unexplained slots.
    assert phase.completion_reliability is None
    assert phase.official_observations == 0
    assert phase.integrity_alerts == []
    assert phase.evidence_cutoff == date(2026, 7, 24)
    assert phase.next_action == "Run the cohort after the July 27, 2026 market close."

    gate = cohort.operational_gate
    # The gate still fails closed...
    assert gate.status == "fail"
    assert gate.operationally_useful is False
    # ...but nothing is presented as an actionable failure, and in particular the
    # pending run's absent snapshot lineage is not a reproducibility defect.
    assert [rule.rule for rule in gate.rules if rule.presentation == "needs-attention"] == []
    reproducibility = next(r for r in gate.rules if r.rule == "reproducibility")
    assert reproducibility.awaiting_evidence is True
    assert all("snapshot lineage missing" not in item for item in reproducibility.evidence)

    # The pending run is still visible, labelled upcoming rather than overdue.
    runs = cohort.run_health.runs
    assert [run.timing for run in runs] == ["upcoming"]
    assert cohort.run_health.upcoming_runs == 1
    assert cohort.run_health.overdue_runs == 0


def test_post_close_run_is_due_and_awaiting_execution():
    """16:05 ET: due, awaiting execution — not a completion or reproducibility failure."""
    cohort = load("awaiting-execution").cohort
    phase = cohort.phase
    assert phase is not None
    assert phase.timing_state == "awaiting-execution"
    assert phase.awaiting_execution_session == date(2026, 7, 27)
    assert phase.phase != "attention-needed"
    assert phase.due_sessions == 0
    assert phase.completion_reliability is None
    assert phase.integrity_alerts == []
    assert phase.headline == "Run due — awaiting execution"
    assert phase.next_action == "Run today's cohort for the July 27, 2026 session."

    gate = cohort.operational_gate
    assert [rule.rule for rule in gate.rules if rule.presentation == "needs-attention"] == []
    assert cohort.run_health.awaiting_execution_runs == 1


def test_a_run_past_its_grace_period_is_surfaced_immediately():
    cohort = load("run-late").cohort
    phase = cohort.phase
    assert phase is not None
    assert phase.timing_state == "overdue"
    assert phase.overdue_sessions == [date(2026, 7, 27)]
    assert phase.phase == "attention-needed"
    assert phase.due_sessions == 1
    assert phase.completion_reliability == 0.0
    assert "never executed" in phase.integrity_alerts[0]
    assert cohort.run_health.overdue_runs == 1
    # The genuine gap is real negative evidence, not merely unrecorded evidence.
    completion = next(r for r in cohort.operational_gate.rules if r.rule == "completion-rate")
    assert completion.status == "fail"
    assert completion.presentation == "needs-attention"


def test_the_first_completed_session_counts_exactly_once():
    cohort = load("first-session-complete").cohort
    phase = cohort.phase
    assert phase is not None
    assert phase.phase == "collecting"
    assert phase.due_sessions == 1
    assert phase.completed_due_sessions == 1
    assert phase.completion_reliability == 1.0
    assert phase.integrity_alerts == []
    # The executed session is evidence; the next one is still upcoming.
    timings = sorted(run.timing for run in cohort.run_health.runs)
    assert timings == ["executed", "upcoming"]


def test_scheduled_cohort_renders_one_readiness_empty_state():
    """Seven identical 'unavailable' cards are replaced by a single message."""
    cohort = load("scheduled").cohort
    assert len(cohort.readiness) == 7
    assert all(row.evidence_status == "unavailable" for row in cohort.readiness)
    assert cohort.comparison.available is False


def test_collecting_partial_run_surfaces_as_an_integrity_alert():
    phase = load("collecting-partial").cohort.phase
    assert phase is not None
    assert phase.phase == "attention-needed"
    assert phase.due_sessions == 12
    assert phase.completed_due_sessions == 11
    assert len(phase.integrity_alerts) == 1
    assert "partial" in phase.integrity_alerts[0]


def test_collecting_cohort_has_no_alerts_and_full_reliability():
    phase = load("collecting").cohort.phase
    assert phase is not None
    assert phase.phase == "collecting"
    assert phase.completion_reliability == 1.0
    assert phase.integrity_alerts == []
    assert phase.sessions_remaining == 18


def test_review_ready_distinguishes_awaiting_evidence_from_failure():
    cohort = load("review-ready").cohort
    phase = cohort.phase
    assert phase is not None
    assert phase.phase == "review-ready"
    assert phase.completed_due_sessions == 30

    by_presentation = {rule.rule: rule.presentation for rule in cohort.operational_gate.rules}
    # Unrecorded review evidence is actionable once the review is owed...
    assert by_presentation["accounting-states"] == "needs-attention"
    assert by_presentation["operator-decisions"] == "needs-attention"
    # ...while evidence that was actually produced reads healthy.
    assert by_presentation["session-history"] == "healthy"
    assert by_presentation["completion-rate"] == "healthy"
    assert by_presentation["data-readiness"] == "healthy"


def test_failed_review_reports_genuine_defects():
    cohort = load("failed").cohort
    phase = cohort.phase
    assert phase is not None
    assert phase.phase == "failed"
    by_rule = {rule.rule: rule for rule in cohort.operational_gate.rules}
    duplicate = by_rule["duplicate-observations"]
    reproducibility = by_rule["reproducibility"]
    # These are real defects, never merely "awaiting evidence".
    assert duplicate.status == "fail" and duplicate.awaiting_evidence is False
    assert reproducibility.status == "fail" and reproducibility.awaiting_evidence is False
    assert duplicate.presentation == "needs-attention"
    assert len(phase.integrity_alerts) >= 2


def test_passed_review_still_withholds_authorization():
    cohort = load("passed").cohort
    assert cohort.phase is not None and cohort.phase.phase == "passed"
    gate = cohort.operational_gate
    assert gate.operationally_useful is True
    assert gate.investment_alpha_assessed is False
    assert gate.live_trading_authorized is False


def test_selector_is_meaningful_only_with_multiple_cohorts():
    assert len(load("scheduled").cohort.selection.available) == 1
    assert len(load("multiple-cohorts").cohort.selection.available) == 2


def test_no_ordinary_fixture_presents_a_superseded_cohort_as_running():
    """Guards the fixtures against silently borrowing a withdrawn cohort's id."""
    for name in sorted(REQUIRED_STATES - {"no-cohort", "historical-cohort-selected"}):
        selection = load(name).cohort.selection
        assert selection.selected_is_historical is False, name


def test_the_default_selection_skips_the_superseded_cohort():
    """Both real cohorts persisted: the default is the one still collecting.

    `paper-first-2026-07-27` sorts first, which is exactly how it used to be chosen.
    """
    selection = load("historical-cohorts").cohort.selection

    assert selection.available == ["paper-first-2026-07-27", "paper-first-2026-07-28"]
    assert selection.selected == "paper-first-2026-07-28"
    assert selection.active == ["paper-first-2026-07-28"]
    assert selection.requested is None
    assert selection.selected_is_historical is False


def test_the_superseded_cohort_is_offered_with_its_label_and_replacement():
    historical = load("historical-cohorts").cohort.selection.historical

    assert [item.cohort_id for item in historical] == ["paper-first-2026-07-27"]
    entry = historical[0]
    assert entry.lifecycle == "superseded"
    assert entry.label == "Superseded — partial incident, do not run"
    assert entry.superseded_by == "paper-first-2026-07-28"
    assert entry.reference.endswith("2026-07-27-paper-first-cohort-daily-bars-stale.md")


def test_an_explicitly_selected_historical_cohort_renders_its_whole_record():
    """Archiving is not hiding: the withdrawn cohort's own view is fully assembled."""
    cohort = load("historical-cohort-selected").cohort

    assert cohort.available is True
    assert cohort.selection.requested == "paper-first-2026-07-27"
    assert cohort.selection.selected == "paper-first-2026-07-27"
    assert cohort.selection.selected_is_historical is True
    assert cohort.identity is not None
    assert cohort.identity.cohort_id == "paper-first-2026-07-27"
    assert len(cohort.identity.member_names) == 7
    assert cohort.phase is not None
    assert cohort.sleeve_definitions
    # Its terminal partial session is reported as recorded, not tidied away.
    assert cohort.phase.latest_due_run is not None
    assert cohort.phase.latest_due_run.status == "partial"


def test_official_and_legacy_sleeves_are_never_co_ranked():
    """Duplicate display names across scopes must not collapse into one leaderboard."""
    rows = load("duplicate-names").sleeves
    official = [row for row in rows if row.scope is SleeveScope.OFFICIAL_COHORT]
    legacy = [row for row in rows if row.scope is not SleeveScope.OFFICIAL_COHORT]
    assert official and legacy

    # The same human-readable name appears in both scopes...
    shared = {row.name for row in official} & {row.name for row in legacy}
    assert "bench-spy" in shared and "trend-large" in shared
    # ...but every stable id is unique, and each scope ranks from 1 independently.
    assert len({row.sleeve_id for row in rows}) == len(rows)
    assert min(row.rank for row in official) == 1
    assert min(row.rank for row in legacy) == 1

    # Capital differs, which is precisely why the two groups are not comparable.
    assert {row.starting_capital for row in official} != {row.starting_capital for row in legacy}

    # A legacy sleeve's excess is only ever measured against its own group's benchmark.
    for row in legacy:
        assert row.excess_benchmark in (None, "bench-spy")
        if row.excess_pct is not None:
            assert row.cohort_id is None


def test_no_cohort_state_explains_itself():
    cohort = load("no-cohort").cohort
    assert cohort.available is False
    assert cohort.phase is None
    assert cohort.message == "No persisted paper cohorts are available."


def test_fixture_json_is_stable_across_regeneration():
    """Regeneration is deterministic: no timestamps, hashes, or ordering drift."""
    assert fixtures.build_all() == fixtures.build_all()


def test_fixtures_are_valid_json_documents():
    for name in NAMES:
        payload = json.loads((FIXTURE_DIR / f"{name}.json").read_text("utf-8"))
        assert payload["api_version"] == "3.5"
