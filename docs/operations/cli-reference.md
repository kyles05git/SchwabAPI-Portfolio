# CLI and configuration reference

This guide contains setup and common operator commands for `schwab-trader`. See the
[project overview](../../README.md) for architecture and capabilities, and the
[safety contract](../SAFETY.md) for brokerage boundaries.

Examples use PowerShell and assume an activated virtual environment in the repository
root. `python -m schwab_trader` is equivalent to `schwab-trader`. Use `--help` on any
command group for its complete option list. Replace bracketed placeholders with actual
identifiers; examples do not create the referenced cohorts or sleeves.

## Local configuration and authentication

After installing the application as described in the README:

```powershell
Copy-Item .env.example .env
```

Edit the new `.env` locally. The relevant settings are:

| Integration | Settings |
| --- | --- |
| Schwab | `SCHWAB_CLIENT_ID`, `SCHWAB_CLIENT_SECRET`; use the registered callback `https://127.0.0.1:8182/callback` |
| SEC EDGAR | `SCHWAB_SEC_USER_AGENT`, a contact string identifying the operator |
| FRED | `SCHWAB_FRED_API_KEY` |
| Optional Claude research | `SCHWAB_ANTHROPIC_API_KEY` |
| Shared storage | `SCHWAB_DATABASE_URL`; leave empty for default local SQLite |

The Anthropic-backed research features call its API and require API access. They are
optional; deterministic paper strategies do not require an LLM.

Preserve the template defaults:

```dotenv
SCHWAB_DRY_RUN=true
SCHWAB_TRADING_ENABLED=false
SCHWAB_REQUIRE_CONFIRMATION=true
```

For brokerage data access:

```powershell
schwab-trader auth login
schwab-trader accounts select
schwab-trader auth status
schwab-trader summary
```

Login stores OAuth tokens locally; account selection records the selected account hash
in local configuration. Follow the interactive login instructions and keep the returned
callback URL private. Access-token refresh is automatic while refresh authorization is
valid. If reauthentication is required, run `auth login` again. `auth status` reports
the stored expiry information.

Real `.env`, token, data, and log files are excluded from Git. A fresh clone does not
contain a configured account or the operator's research history. Configure each machine
explicitly. In shared PostgreSQL mode, research and paper state can be shared while
OAuth credentials, approvals, live-order state, tax lots, and safety controls remain
local. Follow the [PostgreSQL runbook](../migrations/postgresql-runbook.md) for migration
and writer/reader roles. Keep database URLs out of shell arguments and version control.

On a Windows network with an inspecting proxy, Git can use the OS certificate store:

```powershell
git config --global http.sslBackend schannel
```

This uses the OS trust configuration; it does not disable certificate verification.

## Account data and order inspection

```powershell
schwab-trader accounts list
schwab-trader accounts show
schwab-trader balances
schwab-trader positions
schwab-trader quote AAPL
schwab-trader order list --hours 24
schwab-trader order status <order-id>
schwab-trader order history
schwab-trader config show
```

Order preview does not submit an order:

```powershell
schwab-trader order preview --side buy --symbol AAPL --quantity 1 --limit-price 100.00
```

The live-capable command group also includes `submit`, `cancel`, and `replace`.
Live trading requires deliberate configuration, applicable risk and duplicate checks,
and explicit approval. Consult `schwab-trader order --help` and the
[safety contract](../SAFETY.md) before using those commands. An ambiguous submission
outcome must never be automatically resubmitted.

`schwab-trader reconcile` reads brokerage orders and positions and records their
relationship to local intents and tax lots. It does not send orders, but it can update
local reconciliation records. Add `--notify` only when notification delivery is intended.
Tax-lot estimates are advisory; existing or manual fills can require operator review.

## Standalone paper sleeves

These commands create or run simulated portfolios using the configured data sources:

```powershell
schwab-trader agent strategies
schwab-trader sleeve create momo --strategy momentum --symbols large-cap
schwab-trader sleeve create val --strategy fundamental --factor book-to-market
schwab-trader sleeve list
schwab-trader sleeve run --match momo
schwab-trader sleeve compare
schwab-trader sleeve positions momo --detailed
schwab-trader sleeve report momo
```

The fundamental sleeve needs populated SEC EDGAR data. Strategy-specific requirements
are declared by the registry. For intraday paper workflows, use `sleeve watch --help`;
for manual simulated orders, use `paper --help`.

Once multiple cohorts reuse a display name, scope commands with `--cohort` or use the
stable sleeve ID where supported. An unscoped comparison includes standalone and
historical portfolios and should not be interpreted as a matched cohort comparison.

