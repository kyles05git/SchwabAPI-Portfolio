"""Deterministic agent-status labels, transitions, validation, and event handling."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

Runner = Callable[[Sequence[str]], str]

AGENT_STATUS_LABELS = {
    "agent:ready": ("0E8A16", "Defined and available for an agent to claim"),
    "agent:running": ("1D76DB", "Actively implemented by a claimed agent"),
    "agent:review": ("FBCA04", "Implementation ready for independent review"),
    "agent:done": ("5319E7", "Merged or otherwise explicitly completed"),
    "agent:blocked": ("B60205", "Blocked pending a documented unblocking action"),
}
LEGACY_STATUS_LABELS = {
    "status:ready": "agent:ready",
    "status:claimed": "agent:running",
    "status:review": "agent:review",
    "status:done": "agent:done",
    "status:blocked": "agent:blocked",
}
STANDARD_LABELS = AGENT_STATUS_LABELS
ALLOWED_TRANSITIONS: Mapping[str | None, frozenset[str]] = {
    None: frozenset({"agent:ready"}),
    "agent:ready": frozenset({"agent:running"}),
    "agent:running": frozenset({"agent:review", "agent:blocked"}),
    "agent:blocked": frozenset({"agent:running", "agent:review"}),
    "agent:review": frozenset({"agent:running", "agent:done"}),
    "agent:done": frozenset({"agent:ready", "agent:running"}),
}
CLOSING_ISSUE_PATTERN = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(?P<number>\d+)"
)


class StatusError(RuntimeError):
    """A safe, user-facing status-management failure."""


def run_command(command: Sequence[str]) -> str:
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
        raise StatusError(f"{operation} failed: {detail}")
    return completed.stdout.strip()


def _gh(runner: Runner, *arguments: str) -> str:
    return runner(["gh", *arguments])


def _json(raw: str, *, context: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StatusError(f"GitHub returned invalid JSON for {context}.") from exc


def label_names(payload: Mapping[str, Any]) -> set[str]:
    labels = payload.get("labels", [])
    if not isinstance(labels, list):
        raise StatusError("GitHub returned an invalid Issue label response.")
    try:
        return {item["name"] for item in labels if isinstance(item, dict)}
    except (KeyError, TypeError) as exc:
        raise StatusError("GitHub returned an invalid Issue label response.") from exc


def issue_labels(runner: Runner, repo: str, issue: int) -> set[str]:
    raw = _gh(runner, "issue", "view", str(issue), "--repo", repo, "--json", "labels")
    payload = _json(raw, context=f"Issue #{issue}")
    if not isinstance(payload, dict):
        raise StatusError(f"GitHub returned an invalid object for Issue #{issue}.")
    return label_names(payload)


def agent_status(labels: set[str], *, context: str) -> str | None:
    statuses = sorted(labels.intersection(AGENT_STATUS_LABELS))
    if len(statuses) > 1:
        raise StatusError(f"{context} has multiple agent statuses: {', '.join(statuses)}.")
    legacy = sorted(labels.intersection(LEGACY_STATUS_LABELS))
    if statuses and legacy:
        raise StatusError(
            f"{context} mixes agent and legacy statuses: {', '.join(statuses + legacy)}."
        )
    return statuses[0] if statuses else None


def sync_labels(runner: Runner, repo: str) -> None:
    """Idempotently create or update the repository-owned labels."""
    for name, (color, description) in STANDARD_LABELS.items():
        _gh(
            runner,
            "label",
            "create",
            name,
            "--repo",
            repo,
            "--color",
            color,
            "--description",
            description,
            "--force",
        )


def transition_issue(
    runner: Runner,
    repo: str,
    issue: int,
    desired: str,
    *,
    completion_evidence: str | None = None,
    reopen_decision: str | None = None,
    blocker_evidence: str | None = None,
    completion_override: bool = False,
) -> bool:
    """Atomically replace an Issue's agent status while preserving other labels."""
    if desired not in AGENT_STATUS_LABELS:
        raise StatusError(f"Unknown agent status '{desired}'.")
    current_labels = issue_labels(runner, repo, issue)
    current = agent_status(current_labels, context=f"Issue #{issue}")
    if current == desired:
        return False
    may_reconcile_completion = (
        completion_override
        and completion_evidence is not None
        and desired == "agent:done"
        and current is not None
    )
    if desired not in ALLOWED_TRANSITIONS[current] and not may_reconcile_completion:
        raise StatusError(f"Disallowed agent-status transition: {current or 'none'} -> {desired}.")
    if desired == "agent:done" and not completion_evidence:
        raise StatusError("agent:done requires merged-PR or explicit completion evidence.")
    if desired == "agent:blocked" and not blocker_evidence:
        raise StatusError("agent:blocked requires a blocker and unblocking action.")
    if current == "agent:done" and not reopen_decision:
        raise StatusError("Leaving agent:done requires an explicit operator reopening decision.")
    new_labels = sorted(
        (current_labels - set(AGENT_STATUS_LABELS) - set(LEGACY_STATUS_LABELS)) | {desired}
    )
    arguments = [
        "api",
        f"repos/{repo}/issues/{issue}",
        "--method",
        "PATCH",
    ]
    for label in new_labels:
        arguments.extend(["-f", f"labels[]={label}"])
    _gh(runner, *arguments)
    return True


def validate_issue_payload(payload: Mapping[str, Any]) -> bool:
    number = payload.get("number", "?")
    return agent_status(label_names(payload), context=f"Issue #{number}") is not None


