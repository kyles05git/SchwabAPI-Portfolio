# Trading security review workflow

Use this workflow to security-review changes that touch authentication, secrets,
persistence, notifications, or any order path. It produces findings only. It never
applies fixes, never contacts a live service, and never probes a running system.

Read `docs/SAFETY.md` first; it is the authority this review measures against. Findings
that conflict with it are wrong. Report evidence as `file:line`, never as pasted secrets.

## Scope and posture

1. Establish the review target: a PR diff, a branch diff, or named paths. Do not widen
   past it. Security review is not an excuse to read unrelated protected state.
2. Stay read-only. Do not edit, stage, commit, or run the application. If a fix is
   obvious, describe it; a separate authorized implementation task applies it.
3. Never open `.env`, token or state files, logs, local databases, or production data to
   "verify" a finding. Reason from source, configuration examples, and tests instead.
4. Never authenticate, refresh a token, place, replace, or cancel an order, send a
   notification, or connect to Schwab, Neon, or SMTP. There is no read-only live probe.

## Review areas

Work the areas the diff actually touches. Skipping an untouched area is correct; say so.

- **Secrets and protected state.** Hardcoded or defaulted client ID, client secret,
  token, authorization code, account number, or account hash. Secrets reaching logs,
  errors, tests, fixtures, screenshots, prompts, or Git history. Unredacted authorization
  headers, callback query strings, or raw API responses. Account numbers exposing more
  than the last four digits.
- **Schwab OAuth.** Callback scheme, host, port, path, and trailing-slash validation.
  Token refresh before expiry, serialized concurrent refresh, atomic replacement with
  restrictive permissions. Rejected or revoked refresh must stop retrying and require a
  fresh interactive login rather than looping.
- **Transport and error handling.** TLS and certificate verification intact. Finite
  connect, read, write, and pool timeouts. Typed errors sanitized so detail cannot leak
  credentials. Rate limits at or below the documented ceiling.
- **Live-order isolation.** Unvalidated requests, validated intents, and submitted orders
  remain distinct typed states. Every gate in `docs/SAFETY.md` still applies, none is
  reachable around, and preview cannot submit. Dry-run and trading-disabled defaults stay
  fail-closed; no flag, environment variable, or cached approval bypasses confirmation.
- **Idempotency and ambiguous submissions.** Duplicate-intent detection still covers the
  new path. No automatic retry of an order-submission POST after a timeout or lost
  response. Ambiguous results stop new submissions and require human resolution.
- **Persistence.** Parameterized queries only. Connection strings sourced from
  configuration, never literal. Transaction boundaries, locking, and isolation adequate
  for concurrent writers. Test isolation cannot reach shared paper data.
- **Migrations.** Forward and rollback both defined and safe. No destructive change
  without an explicit contract. Model and migration parity holds.
- **Notifications.** Recipients and transport come from configuration. Message bodies
  cannot carry secrets, full account numbers, or raw provider payloads. Delivery failure
  degrades safely instead of blocking or retrying an order path.
- **Risk controls and gates.** No control weakened, made optional, defaulted open, or
  short-circuited. Configuration still fails closed on missing, malformed, stale, or
  ambiguous values.

## Report

Order findings by severity: critical, high, medium, low. Give each one a severity,
`file:line`, the evidence, the concrete impact, and a recommended correction. Separate
blocking defects from non-blocking suggestions.

State explicitly which areas you reviewed, which the diff did not touch, and what you
could not verify without prohibited access. If nothing is wrong, say so plainly and
summarize the checks performed and the residual risk.

Escalate rather than guess when a change would require an explicit user request and
independent human review: authentication behavior, live-order behavior, risk controls, or
a mandatory safety gate.
