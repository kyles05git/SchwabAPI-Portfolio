"""Durable GitHub coordination for agents working across computers and products.

This helper intentionally reads only Git metadata and GitHub Issue/PR metadata. It
never opens project configuration, logs, tokens, or trading databases.
"""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType


def _load_agent_status() -> ModuleType:
    """Load the sibling module in direct-script and importlib test contexts."""
    path = Path(__file__).with_name("agent_status.py")
    spec = importlib.util.spec_from_file_location("agent_status", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load agent status helper at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


agent_status = _load_agent_status()

Runner = Callable[[Sequence[str]], str]

STATUS_LABELS = agent_status.AGENT_STATUS_LABELS
STANDARD_LABELS = agent_status.STANDARD_LABELS
STATE_TO_LABEL = {
    "working": "agent:running",
    "blocked": "agent:blocked",
    "review": "agent:review",
}


class CoordinationError(RuntimeError):
    """A safe, user-facing coordination failure."""


def run_command(command: Sequence[str]) -> str:
    """Run one argument-vector command without a shell and return stdout."""
    completed = subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode:
        operation = " ".join(command[:2])
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise CoordinationError(f"{operation} failed: {detail}")
    return completed.stdout.strip()


@dataclass(frozen=True)
class CommandClient:
    """Typed façade over the only two executables this helper invokes."""

    runner: Runner = run_command

    def git(self, *arguments: str) -> str:
        return self.runner(["git", *arguments])

    def gh(self, *arguments: str) -> str:
        return self.runner(["gh", *arguments])


def resolve_repo(client: CommandClient, explicit: str | None) -> str:
    if explicit:
        return explicit
    repo = client.gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
    if not repo:
        raise CoordinationError("Could not resolve the GitHub repository; pass --repo OWNER/NAME.")
    return repo


def git_snapshot(client: CommandClient) -> tuple[str, str, str]:
    """Return branch, HEAD, and a content-free clean/dirty summary."""
    branch = client.git("branch", "--show-current")
    head = client.git("rev-parse", "HEAD")
    porcelain = client.git("status", "--porcelain", "--untracked-files=normal")
    changed = len([line for line in porcelain.splitlines() if line.strip()])
    working_tree = "clean" if not changed else f"dirty ({changed} paths)"
    return branch, head, working_tree


def ensure_labels(client: CommandClient, repo: str, agent: str) -> None:
    del agent  # Identity belongs in durable comments, not the status-label namespace.
    try:
        agent_status.sync_labels(client.runner, repo)
    except agent_status.StatusError as exc:
        raise CoordinationError(str(exc)) from exc


def issue_labels(client: CommandClient, repo: str, issue: int) -> set[str]:
    try:
        return agent_status.issue_labels(client.runner, repo, issue)
    except agent_status.StatusError as exc:
        raise CoordinationError(str(exc)) from exc


def set_issue_state(
    client: CommandClient,
    repo: str,
    issue: int,
    state: str,
    *,
    agent: str,
    blocker_evidence: str | None = None,
) -> None:
    desired = STATE_TO_LABEL[state]
    del agent
    try:
        agent_status.transition_issue(
            client.runner,
            repo,
            issue,
            desired,
            blocker_evidence=blocker_evidence,
        )
    except agent_status.StatusError as exc:
        raise CoordinationError(str(exc)) from exc


def claim_issue(
    client: CommandClient,
    *,
    repo: str,
    issue: int,
    agent: str,
    provider: str,
    model: str,
    areas: str,
    dependencies: str,
    conflicts: str,
    machine: str,
    branch: str | None,
    lease_hours: int,
    now: datetime | None = None,
) -> str:
    current_branch, _, working_tree = git_snapshot(client)
    if working_tree != "clean":
        raise CoordinationError("Claim from a clean worktree before editing any files.")
    selected_branch = branch or current_branch
    if not selected_branch or selected_branch in {"main", "master"}:
        raise CoordinationError("Claims require a dedicated task branch, never main/master.")
    if branch is not None and branch != current_branch:
        raise CoordinationError(
            f"Requested branch '{branch}' does not match checkout '{current_branch}'."
        )
    base = client.git("rev-parse", "origin/main")
    worktree = client.git("rev-parse", "--show-toplevel")
    checked_at = now or datetime.now(UTC)
    lease_until = checked_at + timedelta(hours=lease_hours)
    body = "\n".join(
        [
            "CLAIM",
            f"Agent/provider: {agent} / {provider}",
            f"Model: {model}",
            f"Branch: {selected_branch}",
            f"Worktree: {worktree}",
            f"Base: {base}",
            f"Lease until: {lease_until:%Y-%m-%d %H:%M UTC}",
            f"Intended file ownership: {areas}",
            f"Known dependencies: {dependencies}",
            f"Known conflicts: {conflicts}",
            f"Machine: {machine}",
        ]
    )
    current = issue_labels(client, repo, issue)
    try:
        current_status = agent_status.agent_status(current, context=f"Issue #{issue}")
    except agent_status.StatusError as exc:
        raise CoordinationError(str(exc)) from exc
    if current_status != "agent:ready":
        raise CoordinationError(
            f"Issue #{issue} requires agent:ready to claim; found "
            f"{current_status or 'no agent status'}."
        )
    ensure_labels(client, repo, agent)
    set_issue_state(client, repo, issue, "working", agent=agent)
    return client.gh("issue", "comment", str(issue), "--repo", repo, "--body", body)


def heartbeat_issue(
    client: CommandClient,
    *,
    repo: str,
    issue: int,
    agent: str,
    state: str,
    summary: str,
    next_step: str,
) -> str:
    branch, head, working_tree = git_snapshot(client)
    body = "\n".join(
        [
            "HEARTBEAT",
            f"Agent: {agent}",
            f"State: {state}",
            f"Branch / HEAD: {branch} / {head}",
            f"Working tree: {working_tree}",
            f"Completed: {summary}",
            f"Next: {next_step}",
        ]
    )
    ensure_labels(client, repo, agent)
    if state == "blocked":
        url = client.gh("issue", "comment", str(issue), "--repo", repo, "--body", body)
        set_issue_state(client, repo, issue, state, agent=agent, blocker_evidence=body)
        return url
    set_issue_state(client, repo, issue, state, agent=agent)
    return client.gh("issue", "comment", str(issue), "--repo", repo, "--body", body)


def handoff_issue(
    client: CommandClient,
    *,
    repo: str,
    issue: int,
    agent: str,
    state: str,
    completed: str,
    tests: str,
    uncommitted: str,
    next_step: str,
    files: str,
    decisions: str,
    risks: str,
) -> str:
    branch, head, working_tree = git_snapshot(client)
    public_state = "ready-for-review" if state == "review" else state
    body = "\n".join(
        [
            "HANDOFF",
            f"Agent: {agent}",
            f"Branch / HEAD: {branch} / {head}",
            f"State: {public_state}",
            f"Completed: {completed}",
            f"Tests run: {tests}",
            f"Working tree: {working_tree}",
            f"Uncommitted changes: {uncommitted}",
            f"Files currently owned: {files}",
            f"Important decisions: {decisions}",
            f"Known risks: {risks}",
            f"Exact next step: {next_step}",
        ]
    )
    ensure_labels(client, repo, agent)
    if state == "blocked":
        url = client.gh("issue", "comment", str(issue), "--repo", repo, "--body", body)
        set_issue_state(client, repo, issue, state, agent=agent, blocker_evidence=body)
        return url
    set_issue_state(client, repo, issue, state, agent=agent)
    return client.gh("issue", "comment", str(issue), "--repo", repo, "--body", body)


def show_status(client: CommandClient, repo: str) -> str:
    issues = client.gh(
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--limit",
        "100",
        "--json",
        "number,title,labels,updatedAt,url",
    )
    pull_requests = client.gh(
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--limit",
        "100",
        "--json",
        "number,title,headRefName,isDraft,mergeStateStatus,url",
    )
    return f"OPEN ISSUES\n{issues or '[]'}\n\nOPEN PULL REQUESTS\n{pull_requests or '[]'}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="GitHub OWNER/NAME; defaults to the current repository.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="Show open Issues and pull requests as JSON.")

    claim = subparsers.add_parser("claim", help="Claim an Issue from a clean task branch.")
    claim.add_argument("issue", type=int)
    claim.add_argument("--agent", required=True)
    claim.add_argument("--provider", required=True)
    claim.add_argument("--model", required=True)
    claim.add_argument("--areas", required=True)
    claim.add_argument("--dependencies", default="none known")
    claim.add_argument("--conflicts", default="none known")
    claim.add_argument("--machine", default="clean checkout; no credentials required")
    claim.add_argument("--branch")
    claim.add_argument("--lease-hours", type=int, default=24)

    heartbeat = subparsers.add_parser("heartbeat", help="Post a milestone heartbeat.")
    heartbeat.add_argument("issue", type=int)
    heartbeat.add_argument("--agent", required=True)
    heartbeat.add_argument("--state", choices=STATE_TO_LABEL, default="working")
    heartbeat.add_argument("--summary", required=True)
    heartbeat.add_argument("--next-step", required=True)

    handoff = subparsers.add_parser("handoff", help="Post a durable pause/review handoff.")
    handoff.add_argument("issue", type=int)
    handoff.add_argument("--agent", required=True)
    handoff.add_argument("--state", choices=STATE_TO_LABEL, required=True)
    handoff.add_argument("--completed", required=True)
    handoff.add_argument("--tests", required=True)
    handoff.add_argument("--uncommitted", default="none")
    handoff.add_argument("--next-step", required=True)
    handoff.add_argument("--files", default="none")
    handoff.add_argument("--decisions", default="none")
    handoff.add_argument("--risks", default="none")
    return parser


