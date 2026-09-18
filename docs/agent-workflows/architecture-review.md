# Architecture review workflow

Use this workflow to review a design or plan before implementation starts. The output is
a better plan and a named set of risks. It is not an implementation.

Review only. Do not write application code, create branches, claim Issues, or expand the
proposal. If the design is wrong, say so and propose the alternative; do not build it.

## Establish the target

Confirm what is under review before reading anything: a written plan, an Issue contract,
a branch diff, or named paths. If the target is ambiguous, ask once and wait. Reviewing
the wrong artifact thoroughly is worse than asking.

Then challenge the scope before the design:

- What existing code already solves part of this? Prefer extending a seam over adding a
  parallel one.
- What is the minimum change that achieves the stated goal? Name what can be deferred.
- Does the plan introduce new components, new state, or new infrastructure? Each one
  needs a reason that a simpler option cannot satisfy. Boring and existing beats novel.

If the plan is materially larger than the goal requires, say so before reviewing the
details, and give an opinionated smaller alternative.

## Review dimensions

- **Component boundaries.** Does each module own one responsibility? Does the change
  respect the existing seams, or does it reach across them? Are broker, strategy, cohort,
  storage, notification, and presentation concerns still separable?
- **Data flow.** Trace the path from input to persisted effect. Name every transformation
  and every place data crosses a process, thread, or network boundary.
- **States.** Enumerate the states the design introduces. Are unvalidated, validated, and
  committed forms distinct types rather than flags on one object? Are illegal states
  representable? What is the state after a partial failure?
- **Trust boundaries.** Where does untrusted or external data enter? Where do secrets
  live and what may read them? Which surface is reachable without authentication? What
  must the design refuse rather than accept.
- **Concurrency.** Which state is shared, which writer wins, and what is serialized. Name
  the read-modify-write windows and the locking or transaction that closes them. Assume
  two runners start at once.
- **Failure and recovery.** For each external call and each write: what does timeout,
  partial success, and an ambiguous result mean? Which failures are retryable and which
  must stop and require a human? Does the design fail closed?
- **Migrations.** Is the schema change forward-compatible with running code? Is there a
  working rollback? Can it run against existing data without loss? Model and migration
  must express the same intent.
- **Observability.** Can an operator tell afterward what happened and why, without
  reading secrets?

## Test matrix

Require a table before implementation begins. For each behavior the plan introduces:
the scenario, the level (unit, integration, end-to-end), the fake or fixture that keeps
it offline, and the observable assertion. Explicitly cover the failure and recovery paths
above, not just the success path — the untested failure path is where this design will
actually break.

Every test in the matrix must be offline, must use fakes or mocks, and must be incapable
of submitting an order or sending a notification.

## Report

Group findings by dimension and order them by severity. For each: the concern, the
concrete failure it produces, and an opinionated recommendation with its tradeoff. Mark
each one blocking or non-blocking, and separate what the plan must resolve before
implementation from what it may defer.

Close with what you did not review and what remains unknown. If a decision needs the
user — a real tradeoff rather than a defect — state the options and stop rather than
choosing silently.
