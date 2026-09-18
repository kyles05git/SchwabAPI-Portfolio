# The 30-session accounting review

How to review an official paper cohort once it has completed thirty due sessions, and
how to record what you found so the operational gate can read it.

This is a **research** procedure. Nothing in it promotes a sleeve, changes a strategy,
moves a paper position, or authorizes live trading. See
[`cohort-scheduler-setup.md`](cohort-scheduler-setup.md) for running sessions and
[`cohort-rollover.md`](cohort-rollover.md) for which cohort you are looking at.

## What the review is for

`schwab-trader`'s operational gate asks one question: *is this experiment's evidence
sound enough to be worth acting on at all?* Six of its eight rules read machine records
— session count, completion reliability, duplicate observations, data readiness,
reproducibility, distinct behavior. Two cannot, because they are judgements only a human
can make:

- **Paper accounting.** Did the recorded cash, positions, and valuation for each official
  observation actually reconcile? Where they did not, is the difference understood?
- **Operator decisions.** Having looked at thirty sessions, what is your disposition for
  each sleeve — keep, modify, pause, or retire — and why?

Until those are recorded, both rules fail closed and the cohort reads as *awaiting
evidence*. This document is how you produce them.

The records are append-only. A correction never overwrites what it corrects; it is a new
revision, and both stay in the record forever. That is what makes the review auditable
six months later, when the only thing left is what was written down.

## Before you start

The review comes due when the cohort has **thirty completed due sessions** — the same
number the dashboard's progress bar counts. Check:

```text
schwab-trader cohort review pending --cohort <cohort-id>
```

Accounting checks and notes may be recorded at any time, including mid-cohort. A **final
operator decision is refused** until the review is due, and that refusal is deliberate:
the gate reads a recorded decision as "a human judged thirty sessions of evidence", so
accepting one on day three would let a cohort satisfy the rule without the evidence ever
existing.

If the command reports missing review tables, the shared database has not had the
Alembic migration applied. Apply it before recording anything; the CLI will not create
tables implicitly.

## The procedure

### 1. See what is owed

```text
schwab-trader cohort review pending --cohort <cohort-id>
```

Three kinds of outstanding work are reported separately:

| Reported | Meaning |
|---|---|
| Unreviewed observations | An official observation whose cash, positions, or valuation has no recorded check. All three areas are required before the observation counts as reviewed. |
| Unexplained differences | A difference you recorded and have not yet explained. These fail the gate. |
| Sleeves without a decision | A cohort member with no current keep/modify/pause/retire record. |

Exit code `1` means the review is due and work remains; `0` means nothing is outstanding
*or* the review is not due yet. `--json` gives the same content as a stable contract.

### 2. Check each observation

For every official observation, reconcile three areas against the recorded snapshots:

- **cash** — settled and unsettled cash against the modeled balance, including anything
  the T+1 settlement model should still be holding.
- **positions** — the recorded position set and quantities against the simulated fills.
- **valuation** — recorded total equity against the equity recomputed from positions and
  cash at the recorded quote snapshot.

Record a clean area:

```text
schwab-trader cohort review record --cohort <cohort-id> \
  --sleeve <name-or-id> --session 2026-09-04 --area cash
```

The command is idempotent by content: running it again with the same finding reports
`unchanged` and writes nothing, so a partially finished pass can simply be re-run.

### 3. Investigate a difference

When an area does not reconcile, record it as a difference *with what differed*, before
you know why:

```text
schwab-trader cohort review record --cohort <cohort-id> \
  --sleeve trend-large --session 2026-09-04 --area cash \
  --finding difference \
  --summary "Settled cash trailed the modeled balance by 0.04."
```

Recording it unexplained is the honest state, and the gate keeps failing on it. That is
the point — an unexplained difference is exactly what the review exists to surface.

To investigate, work outward from the record rather than from memory:

1. The sleeve's recorded positions, cash, and simulated orders for that session —
   `schwab-trader sleeve positions` / `sleeve report`, or the dashboard's sleeve-detail
   drawer, which shows the same record with its lineage.
2. The observation's `snapshot_ids` — the cohort snapshot and quote snapshot the run was
   bound to. A valuation difference is very often the quote snapshot rounding, and the
   snapshot is the authority, not a later quote.
3. The run record for the session: a `partial` run, a member error, or a resolved
   `awaiting_data` wait usually explains a cash or position gap.
4. The settlement model. Under T+1 a fill's cash moves on the *next* session; the next
   session's record either shows it settling or it does not.

Keep anything worth remembering as a note:

```text
schwab-trader cohort review note --cohort <cohort-id> \
  --sleeve trend-large --session 2026-09-04 \
  --note "Traced to the partial fill at 15:58; next session shows it settled."
```

Notes are keyed on their text, so re-running an identical note is a no-op. **Never put a
credential, token, account number, or connection string in a note** — notes are rendered
in the dashboard and served in the JSON contract.

