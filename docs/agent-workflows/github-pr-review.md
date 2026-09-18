# GitHub pull-request review workflow

Use this workflow only in a fresh, independent session and checkout. The implementation
session cannot review its own work, and reviewers never merge the PR.

## Establish the contract

1. Fetch current remote refs and create a clean review checkout that does not reuse the
   implementation worktree.
2. Read `AGENTS.md`, the full Issue and comments, linked dependencies, PR description,
   commits, complete diff, changed files, test evidence, and current CI results.
3. Confirm the linked Issue has exactly one agent-status label, `agent:review`. If it
   does not, report the state mismatch and stop the review transition.
4. Confirm the PR author and reviewer are independent. If the GitHub identity cannot
   submit an approval independently, leave a normal review comment instead.

## Review

Evaluate the implementation against the Issue's acceptance criteria, allowed scope,
and prohibited changes. Check correctness and regressions, security and secret handling,
persistence and concurrency behavior, migration and rollback safety, test adequacy,
workflow permissions, deterministic automation, and any repository-specific safety gate.
Run focused offline verification needed to test the changed behavior; inspect actual CI
instead of relying only on the PR description.

Retrieve context progressively instead of reading the repository up front. Start from the
diff, name what you still cannot decide from it, and read only what answers that question;
repeat for a few bounded rounds and stop when a round adds nothing. If the context needed
to judge a change is still missing, record a verification gap rather than guessing from
partial reading.

Report findings first, ordered by severity. Every finding must include severity,
file/line, evidence, impact, and a recommended correction. Separate blocking defects
from non-blocking suggestions and state any verification gaps. If there are no findings,
say so explicitly and summarize the checks performed and residual risk.

Remain read-only unless the task explicitly authorizes reviewer fixes. Authorization to
review is not authorization to push commits, change the Issue contract, or modify live
or external state.

## Record the outcome

- Changes required: post the findings durably on the PR and Issue, then atomically move
  `agent:review` back to `agent:running` for the implementer with
  `python scripts/agent_status.py --repo OWNER/REPO transition ISSUE agent:running`.
- Pass: post the evidence-backed review and leave the Issue in `agent:review` awaiting
  human merge.
- Blocked review: post the precise blocker and what would unblock review. Do not invent
  a new status transition.

Never merge the PR. Never set `agent:done`; it is reserved for a merged PR or an
otherwise explicitly completed task.
