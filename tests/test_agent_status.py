"""Offline tests for deterministic GitHub agent-status management."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import pytest


def _load_helper() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "agent_status.py"
    spec = importlib.util.spec_from_file_location("agent_status_under_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load status helper at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


agent_status = _load_helper()


class FakeRunner:
    def __init__(
        self,
        *,
        issue_labels: list[str] | None = None,
        repository_labels: list[str] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.issue_labels = set(issue_labels or [])
        self.repository_labels = set(repository_labels or [])

    def __call__(self, command: Sequence[str]) -> str:
        call = list(command)
        self.calls.append(call)
        if call[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": name} for name in sorted(self.issue_labels)]})
        if call[:3] == ["gh", "label", "list"]:
            return json.dumps([{"name": name} for name in sorted(self.repository_labels)])
        if call[:3] == ["gh", "label", "edit"]:
            old = call[3]
            new = call[call.index("--name") + 1]
            self.repository_labels.remove(old)
            self.repository_labels.add(new)
            return ""
        if call[:2] == ["gh", "api"] and "--method" in call:
            self.issue_labels = {
                call[index + 1].removeprefix("labels[]=")
                for index, value in enumerate(call)
                if value == "-f" and call[index + 1].startswith("labels[]=")
            }
            return "{}"
        if call[:3] == ["gh", "issue", "list"]:
            return json.dumps(
                [{"number": 1, "labels": [{"name": name} for name in self.issue_labels]}]
            )
        return ""


def test_agent_status_colors_are_distinct() -> None:
    colors = [definition[0] for definition in agent_status.AGENT_STATUS_LABELS.values()]
    assert len(colors) == len(set(colors))


def test_multiple_agent_statuses_are_rejected() -> None:
    with pytest.raises(agent_status.StatusError, match="multiple agent statuses"):
        agent_status.agent_status({"agent:ready", "agent:running"}, context="Issue #1")


def test_historical_agent_owner_label_is_not_a_status() -> None:
    assert agent_status.agent_status({"agent:someone"}, context="Issue #1") is None


def test_legacy_and_current_status_cannot_be_mixed() -> None:
    with pytest.raises(agent_status.StatusError, match="mixes agent and legacy"):
        agent_status.agent_status({"status:claimed", "agent:running"}, context="Issue #1")


def test_transition_atomically_replaces_only_the_status_label() -> None:
    runner = FakeRunner(issue_labels=["enhancement", "agent:ready"])

    changed = agent_status.transition_issue(runner, "owner/repo", 7, "agent:running")

    assert changed
    assert runner.issue_labels == {"enhancement", "agent:running"}
    api_calls = [call for call in runner.calls if call[:2] == ["gh", "api"]]
    assert len(api_calls) == 1
    assert "agent:ready" not in api_calls[0]


def test_transition_rejects_invalid_edge_without_mutation() -> None:
    runner = FakeRunner(issue_labels=["agent:ready"])

    with pytest.raises(agent_status.StatusError, match="Disallowed"):
        agent_status.transition_issue(runner, "owner/repo", 7, "agent:review")

    assert not any(call[:2] == ["gh", "api"] for call in runner.calls)


def test_done_requires_completion_evidence() -> None:
    runner = FakeRunner(issue_labels=["agent:review"])

    with pytest.raises(agent_status.StatusError, match="completion evidence"):
        agent_status.transition_issue(runner, "owner/repo", 7, "agent:done")


def test_blocked_transition_requires_comment_evidence() -> None:
    runner = FakeRunner(issue_labels=["agent:running"])

    with pytest.raises(agent_status.StatusError, match="blocker and unblocking"):
        agent_status.transition_issue(runner, "owner/repo", 7, "agent:blocked")


def test_blocked_cli_comments_before_transition() -> None:
    runner = FakeRunner(issue_labels=["agent:running"])

    result = agent_status.main(
        [
            "--repo",
            "owner/repo",
            "transition",
            "7",
            "agent:blocked",
            "--blocker",
            "required service unavailable",
            "--unblocks-when",
            "service is restored",
        ],
        runner=runner,
    )

    assert result == 0
    comment_index = next(
        index for index, call in enumerate(runner.calls) if call[:3] == ["gh", "issue", "comment"]
    )
    patch_index = next(
        index for index, call in enumerate(runner.calls) if call[:2] == ["gh", "api"]
    )
    assert comment_index < patch_index
    assert runner.issue_labels == {"agent:blocked"}


def test_reopening_from_done_requires_explicit_decision() -> None:
    runner = FakeRunner(issue_labels=["agent:done"])

    with pytest.raises(agent_status.StatusError, match="reopening decision"):
        agent_status.transition_issue(runner, "owner/repo", 7, "agent:ready")

    agent_status.transition_issue(
        runner,
        "owner/repo",
        7,
        "agent:ready",
        reopen_decision="operator selected ready",
    )
    assert runner.issue_labels == {"agent:ready"}


def test_label_sync_is_idempotent_when_canonical_labels_exist() -> None:
    existing = [*agent_status.STANDARD_LABELS, "enhancement"]
    runner = FakeRunner(repository_labels=existing)

    agent_status.sync_labels(runner, "owner/repo")
    first_calls = list(runner.calls)
    runner.calls.clear()
    agent_status.sync_labels(runner, "owner/repo")

    assert runner.calls == first_calls
    assert not any(call[:3] == ["gh", "label", "edit"] for call in runner.calls)
    assert all("--force" in call for call in runner.calls if call[:3] == ["gh", "label", "create"])


def test_label_sync_does_not_rewrite_legacy_labels() -> None:
    runner = FakeRunner(repository_labels=["status:claimed", "agent:codex-win"])

    agent_status.sync_labels(runner, "owner/repo")

    assert "status:claimed" in runner.repository_labels
    assert "agent:codex-win" in runner.repository_labels
    assert not any(call[:3] == ["gh", "label", "edit"] for call in runner.calls)


def test_closing_reference_parser_accepts_only_keywords_and_issue_numbers() -> None:
    body = "Closes #12; fixes #7. Text $(ignored) and mentions #99."
    assert agent_status.closing_issue_numbers(body) == [12, 7]


def test_closed_issue_in_review_moves_to_done() -> None:
    runner = FakeRunner(issue_labels=["agent:review", "enhancement"])
    payload = {
        "action": "closed",
        "issue": {
            "number": 7,
            "labels": [
                {"name": "agent:review"},
                {"name": "enhancement"},
            ],
        },
    }

    changed = agent_status.reconcile_event(runner, "owner/repo", payload)

    assert changed == [7]
    assert runner.issue_labels == {"agent:done", "enhancement"}


def test_merged_pr_closing_reference_moves_review_issue_to_done() -> None:
    runner = FakeRunner(issue_labels=["agent:review", "enhancement"])
    payload = {
        "action": "closed",
        "pull_request": {
            "number": 42,
            "merged": True,
            "body": "Summary\n\nCloses #7",
        },
    }

    changed = agent_status.reconcile_event(runner, "owner/repo", payload)

    assert changed == [7]
    assert runner.issue_labels == {"agent:done", "enhancement"}


def test_merged_pr_reconciles_out_of_sequence_managed_issue_to_done() -> None:
    runner = FakeRunner(issue_labels=["agent:running"])
    payload = {
        "action": "closed",
        "pull_request": {"number": 42, "merged": True, "body": "Closes #7"},
    }

    changed = agent_status.reconcile_event(runner, "owner/repo", payload)

    assert changed == [7]
    assert runner.issue_labels == {"agent:done"}


def test_reopened_done_issue_requires_operator_choice_and_keeps_status() -> None:
    runner = FakeRunner(issue_labels=["agent:done"])
    payload = {
        "action": "reopened",
        "issue": {"number": 7, "labels": [{"name": "agent:done"}]},
    }

    changed = agent_status.reconcile_event(runner, "owner/repo", payload)

    assert changed == []
    assert runner.issue_labels == {"agent:done"}
    comment = next(call for call in runner.calls if call[:3] == ["gh", "issue", "comment"])
    assert "operator must explicitly choose" in comment[-1]


def test_repository_validation_detects_conflicts() -> None:
    runner = FakeRunner(issue_labels=["agent:running", "agent:review"])

    with pytest.raises(agent_status.StatusError, match="multiple agent statuses"):
        agent_status.validate_repository(runner, "owner/repo")
