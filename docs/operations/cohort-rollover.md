# Cohort rollover runbook

What happens when a second paper cohort starts collecting while the first is still
running, and what the operator still has to decide by hand.

This document is about **reporting and lifecycle**, not about running sessions. For the
scheduled job see [`cohort-scheduler-setup.md`](cohort-scheduler-setup.md). For controlled
replacement, see [`replacement-cohort-plan.md`](replacement-cohort-plan.md).

## The state this covers

Two cohorts collecting at once is a legitimate, intended state â€” a challenger cohort
(#90/#95) starts while `paper-first-2026-07-28` keeps running, because stopping the
incumbent to start the challenger would throw away the comparison. It is *not* a resting
state: two live experiments mean one of them is still owed a retire-or-keep decision.

Everything below exists so that decision cannot quietly go missing.

## How the dashboard picks a cohort

Without an explicit `?cohort=`, the read-only dashboard defaults to the **newest
unambiguous active cohort**, decided by one field:

> the cohort's persisted immutable **start session** (`Cohort.start_session`, written
> once from the cohort manifest when the cohort is bootstrapped).

Nothing else is consulted. Specifically **not**:

| Not used | Why |
|---|---|
| Cohort id ordering | `paper-first-2026-07-28` looks like a date but is a string. A challenger named `challenger-five-sleeve` carries no date; a cohort named `zzz-pilot` sorts last while having started first. |
| Display names | Operator prose, changeable, not evidence. |
| Insertion / row order | Depends on how the registry happened to be written and on restart order. |
| `created_at` | Records when somebody typed the bootstrap command, not which experiment is newer. |
| Wall clock | The dashboard must render the same answer at any hour. |

The rule, in full:

1. Superseded cohorts are never candidates, however recent their start session.
2. **One** active cohort is the answer with no ordering metadata required. This is the
   state today, and it is why the local SQLite layout â€” which persists no cohort
   manifest at all â€” still resolves correctly.
3. **Several** active cohorts rank newest-first. The winner is shown; every other active
   cohort is listed in the banner as still collecting and still owed a decision.
4. A **tie** on the start session, or **any** active candidate with no persisted start
   session, selects nothing at all. The dashboard shows the reason and offers every
   candidate explicitly.

Point 4 is deliberate. When the records cannot say which experiment is newer, a default
is a coin flip wearing a confident label, and the operator has no way to tell it apart
from a correct answer. Refusing is louder and cheaper than being quietly wrong.

`?cohort=<id>` is always authoritative and overrides all of the above, including for
superseded cohorts â€” that is how the July 27 incident record stays auditable.

### If the dashboard says it cannot choose

The message names the cohorts and the missing field. The fix is to record the exchange
start session in the offending cohort's manifest **when it is bootstrapped** â€” not to
backfill it later to make a screen resolve. A cohort whose start session was never
recorded has a gap in its immutable metadata, and that gap is worth fixing at the source.

Until then, use `?cohort=<id>` to view either cohort. Nothing is blocked; only the guess
is.

## Retiring a cohort is a human decision

**The dashboard never changes a cohort's lifecycle state.** There is no button, no
automatic supersession when a newer cohort appears, and no timeout after which an old
cohort retires itself. Rendering the rollover banner changes nothing at all.

A cohort becomes `superseded` exactly one way: a reviewed pull request adding it to the
registry in `src/schwab_trader/cohort_lifecycle.py`, citing the incident or decision
document that justifies it. That is a code constant rather than a database column
precisely so withdrawing a cohort never writes to the evidence it is withdrawing, and so
the decision appears in a diff with a reviewer's name on it.

Before retiring a cohort, decide and record:

- Whether its evidence is complete enough for the comparison it was started for.
- Whether the replacement or challenger has actually begun collecting.
- Which document holds the reasoning (`docs/incidents/â€¦` or a decision note).
- That its records will be kept unchanged, not deleted or merged.

Then add the `CohortStatus` entry, with `superseded_by` and `reference` populated, and
open the pull request. The banner disappears on its own once only one active cohort
remains â€” because the state it was reporting has actually been resolved, which is the
point.

### What retiring changes

Once registered as superseded, a cohort is refused by `sleeve run --cohort`, skipped by
broad `--all` runs, blocked at `cohort readiness`, excluded from default benchmark
resolution and comparisons, and dropped out of the dashboard's default selection into the
collapsed **Historical Cohorts** section. It stays fully readable by explicit id forever.

## Execution is stricter than reporting

The dashboard defaults; the scheduler does not. With more than one active cohort and no
explicit `--cohort` or `SCHWAB_COHORT_ID`, every mutating, scheduling, and execution
command refuses:

```text
Several active cohorts exist (a, b). Pass --cohort or set SCHWAB_COHORT_ID;
the cohort is never guessed.
```

The asymmetry is intentional. A wrong reporting default costs a confusing screen that the
next click corrects. A wrong execution default writes observations, fills, and paper
positions into the wrong experiment, and that is not correctable â€” the run happened. So
reporting is allowed a well-founded default and execution is not allowed any.

Do not "fix" that refusal by teaching the runner the recency rule. Pass the cohort.

## Checklist: starting a second cohort

1. Bootstrap the new cohort **with a start session recorded in its manifest**. Verify it:
   the dashboard resolving without a banner complaint is the check.
2. Confirm the dashboard defaults to the newer cohort and names the older one in the
   rollover banner.
3. Update the scheduled job's `--cohort` if the job should follow the new cohort, or add
   a second scheduled invocation if both are to be collected. The runner will not guess.
4. Leave the older cohort **active** until you have actually decided to retire it. The
   banner is the reminder; do not silence it by retiring a cohort you have not reviewed.
   The review itself is
   [`cohort-accounting-review.md`](cohort-accounting-review.md), and its recorded
   keep/modify/pause/retire disposition is the evidence for step 5 â€” not a substitute for
   it, since a recorded disposition changes nothing on its own.
5. When you do retire it, follow *Retiring a cohort is a human decision* above.
