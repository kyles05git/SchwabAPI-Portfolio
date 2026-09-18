# Schwab application safety contract

Read this document before changing authentication, accounts, market data, persistence,
notifications, orders, risk controls, or live-service behavior. This private application
is for its creator's own brokerage cash account and must not be designed for third-party
use.

## Secrets and protected local state

Never place a client secret, client ID, access or refresh token, authorization code,
full callback response URL, account number, account hash, credential, or production
record in source, tests, logs, screenshots, prompts, Git history, or GitHub.

- Keep real configuration in the ignored local `.env`; examples contain placeholders.
- Keep token and state files local with restrictive user-only permissions.
- Update token files atomically: temporary file in the same directory, flush and sync,
  restrictive permissions, then atomic replace.
- Redact authorization headers, credentials, tokens, authorization codes, callback query
  strings, full account numbers, account hashes, and sensitive raw API responses.
- Masked account numbers may expose only the last four digits.
- Never copy credentials, logs, or local trading databases between machines to coordinate.

The registered callback URL is `https://127.0.0.1:8182/callback`. Validate scheme, host,
port, path, and trailing-slash semantics exactly; never silently substitute a callback.

## API and authentication boundaries

Use direct HTTPS requests and verify endpoints, fields, enum values, response shapes,
and retry guidance against current official Schwab documentation. Do not invent API
contracts or weaken TLS/certificate verification.

- Apply finite connect, read, write, and pool timeouts.
- Sanitize typed errors so response details cannot leak secrets.
- Keep the default general limiter at or below 100 requests per minute, below the portal
  order limit of 120, and honor documented server guidance.
- Refresh access tokens before expiry, serialize concurrent refreshes, and atomically
  store successful replacements.
- If refresh authorization is rejected or revoked, stop retrying and require a fresh
  interactive login.
- Automatic retries are limited to clearly read-only operations and eligible transient
  failures. Never automatically retry an order-submission POST after a timeout or lost
  response.
- For ambiguous submission results, stop new submissions, inspect recent orders, compare
  the persisted intent fingerprint, and require human resolution.

Account selection must be explicit. Display only masked numbers, use the Schwab account
hash for API calls, and never guess among multiple authorized accounts. Re-read the
selected account before order operations.

Record quote timestamps and distinguish bid, ask, last, mark, and previous close.
Reject stale or missing data for risk calculations, and never imply a quote guarantees
execution.

## Order and risk gates

The initial live-capable order surface is limited to U.S.-listed equities and ETFs,
whole-share `BUY` or `SELL` `LIMIT` orders, `DAY` duration, and normal session.
Options, shorts, margin, extended hours, complex/multi-leg, and trailing orders require
an explicit user request plus dedicated models, validation, and tests.

Keep unvalidated requests, validated intents, and submitted orders as distinct typed
states. Live submission is forbidden unless every applicable check passes:

- explicitly selected and immediately readable account hash;
- syntactically valid and, when configured, allow-listed symbol;
- positive whole-share quantity within the maximum;
- estimated notional within the maximum;
- sufficient available cash or applicable buying power for a buy;
- verified position quantity sufficient for a sell;
- available, recent quote when the check depends on a quote;
- only supported documented payload enum values;
- no duplicate recent pending or submitted intent;
- trading enabled, dry-run disabled, and confirmation required; and
- exact final human confirmation in the current terminal session.

Risk configuration fails closed on missing, malformed, stale, or ambiguous values. No
command-line flag, environment variable, cached approval, prior approval, or simple
yes/no response may bypass all controls.

Preview must never submit. Before live submission, show the masked account, side, symbol,
quantity, type, limit, maximum notional, session, duration, quote and timestamp, active
risk limits, and dry-run/live state. Require an exact phrase containing the key order
details. After confirmation, rerun critical checks, persist a unique intent fingerprint,
submit exactly once, persist sanitized result metadata, retrieve status when safe, and
tell the user to verify directly with Schwab.

Persist duplicate protection across restarts. A fingerprint includes account hash, side,
symbol, quantity, type, limit, session, duration, and a bounded bucket or intent ID.
A dedicated override still requires full review, confirmation, and audit logging.

Audit records may include timestamp, command, masked account, sanitized intent, risk
outcomes, confirmation outcome, attempt ID, HTTP status, sanitized order identifier or
`Location`, and final retrieved status. They must never include protected data.

## Autonomous trading boundary

Until a separately requested autonomous mode is explicitly built and enabled, every
manual or AI-assisted live order requires current-session human confirmation. Any future
autonomous mode must enforce quantity, notional, total-capital, symbol, daily-trade, and
daily-loss limits; expose an immediate kill switch; prove itself in paper mode against
live data before real money; and audit every decision, rationale, order, and outcome.

## Verification boundary

All automated tests run without credentials and without network access. Mock OAuth,
refresh, account mapping, balances, positions, quotes, submissions, rejections, limits,
timeouts, ambiguous results, duplicates, cash/position failures, stale quotes, and
redaction. No automated test may submit an order or notification.

Optional integration tests must be read-only, skipped by default, explicitly enabled,
and incapable of printing sensitive values. Never run migrations or live-service checks
without the task's explicit authorization and required human review.

## Official references

- OAuth: https://developer.schwab.com/user-guides/get-started/authenticate-with-oauth
- Callback requirements:
  https://developer.schwab.com/user-guides/apis-and-apps/app-callback-url-requirements
- OAuth restart and refresh:
  https://developer.schwab.com/user-guides/apis-and-apps/oauth-restart-vs-refresh-token
- API products and reference: https://developer.schwab.com/products

If current official documentation conflicts with repository instructions, stop and
surface the discrepancy for human review before changing an API contract or safety gate.
