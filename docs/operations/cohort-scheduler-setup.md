# Cohort scheduler runbook (single machine)

Operator procedure for running the durable daily paper-cohort job automatically and
knowing, without remembering, whether it ran.

Installing the scheduled task is **documentation, not an enabled task**. Nothing in the
application registers, enables, disables, or removes an operating-system scheduled task;
the `Register-ScheduledTask` step below is an explicit operator action after review.

## Model

- Exactly **one** machine holds the runtime **writer** role and runs this task. Set
  `SCHWAB_COHORT_WRITER=true` in that machine's local `.env` and leave it unset
  everywhere else.
- Every other machine is a **read-only dashboard client** (`schwab-trader serve
  --no-live`) and must NOT run the scheduler.
- Correctness relies on the shared PostgreSQL database's durable `(cohort_id,
  scheduled_for)` identity and the official-session advisory lock, so repeated
  invocation is safe and cannot double-write observations or fills.
- Alerts deduplicate on a durable `(cohort, session, kind)` transition record, so
  repeated invocations and process restarts cannot resend a notification.

Keep `SCHWAB_DATABASE_URL`, SMTP credentials, and every other secret only in the local
`.env`. Never place one in a task definition, a command line, or a commit.

## Before you schedule anything

```powershell
.\.venv\Scripts\python.exe -m schwab_trader cohort readiness
```

This is read-only and mutates nothing. It checks the one-writer rule, runtime
configuration, cohort existence and reproducibility, durable-store compatibility, the
alert record, and notification readiness. Every check fails closed: an unset
`SCHWAB_COHORT_WRITER` is a refusal, not an assumption that this is the only machine.

Exit code `0` means it is safe to schedule; `1` means at least one check blocked, and
each blocking check prints its remedy. A `warn` (for example, no SMTP configured) does
not block â€” scheduling still records correct evidence, you just will not be told about
a missed session.

Add `--json` for the machine-readable `cohort-readiness/1` contract, or `--show-setup`
to be pointed back at this document.

## Which cohort

`paper-first-2026-07-28` is the active cohort. `paper-first-2026-07-27` is **superseded**
â€” it failed its shakedown on session 1, was replaced rather than repaired, and its
records are kept unchanged as incident evidence.

That status lives in `src/schwab_trader/cohort_lifecycle.py`, a reviewed code constant
rather than a database column, precisely so that withdrawing a cohort never writes to
the evidence. It is enforced, not advisory:

- `sleeve run --cohort paper-first-2026-07-27` is refused outright.
- A broad `--all` run skips superseded cohorts and says so, then runs the rest.
- Naming one of its sleeves in an ordinary run (`sleeve run <name>`) is refused too, and
  `sleeve run --match` / `sleeve watch --match` skip them. An unofficial cycle still
  writes a paper account and an evaluation row, so "never re-run" covers those paths.
- `cohort readiness` reports `cohort-identity` as a blocking `fail` for one.
- The dashboard never defaults to one; it appears under **Historical Cohorts** and
  renders in full only when explicitly selected (`?cohort=<id>`).

Retiring a future cohort is a pull request against that registry, reviewed alongside the
incident document it cites. Nothing retires a cohort automatically â€” not a newer cohort
appearing, not the dashboard defaulting away from it, not elapsed time.

### When a second cohort is collecting

Two active cohorts at once is expected during a rollover, and the dashboard defaults to
the **newest** of them by persisted start session (never by id ordering), showing a banner
that names the others as still owed a retire-or-keep decision. Execution stays stricter:
with more than one active cohort, `--cohort` or `SCHWAB_COHORT_ID` is required and the
runner refuses rather than guessing.

See [`cohort-rollover.md`](cohort-rollover.md) for the full selection rule, what happens
when the records cannot say which cohort is newest, and the checklist for starting a
second cohort. Once a cohort reaches thirty completed due sessions, the review it is then
owed is [`cohort-accounting-review.md`](cohort-accounting-review.md).

### The shared benchmark name

Every cohort keeps its own `bench-spy` control, so the name matches more than one sleeve
once a second cohort exists. `sleeve compare`, `digest`, and the dashboard all resolve it
through `src/schwab_trader/benchmark_scope.py`, which narrows to the cohorts still
collecting â€” that is why `--compare` below works rather than failing as ambiguous.

Both commands take `--cohort` to measure inside a specific cohort, including a superseded
one:

```powershell
.\.venv\Scripts\python.exe -m schwab_trader sleeve compare --benchmark bench-spy `
  --cohort paper-first-2026-07-27
```

