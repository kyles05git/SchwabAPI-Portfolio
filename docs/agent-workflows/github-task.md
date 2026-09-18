# GitHub task workflow

Use this workflow for implementation. GitHub Issues are the durable task ledger; the
branch and isolated worktree are execution state. Never use chat as the only record.

## Before claiming

1. Read `AGENTS.md` and any platform instructions. Read the entire Issue, comments,
   linked dependencies, and files or documentation named by the contract.
2. Fetch `origin --prune`. Inspect open Issues, claims, PRs, and changed-file lists for
   dependency or ownership conflicts.
3. Confirm the task is sufficiently specified and has exactly one agent-status label:
   `agent:ready`. Normal work cannot be claimed from any other state.
4. Record the provider/agent identity and model that will do the work. Do not put
   secrets, account identifiers, tokens, local data, or credential paths in GitHub.
5. Create `task-<issue>-<slug>` from current `origin/main` in a new isolated worktree.
   Never repurpose or modify another session's checkout.

## Claim and start

From the clean task worktree, run:

```text
python scripts/agent_coord.py claim <issue> \
  --agent <stable-id> --provider <provider> --model <model> \
  --areas <paths-or-areas> --dependencies <known-dependencies> \
  --conflicts <known-conflicts>
```

The helper replaces `agent:ready` with `agent:running` in one GitHub API update, then
posts a durable claim containing the agent/provider, model, branch, worktree, base SHA,
intended file ownership, and known dependencies or conflicts. If the state changed or
validation fails, stop without editing.

Push an early checkpoint and open a draft PR against `main` with `Closes #<issue>`.
Include the issue contract, base SHA, affected areas, safety impact, and planned checks.

## Implement

- Stay within the Issue's allowed scope and prohibited-change boundaries. Update the
  Issue before materially expanding scope.
- Inspect before replacing. Preserve unrelated work and never reset, clean, overwrite,
  or delete another user's or agent's changes.
- Use current official documentation for external APIs. Never expose secrets or access
  live services unless the Issue and repository safety policy explicitly authorize it.
- Keep commits reviewable and push recoverable checkpoints. Post a concise heartbeat
  before a long pause or after a major milestone.
- For a bug fix or regression, write the failing test first and run it. A regression test
  that has never failed proves nothing. Confirm it fails for the intended reason, then fix.
- Try to falsify your own change before calling it done. The session that wrote the code
  carries its assumptions into its own review, so look for the observation that would
  prove the change wrong: the untaken branch, the empty or partial input, a second
  concurrent writer, and every parallel path the change did not touch.
- Run verification proportionate to risk. Tests must remain offline and incapable of
  submitting real orders. Record exact commands, results, and intentional omissions.

## Finish or block

Move `agent:running` to `agent:review` only after the implementation, documentation,
and required checks are ready for independent review. Push the final commit, update the
draft PR, and post a durable handoff with PR/commit, changed files, decisions, tests,
risks, limitations, uncommitted state, and the exact next step.

If work cannot continue, post a precise blocker explaining what failed, what was tried,
and the specific action or condition that would unblock it; then atomically transition
`agent:running` to `agent:blocked`. The coordination helper's blocked heartbeat or
`agent_status.py transition --blocker ... --unblocks-when ...` records the comment
before applying the status. Resume only through an allowed explicit transition.

Never approve, review, or merge your own PR. Never set `agent:done`; merged/completed
work owns that transition.
