# Dashboard QA workflow

Use this workflow to test the local React dashboard and produce a findings report. It is
**report-only**: find and document defects, never fix them, and never change application
state.

## Hard boundaries

- Local development server and deterministic fixtures only. Never open a deployed or
  authenticated dashboard, and never point the browser at production.
- Never import, reuse, or export browser cookies, sessions, or stored credentials.
- Never authenticate to Schwab, and never trigger a request that reaches Schwab, Neon,
  SMTP, or any broker, notification, or migration path.
- Do not mutate application state: no submitting, approving, cancelling, seeding,
  rolling over, or resetting. Read, navigate, and observe.
- Do not edit source files. A fix belongs to a separate authorized implementation task.
- Take no action whose effect leaves the browser. If a control's effect is unclear, do
  not click it; report it as untested and say why.

## Set up

The dashboard renders synthetic fixtures without any backend. Regenerate them offline and
serve the dev build:

```text
python scripts/dashboard_fixtures.py
cd frontend && npm ci && npm run dev
```

Load one scenario at a time with `?fixture=<name>`, using the names in
`frontend/src/fixtures/`. The fixture loader is development-only and is dropped from
production builds, so this surface cannot reach live data by construction.

Cover the fixtures that represent distinct states rather than all of them uniformly; the
empty, partial, late, ambiguous, and failed scenarios carry more signal than the healthy
ones.

## Check

For each scenario:

- **Navigation.** Every route reachable and every path back. No dead control, no link to
  nowhere, no state that traps the user.
- **Loading, empty, error, partial, and stale states.** Each one must be explicit and
  legible. An empty shell that looks like a healthy dashboard with no data is a defect,
  not a cosmetic issue — say so at the severity it deserves. Stale or late data must
  announce itself rather than render as current.
- **Data honesty.** The rendered numbers, timestamps, and status text match the fixture.
  Nothing is invented, rounded into a wrong claim, or silently defaulted. No account
  number beyond a masked suffix, and no secret, appears anywhere in the UI.
- **Responsive layout.** Exercise the real breakpoints. Driving Chrome's window resize is
  unreliable here; load the page in same-origin iframes at fixed widths instead, and
  check narrow, tablet, and wide. Look for horizontal overflow, clipped tables, collapsed
  controls, and unreachable actions.
- **Accessibility basics.** Landmark and heading order, labels on every control, keyboard
  reachability and a visible focus ring, alt text, and text contrast. Status conveyed by
  color alone is a finding.
- **Console failures.** Watch for errors and warnings on load and after each interaction.
  Filter to the application's own output rather than reading everything.
- **API contract failures.** Compare what the UI expects against `frontend/src/lib/types.ts`
  and the fixture payloads. Missing optional handling, an assumed non-empty array, an
  unhandled null, or a field the server never sends is a finding even when the fixture
  happens to render.

## Report

Write findings only — no fixes, no patches, no source edits.

Order by severity. Give each finding a severity, the fixture and route that reproduces it,
exact reproduction steps, observed versus expected behavior, and evidence (a screenshot
path or the console text). Distinguish defects from suggestions.

Close with the scenarios and breakpoints covered, what you deliberately did not exercise,
and anything untestable without prohibited access. Screenshots must contain synthetic
fixture data only; confirm this before attaching one.