If a *second active* cohort ever exists, the bare name becomes genuinely ambiguous and
both commands say so instead of guessing; pass `--cohort` then. The dashboard resolves its
own default by recency instead (see [`cohort-rollover.md`](cohort-rollover.md)); these
reporting commands do not, because a comparison silently scoped to the wrong cohort reads
exactly like a correct one.

## The command

```powershell
.\.venv\Scripts\python.exe scripts\run_sleeves.py --cohort paper-first-2026-07-28 --compare
```

- **Do not** pass `--scheduled-for`. The runner resolves the eligible XNYS session from
  the exchange calendar, which correctly handles regular 4:00 PM ET closes, 1:00 PM ET
  early closes, weekends, and holidays.
- Before the session closes the run is a no-op / `pending`; after close it produces the
  official run exactly once.
- After the run's outcome is durably recorded, a sanitized alert is sent for the
  transition (completed, late, partial, failed, or missed). A delivery failure is
  printed and recorded but **never** changes, retries, or corrupts the run outcome.

## Trigger

Fire on **weekdays** with a repetition window that covers both close times, e.g. start
at **1:05 PM ET** and repeat every **30 minutes until ~6:00 PM ET**. Early-close days are
handled because the calendar-aware runner completes on the first invocation after the
actual close and no-ops on the rest.

## Installation (requires operator approval â€” do not run unattended)

Run from the writer machine, in the project directory, with the venv present. Adjust the
path. This registers but you may leave it **disabled** until reviewed.

```powershell
$py  = "$PWD\.venv\Scripts\python.exe"
$arg = "$PWD\scripts\run_sleeves.py --cohort paper-first-2026-07-28 --compare"
$action  = New-ScheduledTaskAction -Execute $py -Argument $arg -WorkingDirectory "$PWD"
$trigger = New-ScheduledTaskTrigger -Daily -At 1:05PM
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At 1:05PM `
    -RepetitionInterval (New-TimeSpan -Minutes 30) `
    -RepetitionDuration (New-TimeSpan -Hours 5)).Repetition
# Weekdays only:
$trigger.DaysOfWeek = 'Monday','Tuesday','Wednesday','Thursday','Friday'
Register-ScheduledTask -TaskName "SchwabCohortRun" -Action $action -Trigger $trigger `
    -Description "Durable paper cohort run (writer machine only)" -RunLevel Limited
