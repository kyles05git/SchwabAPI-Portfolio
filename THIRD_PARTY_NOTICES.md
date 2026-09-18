# Third-party notices

This repository's agent skills and workflow references draw on two external open-source
projects. Neither project is installed, vendored, or executed here. Both were inspected
read-only at the pinned commits below; no upstream setup, install, upgrade, hook, memory,
cookie, browser, deployment, or release command was run.

## ECC

- Repository: <https://github.com/affaan-m/ECC>
- Pinned commit: `0c1d7be9a750627fb2a6534c78a998cc46d03f9c`
- License: MIT — Copyright (c) 2026 Affaan Mustafa

## gstack

- Repository: <https://github.com/garrytan/gstack>
- Pinned commit: `a3259400a366593e0c909dd9ac3e59752efd2488`
- License: MIT — Copyright (c) 2026 Garry Tan

Each project's full MIT license text is in the `LICENSE` file at the root of its own
repository at the commit pinned above.

## Derivation of this repository's skills

Every entry below is a **conceptual adaptation**: the upstream component was read for its
methodology, and the SchwabAPI workflow was then written from scratch against this
repository's own safety contract, toolchain, and coordination model. No upstream file was
copied, and no upstream prose was reproduced verbatim. None of the results is a copy or a
line-level modification of upstream text.

| This repository | Upstream source consulted | Project | Relationship |
|---|---|---|---|
| `docs/agent-workflows/trading-security-review.md` | `.agents/skills/security-review/SKILL.md`, `agents/security-reviewer.md` | ECC | Conceptually adapted |
| `docs/agent-workflows/build-diagnosis.md` | `agents/build-error-resolver.md` | ECC | Conceptually adapted |
| `docs/agent-workflows/architecture-review.md` | `plan-eng-review/SKILL.md.tmpl` | gstack | Conceptually adapted |
| `docs/agent-workflows/incident-investigation.md` | `investigate/SKILL.md.tmpl` | gstack | Conceptually adapted |
| `docs/agent-workflows/dashboard-qa.md` | `qa-only/SKILL.md.tmpl`, `qa-only/SKILL.md` (test phases and per-page checks), `plan-design-review/SKILL.md.tmpl` (design-dimension framing) | gstack | Conceptually adapted |
| `docs/agent-workflows/github-task.md` (regression-first and falsification guidance) | `skills/ai-regression-testing/SKILL.md` | ECC | Conceptually adapted |
| `docs/agent-workflows/github-pr-review.md` (progressive context retrieval) | `skills/iterative-retrieval/SKILL.md` | ECC | Conceptually adapted |

The five platform skill surfaces under `.claude/skills/` and `.agents/skills/` are
original routers written for this repository; they contain no upstream material.

### What was deliberately not adopted

Both upstream projects ship capabilities that conflict with this repository's policy.
None of the following was taken: installers or setup scripts, Git or tool hooks,
persistent memory and continuous-learning systems, MCP or plugin configuration, browser
cookie import, deployment and ship or merge commands, auto-fix behavior in review
skills, and any user-global configuration change. This repository's own GitHub Issue
ledger, `agent:*` labels, and independent pull-request review remain the sole
coordination mechanism.