def main(argv: Sequence[str] | None = None, *, client: CommandClient | None = None) -> int:
    args = build_parser().parse_args(argv)
    active_client = client or CommandClient()
    try:
        repo = resolve_repo(active_client, args.repo)
        if args.command == "status":
            print(show_status(active_client, repo))
        elif args.command == "claim":
            if args.lease_hours < 1 or args.lease_hours > 168:
                raise CoordinationError("--lease-hours must be between 1 and 168.")
            print(
                claim_issue(
                    active_client,
                    repo=repo,
                    issue=args.issue,
                    agent=args.agent,
                    provider=args.provider,
                    model=args.model,
                    areas=args.areas,
                    dependencies=args.dependencies,
                    conflicts=args.conflicts,
                    machine=args.machine,
                    branch=args.branch,
                    lease_hours=args.lease_hours,
                )
            )
        elif args.command == "heartbeat":
            print(
                heartbeat_issue(
                    active_client,
                    repo=repo,
                    issue=args.issue,
                    agent=args.agent,
                    state=args.state,
                    summary=args.summary,
                    next_step=args.next_step,
                )
            )
        else:
            print(
                handoff_issue(
                    active_client,
                    repo=repo,
                    issue=args.issue,
                    agent=args.agent,
                    state=args.state,
                    completed=args.completed,
                    tests=args.tests,
                    uncommitted=args.uncommitted,
                    next_step=args.next_step,
                    files=args.files,
                    decisions=args.decisions,
                    risks=args.risks,
                )
            )
    except CoordinationError as exc:
        print(f"coordination error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
