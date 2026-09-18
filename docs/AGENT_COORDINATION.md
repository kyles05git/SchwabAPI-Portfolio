# Agent coordination

GitHub is the durable coordination plane for Codex, Claude Code, and human contributors.
Issues hold task contracts and status; PRs hold review and integration evidence; branches
and isolated worktrees hold execution state. Conversation history is never authoritative.

Detailed implementation and review procedures load on demand:

- `github-task`: `docs/agent-workflows/github-task.md`
- `github-pr-review`: `docs/agent-workflows/github-pr-review.md`

## Issue contract

An agent-ready Issue states:

- goal;
- acceptance criteria;
- allowed scope;
- prohibited changes;
- dependencies and machine requirements;
- file ownership or expected areas;
- required verification; and
- risk level.

Use a stable agent ID in comments, such as `codex-win` or `claude-clean`. The claim
comment also records provider, model, branch, worktree, base SHA, intended ownership,
dependencies, and conflicts. Identity is comment metadata; `agent:*` status labels do
not identify owners.

Never put secrets, OAuth material, account identifiers, logs, local databases, or
production data in an Issue or PR. Use `machine:auth-required` only to describe a
capability dependency, never to transfer protected data.

## Agent statuses

Each new or migrated agent-managed Issue has at most one of:

- `agent:ready` — specified and available to claim;
- `agent:running` — claimed implementation is active;
- `agent:review` — implementation is ready for independent review;
- `agent:done` — associated PR merged or task explicitly completed;
- `agent:blocked` — progress stopped with a documented unblocking action.

```text
agent:ready -> agent:running -> agent:review -> agent:done
                     \-> agent:blocked -/
agent:review -> agent:running
```

Allowed transitions are:

- no status to `agent:ready` when an operator creates or explicitly adopts a task;
- `agent:ready` to `agent:running`;
- `agent:running` to `agent:review` or `agent:blocked`;
- `agent:blocked` to `agent:running` or `agent:review`;
- `agent:review` to `agent:running` or `agent:done`; and
- `agent:done` to `agent:ready` or `agent:running` only with an explicit operator
  reopening decision.

A transition replaces the prior status in one GitHub API update while preserving
non-status labels. Multiple new agent-status labels are an error. `agent:blocked`
requires a comment describing the blocker and what would unblock it. An implementation
agent and reviewer never set `agent:done`; completion automation or an operator does.

Historical `status:*` and agent-owner labels remain legacy metadata until an operator
schedules migration after checking active tasks. Do not mix a legacy status with a new
agent status on one Issue.

## Deterministic tooling

`scripts/agent_status.py` owns label definitions, validation, transitions, and trusted
GitHub-event reconciliation:

```text
python scripts/agent_status.py --repo OWNER/REPO sync-labels
python scripts/agent_status.py --repo OWNER/REPO validate --all
python scripts/agent_status.py --repo OWNER/REPO transition 123 agent:running
```

`sync-labels` is idempotent and updates the five labels to their repository-owned
colors and descriptions. It deliberately does not rewrite legacy labels or active-task
assignments; any historical migration requires a separate operator decision after an
impact review. Validation reports conflicts and never repairs them silently.

`scripts/agent_coord.py` posts claims, heartbeats, and handoffs without reading content
from local changed files:

```text
python scripts/agent_coord.py --repo OWNER/REPO claim 123 \
  --agent codex-win --provider "OpenAI Codex" --model "GPT-5" \
  --areas "docs, coordination scripts"
python scripts/agent_coord.py --repo OWNER/REPO heartbeat 123 \
  --agent codex-win --summary "status helper complete" --next-step "run tests"
python scripts/agent_coord.py --repo OWNER/REPO handoff 123 \
  --agent codex-win --state review --completed "implementation complete" \
  --tests "ruff, mypy, pytest" --next-step "independent PR review"
```

The `agent-status.yml` workflow uses only trusted default-branch code and a fixed event
file path. It has read-only contents/PR access and Issue-write access. It synchronizes
labels on default-branch updates or manual dispatch, detects invalid status combinations,
sets `agent:done` after managed work closes or its linked `Closes #N` PR merges, and
comments without choosing a status when a completed Issue is reopened. Completion
reconciliation can repair an out-of-sequence managed status after a human merge or
explicit close; normal agent transitions cannot skip review. Issue or PR text is parsed
as data and never executed.

## Branches, review, and integration

One implementation task uses one `task-<issue>-<slug>` branch and isolated worktree
created from current `origin/main`. Do not force-push reviewed/shared branches or
modify another session's checkout. Open a draft PR early with `Closes #<issue>`.

Review runs in a fresh independent session and checkout. Findings are durable PR/Issue
comments. Agents do not self-review, self-approve, self-merge, or merge another PR.
Only the user or explicitly designated human/integration owner merges after required
checks and review. GitHub remains authoritative if a roadmap or transcript disagrees.
