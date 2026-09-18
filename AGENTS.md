# Repository agent policy

GitHub Issues are the source of truth for task scope and status. Pull requests are the
source of truth for review and integration. Local branches, worktrees, and chat are not
durable coordination records.

## Start and coordinate work

- Fetch current `origin/main`; read the full Issue, dependencies, active claims, and
  overlapping PRs before editing.
- Use one Issue, dedicated `task-<issue>-<slug>` branch, and isolated worktree per
  implementation session. Never modify another session's checkout.
- Claim only `agent:ready` work. Open a draft PR early, keep changes within the Issue
  contract, preserve unrelated changes, and post durable progress and handoff comments.
- Do not review, approve, or merge your own work. Agents never merge PRs.

New or migrated agent-managed Issues have exactly one of these status labels:

```text
agent:ready -> agent:running -> agent:review -> agent:done
                     \-> agent:blocked -/
agent:review -> agent:running  (changes requested)
```

Use the repository's deterministic helper for transitions. Remove the prior status when
applying the next one. A blocked status requires a precise blocker and unblocking action.
Only merged or explicitly completed work may become `agent:done`. Reopening a completed
Issue requires an explicit operator choice of its next status.

## Safety and authorization

- Never read, print, modify, or commit `.env`, OAuth tokens, credentials, account
  identifiers, local databases, logs, production data, or callback URLs with secrets.
- Automated tests must be offline, use fakes/mocks, and be incapable of submitting an
  order or notification. Do not connect tests to Schwab, Neon, SMTP, or other live data.
- Never submit, replace, or cancel a live order during development, testing, review, or
  troubleshooting. Keep dry-run and trading-disabled defaults fail-closed.
- Changes to authentication, live-order behavior, risk controls, or mandatory safety
  gates require an explicit user request and independent human review.
- Do not weaken TLS, authentication, confirmation, duplicate protection, auditability,
  rate limits, kill switches, or risk limits. Never automatically retry an ambiguous
  order submission.
- For broker, account, market-data, order, notification, or persistence work, read
  `docs/SAFETY.md` before editing and follow current official API documentation.

## Verification

Run checks proportionate to the change and record exact results. The baseline backend
checks are `ruff check .`, `mypy src`, and `pytest -q`; use checked-in frontend
checks when frontend files change. Inspect staged changes for secrets before every
commit. Do not mark work ready while required checks are failing or unexplained.
