"""Static checks for lean startup policy and trusted coordination configuration."""

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_claude_instructions_are_concise_and_route_to_skills() -> None:
    content = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")

    assert content.splitlines()[0] == "@AGENTS.md"
    assert len(content.splitlines()) < 200
    assert "`github-task`" in content
    assert "`github-pr-review`" in content
    assert "@docs/" not in content


def test_platform_skills_route_to_canonical_workflows() -> None:
    for platform in (".agents", ".claude"):
        task = (ROOT / platform / "skills" / "github-task" / "SKILL.md").read_text(encoding="utf-8")
        review = (ROOT / platform / "skills" / "github-pr-review" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert "docs/agent-workflows/github-task.md" in task
        assert "docs/agent-workflows/github-pr-review.md" in review
        assert len(task.splitlines()) < 20
        assert len(review.splitlines()) < 20


def test_agent_issue_template_contains_complete_contract() -> None:
    content = (ROOT / ".github" / "ISSUE_TEMPLATE" / "agent-task.yml").read_text(encoding="utf-8")

    for field in (
        "agent:ready",
        "Acceptance criteria",
        "Allowed scope",
        "Prohibited changes",
        "Dependencies",
        "File ownership or expected areas",
        "Required verification",
        "Risk level",
    ):
        assert field in content


def test_status_workflow_uses_trusted_checkout_and_least_privilege() -> None:
    content = (ROOT / ".github" / "workflows" / "agent-status.yml").read_text(encoding="utf-8")

    assert "contents: read" in content
    assert "issues: write" in content
    assert "pull-requests: read" in content
    assert "pull_request_target" not in content
    assert "ref: ${{ github.event.repository.default_branch }}" in content
    assert '--event-path "$GITHUB_EVENT_PATH"' in content
    assert "${{ github.event.issue" not in content
    assert "${{ github.event.pull_request.body" not in content
