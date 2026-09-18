---
name: dashboard-qa
description: Report-only QA of the local React dashboard against deterministic offline fixtures — navigation, loading/empty/error/partial/stale states, responsive layout, accessibility basics, console errors, and API-contract gaps. Never fixes code, imports cookies, or opens an authenticated or deployed page.
---

# Dashboard QA

Read `docs/agent-workflows/dashboard-qa.md` completely from the repository root, then follow it. Test only the local dev server with synthetic `?fixture=<name>` scenarios. Report findings; change no source, mutate no application state, and never reach a live or authenticated surface.
