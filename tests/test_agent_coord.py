"""Offline tests for the cross-agent GitHub coordination helper."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest


def _load_helper() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "agent_coord.py"
    spec = importlib.util.spec_from_file_location("agent_coord_under_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load coordination helper at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


agent_coord = _load_helper()


class FakeRunner:
    def __init__(self, *, dirty: bool = False, labels: list[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.dirty = dirty
        self.labels = labels or []

    def __call__(self, command: Sequence[str]) -> str:
        call = list(command)
        self.calls.append(call)
        if call[:3] == ["git", "branch", "--show-current"]:
            return "task-13-agent-coordination-protocol"
        if call[:3] == ["git", "rev-parse", "HEAD"]:
            return "head-sha"
        if call[:3] == ["git", "rev-parse", "origin/main"]:
            return "base-sha"
        if call[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return "/worktrees/task-13"
        if call[:3] == ["git", "status", "--porcelain"]:
            return " M README.md" if self.dirty else ""
        if call[:3] == ["gh", "issue", "view"]:
            return json.dumps({"labels": [{"name": label} for label in self.labels]})
        if call[:3] == ["gh", "label", "list"]:
            return json.dumps([{"name": label} for label in agent_coord.STANDARD_LABELS])
        if call[:2] == ["gh", "api"]:
            self.labels = [
                call[index + 1].removeprefix("labels[]=")
                for index, value in enumerate(call)
                if value == "-f" and call[index + 1].startswith("labels[]=")
            ]
            return "{}"
        if call[:3] == ["gh", "issue", "comment"]:
            return "https://example.test/comment"
        if call[:3] in (["gh", "issue", "list"], ["gh", "pr", "list"]):
            return "[]"
        return ""


def _client(runner: FakeRunner) -> agent_coord.CommandClient:
    return agent_coord.CommandClient(runner=runner)


def _comment_body(runner: FakeRunner) -> str:
    comment = next(call for call in runner.calls if call[:3] == ["gh", "issue", "comment"])
    return comment[comment.index("--body") + 1]


def test_claim_posts_reproducible_lease_and_sets_labels() -> None:
    runner = FakeRunner(labels=["agent:ready", "enhancement"])
    url = agent_coord.claim_issue(
        _client(runner),
        repo="owner/repo",
        issue=13,
        agent="codex-win",
        provider="OpenAI Codex",
        model="GPT-5",
        areas="docs, CI",
        dependencies="none",
        conflicts="none",
        machine="clean checkout",
        branch=None,
        lease_hours=24,
        now=datetime(2026, 7, 21, tzinfo=UTC),
    )

    assert url == "https://example.test/comment"
    body = _comment_body(runner)
    assert "Agent/provider: codex-win / OpenAI Codex" in body
    assert "Model: GPT-5" in body
    assert "Worktree: /worktrees/task-13" in body
    assert "Base: base-sha" in body
    assert "Lease until: 2026-07-22 00:00 UTC" in body
    assert "Intended file ownership: docs, CI" in body
    edit = next(call for call in runner.calls if call[:2] == ["gh", "api"])
    assert "labels[]=agent:running" in edit
    assert "labels[]=agent:ready" not in edit
    assert "agent:codex-win" not in edit
    created_labels = {call[3] for call in runner.calls if call[:3] == ["gh", "label", "create"]}
    assert created_labels == set(agent_coord.STATUS_LABELS)
    assert all(".env" not in argument for call in runner.calls for argument in call)


def test_claim_fails_before_github_mutation_when_worktree_is_dirty() -> None:
    runner = FakeRunner(dirty=True)

    with pytest.raises(agent_coord.CoordinationError, match="clean worktree"):
        agent_coord.claim_issue(
            _client(runner),
            repo="owner/repo",
            issue=13,
            agent="codex-win",
            provider="OpenAI Codex",
            model="GPT-5",
            areas="docs",
            dependencies="none",
            conflicts="none",
            machine="clean checkout",
            branch=None,
            lease_hours=24,
        )

    assert not any(call[0] == "gh" for call in runner.calls)


def test_heartbeat_reports_dirty_count_without_file_contents() -> None:
    runner = FakeRunner(dirty=True, labels=["agent:running"])
    agent_coord.heartbeat_issue(
        _client(runner),
        repo="owner/repo",
        issue=13,
        agent="codex-win",
        state="working",
        summary="policy complete",
        next_step="run tests",
    )

    body = _comment_body(runner)
    assert "Working tree: dirty (1 paths)" in body
    assert "README.md" not in body
    assert "Next: run tests" in body


def test_handoff_moves_issue_to_review_and_formats_required_fields() -> None:
    runner = FakeRunner(labels=["agent:running"])
    agent_coord.handoff_issue(
        _client(runner),
        repo="owner/repo",
        issue=13,
        agent="codex-win",
        state="review",
        completed="implementation complete",
        tests="pytest passed",
        uncommitted="none",
        next_step="review PR",
        files="none",
        decisions="GitHub is source of truth",
        risks="none",
    )

    body = _comment_body(runner)
    assert "State: ready-for-review" in body
    assert "Tests run: pytest passed" in body
    assert "Uncommitted changes: none" in body
    assert "Exact next step: review PR" in body
    edit = next(call for call in runner.calls if call[:2] == ["gh", "api"])
    assert "labels[]=agent:review" in edit
    assert "labels[]=agent:running" not in edit


def test_status_combines_issue_and_pull_request_queries() -> None:
    runner = FakeRunner()
    output = agent_coord.show_status(_client(runner), "owner/repo")

    assert "OPEN ISSUES\n[]" in output
    assert "OPEN PULL REQUESTS\n[]" in output
    assert any(call[:3] == ["gh", "issue", "list"] for call in runner.calls)
    assert any(call[:3] == ["gh", "pr", "list"] for call in runner.calls)


def test_claim_refuses_to_overwrite_another_agents_active_claim() -> None:
    runner = FakeRunner(labels=["agent:running"])

    with pytest.raises(agent_coord.CoordinationError, match="requires agent:ready"):
        agent_coord.claim_issue(
            _client(runner),
            repo="owner/repo",
            issue=13,
            agent="codex-win",
            provider="OpenAI Codex",
            model="GPT-5",
            areas="docs",
            dependencies="none",
            conflicts="active claim",
            machine="clean checkout",
            branch=None,
            lease_hours=24,
        )

    assert not any(call[:3] == ["gh", "issue", "comment"] for call in runner.calls)


def test_cli_rejects_unbounded_lease_without_running_commands(capsys) -> None:
    runner = FakeRunner()
    result = agent_coord.main(
        [
            "--repo",
            "owner/repo",
            "claim",
            "13",
            "--agent",
            "codex-win",
            "--provider",
            "OpenAI Codex",
            "--model",
            "GPT-5",
            "--areas",
            "docs",
            "--lease-hours",
            "0",
        ],
        client=_client(runner),
    )

    assert result == 2
    assert "between 1 and 168" in capsys.readouterr().err
    assert not runner.calls