## Official cohort operations

Cohorts must be created with reviewed, fixed definitions before running. The
[bootstrap and replacement guide](replacement-cohort-plan.md) describes the paper-first
workflow. The challenger workflow has a separate
[contract](../architecture/challenger-v1-contract.md) and
[opening-evidence integration boundary](t1-open-execution.md#current-integration-boundary).

Read the current state before an official run:

```powershell
schwab-trader cohort readiness --cohort <cohort-id>
schwab-trader cohort health --cohort <cohort-id>
schwab-trader cohort alerts --cohort <cohort-id>
```

Run the eligible session on the designated writer machine:

```powershell
python scripts/run_sleeves.py --cohort <cohort-id> --compare
```

To identify a particular session, add `--scheduled-for YYYY-MM-DD`. That option is
subject to the same calendar and retry deadline; it does not turn a missed session into
a historical backfill. The script invokes the durable official runner. It does not
install an OS task or continuously poll on its own.

Compare and inspect the same cohort:

```powershell
schwab-trader sleeve compare --cohort <cohort-id> --benchmark bench-spy
schwab-trader cohort health --cohort <cohort-id> --json
schwab-trader storage recover --cohort <cohort-id> --scheduled-for YYYY-MM-DD
schwab-trader cohort review pending --cohort <cohort-id>
schwab-trader cohort review show --cohort <cohort-id>
```

`storage recover` inspects the recorded state; it does not repair it. An `awaiting-data`
result means no member executed during preflight and another invocation may retry
within the deadline. Terminal or uncertain execution states require inspection before
further action. See [scheduler setup](cohort-scheduler-setup.md) for the full state
contract and [accounting review](cohort-accounting-review.md) for recording findings.

## Market-data diagnostics

```powershell
schwab-trader market-data diagnose-bars --symbols SPY,BLK --session YYYY-MM-DD --json
```

This checks official daily coverage and, when necessary, exact-session five-minute
coverage without running a cohort. The JSON names missing or invalid intervals.
`monitor-bars` supports bounded publication monitoring and `reconcile-bars` compares a
persisted derived candle with later official data; see
[Schwab bar evidence](schwab-bar-evidence.md) for their full commands and interpretation.

## Historical data and backtesting

Data acquisition contacts the configured providers and writes research data. Coverage
is provider-dependent; the requested date range does not guarantee complete history.

```powershell
schwab-trader panel build --symbols large-cap --years 20
schwab-trader panel info
schwab-trader panel show AAPL
schwab-trader edgar fetch --symbols large-cap
schwab-trader edgar concepts AAPL --contains income
schwab-trader edgar ratios AAPL --as-of 2023-06-30
```

Run research over available price and fundamental history:

```powershell
schwab-trader backtest run --strategy momentum --days 180 --cost-bps 10
schwab-trader backtest walkforward --strategy momentum --window 180 --step 90 --folds 6
schwab-trader backtest fundamental --factor book-to-market --top 10 --start 2012-01-01
schwab-trader validate list
schwab-trader validate explain --strategy momentum --symbols large-cap
```

These backtest commands can retrieve provider data; they are not necessarily offline.
The separate [historical replay workflow](historical-replay.md) downloads and validates
five-minute research datasets without creating forward cohort observations.

Additional research tools include `universes`, `signals`, `macro`, `fundamentals`, and
`screen`. Optional `research run --web-search` calls Claude to produce a strategy
specification; `research show` and `research history` inspect those records. Research
generation must not silently change a frozen experiment definition.

## Dashboard and maintenance

```powershell
schwab-trader serve --no-live
schwab-trader storage health
schwab-trader safety status
```

`serve --no-live` reads configured application storage with live brokerage collection
disabled. A configured shared database is still accessed. Use `serve` for brokerage
sections once authentication and account selection are configured. The React build is
served by Python at `http://localhost:8787`; without it, a legacy HTML view is available.

The dashboard is read-only except for engaging the kill switch. Selecting a sleeve
opens its stable-ID-scoped record, including cash, positions, simulated orders,
valuation history, observations, and lineage. Unknown values are reported as
unavailable. Resuming trading and recording accounting decisions remain CLI actions.

For database maintenance, follow the [operator checklist](storage-operator-checklist.md)
and [backup/restore guide](storage-backup-restore.md). Backup verification and a successful
restore rehearsal are distinct checks; neither a file checksum nor an archive read
alone establishes that a backup can restore into a working database.