# Optionally keep it disabled until reviewed:
# Disable-ScheduledTask -TaskName "SchwabCohortRun"
```

The machine clock/timezone must be Eastern, or convert the trigger time accordingly.
To stop: `Unregister-ScheduledTask -TaskName "SchwabCohortRun"`.

## Verifying a daily run

```powershell
.\.venv\Scripts\python.exe -m schwab_trader cohort health
```

One command answers which XNYS session is relevant, whether its run is merely pending or
actually missing, how many members recorded evidence, and the single next step. It is
read-only: it never runs a cohort or mutates anything.

| Exit | Meaning |
|---|---|
| `0` | On schedule. Nothing is owed. |
| `1` | Evidence is late, partial, failed, or missing. Act on the printed next step. |
| `2` | The question could not be answered (no cohort, no roster). Fails closed. |

Options:

- `--json` emits the stable `cohort-health/1` contract for scripting.
- `--now 2026-07-27T16:30` evaluates a specific naive Eastern instant, so you can
  rehearse a verdict without waiting for the clock.
- `--notify` also sends an alert for a terminal problem state, at most once ever.

`cohort alerts` lists what you were told and when.

## States and how to respond

| State | Meaning | Response |
|---|---|---|
| `pre-close` | Today's session has not closed yet. | Nothing. This is the normal daytime answer. |
| `due` | The close has passed; the run is inside its grace period. | Nothing. The next scheduler invocation will produce it. |
| `late` | Past the grace period, no durable outcome, still recoverable. | Run `scripts\run_sleeves.py --cohort <id>` now on the writer machine, then re-check health. |
| `awaiting-data` | The runner fired and deliberately executed **nobody** because required provider evidence is not complete for the target session. Nothing was recorded and nothing was lost. | Let the scheduler retry. Schwab daily publication has no documented fixed-time SLA, so readiness is proved by the official target-session candle or complete calendar-validated five-minute coverage, never by elapsed clock time. Health shows the target session, latest official session, interval coverage, and actual retry deadline. Passing the preferred grace period is still retryable; only the scheduling deadline makes it `missed`. |
| `awaiting-data` (`awaiting_reauthentication`) | The runner could not capture a snapshot at all because the Schwab refresh token was rejected or expired â€” and it hit that **before** any member started and before a snapshot was bound. Nothing was recorded and nothing was lost. | Run `python -m schwab_trader auth login` on the runner machine, then re-run the cohort. This is the one wait that never clears by itself, so it alerts once and health reads `AWAITING AUTHENTICATION â€” RETRYABLE` instead of the quiet provider wait. The session stays recordable until its scheduling deadline; after that it becomes `missed` like any other unresolved wait. A reauthentication failure at any *later* point â€” a started member, a bound snapshot â€” stays terminal. See issue #109. |
| `failed` (`data_misconfigured`) | A required capability is missing, disabled, or not vintage-safe. Waiting can never fix this, so the session fails **immediately** rather than burning its window first. | Fix the capability the error names (configure the source, enable it, or supply vintage-correct data), then re-run. This is a wiring problem, not a late provider. |
| `missed` | Past the deadline; a fresher session supersedes it. Unrecoverable. | Find out why the scheduler did not fire (machine asleep, task disabled, kill switch), or why the awaited data never arrived, then confirm the next session runs. The lost session stays a permanent gap; do not backfill it. |
| `partial` | Some members completed, some did not. | Read the printed run errors. Partial evidence is durable and is never replayed, so decide whether the session is usable before the review. |
| `failed` | A durable outcome in which no member completed. | Investigate the run errors. It is not retried automatically. |
| `completed` | Every expected member recorded an official observation. | Nothing. If it is also flagged as finishing after the grace period, check scheduler timing. |
| `closed-session` | Weekend or exchange holiday. | Nothing was ever owed. |

An official session is **all-or-nothing**. Before any member can change paper cash,
positions, fills, cycles, or official observations, a cohort-wide preflight checks that
every member's declared data is settled through the target session. If anything is
short, nobody runs and the session records `awaiting-data` rather than a terminal
partial result. For the technical rationale and regression coverage, see
`docs/incidents/2026-07-27-paper-first-cohort-daily-bars-stale.md`.

For the exact daily-first fallback, 78/42-interval coverage rules, read-only
`market-data` commands, evidence migration, and reconciliation tolerances, see
`docs/operations/schwab-bar-evidence.md`.

A gap that waiting *cannot* resolve â€” an unconfigured, disabled, or non-vintage-safe
capability â€” is separated out and fails immediately instead, so a wiring mistake is
reported in seconds rather than after the whole scheduling window.

Exit codes from `sleeve run --official`:

| Exit | Meaning |
|---|---|
| `0` | At least one member executed. |
| `1` | The session recorded a failed, partial, or missed outcome. |
| `3` | Nothing executed â€” awaiting data, or the session was already resolved. |

`run_sleeves.py` maps `3` to success and **skips** `--compare` and `--email`. That is
deliberate: a leaderboard or emailed digest on a poll where nothing ran would report the
previous session's numbers as though they were this one's, once per poll for the whole
waiting window.

Two behaviors worth knowing:

- **The headline verdict covers only the last two due sessions**, so the next step stays
  one action. Every *other* unresolved session in the scanned window (40 trading
  sessions by default, comfortably more than the 30-session review target) is still
  listed underneath, under "other session(s) never delivered complete evidence".
- **Those listed sessions do not change the exit code.** A permanent past gap must not
  make every future daily check red; it stays visible instead. The durable run record
  and the 30-session review remain the authoritative long-term account.
- **The gap horizon is the cohort's persisted `start_session`**, falling back to the
  earliest member's creation date. It is deliberately *not* derived from the earliest
  recorded run: a first session the scheduler missed outright leaves no run to derive
  from, and inferring the boundary from runs would write that day off as "before the
  cohort existed".

## Alert delivery

Alerts are claimed in the database *before* being sent, so the record survives a crash:

- already `sent` â†’ suppressed; you were already told.
- `failed` â†’ retried on the next invocation, because nothing was delivered and a retry
  cannot duplicate what you saw.
- `pending` and recent (under 15 minutes) â†’ left alone; another process may still be
  sending it.
- `pending` and stale â†’ **re-sent**, with `[possible duplicate]` in the subject and a
  note explaining why. Delivery was never confirmed, and for an operational warning a
  possible duplicate is better than a permanently lost alert. This is the opposite of
  the rule for order submission, and intentionally so: a duplicated order moves real
  money, a duplicated warning costs ten seconds.
- `pending` after 3 total attempts â†’ stops retrying and is recorded as unresolved.

`cohort health` prints a prominent warning for any transition that was never confirmed
delivered, because a silently dropped alert means you may never have been told about a
cohort problem. `cohort alerts` shows the full record, and the JSON contract carries the
same information under `alert_health`.

If no channel is configured, the transition is still recorded and marked delivered, so
turning SMTP on later does not replay a backlog of stale alerts.
