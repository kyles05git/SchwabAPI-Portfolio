# Incident investigation workflow

Use this workflow to find the root cause of a defect or an operational incident. The rule
is absolute: **no fix before a confirmed root cause.** A fix applied to a symptom hides
the cause and makes the next occurrence harder to find.

## Preserve evidence first

Before changing anything, capture the state that explains the failure. Investigation that
destroys its own evidence cannot be reviewed or reproduced.

- Record the exact symptom: the message, the traceback, the failing assertion, the
  observed versus expected behavior, and when it started.
- Record the environment: branch, commit, interpreter, and which checkout and virtual
  environment produced it.
- Do not delete, truncate, reset, clean, rebuild, or re-run anything that would discard
  the failing state until it is captured.
- Redact before recording. Secrets, tokens, account numbers, account hashes, callback
  query strings, and raw provider payloads never enter notes, commits, or GitHub. If a
  value cannot be redacted safely, describe it instead of quoting it.
- Never read protected local state — `.env`, token files, logs, or a live database — to
  advance an investigation. Reproduce with fakes instead.

## Separate symptom from cause

The symptom is what was observed. The cause is what makes the symptom inevitable. Write
both down separately; do not let the first plausible explanation become the conclusion.

1. Trace the code path from the symptom backward to every input that could produce it.
2. Check what changed: `git log` on the affected files. A regression means the cause is
   in a diff you can read.
3. Reproduce deterministically, offline, with a failing test or a minimal script. If the
   failure cannot be reproduced, keep gathering evidence — do not proceed on a hunch.
4. Recurrence matters. If the same area has failed before, the cause is more likely
   structural than local. Say so.

State the result as one specific, falsifiable claim: *this input, through this path,
produces this failure, because of this.*

## Test hypotheses one at a time

- Test exactly one hypothesis per cycle. Changing two things at once means a pass proves
  nothing.
- Try to **falsify** it, not confirm it. Find the observation that would prove it wrong,
  and go look for that observation.
- Confirm with evidence — a failing test, an assertion, a targeted probe — before
  accepting it. "It would explain the symptom" is not confirmation.
- If a hypothesis fails, discard it and return to evidence gathering. Do not patch it into
  a variant that survives.

**No speculative fixes.** Never apply a change to see whether it helps. Never ship a fix
you cannot demonstrate. Never write "this should fix it" — either the evidence shows it
does, or the investigation is not finished.

**Three-strike rule.** After three confirmed-and-falsified hypotheses, stop. Repeated
failure at this point usually means the cause is structural, the reproduction is wrong, or
the problem is outside the assumed boundary. Post a durable handoff instead of a fourth
guess: every hypothesis tried, the evidence that ruled it out, what remains unexplained,
and the specific access, information, or decision that would unblock it.

## Fix and verify

Only after the root cause is confirmed, and only when the invoking GitHub task authorizes
implementation:

1. Write the regression test first. It must fail against the current code for the right
   reason — run it and confirm the failure — and pass after the fix.
2. Fix the cause with the smallest change that eliminates it. Do not refactor adjacent
   code in the same commit.
3. Re-run the original reproduction and the baseline checks. Record exact results.
4. If the fix spans many files, stop and reconsider the boundary before continuing; a
   wide blast radius usually means the cause was identified at the wrong layer.

## Report

Record the symptom, the root cause with `file:line`, the evidence that confirmed it, the
hypotheses ruled out, the fix, the regression test, and the verification output. Name what
remains unverified. If the cause was found but the fix is out of scope, hand off with the
cause documented — that is a complete investigation.