def validate_repository(runner: Runner, repo: str) -> tuple[int, int]:
    raw = _gh(
        runner,
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "all",
        "--limit",
        "1000",
        "--json",
        "number,labels",
    )
    payload = _json(raw, context="Issue validation")
    if not isinstance(payload, list):
        raise StatusError("GitHub returned an invalid Issue validation response.")
    managed = sum(validate_issue_payload(issue) for issue in payload)
    return len(payload), managed


def closing_issue_numbers(body: str) -> list[int]:
    return list(
        dict.fromkeys(int(match.group("number")) for match in CLOSING_ISSUE_PATTERN.finditer(body))
    )


def _comment(runner: Runner, repo: str, issue: int, body: str) -> str:
    return _gh(runner, "issue", "comment", str(issue), "--repo", repo, "--body", body)


def reconcile_event(
    runner: Runner,
    repo: str,
    payload: Mapping[str, Any],
) -> list[int]:
    """Reconcile trusted GitHub close/reopen events without executing event text."""
    action = payload.get("action")
    issue = payload.get("issue")
    if isinstance(issue, dict):
        number = issue.get("number")
        if not isinstance(number, int):
            raise StatusError("Issue event is missing an integer Issue number.")
        labels = label_names(issue)
        current = agent_status(labels, context=f"Issue #{number}")
        if action == "closed" and current is not None and current != "agent:done":
            transition_issue(
                runner,
                repo,
                number,
                "agent:done",
                completion_evidence="GitHub Issue closed explicitly",
                completion_override=True,
            )
            return [number]
        if action == "reopened" and current == "agent:done":
            _comment(
                runner,
                repo,
                number,
                "AGENT STATUS\nIssue reopened while still agent:done. An operator must "
                "explicitly choose agent:ready or agent:running; automation made no "
                "status choice.",
            )
        return []

    pull_request = payload.get("pull_request")
    if isinstance(pull_request, dict) and action == "closed" and pull_request.get("merged"):
        body = pull_request.get("body") or ""
        if not isinstance(body, str):
            raise StatusError("Pull-request body must be text.")
        changed: list[int] = []
        for number in closing_issue_numbers(body):
            labels = issue_labels(runner, repo, number)
            current = agent_status(labels, context=f"Issue #{number}")
            if current is not None and current != "agent:done":
                transition_issue(
                    runner,
                    repo,
                    number,
                    "agent:done",
                    completion_evidence=f"merged PR #{pull_request.get('number', '?')}",
                    completion_override=True,
                )
                changed.append(number)
        return changed
    return []


def _load_event(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StatusError(f"Could not read GitHub event payload at {path}.") from exc
    if not isinstance(payload, dict):
        raise StatusError("GitHub event payload must be a JSON object.")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="GitHub OWNER/NAME.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("sync-labels", help="Create or synchronize status labels.")

    validate = subparsers.add_parser("validate", help="Detect invalid agent-status labels.")
    target = validate.add_mutually_exclusive_group(required=True)
    target.add_argument("--issue", type=int)
    target.add_argument("--all", action="store_true")

    transition = subparsers.add_parser("transition", help="Apply one allowed status transition.")
    transition.add_argument("issue", type=int)
    transition.add_argument("status", choices=AGENT_STATUS_LABELS)
    transition.add_argument("--completion-evidence")
    transition.add_argument("--reopen-decision")
    transition.add_argument("--blocker")
    transition.add_argument("--unblocks-when")

    event = subparsers.add_parser(
        "reconcile-event", help="Reconcile a trusted GitHub Issue or PR event."
    )
    event.add_argument("--event-path", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None, *, runner: Runner = run_command) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "sync-labels":
            sync_labels(runner, args.repo)
            print("agent labels synchronized")
        elif args.command == "validate":
            if args.issue is not None:
                labels = issue_labels(runner, args.repo, args.issue)
                managed = agent_status(labels, context=f"Issue #{args.issue}") is not None
                print(f"validated Issue #{args.issue}; managed={str(managed).lower()}")
            else:
                total, managed = validate_repository(runner, args.repo)
                print(f"validated {total} issues; managed={managed}; conflicts=0")
        elif args.command == "transition":
            blocker_evidence = None
            if args.status == "agent:blocked":
                labels = issue_labels(runner, args.repo, args.issue)
                current = agent_status(labels, context=f"Issue #{args.issue}")
                if current != "agent:blocked":
                    if "agent:blocked" not in ALLOWED_TRANSITIONS[current]:
                        raise StatusError(
                            f"Disallowed agent-status transition: "
                            f"{current or 'none'} -> agent:blocked."
                        )
                    if not args.blocker or not args.unblocks_when:
                        raise StatusError("agent:blocked requires --blocker and --unblocks-when.")
                    blocker_evidence = (
                        f"AGENT BLOCKED\nBlocker: {args.blocker}\n"
                        f"Unblocks when: {args.unblocks_when}"
                    )
                    _comment(runner, args.repo, args.issue, blocker_evidence)
            changed = transition_issue(
                runner,
                args.repo,
                args.issue,
                args.status,
                completion_evidence=args.completion_evidence,
                reopen_decision=args.reopen_decision,
                blocker_evidence=blocker_evidence,
            )
            print("status changed" if changed else "status already current")
        else:
            changed = reconcile_event(runner, args.repo, _load_event(args.event_path))
            print(f"reconciled event; changed={','.join(map(str, changed)) or 'none'}")
    except StatusError as exc:
        print(f"agent status error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
