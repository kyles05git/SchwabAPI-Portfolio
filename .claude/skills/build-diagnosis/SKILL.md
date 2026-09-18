---
name: build-diagnosis
description: Diagnose a failing SchwabAPI check — Ruff, mypy, pytest, Alembic model/migration parity, TypeScript, or Vite. Finds the first causal failure and never suppresses a check. Use when a build, lint, type, or test command fails.
---

# Build diagnosis

Read `docs/agent-workflows/build-diagnosis.md` completely from the repository root, then follow it. Reproduce the failure, report the first causal failure, and never silence a check to make it pass. Edit only when the invoking GitHub task authorizes implementation and the file is inside that Issue's allowed scope.
