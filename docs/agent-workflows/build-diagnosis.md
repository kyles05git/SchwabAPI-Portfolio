# Build diagnosis workflow

Use this workflow when a repository check fails: Ruff, mypy, pytest, Alembic parity,
TypeScript, or Vite. The goal is the first causal failure and its minimal correction, not
a green check.

Diagnosis is always allowed. Editing is allowed only when the invoking GitHub task
authorizes implementation and the file is inside that Issue's allowed scope. Without that
authorization, report the diagnosis and stop.

## Diagnose

1. Reproduce the failure yourself with the checked-in command. Do not trust a pasted log;
   the environment matters more than the message.
2. Collect the whole failure set before fixing anything, then find the **first causal**
   failure. Later errors are usually consequences. A missing import produces dozens of
   type errors; fixing the import clears them all.
3. Classify before editing. Name which of these it is:

   | Failure | Where the cause usually is |
   |---|---|
   | Ruff | The flagged line, or a lint rule newly reaching changed code |
   | mypy | A weakened annotation, an unnarrowed optional, or a real type mismatch |
   | pytest collection error | Import path, missing fixture, or a module renamed |
   | pytest assertion | Behavior actually changed; decide which side is right |
   | Alembic parity | Model and migration drifted; neither is automatically correct |
   | TypeScript (`tsc --noEmit`) | Missing annotation, unhandled null, stale contract type |
   | Vite build | Module resolution, an asset path, or an environment assumption |

4. Check whether the worktree is the cause before the code is. A worktree using the root
   virtual environment imports the main checkout, so `pytest` can pass or fail against
   code you did not change. Confirm the interpreter resolves inside the worktree.
5. Separate a broken check from a correct check reporting a real defect. A failing test
   that describes intended behavior is a finding, not an obstacle.

## Correct

Only with implementation authorization:

- Fix the cause, not the report. Re-run and let the consequent failures disappear on
  their own rather than editing each one.
- Keep the diff minimal. No refactor, rename, reformat, or adjacent cleanup. Never run a
  repository-wide formatter to resolve a lint failure.
- **Never suppress a check.** No `# type: ignore`, `# noqa`, `# pragma: no cover`,
  `@ts-ignore`, skip marker, xfail, lowered strictness, narrowed lint selection, or
  excluded path used to make a failure quiet. If suppression genuinely is the right
  answer, justify it in the PR and name what would remove it.
- Do not change a test's expectation to match the code unless the expectation is provably
  wrong. Say why in the commit message.
- Do not add, upgrade, or pin a dependency, and do not touch a lockfile, to resolve a
  build failure. That is a separate decision with its own Issue.
- Alembic parity is repaired by making the migration express the model's intent, with a
  working downgrade. Never resolve drift by editing history.

## Verify and report

Re-run the failing check, then the baseline set proportionate to the change:
`ruff check .`, `mypy src`, `pytest -q`, and the checked-in frontend checks when frontend
files changed. Record exact commands and exact results.

Report the symptom, the first causal failure with `file:line`, why the consequent failures
followed from it, what changed, and what still fails. If the cause is outside the allowed
scope of the invoking Issue, stop and hand off with that boundary named precisely.