Once you understand it, supersede the entry with the explanation:

```text
schwab-trader cohort review record --cohort <cohort-id> \
  --sleeve trend-large --session 2026-09-04 --area cash \
  --finding difference \
  --summary "Settled cash trailed the modeled balance by 0.04." \
  --explanation "T+1 settlement released it on the next session, as that session's record shows." \
  --supersede
```

`--supersede` is required for any change to an existing entry. Without it the command
refuses and writes nothing, so a typo cannot quietly replace a finding. The earlier entry
is preserved and shown in the dashboard's *Superseded records*.

An explanation must be something a reader can check. "Looks fine" explains nothing;
"the snapshot agrees to the cent, the difference is display rounding" can be verified.

### 4. Record a decision per sleeve

Once the accounting review is complete and the review is due:

```text
schwab-trader cohort review decide --cohort <cohort-id> \
  --sleeve trend-large --action keep \
  --rationale "Complete evidence over 30 sessions, reproducible definition, no unexplained accounting state."
```

A rationale is required and may not be blank — at the database level, not just in the
CLI. A decision with no reason is precisely what this review exists to prevent.

#### Which action

All four are **research dispositions about a paper experiment**. None of them changes
anything by itself: `pause` does not stop the sleeve running, `retire` does not remove it
from the cohort, `modify` does not change a parameter, and `keep` does not promote
anything. Acting on a disposition is a separate, explicit operator step, and cohort
membership and configuration are immutable for the life of the experiment either way.

| Action | Use when |
|---|---|
| `keep` | The sleeve produced usable, reconciled evidence and is worth carrying forward as-is into the next cohort or a longer run. |
| `modify` | The sleeve is worth continuing, but something about how it was specified needs to change first — cadence, cost assumptions, universe. Record what, in the rationale. The change happens in a *new* cohort; this one is not edited. |
| `pause` | Evidence is inconclusive or the sleeve depends on something currently unreliable (a data source, a provider gap). Not a verdict on the strategy — a verdict on whether more of the same run would tell you anything. |
| `retire` | The sleeve did not earn further attention: it behaved indistinguishably from the control, or its operational cost outweighs what it teaches. |

A decision may be superseded the same way as a check, with `--supersede`. Both revisions
stay in the record, and the gate counts only the current one.

### 5. Read the result

```text
schwab-trader cohort review show --cohort <cohort-id>
schwab-trader cohort review show --cohort <cohort-id> --json   # the complete record
```

The Paper Cohort dashboard shows the same records read-only, directly under the
30-session review panel: coverage, every difference and its explanation, notes, current
decisions, and superseded records. **The dashboard never writes a review record** — the
CLI is the only write path — and the JSON contract carries the full history including
every superseded revision.

## What passing does and does not mean

With a complete review and at least one reasoned decision, the *Paper accounting* and
*Operator decisions* rules can pass on real evidence. If the other six rules also pass,
the gate reports the cohort operationally useful.

That means the experiment was run competently and its records can be trusted. It does
**not** mean the strategy works. `investment_alpha_assessed` and
`live_trading_authorized` are hard `False` and cannot be changed by anything recorded
here. Thirty sessions is not evidence of alpha, expected return, or statistical
significance, and no combination of review records makes it so.

## End-of-cohort checklist

- [ ] `cohort review pending` reports zero unreviewed observations.
- [ ] Every recorded difference has an explanation a second person could verify.
- [ ] Every member sleeve has a current decision with a substantive rationale.
- [ ] Anything you had to work out is written down as a note, not left in chat.
- [ ] No note or rationale contains a credential, token, account number, or connection
      string.
- [ ] `cohort review show --json` has been read once, end to end, including the
      superseded entries.
- [ ] The gate's remaining rules have been read on their own terms; a passing accounting
      review does not excuse a failing readiness or reproducibility rule.
- [ ] The next cohort's changes, if any, are captured as a plan — not applied to this
      one. A running experiment is never edited.

## Where the records live

With a shared database configured, review records live beside the cohort runs they
describe, in `cohort_accounting_checks`, `cohort_review_notes`, and
`cohort_operator_decisions` (Alembic revision `20260801_0006`). Without one, they live in
a local SQLite file at `SCHWAB_COHORT_REVIEW_DB_PATH`.

They reference cohorts, sleeves, and observations by **stable identity**, never by
display name, so two sleeves sharing a name in different cohorts can never be confused.
The tables deliberately carry no foreign key to `cohorts` or `sleeves`: in the local
layout the sleeve registry is not in that database at all, so the constraint could only
exist on one of the two supported backends. Every reference is instead validated against
the authoritative records before any row is written, and an invalid one is refused with
nothing written.

Downgrading past `20260801_0006` drops these three tables, and every review record lives
entirely inside them. Export with `cohort review show --json` before downgrading a
database whose review has begun.
