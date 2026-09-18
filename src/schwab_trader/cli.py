"""User-facing command-line interface.

Phase 1 wires up the command structure, configuration loading, logging, and a
``config show`` command that displays sanitized settings. Trading and data
commands are registered but intentionally not implemented yet; they will be
built out in later phases per CLAUDE.md. Live trading remains disabled.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import tempfile
import time
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import NoReturn, cast

import httpx
import typer
from pydantic import ValidationError
from rich import markup
from rich.console import Console
from rich.table import Table

from schwab_trader import (
    __version__,
    agent,
    approval,
    backtest,
    benchmark_scope,
    challenger_cohort,
    cohort_alerts,
    cohort_lifecycle,
    cohort_ops,
    cohort_readiness,
    cohort_review,
    cohort_scope,
    daily_bar_fallback,
    dashboard,
    data_contracts,
    data_readiness,
    digest,
    emailfmt,
    evaluation,
    events,
    fred,
    fundamental_backtest,
    fundamentals,
    history_cache,
    intraday_backtest,
    intraday_import,
    intraday_strategies,
    llm_strategy,
    market_calendar,
    market_data,
    notify,
    orb_universe,
    orders,
    paper,
    promotion,
    reconciliation,
    research,
    risk,
    safety,
    scheduling,
    screen,
    sec_edgar,
    sec_store,
    signals,
    sleeve_runs,
    sleeves,
    state,
    strategy_registry,
    survivorship,
    taxlots,
    universes,
    usage,
)
from schwab_trader import accounts as accounts_mod
from schwab_trader import auth as oauth
from schwab_trader import client as api
from schwab_trader.config import Settings, get_settings, set_env_value
from schwab_trader.logging_config import get_logger, setup_logging
from schwab_trader.market_data_cli import market_data_app
from schwab_trader.models import (
    OrderDetail,
    OrderRequest,
    OrderSide,
    SubmittedOrder,
)
from schwab_trader.storage import factory as storage_factory
from schwab_trader.storage.cli import storage_app
from schwab_trader.storage.contracts import MarketDataEvidenceRepository
from schwab_trader.token_store import TokenStore, TokenStoreError

app = typer.Typer(
    help="Personal-use Charles Schwab trading CLI (dry-run by default).",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

#: Exit code from ``sleeve run --official`` meaning *no member executed* — the cohort is
#: waiting on data, or the session was already resolved. Distinct from success (0) and
#: from a failed/partial/missed outcome (1) so a caller can skip post-run reporting
#: without treating a normal wait as a failure.
EXIT_NOTHING_EXECUTED = 3

auth_app = typer.Typer(help="OAuth login and token management.", no_args_is_help=True)
accounts_app = typer.Typer(help="Account discovery and details.", no_args_is_help=True)
order_app = typer.Typer(help="Order preview, submission, and management.", no_args_is_help=True)
config_app = typer.Typer(help="Inspect local configuration.", no_args_is_help=True)
paper_app = typer.Typer(
    help="Paper-trading sleeve (simulated, no real money).", no_args_is_help=True
)
agent_app = typer.Typer(help="Strategy agent (runs against paper only).", no_args_is_help=True)
research_app = typer.Typer(
    help="Research step (Claude writes the strategy spec the agent follows).",
    no_args_is_help=True,
)
backtest_app = typer.Typer(
    help="Backtest rule-based strategies over historical prices.", no_args_is_help=True
)
sleeve_app = typer.Typer(
    help="Parallel paper sleeves for comparing strategies head-to-head.",
    no_args_is_help=True,
)
panel_app = typer.Typer(
    help="Build and query the local long-history daily price panel.", no_args_is_help=True
)
edgar_app = typer.Typer(
    help="SEC EDGAR point-in-time fundamentals (free historical XBRL facts).",
    no_args_is_help=True,
)
safety_app = typer.Typer(
    help="Autonomous-trading safety controls: kill switch and per-day limits.",
    no_args_is_help=True,
)
validate_app = typer.Typer(
    help="Walk-forward validate strategies and record promotion verdicts.",
    no_args_is_help=True,
)
cohort_app = typer.Typer(
    help="Official paper-cohort operations: daily health and scheduler readiness.",
    no_args_is_help=True,
)
challenger_app = typer.Typer(
    help="Bootstrap and inspect the immutable five-sleeve challenger-v1 paper cohort.",
    no_args_is_help=True,
)

app.add_typer(auth_app, name="auth")
app.add_typer(accounts_app, name="accounts")
app.add_typer(order_app, name="order")
app.add_typer(config_app, name="config")
app.add_typer(paper_app, name="paper")
app.add_typer(agent_app, name="agent")
app.add_typer(research_app, name="research")
app.add_typer(backtest_app, name="backtest")
app.add_typer(sleeve_app, name="sleeve")
app.add_typer(panel_app, name="panel")
app.add_typer(market_data_app, name="market-data")
app.add_typer(edgar_app, name="edgar")
app.add_typer(safety_app, name="safety")
app.add_typer(validate_app, name="validate")
app.add_typer(storage_app, name="storage")
app.add_typer(cohort_app, name="cohort")
app.add_typer(challenger_app, name="challenger")


@app.callback()
def _configure() -> None:
    """Personal-use Charles Schwab trading CLI (dry-run by default)."""
    # Trust the OS certificate store so HTTPS works behind TLS-inspecting proxies.
    api.enable_os_trust_store()


def _not_implemented(feature: str) -> None:
    console.print(
        f"[yellow]'{feature}' is not implemented yet.[/] "
        "It is scheduled for a later phase (see CLAUDE.md)."
    )
    raise typer.Exit(code=1)


@app.command("version")
def version() -> None:
    """Print the application version."""
    console.print(f"schwab-trader {__version__}")


@config_app.command("show")
def config_show() -> None:
    """Show current configuration with secrets masked.

    Never prints the client id/secret, tokens, or full account hash.
    """
    settings = get_settings()
    setup_logging(settings)

    table = Table(title="schwab-trader configuration", show_header=True)
    table.add_column("Setting", style="cyan", no_wrap=True)
    table.add_column("Value")

    def secret_state(present: bool) -> str:
        return "[green]set[/]" if present else "[red]not set[/]"

    table.add_row("client_id", secret_state(bool(settings.client_id)))
    table.add_row("client_secret", secret_state(bool(settings.client_secret.get_secret_value())))
    table.add_row("callback_url", settings.callback_url)
    table.add_row("account", settings.masked_account_tail())
    table.add_row("token_path", str(settings.token_path))
    table.add_row("state_db_path", str(settings.state_db_path))
    table.add_row(
        "application_storage",
        "PostgreSQL/shared" if settings.has_shared_database else "SQLite/local",
    )
    table.add_row("log_path", str(settings.log_path))
    table.add_row("dry_run", str(settings.dry_run))
    table.add_row("trading_enabled", str(settings.trading_enabled))
    table.add_row("require_confirmation", str(settings.require_confirmation))
    table.add_row("max_order_quantity", str(settings.max_order_quantity))
    table.add_row("max_order_notional", str(settings.max_order_notional))
    allowed = ", ".join(sorted(settings.allowed_symbol_set)) or "(none - all symbols allowed)"
    table.add_row("allowed_symbols", allowed)
    table.add_row("rate_limit_per_minute", str(settings.rate_limit_per_minute))
    universe = settings.agent_universe_list
    if not universe:
        agent_universe = "(built-in default)"
    elif len(universe) > 12:
        raw = settings.agent_universe.strip()
        preview = ", ".join(universe[:8])
        agent_universe = f"{raw}: {len(universe)} symbols ({preview}, ...)"
    else:
        agent_universe = ", ".join(universe)
    table.add_row("agent_universe", agent_universe)
    table.add_row("agent_max_positions", str(settings.agent_max_positions))
    table.add_row("agent_max_position_fraction", str(settings.agent_max_position_fraction))

    console.print(table)
    _print_safety_banner(settings)


def _print_safety_banner(settings: Settings) -> None:
    if settings.dry_run or not settings.trading_enabled:
        console.print(
            "[bold green]LIVE TRADING DISABLED[/] (dry_run or trading_enabled gate is engaged)."
        )
    else:
        console.print(
            "[bold red]WARNING: live-trading gates are OFF in configuration.[/] "
            "Orders still require explicit interactive confirmation."
        )


# --- Commands scheduled for later phases (registered, not yet implemented) ---


@auth_app.command("login")
def auth_login(
    open_browser: bool = typer.Option(
        False,
        "--open/--no-open",
        help="Also open the authorization URL in your default browser.",
    ),
) -> None:
    """Run the full Schwab OAuth login: URL -> paste callback -> exchange -> store.

    Displays the authorization URL, then prompts (hidden) for the redirected
    callback URL, exchanges the code for tokens, stores them securely, and
    verifies authorization with a read-only request. Makes live HTTPS calls to
    Schwab but never places an order.
    """
    settings = get_settings()
    setup_logging(settings)
    log = get_logger("schwab_trader.auth")

    if not settings.has_credentials:
        console.print(
            "[red]Missing credentials.[/] Set SCHWAB_CLIENT_ID and SCHWAB_CLIENT_SECRET "
            "in your .env, then run this command again."
        )
        raise typer.Exit(code=1)

    url = oauth.build_authorization_url(settings)
    log.info("Generated Schwab authorization URL for interactive login.")

    console.print("[bold]Step 1 - Authorize with Schwab[/]")
    console.print(
        "Open the URL below, sign in with your [bold]Schwab.com[/] brokerage login "
        "(not your developer-portal login), and authorize your individual brokerage account:\n"
    )
    # Printed to your terminal only (contains the app key, never the secret; not written to logs).
    console.print(url, markup=False, highlight=False, soft_wrap=True)

    if open_browser:
        webbrowser.open(url)
        console.print("\n[dim]Opened in your default browser.[/]")

    console.print(
        "\n[dim]After you authorize, the browser will redirect to your callback URL. It may "
        "show a connection error - that is expected, since no local server is listening. "
        "Copy the FULL redirected URL from the address bar.[/]\n"
    )
    console.print(
        "[bold]Step 2 - Complete login[/] (the code expires within ~30 seconds, so paste "
        "promptly). Your input is hidden and never logged."
    )

    raw_callback = typer.prompt("Paste the full redirected callback URL", hide_input=True)
    try:
        result = oauth.parse_authorization_response(raw_callback, settings.callback_url)
    except oauth.OAuthError as exc:
        console.print(f"[red]Callback rejected:[/] {exc}")
        raise typer.Exit(code=1) from exc

    try:
        tokens = oauth.exchange_code_for_tokens(settings, result.code)
    except oauth.OAuthError as exc:
        console.print(f"[red]Token exchange failed:[/] {exc}")
        raise typer.Exit(code=1) from exc

    store = TokenStore(settings.token_path)
    store.save(tokens)
    log.info("Stored new tokens after successful login.")
    console.print(f"[green]Tokens stored[/] at {settings.token_path} (permissions restricted).")
    console.print(
        f"[dim]Access token expires at {tokens.expires_at.isoformat()}; "
        f"refresh window ends {_fmt_dt(tokens.refresh_token_expires_at)}.[/]"
    )

    if oauth.verify_authorization(settings, tokens.access_token.get_secret_value()):
        console.print("[bold green]Authorization verified[/] with a read-only Schwab request.")
    else:
        console.print(
            "[yellow]Tokens stored, but the read-only verification request did not return "
            "success.[/] You can retry later with 'accounts list' (Phase 4)."
        )


@auth_app.command("status")
def auth_status() -> None:
    """Show whether tokens are stored and when they expire (no secrets shown)."""
    settings = get_settings()
    setup_logging(settings)
    store = TokenStore(settings.token_path)
    try:
        tokens = store.load()
    except TokenStoreError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from exc

    if tokens is None:
        console.print("[yellow]Not authenticated.[/] Run 'python -m schwab_trader auth login'.")
        raise typer.Exit(code=1)

    now = datetime.now(UTC)
    access_expired = tokens.is_access_expired(timedelta(0))
    access_state = "[red]expired[/]" if access_expired else "[green]valid[/]"
    refresh_state = "[red]expired[/]" if tokens.is_refresh_expired() else "[green]valid[/]"

    table = Table(title="Authentication status", show_header=True)
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Value")
    table.add_row("token_type", tokens.token_type)
    table.add_row("scope", tokens.scope or "(none)")
    table.add_row("access token", access_state)
    table.add_row("access expires_at", tokens.expires_at.isoformat())
    table.add_row("refresh token", refresh_state)
    table.add_row("refresh expires_at", _fmt_dt(tokens.refresh_token_expires_at))
    table.add_row("obtained_at", tokens.obtained_at.isoformat())
    table.add_row("checked_at", now.isoformat())
    console.print(table)


def _fmt_dt(value: datetime | None) -> str:
    return value.isoformat() if value is not None else "(unknown)"


def _fail(message: object) -> NoReturn:
    console.print(f"[red]Error:[/] {message}")
    raise typer.Exit(code=1)


def _build_client(settings: Settings) -> api.SchwabClient:
    """Build an authenticated client, or exit with guidance if not logged in."""
    store = TokenStore(settings.token_path)
    try:
        manager = oauth.TokenManager(settings, store)
    except TokenStoreError as exc:
        _fail(exc)
    if manager.tokens is None:
        console.print(
            "[yellow]Not authenticated.[/] Run 'python -m schwab_trader auth login' first."
        )
        raise typer.Exit(code=1)
    return api.SchwabClient(settings, manager)


def _build_client_soft(settings: Settings) -> api.SchwabClient:
    """Like _build_client, but raises OAuthError instead of exiting the process.

    For long-running callers (the dashboard's client factory): its collectors
    catch OAuthError and render the section as unavailable instead of letting
    a typer.Exit crash the whole page render.
    """
    store = TokenStore(settings.token_path)
    try:
        manager = oauth.TokenManager(settings, store)
    except TokenStoreError as exc:
        raise oauth.OAuthError(str(exc)) from exc
    if manager.tokens is None:
        raise oauth.OAuthError("not authenticated - run 'python -m schwab_trader auth login' first")
    return api.SchwabClient(settings, manager)


def _print_account_summary(summary: accounts_mod.AccountSummary) -> None:
    table = Table(title="Selected account", show_header=True)
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Value")
    table.add_row("account_type", summary.account_type)
    table.add_row("account_number", summary.account_number_masked)
    if summary.is_day_trader is not None:
        table.add_row("pattern_day_trader", str(summary.is_day_trader))
    console.print(table)


@accounts_app.command("list")
def accounts_list() -> None:
    """List authorized accounts with masked numbers and hashes."""
    settings = get_settings()
    setup_logging(settings)
    client = _build_client(settings)
    try:
        with client:
            mappings = accounts_mod.get_account_numbers(client)
    except (oauth.OAuthError, api.ApiError) as exc:
        _fail(exc)

    if not mappings:
        console.print("[yellow]No authorized accounts were returned.[/]")
        raise typer.Exit(code=1)

    selected_hash = settings.account_hash.get_secret_value()
    table = Table(title="Authorized accounts", show_header=True)
    table.add_column("#", justify="right", style="cyan")
    table.add_column("Account (masked)")
    table.add_column("Hash (masked)")
    table.add_column("Selected", justify="center")
    for position, mapping in enumerate(mappings, start=1):
        is_selected = bool(selected_hash) and selected_hash == mapping.hash_value
        table.add_row(
            str(position), mapping.masked_number, mapping.masked_hash, "*" if is_selected else ""
        )
    console.print(table)

    if not settings.has_account_selected:
        console.print(
            "[dim]Select your brokerage account with: python -m schwab_trader accounts select[/]"
        )


@accounts_app.command("select")
def accounts_select(
    index: int | None = typer.Option(
        None, "--index", help="Choose the Nth listed account non-interactively."
    ),
) -> None:
    """Select the brokerage account to use and store its hash in .env.

    Never guesses: with multiple accounts you must choose explicitly.
    """
    settings = get_settings()
    setup_logging(settings)
    log = get_logger("schwab_trader.accounts")
    client = _build_client(settings)
    try:
        with client:
            mappings = accounts_mod.get_account_numbers(client)
    except (oauth.OAuthError, api.ApiError) as exc:
        _fail(exc)

    if not mappings:
        console.print("[yellow]No authorized accounts were returned.[/]")
        raise typer.Exit(code=1)

    table = Table(title="Authorized accounts", show_header=True)
    table.add_column("#", justify="right", style="cyan")
    table.add_column("Account (masked)")
    table.add_column("Hash (masked)")
    for position, mapping in enumerate(mappings, start=1):
        table.add_row(str(position), mapping.masked_number, mapping.masked_hash)
    console.print(table)

    if index is None:
        index = typer.prompt("Select the account number (#) to use", type=int)
    if index is None or not (1 <= index <= len(mappings)):
        _fail(f"Selection must be between 1 and {len(mappings)}.")

    chosen = mappings[index - 1]
    set_env_value("SCHWAB_ACCOUNT_HASH", chosen.hash_value)
    log.info("Selected an account and stored its hash in .env.")
    console.print(
        f"[green]Saved[/] account {chosen.masked_number} to .env "
        "(SCHWAB_ACCOUNT_HASH). New commands will use it automatically."
    )

    try:
        with api.SchwabClient(
            settings, oauth.TokenManager(settings, TokenStore(settings.token_path))
        ) as verify_client:
            summary = accounts_mod.get_account_summary(verify_client, chosen.hash_value)
    except (oauth.OAuthError, api.ApiError) as exc:
        console.print(f"[yellow]Saved, but could not read the account summary:[/] {exc}")
        raise typer.Exit(code=0) from exc
    _print_account_summary(summary)


@accounts_app.command("show")
def accounts_show() -> None:
    """Show a sanitized summary of the selected account."""
    settings = get_settings()
    setup_logging(settings)
    if not settings.has_account_selected:
        console.print(
            "[yellow]No account selected.[/] Run 'python -m schwab_trader accounts select' first."
        )
        raise typer.Exit(code=1)

    client = _build_client(settings)
    try:
        with client:
            summary = accounts_mod.get_account_summary(
                client, settings.account_hash.get_secret_value()
            )
    except (oauth.OAuthError, api.ApiError) as exc:
        _fail(exc)
    _print_account_summary(summary)


def _require_account(settings: Settings) -> str:
    """Return the selected account hash, or exit with guidance if none is set."""
    if not settings.has_account_selected:
        console.print(
            "[yellow]No account selected.[/] Run 'python -m schwab_trader accounts select' first."
        )
        raise typer.Exit(code=1)
    return settings.account_hash.get_secret_value()


def _money(value: object) -> str:
    if isinstance(value, Decimal):
        return f"${value:,.2f}"
    return "-"


def _cost(value: Decimal) -> str:
    """Format a (usually sub-cent) API cost with enough precision to be useful."""
    return f"${value:.4f}"


def _usage_recorder(
    settings: Settings, kind: str
) -> tuple[Callable[[usage.Usage], None], list[Decimal]]:
    """Return an on_usage callback that persists usage and a 1-element cost tally."""
    store = storage_factory.usage_store(settings)
    total = [Decimal(0)]

    def record(u: usage.Usage) -> None:
        total[0] += store.record(kind, u)

    return record, total


def _render_balances(data: accounts_mod.Balances, settings: Settings) -> None:
    table = Table(title=f"Balances ({settings.masked_account_tail()})", show_header=True)
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Value", justify="right")
    table.add_row("account_type", data.account_type)
    table.add_row("cash_available_for_trading", _money(data.cash_available_for_trading))
    table.add_row("cash_balance", _money(data.cash_balance))
    table.add_row("unsettled_cash", _money(data.unsettled_cash))
    table.add_row("cash_available_for_withdrawal", _money(data.cash_available_for_withdrawal))
    table.add_row("total_cash", _money(data.total_cash))
    table.add_row("long_market_value", _money(data.long_market_value))
    table.add_row("liquidation_value", _money(data.liquidation_value))
    console.print(table)
    if data.unsettled_cash and data.unsettled_cash > 0:
        console.print(
            "[dim]Note: unsettled cash reflects recent sales that have not settled "
            "(cash accounts settle ~T+1) and is not yet available to re-trade.[/]"
        )


def _render_positions(holdings: list[accounts_mod.Position], settings: Settings) -> None:
    if not holdings:
        console.print("[dim]No open positions.[/]")
        return
    table = Table(title=f"Positions ({settings.masked_account_tail()})", show_header=True)
    table.add_column("Symbol", style="cyan")
    table.add_column("Type")
    table.add_column("Qty", justify="right")
    table.add_column("Settled", justify="right")
    table.add_column("Avg price", justify="right")
    table.add_column("Market value", justify="right")
    table.add_column("Day P/L", justify="right")
    for position in holdings:
        table.add_row(
            position.symbol,
            position.asset_type,
            f"{position.long_quantity:g}",
            f"{position.settled_long_quantity:g}",
            _money(position.average_price),
            _money(position.market_value),
            _money(position.current_day_profit_loss),
        )
    console.print(table)


@app.command("balances")
def balances() -> None:
    """Show cash and balances for the selected account."""
    settings = get_settings()
    setup_logging(settings)
    account_hash = _require_account(settings)
    client = _build_client(settings)
    try:
        with client:
            data = accounts_mod.get_balances(client, account_hash)
    except (oauth.OAuthError, api.ApiError) as exc:
        _fail(exc)
    _render_balances(data, settings)


@app.command("positions")
def positions() -> None:
    """Show open positions for the selected account."""
    settings = get_settings()
    setup_logging(settings)
    account_hash = _require_account(settings)
    client = _build_client(settings)
    try:
        with client:
            holdings = accounts_mod.get_positions(client, account_hash)
    except (oauth.OAuthError, api.ApiError) as exc:
        _fail(exc)
    _render_positions(holdings, settings)


@app.command("summary")
def summary() -> None:
    """Show a full account summary: account, balances, and positions."""
    settings = get_settings()
    setup_logging(settings)
    account_hash = _require_account(settings)
    client = _build_client(settings)
    try:
        with client:
            account = accounts_mod.get_account_summary(client, account_hash)
            data = accounts_mod.get_balances(client, account_hash)
            holdings = accounts_mod.get_positions(client, account_hash)
    except (oauth.OAuthError, api.ApiError) as exc:
        _fail(exc)
    _print_account_summary(account)
    _render_balances(data, settings)
    _render_positions(holdings, settings)
    _print_safety_banner(settings)


@app.command("reconcile")
def reconcile_command(
    hours: int = typer.Option(
        168,
        "--hours",
        min=1,
        max=1440,
        help="Recent broker-order window to compare (default: 7 days).",
    ),
    notify_events: bool = typer.Option(
        False,
        "--notify",
        help="Send configured notifications for lifecycle changes and new fills.",
    ),
) -> None:
    """Compare Schwab orders/positions with local lifecycle and tax-lot state.

    This command is broker-read-only: it never submits, replaces, or cancels an
    order. It records sanitized lifecycle observations locally and ingests only
    fill deltas from orders that were already being tracked.
    """
    settings = get_settings()
    setup_logging(settings)
    account_hash = _require_account(settings)
    try:
        with _build_client(settings) as client:
            report = reconciliation.reconcile_account(
                client,
                settings,
                account_hash,
                hours=hours,
                send_notifications=notify_events,
            )
    except (oauth.OAuthError, api.ApiError, OSError) as exc:
        _fail(f"Reconciliation failed without changing broker state: {exc}")

    console.print(
        f"[bold]Reconciliation complete[/] — {report.orders_seen} order(s), "
        f"{report.transitions} transition(s), {report.fills_applied} new fill(s), "
        f"{report.discrepancy_count} discrepancy item(s)."
    )
    if report.discrepancies:
        table = Table(title="Reconciliation discrepancies")
        table.add_column("severity")
        table.add_column("kind")
        table.add_column("subject")
        table.add_column("detail")
        severity_style = {"error": "bold red", "warning": "yellow", "info": "dim"}
        for item in report.discrepancies:
            style = severity_style.get(item.severity, "")
            table.add_row(
                f"[{style}]{item.severity}[/]",
                item.kind,
                item.subject,
                item.detail,
            )
        console.print(table)
    else:
        console.print("[green]No local/broker discrepancies detected.[/]")

    if any(item.severity == "error" for item in report.discrepancies):
        raise typer.Exit(code=2)


@app.command("quote")
def quote(symbol: str) -> None:
    """Show a market quote for SYMBOL (bid/ask/last/mark/prev close + freshness)."""
    settings = get_settings()
    setup_logging(settings)
    client = _build_client(settings)
    try:
        with client:
            data = market_data.get_quote(client, symbol)
    except (oauth.OAuthError, api.ApiError, market_data.QuoteError) as exc:
        _fail(exc)

    stale = data.is_stale(settings.quote_max_age)
    age_seconds = int(data.age().total_seconds())

    table = Table(title=f"Quote: {data.symbol}", show_header=True)
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Value", justify="right")
    table.add_row("bid", _money(data.bid))
    table.add_row("ask", _money(data.ask))
    table.add_row("last", _money(data.last))
    table.add_row("mark", _money(data.mark))
    table.add_row("previous_close", _money(data.previous_close))
    table.add_row("quote_time", data.quote_time.astimezone().isoformat())
    table.add_row("age", f"{age_seconds}s")
    table.add_row("security_status", data.security_status or "-")
    console.print(table)

    if stale:
        console.print(
            f"[bold yellow]STALE QUOTE[/] (older than {settings.quote_max_age_seconds}s). "
            "This quote must not be used for order pricing or risk checks."
        )
    console.print("[dim]A quote does not guarantee execution at that price.[/]")


@app.command("serve")
def serve(
    host: str = typer.Option(
        "127.0.0.1", "--host", help="Bind address (loopback only by default)."
    ),
    port: int = typer.Option(8787, "--port", min=1, max=65535, help="Port to listen on."),
    benchmark: str = typer.Option(
        "bench-spy", "--benchmark", help="Sleeve to measure excess return against."
    ),
    live: bool = typer.Option(
        True, "--live/--no-live", help="Fetch live positions/balances (needs auth + network)."
    ),
    refresh: int = typer.Option(
        30, "--refresh", min=5, max=3600, help="Auto-refresh interval in seconds."
    ),
    allow_remote: bool = typer.Option(
        False, "--allow-remote", help="Permit a non-loopback --host (serves account data!)."
    ),
) -> None:
    """Serve a local web dashboard: account, orders, sleeves, validation, safety, and more.

    Read-only except one route: ``POST /kill`` *engages* the kill switch (a safety halt
    that can only stop trading). No route can place, replace, cancel, or approve an
    order, and resuming stays on the CLI. Binds to localhost unless --allow-remote is
    set. Live sections degrade to a message without an account or network. Stop with
    Ctrl-C.
    """
    settings = get_settings()
    setup_logging(settings)
    # The dashboard resolves the benchmark *within* each comparability group, so the
    # same display name in several cohorts is expected and must not stop the server -
    # resolving it globally here refused to start over an ambiguity the dashboard
    # handles correctly. A name that matches nothing anywhere is still worth saying,
    # but only as a notice: it blanks one optional column, it is not a failure.
    if not dashboard.benchmark_is_registered(settings, benchmark):
        console.print(
            f"[yellow]No sleeve matches[/] --benchmark {benchmark!r}; "
            "excess-return columns will be blank."
        )
    client_factory = (lambda: _build_client_soft(settings)) if live else None
    scheme_host = "localhost" if dashboard._is_loopback(host) else host
    console.print(
        f"[green]Dashboard[/] on [bold]http://{scheme_host}:{port}/[/] "
        f"([dim]{settings.masked_account_tail()}, read-only + kill switch, Ctrl-C to stop[/])"
    )
    try:
        dashboard.serve(
            settings,
            host=host,
            port=port,
            client_factory=client_factory,
            benchmark=benchmark,
            allow_remote=allow_remote,
            refresh=refresh,
        )
    except dashboard.NonLoopbackHostError as exc:
        _fail(exc)
    except OSError as exc:
        _fail(f"Could not bind {host}:{port} - {exc}")


@app.command("digest")
def weekly_digest(
    benchmark: str = typer.Option(
        "bench-spy", "--benchmark", help="Sleeve to measure excess return against."
    ),
    cohort: str = typer.Option(
        "", "--cohort", help="Report only this cohort's sleeves (historical ones included)."
    ),
    send: bool = typer.Option(
        False, "--send/--no-send", help="Email the digest via the notification channel."
    ),
) -> None:
    """Print (and optionally email) a weekly performance digest of the paper sleeves.

    Reads recorded sleeve cycles (no network, places no orders) and formats the
    leaderboard, excess-over-benchmark, and activity. With --send it emails the
    digest through the configured channel (SCHWAB_SMTP_* / SCHWAB_NOTIFY_*); it is
    safe to schedule weekly.

    --cohort scopes the whole report to that cohort's members - the same scope as
    ``sleeve compare --cohort`` - and names it in the subject, so a digest covering two
    cohorts with different start dates and capital cannot be mistaken for one.

    Without --cohort every persisted sleeve is reported, and the default ``bench-spy`` -
    a name every cohort reuses - resolves against the cohorts still collecting.
    """
    settings = get_settings()
    setup_logging(settings)
    try:
        message = digest.build_daily_digest(
            settings, benchmark=benchmark, cohort_id=cohort.strip() or None
        )
    except LookupError as exc:
        # An ambiguous or missing benchmark is an operator-fixable mistake, not a crash.
        _fail(exc)
    # markup=False: the body is a plain-text email body, and a digest covering two
    # cohorts qualifies a shared sleeve name as 'control-cash [challenger-v1-...]'.
    # Rich would read that qualifier as a style tag and silently delete it, leaving two
    # identically labelled rows — exactly the confusion the qualifier exists to prevent.
    console.print(message.body, markup=False)
    if not send:
        return
    if not settings.has_smtp:
        _fail("No notification channel configured (set SCHWAB_SMTP_* / SCHWAB_NOTIFY_*).")
    try:
        notify.build_notifier(settings).send(message)
    except notify.NotifyError as exc:
        _fail(exc)
    console.print(f"[green]Digest emailed[/] to {', '.join(settings.notify_to_list)}.")


def _parse_side(value: str) -> OrderSide:
    normalized = value.strip().upper()
    if normalized in ("BUY", "B"):
        return OrderSide.BUY
    if normalized in ("SELL", "S"):
        return OrderSide.SELL
    _fail("--side must be 'buy' or 'sell'.")


def _build_order_request(side: str, symbol: str, quantity: int, limit_price: str) -> OrderRequest:
    parsed_side = _parse_side(side)
    try:
        price = Decimal(limit_price)
    except InvalidOperation:
        _fail(f"--limit-price '{limit_price}' is not a valid number.")
    try:
        return OrderRequest(side=parsed_side, symbol=symbol, quantity=quantity, limit_price=price)
    except ValidationError as exc:
        messages = "; ".join(err.get("msg", "invalid") for err in exc.errors())
        _fail(f"Invalid order: {messages}")


def _render_order_review(
    request: OrderRequest,
    settings: Settings,
    quote: market_data.Quote,
    report: risk.RiskReport,
    *,
    live: bool,
) -> None:
    stale = quote.is_stale(settings.quote_max_age)
    age_seconds = int(quote.age().total_seconds())
    if settings.dry_run:
        mode = "[green]DRY-RUN[/]"
    elif settings.trading_enabled:
        mode = "[red]LIVE[/]"
    else:
        mode = "[green]DISABLED[/]"

    summary = Table(title="Order review", show_header=False)
    summary.add_column("Field", style="cyan", no_wrap=True)
    summary.add_column("Value")
    summary.add_row("account", settings.masked_account_tail())
    summary.add_row("side", request.side.value)
    summary.add_row("symbol", request.symbol)
    summary.add_row("quantity", str(request.quantity))
    summary.add_row("order_type", request.order_type.value)
    summary.add_row("limit_price", f"{request.limit_price:.2f}")
    summary.add_row("estimated_max_notional", _money(request.estimated_notional))
    summary.add_row("session/duration", f"{request.session.value}/{request.duration.value}")
    summary.add_row(
        "quote bid/ask/last/mark",
        f"{_money(quote.bid)} / {_money(quote.ask)} / {_money(quote.last)} / {_money(quote.mark)}",
    )
    summary.add_row("quote_time", quote.quote_time.astimezone().isoformat())
    summary.add_row("quote_age", f"{age_seconds}s{' [STALE]' if stale else ''}")
    summary.add_row("mode", mode)
    console.print(summary)

    limits = Table(title="Active risk limits", show_header=False)
    limits.add_column("Limit", style="cyan", no_wrap=True)
    limits.add_column("Value")
    limits.add_row("max_order_quantity", str(settings.max_order_quantity))
    limits.add_row("max_order_notional", _money(settings.max_order_notional))
    allowed = ", ".join(sorted(settings.allowed_symbol_set)) or "(none - all allowed)"
    limits.add_row("allowed_symbols", allowed)
    limits.add_row("quote_max_age_seconds", str(settings.quote_max_age_seconds))
    console.print(limits)

    checks = Table(title="Risk checks", show_header=True)
    checks.add_column("Check", style="cyan", no_wrap=True)
    checks.add_column("Result")
    checks.add_column("Detail")
    for check in report.checks:
        result = "[green]pass[/]" if check.passed else "[red]FAIL[/]"
        checks.add_row(check.name, result, "" if check.passed else check.detail)
    console.print(checks)

    if report.passed:
        console.print("[bold green]RISK CHECKS PASSED[/]")
    else:
        reasons = "; ".join(check.name for check in report.failures)
        console.print(f"[bold red]BLOCKED[/] - failed: {reasons}")

    _render_tax_breakdown(request, settings)

    if live:
        blockers = risk.live_submission_blockers(settings)
        if blockers:
            console.print("[bold red]LIVE GATES CLOSED:[/] " + "; ".join(blockers))


def _render_tax_breakdown(request: OrderRequest, settings: Settings) -> None:
    """Show an order's estimated tax impact from the local lot ledger (advisory).

    Read-only and warn-only: it never blocks or alters an order. The ledger is built
    from recorded fills, so it stays empty until fill-recording is wired in - in that
    case it degrades to a short note rather than a table.
    """
    store = taxlots.TaxLotStore(settings.tax_lots_db_path)
    method = taxlots.LotMethod.parse(settings.tax_lot_method)
    now = datetime.now(UTC)
    window = settings.wash_sale_window_days
    warning: taxlots.WashSaleWarning | None

    if request.side is OrderSide.SELL:
        result = store.compute_sale(
            symbol=request.symbol,
            quantity=Decimal(request.quantity),
            price=request.limit_price,
            sold_at=now,
            method=method,
        )
        if result.consumptions:
            table = Table(
                title=f"Estimated tax impact (at limit, {method.value} lots)", show_header=False
            )
            table.add_column("Field", style="cyan", no_wrap=True)
            table.add_column("Value")
            table.add_row("proceeds", _money(result.proceeds))
            table.add_row("cost_basis", _money(result.cost_basis))
            gain_color = "green" if result.gain >= 0 else "red"
            table.add_row("realized_gain", f"[{gain_color}]{_money(result.gain)}[/]")
            table.add_row("short_term", _money(result.short_term_gain))
            table.add_row("long_term", _money(result.long_term_gain))
            if not result.fully_covered:
                table.add_row(
                    "coverage",
                    f"[yellow]only {result.covered_quantity} of {result.quantity} "
                    "sh have lot history[/]",
                )
            console.print(table)
            console.print(
                "[dim]Estimate from local tax lots - not tax advice; does not affect the order.[/]"
            )
            warning = store.wash_sale_on_sell(
                symbol=request.symbol, sold_at=now, is_loss=result.is_loss, window_days=window
            )
        else:
            console.print(
                f"[dim]No tax-lot history on record for {request.symbol} "
                "(lots are recorded from fills).[/]"
            )
            warning = None
    else:  # BUY
        warning = store.wash_sale_on_buy(symbol=request.symbol, buy_at=now, window_days=window)

    if warning is not None:
        dates = ", ".join(d.astimezone().date().isoformat() for d in warning.related_dates)
        console.print(f"[yellow]WASH-SALE WARNING:[/] {warning.reason} (related: {dates})")


@order_app.command("preview")
def order_preview(
    side: str = typer.Option(..., "--side", help="buy or sell"),
    symbol: str = typer.Option(..., "--symbol", help="U.S. equity/ETF symbol"),
    quantity: int = typer.Option(..., "--quantity", min=1, help="Whole shares"),
    limit_price: str = typer.Option(..., "--limit-price", help="Limit price, e.g. 100.00"),
) -> None:
    """Preview an order and run all risk checks. Never submits."""
    settings = get_settings()
    setup_logging(settings)
    account_hash = _require_account(settings)
    request = _build_order_request(side, symbol, quantity, limit_price)

    client = _build_client(settings)
    try:
        with client:
            balances = accounts_mod.get_balances(client, account_hash)
            positions = accounts_mod.get_positions(client, account_hash)
            quote = market_data.get_quote(client, request.symbol)
    except (oauth.OAuthError, api.ApiError, market_data.QuoteError) as exc:
        _fail(exc)

    report = risk.evaluate_order(
        request, settings, balances=balances, positions=positions, quote=quote
    )
    _render_order_review(request, settings, quote, report, live=False)
    console.print("[dim]Preview only - no order was submitted.[/]")


def _evaluate_live(
    client: api.SchwabClient, settings: Settings, account_hash: str, request: OrderRequest
) -> tuple[market_data.Quote, risk.RiskReport]:
    balances = accounts_mod.get_balances(client, account_hash)
    positions = accounts_mod.get_positions(client, account_hash)
    quote = market_data.get_quote(client, request.symbol)
    report = risk.evaluate_order(
        request, settings, balances=balances, positions=positions, quote=quote
    )
    return quote, report


def _reconcile_ambiguous(settings: Settings, account_hash: str, request: OrderRequest) -> None:
    """Best-effort: show recent orders so the user can reconcile. Never resubmits."""
    console.print("[bold]Checking recent orders so you can reconcile (NOT resubmitting)...[/]")
    to_time = datetime.now(UTC)
    from_time = to_time - timedelta(hours=1)
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    try:
        with _build_client(settings) as client:
            recent = orders.get_recent_orders(
                client,
                account_hash,
                from_time=from_time.strftime(fmt),
                to_time=to_time.strftime(fmt),
            )
    except (oauth.OAuthError, api.ApiError) as exc:
        console.print(f"[yellow]Could not fetch recent orders:[/] {exc}")
        console.print("[bold]Verify in the Schwab app whether this order exists before acting.[/]")
        return
    matches = [
        order
        for order in recent
        if isinstance(order, dict)
        and any(
            leg.get("instrument", {}).get("symbol") == request.symbol
            for leg in order.get("orderLegCollection", [])
        )
    ]
    console.print(f"Found {len(matches)} recent order(s) for {request.symbol} in the last hour.")
    console.print("[bold]Verify in the Schwab app whether your order exists. Do NOT resubmit.[/]")


OrderAction = Callable[[api.SchwabClient, str, OrderRequest], SubmittedOrder]


def _run_gated_order(
    settings: Settings,
    account_hash: str,
    request: OrderRequest,
    *,
    command: str,
    confirm_phrase: str,
    action: OrderAction,
    override: bool,
    approval_token: str | None = None,
    approval_store: approval.ApprovalStore | None = None,
) -> bool:
    """Shared gated flow for submit and replace. Returns True iff a live order was placed.

    review -> risk gate -> config gates -> kill switch -> duplicate/override -> exact
    confirmation -> re-check fresh data -> record pending -> action once -> mark ->
    status. Stops before any live call in dry-run/gated mode. Audits every step.

    When ``approval_token``/``approval_store`` are given (the notify-and-approve path),
    a single-use, pre-authorized token stands in for *only* the interactive typed
    phrase: it is consumed atomically at the confirmation point, after the dry-run/
    gated early-return (so a preview never burns it) and after the kill-switch and
    duplicate checks. Every other gate is unchanged. Consuming re-verifies the token
    is unexpired, unused, and bound to this exact order + account.
    """
    log = get_logger("schwab_trader.orders")
    account_tail = settings.masked_account_tail()
    store = state.StateStore(settings.state_db_path)

    def audit(event: str, detail: str | None = None) -> None:
        store.append_audit(command=command, event=event, account_tail=account_tail, detail=detail)

    audit("start", request.describe())

    try:
        with _build_client(settings) as client:
            quote, report = _evaluate_live(client, settings, account_hash, request)
    except (oauth.OAuthError, api.ApiError, market_data.QuoteError) as exc:
        audit("data_error", str(exc))
        _fail(exc)

    _render_order_review(request, settings, quote, report, live=True)

    if not report.passed:
        audit("risk_blocked", "; ".join(check.name for check in report.failures))
        _fail("Risk checks failed - order not submitted.")

    gate_blockers = risk.live_submission_blockers(settings)
    if settings.dry_run or gate_blockers:
        reasons = (["SCHWAB_DRY_RUN=true"] if settings.dry_run else []) + gate_blockers
        console.print(
            f"[bold green]DRY-RUN / gated:[/] no live order was submitted ({'; '.join(reasons)})."
        )
        console.print(
            "[dim]Enable live trading only by intentionally setting the config gates; "
            "you will still have to type the exact confirmation phrase.[/]"
        )
        audit("dry_run_or_gated", "; ".join(reasons))
        return False

    # --- LIVE PATH (all gates open) -----------------------------------------
    # Out-of-band notifications on live-order outcomes (best-effort; never blocks the
    # order). A NullNotifier (no channel configured) makes every emit a silent no-op.
    notifier = notify.build_notifier(settings)
    # The kill switch is a global halt for real orders (manual and strategy-driven).
    if safety.KillSwitch(settings.kill_switch_path).is_engaged():
        audit("kill_switch_blocked")
        events.emit(
            notifier, events.kill_switch_blocked_message(request, account_tail=account_tail)
        )
        _fail("Kill switch engaged - no live order submitted. Clear with 'safety resume'.")

    fingerprint = state.compute_fingerprint(account_hash, request)
    duplicate = store.find_recent_duplicate(fingerprint, settings.duplicate_window)
    if duplicate and not override:
        console.print(
            f"[bold red]DUPLICATE BLOCKED[/] - an identical order (status "
            f"{duplicate.status}, id {duplicate.order_id or 'n/a'}) was recorded at "
            f"{duplicate.created_at.astimezone().isoformat(timespec='seconds')}."
        )
        console.print("Review with 'order history'. To proceed anyway, re-run with --override.")
        audit("duplicate_blocked", f"dup_id={duplicate.id}")
        raise typer.Exit(code=1)
    if duplicate and override:
        console.print(
            f"[yellow]OVERRIDE[/] of duplicate (status {duplicate.status}, "
            f"id {duplicate.order_id or 'n/a'}). Full confirmation still required."
        )
        audit("duplicate_override", f"dup_id={duplicate.id}")

    if approval_token is not None and approval_store is not None:
        # A pre-authorized, single-use token stands in for the typed phrase. It is
        # consumed atomically here (once), re-verified against this exact order +
        # account. All other gates above and below still apply.
        try:
            approved = approval_store.consume(token=approval_token, account_hash=account_hash)
        except approval.ApprovalError as exc:
            audit("approval_rejected", str(exc))
            _fail(f"Approval token rejected: {exc}")
        audit("confirmation_via_approval", f"approval_id={approved.id}")
        console.print(f"[green]Confirmed via approval token[/] (approval #{approved.id}).")
    else:
        console.print("\n[bold red]This will place a LIVE order with real money.[/]")
        console.print(f"Type this phrase EXACTLY to confirm:  [bold]{confirm_phrase}[/]")
        typed = typer.prompt("Confirmation")
        if typed.strip() != confirm_phrase:
            audit("confirmation_failed")
            _fail("Confirmation phrase did not match - order not submitted.")

    intent_id: int | None = None
    result: SubmittedOrder | None = None
    try:
        with _build_client(settings) as live_client:
            # Re-run critical checks against fresh data right before submitting.
            _, recheck = _evaluate_live(live_client, settings, account_hash, request)
            if not recheck.passed:
                audit("recheck_failed", "; ".join(c.name for c in recheck.failures))
                _fail("Risk re-check failed after confirmation - order not submitted.")
            if store.find_recent_duplicate(fingerprint, settings.duplicate_window) and not override:
                audit("duplicate_recheck_blocked")
                _fail("Duplicate detected during final checks - order not submitted.")

            intent_id = store.record_pending(account_hash=account_hash, request=request)
            audit("submit_attempt", f"intent={intent_id}")
            log.info("Placing order once (intent %s).", intent_id)
            result = action(live_client, account_hash, request)
    except orders.AmbiguousSubmission as exc:
        audit("ambiguous", str(exc))  # intent stays pending on purpose
        events.emit(notifier, events.order_ambiguous_message(request, account_tail=account_tail))
        console.print(
            "[bold red]AMBIGUOUS RESULT[/] - the order may or may not have been placed. "
            "It will NOT be retried automatically."
        )
        _reconcile_ambiguous(settings, account_hash, request)
        raise typer.Exit(code=2) from exc
    except orders.OrderRejected as exc:
        if intent_id is not None:
            store.mark_failed(intent_id)
        audit("rejected", str(exc))
        events.emit(
            notifier,
            events.order_rejected_message(request, reason=str(exc), account_tail=account_tail),
        )
        _fail(str(exc))
    except (oauth.OAuthError, api.ApiError, market_data.QuoteError) as exc:
        if intent_id is not None:
            store.mark_failed(intent_id)
        audit("live_error", str(exc))
        _fail(exc)

    assert result is not None and intent_id is not None  # for type-checkers; live path only
    store.mark_submitted(intent_id, order_id=result.order_id)
    if result.order_id:
        reconciliation.track_submission(
            settings,
            order_id=result.order_id,
            request=request,
        )
    audit("submitted", f"order_id={result.order_id} status={result.status.value}")
    events.emit(
        notifier,
        events.order_filled_message(
            request, order_id=result.order_id, status=result.status.value, account_tail=account_tail
        ),
    )
    # Count the live trade against the autonomous per-day limits.
    safety.SafetyLedger(settings.agent_activity_db_path).record(trades=1)
    console.print(
        f"[bold green]ORDER PLACED[/] - id {result.order_id or 'unknown'} "
        f"status {result.status.value}"
    )
    console.print("[bold]Verify this order now in the Schwab app or website.[/]")

    if result.order_id:
        try:
            with _build_client(settings) as status_client:
                final = orders.get_order(status_client, account_hash, result.order_id)
            console.print(f"Retrieved status: [bold]{final.status.value}[/]")
            audit("status", final.status.value)
            _record_fill_tax_lot(settings, request, final, audit)
        except (oauth.OAuthError, api.ApiError):
            console.print("[dim]Could not retrieve final status; check Schwab.[/]")
    return True


def _record_fill_tax_lot(
    settings: Settings,
    request: OrderRequest,
    detail: OrderDetail,
    audit: Callable[[str, str | None], None],
) -> None:
    """Record an immediate status through the idempotent reconciliation path.

    The submitted order is tracked at zero fills before this helper runs. Partial
    and complete executions therefore use the same delta/reservation logic as later
    reconciliation passes. Bookkeeping remains post-order and can never affect a
    broker operation or safety gate.
    """
    reconciliation.record_immediate_detail(settings, request, detail, audit)


@order_app.command("submit")
def order_submit(
    side: str = typer.Option(..., "--side", help="buy or sell"),
    symbol: str = typer.Option(..., "--symbol", help="U.S. equity/ETF symbol"),
    quantity: int = typer.Option(..., "--quantity", min=1, help="Whole shares"),
    limit_price: str = typer.Option(..., "--limit-price", help="Limit price, e.g. 100.00"),
    override: bool = typer.Option(
        False, "--override", help="Override a duplicate block (still requires full confirmation)."
    ),
) -> None:
    """Submit a live order behind every safety gate.

    Requires all risk checks to pass AND the config gates open (trading_enabled,
    dry_run=false, require_confirmation) AND the exact typed confirmation phrase.
    In dry-run / gated mode it shows the review and stops without submitting.
    """
    settings = get_settings()
    setup_logging(settings)
    account_hash = _require_account(settings)
    request = _build_order_request(side, symbol, quantity, limit_price)
    _run_gated_order(
        settings,
        account_hash,
        request,
        command="order submit",
        confirm_phrase=request.confirmation_phrase,
        action=orders.submit_order,
        override=override,
    )


@order_app.command("replace")
def order_replace(
    order_id: str = typer.Argument(..., help="Existing Schwab order id to replace"),
    side: str = typer.Option(..., "--side", help="buy or sell"),
    symbol: str = typer.Option(..., "--symbol", help="U.S. equity/ETF symbol"),
    quantity: int = typer.Option(..., "--quantity", min=1, help="Whole shares"),
    limit_price: str = typer.Option(..., "--limit-price", help="New limit price, e.g. 100.00"),
    override: bool = typer.Option(
        False, "--override", help="Override a duplicate block (still requires full confirmation)."
    ),
) -> None:
    """Cancel-and-replace an existing order with new parameters (live, gated).

    Shows the existing order, then runs the full gated flow on the replacement.
    """
    settings = get_settings()
    setup_logging(settings)
    account_hash = _require_account(settings)
    request = _build_order_request(side, symbol, quantity, limit_price)

    try:
        with _build_client(settings) as client:
            existing = orders.get_order(client, account_hash, order_id)
    except (oauth.OAuthError, api.ApiError) as exc:
        _fail(exc)
    console.print("[bold]Replacing this existing order:[/]")
    _render_order_detail(existing)

    def _replace(client: api.SchwabClient, acct: str, req: OrderRequest) -> SubmittedOrder:
        return orders.replace_order(client, acct, order_id, req)

    _run_gated_order(
        settings,
        account_hash,
        request,
        command="order replace",
        confirm_phrase=f"REPLACE {order_id} WITH {request.confirmation_phrase}",
        action=_replace,
        override=override,
    )


def _render_order_detail(detail: OrderDetail) -> None:
    def _num(value: object) -> str:
        return str(value) if value is not None else "-"

    table = Table(title=f"Order {detail.order_id}", show_header=False)
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Value")
    table.add_row("status", detail.status.value)
    table.add_row("symbol", detail.symbol or "-")
    table.add_row("side", detail.side or "-")
    table.add_row("quantity", _num(detail.quantity))
    table.add_row("filled", _num(detail.filled_quantity))
    table.add_row("remaining", _num(detail.remaining_quantity))
    table.add_row("limit_price", _money(detail.limit_price))
    table.add_row("cancelable", _num(detail.cancelable))
    table.add_row("entered_time", detail.entered_time or "-")
    table.add_row("close_time", detail.close_time or "-")
    console.print(table)


@order_app.command("status")
def order_status(order_id: str = typer.Argument(..., help="Schwab order id")) -> None:
    """Show the current status/details of an order by id."""
    settings = get_settings()
    setup_logging(settings)
    account_hash = _require_account(settings)
    client = _build_client(settings)
    try:
        with client:
            detail = orders.get_order(client, account_hash, order_id)
    except (oauth.OAuthError, api.ApiError) as exc:
        _fail(exc)
    _render_order_detail(detail)


@order_app.command("list")
def order_list(
    hours: int = typer.Option(24, "--hours", min=1, max=168, help="Look back this many hours."),
) -> None:
    """List recent orders for the selected account."""
    settings = get_settings()
    setup_logging(settings)
    account_hash = _require_account(settings)
    to_time = datetime.now(UTC)
    from_time = to_time - timedelta(hours=hours)
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    client = _build_client(settings)
    try:
        with client:
            raw = orders.get_recent_orders(
                client,
                account_hash,
                from_time=from_time.strftime(fmt),
                to_time=to_time.strftime(fmt),
            )
    except (oauth.OAuthError, api.ApiError) as exc:
        _fail(exc)

    details = [
        orders.parse_order_detail(item, fallback_id=str(item.get("orderId", "?"))) for item in raw
    ]
    if not details:
        console.print(f"[dim]No orders in the last {hours}h.[/]")
        return

    table = Table(
        title=f"Orders (last {hours}h, {settings.masked_account_tail()})", show_header=True
    )
    table.add_column("order_id", style="cyan")
    table.add_column("status")
    table.add_column("side")
    table.add_column("symbol")
    table.add_column("qty", justify="right")
    table.add_column("filled", justify="right")
    table.add_column("limit", justify="right")
    for detail in details:
        table.add_row(
            detail.order_id,
            detail.status.value,
            detail.side or "-",
            detail.symbol or "-",
            str(detail.quantity) if detail.quantity is not None else "-",
            str(detail.filled_quantity) if detail.filled_quantity is not None else "-",
            _money(detail.limit_price),
        )
    console.print(table)


@order_app.command("cancel")
def order_cancel(order_id: str = typer.Argument(..., help="Schwab order id to cancel")) -> None:
    """Cancel an open order (a live action; requires typed confirmation)."""
    settings = get_settings()
    setup_logging(settings)
    account_hash = _require_account(settings)
    account_tail = settings.masked_account_tail()
    store = state.StateStore(settings.state_db_path)

    def audit(event: str, detail: str | None = None) -> None:
        store.append_audit(
            command="order cancel", event=event, account_tail=account_tail, detail=detail
        )

    client = _build_client(settings)
    try:
        with client:
            detail = orders.get_order(client, account_hash, order_id)
    except (oauth.OAuthError, api.ApiError) as exc:
        audit("lookup_error", str(exc))
        _fail(exc)

    _render_order_detail(detail)
    audit("start", f"order_id={order_id} status={detail.status.value}")

    console.print("\n[bold red]Canceling sends a live request to Schwab.[/]")
    phrase = f"CANCEL {order_id}"
    typed = typer.prompt(f"Type '{phrase}' to confirm")
    if typed.strip() != phrase:
        audit("confirmation_failed")
        _fail("Confirmation did not match - the order was not canceled.")

    try:
        with _build_client(settings) as cancel_client:
            orders.cancel_order(cancel_client, account_hash, order_id)
    except orders.AmbiguousSubmission as exc:
        audit("ambiguous", str(exc))
        console.print(f"[bold red]AMBIGUOUS:[/] {exc}")
        raise typer.Exit(code=2) from exc
    except (oauth.OAuthError, api.ApiError) as exc:
        audit("cancel_failed", str(exc))
        _fail(f"Cancel failed: {exc}")

    audit("canceled", f"order_id={order_id}")
    console.print(f"[bold green]Cancel requested[/] for order {order_id}.")
    console.print("[bold]Verify in the Schwab app that the order is canceled.[/]")
    try:
        with _build_client(settings) as status_client:
            updated = orders.get_order(status_client, account_hash, order_id)
        console.print(f"Current status: [bold]{updated.status.value}[/]")
    except (oauth.OAuthError, api.ApiError):
        console.print("[dim]Could not retrieve updated status; check Schwab.[/]")


@order_app.command("history")
def order_history(
    limit: int = typer.Option(10, "--limit", min=1, help="How many recent intents to show."),
) -> None:
    """Show recently recorded order intents (from the local state DB)."""
    settings = get_settings()
    setup_logging(settings)
    store = state.StateStore(settings.state_db_path)
    intents = store.recent_intents(limit)
    if not intents:
        console.print("[dim]No recorded order intents yet.[/]")
        return

    table = Table(title="Recent order intents", show_header=True)
    table.add_column("id", justify="right", style="cyan")
    table.add_column("created_at")
    table.add_column("account")
    table.add_column("side")
    table.add_column("symbol")
    table.add_column("qty", justify="right")
    table.add_column("limit")
    table.add_column("status")
    table.add_column("order_id")
    for record in intents:
        table.add_row(
            str(record.id),
            record.created_at.astimezone().isoformat(timespec="seconds"),
            record.account_tail,
            record.side,
            record.symbol,
            str(record.quantity),
            record.limit_price,
            record.status,
            record.order_id or "-",
        )
    console.print(table)


# --- Paper trading (simulated sleeve, no real money) ------------------------


def _paper_engine(settings: Settings) -> paper.PaperEngine:
    return storage_factory.default_paper_engine(settings)


def _paper_trade(side: str, symbol: str, quantity: int, limit_price: str) -> None:
    settings = get_settings()
    setup_logging(settings)
    request = _build_order_request(side, symbol, quantity, limit_price)
    engine = _paper_engine(settings)
    try:
        with _build_client(settings) as client:
            quote = market_data.get_quote(client, request.symbol)
    except (oauth.OAuthError, api.ApiError, market_data.QuoteError) as exc:
        _fail(exc)

    order = engine.place_order(request, quote)
    if order.status == paper.STATUS_FILLED:
        console.print(
            f"[bold green]PAPER FILL[/] {order.side} {order.quantity} {order.symbol} "
            f"@ {_money(order.fill_price)}"
        )
    else:
        console.print(f"[yellow]PAPER REJECTED[/] - {order.reason}")
    console.print(f"[dim]Paper cash now {_money(engine.account().cash)}.[/]")


@paper_app.command("buy")
def paper_buy(
    symbol: str = typer.Argument(..., help="U.S. equity/ETF symbol"),
    quantity: int = typer.Option(..., "--quantity", min=1, help="Whole shares"),
    limit_price: str = typer.Option(..., "--limit-price", help="Limit price, e.g. 100.00"),
) -> None:
    """Simulate a BUY against the current live quote (no real money)."""
    _paper_trade("buy", symbol, quantity, limit_price)


@paper_app.command("sell")
def paper_sell(
    symbol: str = typer.Argument(..., help="U.S. equity/ETF symbol"),
    quantity: int = typer.Option(..., "--quantity", min=1, help="Whole shares"),
    limit_price: str = typer.Option(..., "--limit-price", help="Limit price, e.g. 100.00"),
) -> None:
    """Simulate a SELL against the current live quote (no real money)."""
    _paper_trade("sell", symbol, quantity, limit_price)


@paper_app.command("status")
def paper_status() -> None:
    """Show the paper sleeve: cash, positions (marked to live quotes), and P&L."""
    settings = get_settings()
    setup_logging(settings)
    engine = _paper_engine(settings)
    positions = engine.positions()

    marks: dict[str, Decimal | None] = {}
    if positions:
        try:
            with _build_client(settings) as client:
                for position in positions:
                    try:
                        marks[position.symbol] = market_data.get_quote(client, position.symbol).mark
                    except market_data.QuoteError:
                        marks[position.symbol] = None
        except (oauth.OAuthError, api.ApiError) as exc:
            console.print(f"[yellow]Could not fetch live quotes:[/] {exc} (using avg cost).")

    valuation = engine.value(marks)

    if positions:
        table = Table(title="Paper positions", show_header=True)
        table.add_column("Symbol", style="cyan")
        table.add_column("Qty", justify="right")
        table.add_column("Avg cost", justify="right")
        table.add_column("Mark", justify="right")
        table.add_column("Value", justify="right")
        table.add_column("Unrealized", justify="right")
        for position in positions:
            mark = marks.get(position.symbol)
            mark_dec = mark if isinstance(mark, Decimal) else position.avg_cost
            value = mark_dec * position.quantity
            unrealized = (mark_dec - position.avg_cost) * position.quantity
            table.add_row(
                position.symbol,
                str(position.quantity),
                _money(position.avg_cost),
                _money(mark) if isinstance(mark, Decimal) else "-",
                _money(value),
                _money(unrealized),
            )
        console.print(table)
    else:
        console.print("[dim]No paper positions.[/]")

    summary = Table(title="Paper sleeve", show_header=False)
    summary.add_column("Field", style="cyan", no_wrap=True)
    summary.add_column("Value", justify="right")
    summary.add_row("starting_cash", _money(valuation.starting_cash))
    summary.add_row("cash", _money(valuation.cash))
    summary.add_row("positions_value", _money(valuation.positions_value))
    summary.add_row("total_value", _money(valuation.total_value))
    summary.add_row("unrealized_pnl", _money(valuation.unrealized_pnl))
    summary.add_row("realized_pnl", _money(valuation.realized_pnl))
    summary.add_row("total_return", f"{valuation.total_return_pct:.2f}%")
    console.print(summary)


@paper_app.command("orders")
def paper_orders(
    limit: int = typer.Option(15, "--limit", min=1, help="How many recent paper orders."),
) -> None:
    """List recent paper orders (fills and rejections)."""
    settings = get_settings()
    setup_logging(settings)
    engine = _paper_engine(settings)
    records = engine.recent_orders(limit)
    if not records:
        console.print("[dim]No paper orders yet.[/]")
        return
    table = Table(title="Recent paper orders", show_header=True)
    table.add_column("id", justify="right", style="cyan")
    table.add_column("created_at")
    table.add_column("side")
    table.add_column("symbol")
    table.add_column("qty", justify="right")
    table.add_column("limit", justify="right")
    table.add_column("status")
    table.add_column("fill/reason")
    for record in records:
        outcome = (
            _money(record.fill_price) if record.fill_price is not None else (record.reason or "-")
        )
        table.add_row(
            str(record.id),
            record.created_at.astimezone().isoformat(timespec="seconds"),
            record.side,
            record.symbol,
            str(record.quantity),
            _money(record.limit_price),
            record.status,
            outcome,
        )
    console.print(table)


@paper_app.command("reset")
def paper_reset() -> None:
    """Reset the paper sleeve to its starting cash (wipes positions and orders)."""
    settings = get_settings()
    setup_logging(settings)
    engine = _paper_engine(settings)
    console.print(f"This wipes the paper sleeve back to {_money(settings.paper_starting_cash)}.")
    typed = typer.prompt("Type 'RESET' to confirm")
    if typed.strip() != "RESET":
        _fail("Not confirmed - paper sleeve unchanged.")
    engine.reset()
    console.print(f"[green]Paper sleeve reset[/] to {_money(settings.paper_starting_cash)}.")


# --- Agent (strategy loop; paper only) --------------------------------------


@agent_app.command("strategies")
def agent_strategies() -> None:
    """List available strategies."""
    for name in agent.available_strategies():
        console.print(f"- {name}")
    console.print(
        f"- {llm_strategy.LLM_STRATEGY_NAME}  "
        "[dim](Claude-backed; needs SCHWAB_ANTHROPIC_API_KEY)[/]"
    )


def _expand_symbols(symbols: str) -> list[str]:
    """Expand a --symbols value: a preset name (e.g. 'large-cap') or CSV tickers."""
    raw = symbols.strip()
    if not raw:
        return []
    preset = universes.get_preset(raw)
    if preset is not None:
        return preset
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def _resolve_universe(settings: Settings, symbols: str) -> list[str]:
    """Universe precedence: --symbols override, then config, then built-in default."""
    return _expand_symbols(symbols) or settings.agent_universe_list or agent.DEFAULT_UNIVERSE


def _build_agent_strategy(
    settings: Settings,
    strategy: str,
    universe: list[str],
    *,
    on_usage: llm_strategy.UsageSink | None = None,
) -> agent.Strategy:
    """Construct a strategy by name, wiring the LLM strategy to Anthropic if asked."""
    if strategy == llm_strategy.LLM_STRATEGY_NAME:
        api_key = settings.anthropic_api_key.get_secret_value()
        spec = storage_factory.research_store(settings).latest()
        if spec is not None:
            console.print(
                f"[dim]Following strategy spec from {spec.created_at:%Y-%m-%d %H:%M} "
                f"({spec.model}): {spec.market_regime}.[/]"
            )
        else:
            console.print(
                "[dim]No strategy spec yet - using defaults. Run 'schwab-trader research run' "
                "to have Claude write one.[/]"
            )
        try:
            if spec is not None:
                return llm_strategy.build_llm_strategy(
                    universe,
                    api_key=api_key,
                    model=settings.llm_model,
                    spec_guidance=spec.guidance_text(),
                    max_positions=spec.max_positions,
                    max_position_fraction=spec.max_position_fraction,
                    on_usage=on_usage,
                )
            return llm_strategy.build_llm_strategy(
                universe,
                api_key=api_key,
                model=settings.llm_model,
                max_positions=settings.agent_max_positions,
                max_position_fraction=settings.agent_max_position_fraction,
                on_usage=on_usage,
            )
        except llm_strategy.AnthropicUnavailable as exc:
            _fail(exc)
    try:
        return agent.build_strategy(strategy, universe)
    except KeyError:
        names = ", ".join([*agent.available_strategies(), llm_strategy.LLM_STRATEGY_NAME])
        _fail(f"Unknown strategy '{strategy}'. Try: {names}")


@agent_app.command("run")
def agent_run(
    strategy: str = typer.Option("dip-buyer", "--strategy", help="Strategy name."),
    symbols: str = typer.Option(
        "", "--symbols", help="Comma-separated universe override (default: built-in)."
    ),
) -> None:
    """Run one strategy decision cycle against the paper sleeve (no real money)."""
    settings = get_settings()
    setup_logging(settings)
    universe = _resolve_universe(settings, symbols)
    on_usage, cost_tally = _usage_recorder(settings, "execution")
    strat = _build_agent_strategy(settings, strategy, universe, on_usage=on_usage)

    engine = _paper_engine(settings)
    console.print(
        f"[dim]Strategy [bold]{strategy}[/] over {len(strat.universe)} symbols "
        "(paper only, simulated money).[/]"
    )
    try:
        with _build_client(settings) as client:

            def source(symbol: str) -> market_data.Quote:
                return market_data.get_quote(client, symbol)

            report = agent.AgentRunner(strat, engine, source).run_cycle()
    except (oauth.OAuthError, api.ApiError, llm_strategy.LLMError) as exc:
        _fail(exc)

    cycle_id = storage_factory.default_evaluation_store(settings).record_cycle(report)

    if report.missing_quotes:
        console.print(f"[dim]No quote for: {', '.join(report.missing_quotes)}[/]")

    if not report.outcomes:
        console.print("[dim]No orders proposed this cycle (hold).[/]")
    else:
        table = Table(title="Agent decisions", show_header=True)
        table.add_column("side")
        table.add_column("symbol", style="cyan")
        table.add_column("qty", justify="right")
        table.add_column("limit", justify="right")
        table.add_column("outcome")
        table.add_column("fill/reason")
        for outcome in report.outcomes:
            request = outcome.proposal.request
            fill = _money(outcome.fill_price) if outcome.fill_price is not None else outcome.detail
            table.add_row(
                request.side.value,
                request.symbol,
                str(request.quantity),
                _money(request.limit_price),
                outcome.status,
                fill,
            )
        console.print(table)
        for outcome in report.outcomes:
            console.print(
                f"[dim]- {outcome.proposal.request.symbol}: {outcome.proposal.rationale}[/]"
            )

    console.print(
        f"Paper value: {_money(report.starting_value)} -> {_money(report.ending_value)} "
        "(after this cycle's fills)."
    )
    if cost_tally[0] > 0:
        console.print(f"[dim]API cost this cycle: {_cost(cost_tally[0])}.[/]")
    console.print(
        f"[dim]Recorded cycle #{cycle_id}. See 'schwab-trader agent report' or "
        "'schwab-trader paper status'.[/]"
    )


def _parse_interval(text: str) -> int:
    """Parse '30s' / '5m' / '1h' / '90' into seconds (minimum 5)."""
    raw = text.strip().lower()
    mult = 1
    if raw.endswith("s"):
        raw = raw[:-1]
    elif raw.endswith("m"):
        raw, mult = raw[:-1], 60
    elif raw.endswith("h"):
        raw, mult = raw[:-1], 3600
    try:
        seconds = int(float(raw) * mult)
    except ValueError:
        _fail(f"Invalid --interval '{text}'. Use e.g. 30s, 5m, 1h.")
    if seconds < 5:
        _fail("--interval must be at least 5 seconds.")
    return seconds


def _parse_until(text: str) -> datetime:
    """Parse a local 'HH:MM' clock time into a datetime for today (local tz)."""
    try:
        hours, minutes = (int(part) for part in text.strip().split(":", 1))
        now = datetime.now().astimezone()
        return now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
    except (ValueError, TypeError):
        _fail(f"Invalid --until '{text}'. Use HH:MM (24-hour local), e.g. 16:00.")


@agent_app.command("loop")
def agent_loop(
    strategy: str = typer.Option("llm", "--strategy", help="Strategy name."),
    symbols: str = typer.Option("", "--symbols", help="Comma-separated universe override."),
    interval: str = typer.Option("5m", "--interval", help="Time between cycles (e.g. 30s, 5m)."),
    until: str = typer.Option("", "--until", help="Stop at this local time today (e.g. 16:00)."),
    max_cycles: int = typer.Option(
        0, "--max-cycles", min=0, help="Stop after N cycles (0 = unlimited)."
    ),
) -> None:
    """Run a strategy on a timer until the close (paper only). Ctrl-C to stop."""
    settings = get_settings()
    setup_logging(settings)
    interval_s = _parse_interval(interval)
    stop_at = _parse_until(until) if until else None
    if stop_at is not None and datetime.now().astimezone() >= stop_at:
        _fail(f"--until {until} is already in the past.")

    universe = _resolve_universe(settings, symbols)
    on_usage, cost_tally = _usage_recorder(settings, "execution")
    strat = _build_agent_strategy(settings, strategy, universe, on_usage=on_usage)
    engine = _paper_engine(settings)
    eval_store = storage_factory.default_evaluation_store(settings)

    stop_note = f" until {until}" if stop_at else ""
    limit_note = f", max {max_cycles} cycles" if max_cycles else ""
    console.print(
        f"[dim]Looping [bold]{strategy}[/] every {interval_s}s{stop_note}{limit_note} "
        "(paper only). Press Ctrl-C to stop.[/]"
    )

    cycles = 0
    try:
        with _build_client(settings) as client:

            def source(symbol: str) -> market_data.Quote:
                return market_data.get_quote(client, symbol)

            kill_switch = safety.KillSwitch(settings.kill_switch_path)
            while True:
                if kill_switch.is_engaged():
                    console.print("[bold red]Kill switch engaged - halting agent loop.[/]")
                    break
                if stop_at is not None and datetime.now().astimezone() >= stop_at:
                    break
                cycles += 1
                before = cost_tally[0]
                try:
                    report = agent.AgentRunner(strat, engine, source).run_cycle()
                except (oauth.OAuthError, api.ApiError, llm_strategy.LLMError) as exc:
                    console.print(f"[red]Cycle {cycles} failed:[/] {exc}")
                    break
                eval_store.record_cycle(report)
                valuation = report.valuation
                cycle_cost = cost_tally[0] - before
                console.print(
                    f"[dim]{datetime.now():%H:%M:%S}[/] cycle {cycles}: "
                    f"filled {report.num_filled}/rej {report.num_rejected} | "
                    f"{_money(valuation.total_value)} "
                    f"({valuation.total_return_pct:+.2f}%) | "
                    f"cost {_cost(cycle_cost)} (cum {_cost(cost_tally[0])})"
                )
                if max_cycles and cycles >= max_cycles:
                    break
                if not _sleep_until(interval_s, stop_at):
                    break
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped by user.[/]")
    except (oauth.OAuthError, api.ApiError) as exc:
        _fail(exc)

    console.print(
        f"[green]Loop finished[/] after {cycles} cycle(s). "
        f"API spend this run: {_cost(cost_tally[0])}. "
        "See 'schwab-trader agent report'."
    )


def _sleep_until(interval_s: int, stop_at: datetime | None) -> bool:
    """Sleep up to ``interval_s`` in 1s steps; return False if ``stop_at`` passed."""
    for _ in range(interval_s):
        if stop_at is not None and datetime.now().astimezone() >= stop_at:
            return False
        time.sleep(1)
    return True


def _render_eval(
    store: evaluation.EvaluationStore,
    limit: int,
    *,
    title: str,
    extra_rows: list[tuple[str, str]] | None = None,
    empty_hint: str = "No cycles recorded yet.",
) -> None:
    """Render an evaluation store's scorecard + recent cycles."""
    summary = store.summary()
    if summary.cycles == 0:
        console.print(f"[dim]{empty_hint}[/]")
        return

    scorecard = Table(title=title, show_header=False)
    scorecard.add_column("Field", style="cyan", no_wrap=True)
    scorecard.add_column("Value", justify="right")
    scorecard.add_row("cycles", str(summary.cycles))
    scorecard.add_row("trades_filled", str(summary.trades_filled))
    scorecard.add_row("starting_cash", _money(summary.starting_cash))
    scorecard.add_row("latest_value", _money(summary.latest_value))
    scorecard.add_row("total_return", f"{summary.total_return_pct:.2f}%")
    scorecard.add_row("max_drawdown", f"-{summary.max_drawdown_pct:.2f}%")
    scorecard.add_row("sharpe", str(summary.sharpe) if summary.sharpe is not None else "n/a")
    scorecard.add_row("realized_pnl", _money(summary.realized_pnl))
    for field, value in extra_rows or []:
        scorecard.add_row(field, value)
    if summary.first_ts and summary.last_ts:
        scorecard.add_row(
            "first_cycle", summary.first_ts.astimezone().isoformat(timespec="seconds")
        )
        scorecard.add_row("last_cycle", summary.last_ts.astimezone().isoformat(timespec="seconds"))
    console.print(scorecard)

    table = Table(title="Recent cycles", show_header=True)
    table.add_column("id", justify="right", style="cyan")
    table.add_column("ts")
    table.add_column("strategy")
    table.add_column("filled", justify="right")
    table.add_column("rejected", justify="right")
    table.add_column("total_value", justify="right")
    table.add_column("return", justify="right")
    for cycle in store.recent_cycles(limit):
        table.add_row(
            str(cycle.id),
            cycle.ts.astimezone().strftime("%m-%d %H:%M"),
            cycle.strategy,
            str(cycle.num_filled),
            str(cycle.num_rejected),
            _money(cycle.total_value),
            f"{cycle.return_pct:.2f}%",
        )
    console.print(table)


@agent_app.command("report")
def agent_report(
    limit: int = typer.Option(10, "--limit", min=1, help="How many recent cycles to show."),
) -> None:
    """Show the agent's performance scorecard and recent cycles (paper)."""
    settings = get_settings()
    setup_logging(settings)
    store = storage_factory.default_evaluation_store(settings)
    spend = storage_factory.usage_store(settings).summary()
    extra = (
        [("api_calls", str(spend.calls)), ("api_spend", _cost(spend.total_cost))]
        if spend.calls > 0
        else None
    )
    _render_eval(
        store,
        limit,
        title="Agent performance (paper)",
        extra_rows=extra,
        empty_hint="No agent cycles recorded yet. Run 'schwab-trader agent run'.",
    )


@agent_app.command("autopilot")
def agent_autopilot(
    symbols: str = typer.Option("", "--symbols", help="Universe override."),
    from_file: list[str] = typer.Option(
        None, "--from-file", help="Docs to ground research (repeatable)."
    ),
    do_research: bool = typer.Option(
        True, "--research/--no-research", help="(Re)research the spec before trading."
    ),
    web_search: bool = typer.Option(
        True, "--web-search/--no-web-search", help="Let research search the web."
    ),
    interval: str = typer.Option("30m", "--interval", help="Time between trade cycles."),
    until: str = typer.Option("16:00", "--until", help="Stop trading at this local time."),
    max_cycles: int = typer.Option(
        0, "--max-cycles", min=0, help="Cap trade cycles (0 = until close)."
    ),
) -> None:
    """One autonomous session: (re)research the strategy, then trade until the close.

    Scheduled daily, this is the full research -> trade -> evaluate -> adapt loop.
    It drives the default agent sleeve. Note: research updates the shared strategy
    spec, so don't run this during a frozen sleeve comparison (it would un-freeze
    the 'llm' sleeve).
    """
    settings = get_settings()
    setup_logging(settings)
    interval_s = _parse_interval(interval)
    stop_at = _parse_until(until) if until else None
    universe = _resolve_universe(settings, symbols)

    documents: list[str] = []
    for path_str in from_file or []:
        try:
            documents.append(Path(path_str).read_text(encoding="utf-8"))
        except OSError as exc:
            _fail(f"Could not read source file '{path_str}': {exc}")

    research_on_usage, research_cost = _usage_recorder(settings, "research")
    exec_on_usage, exec_cost = _usage_recorder(settings, "execution")

    if do_research:
        console.print("[dim]Autopilot: researching the strategy...[/]")
        try:
            spec, spec_id, missing = _execute_research(
                settings,
                universe=universe,
                documents=documents,
                model=settings.llm_research_model,
                use_web_search=web_search,
                on_usage=research_on_usage,
            )
        except research.AnthropicUnavailable as exc:
            _fail(exc)
        except (oauth.OAuthError, api.ApiError, llm_strategy.LLMError) as exc:
            _fail(exc)
        if missing:
            console.print(f"[dim]No quote for: {', '.join(missing)}[/]")
        console.print(
            f"[green]Spec #{spec_id}[/]: {spec.market_regime} "
            f"(research cost {_cost(research_cost[0])})."
        )

    strat = _build_agent_strategy(
        settings, llm_strategy.LLM_STRATEGY_NAME, universe, on_usage=exec_on_usage
    )
    engine = _paper_engine(settings)
    eval_store = storage_factory.default_evaluation_store(settings)
    console.print(f"[dim]Autopilot: trading until {until} (Ctrl-C to stop)...[/]")

    cycles = 0
    try:
        with _build_client(settings) as client:

            def source(symbol: str) -> market_data.Quote:
                return market_data.get_quote(client, symbol)

            kill_switch = safety.KillSwitch(settings.kill_switch_path)
            while True:
                if kill_switch.is_engaged():
                    console.print("[bold red]Kill switch engaged - halting autopilot.[/]")
                    break
                cycles += 1
                before = exec_cost[0]
                try:
                    report = agent.AgentRunner(strat, engine, source).run_cycle()
                except (oauth.OAuthError, api.ApiError, llm_strategy.LLMError) as exc:
                    console.print(f"[red]Cycle {cycles} failed:[/] {exc}")
                    break
                eval_store.record_cycle(report)
                valuation = report.valuation
                console.print(
                    f"[dim]{datetime.now():%H:%M:%S}[/] cycle {cycles}: "
                    f"filled {report.num_filled}/rej {report.num_rejected} | "
                    f"{_money(valuation.total_value)} ({valuation.total_return_pct:+.2f}%) | "
                    f"cost {_cost(exec_cost[0] - before)}"
                )
                if max_cycles and cycles >= max_cycles:
                    break
                if stop_at is not None and datetime.now().astimezone() >= stop_at:
                    break
                if not _sleep_until(interval_s, stop_at):
                    break
    except KeyboardInterrupt:
        console.print("\n[yellow]Autopilot stopped by user.[/]")
    except (oauth.OAuthError, api.ApiError) as exc:
        _fail(exc)

    total = research_cost[0] + exec_cost[0]
    console.print(
        f"[green]Autopilot session done[/]: {cycles} cycle(s), total cost {_cost(total)}. "
        "See 'schwab-trader agent report'."
    )


# Strategies allowed on the live path (llm and intraday are intentionally excluded).
_LIVE_STRATEGIES = (
    "buy-hold",
    "hold",
    "momentum",
    "trend",
    "mean-reversion",
    "low-vol",
    "value-momentum",
    "fundamental",
    "post-earnings-drift",
)


def _validation_factor(strategy: str, factor: str) -> str:
    """Only factor-driven strategies include ``--factor`` in their identity."""
    if strategy in (agent.VALUE_MOMENTUM_NAME, agent.FUNDAMENTAL_STRATEGY_NAME):
        return factor.strip().lower()
    return ""


def _validation_fingerprint(
    settings: Settings, strategy: str, universe: list[str], factor: str
) -> str:
    return promotion.configuration_fingerprint(
        strategy=strategy,
        symbols=universe,
        factor=_validation_factor(strategy, factor),
        max_positions=settings.agent_max_positions,
        max_position_fraction=settings.agent_max_position_fraction,
        benchmark="SPY",
        settlement_t1=True,
        leverage=Decimal("1"),
    )


def _require_runtime_validation(
    settings: Settings,
    *,
    strategy: str,
    universe: list[str],
    universe_label: str,
    factor: str,
) -> tuple[bool, str]:
    """Apply the full quality/freshness/provenance/compatibility gate."""
    return promotion.require_validated(
        storage_factory.promotion_store(settings),
        strategy,
        universe_label,
        max_age_days=settings.promotion_max_age_days,
        expected_configuration_fingerprint=_validation_fingerprint(
            settings, strategy, universe, factor
        ),
        active_code_revision=promotion.current_code_revision(),
    )


def _assess_for_active_settings(
    settings: Settings,
    verdict: promotion.PromotionVerdict,
    *,
    active_code_revision: str,
) -> promotion.ValidationAssessment:
    manifest = verdict.manifest
    active_symbols = _resolve_universe(
        settings, "" if verdict.universe == "default" else verdict.universe
    )
    fingerprint = (
        _validation_fingerprint(
            settings,
            verdict.strategy,
            active_symbols,
            manifest.factor,
        )
        if manifest is not None
        else None
    )
    return promotion.assess_verdict(
        verdict,
        max_age_days=settings.promotion_max_age_days,
        expected_configuration_fingerprint=fingerprint,
        active_code_revision=active_code_revision,
    )


def _build_live_strategy(
    settings: Settings,
    strategy: str,
    universe: list[str],
    factor: str,
    client: api.SchwabClient,
    cache: history_cache.HistoryCache,
) -> agent.Strategy:
    """Build a rule strategy for live trading (history/EDGAR wired in as needed)."""
    if strategy in agent.HISTORY_STRATEGY_NAMES or strategy == agent.VALUE_MOMENTUM_NAME:
        store = None
        if strategy == agent.VALUE_MOMENTUM_NAME:
            store = storage_factory.sec_store(settings)
            if store.total_facts() == 0:
                _fail("value-momentum needs EDGAR data. Run 'schwab-trader edgar fetch' first.")
        return _build_history_strategy(
            strategy,
            client,
            universe,
            cache=cache,
            max_positions=settings.agent_max_positions,
            max_position_fraction=settings.agent_max_position_fraction,
            store=store,
            factor=factor,
        )
    if strategy == agent.FUNDAMENTAL_STRATEGY_NAME:
        store = storage_factory.sec_store(settings)
        if store.total_facts() == 0:
            _fail("fundamental needs EDGAR data. Run 'schwab-trader edgar fetch' first.")
        return agent.FundamentalStrategy(
            universe,
            store=store,
            factor=factor,
            max_positions=settings.agent_max_positions,
            max_position_fraction=settings.agent_max_position_fraction,
        )
    if strategy == agent.POST_EARNINGS_DRIFT_NAME:
        store = storage_factory.sec_store(settings)
        if store.total_facts() == 0:
            _fail("post-earnings-drift needs EDGAR data. Run 'schwab-trader edgar fetch' first.")
        return agent.PostEarningsDriftStrategy(
            universe,
            store=store,
            max_positions=settings.agent_max_positions,
            max_position_fraction=settings.agent_max_position_fraction,
        )
    return agent.build_strategy(strategy, universe)


@agent_app.command("live")
def agent_live(
    strategy: str = typer.Option(..., "--strategy", help="A VALIDATED rule strategy to trade."),
    symbols: str = typer.Option(
        "", "--symbols", help="Universe (must match the validation verdict, e.g. large-cap)."
    ),
    factor: str = typer.Option(
        "earnings-yield", "--factor", help="Value factor for value-momentum/fundamental."
    ),
    max_orders: int = typer.Option(
        5, "--max-orders", min=1, help="Cap on orders routed this run (a safety valve)."
    ),
    override: bool = typer.Option(False, "--override", help="Override a duplicate block."),
) -> None:
    """Assisted LIVE trading from a validated strategy - each order runs the full gate.

    Generates the strategy's proposed orders against your REAL account and routes each
    through the same gated flow as 'order submit': risk checks, the config gates, the
    exact typed confirmation, a single submit, and audit. Fails closed - the strategy
    must be VALIDATED (see 'validate run'), the kill switch clear, and each order must
    pass the safety gate (capital cap, daily loss/trade limits, per-order notional).

    In dry-run / gated mode (the default) it PREVIEWS every order and submits nothing.
    This never removes human confirmation; it is assisted, not autonomous.
    """
    settings = get_settings()
    setup_logging(settings)
    account_hash = _require_account(settings)
    universe = _resolve_universe(settings, symbols)
    universe_label = symbols.strip() or "default"

    if strategy not in _LIVE_STRATEGIES:
        _fail(f"agent live supports: {', '.join(_LIVE_STRATEGIES)} (llm/intraday excluded).")
    if safety.KillSwitch(settings.kill_switch_path).is_engaged():
        _fail("Kill switch engaged. Clear with 'schwab-trader safety resume' before live trading.")
    ok, reason = _require_runtime_validation(
        settings,
        strategy=strategy,
        universe=universe,
        universe_label=universe_label,
        factor=factor,
    )
    if not ok:
        symbols_hint = symbols or "large-cap"
        _fail(
            f"Refusing live trading: {reason}. Validate first with "
            f"'schwab-trader validate run --strategy {strategy} --symbols {symbols_hint}'."
        )

    hist_cache = history_cache.HistoryCache(settings.history_cache_dir)
    try:
        with _build_client(settings) as client:
            balances = accounts_mod.get_balances(client, account_hash)
            live_positions = accounts_mod.get_positions(client, account_hash)
            strat = _build_live_strategy(settings, strategy, universe, factor, client, hist_cache)
            quotes: dict[str, market_data.Quote] = {}
            for symbol in strat.universe:
                try:
                    quotes[symbol] = market_data.get_quote(client, symbol)
                except market_data.QuoteError:
                    continue
    except (oauth.OAuthError, api.ApiError) as exc:
        _fail(exc)

    cash = balances.cash_available_for_trading or balances.total_cash or Decimal(0)
    deployed = balances.long_market_value or Decimal(0)
    equity = balances.liquidation_value or (cash + deployed)
    positions = {p.symbol: int(p.long_quantity) for p in live_positions if p.long_quantity > 0}

    context = agent.MarketContext(
        now=datetime.now(UTC), cash=cash, positions=positions, quotes=quotes, equity=equity
    )
    proposals = strat.decide(context)
    if not proposals:
        console.print("[dim]The strategy proposes no orders right now.[/]")
        return

    is_live = settings.trading_enabled and not settings.dry_run
    mode = "[bold red]LIVE[/]" if is_live else "[green]DRY-RUN (preview only)[/]"
    console.print(
        f"{mode} - [bold]{strategy}[/] on {universe_label}: {len(proposals)} proposal(s), "
        f"account {settings.masked_account_tail()}."
    )
    console.print(
        f"[dim]Live cash {_money(cash)}, deployed {_money(deployed)}, equity {_money(equity)}. "
        "Each live order still requires its exact typed confirmation.[/]"
    )

    gate = _safety_gate(settings)
    submitted = 0
    for proposal in proposals[:max_orders]:
        request = proposal.request
        notional = request.limit_price * request.quantity
        decision = gate.check(order_notional=notional, deployed=deployed, equity=equity)
        if not decision.allowed:
            console.print(f"[yellow]SKIPPED[/] {request.describe()}: {decision.reason}")
            continue
        console.print(f"\n[cyan]Strategy rationale:[/] {proposal.rationale}")
        if _run_gated_order(
            settings,
            account_hash,
            request,
            command="agent live",
            confirm_phrase=request.confirmation_phrase,
            action=orders.submit_order,
            override=override,
        ):
            submitted += 1
            deployed += notional
    tail = "" if is_live else " (dry-run: nothing was placed)"
    console.print(f"\n[dim]{submitted} live order(s) placed this run{tail}.[/]")


def _approval_batch_message(
    settings: Settings,
    items: list[tuple[OrderRequest, str, str]],
    ttl: timedelta,
) -> notify.NotifyMessage:
    """Compose ONE approval email listing every proposed order (request, rationale, token)."""
    minutes = int(ttl.total_seconds() // 60)
    tail = settings.masked_account_tail()
    n = len(items)

    # Plain-text body (fallback).
    text_blocks: list[str] = []
    for i, (request, rationale, token) in enumerate(items, start=1):
        text_blocks.append(
            f"{i}) {request.describe()}\n"
            f"   est. max notional: ${request.estimated_notional:,.2f}\n"
            f"   rationale: {rationale}\n"
            f"   approve:   schwab-trader agent approve {token}"
        )
    body = (
        f"{n} proposed order(s) for account {tail}. "
        f"Each token is single-use and expires in {minutes} minutes.\n\n"
        + "\n\n".join(text_blocks)
        + "\n\nApproval still runs all risk checks, the config gates, duplicate detection, and "
        "the kill switch - it replaces only the typed confirmation phrase. If you did not expect "
        "these, do nothing (they expire) or halt everything: schwab-trader safety kill."
    )

    # HTML body: one section per order, each with its own approve command.
    sections: list[str] = []
    for i, (request, rationale, token) in enumerate(items, start=1):
        border = "" if i == n else f"border-bottom:1px solid {emailfmt.LINE};"
        section = (
            f'<div style="padding:14px 0;{border}">'
            + emailfmt.kv_table(
                [
                    (f"Order {i} of {n}", f"<strong>{emailfmt.esc(request.describe())}</strong>"),
                    ("Est. max notional", emailfmt.esc(f"${request.estimated_notional:,.2f}")),
                    ("Rationale", emailfmt.esc(rationale)),
                ]
            )
            + emailfmt.button_row("Approve:", f"schwab-trader agent approve {token}")
            + "</div>"
        )
        sections.append(section)
    inner = "".join(sections) + emailfmt.note(
        "Each token is single-use. Approval still runs all risk checks, the config gates, "
        "duplicate detection, and the kill switch - it replaces only the typed confirmation "
        "phrase. If you did not expect these, do nothing (they expire) or halt everything with "
        "<code>schwab-trader safety kill</code>."
    )
    html = emailfmt.document(
        heading=f"{n} order proposal(s) - approval needed",
        subheading=f"{tail} - expires in {minutes} minutes",
        inner_html=inner,
        footer="You still authorize every live order. Nothing was placed.",
    )
    return notify.NotifyMessage(
        subject=f"{n} order proposal(s) to review - {tail}",
        body=body,
        html_body=html,
        category="approval",
    )


@agent_app.command("propose")
def agent_propose(
    strategy: str = typer.Option(..., "--strategy", help="A VALIDATED rule strategy to trade."),
    symbols: str = typer.Option(
        "", "--symbols", help="Universe (must match the validation verdict, e.g. large-cap)."
    ),
    factor: str = typer.Option(
        "earnings-yield", "--factor", help="Value factor for value-momentum/fundamental."
    ),
    max_orders: int = typer.Option(
        5, "--max-orders", min=1, help="Cap on proposals emitted this run (a safety valve)."
    ),
) -> None:
    """Generate orders from a validated strategy and NOTIFY for out-of-band approval.

    Unattended-friendly (schedule it): previews each proposed order through the full
    risk + safety gate, submits NOTHING, issues a single-use time-boxed approval token
    per passing order, and emails all of them together in one notification. Approve
    later with 'agent approve <token>', which runs the same gated live submit.

    Fails closed: requires a VALIDATED strategy (see 'validate run'), a clear kill
    switch, and a configured notification channel (SCHWAB_SMTP_* / SCHWAB_NOTIFY_*).
    Never places an order itself and never removes human approval.
    """
    settings = get_settings()
    setup_logging(settings)
    account_hash = _require_account(settings)
    universe = _resolve_universe(settings, symbols)
    universe_label = symbols.strip() or "default"

    if strategy not in _LIVE_STRATEGIES:
        _fail(f"agent propose supports: {', '.join(_LIVE_STRATEGIES)} (llm/intraday excluded).")
    if not settings.has_smtp:
        _fail(
            "No notification channel configured. Set SCHWAB_SMTP_* / SCHWAB_NOTIFY_* "
            "so proposed orders can be delivered for approval."
        )
    if safety.KillSwitch(settings.kill_switch_path).is_engaged():
        _fail("Kill switch engaged. Clear with 'schwab-trader safety resume' before proposing.")
    ok, reason = _require_runtime_validation(
        settings,
        strategy=strategy,
        universe=universe,
        universe_label=universe_label,
        factor=factor,
    )
    if not ok:
        symbols_hint = symbols or "large-cap"
        _fail(
            f"Refusing to propose: {reason}. Validate first with "
            f"'schwab-trader validate run --strategy {strategy} --symbols {symbols_hint}'."
        )

    hist_cache = history_cache.HistoryCache(settings.history_cache_dir)
    try:
        with _build_client(settings) as client:
            balances = accounts_mod.get_balances(client, account_hash)
            live_positions = accounts_mod.get_positions(client, account_hash)
            strat = _build_live_strategy(settings, strategy, universe, factor, client, hist_cache)
            quotes: dict[str, market_data.Quote] = {}
            for symbol in strat.universe:
                try:
                    quotes[symbol] = market_data.get_quote(client, symbol)
                except market_data.QuoteError:
                    continue
    except (oauth.OAuthError, api.ApiError) as exc:
        _fail(exc)

    cash = balances.cash_available_for_trading or balances.total_cash or Decimal(0)
    deployed = balances.long_market_value or Decimal(0)
    equity = balances.liquidation_value or (cash + deployed)
    positions = {p.symbol: int(p.long_quantity) for p in live_positions if p.long_quantity > 0}

    context = agent.MarketContext(
        now=datetime.now(UTC), cash=cash, positions=positions, quotes=quotes, equity=equity
    )
    proposals = strat.decide(context)
    if not proposals:
        console.print("[dim]The strategy proposes no orders right now.[/]")
        return

    console.print(
        f"[bold]{strategy}[/] on {universe_label}: {len(proposals)} proposal(s), "
        f"account {settings.masked_account_tail()}. Previewing and notifying (nothing is placed)."
    )

    store = approval.ApprovalStore(settings.approval_db_path)
    notifier = notify.build_notifier(settings)
    gate = _safety_gate(settings)
    ttl = timedelta(minutes=settings.approval_ttl_minutes)
    audit_store = state.StateStore(settings.state_db_path)
    account_tail = settings.masked_account_tail()
    issued_items: list[tuple[OrderRequest, str, str]] = []

    tripped_notified = False
    for proposal in proposals[:max_orders]:
        request = proposal.request
        notional = request.limit_price * request.quantity
        decision = gate.check(order_notional=notional, deployed=deployed, equity=equity)
        if not decision.allowed:
            reason = decision.reason or "blocked by safety gate"
            # The gate may auto-engage the kill switch (e.g. daily-loss breach); notify once.
            if not tripped_notified and "kill switch" in reason.lower():
                events.emit(
                    notifier,
                    events.kill_switch_tripped_message(reason=reason, account_tail=account_tail),
                )
                tripped_notified = True
            console.print(f"[yellow]SKIPPED[/] {request.describe()}: {reason}")
            continue
        # Preview risk against fresh account data; only notify orders that pass.
        try:
            with _build_client(settings) as client:
                _, report = _evaluate_live(client, settings, account_hash, request)
        except (oauth.OAuthError, api.ApiError, market_data.QuoteError) as exc:
            console.print(f"[yellow]SKIPPED[/] {request.describe()}: {exc}")
            continue
        if not report.passed:
            fails = "; ".join(check.name for check in report.failures)
            console.print(f"[yellow]SKIPPED[/] {request.describe()}: risk failed ({fails})")
            continue

        token = store.issue(
            account_hash=account_hash, request=request, ttl=ttl, rationale=proposal.rationale
        )
        issued_items.append((request, proposal.rationale, token))
        deployed += notional
        audit_store.append_audit(
            command="agent propose",
            event="proposed",
            account_tail=account_tail,
            detail=request.describe(),
        )
        console.print(
            f"[green]PROPOSED[/] {request.describe()} - token issued "
            f"(expires in {settings.approval_ttl_minutes}m)."
        )

    if not issued_items:
        console.print("\n[dim]No proposals passed the gate; no email sent.[/]")
        return

    # One consolidated email with every passing proposal (each keeps its own token).
    try:
        notifier.send(_approval_batch_message(settings, issued_items, ttl))
    except notify.NotifyError as exc:
        console.print(f"[red]Notify failed[/] for the proposal email: {exc}")
        audit_store.append_audit(
            command="agent propose",
            event="notify_failed",
            account_tail=account_tail,
            detail=f"{len(issued_items)} proposal(s)",
        )
        return
    console.print(
        f"\n[dim]{len(issued_items)} proposal(s) sent in one email. "
        "Approve each with 'schwab-trader agent approve <token>'.[/]"
    )


@agent_app.command("approve")
def agent_approve(
    token: str = typer.Argument(..., help="The single-use approval token from the notification."),
    override: bool = typer.Option(False, "--override", help="Override a duplicate block."),
) -> None:
    """Approve a previously proposed order (from 'agent propose') and submit it LIVE.

    Consumes the single-use token (validates it is unexpired, unused, and matches this
    exact order + account), then routes the order through the SAME gated flow as
    'order submit': risk re-check on fresh data, the config gates, kill switch,
    duplicate detection, a single submit, and audit. The token replaces only the typed
    phrase; every other gate still applies. In dry-run/gated mode it previews and the
    token is NOT consumed, so it stays valid until you open the config gates.
    """
    settings = get_settings()
    setup_logging(settings)
    account_hash = _require_account(settings)

    store = approval.ApprovalStore(settings.approval_db_path)
    record = store.get(token)
    if record is None:
        _fail("Unknown approval token. Use the exact value from your notification.")
    if record.status != approval.STATUS_PENDING:
        _fail("This approval token has already been used.")
    if record.is_expired():
        _fail("This approval token has expired. Re-run 'agent propose' for a fresh one.")

    request = record.to_request()
    console.print(f"[cyan]Approving proposal #{record.id}:[/] {request.describe()}")
    if record.rationale:
        console.print(f"[dim]Rationale: {record.rationale}[/]")
    _run_gated_order(
        settings,
        account_hash,
        request,
        command="agent approve",
        confirm_phrase=request.confirmation_phrase,
        action=orders.submit_order,
        override=override,
        approval_token=token,
        approval_store=store,
    )


# --- Research (Claude writes the strategy spec; paper only) ------------------


def _render_spec(spec: research.StrategySpec) -> None:
    """Print a strategy spec as a readable panel."""
    console.print(
        f"[bold]Strategy spec[/] - {spec.created_at.astimezone():%Y-%m-%d %H:%M} "
        f"([cyan]{spec.model}[/])"
    )
    console.print(f"  Market regime: {spec.market_regime}")
    console.print(f"  Thesis: {spec.thesis}")
    if spec.focus_symbols:
        console.print(f"  Focus: {', '.join(spec.focus_symbols)}")
    if spec.avoid_symbols:
        console.print(f"  Avoid: {', '.join(spec.avoid_symbols)}")
    console.print(
        f"  Sizing: up to {spec.max_positions} positions, "
        f"max {spec.max_position_fraction * 100:.0f}% of the sleeve per position."
    )
    if spec.rules:
        console.print("  Rules:")
        for i, rule in enumerate(spec.rules, start=1):
            console.print(f"    {i}. {rule}")


def _performance_feedback(settings: Settings) -> str | None:
    """Summarize how the latest spec has performed, to feed the next research pass."""
    prev = storage_factory.research_store(settings).latest()
    summary = storage_factory.default_evaluation_store(settings).summary()
    if prev is None or summary.cycles == 0:
        return None
    sharpe = f", Sharpe {summary.sharpe}" if summary.sharpe is not None else ""
    return (
        f'previous thesis was "{prev.thesis}" (regime: {prev.market_regime}); '
        f"since then the sleeve ran {summary.cycles} cycles with "
        f"total return {summary.total_return_pct:+.2f}%, "
        f"max drawdown -{summary.max_drawdown_pct:.2f}%{sharpe}, "
        f"{summary.trades_filled} fills. Keep what worked; change what did not."
    )


def _execute_research(
    settings: Settings,
    *,
    universe: list[str],
    documents: list[str],
    model: str,
    use_web_search: bool,
    on_usage: llm_strategy.UsageSink,
    use_feedback: bool = True,
) -> tuple[research.StrategySpec, int, list[str]]:
    """Run one research pass against the default sleeve; persist and return the spec.

    Returns ``(spec, spec_id, missing_symbols)``. Raises ``AnthropicUnavailable``,
    ``OAuthError``, ``ApiError``, or ``LLMError`` to the caller. Shared by
    ``research run`` and ``agent autopilot``.
    """
    researcher = research.build_anthropic_researcher(
        api_key=settings.anthropic_api_key.get_secret_value(),
        model=model,
        use_web_search=use_web_search,
        on_usage=on_usage,
    )
    engine = _paper_engine(settings)
    positions = {p.symbol: p.quantity for p in engine.positions()}
    quotes: dict[str, market_data.Quote] = {}
    schwab_fundamentals: dict[str, market_data.Fundamentals] = {}
    missing: list[str] = []
    with _build_client(settings) as client:
        for symbol in universe:
            try:
                quotes[symbol] = market_data.get_quote(client, symbol)
            except market_data.QuoteError:
                missing.append(symbol)
            fund = market_data.get_fundamentals(client, symbol)
            if fund is not None:
                schwab_fundamentals[symbol] = fund

    macro: str | None = None
    if settings.has_fred_key:
        with contextlib.suppress(fred.FredUnavailable, httpx.HTTPError):
            macro = fred.get_macro(settings.fred_api_key.get_secret_value()).summary()

    # Point-in-time SEC EDGAR ratios (TTM), priced off the live quotes we just fetched.
    now = datetime.now(UTC)
    edgar = storage_factory.sec_store(settings)
    ratios: dict[str, fundamentals.Ratios] = {}
    if edgar.total_facts() > 0:
        for symbol in universe:
            quote = quotes.get(symbol)
            price = (quote.mark or quote.last) if quote is not None else None
            if price is None or price <= 0:
                continue
            computed = fundamentals.ratios(edgar, symbol, now.date(), price)
            if computed.pe_ttm is not None or computed.book_to_market is not None:
                ratios[symbol] = computed

    # Adaptive feedback: how has the PREVIOUS spec performed on the agent sleeve?
    feedback = _performance_feedback(settings) if use_feedback else None

    context = research.ResearchContext(
        now=now,
        cash=engine.account().cash,
        positions=positions,
        quotes=quotes,
        universe=universe,
        source_documents=documents,
        fundamentals=schwab_fundamentals,
        ratios=ratios,
        macro=macro,
        performance_feedback=feedback,
    )
    spec = researcher(context)
    spec_id = storage_factory.research_store(settings).record(spec)
    return spec, spec_id, missing


@research_app.command("run")
def research_run(
    symbols: str = typer.Option(
        "", "--symbols", help="Comma-separated universe override (default: config/built-in)."
    ),
    from_file: list[str] = typer.Option(
        None,
        "--from-file",
        help="Markdown research/strategy file(s) to ground the spec (repeatable).",
    ),
    web_search: bool = typer.Option(
        True, "--web-search/--no-web-search", help="Let Claude search the web for recent news."
    ),
    adaptive: bool = typer.Option(
        True, "--adaptive/--no-adaptive", help="Feed the prior spec's performance in (learn)."
    ),
    model: str = typer.Option("", "--model", help="Override the research model."),
) -> None:
    """Have Claude study the market and write a strategy spec (paper sleeve)."""
    settings = get_settings()
    setup_logging(settings)
    universe = _resolve_universe(settings, symbols)
    research_model = model or settings.llm_research_model

    documents: list[str] = []
    for path_str in from_file or []:
        try:
            documents.append(Path(path_str).read_text(encoding="utf-8"))
        except OSError as exc:
            _fail(f"Could not read source file '{path_str}': {exc}")

    on_usage, cost_tally = _usage_recorder(settings, "research")
    search_note = " with web search" if web_search else ""
    sources_note = f" grounded in {len(documents)} document(s)" if documents else ""
    console.print(
        f"[dim]Researching {len(universe)} symbols with [bold]{research_model}[/]{search_note}"
        f"{sources_note} (paper sleeve; no orders placed).[/]"
    )
    try:
        spec, spec_id, missing = _execute_research(
            settings,
            universe=universe,
            documents=documents,
            model=research_model,
            use_web_search=web_search,
            on_usage=on_usage,
            use_feedback=adaptive,
        )
    except research.AnthropicUnavailable as exc:
        _fail(exc)
    except (oauth.OAuthError, api.ApiError, llm_strategy.LLMError) as exc:
        _fail(exc)

    if missing:
        console.print(f"[dim]No quote for: {', '.join(missing)}[/]")
    _render_spec(spec)
    if cost_tally[0] > 0:
        console.print(f"[dim]API cost for this research pass: {_cost(cost_tally[0])}.[/]")
    console.print(
        f"\n[green]Recorded spec #{spec_id}.[/] "
        "'schwab-trader agent run --strategy llm' will now follow it."
    )


@research_app.command("show")
def research_show() -> None:
    """Show the current strategy spec the agent would follow."""
    settings = get_settings()
    setup_logging(settings)
    spec = storage_factory.research_store(settings).latest()
    if spec is None:
        console.print(
            "[dim]No strategy spec yet. Run 'schwab-trader research run' to create one.[/]"
        )
        return
    _render_spec(spec)


@research_app.command("history")
def research_history(
    limit: int = typer.Option(10, "--limit", min=1, help="How many specs to show."),
) -> None:
    """List recent strategy specs (most recent first)."""
    settings = get_settings()
    setup_logging(settings)
    specs = storage_factory.research_store(settings).recent(limit)
    if not specs:
        console.print("[dim]No strategy specs recorded yet.[/]")
        return
    table = Table(title="Strategy specs", show_header=True)
    table.add_column("created", no_wrap=True)
    table.add_column("model", style="cyan")
    table.add_column("regime")
    table.add_column("focus")
    table.add_column("sizing", justify="right")
    for spec in specs:
        table.add_row(
            spec.created_at.astimezone().strftime("%m-%d %H:%M"),
            spec.model,
            spec.market_regime,
            ", ".join(spec.focus_symbols) or "-",
            f"{spec.max_positions} @ {spec.max_position_fraction * 100:.0f}%",
        )
    console.print(table)


# --- Backtest (historical replay; rule-based strategies) --------------------

# Deterministic rule strategies for the historical backtest/validate path. The
# strategy_registry is the single source; this tuple is derived from it.
_RULE_STRATEGIES = tuple(strategy_registry.rule_backtest_strategy_names())
# Extra trailing history a momentum backtest needs for its ~253-day lookback.
_MOMENTUM_LOOKBACK = 260


def _resolve_rule_universe(
    settings: Settings, strategy: str, symbols: str, benchmark: str
) -> list[str]:
    """Resolve symbols, enforcing version-0 tactical research boundaries."""
    if strategy != agent.TacticalRegimeStrategy.name:
        return _resolve_universe(settings, symbols)
    risk_asset = benchmark.strip().upper()
    if not risk_asset:
        _fail("The tactical strategy needs --benchmark (SPY for version 0).")
    # Both empty (backtest defaults) and large-cap (validate's generic default)
    # resolve to the benchmark for this deliberately single-asset first slice.
    if not symbols.strip() or symbols.strip().lower() == "large-cap":
        return [risk_asset]
    universe = _resolve_universe(settings, symbols)
    if universe != [risk_asset]:
        _fail(
            "Tactical version 0 is a single-asset router: --symbols must contain only "
            f"the benchmark ({risk_asset})."
        )
    return universe


def _needs_history(strategy: str) -> bool:
    """True when a strategy's signal needs a trailing price-history lookback."""
    return strategy in agent.HISTORY_STRATEGY_NAMES or strategy == agent.VALUE_MOMENTUM_NAME


def _needs_store(strategy: str) -> bool:
    """True when a strategy needs the SEC EDGAR store injected."""
    return strategy in (agent.VALUE_MOMENTUM_NAME, agent.POST_EARNINGS_DRIFT_NAME)


def _uses_bars_builder(strategy: str) -> bool:
    """True when the strategy is constructed via :func:`_history_strategy_from_bars`.

    Covers the history strategies, value-momentum, and post-earnings-drift (which needs
    the store but no lookback - it ignores the bars for its signal, but the replay still
    prices fills off them).
    """
    return _needs_history(strategy) or strategy == agent.POST_EARNINGS_DRIFT_NAME


@backtest_app.command("run")
def backtest_run(
    strategy: str = typer.Option("buy-hold", "--strategy", help="Rule-based strategy to replay."),
    symbols: str = typer.Option("", "--symbols", help="Comma-separated universe override."),
    days: int = typer.Option(180, "--days", min=2, help="Trading days of history to replay."),
    starting_cash: str = typer.Option("", "--starting-cash", help="Override the sleeve size."),
    benchmark: str = typer.Option("SPY", "--benchmark", help="Benchmark symbol ('' to skip)."),
    cost_bps: float = typer.Option(
        10.0, "--cost-bps", help="Round-trip transaction cost in bps (spread+slippage; 0=off)."
    ),
    settlement: bool = typer.Option(
        False, "--settlement/--no-settlement", help="Model T+1 settled cash (cash-account realism)."
    ),
    leverage: float = typer.Option(
        1.0, "--leverage", min=1.0, help="Buying-power multiplier (1 = cash; 2 = Reg T margin)."
    ),
    factor: str = typer.Option(
        "earnings-yield", "--factor", help="Value factor(s) for value-momentum (comma-separated)."
    ),
) -> None:
    """Replay a deterministic strategy over historical daily prices (no orders placed)."""
    settings = get_settings()
    setup_logging(settings)
    if strategy == llm_strategy.LLM_STRATEGY_NAME:
        _fail(
            "Backtest supports only rule-based strategies "
            f"({', '.join(_RULE_STRATEGIES)}); the 'llm' strategy is validated forward via paper."
        )
    if strategy not in _RULE_STRATEGIES:
        _fail(f"Unknown strategy '{strategy}'. Try: {', '.join(_RULE_STRATEGIES)}")
    universe = _resolve_rule_universe(settings, strategy, symbols, benchmark)
    needs_history = _needs_history(strategy)
    # History strategies need extra leading history for their lookback; only the
    # last `days` are measured (via window), so they stay comparable to the simpler
    # strategies.
    fetch_days = days + _MOMENTUM_LOOKBACK if needs_history else days

    try:
        cash = Decimal(starting_cash) if starting_cash else settings.paper_starting_cash
    except InvalidOperation:
        _fail(f"Invalid --starting-cash '{starting_cash}'.")
    if leverage > 1.0 and settlement:
        _fail("--settlement (cash account) and --leverage > 1 (margin) are mutually exclusive.")

    margin_note = f", {leverage:g}x margin" if leverage > 1.0 else ""
    console.print(
        f"[dim]Backtesting [bold]{strategy}[/] over {len(universe)} symbols, "
        f"~{days} trading days, starting {_money(cash)}, cost {cost_bps:.0f}bps{margin_note}.[/]"
    )

    bars: dict[str, list[market_data.Candle]] = {}
    benchmark_candles: list[market_data.Candle] = []
    hist_cache = history_cache.HistoryCache(settings.history_cache_dir)
    try:
        with _build_client(settings) as client:
            for symbol in universe:
                candles = hist_cache.get(client, symbol, days=fetch_days)
                if candles:
                    bars[symbol] = candles
            if benchmark:
                benchmark_candles = hist_cache.get(client, benchmark, days=fetch_days)
    except (oauth.OAuthError, api.ApiError) as exc:
        _fail(exc)

    if not bars:
        _fail("No historical data was returned for any symbol.")
    missing = [s for s in universe if s not in bars]
    if missing:
        console.print(f"[dim]No history for: {', '.join(missing)}[/]")

    if _uses_bars_builder(strategy):
        edgar_store = storage_factory.sec_store(settings) if _needs_store(strategy) else None
        strat: agent.Strategy = _history_strategy_from_bars(
            strategy,
            universe,
            bars,
            benchmark_candles,
            max_positions=settings.agent_max_positions,
            max_position_fraction=settings.agent_max_position_fraction,
            store=edgar_store,
            factor=factor,
        )
    else:
        strat = agent.build_strategy(strategy, universe)

    tmp_dir = Path(tempfile.mkdtemp(prefix="schwab-backtest-"))
    try:
        engine = paper.PaperEngine(
            tmp_dir / "bt.sqlite3",
            starting_cash=cash,
            settle_t1=settlement,
            leverage=Decimal(str(leverage)),
        )
        result = backtest.run_backtest(
            strat, bars, engine, window=days if needs_history else None, cost_bps=cost_bps
        )
    finally:
        with contextlib.suppress(OSError):
            (tmp_dir / "bt.sqlite3").unlink()
            tmp_dir.rmdir()

    table = Table(title=f"Backtest: {strategy}", show_header=False)
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Value", justify="right")
    if result.first_day and result.last_day:
        table.add_row(
            "period",
            f"{result.first_day:%Y-%m-%d} -> {result.last_day:%Y-%m-%d} ({result.days}d)",
        )
    table.add_row("start_value", _money(result.start_value))
    table.add_row("end_value", _money(result.end_value))
    table.add_row("total_return", f"{result.total_return_pct:+.2f}%")
    if result.cagr_pct is not None:
        table.add_row("annualized (CAGR)", f"{result.cagr_pct:+.2f}%")
    table.add_row("max_drawdown", f"-{result.max_drawdown_pct:.2f}%")
    table.add_row("sharpe", str(result.sharpe) if result.sharpe is not None else "n/a")
    table.add_row("calmar", str(result.calmar) if result.calmar is not None else "n/a")
    table.add_row("turnover", f"{result.turnover:.2f}x")
    table.add_row("trades", str(result.trades))
    if benchmark_candles:
        # Compare over the same measured window (momentum fetched extra lookback).
        window_candles = benchmark_candles[-days:] if needs_history else benchmark_candles
        bench = backtest.buy_hold_return_pct(window_candles)
        if bench is not None:
            table.add_row(f"{benchmark} buy-hold", f"{bench:+.2f}%")
            table.add_row("vs benchmark", f"{result.total_return_pct - bench:+.2f}%")
    console.print(table)

    gates = backtest.evaluate_gates(result)
    verdict = "[green]PASS[/]" if gates.passed else "[red]FAIL[/]"
    console.print(f"Promotion gates: {verdict}")
    for check in gates.checks:
        mark = "[green]ok[/]" if check.passed else "[red]x[/]"
        console.print(f"  {mark} {check.name} (got {check.detail})")
    console.print("[dim]Backtest is a historical simulation, not a forecast.[/]")


def _fetch_dividend_yields(client: api.SchwabClient, symbols: list[str]) -> dict[str, float]:
    """Current annual dividend yield per symbol (fraction, e.g. 0.013), from Schwab.

    Schwab's ``dividendYield`` is a percentage (1.3 = 1.3%), so it is divided by 100.
    This is the *current* yield used as a constant across history - an approximation,
    since Schwab price history carries no point-in-time ex-date dividend series.
    """
    yields: dict[str, float] = {}
    for symbol in symbols:
        try:
            fundamentals = market_data.get_fundamentals(client, symbol)
        except (oauth.OAuthError, api.ApiError):
            continue
        if fundamentals is not None and fundamentals.dividend_yield:
            yields[symbol] = fundamentals.dividend_yield / 100.0
    return yields


def _run_walkforward(
    settings: Settings,
    *,
    strategy: str,
    universe: list[str],
    window: int,
    step: int,
    folds: int,
    cost_bps: float,
    benchmark: str,
    cash: Decimal,
    settlement: bool,
    leverage: float,
    factor: str = "earnings-yield",
    dividends: bool = False,
) -> backtest.WalkForwardResult:
    """Fetch history and run a walk-forward. Shared by 'backtest walkforward' and 'validate'."""
    needs_history = _needs_history(strategy)
    lookback = _MOMENTUM_LOOKBACK if needs_history else 0
    fetch_days = window + step * (folds - 1) + lookback
    edgar_store = storage_factory.sec_store(settings) if _needs_store(strategy) else None

    bars: dict[str, list[market_data.Candle]] = {}
    benchmark_candles: list[market_data.Candle] = []
    dividend_yields: dict[str, float] = {}
    benchmark_yield = 0.0
    hist_cache = history_cache.HistoryCache(settings.history_cache_dir)
    try:
        with _build_client(settings) as client:
            for symbol in universe:
                candles = hist_cache.get(client, symbol, days=fetch_days)
                if candles:
                    bars[symbol] = candles
            if benchmark:
                benchmark_candles = hist_cache.get(client, benchmark, days=fetch_days)
            if dividends:
                dividend_yields = _fetch_dividend_yields(client, list(bars))
                if benchmark:
                    benchmark_yield = _fetch_dividend_yields(client, [benchmark]).get(
                        benchmark, 0.0
                    )
    except (oauth.OAuthError, api.ApiError) as exc:
        _fail(exc)
    if not bars:
        _fail("No historical data was returned for any symbol.")

    def build(
        fold_bars: dict[str, list[market_data.Candle]], fold_bench: list[market_data.Candle]
    ) -> agent.Strategy:
        if _uses_bars_builder(strategy):
            return _history_strategy_from_bars(
                strategy,
                universe,
                fold_bars,
                fold_bench,
                max_positions=settings.agent_max_positions,
                max_position_fraction=settings.agent_max_position_fraction,
                store=edgar_store,
                factor=factor,
            )
        return agent.build_strategy(strategy, universe)

    tmp_dir = Path(tempfile.mkdtemp(prefix="schwab-wf-"))
    counter = {"n": 0}

    def engine_factory() -> paper.PaperEngine:
        counter["n"] += 1
        return paper.PaperEngine(
            tmp_dir / f"wf-{counter['n']}.sqlite3",
            starting_cash=cash,
            settle_t1=settlement,
            leverage=Decimal(str(leverage)),
        )

    try:
        return backtest.walk_forward(
            build,
            engine_factory,
            bars,
            benchmark_candles,
            window=window,
            step=step,
            folds=folds,
            cost_bps=cost_bps,
            dividend_yields=dividend_yields or None,
            benchmark_yield=benchmark_yield,
        )
    finally:
        with contextlib.suppress(OSError):
            for file in tmp_dir.glob("*.sqlite3"):
                file.unlink()
            tmp_dir.rmdir()


@backtest_app.command("walkforward")
def backtest_walkforward(
    strategy: str = typer.Option("momentum", "--strategy", help="Rule-based strategy to replay."),
    symbols: str = typer.Option("", "--symbols", help="Comma-separated universe or a preset."),
    window: int = typer.Option(180, "--window", min=20, help="Trading days measured per fold."),
    step: int = typer.Option(90, "--step", min=5, help="Days between fold end-points."),
    folds: int = typer.Option(6, "--folds", min=2, help="Number of windows to test."),
    starting_cash: str = typer.Option("", "--starting-cash", help="Override the sleeve size."),
    benchmark: str = typer.Option("SPY", "--benchmark", help="Benchmark symbol ('' to skip)."),
    cost_bps: float = typer.Option(10.0, "--cost-bps", help="Round-trip cost in bps."),
    factor: str = typer.Option(
        "earnings-yield", "--factor", help="Value factor for value-momentum."
    ),
    settlement: bool = typer.Option(
        False, "--settlement/--no-settlement", help="Model T+1 settled cash."
    ),
    leverage: float = typer.Option(
        1.0, "--leverage", min=1.0, help="Buying-power multiplier (1 = cash; 2 = Reg T margin)."
    ),
    dividends: bool = typer.Option(
        False, "--dividends/--no-dividends", help="Total-return: add current dividend yields."
    ),
) -> None:
    """Backtest a strategy across several rolling windows to check it isn't one-window luck."""
    settings = get_settings()
    setup_logging(settings)
    if strategy == llm_strategy.LLM_STRATEGY_NAME or strategy not in _RULE_STRATEGIES:
        _fail(f"Walk-forward supports rule-based strategies: {', '.join(_RULE_STRATEGIES)}.")
    if leverage > 1.0 and settlement:
        _fail("--settlement (cash account) and --leverage > 1 (margin) are mutually exclusive.")
    universe = _resolve_rule_universe(settings, strategy, symbols, benchmark)
    try:
        cash = Decimal(starting_cash) if starting_cash else settings.paper_starting_cash
    except InvalidOperation:
        _fail(f"Invalid --starting-cash '{starting_cash}'.")

    console.print(
        f"[dim]Walk-forward [bold]{strategy}[/]: {folds} folds of {window}d "
        f"(step {step}d) over {len(universe)} symbols, cost {cost_bps:.0f}bps"
        f"{', total-return' if dividends else ''}.[/]"
    )
    wf = _run_walkforward(
        settings,
        strategy=strategy,
        universe=universe,
        window=window,
        step=step,
        folds=folds,
        cost_bps=cost_bps,
        benchmark=benchmark,
        cash=cash,
        settlement=settlement,
        leverage=leverage,
        factor=factor,
        dividends=dividends,
    )

    table = Table(title=f"Walk-forward: {strategy}", show_header=True)
    table.add_column("fold end", no_wrap=True)
    table.add_column("return", justify="right")
    table.add_column("sharpe", justify="right")
    table.add_column("max DD", justify="right")
    table.add_column("vs bench", justify="right")
    table.add_column("gate")
    for fold in wf.folds:
        excess = f"{fold.excess_pct:+.1f}%" if fold.excess_pct is not None else "-"
        table.add_row(
            fold.end_day.strftime("%Y-%m-%d"),
            f"{fold.return_pct:+.1f}%",
            str(fold.sharpe) if fold.sharpe is not None else "-",
            f"-{fold.max_drawdown_pct:.1f}%",
            excess,
            "[green]PASS[/]" if fold.passed else "[red]FAIL[/]",
        )
    console.print(table)

    summary = Table(title="Consistency", show_header=False)
    summary.add_column("Field", style="cyan", no_wrap=True)
    summary.add_column("Value", justify="right")
    summary.add_row("mean return", f"{wf.mean_return_pct:+.2f}%")
    summary.add_row("median return", f"{wf.median_return_pct:+.2f}%")
    summary.add_row("worst fold", f"{wf.worst_return_pct:+.2f}%")
    if wf.mean_excess_pct is not None:
        summary.add_row("mean vs benchmark", f"{wf.mean_excess_pct:+.2f}%")
    summary.add_row("folds passing gates", f"{wf.pass_rate * 100:.0f}%")
    console.print(summary)
    console.print(
        "[dim]Consistency across folds matters more than any single number. "
        "Survivorship-biased data; directional, not proof.[/]"
    )


@backtest_app.command("survivorship")
def backtest_survivorship(
    symbols: str = typer.Option("large-cap", "--symbols", help="Universe: a preset or CSV."),
    window: int = typer.Option(180, "--window", min=20, help="Trading days measured per fold."),
    step: int = typer.Option(90, "--step", min=5, help="Days between fold end-points."),
    folds: int = typer.Option(6, "--folds", min=2, help="Number of windows."),
) -> None:
    """Measure the survivorship-bias footprint of a universe over the validation folds.

    Fetches each name's earliest available bar (a listing-date proxy) and reports, per
    walk-forward fold, how many of today's survivor list were actually listed at the
    fold's window start. Names 'not yet listed' are the concrete backfill bias that the
    point-in-time walk-forward now removes. It cannot measure inclusion or delisting
    bias (those need point-in-time index membership and delisted price data).
    """
    settings = get_settings()
    setup_logging(settings)
    universe = _resolve_universe(settings, symbols)

    # Fetch as much history as Schwab will serve so first bars approximate listings.
    hist_cache = history_cache.HistoryCache(settings.history_cache_dir)
    bars: dict[str, list[market_data.Candle]] = {}
    try:
        with _build_client(settings) as client:
            for symbol in universe:
                candles = hist_cache.get(client, symbol, days=7300)
                if candles:
                    bars[symbol] = candles
    except (oauth.OAuthError, api.ApiError) as exc:
        _fail(exc)
    if not bars:
        _fail("No historical data was returned for any symbol.")

    firsts = survivorship.listing_dates(bars)
    all_days = sorted({c.date for candles in bars.values() for c in candles})

    table = Table(title=f"Point-in-time coverage: {symbols}", show_header=True)
    table.add_column("fold window start", no_wrap=True)
    table.add_column("listed", justify="right")
    table.add_column("of", justify="right")
    table.add_column("coverage", justify="right")
    table.add_column("not yet listed")
    for k in range(folds):
        end_idx = len(all_days) - 1 - k * step
        if end_idx < window:
            break
        window_start = all_days[end_idx - window + 1]
        cov = survivorship.coverage_as_of(bars, window_start, universe)
        missing = ", ".join(cov.not_yet_listed[:8]) + (
            f" (+{len(cov.not_yet_listed) - 8})" if len(cov.not_yet_listed) > 8 else ""
        )
        style = "green" if cov.coverage_pct >= 99.0 else "yellow"
        table.add_row(
            window_start.strftime("%Y-%m-%d"),
            f"[{style}]{cov.eligible}[/]",
            str(cov.total),
            f"[{style}]{cov.coverage_pct:.0f}%[/]",
            missing or "-",
        )
    # Oldest fold first (walk-forward order).
    console.print(table)

    # Names whose visible history is younger than the full backtest span - the ones
    # that would inflate the oldest folds if not filtered out.
    span_start = all_days[max(0, len(all_days) - 1 - (window + step * (folds - 1)))]
    young = sorted(s for s, d in firsts.items() if d > span_start)
    missing_data = sorted(s for s in universe if s not in firsts)
    console.print(
        f"\n[bold]{len(firsts)}/{len(universe)}[/] names returned history; "
        f"[bold]{len(young)}[/] list after the backtest span start "
        f"({span_start.strftime('%Y-%m-%d')})."
    )
    if young:
        console.print(f"[yellow]Younger than the span:[/] {', '.join(young)}")
    if missing_data:
        console.print(f"[dim]No history returned: {', '.join(missing_data)}[/]")
    console.print(
        "\n[dim]Point-in-time folds now exclude not-yet-listed names (backfill bias "
        "removed). Two biases remain and cannot be fixed with live-ticker data: "
        "[bold]inclusion[/] (this list is today's winners) and [bold]delisting[/] "
        "(dead/acquired names are absent). Both flatter our numbers - and since SPY is "
        "survivorship-clean and the strategies already lag it, correcting them only "
        "widens that gap. The 'match the index, don't beat it' verdict survives.[/]"
    )


@app.command("universes")
def universes_show(
    name: str = typer.Argument("", help="Preset to expand (omit to list all)."),
) -> None:
    """List preset universes, or show the tickers in one (for --symbols)."""
    if name:
        preset = universes.get_preset(name)
        if preset is None:
            _fail(f"Unknown preset '{name}'. Available: {', '.join(universes.available())}")
        console.print(f"[bold]{name}[/] ({len(preset)} symbols):")
        console.print(", ".join(preset))
        return
    table = Table(title="Preset universes", show_header=True)
    table.add_column("preset", style="cyan")
    table.add_column("symbols", justify="right")
    table.add_column("sample")
    for preset_name in universes.available():
        symbols = universes.get_preset(preset_name) or []
        table.add_row(preset_name, str(len(symbols)), ", ".join(symbols[:6]) + " ...")
    console.print(table)
    console.print(
        "[dim]Use with any --symbols flag, e.g. "
        "'backtest run --strategy momentum --symbols large-cap'.[/]"
    )


@app.command("fundamentals")
def fundamentals_show(
    symbols: str = typer.Argument(..., help="Symbol(s): a preset, or comma-separated tickers."),
) -> None:
    """Show current fundamentals (P/E, margins, ROE, growth) for symbol(s)."""
    settings = get_settings()
    setup_logging(settings)
    universe = _expand_symbols(symbols)
    if not universe:
        _fail("Give at least one symbol (or a preset name).")

    rows: list[market_data.Fundamentals] = []
    missing: list[str] = []
    try:
        with _build_client(settings) as client:
            for symbol in universe:
                fundamentals = market_data.get_fundamentals(client, symbol)
                if fundamentals is None:
                    missing.append(symbol)
                else:
                    rows.append(fundamentals)
    except (oauth.OAuthError, api.ApiError) as exc:
        _fail(exc)

    if missing:
        console.print(f"[dim]No fundamentals for: {', '.join(missing)}[/]")
    if not rows:
        _fail("No fundamentals returned.")

    def _num(value: float | None, *, pct: bool = False, suffix: str = "") -> str:
        if value is None:
            return "-"
        return f"{value:+.1f}%" if pct else f"{value:.2f}{suffix}"

    table = Table(title="Fundamentals (current)", show_header=True)
    table.add_column("symbol", style="cyan")
    table.add_column("P/E", justify="right")
    table.add_column("PEG", justify="right")
    table.add_column("div yld", justify="right")
    table.add_column("ROE", justify="right")
    table.add_column("op margin", justify="right")
    table.add_column("EPS chg", justify="right")
    table.add_column("rev chg", justify="right")
    table.add_column("D/E", justify="right")
    table.add_column("mkt cap", justify="right")
    for f in rows:
        cap = f"{f.market_cap / 1e9:.0f}B" if f.market_cap else "-"
        table.add_row(
            f.symbol,
            _num(f.pe_ratio),
            _num(f.peg_ratio),
            _num(f.dividend_yield, pct=True),
            _num(f.return_on_equity, pct=True),
            _num(f.operating_margin_ttm, pct=True),
            _num(f.eps_change_pct_ttm, pct=True),
            _num(f.rev_change_ttm, pct=True),
            _num(f.total_debt_to_equity),
            cap,
        )
    console.print(table)


@app.command("screen")
def screen_universe(
    from_universe: str = typer.Option(
        "large-cap", "--from", help="Candidate universe: a preset or CSV tickers."
    ),
    min_market_cap: float = typer.Option(0, "--min-cap", help="Min market cap in $B (0=off)."),
    max_pe: float = typer.Option(0, "--max-pe", help="Max P/E (0=off)."),
    profitable: bool = typer.Option(
        False, "--profitable", help="Require positive P/E (exclude loss-makers)."
    ),
    max_peg: float = typer.Option(0, "--max-peg", help="Max PEG (0=off)."),
    min_roe: float = typer.Option(0, "--min-roe", help="Min return on equity %% (0=off)."),
    max_debt_equity: float = typer.Option(0, "--max-de", help="Max debt/equity (0=off)."),
    min_eps_growth: float = typer.Option(0, "--min-eps-growth", help="Min EPS chg %% TTM (0=off)."),
) -> None:
    """Filter a universe by fundamentals into a screened symbol list (for --symbols)."""
    settings = get_settings()
    setup_logging(settings)
    candidates = _expand_symbols(from_universe)
    if not candidates:
        _fail("Give a --from preset or ticker list.")

    criteria = screen.ScreenCriteria(
        min_market_cap=min_market_cap * 1e9 if min_market_cap > 0 else None,
        max_pe=max_pe if max_pe > 0 else None,
        min_pe=0.0 if profitable else None,
        max_peg=max_peg if max_peg > 0 else None,
        min_roe=min_roe if min_roe > 0 else None,
        max_debt_to_equity=max_debt_equity if max_debt_equity > 0 else None,
        min_eps_growth=min_eps_growth if min_eps_growth != 0 else None,
    )

    console.print(f"[dim]Screening {len(candidates)} candidates on fundamentals...[/]")
    rows: list[market_data.Fundamentals] = []
    try:
        with _build_client(settings) as client:
            for symbol in candidates:
                fundamentals = market_data.get_fundamentals(client, symbol)
                if fundamentals is not None:
                    rows.append(fundamentals)
    except (oauth.OAuthError, api.ApiError) as exc:
        _fail(exc)

    passing = screen.apply_screen(rows, criteria)
    passing.sort(key=lambda f: f.pe_ratio if f.pe_ratio is not None else 1e18)

    table = Table(title=f"Screen: {len(passing)}/{len(candidates)} passed", show_header=True)
    table.add_column("symbol", style="cyan")
    table.add_column("P/E", justify="right")
    table.add_column("PEG", justify="right")
    table.add_column("ROE", justify="right")
    table.add_column("EPS chg", justify="right")
    table.add_column("D/E", justify="right")
    table.add_column("mkt cap", justify="right")
    for f in passing:
        table.add_row(
            f.symbol,
            f"{f.pe_ratio:.1f}" if f.pe_ratio is not None else "-",
            f"{f.peg_ratio:.2f}" if f.peg_ratio is not None else "-",
            f"{f.return_on_equity:+.0f}%" if f.return_on_equity is not None else "-",
            f"{f.eps_change_pct_ttm:+.0f}%" if f.eps_change_pct_ttm is not None else "-",
            f"{f.total_debt_to_equity:.0f}" if f.total_debt_to_equity is not None else "-",
            f"{f.market_cap / 1e9:.0f}B" if f.market_cap else "-",
        )
    console.print(table)
    if passing:
        symbols = ",".join(f.symbol for f in passing)
        console.print(f"\n[green]Passing symbols[/] (paste into --symbols):\n{symbols}")


@app.command("macro")
def macro_show() -> None:
    """Show the current macro read (VIX, yield curve, credit spread) from FRED."""
    settings = get_settings()
    setup_logging(settings)
    if not settings.has_fred_key:
        _fail(
            "No FRED API key configured. Get a free one at "
            "https://fredaccount.stlouisfed.org/apikeys and set SCHWAB_FRED_API_KEY in .env."
        )
    try:
        snapshot = fred.get_macro(settings.fred_api_key.get_secret_value())
    except fred.FredUnavailable as exc:
        _fail(exc)
    except httpx.HTTPError as exc:
        _fail(f"FRED request failed: {exc}")

    table = Table(title=f"Macro (FRED, as of {snapshot.as_of or 'n/a'})", show_header=False)
    table.add_column("Series", style="cyan", no_wrap=True)
    table.add_column("Value", justify="right")
    table.add_row("VIX (volatility)", f"{snapshot.vix:.1f}" if snapshot.vix is not None else "-")
    table.add_row(
        "10y-3m yield curve",
        f"{snapshot.yield_curve_10y_3m:+.2f}%" if snapshot.yield_curve_10y_3m is not None else "-",
    )
    table.add_row(
        "BAA credit spread",
        f"{snapshot.credit_spread:.2f}%" if snapshot.credit_spread is not None else "-",
    )
    console.print(table)
    if snapshot.yield_curve_10y_3m is not None and snapshot.yield_curve_10y_3m < 0:
        console.print("[yellow]Yield curve inverted (10y < 3m) - a classic recession signal.[/]")


@app.command("signals")
def signals_show(
    symbols: str = typer.Option("", "--symbols", help="Comma-separated universe override."),
    benchmark: str = typer.Option("SPY", "--benchmark", help="Regime benchmark symbol."),
) -> None:
    """Show the current market regime and momentum ranks for the universe (read-only)."""
    settings = get_settings()
    setup_logging(settings)
    universe = _resolve_universe(settings, symbols)

    bars: dict[str, list[market_data.Candle]] = {}
    benchmark_candles: list[market_data.Candle] = []
    hist_cache = history_cache.HistoryCache(settings.history_cache_dir)
    try:
        with _build_client(settings) as client:
            for symbol in universe:
                candles = hist_cache.get(client, symbol, days=300)
                if candles:
                    bars[symbol] = candles
            benchmark_candles = hist_cache.get(client, benchmark, days=300)
    except (oauth.OAuthError, api.ApiError) as exc:
        _fail(exc)

    if not bars:
        _fail("No historical data was returned for any symbol.")

    if benchmark_candles:
        regime = signals.regime_signal(benchmark_candles, bars)
        console.print(
            f"[bold]Regime[/] ({benchmark}): score [bold]{regime.score}/4[/] -> deploy up to "
            f"[bold]{regime.gross_exposure_cap * 100:.0f}%[/] of the sleeve"
        )
        console.print(
            f"  [dim]{benchmark}>200DMA={regime.spy_above_200dma}, "
            f"50>200={regime.trend_50_over_200}, "
            f"breadth>50%={regime.breadth_above_50} ({regime.breadth_pct * 100:.0f}%), "
            f"calm-vol={regime.calm_volatility}[/]"
        )
    else:
        console.print(f"[dim]No {benchmark} history; skipping regime.[/]")

    features = {symbol: signals.momentum_features(candles) for symbol, candles in bars.items()}
    composite = signals.momentum_composite(features)

    table = Table(title="Momentum ranks", show_header=True)
    table.add_column("symbol", style="cyan")
    table.add_column("mom rank", justify="right")
    table.add_column("ret_20", justify="right")
    table.add_column("ret_60", justify="right")
    table.add_column("ewma vol", justify="right")

    def _pct(value: float | None) -> str:
        return f"{value * 100:+.1f}%" if value is not None else "n/a"

    ranked = sorted(composite, key=lambda s: composite[s], reverse=True)
    for symbol in ranked:
        feats = features[symbol]
        vol = signals.ewma_vol([float(c.close) for c in bars[symbol]])
        table.add_row(
            symbol,
            f"{composite[symbol]:.2f}",
            _pct(feats.ret_20),
            _pct(feats.ret_60),
            f"{vol * 100:.0f}%" if vol is not None else "n/a",
        )
    for symbol in bars:
        if symbol not in composite:
            table.add_row(symbol, "[dim]n/a[/]", "-", "-", "-")
    console.print(table)


# --- Sleeves (parallel strategy comparison; paper only) ---------------------

# Every strategy that can run in a paper sleeve. Derived from the single
# authoritative strategy_registry table.
_SLEEVE_STRATEGIES = tuple(strategy_registry.paper_strategy_names())


def _history_strategy_from_bars(
    strategy: str,
    universe: list[str],
    history: dict[str, list[market_data.Candle]],
    benchmark_history: list[market_data.Candle],
    *,
    max_positions: int,
    max_position_fraction: Decimal,
    store: sec_store.SecStore | None = None,
    factor: str = "earnings-yield",
) -> agent.Strategy:
    """Construct a history-based strategy from already-fetched bars (no network)."""
    if strategy == agent.VALUE_MOMENTUM_NAME:
        if store is None:
            _fail("value-momentum needs EDGAR data. Run 'schwab-trader edgar fetch' first.")
        return agent.ValueMomentumStrategy(
            universe,
            history=history,
            benchmark_history=benchmark_history,
            store=store,
            factor=factor,
            max_positions=max_positions,
            max_position_fraction=max_position_fraction,
        )
    if strategy == agent.POST_EARNINGS_DRIFT_NAME:
        # Store-backed but not history-backed: the SUE signal comes from EDGAR; the bars
        # are only used by the replay to price fills, so history is ignored here.
        if store is None:
            _fail("post-earnings-drift needs EDGAR data. Run 'schwab-trader edgar fetch' first.")
        return agent.PostEarningsDriftStrategy(
            universe,
            store=store,
            max_positions=max_positions,
            max_position_fraction=max_position_fraction,
        )
    if strategy == agent.TrendStrategy.name:
        return agent.TrendStrategy(
            universe,
            history=history,
            benchmark_history=benchmark_history,
            max_positions=max_positions,
            max_position_fraction=max_position_fraction,
        )
    if strategy == agent.MeanReversionStrategy.name:
        return agent.MeanReversionStrategy(
            universe,
            history=history,
            benchmark_history=benchmark_history,
            max_positions=max_positions,
            max_position_fraction=max_position_fraction,
        )
    if strategy == agent.LowVolatilityStrategy.name:
        return agent.LowVolatilityStrategy(
            universe,
            history=history,
            benchmark_history=benchmark_history,
            max_positions=max_positions,
            max_position_fraction=max_position_fraction,
        )
    if strategy == agent.TacticalRegimeStrategy.name:
        return agent.TacticalRegimeStrategy(
            universe,
            history=history,
            benchmark_history=benchmark_history,
        )
    return agent.MomentumStrategy(
        universe,
        history=history,
        benchmark_history=benchmark_history,
        max_positions=max_positions,
        max_position_fraction=max_position_fraction,
    )


def _build_history_strategy(
    strategy: str,
    client: api.SchwabClient,
    universe: list[str],
    *,
    cache: history_cache.HistoryCache,
    max_positions: int,
    max_position_fraction: Decimal,
    benchmark: str = "SPY",
    days: int = 300,
    store: sec_store.SecStore | None = None,
    factor: str = "earnings-yield",
) -> agent.Strategy:
    """Fetch daily history and build a history-based strategy (+EDGAR for value-momentum)."""
    history: dict[str, list[market_data.Candle]] = {}
    for symbol in universe:
        candles = cache.get(client, symbol, days=days)
        if candles:
            history[symbol] = candles
    benchmark_history = cache.get(client, benchmark, days=days)
    return _history_strategy_from_bars(
        strategy,
        universe,
        history,
        benchmark_history,
        max_positions=max_positions,
        max_position_fraction=max_position_fraction,
        store=store,
        factor=factor,
    )


def _sleeve_llm_builder(
    settings: Settings,
    spec: research.StrategySpec | None,
    on_usage: llm_strategy.UsageSink,
) -> strategy_registry.LlmBuilder:
    """Return the injected builder the registry uses for an LLM sleeve.

    The Anthropic wiring (api key, model, research spec, usage sink) is captured
    here; the registry only supplies the universe and the validated sizing.
    """

    def _build(universe: list[str], params: dict[str, object]) -> agent.Strategy:
        try:
            return llm_strategy.build_llm_strategy(
                universe,
                api_key=settings.anthropic_api_key.get_secret_value(),
                model=settings.llm_model,
                spec_guidance=spec.guidance_text() if spec is not None else None,
                max_positions=cast(int, params["max_positions"]),
                max_position_fraction=cast(Decimal, params["max_position_fraction"]),
                on_usage=on_usage,
            )
        except llm_strategy.AnthropicUnavailable as exc:
            _fail(exc)

    return _build


def _sleeve_resources(
    settings: Settings,
    cfg: sleeves.SleeveConfig,
    entry: strategy_registry.StrategyEntry,
    spec: research.StrategySpec | None,
    on_usage: llm_strategy.UsageSink,
    client: api.SchwabClient,
    cache: history_cache.HistoryCache,
    universe: list[str],
) -> strategy_registry.StrategyResources:
    """Resolve exactly the runtime dependencies the sleeve's strategy declares."""
    history: dict[str, list[market_data.Candle]] | None = None
    benchmark_history: list[market_data.Candle] | None = None
    store: sec_store.SecStore | None = None
    llm_builder: strategy_registry.LlmBuilder | None = None

    if strategy_registry.CAP_DAILY_HISTORY in entry.capabilities:
        history = {}
        for symbol in universe:
            candles = cache.get(client, symbol, days=300)
            if candles:
                history[symbol] = candles
        benchmark_history = cache.get(client, "SPY", days=300)
    if strategy_registry.CAP_SEC_EDGAR in entry.capabilities:
        edgar = storage_factory.sec_store(settings)
        if edgar.total_facts() == 0:
            _fail(
                f"Sleeve '{cfg.name}' uses '{cfg.strategy}' but the EDGAR store is empty. "
                "Run 'schwab-trader edgar fetch' first."
            )
        store = edgar
    if entry.is_llm:
        llm_builder = _sleeve_llm_builder(settings, spec, on_usage)

    return strategy_registry.StrategyResources(
        history=history,
        benchmark_history=benchmark_history,
        store=store,
        llm_builder=llm_builder,
    )


def _build_sleeve_strategy(
    settings: Settings,
    cfg: sleeves.SleeveConfig,
    spec: research.StrategySpec | None,
    on_usage: llm_strategy.UsageSink,
    client: api.SchwabClient,
    cache: history_cache.HistoryCache,
) -> agent.Strategy:
    """Build a sleeve's strategy from its config, entirely via the strategy registry.

    Runtime dependencies (daily history, the EDGAR store, the LLM wiring) are
    resolved from the strategy's declared capabilities, then construction and
    parameter validation are delegated to :mod:`schwab_trader.strategy_registry`,
    which fails closed on an unknown strategy or missing dependency.
    """
    universe = cfg.universe or settings.agent_universe_list or agent.DEFAULT_UNIVERSE
    try:
        entry = strategy_registry.entry(cfg.strategy)
    except strategy_registry.UnknownStrategyError:
        _fail(f"Sleeve '{cfg.name}' has an unknown strategy '{cfg.strategy}'.")
    resources = _sleeve_resources(settings, cfg, entry, spec, on_usage, client, cache, universe)
    params = strategy_registry.sleeve_parameter_values(
        cfg.strategy,
        max_positions=cfg.max_positions,
        max_position_fraction=cfg.max_position_fraction,
        factor=cfg.factor,
    )
    try:
        return strategy_registry.build(
            cfg.strategy, universe, parameters=params, resources=resources
        )
    except strategy_registry.StrategyRegistryError as exc:
        _fail(exc)


def _snapshot_digest(prefix: str, payload: object) -> str:
    encoded = json.dumps(payload, default=str, sort_keys=True, separators=(",", ":"))
    return f"{prefix}:{hashlib.sha256(encoded.encode()).hexdigest()}"


def _capture_official_sleeve_snapshot(
    settings: Settings,
    configs: tuple[sleeves.SleeveConfig, ...],
    session: scheduling.ExchangeSession,
    required_snapshot_id: str | None,
    *,
    client: api.SchwabClient,
    cache: history_cache.HistoryCache,
    evidence_store: MarketDataEvidenceRepository,
    spec: research.StrategySpec | None,
    on_usage: llm_strategy.UsageSink,
) -> sleeve_runs.CohortSnapshot:
    """Capture one immutable quote/data view for every member in a cohort run."""
    captured_at = datetime.now(UTC)
    universes = {
        cfg.name: cfg.universe or settings.agent_universe_list or agent.DEFAULT_UNIVERSE
        for cfg in configs
    }
    symbols = sorted({symbol for universe in universes.values() for symbol in universe})
    quotes = market_data.get_quotes(client, symbols)
    quote_id = _snapshot_digest(
        "quotes",
        {symbol: quote.model_dump(mode="json") for symbol, quote in sorted(quotes.items())},
    )

    capabilities = {
        capability
        for cfg in configs
        if cfg.definition is not None
        for capability in cfg.definition.data_requirements
    }
    needs_history = strategy_registry.CAP_DAILY_HISTORY in capabilities
    needs_edgar = strategy_registry.CAP_SEC_EDGAR in capabilities
    needs_llm = strategy_registry.CAP_LLM_PROVIDER in capabilities

    history: dict[str, list[market_data.Candle]] | None = None
    benchmark_history: list[market_data.Candle] | None = None
    bar_probe = data_readiness.SourceProbe.missing()
    data_ids: dict[str, str] = {}
    operator_diagnostics: dict[str, object] = {}
    if needs_history:
        history = {}
        # A provider failure must never look like an absence. Collecting the sanitized
        # failure here means an unreachable source reports `provider_error` with a
        # class name rather than silently degrading into missing or stale history.
        bar_failures: dict[str, str] = {}
        bar_resolutions: dict[str, daily_bar_fallback.DailyBarResolution] = {}
        bar_evidence_ids: dict[str, str] = {}
        for symbol in sorted({*symbols, "SPY"}):
            try:
                resolution = daily_bar_fallback.resolve_daily_history(
                    client,
                    cache,
                    evidence_store,
                    symbol,
                    session.session_date,
                    days=300,
                )
                bar_resolutions[symbol] = resolution
                candles = list(resolution.history)
                if resolution.dataset_id is not None:
                    bar_evidence_ids[symbol] = resolution.dataset_id
            except Exception as exc:
                bar_failures[symbol] = type(exc).__name__
                candles = []
            if symbol == "SPY":
                benchmark_history = candles
            if symbol in symbols and candles:
                history[symbol] = candles
        benchmark_history = benchmark_history or []
        all_history = {**history, "SPY": benchmark_history}
        history_id = _snapshot_digest(
            "daily-bars",
            {
                "candles": {
                    symbol: [candle.model_dump(mode="json") for candle in candles]
                    for symbol, candles in sorted(all_history.items())
                },
                # The exact intraday constituent identity is part of the snapshot,
                # even when two distinct constituent datasets aggregate to the same
                # OHLCV candle.
                "derived_evidence": dict(sorted(bar_evidence_ids.items())),
            },
        )
        data_ids[data_readiness.DataKind.DAILY_BARS.value] = history_id
        data_ids.update(
            {
                f"daily_bars_evidence:{symbol}": dataset_id
                for symbol, dataset_id in sorted(bar_evidence_ids.items())
            }
        )
        latest = [candle.date for candles in history.values() for candle in candles]
        as_of = max(latest) if latest else (session.decision_utc or captured_at)
        retrieved_at = max(
            (resolution.retrieved_at for resolution in bar_resolutions.values()),
            default=captured_at,
        )
        has_derived = any(
            resolution.state is daily_bar_fallback.DailyBarState.DERIVED
            for resolution in bar_resolutions.values()
        )
        bars = tuple(
            data_contracts.BarObservation(
                symbol=symbol,
                session_date=candles[-1].date.date(),
                open=candles[-1].open or candles[-1].close,
                high=candles[-1].high or candles[-1].close,
                low=candles[-1].low or candles[-1].close,
                close=candles[-1].close,
                volume=candles[-1].volume,
            )
            for symbol, candles in sorted(all_history.items())
            if candles
        )
        batch = data_contracts.BarBatch(
            provenance=data_contracts.Provenance(
                source=(
                    "schwab-daily-plus-intraday-derived"
                    if has_derived
                    else market_data.SCHWAB_DAILY_HISTORY_SOURCE
                ),
                snapshot_id=history_id,
                retrieved_at=retrieved_at,
                as_of=as_of,
                timing=data_contracts.TimingPolicy.SETTLED_EOD,
                vintage_safe=True,
            ),
            bars=bars,
        )
        coverage = [
            daily_bar_fallback.operator_payload(resolution)
            for _, resolution in sorted(bar_resolutions.items())
        ]
        coverage.extend(
            {
                "symbol": symbol,
                "target_session": session.session_date.isoformat(),
                "state": "provider_error",
                "latest_official_session": None,
                "evidence_source": None,
                "expected_interval_count": None,
                "observed_interval_count": None,
                "first_interval_at": None,
                "final_interval_at": None,
                "aggregation_safe": False,
                "dataset_id": None,
                "error": error,
            }
            for symbol, error in sorted(bar_failures.items())
        )
        coverage.sort(key=lambda item: str(item["symbol"]))
        operator_diagnostics["market_data"] = {
            "target_session": session.session_date.isoformat(),
            "symbols": coverage,
        }
        if bar_failures:
            # Any provider fault blocks the cohort with a named cause rather than
            # letting the affected symbols read as merely absent.
            failed = ", ".join(sorted({reason for reason in bar_failures.values()}))
            bar_probe = data_readiness.SourceProbe.failed(
                f"{len(bar_failures)} symbol(s) failed to fetch ({failed})"
            )
        else:
            bar_probe = data_readiness.SourceProbe.of(batch)

    edgar_store: sec_store.SecStore | None = None
    fundamental_probe = data_readiness.SourceProbe.missing()
    if needs_edgar:
        candidate: sec_store.SecStore | None = None
        try:
            candidate = storage_factory.sec_store(settings)
            covered = set(candidate.tickers())
            total_facts = candidate.total_facts()
        except Exception:
            covered = set()
            total_facts = 0
        try:
            sec_stat = settings.sec_db_path.stat()
            file_identity: dict[str, int] = {
                "size": sec_stat.st_size,
                "mtime_ns": sec_stat.st_mtime_ns,
            }
        except OSError:
            file_identity = {}
        fundamental_id = _snapshot_digest(
            "sec-edgar",
            {
                "session": session.session_date.isoformat(),
                "tickers": sorted(covered),
                "total_facts": total_facts,
                "file": file_identity,
            },
        )
        data_ids[data_readiness.DataKind.FUNDAMENTALS.value] = fundamental_id
        fundamental_batch = sleeve_runs.SnapshotCoverage(
            provenance=data_contracts.Provenance(
                source="sec-edgar-store",
                snapshot_id=fundamental_id,
                retrieved_at=captured_at,
                as_of=captured_at,
                available_at=captured_at,
                timing=data_contracts.TimingPolicy.POINT_IN_TIME,
                vintage_safe=True,
            ),
            keys=frozenset(symbol for symbol in symbols if symbol in covered),
        )
        fundamental_probe = data_readiness.SourceProbe.of(fundamental_batch)
        if total_facts and candidate is not None:
            edgar_store = candidate

    readiness_by_member: dict[str, data_readiness.DataReadiness] = {}
    for cfg in configs:
        pairs: list[tuple[data_readiness.DataRequirement, data_readiness.SourceProbe]] = []
        definition = cfg.definition
        if definition is not None:
            for capability in definition.data_requirements:
                if capability == strategy_registry.CAP_DAILY_HISTORY:
                    pairs.append(
                        (
                            data_readiness.DataRequirement(
                                kind=data_readiness.DataKind.DAILY_BARS,
                                keys=tuple(sorted({*universes[cfg.name], "SPY"})),
                                # Judge freshness by exchange session, not elapsed
                                # hours: for an official session dated D the strategy
                                # needs settled bars through D, and Friday's close is
                                # not evidence about Monday.
                                required_session=session.session_date,
                            ),
                            bar_probe,
                        )
                    )
                elif capability == strategy_registry.CAP_SEC_EDGAR:
                    pairs.append(
                        (
                            data_readiness.DataRequirement(
                                kind=data_readiness.DataKind.FUNDAMENTALS,
                                keys=tuple(sorted(universes[cfg.name])),
                                require_vintage_safe=True,
                            ),
                            fundamental_probe,
                        )
                    )
                elif capability in {data_readiness.DataKind.MACRO.value, "macro-data"}:
                    pairs.append(
                        (
                            data_readiness.DataRequirement(kind=data_readiness.DataKind.MACRO),
                            data_readiness.SourceProbe.missing(),
                        )
                    )
        readiness_by_member[cfg.name] = data_readiness.evaluate_readiness(
            pairs,
            now=captured_at,
        )

    llm_builder = _sleeve_llm_builder(settings, spec, on_usage) if needs_llm else None
    snapshot_id = _snapshot_digest(
        "cohort-snapshot",
        {"quotes": quote_id, "data": dict(sorted(data_ids.items()))},
    )
    if required_snapshot_id is not None and snapshot_id != required_snapshot_id:
        raise sleeve_runs.SnapshotMismatchError(
            "The current data cannot reproduce the persisted cohort snapshot."
        )
    return sleeve_runs.CohortSnapshot(
        snapshot_id=snapshot_id,
        quote_snapshot_id=quote_id,
        captured_at=captured_at,
        quotes=quotes,
        resources=strategy_registry.StrategyResources(
            history=history,
            benchmark_history=benchmark_history,
            store=edgar_store,
            llm_builder=llm_builder,
        ),
        readiness_by_member=readiness_by_member,
        data_snapshot_ids=data_ids,
        operator_diagnostics=operator_diagnostics,
    )


def _emit_cohort_run_alert(
    settings: Settings,
    members: Sequence[sleeves.SleeveConfig],
    result: sleeve_runs.SleeveRun,
    *,
    now_et: datetime,
) -> None:
    """Notify the operator about one finished cohort session, at most once ever.

    Called only after the run's outcome is durably recorded. Every failure inside —
    reading observations, the alert record, the notifier — is caught and printed. A
    dark or broken notification channel must never make a completed paper run look
    failed, and must never cause it to be repeated.
    """
    try:
        report = cohort_ops.assess_cohort(
            result.cohort_id,
            now_et=now_et,
            runs=[result],
            observations=_cohort_observations(settings, members),
            expected_members=[cfg.identity for cfg in members],
            storage_kind=_storage_kind(settings),
            session_date=result.scheduled_for,
        )
        ran_late = report.run is not None and report.run.ran_late
        kind = cohort_alerts.kind_for_run(result, ran_late=ran_late)
        if kind is None:
            return
        outcome = _alert_service(settings).emit(kind, report)
    except Exception as exc:
        console.print(
            f"[yellow]alert:[/] not delivered ({type(exc).__name__}). "
            "The recorded cohort run is unaffected."
        )
        return
    style = "yellow" if outcome.is_problem else "dim"
    console.print(
        f"[{style}]alert {outcome.kind.value}: {outcome.status.value} — {outcome.detail}[/]"
    )


def _run_official_sleeve_cohorts(
    settings: Settings,
    store: sleeves.SleeveStore,
    targets: list[sleeves.SleeveConfig],
    *,
    scheduled_for: date | None,
) -> None:
    unassigned = [cfg.name for cfg in targets if not cfg.cohort_id]
    if unassigned:
        _fail(
            "Official runs require persisted cohort identity and StrategyDefinition; "
            f"unassigned sleeves: {unassigned}."
        )
    incomplete = [cfg.name for cfg in targets if not cfg.reproducible]
    if incomplete:
        _fail(f"Official runs require reproducible sleeve definitions: {incomplete}.")

    # A superseded cohort's record is closed evidence. Appending a session to it would
    # rewrite the history of an incident, so this refuses before anything is captured,
    # locked, or persisted — including when the cohort was named explicitly.
    for refusal in sorted(
        filter(None, {cohort_lifecycle.run_refusal(cfg.cohort_id) for cfg in targets})
    ):
        _fail(refusal)

    grouped: dict[str, list[sleeves.SleeveConfig]] = {}
    for cfg in targets:
        grouped.setdefault(cfg.cohort_id, []).append(cfg)
    run_store = storage_factory.run_store(settings)
    hist_cache = history_cache.HistoryCache(settings.history_cache_dir)
    on_usage, cost_tally = _usage_recorder(settings, "execution")
    now_et = market_calendar.eastern_now()
    had_failure = False
    executed = False

    try:
        with contextlib.ExitStack() as stack:
            client: api.SchwabClient | None = None
            evidence_store: MarketDataEvidenceRepository | None = None
            for cohort_id, members in sorted(grouped.items()):
                configs = tuple(members)
                completed = run_store.completed_run_keys(cohort_id=cohort_id)
                decision = (
                    scheduling.evaluate_session(
                        cohort_id,
                        scheduled_for,
                        now_et,
                        completed_run_keys=completed,
                    )
                    if scheduled_for is not None
                    else scheduling.plan_run(
                        cohort_id,
                        now_et,
                        completed_run_keys=completed,
                    )
                )

                def provider(
                    captured_configs: tuple[sleeves.SleeveConfig, ...],
                    session: scheduling.ExchangeSession,
                    prior_snapshot_id: str | None,
                ) -> sleeve_runs.CohortSnapshot:
                    nonlocal client, evidence_store
                    if client is None:
                        client = stack.enter_context(_build_client(settings))
                    if evidence_store is None:
                        evidence_store = storage_factory.market_data_evidence_store(settings)
                    return _capture_official_sleeve_snapshot(
                        settings,
                        captured_configs,
                        session,
                        prior_snapshot_id,
                        client=client,
                        cache=hist_cache,
                        evidence_store=evidence_store,
                        spec=storage_factory.research_store(settings).latest(),
                        on_usage=on_usage,
                    )

                runner = sleeve_runs.SleeveRunOrchestrator(
                    run_store=run_store,
                    sleeve_store=store,
                    kill_switch=safety.KillSwitch(settings.kill_switch_path),
                    snapshot_provider=provider,
                    universe_resolver=lambda cfg: (
                        cfg.universe or settings.agent_universe_list or agent.DEFAULT_UNIVERSE
                    ),
                    engine_factory=lambda cfg: storage_factory.paper_engine(settings, cfg),
                    evaluation_factory=lambda cfg: storage_factory.evaluation_store(settings, cfg),
                )
                result = runner.run(decision, configs, now=datetime.now(UTC))
                # `awaiting-data` is deliberately absent from the failure set: nothing
                # ran, nothing was lost, and the next invocation retries. It is tracked
                # separately so the caller can skip post-run reporting without treating
                # a normal wait as a failure.
                had_failure = had_failure or result.status in {
                    sleeve_runs.SleeveRunStatus.FAILED,
                    sleeve_runs.SleeveRunStatus.MISSED,
                    sleeve_runs.SleeveRunStatus.PARTIAL,
                }
                executed = executed or bool(result.completed_members)
                console.print(
                    f"[cyan]{cohort_id}[/] {result.scheduled_for}: "
                    f"{result.status.value} "
                    f"({len(result.completed_members)}/{len(result.expected_members)} members)"
                )
                if result.status is sleeve_runs.SleeveRunStatus.AWAITING_DATA:
                    console.print(
                        "[dim]No member executed and nothing was recorded; the session "
                        "stays retryable until its scheduling deadline.[/]"
                    )
                    if sleeve_runs.awaits_reauthentication(result):
                        # Nothing was captured, so there is no coverage to print — and
                        # unlike a provider wait, this one never clears on its own.
                        console.print(
                            "[yellow]Schwab reauthentication is required.[/] Run "
                            "'python -m schwab_trader auth login' on this machine, then "
                            "re-run this cohort before the session deadline."
                        )
                    # Per-member coverage detail: data kind, reason code, target
                    # session, latest available session, and which keys are short.
                    preflight = runner.last_preflight
                    if preflight is not None:
                        for line in preflight.diagnostics():
                            console.print(f"[dim]  {line}[/]")
                for error in sleeve_runs.active_errors(result):
                    member = f" [{error.member_id}]" if error.member_id else ""
                    console.print(f"[yellow]{error.code}{member}:[/] {error.message}")
                # Strictly after the durable result exists, and strictly unable to
                # change it: the alert path swallows every failure into console output.
                _emit_cohort_run_alert(settings, configs, result, now_et=now_et)
    except (oauth.OAuthError, api.ApiError) as exc:
        _fail(exc)

    if cost_tally[0] > 0:
        console.print(f"[dim]API cost this run: {_cost(cost_tally[0])}.[/]")
    if had_failure:
        raise typer.Exit(code=1)
    if not executed:
        # Distinct from both success and failure: no member executed, so any post-run
        # step (leaderboard, digest) would report the previous session's numbers as
        # though they were this one's. Callers skip those; nothing failed.
        raise typer.Exit(code=EXIT_NOTHING_EXECUTED)


def _storage_kind(settings: Settings) -> str:
    """A *category* of storage for operator output — never the connection string."""
    shared = storage_factory.database(settings)
    if shared is None:
        return "local-sqlite"
    return f"shared-{shared.dialect}"


def _exclude_superseded_sleeves(
    targets: list[sleeves.SleeveConfig], *, named: bool
) -> list[sleeves.SleeveConfig]:
    """Drop members of superseded cohorts from an ordinary (non-official) run.

    An unofficial cycle is not an official observation, but it still writes to the
    sleeve's paper account and evaluation store — and for a withdrawn cohort those rows
    are the incident evidence. "Never re-run" has to mean the ``--match``/``--all``
    convenience paths too, or the guarantee only holds for the command that happens to
    pass ``--official``.

    Same split as the official path, for the same reason: naming one sleeve explicitly is
    refused, because the operator asked for that exact thing and needs to know why it will
    not run, while a broad selection drops the retired members and carries on.
    """
    retired = sorted(
        {cfg.cohort_id for cfg in targets if cohort_lifecycle.is_historical(cfg.cohort_id)}
    )
    if not retired:
        return targets
    if named:
        _fail(cohort_lifecycle.run_refusal(retired[0]))
    kept = [cfg for cfg in targets if not cohort_lifecycle.is_historical(cfg.cohort_id)]
    console.print(
        f"[yellow]Skipping superseded cohort(s): {', '.join(retired)}.[/] "
        "Their records are kept unchanged and are never re-run."
    )
    if not kept:
        _fail(
            "Every selected sleeve belongs to a superseded cohort; nothing is eligible to "
            "run. Select the active cohort's sleeves instead."
        )
    return kept


def _find_cohort(
    settings: Settings,
    store: sleeves.SleeveStore,
    cohort: str,
) -> tuple[str, list[sleeves.SleeveConfig], str]:
    """Resolve the cohort to operate on, refusing to guess between several.

    Precedence is explicit flag, then configured default, then the only *active* cohort
    that exists. More than one active candidate with nothing configured is unresolved,
    not a pick. A superseded cohort is never resolved implicitly — it can still be named
    explicitly, because read-only commands must be able to inspect its records — so the
    replacement of a failed experiment does not turn an unambiguous default into an
    ambiguous one. Returns ``(cohort_id, members, problem)`` where an empty ``problem``
    means the cohort resolved.
    """
    members = store.list()
    known = sorted({cfg.cohort_id for cfg in members if cfg.cohort_id})
    active = cohort_lifecycle.active_cohorts(known)
    chosen = cohort.strip() or settings.cohort_id.strip()
    if not chosen:
        if len(active) == 1:
            chosen = active[0]
        elif not known:
            return "", [], "No cohort exists yet. Bootstrap the paper cohort first."
        elif not active:
            return (
                "",
                [],
                f"No active cohort exists; every persisted cohort ({', '.join(known)}) is "
                "historical. Bootstrap a replacement, or pass --cohort to inspect one.",
            )
        else:
            return (
                "",
                [],
                f"Several active cohorts exist ({', '.join(active)}). Pass --cohort or set "
                "SCHWAB_COHORT_ID; the cohort is never guessed.",
            )
    return chosen, [cfg for cfg in members if cfg.cohort_id == chosen], ""


def _resolve_cohort(
    settings: Settings,
    store: sleeves.SleeveStore,
    cohort: str,
) -> tuple[str, list[sleeves.SleeveConfig]]:
    """:func:`_find_cohort` for commands that cannot proceed without a cohort."""
    chosen, members, problem = _find_cohort(settings, store, cohort)
    if problem:
        _fail(problem)
    return chosen, members


def _cohort_observations(
    settings: Settings,
    members: Sequence[sleeves.SleeveConfig],
) -> list[evaluation.OfficialDailyObservation]:
    collected: list[evaluation.OfficialDailyObservation] = []
    for cfg in members:
        collected.extend(
            storage_factory.evaluation_store(settings, cfg).official_observations(limit=10_000)
        )
    return collected


def _cohort_start_session(
    store: sleeves.SleeveStore,
    cohort_id: str,
    members: Sequence[sleeves.SleeveConfig],
) -> date | None:
    """When the cohort was first owed an official run.

    Thin alias for :func:`schwab_trader.sleeves.resolve_cohort_start`, which owns the
    precedence rule. It lives there so the dashboard resolves the start identically:
    two separate implementations of "when did this cohort begin" is exactly what let
    the dashboard report a freshly created cohort as never scheduled.
    """
    return sleeves.resolve_cohort_start(store, cohort_id, members)


def _cohort_health_report(
    settings: Settings,
    store: sleeves.SleeveStore,
    cohort_id: str,
    members: Sequence[sleeves.SleeveConfig],
    *,
    now_et: datetime,
) -> cohort_ops.CohortHealthReport:
    runs = list(storage_factory.run_store(settings).list(cohort_id=cohort_id, limit=400))
    return cohort_ops.assess_cohort(
        cohort_id,
        now_et=now_et,
        runs=runs,
        observations=_cohort_observations(settings, members),
        expected_members=[cfg.identity for cfg in members],
        storage_kind=_storage_kind(settings),
        cohort_start=_cohort_start_session(store, cohort_id, members),
    )


def _alert_service(settings: Settings) -> cohort_alerts.CohortAlertService:
    return cohort_alerts.CohortAlertService(
        storage_factory.alert_store(settings),
        notify.build_notifier(settings),
        channel_live=settings.has_smtp,
    )


_STATE_STYLE = {
    cohort_ops.CohortState.COMPLETED: "green",
    cohort_ops.CohortState.PRE_CLOSE: "cyan",
    cohort_ops.CohortState.DUE: "cyan",
    cohort_ops.CohortState.CLOSED_SESSION: "dim",
    cohort_ops.CohortState.LATE: "yellow",
    cohort_ops.CohortState.AWAITING_DATA: "cyan",
    cohort_ops.CohortState.PARTIAL: "yellow",
    cohort_ops.CohortState.FAILED: "red",
    cohort_ops.CohortState.MISSED: "red",
    cohort_ops.CohortState.UNKNOWN: "red",
}


def _print_cohort_health(report: cohort_ops.CohortHealthReport) -> None:
    style = _STATE_STYLE.get(report.state, "white")
    session = report.session
    awaiting_auth = report.run is not None and report.run.awaiting_authentication
    if report.state is not cohort_ops.CohortState.AWAITING_DATA:
        state_label = report.state.value.upper()
    elif awaiting_auth:
        # Same non-terminal state, opposite instruction: this one needs a human, so it
        # must not read as the quiet provider wait an operator is trained to ignore.
        state_label = "AWAITING AUTHENTICATION — RETRYABLE"
        style = "yellow"
    else:
        state_label = "AWAITING PROVIDER DATA — RETRYABLE"
    console.print(
        f"[bold]{report.cohort_id}[/] [{style}]{state_label}[/] "
        f"({report.completed_count}/{report.expected_count} members)"
    )
    # Said before the numbers, because every figure below belongs to a closed record.
    lifecycle = cohort_lifecycle.status_for(report.cohort_id)
    if lifecycle.historical:
        console.print(f"[yellow]{lifecycle.label}[/] [dim]{lifecycle.reason}[/]")
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="dim")
    table.add_column()
    table.add_row("Now", f"{report.now_et:%Y-%m-%d %H:%M:%S ET}")
    close = "not a trading session"
    if session.close_et is not None:
        early = " (early close)" if session.is_early_close else ""
        close = f"{session.close_et:%H:%M ET}{early}"
    table.add_row("Session", f"{session.session_id} — closes {close}")
    if session.due_et is not None:
        table.add_row("Run due", f"{session.due_et:%Y-%m-%d %H:%M ET}")
    if report.schedule.grace_ends_et is not None:
        table.add_row("Preferred grace ends", f"{report.schedule.grace_ends_et:%Y-%m-%d %H:%M ET}")
    if report.schedule.deadline_et is not None:
        table.add_row("Retry deadline", f"{report.schedule.deadline_et:%Y-%m-%d %H:%M ET}")
    table.add_row("Verdict", f"{report.schedule.verdict.value} — {report.schedule.reason}")
    if report.run is not None:
        finished = (
            "not finished"
            if report.run.completed_at is None
            else f"{report.run.completed_at:%Y-%m-%d %H:%M UTC}"
        )
        late = " [yellow](after grace)[/]" if report.run.ran_late else ""
        table.add_row("Run", f"{report.run.status.value} — finished {finished}{late}")
    if report.observations is not None:
        obs = report.observations
        table.add_row(
            "Evidence",
            f"{obs.official} official / {obs.partial} partial / {obs.missing} missing",
        )
    if report.next_session is not None and report.next_session.session_date != session.session_date:
        table.add_row("Next session", report.next_session.session_id)
    table.add_row("Storage", report.storage_kind)
    console.print(table)
    for error in report.run.errors if report.run is not None else ():
        member = f" [{error.member_id}]" if error.member_id else ""
        console.print(f"[yellow]{error.code}{member}:[/] {error.message}")
    console.print(f"\n[bold]Next step:[/] {report.next_action}")

    if report.unresolved_history:
        scanned = (
            "" if report.history_scanned_from is None else f" since {report.history_scanned_from}"
        )
        console.print(
            f"\n[yellow]{len(report.unresolved_history)} other session(s) never delivered "
            f"complete evidence{scanned}.[/] These do not change the exit code above."
        )
        history = Table(show_header=True, box=None, pad_edge=False)
        history.add_column("Session", style="dim")
        history.add_column("State")
        history.add_column("Members", justify="right")
        for item in report.unresolved_history[:10]:
            history.add_row(
                item.session_date.isoformat(),
                item.state.value,
                f"{item.completed}/{item.expected}",
            )
        console.print(history)
        if len(report.unresolved_history) > 10:
            console.print(f"[dim]... and {len(report.unresolved_history) - 10} more.[/]")


def _print_alert_health(records: Sequence[cohort_alerts.CohortAlert]) -> None:
    """Show undelivered alert transitions up front — a silent alert is a real risk."""
    stuck = cohort_alerts.unresolved(records)
    if not stuck:
        return
    console.print(
        f"\n[bold yellow]Warning: {len(stuck)} cohort alert(s) were never confirmed "
        "delivered.[/] You may not have been told about a cohort problem."
    )
    for record in stuck[:10]:
        reason = record.failure_reason or record.delivery.value
        console.print(
            f"[yellow]  {record.scheduled_for} {record.kind.value}:[/] {reason} "
            f"(attempt {record.attempts})"
        )
    console.print("[dim]  Run 'schwab-trader cohort alerts' for the full record.[/]")


@cohort_app.command("health")
def cohort_health(
    cohort: str = typer.Option("", "--cohort", help="Cohort to report on (default: configured)."),
    as_json: bool = typer.Option(False, "--json", help="Emit the stable JSON contract instead."),
    notify_alerts: bool = typer.Option(
        False,
        "--notify",
        help="Also send a sanitized alert for a terminal problem state (at most once).",
    ),
    now: str = typer.Option(
        "",
        "--now",
        help="Eastern wall-clock instant to evaluate (ISO 8601), for rehearsing a verdict.",
    ),
) -> None:
    """Report whether the official cohort's scheduled run is on time (read-only).

    Answers, deterministically for a given Eastern instant: which XNYS session is
    relevant, whether its run is merely pending or actually missing, how many members
    recorded evidence, and the one thing to do next. It reads durable records only —
    it never runs a cohort, mutates state, or touches a broker path.

    Exit code 0 means on schedule, 1 means evidence is late/partial/failed/missing,
    and 2 means the question could not be answered.
    """
    settings = get_settings()
    setup_logging(settings)
    store = storage_factory.sleeve_store(settings)
    cohort_id, members = _resolve_cohort(settings, store, cohort)

    if now:
        try:
            now_et = datetime.fromisoformat(now)
        except ValueError:
            _fail("--now must be an ISO 8601 Eastern wall-clock instant, e.g. 2026-07-27T16:30.")
        if now_et.tzinfo is not None:
            _fail("--now must be naive Eastern wall-clock time, not an offset-aware timestamp.")
    else:
        now_et = market_calendar.eastern_now()

    report = _cohort_health_report(settings, store, cohort_id, members, now_et=now_et)

    outcomes: list[cohort_alerts.AlertOutcome] = []
    if notify_alerts and (kind := cohort_alerts.kind_for_state(report.state)) is not None:
        outcomes.append(_alert_service(settings).emit(kind, report))

    records = list(storage_factory.alert_store(settings).list(cohort_id=cohort_id, limit=100))

    if as_json:
        payload = cohort_ops.health_payload(report)
        payload["alerts"] = [cohort_alerts.outcome_payload(item) for item in outcomes]
        payload["alert_health"] = cohort_alerts.alert_health_payload(records)
        console.print_json(json.dumps(payload, sort_keys=True))
    else:
        _print_cohort_health(report)
        for outcome in outcomes:
            style = "yellow" if outcome.is_problem else "dim"
            console.print(
                f"[{style}]alert {outcome.kind.value}: {outcome.status.value} — {outcome.detail}[/]"
            )
        _print_alert_health(records)

    if report.exit_code != cohort_ops.EXIT_OK:
        raise typer.Exit(code=report.exit_code)


@cohort_app.command("readiness")
def cohort_readiness_check(
    cohort: str = typer.Option("", "--cohort", help="Cohort the scheduled job would run."),
    as_json: bool = typer.Option(False, "--json", help="Emit the stable JSON contract instead."),
    show_setup: bool = typer.Option(
        False, "--show-setup", help="Print the reviewed Task Scheduler commands without running."
    ),
) -> None:
    """Check whether this machine may safely run the scheduled cohort job.

    Verifies the one-writer rule, runtime configuration, cohort existence and
    reproducibility, durable-store compatibility, and notification readiness. Every
    check fails closed on ambiguity.

    This command **never** registers, enables, disables, or removes an operating-system
    scheduled task, and mutates nothing. Installing the task remains an explicit
    operator action after review; ``--show-setup`` only prints the commands to review.
    """
    settings = get_settings()
    setup_logging(settings)
    store = storage_factory.sleeve_store(settings)

    # An unresolved cohort is itself a reported failing check, not a crash: the
    # operator needs the rest of the checklist either way.
    chosen, members, _ = _find_cohort(settings, store, cohort)

    schema_error: str | None = None
    try:
        storage_factory.run_store(settings).list(cohort_id=chosen or None, limit=1)
    except Exception as exc:
        schema_error = type(exc).__name__

    alert_error: str | None = None
    try:
        storage_factory.alert_store(settings).list(cohort_id=chosen or None, limit=1)
    except Exception as exc:
        alert_error = type(exc).__name__

    # Read-only catalog inspection. The evidence store is otherwise only opened once a
    # five-minute fallback is already under way, which is far too late to learn that its
    # migration was never applied.
    missing_evidence: tuple[str, ...] = ()
    evidence_error: str | None = None
    try:
        missing_evidence = storage_factory.missing_market_data_evidence_tables(settings)
    except Exception as exc:
        evidence_error = type(exc).__name__

    report = cohort_readiness.assess_readiness(
        cohort_readiness.ReadinessFacts(
            cohort_id=chosen,
            writer_role=settings.cohort_writer,
            shared_database=settings.has_shared_database,
            storage_kind=_storage_kind(settings),
            member_count=len(members),
            reproducible_members=sum(1 for cfg in members if cfg.reproducible),
            schema_error=schema_error,
            alert_store_error=alert_error,
            missing_evidence_tables=missing_evidence,
            evidence_store_error=evidence_error,
            notifications_live=settings.has_smtp,
            kill_switch_engaged=safety.KillSwitch(settings.kill_switch_path).is_engaged(),
        )
    )

    if as_json:
        console.print_json(json.dumps(cohort_readiness.readiness_payload(report), sort_keys=True))
    else:
        table = Table(title=f"Scheduler readiness — {chosen or '(no cohort)'}")
        table.add_column("Check")
        table.add_column("Status")
        table.add_column("Detail")
        marks = {
            cohort_readiness.CheckStatus.PASS: "[green]pass[/]",
            cohort_readiness.CheckStatus.WARN: "[yellow]warn[/]",
            cohort_readiness.CheckStatus.FAIL: "[red]fail[/]",
        }
        for check in report.checks:
            table.add_row(check.name, marks[check.status], check.detail)
        console.print(table)
        for check in report.checks:
            if check.status is not cohort_readiness.CheckStatus.PASS and check.remedy:
                console.print(f"[dim]{check.name}:[/] {check.remedy}")
        console.print(f"\n[{'green' if report.ready else 'red'}]{report.summary}[/]")
        if show_setup:
            console.print(
                "\n[bold]Scheduled-task installation is an operator action.[/] This command "
                "does not register, enable, disable, or remove anything. Review "
                "docs/operations/cohort-scheduler-setup.md and run the PowerShell block "
                "there yourself on the writer machine."
            )

    if not report.ready:
        raise typer.Exit(code=cohort_readiness.EXIT_NOT_READY)


@cohort_app.command("alerts")
def cohort_alerts_list(
    cohort: str = typer.Option("", "--cohort", help="Cohort to list alert transitions for."),
    limit: int = typer.Option(20, "--limit", min=1, max=200),
) -> None:
    """Show recorded cohort alert transitions (read-only, sanitized).

    Useful for answering "was I told about that?" and for spotting a transition stuck
    in ``pending``, which means a previous attempt never recorded its delivery outcome
    and was deliberately not resent.
    """
    settings = get_settings()
    setup_logging(settings)
    store = storage_factory.sleeve_store(settings)
    cohort_id, _ = _resolve_cohort(settings, store, cohort)
    records = storage_factory.alert_store(settings).list(cohort_id=cohort_id, limit=limit)
    if not records:
        console.print("[dim]No cohort alert transitions recorded.[/]")
        return
    table = Table(title=f"Cohort alerts — {cohort_id}")
    table.add_column("Session")
    table.add_column("Kind")
    table.add_column("Delivery")
    table.add_column("Attempts", justify="right")
    table.add_column("Detail")
    for record in records:
        style = {
            cohort_alerts.AlertDelivery.SENT: "green",
            cohort_alerts.AlertDelivery.PENDING: "yellow",
            cohort_alerts.AlertDelivery.FAILED: "red",
        }[record.delivery]
        table.add_row(
            record.scheduled_for.isoformat(),
            record.kind.value,
            f"[{style}]{record.delivery.value}[/]",
            str(record.attempts),
            record.failure_reason or record.detail or "",
        )
    console.print(table)


# --- 30-session accounting review and operator decisions (#63) --------------
#
# The CLI is the canonical *write* path for review records; the dashboard presents them
# read-only. Every command here reads durable records, validates against them, and
# appends. None of them runs a cohort, changes a sleeve definition, moves a paper
# position, promotes anything, or reaches an order path.


def _review_context(
    settings: Settings,
    store: sleeves.SleeveStore,
    cohort_id: str,
    members: Sequence[sleeves.SleeveConfig],
    *,
    now_et: datetime,
) -> cohort_review.ReviewContext:
    """Build the validation context from this cohort's authoritative records."""
    runs = list(storage_factory.run_store(settings).list(cohort_id=cohort_id, limit=10_000))
    return cohort_review.build_context(
        cohort_id=cohort_id,
        configs=list(members),
        runs=runs,
        observations=_cohort_observations(settings, members),
        now_et=now_et,
        start_session=_cohort_start_session(store, cohort_id, members),
    )


def _review_service(settings: Settings) -> cohort_review.CohortReviewService:
    missing = storage_factory.missing_cohort_review_tables(settings)
    if missing:
        _fail(
            "The cohort review tables are missing from the shared database "
            f"({', '.join(missing)}). Apply the Alembic migration first; this command "
            "will not create them implicitly."
        )
    return cohort_review.CohortReviewService(storage_factory.cohort_review_store(settings))


def _review_sleeve(members: Sequence[sleeves.SleeveConfig], reference: str) -> str:
    """Resolve a sleeve name or stable id to the identity the records are keyed by.

    An ambiguous display name is refused rather than resolved to the first match: two
    cohorts can hold sleeves with the same name, and recording a decision against the
    wrong one is exactly the kind of quiet error the audit trail cannot undo.
    """
    wanted = reference.strip()
    if not wanted:
        _fail("--sleeve is required.")
    exact = [cfg for cfg in members if cfg.identity == wanted]
    if exact:
        return exact[0].identity
    named = [cfg for cfg in members if cfg.name == wanted]
    if len(named) == 1:
        return named[0].identity
    if not named:
        known = ", ".join(sorted(cfg.name for cfg in members)) or "none"
        _fail(f"Unknown sleeve {wanted!r} in this cohort. Members: {known}")
    _fail(f"Sleeve name {wanted!r} is ambiguous in this cohort; pass its stable id instead.")


def _review_now() -> datetime:
    """The recorded-at instant. Always timezone-aware; the store refuses a naive one."""
    return datetime.now(UTC)


def _review_clock(now: str) -> datetime:
    if not now:
        return market_calendar.eastern_now()
    try:
        parsed = datetime.fromisoformat(now)
    except ValueError:
        _fail("--now must be an ISO 8601 Eastern wall-clock instant, e.g. 2026-09-04T18:00.")
    if parsed.tzinfo is not None:
        _fail("--now must be naive Eastern wall-clock time, not an offset-aware timestamp.")
    return parsed


def _observation_key(
    context: cohort_review.ReviewContext, sleeve_id: str, session: str
) -> str:
    """Resolve --sleeve/--session to the official observation identity it names."""
    try:
        session_date = date.fromisoformat(session)
    except ValueError:
        _fail("--session must be an ISO date, e.g. 2026-09-04.")
    key = evaluation.official_observation_key(context.cohort_id, sleeve_id, session_date)
    if key not in context.observations:
        _fail(
            f"No due official observation exists for that sleeve on {session_date.isoformat()}. "
            "Run 'cohort review pending' to see what is reviewable."
        )
    return key


def _print_review(review: cohort_review.CohortReview, names: Mapping[str, str]) -> None:
    """Human-readable review summary. Deliberately says what is *not* recorded too."""
    label = names.get
    # Escaped: Rich reads square brackets as style tags and would silently delete an id
    # that contained one.
    console.print(
        f"[bold]Cohort review — {markup.escape(review.cohort_id)}[/]\n"
        f"Completed due sessions: {review.completed_due_sessions}/{review.review_target} "
        f"({'review due' if review.review_due else 'review not due yet'})\n"
        f"Reviewed observations: {review.reviewed_observations}/{review.official_observations}\n"
        f"Unexplained differences: {len(review.unexplained_differences)}\n"
        f"Sleeves with a current decision: {len(review.decided_sleeves)}"
    )
    if review.differences:
        table = Table(title="Accounting differences")
        table.add_column("Session")
        table.add_column("Sleeve")
        table.add_column("Area")
        table.add_column("Explained")
        table.add_column("Summary")
        for item in review.differences:
            table.add_row(
                item.session_date.isoformat(),
                markup.escape(label(item.sleeve_id, item.sleeve_id)),
                item.area.value,
                "[green]yes[/]" if item.explained else "[red]no[/]",
                markup.escape(item.summary or ""),
            )
        console.print(table)
    if review.decisions:
        table = Table(title="Current operator decisions")
        table.add_column("Sleeve")
        table.add_column("Action")
        table.add_column("Revision", justify="right")
        table.add_column("Rationale")
        for decision in review.decisions:
            table.add_row(
                markup.escape(label(decision.sleeve_id, decision.sleeve_id)),
                decision.action.value,
                str(decision.revision),
                markup.escape(decision.rationale),
            )
        console.print(table)
    if review.notes:
        console.print(f"\n[bold]Notes ({len(review.notes)})[/]")
        for entry in review.notes:
            target = (
                ""
                if entry.sleeve_id is None
                else f" ({label(entry.sleeve_id, entry.sleeve_id)})"
            )
            console.print(
                f"[dim]{entry.recorded_at:%Y-%m-%d %H:%M UTC}{target}:[/] "
                f"{markup.escape(entry.note)}"
            )
    if review.superseded_checks or review.superseded_decisions:
        console.print(
            f"\n[dim]{len(review.superseded_checks)} superseded review entries and "
            f"{len(review.superseded_decisions)} superseded decisions are preserved for "
            "audit; use --json to read them.[/]"
        )
    console.print(
        "\n[dim]A recorded decision is a research disposition about a paper experiment. "
        "It does not promote, pause, retire, or reconfigure a sleeve, and it never "
        "authorizes live trading.[/]"
    )


review_app = typer.Typer(
    help="Record and inspect the 30-session accounting review and operator decisions.",
    no_args_is_help=True,
)
cohort_app.add_typer(review_app, name="review")


@review_app.command("show")
def cohort_review_show(
    cohort: str = typer.Option("", "--cohort", help="Cohort to show the review for."),
    as_json: bool = typer.Option(False, "--json", help="Emit the stable JSON contract."),
    now: str = typer.Option(
        "", "--now", help="Eastern wall-clock instant to evaluate (ISO 8601), for rehearsal."
    ),
) -> None:
    """Show the current accounting review and operator decisions (read-only).

    Current entries are the highest revision for each identity. Superseded entries are
    never deleted; they are included in ``--json`` so a correction stays auditable.
    """
    settings = get_settings()
    setup_logging(settings)
    store = storage_factory.sleeve_store(settings)
    cohort_id, members = _resolve_cohort(settings, store, cohort)
    context = _review_context(settings, store, cohort_id, members, now_et=_review_clock(now))
    review = _review_service(settings).review(context)
    if as_json:
        console.print_json(json.dumps(cohort_review.review_payload(review), sort_keys=True))
        return
    _print_review(review, {cfg.identity: cfg.name for cfg in members})


@review_app.command("pending")
def cohort_review_pending(
    cohort: str = typer.Option("", "--cohort", help="Cohort to report pending work for."),
    as_json: bool = typer.Option(False, "--json", help="Emit the stable JSON contract."),
    limit: int = typer.Option(20, "--limit", min=1, max=1000, help="Rows to print."),
    now: str = typer.Option(
        "", "--now", help="Eastern wall-clock instant to evaluate (ISO 8601), for rehearsal."
    ),
) -> None:
    """List the review work this cohort still owes (read-only).

    Three kinds of outstanding work: official observations with no complete accounting
    check, differences recorded without an explanation, and member sleeves with no
    current operator decision.

    Exit code 0 means nothing is outstanding *or* the review is not due yet; 1 means the
    review is due and work remains.
    """
    settings = get_settings()
    setup_logging(settings)
    store = storage_factory.sleeve_store(settings)
    cohort_id, members = _resolve_cohort(settings, store, cohort)
    names = {cfg.identity: cfg.name for cfg in members}
    context = _review_context(settings, store, cohort_id, members, now_et=_review_clock(now))
    review = _review_service(settings).review(context)
    undecided = sorted(context.member_sleeve_ids - review.decided_sleeves)

    if as_json:
        payload = cohort_review.review_payload(review)
        payload["undecided_sleeves"] = undecided
        console.print_json(json.dumps(payload, sort_keys=True))
    else:
        console.print(
            f"[bold]Pending review work — {markup.escape(cohort_id)}[/]\n"
            f"Unreviewed observations: {len(review.pending_observations)}\n"
            f"Unexplained differences: {len(review.unexplained_differences)}\n"
            f"Sleeves without a current decision: {len(undecided)}"
        )
        if review.pending_observations:
            table = Table(title=f"Unreviewed observations (showing up to {limit})")
            table.add_column("Session")
            table.add_column("Sleeve")
            table.add_column("Missing areas")
            for item in review.pending_observations[:limit]:
                table.add_row(
                    item.session_date.isoformat(),
                    markup.escape(names.get(item.sleeve_id, item.sleeve_id)),
                    ", ".join(area.value for area in item.missing_areas),
                )
            console.print(table)
        for entry in review.unexplained_differences:
            console.print(
                f"[yellow]Unexplained {entry.area.value} difference[/] "
                f"{entry.session_date.isoformat()} "
                f"{markup.escape(names.get(entry.sleeve_id, entry.sleeve_id))}: "
                f"{markup.escape(entry.summary or '')}"
            )
        if undecided:
            listed = ", ".join(markup.escape(names.get(item, item)) for item in undecided)
            console.print(f"[yellow]No current decision:[/] {listed}")
        if not review.review_due:
            console.print(
                f"\n[dim]The review is not due yet ({review.completed_due_sessions} of "
                f"{review.review_target} completed due sessions). Accounting checks and "
                "notes may be recorded now; a final decision may not.[/]"
            )

    outstanding = bool(review.pending_observations or review.unexplained_differences or undecided)
    if review.review_due and outstanding:
        raise typer.Exit(code=1)


@review_app.command("record")
def cohort_review_record(
    sleeve: str = typer.Option(..., "--sleeve", help="Cohort member (name or stable id)."),
    session: str = typer.Option(..., "--session", help="Official session date (YYYY-MM-DD)."),
    area: str = typer.Option(..., "--area", help="Accounting area: cash, positions, valuation."),
    finding: str = typer.Option(
        "matched", "--finding", help="matched (reconciles) or difference (does not)."
    ),
    summary: str = typer.Option("", "--summary", help="What differed. Required for a difference."),
    explanation: str = typer.Option(
        "", "--explanation", help="Why the difference is understood. Clears it for the gate."
    ),
    supersede: bool = typer.Option(
        False,
        "--supersede",
        help="Authorize correcting an existing entry. The original is preserved.",
    ),
    recorded_by: str = typer.Option(
        cohort_review.DEFAULT_RECORDED_BY, "--by", help="Role label recorded as the author."
    ),
    cohort: str = typer.Option("", "--cohort", help="Cohort the observation belongs to."),
    as_json: bool = typer.Option(False, "--json", help="Emit the written record as JSON."),
    now: str = typer.Option(
        "", "--now", help="Eastern wall-clock instant to evaluate (ISO 8601), for rehearsal."
    ),
) -> None:
    """Record one accounting area of one official observation as checked.

    Idempotent: re-running with the same finding, summary, and explanation reports
    ``unchanged`` and writes nothing. Changing any of them requires ``--supersede``,
    which appends a new revision and leaves the earlier entry exactly as it was.

    A difference without an explanation is recorded honestly and keeps failing the gate.
    That is the point — an unexplained difference is what the review exists to surface.
    """
    settings = get_settings()
    setup_logging(settings)
    store = storage_factory.sleeve_store(settings)
    cohort_id, members = _resolve_cohort(settings, store, cohort)
    context = _review_context(settings, store, cohort_id, members, now_et=_review_clock(now))
    sleeve_id = _review_sleeve(members, sleeve)
    key = _observation_key(context, sleeve_id, session)
    try:
        parsed_area = cohort_review.parse_area(area)
        parsed_finding = cohort_review.parse_finding(finding)
        outcome = _review_service(settings).record_check(
            context,
            observation_key=key,
            area=parsed_area,
            finding=parsed_finding,
            summary=summary or None,
            explanation=explanation or None,
            recorded_at=_review_now(),
            recorded_by=recorded_by,
            allow_supersede=supersede,
        )
    except cohort_review.CohortReviewError as exc:
        _fail(exc)

    record = outcome.record
    if as_json:
        console.print_json(
            json.dumps(
                {
                    "status": outcome.status.value,
                    "entry_id": record.entry_id,
                    "revision": record.revision,
                },
                sort_keys=True,
            )
        )
        return
    console.print(
        f"[green]{outcome.status.value}[/]: {record.area.value} for "
        f"{markup.escape(record.observation_key)} recorded as {record.finding.value} "
        f"(revision {record.revision})."
    )
    if record.finding is cohort_review.ReviewFinding.DIFFERENCE and not record.explained:
        console.print(
            "[yellow]This difference has no explanation, so the operational gate still "
            "fails on it.[/] Re-run with --supersede and --explanation once it is understood."
        )


@review_app.command("note")
def cohort_review_note(
    note: str = typer.Option(..., "--note", help="The note text."),
    sleeve: str = typer.Option("", "--sleeve", help="Optional member the note is about."),
    session: str = typer.Option(
        "", "--session", help="Optional official session the note is about (YYYY-MM-DD)."
    ),
    recorded_by: str = typer.Option(
        cohort_review.DEFAULT_RECORDED_BY, "--by", help="Role label recorded as the author."
    ),
    cohort: str = typer.Option("", "--cohort", help="Cohort the note belongs to."),
    as_json: bool = typer.Option(False, "--json", help="Emit the written record as JSON."),
    now: str = typer.Option(
        "", "--now", help="Eastern wall-clock instant to evaluate (ISO 8601), for rehearsal."
    ),
) -> None:
    """Attach a durable note to the cohort, a member sleeve, or an observation.

    Notes are for investigation context an operator will need later and would otherwise
    keep in chat. An identical note about the same target is a no-op rather than a
    duplicate; any change in wording is a new note and the original is kept.

    Never put a credential, token, account number, or connection string in a note: these
    records are read by the dashboard and by ``--json`` tooling.
    """
    settings = get_settings()
    setup_logging(settings)
    store = storage_factory.sleeve_store(settings)
    cohort_id, members = _resolve_cohort(settings, store, cohort)
    context = _review_context(settings, store, cohort_id, members, now_et=_review_clock(now))
    sleeve_id = _review_sleeve(members, sleeve) if sleeve else None
    key: str | None = None
    if session:
        if sleeve_id is None:
            _fail("--session names an observation, so it requires --sleeve too.")
        key = _observation_key(context, sleeve_id, session)
    try:
        outcome = _review_service(settings).add_note(
            context,
            note=note,
            sleeve_id=sleeve_id,
            observation_key=key,
            recorded_at=_review_now(),
            recorded_by=recorded_by,
        )
    except cohort_review.CohortReviewError as exc:
        _fail(exc)

    if as_json:
        console.print_json(
            json.dumps(
                {"status": outcome.status.value, "note_id": outcome.record.note_id},
                sort_keys=True,
            )
        )
        return
    console.print(f"[green]{outcome.status.value}[/]: note {outcome.record.note_id[:12]} recorded.")


@review_app.command("decide")
def cohort_review_decide(
    sleeve: str = typer.Option(..., "--sleeve", help="Cohort member (name or stable id)."),
    action: str = typer.Option(..., "--action", help="keep, modify, pause, or retire."),
    rationale: str = typer.Option(..., "--rationale", help="Why. Required and never blank."),
    supersede: bool = typer.Option(
        False,
        "--supersede",
        help="Authorize replacing an existing decision. The original is preserved.",
    ),
    recorded_by: str = typer.Option(
        cohort_review.DEFAULT_RECORDED_BY, "--by", help="Role label recorded as the author."
    ),
    cohort: str = typer.Option("", "--cohort", help="Cohort the sleeve belongs to."),
    as_json: bool = typer.Option(False, "--json", help="Emit the written record as JSON."),
    now: str = typer.Option(
        "", "--now", help="Eastern wall-clock instant to evaluate (ISO 8601), for rehearsal."
    ),
) -> None:
    """Record the final keep/modify/pause/retire disposition for one sleeve.

    Refused before the formal review is due: the gate reads a recorded decision as an
    operator having judged 30 sessions of evidence, so accepting one earlier would let a
    cohort satisfy that rule without the evidence existing.

    The disposition is a *research* judgement about a paper experiment. It does not
    promote, pause, retire, or reconfigure the running sleeve, does not change cohort
    membership, and does not authorize live trading. Acting on it stays a separate,
    explicit operator step.
    """
    settings = get_settings()
    setup_logging(settings)
    store = storage_factory.sleeve_store(settings)
    cohort_id, members = _resolve_cohort(settings, store, cohort)
    context = _review_context(settings, store, cohort_id, members, now_et=_review_clock(now))
    sleeve_id = _review_sleeve(members, sleeve)
    try:
        outcome = _review_service(settings).record_decision(
            context,
            sleeve_id=sleeve_id,
            action=cohort_review.parse_action(action),
            rationale=rationale,
            recorded_at=_review_now(),
            recorded_by=recorded_by,
            allow_supersede=supersede,
        )
    except cohort_review.CohortReviewError as exc:
        _fail(exc)

    record = outcome.record
    if as_json:
        console.print_json(
            json.dumps(
                {
                    "status": outcome.status.value,
                    "decision_id": record.decision_id,
                    "revision": record.revision,
                    "action": record.action.value,
                },
                sort_keys=True,
            )
        )
        return
    console.print(
        f"[green]{outcome.status.value}[/]: "
        f"{markup.escape(_sleeve_label(members, record.sleeve_id))} recorded as "
        f"{record.action.value} (revision {record.revision})."
    )
    console.print(
        "[dim]Research disposition only. Nothing was promoted, paused, retired, or "
        "reconfigured, and live trading remains unauthorized.[/]"
    )


def _sleeve_label(members: Sequence[sleeves.SleeveConfig], sleeve_id: str) -> str:
    return next((cfg.name for cfg in members if cfg.identity == sleeve_id), sleeve_id)


# --- challenger-v1 bootstrap (#95) ------------------------------------------
#
# Deliberately a separate command group from `cohort`, and a separate module from
# `scripts/bootstrap_paper_cohort.py`. The July cohorts' template, identity, and hashes
# are not reachable from here, and nothing in this group can modify, rename, supersede,
# or re-run them.


def _challenger_plan(start_session: str, cohort_id: str) -> challenger_cohort.ChallengerPlan:
    """Parse the operator's arguments into a validated plan, or fail cleanly."""
    try:
        parsed = date.fromisoformat(start_session)
    except ValueError:
        _fail("--start-session must be an ISO date, e.g. 2026-08-17.")
    try:
        return challenger_cohort.build_plan(start_session=parsed, cohort_id=cohort_id or None)
    except ValueError as exc:
        _fail(str(exc))


@challenger_app.command("preview")
def challenger_preview(
    start_session: str = typer.Option(
        ...,
        "--start-session",
        help="Explicit current/future XNYS session (YYYY-MM-DD). Historical dates are refused.",
    ),
    cohort_id: str = typer.Option(
        "", "--cohort-id", help="Stable cohort id (default: challenger-v1-<start-session>)."
    ),
) -> None:
    """Print the complete challenger-v1 desired state. Performs zero persistent writes.

    Read-only in the strongest sense: it opens no store, creates no directory or
    database, and touches no existing cohort. Run it as many times as you like — the
    output is deterministic for a given start session, so two runs are diffable.
    """
    plan = _challenger_plan(start_session, cohort_id)
    console.print_json(challenger_cohort.render(challenger_cohort.preview(plan)))
    console.print("[dim]PREVIEW COMPLETE: zero persistent writes were performed.[/]")


@challenger_app.command("create")
def challenger_create(
    start_session: str = typer.Option(
        ...,
        "--start-session",
        help="Explicit current/future XNYS session (YYYY-MM-DD). Historical dates are refused.",
    ),
    cohort_id: str = typer.Option(
        "", "--cohort-id", help="Stable cohort id (default: challenger-v1-<start-session>)."
    ),
) -> None:
    """Print the full preview, then idempotently create the five missing sleeve records.

    Atomic and create-if-absent. Every conflict is detected before the first record is
    written, and a failure part-way through rolls back what this invocation created, so
    the command either produces the whole cohort or produces nothing. Repeating it is a
    no-op that reports ``already-exists``.

    There is deliberately no force, repair, or partial-create option: a cohort whose
    stored configuration disagrees with the requested one needs a new id, because
    mutating a running experiment destroys what it was measuring.
    """
    settings = get_settings()
    setup_logging(settings)
    plan = _challenger_plan(start_session, cohort_id)
    console.print_json(challenger_cohort.render(challenger_cohort.preview(plan)))

    store = storage_factory.sleeve_store(settings)
    try:
        result = challenger_cohort.create(plan, store=store)
    except (challenger_cohort.ChallengerConflictError, sleeves.SleeveExists, OSError) as exc:
        _fail(exc)
    console.print_json(challenger_cohort.render(result.payload()))
    console.print(
        f"[green]{plan.cohort_id}[/]: "
        f"{len(result.created)} created, {len(result.existing)} already present. "
        "Nothing is scheduled or run by this command."
    )


@challenger_app.command("inspect")
def challenger_inspect(
    cohort_id: str = typer.Option(..., "--cohort-id", help="Challenger cohort to inspect."),
) -> None:
    """Print the exact stored manifest, sleeve records, and configuration hashes."""
    settings = get_settings()
    setup_logging(settings)
    store = storage_factory.sleeve_store(settings)
    try:
        payload = challenger_cohort.inspect_cohort(store=store, cohort_id=cohort_id)
    except FileNotFoundError as exc:
        _fail(exc)
    console.print_json(challenger_cohort.render(payload))


@sleeve_app.command("create")
def sleeve_create(
    name: str = typer.Argument(..., help="Sleeve name (letters/digits/-/_)."),
    strategy: str = typer.Option(
        ..., "--strategy", help=f"One of: {', '.join(_SLEEVE_STRATEGIES)}."
    ),
    symbols: str = typer.Option(
        "", "--symbols", help="Universe override: a preset (e.g. large-cap) or CSV tickers."
    ),
    cash: str = typer.Option("", "--cash", help="Starting cash (default: paper sleeve size)."),
    max_positions: int = typer.Option(0, "--max-positions", min=0, help="0 = config default."),
    max_fraction: str = typer.Option("", "--max-fraction", help="Max fraction per name (llm)."),
    t1_settlement: bool = typer.Option(
        False, "--t1/--no-t1", help="Model T+1 settled-cash (realistic for a cash account)."
    ),
    leverage: str = typer.Option(
        "1", "--leverage", help="Buying-power multiplier (1 = cash; 2 = Reg T margin)."
    ),
    factor: str = typer.Option(
        "",
        "--factor",
        help=f"For --strategy fundamental: one of {', '.join(fundamentals.FACTORS)}.",
    ),
) -> None:
    """Create a comparison sleeve running one strategy in an isolated portfolio."""
    settings = get_settings()
    setup_logging(settings)
    if strategy not in _SLEEVE_STRATEGIES:
        _fail(f"Unknown strategy '{strategy}'. Choose: {', '.join(_SLEEVE_STRATEGIES)}")
    try:
        starting = Decimal(cash) if cash else settings.paper_starting_cash
        fraction = Decimal(max_fraction) if max_fraction else settings.agent_max_position_fraction
        lev = Decimal(leverage)
    except InvalidOperation:
        _fail("Invalid --cash, --max-fraction, or --leverage value.")
    if lev < 1:
        _fail("--leverage must be >= 1 (1 = cash account, 2 = Reg T margin).")
    if lev > 1 and t1_settlement:
        _fail("--t1 (cash-account settlement) and --leverage > 1 (margin) are mutually exclusive.")
    if strategy in (agent.FUNDAMENTAL_STRATEGY_NAME, agent.VALUE_MOMENTUM_NAME):
        default_factor = (
            "book-to-market" if strategy == agent.FUNDAMENTAL_STRATEGY_NAME else "earnings-yield"
        )
        factor = factor or default_factor
        # value-momentum accepts a comma-separated blend (e.g. earnings-yield,roe).
        chosen = [f.strip() for f in factor.split(",") if f.strip()]
        if strategy == agent.FUNDAMENTAL_STRATEGY_NAME and len(chosen) != 1:
            _fail("--factor must be a single factor for the fundamental strategy.")
        bad = [f for f in chosen if f not in fundamentals.FACTORS]
        if bad:
            _fail(f"Invalid --factor {bad}. Choose from: {', '.join(fundamentals.FACTORS)}.")
        if storage_factory.sec_store(settings).total_facts() == 0:
            _fail(
                f"The '{strategy}' strategy needs EDGAR data. "
                "Run 'schwab-trader edgar fetch' first."
            )
    elif strategy == agent.POST_EARNINGS_DRIFT_NAME:
        if factor:
            _fail("--factor does not apply to post-earnings-drift.")
        if storage_factory.sec_store(settings).total_facts() == 0:
            _fail(
                "The 'post-earnings-drift' strategy needs EDGAR data. "
                "Run 'schwab-trader edgar fetch' first."
            )
    elif factor:
        _fail("--factor only applies to --strategy fundamental or value-momentum.")

    positions = max_positions or settings.agent_max_positions
    universe = _expand_symbols(symbols)
    store = storage_factory.sleeve_store(settings)
    try:
        cfg = store.create(
            name,
            strategy=strategy,
            universe=universe,
            starting_cash=starting,
            max_positions=positions,
            max_position_fraction=fraction,
            settlement_t1=t1_settlement,
            leverage=lev,
            factor=factor,
        )
    except (sleeves.SleeveExists, sleeves.SleeveNameError) as exc:
        _fail(exc)
    notes = ""
    if cfg.settlement_t1:
        notes += " [T+1 settlement]"
    if cfg.leverage > 1:
        notes += f" [{cfg.leverage}x margin]"
    if cfg.factor:
        notes += f" [factor: {cfg.factor}]"
    console.print(
        f"[green]Created sleeve '{cfg.name}'[/] "
        f"({cfg.strategy}, {_money(cfg.starting_cash)}){notes}."
    )


@sleeve_app.command("list")
def sleeve_list() -> None:
    """List all sleeves and their configs."""
    settings = get_settings()
    setup_logging(settings)
    items = storage_factory.sleeve_store(settings).list()
    if not items:
        console.print("[dim]No sleeves yet. Create one with 'schwab-trader sleeve create'.[/]")
        return
    table = Table(title="Sleeves", show_header=True)
    table.add_column("sleeve_id")
    table.add_column("name", style="cyan")
    table.add_column("cohort")
    table.add_column("strategy")
    table.add_column("cash", justify="right")
    table.add_column("T+1")
    table.add_column("margin")
    table.add_column("universe")
    for cfg in items:
        universe = ", ".join(cfg.universe) if cfg.universe else "(default)"
        table.add_row(
            cfg.sleeve_id,
            cfg.name,
            cfg.cohort_id or "legacy/unassigned",
            cfg.strategy,
            _money(cfg.starting_cash),
            "yes" if cfg.settlement_t1 else "-",
            f"{cfg.leverage}x" if cfg.leverage > 1 else "-",
            universe,
        )
    console.print(table)


@sleeve_app.command("remove")
def sleeve_remove(
    name: str = typer.Argument(..., help="Sleeve name or stable sleeve ID."),
    cohort: str = typer.Option(
        "",
        "--cohort",
        help="Cohort scope required when the name is ambiguous.",
    ),
) -> None:
    """Delete a sleeve and its data."""
    settings = get_settings()
    setup_logging(settings)
    store = storage_factory.sleeve_store(settings)
    try:
        config = store.resolve(name, cohort_id=cohort or None)
    except LookupError as exc:
        _fail(exc)
    if config is not None and store.remove(config.identity):
        console.print(f"[green]Removed sleeve '{config.name}'.[/]")
    else:
        _fail(f"No sleeve named '{name}'.")


@sleeve_app.command("run")
def sleeve_run(
    official: bool = typer.Option(
        False,
        "--official",
        help="Run durable scheduled cohort orchestration instead of a legacy cycle.",
    ),
    cohort: str = typer.Option("", "--cohort", help="Run every member of this cohort."),
    scheduled_for: str = typer.Option(
        "",
        "--scheduled-for",
        help="Explicit exchange session date; calendar rules still apply.",
    ),
    name: str = typer.Argument("", help="Sleeve to run (omit when using --all/--match)."),
    all_sleeves: bool = typer.Option(False, "--all", help="Run one cycle on every sleeve."),
    match: str = typer.Option(
        "", "--match", help="Run every sleeve whose name contains this text (shares quotes)."
    ),
) -> None:
    """Run one decision cycle on a sleeve (or a group), sharing quotes across sleeves."""
    settings = get_settings()
    setup_logging(settings)
    store = storage_factory.sleeve_store(settings)
    if cohort:
        if name or match:
            _fail("--cohort cannot be combined with a sleeve name or --match.")
        targets = [cfg for cfg in store.list() if cfg.cohort_id == cohort]
        if not targets:
            _fail(f"No sleeves belong to cohort {cohort!r}.")
        # Naming a superseded cohort is refused rather than quietly skipped: the operator
        # asked for this one specifically, and needs to know why it will not run.
        refusal = cohort_lifecycle.run_refusal(cohort)
        if refusal:
            _fail(refusal)
    elif all_sleeves:
        targets = store.list()
    elif match:
        targets = [cfg for cfg in store.list() if match.lower() in cfg.name.lower()]
        if not targets:
            _fail(f"No sleeves match '{match}'.")
    elif name:
        try:
            found = store.resolve(name)
        except LookupError as exc:
            _fail(exc)
        if found is None:
            _fail(f"No sleeve named '{name}'.")
        targets = [found]
    else:
        _fail("Give a sleeve name, --match, or --all.")
    if official or cohort or scheduled_for:
        if not targets:
            _fail("No sleeves are available for an official cohort run.")
        scheduled_session: date | None = None
        if scheduled_for:
            try:
                scheduled_session = date.fromisoformat(scheduled_for)
            except ValueError:
                _fail("--scheduled-for must use YYYY-MM-DD.")
        if not cohort and not all_sleeves:
            cohort_ids = {cfg.cohort_id for cfg in targets if cfg.cohort_id}
            targets = [cfg for cfg in store.list() if cfg.cohort_id in cohort_ids]
        # A broad selection ('--all' is what the scheduled job uses) drops superseded
        # cohorts and carries on with the rest. Refusing the whole invocation because a
        # withdrawn experiment still has rows would stop the running collection instead.
        retired = sorted(
            {cfg.cohort_id for cfg in targets if cohort_lifecycle.is_historical(cfg.cohort_id)}
        )
        if retired:
            targets = [
                cfg for cfg in targets if not cohort_lifecycle.is_historical(cfg.cohort_id)
            ]
            console.print(
                f"[yellow]Skipping superseded cohort(s): {', '.join(retired)}.[/] "
                "Their records are kept unchanged and are never re-run."
            )
        if not targets:
            _fail(
                "Every selected cohort is superseded; nothing is eligible to run. "
                "Bootstrap or select the active cohort instead."
            )
        _run_official_sleeve_cohorts(
            settings,
            store,
            targets,
            scheduled_for=scheduled_session,
        )
        return

    if not targets:
        _fail("No sleeves to run. Create one with 'schwab-trader sleeve create'.")

    # An unofficial cycle still writes a paper account and an evaluation row, so a
    # superseded cohort's members are off limits here too.
    targets = _exclude_superseded_sleeves(targets, named=bool(name))

    on_usage, cost_tally = _usage_recorder(settings, "execution")
    spec = storage_factory.research_store(settings).latest()
    hist_cache = history_cache.HistoryCache(settings.history_cache_dir)

    try:
        with _build_client(settings) as client:
            quote_cache: dict[str, market_data.Quote | None] = {}

            def source(symbol: str) -> market_data.Quote:
                if symbol not in quote_cache:
                    try:
                        quote_cache[symbol] = market_data.get_quote(client, symbol)
                    except market_data.QuoteError:
                        quote_cache[symbol] = None
                quote = quote_cache[symbol]
                if quote is None:
                    raise market_data.QuoteError(f"No quote for {symbol}.")
                return quote

            for cfg in targets:
                strat = _build_sleeve_strategy(settings, cfg, spec, on_usage, client, hist_cache)
                engine = storage_factory.paper_engine(settings, cfg)
                report = agent.AgentRunner(strat, engine, source).run_cycle()
                storage_factory.evaluation_store(settings, cfg).record_cycle(report)
                valuation = report.valuation
                console.print(
                    f"[cyan]{cfg.name}[/] ({cfg.strategy}): "
                    f"filled {report.num_filled}/rej {report.num_rejected} | "
                    f"{_money(valuation.total_value)} ({valuation.total_return_pct:+.2f}%)"
                )
    except (oauth.OAuthError, api.ApiError, llm_strategy.LLMError) as exc:
        _fail(exc)

    if cost_tally[0] > 0:
        console.print(f"[dim]API cost this run: {_cost(cost_tally[0])}.[/]")


@sleeve_app.command("watch")
def sleeve_watch(
    match: str = typer.Option(
        "intraday", "--match", help="Watch sleeves whose name contains this text."
    ),
    interval: float = typer.Option(
        15.0, "--interval", min=1.0, help="Seconds between evaluations (fresh quotes each tick)."
    ),
    duration: float = typer.Option(
        0.0, "--duration", min=0.0, help="Minutes to run (0 = until the market close)."
    ),
    force: bool = typer.Option(
        False, "--force", help="Run even outside regular market hours (for testing)."
    ),
    once: bool = typer.Option(False, "--once", help="Evaluate a single tick and exit."),
) -> None:
    """Continuously re-evaluate the intraday sleeves on fresh batched quotes (a daemon).

    One long-running loop: every ``--interval`` seconds it batch-fetches quotes for the
    watched sleeves' combined universe in a *single* request, then runs one decision
    cycle per sleeve against that shared snapshot. This is the event-driven form of
    ``sleeve run`` for the high-turnover ``intraday`` strategy - leave it running during
    the session instead of scheduling repeated one-shot runs.

    Paper only; it never places a live order. Stops at the market close, after
    ``--duration``, on ``--once``, or on Ctrl-C.
    """
    settings = get_settings()
    setup_logging(settings)
    store = storage_factory.sleeve_store(settings)
    targets = [cfg for cfg in store.list() if match.lower() in cfg.name.lower()]
    if not targets:
        _fail(f"No sleeves match '{match}'. Create one with 'schwab-trader sleeve create'.")

    # `--match` is a substring, so it happily sweeps in a retired cohort's sleeves; this
    # loop records a cycle per tick, which must never land on incident evidence.
    targets = _exclude_superseded_sleeves(targets, named=False)

    llm_names = [cfg.name for cfg in targets if cfg.strategy == llm_strategy.LLM_STRATEGY_NAME]
    if llm_names:
        console.print(
            f"[yellow]Warning:[/] watched LLM sleeves {llm_names} call the model every tick - "
            "that gets expensive fast. Intraday churn is meant for rule-based strategies."
        )

    universe = sorted(
        {
            symbol
            for cfg in targets
            for symbol in (cfg.universe or settings.agent_universe_list or agent.DEFAULT_UNIVERSE)
        }
    )
    on_usage, cost_tally = _usage_recorder(settings, "execution")
    spec = storage_factory.research_store(settings).latest()
    hist_cache = history_cache.HistoryCache(settings.history_cache_dir)

    console.print(
        f"[green]Watching {len(targets)} sleeve(s)[/] matching '{match}' "
        f"({len(universe)} symbols) every {interval:g}s. Ctrl-C to stop."
    )

    latest: dict[str, market_data.Quote] = {}

    def source(symbol: str) -> market_data.Quote:
        quote = latest.get(symbol)
        if quote is None:
            raise market_data.QuoteError(f"No quote for {symbol}.")
        return quote

    started = market_calendar.eastern_now()
    tick = 0
    try:
        with _build_client(settings) as client:
            # Build each sleeve's strategy and engine once, then reuse across ticks.
            runners: list[tuple[sleeves.SleeveConfig, agent.Strategy, paper.PaperEngine]] = []
            for cfg in targets:
                strat = _build_sleeve_strategy(settings, cfg, spec, on_usage, client, hist_cache)
                engine = storage_factory.paper_engine(settings, cfg)
                runners.append((cfg, strat, engine))

            kill_switch = safety.KillSwitch(settings.kill_switch_path)
            while True:
                if kill_switch.is_engaged():
                    console.print("[bold red]Kill switch engaged - stopping watch.[/]")
                    break
                now_et = market_calendar.eastern_now()
                if not force and not market_calendar.is_regular_session(now_et):
                    console.print(f"[{now_et:%H:%M ET}] Market closed - stopping watch.")
                    break

                tick += 1
                latest.clear()
                latest.update(market_data.get_quotes(client, universe))  # one batched request

                for cfg, strat, engine in runners:
                    report = agent.AgentRunner(strat, engine, source).run_cycle(
                        now=datetime.now(UTC)
                    )
                    storage_factory.evaluation_store(settings, cfg).record_cycle(report)
                    if report.num_filled or report.num_rejected:
                        val = report.valuation
                        console.print(
                            f"[{now_et:%H:%M:%S} ET] [cyan]{cfg.name}[/]: "
                            f"filled {report.num_filled}/rej {report.num_rejected} | "
                            f"{_money(val.total_value)} ({val.total_return_pct:+.2f}%)"
                        )
                console.print(
                    f"[dim][{now_et:%H:%M:%S} ET] tick {tick}: "
                    f"{len(latest)}/{len(universe)} quotes.[/]"
                )

                if once:
                    break
                if duration > 0 and (market_calendar.eastern_now() - started).total_seconds() >= (
                    duration * 60
                ):
                    console.print(f"[dim]Reached --duration {duration:g} min; stopping.[/]")
                    break
                time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[yellow]Watch stopped (Ctrl-C).[/]")
    except (oauth.OAuthError, api.ApiError, llm_strategy.LLMError) as exc:
        _fail(exc)

    console.print(f"[dim]Ran {tick} tick(s).[/]")
    if cost_tally[0] > 0:
        console.print(f"[dim]API cost this session: {_cost(cost_tally[0])}.[/]")


@sleeve_app.command("compare")
def sleeve_compare(
    benchmark: str = typer.Option(
        "", "--benchmark", help="Sleeve name to measure excess return against (e.g. bench-spy)."
    ),
    cohort: str = typer.Option(
        "", "--cohort", help="Report only this cohort's sleeves (historical ones included)."
    ),
) -> None:
    """Leaderboard of sleeves by return (reads recorded cycles; no network).

    With --benchmark, adds an 'excess' column = each sleeve's return minus the
    benchmark sleeve's - the honest 'are we beating the index?' view.

    --cohort scopes the *whole* report, not only the benchmark: just that cohort's
    members are listed and ranked. Cohorts differ in start session, starting capital,
    configuration, and observation history, so an excess column spanning two of them is
    not a real number. A superseded cohort stays fully readable when named explicitly -
    this command only reads.

    Without --cohort every persisted sleeve is listed, and a bare ``--benchmark
    bench-spy`` - a name every cohort reuses for its control - resolves against the
    cohorts still collecting.
    """
    settings = get_settings()
    setup_logging(settings)
    store = storage_factory.sleeve_store(settings)
    items = store.list()
    if not items:
        console.print("[dim]No sleeves yet.[/]")
        return

    scope = cohort.strip() or None
    scoped = items
    if scope is not None:
        try:
            scoped = cohort_scope.members(items, scope)
        except LookupError as exc:
            _fail(exc)

    rows: list[tuple[sleeves.SleeveConfig, evaluation.EvalSummary]] = []
    for cfg in scoped:
        summary = storage_factory.evaluation_store(settings, cfg).summary()
        rows.append((cfg, summary))
    rows.sort(key=lambda item: item[1].total_return_pct, reverse=True)
    # Counted over the whole registry rather than the current scope: a name two cohorts
    # share is worth qualifying even when only one of them is on screen, so each row
    # still says which stored record it is.
    name_counts = {
        name: sum(config.name == name for config in items)
        for name in {config.name for config in items}
    }

    bench_return: Decimal | None = None
    benchmark_id: str | None = None
    if benchmark:
        try:
            # Resolved against the full registry even when scoped, so naming a benchmark
            # from another cohort is refused with a message saying where it does live -
            # rather than silently comparing across cohorts, or reporting "no such
            # sleeve" for one that plainly exists.
            benchmark_config = benchmark_scope.resolve(items, benchmark, cohort_id=scope)
        except LookupError as exc:
            _fail(exc)
        if benchmark_config is None:
            _fail(f"No sleeve named '{benchmark}' to benchmark against.")
        benchmark_id = benchmark_config.identity
        bench_return = next(
            summary.total_return_pct for config, summary in rows if config.identity == benchmark_id
        )

    if scope is not None:
        status = cohort_lifecycle.status_for(scope)
        suffix = f" [yellow]{status.label}[/]" if status.historical else ""
        console.print(
            f"[bold]Cohort {scope}[/] - {len(rows)} sleeve(s) in scope; "
            f"other cohorts and legacy sleeves excluded.{suffix}"
        )

    table = Table(title="Sleeve comparison (best return first)", show_header=True)
    table.add_column("#", justify="right")
    table.add_column("name", style="cyan")
    table.add_column("strategy")
    table.add_column("cycles", justify="right")
    table.add_column("trades", justify="right")
    table.add_column("value", justify="right")
    table.add_column("return", justify="right")
    if bench_return is not None:
        table.add_column(f"vs {benchmark}", justify="right")
    table.add_column("max DD", justify="right")
    table.add_column("sharpe", justify="right")
    for rank, (cfg, summary) in enumerate(rows, start=1):
        value = summary.latest_value if summary.cycles > 0 else cfg.starting_cash
        label = (
            cfg.name
            if name_counts[cfg.name] == 1
            else f"{cfg.name} [{cfg.cohort_id or 'legacy'}]"
        )
        cells = [
            str(rank),
            # Escaped: Rich reads '[paper-first-2026-07-28]' as a style tag and drops it,
            # so the qualifier that says *which* duplicate a row is never reached the
            # screen. Only this cell is escaped; the excess cell below sets a real colour.
            markup.escape(label),
            cfg.strategy,
            str(summary.cycles),
            str(summary.trades_filled),
            _money(value),
            f"{summary.total_return_pct:+.2f}%",
        ]
        if bench_return is not None:
            excess = summary.total_return_pct - bench_return
            marker = "[green]" if excess > 0 else "[red]"
            cells.append(f"{marker}{excess:+.2f}%[/]" if cfg.identity != benchmark_id else "-")
        cells += [
            f"-{summary.max_drawdown_pct:.1f}%",
            str(summary.sharpe) if summary.sharpe is not None else "-",
        ]
        table.add_row(*cells)
    console.print(table)


@sleeve_app.command("positions")
def sleeve_positions(
    name: str = typer.Argument("", help="Sleeve to show (omit when using --all)."),
    cohort: str = typer.Option(
        "",
        "--cohort",
        help="Cohort scope required when the name is ambiguous.",
    ),
    all_sleeves: bool = typer.Option(False, "--all", help="Show positions for every sleeve."),
    detailed: bool = typer.Option(
        False,
        "--detailed",
        "-d",
        help="Add per-position cost basis, unrealized %, and portfolio weight columns.",
    ),
) -> None:
    """Show positions (marked to live quotes) plus a per-sleeve stats summary."""
    settings = get_settings()
    setup_logging(settings)
    store = storage_factory.sleeve_store(settings)
    if all_sleeves:
        targets = store.list()
    elif name:
        try:
            found = store.resolve(name, cohort_id=cohort or None)
        except LookupError as exc:
            _fail(exc)
        if found is None:
            _fail(f"No sleeve named '{name}'.")
        targets = [found]
    else:
        _fail("Give a sleeve name or --all.")
    if not targets:
        _fail("No sleeves to show. Create one with 'schwab-trader sleeve create'.")

    engines = {cfg.identity: storage_factory.paper_engine(settings, cfg) for cfg in targets}
    all_symbols = sorted({p.symbol for eng in engines.values() for p in eng.positions()})

    marks: dict[str, Decimal | None] = {}
    if all_symbols:
        try:
            with _build_client(settings) as client:
                for symbol in all_symbols:
                    try:
                        marks[symbol] = market_data.get_quote(client, symbol).mark
                    except market_data.QuoteError:
                        marks[symbol] = None
        except (oauth.OAuthError, api.ApiError) as exc:
            console.print(f"[yellow]Could not fetch live quotes:[/] {exc} (using avg cost).")

    for cfg in targets:
        engine = engines[cfg.identity]
        positions = engine.positions()
        valuation = engine.value(marks)
        _render_sleeve_positions(cfg, engine, positions, marks, valuation, detailed=detailed)


def _pnl(amount: Decimal) -> str:
    """Money string colored green when non-negative, red when negative."""
    return f"[green]{_money(amount)}[/]" if amount >= 0 else f"[red]{_money(amount)}[/]"


def _render_sleeve_positions(
    cfg: sleeves.SleeveConfig,
    engine: paper.PaperEngine,
    positions: list[paper.PaperPosition],
    marks: dict[str, Decimal | None],
    valuation: paper.PaperValuation,
    *,
    detailed: bool,
) -> None:
    """Render one sleeve's position table (optionally detailed) plus a stats summary."""
    total_positions_value = valuation.positions_value
    if positions:
        table = Table(title=f"Sleeve '{cfg.name}' positions ({cfg.strategy})", show_header=True)
        # Default view is per-share (avg cost/mark); --detailed swaps those derivable
        # columns for dollar analytics (cost basis/unreal %/weight) so both fit ~80 cols.
        table.add_column("Symbol", style="cyan")
        table.add_column("Qty", justify="right")
        if detailed:
            table.add_column("Cost basis", justify="right")
            table.add_column("Value", justify="right")
            table.add_column("Unreal $", justify="right")
            table.add_column("Unreal %", justify="right")
            table.add_column("Weight", justify="right")
        else:
            table.add_column("Avg cost", justify="right")
            table.add_column("Mark", justify="right")
            table.add_column("Value", justify="right")
            table.add_column("Unreal $", justify="right")
        for position in positions:
            mark = marks.get(position.symbol)
            mark_dec = mark if isinstance(mark, Decimal) else position.avg_cost
            cost_basis = position.avg_cost * position.quantity
            value = mark_dec * position.quantity
            unrealized = value - cost_basis
            if detailed:
                unreal_pct = (unrealized / cost_basis * 100) if cost_basis else Decimal(0)
                weight = (
                    (value / total_positions_value * 100) if total_positions_value else Decimal(0)
                )
                table.add_row(
                    position.symbol,
                    str(position.quantity),
                    _money(cost_basis),
                    _money(value),
                    _pnl(unrealized),
                    f"{unreal_pct:+.2f}%",
                    f"{weight:.1f}%",
                )
            else:
                table.add_row(
                    position.symbol,
                    str(position.quantity),
                    _money(position.avg_cost),
                    _money(mark) if isinstance(mark, Decimal) else "-",
                    _money(value),
                    _pnl(unrealized),
                )
        console.print(table)
    else:
        console.print(f"[dim]Sleeve '{cfg.name}': no positions.[/]")

    # --- Aggregate stats summary (always shown) ---
    total_cost_basis = valuation.positions_value - valuation.unrealized_pnl
    equity = valuation.total_value
    unreal_pct = (
        (valuation.unrealized_pnl / total_cost_basis * 100) if total_cost_basis else Decimal(0)
    )
    invested_pct = (total_positions_value / equity * 100) if equity else Decimal(0)

    summary = Table(title=f"Sleeve '{cfg.name}' summary", show_header=False, title_justify="left")
    summary.add_column("Metric", style="cyan", no_wrap=True)
    summary.add_column("Value", justify="right")
    summary.add_row("Positions", str(len(positions)))
    summary.add_row("Total cost basis", _money(total_cost_basis))
    summary.add_row("Total position value", _money(total_positions_value))
    summary.add_row("Unrealized P&L", f"{_pnl(valuation.unrealized_pnl)} ({unreal_pct:+.2f}%)")
    summary.add_row("Realized P&L", _pnl(valuation.realized_pnl))
    summary.add_row("Cash (settled)", _money(valuation.cash))
    if cfg.settlement_t1 or valuation.unsettled_cash > 0:
        summary.add_row("Unsettled cash (T+1)", _money(valuation.unsettled_cash))
    if cfg.leverage > 1:
        summary.add_row("Leverage", f"{cfg.leverage}x")
        summary.add_row("Buying power", _money(engine.buying_power()))
        if valuation.cash < 0:
            summary.add_row("Margin debit", _money(-valuation.cash))
    summary.add_row("Equity (total value)", _money(equity))
    summary.add_row("Invested", f"{invested_pct:.1f}% of equity")
    summary.add_row(
        "Total return",
        f"[bold]{valuation.total_return_pct:+.2f}%[/] vs {_money(cfg.starting_cash)}",
    )
    console.print(summary)


@sleeve_app.command("report")
def sleeve_report(
    name: str = typer.Argument(..., help="Sleeve name or stable sleeve ID."),
    cohort: str = typer.Option(
        "",
        "--cohort",
        help="Cohort scope required when the name is ambiguous.",
    ),
    limit: int = typer.Option(10, "--limit", min=1, help="How many recent cycles to show."),
) -> None:
    """Detailed scorecard + recent cycles for one sleeve."""
    settings = get_settings()
    setup_logging(settings)
    store = storage_factory.sleeve_store(settings)
    try:
        cfg = store.resolve(name, cohort_id=cohort or None)
    except LookupError as exc:
        _fail(exc)
    if cfg is None:
        _fail(f"No sleeve named '{name}'.")
    _render_eval(
        storage_factory.evaluation_store(settings, cfg),
        limit,
        title=f"Sleeve '{name}' ({cfg.strategy})",
        empty_hint=f"Sleeve '{name}' has no cycles yet. Run 'schwab-trader sleeve run {name}'.",
    )


# --- Autonomous safety layer (kill switch + per-day limits) -----------------


def _safety_gate(settings: Settings) -> safety.SafetyGate:
    return safety.SafetyGate(
        safety.KillSwitch(settings.kill_switch_path),
        safety.SafetyLedger(settings.agent_activity_db_path),
        safety.SafetyLimits(
            capital_cap=settings.agent_capital_cap,
            daily_loss_limit=settings.agent_daily_loss_limit,
            max_trades_per_day=settings.agent_max_trades_per_day,
            max_order_notional=settings.max_order_notional,
        ),
    )


@safety_app.command("kill")
def safety_kill(
    reason: str = typer.Option("", "--reason", help="Why you're halting (recorded)."),
) -> None:
    """Engage the kill switch: halt all autonomous loops immediately."""
    settings = get_settings()
    setup_logging(settings)
    safety.KillSwitch(settings.kill_switch_path).engage(reason)
    console.print(
        "[bold red]KILL SWITCH ENGAGED.[/] Autonomous loops will stop. "
        "Resume with 'schwab-trader safety resume'."
    )


@safety_app.command("resume")
def safety_resume() -> None:
    """Disengage the kill switch (allow autonomous loops to run again)."""
    settings = get_settings()
    setup_logging(settings)
    safety.KillSwitch(settings.kill_switch_path).disengage()
    console.print("[green]Kill switch cleared.[/] Autonomous trading may run again.")


@safety_app.command("status")
def safety_status() -> None:
    """Show the kill switch, today's activity, and the configured limits."""
    settings = get_settings()
    setup_logging(settings)
    ks = safety.KillSwitch(settings.kill_switch_path).status()
    day = safety.SafetyLedger(settings.agent_activity_db_path).day()

    table = Table(title="Autonomous safety", show_header=False)
    table.add_column("Item", style="cyan")
    table.add_column("Value", justify="right")
    if ks.engaged:
        since = ks.since.isoformat() if ks.since else "?"
        table.add_row("Kill switch", f"[bold red]ENGAGED[/] ({since})")
        if ks.reason:
            table.add_row("Reason", ks.reason)
    else:
        table.add_row("Kill switch", "[green]clear[/]")
    table.add_row("Trades today", str(day.trades))
    table.add_row("Realized P&L today", _money(day.realized_pnl))
    if day.start_equity is not None:
        table.add_row("Opening equity today", _money(day.start_equity))
    table.add_row(
        "Capital cap", _money(settings.agent_capital_cap) if settings.agent_capital_cap else "off"
    )
    table.add_row(
        "Daily loss limit",
        _money(settings.agent_daily_loss_limit) if settings.agent_daily_loss_limit else "off",
    )
    table.add_row(
        "Max trades/day",
        str(settings.agent_max_trades_per_day) if settings.agent_max_trades_per_day else "off",
    )
    table.add_row("Max order notional", _money(settings.max_order_notional))
    console.print(table)
    if not any(
        (
            settings.agent_capital_cap,
            settings.agent_daily_loss_limit,
            settings.agent_max_trades_per_day,
        )
    ):
        console.print(
            "[yellow]No per-day/capital limits set.[/] Set SCHWAB_AGENT_CAPITAL_CAP, "
            "SCHWAB_AGENT_DAILY_LOSS_LIMIT, and SCHWAB_AGENT_MAX_TRADES_PER_DAY "
            "before autonomous live use."
        )


# --- Strategy validation (walk-forward promotion verdicts) ------------------


@validate_app.command("run")
def validate_run(
    strategy: str = typer.Option(..., "--strategy", help="Rule-based strategy to validate."),
    symbols: str = typer.Option("large-cap", "--symbols", help="Universe: a preset or CSV."),
    window: int = typer.Option(180, "--window", min=20, help="Trading days per fold."),
    step: int = typer.Option(90, "--step", min=5, help="Days between fold end-points."),
    folds: int = typer.Option(6, "--folds", min=2, help="Number of windows."),
    benchmark: str = typer.Option("SPY", "--benchmark", help="Benchmark symbol ('' to skip)."),
    cost_bps: float = typer.Option(10.0, "--cost-bps", help="Round-trip cost in bps."),
    factor: str = typer.Option(
        "earnings-yield", "--factor", help="Value factor for value-momentum/fundamental."
    ),
    settlement: bool = typer.Option(
        True,
        "--settlement/--no-settlement",
        help="Model T+1 settled cash (required for compatibility with the live cash path).",
    ),
    dividends: bool = typer.Option(
        False,
        "--dividends/--no-dividends",
        help="Include the existing current-yield total-return approximation.",
    ),
) -> None:
    """Walk-forward validate a strategy and record a promotion verdict (PASS/FAIL)."""
    settings = get_settings()
    setup_logging(settings)
    if strategy == llm_strategy.LLM_STRATEGY_NAME or strategy not in _RULE_STRATEGIES:
        _fail(f"Validation supports rule-based strategies: {', '.join(_RULE_STRATEGIES)}.")
    universe = _resolve_rule_universe(settings, strategy, symbols, benchmark)
    universe_label = (
        universe[0]
        if strategy == agent.TacticalRegimeStrategy.name
        else symbols.strip() or "default"
    )
    console.print(
        f"[dim]Validating [bold]{strategy}[/] on {universe_label} "
        f"({len(universe)} symbols): {folds} folds of {window}d, {cost_bps:.0f}bps...[/]"
    )
    wf = _run_walkforward(
        settings,
        strategy=strategy,
        universe=universe,
        window=window,
        step=step,
        folds=folds,
        cost_bps=cost_bps,
        benchmark=benchmark,
        cash=settings.paper_starting_cash,
        settlement=settlement,
        leverage=1.0,
        factor=factor,
        dividends=dividends,
    )
    manifest = promotion.manifest_from_walkforward(
        wf,
        strategy=strategy,
        universe_symbols=universe,
        factor=_validation_factor(strategy, factor),
        max_positions=settings.agent_max_positions,
        max_position_fraction=settings.agent_max_position_fraction,
        benchmark=benchmark,
        window=window,
        step=step,
        requested_folds=folds,
        cost_bps=cost_bps,
        settlement_t1=settlement,
        leverage=Decimal("1"),
        dividends=dividends,
        code_revision=promotion.current_code_revision(),
    )
    verdict = promotion.verdict_from_walkforward(
        wf,
        strategy=strategy,
        universe=universe_label,
        min_pass_rate=settings.promotion_min_pass_rate,
        manifest=manifest,
    )
    storage_factory.promotion_store(settings).record(verdict)

    assessment = promotion.assess_verdict(
        verdict,
        max_age_days=settings.promotion_max_age_days,
        expected_configuration_fingerprint=_validation_fingerprint(
            settings, strategy, universe, factor
        ),
        active_code_revision=promotion.current_code_revision(),
    )
    tag = "[green]QUALITY PASS[/]" if verdict.validated else "[red]QUALITY FAIL[/]"
    console.print(
        f"{tag}  {strategy} / {universe_label}: {verdict.passing_folds}/{verdict.folds} folds "
        f"passed gates ({verdict.pass_rate:.0%}, need {verdict.min_pass_rate:.0%})."
    )
    authorization = "[green]LIVE AUTHORIZED[/]" if assessment.authorized else "[red]BLOCKED[/]"
    console.print(f"{authorization}: {assessment.reason}.")
    excess = (
        f"{verdict.mean_excess_pct:+.2f}% vs {benchmark}"
        if verdict.mean_excess_pct is not None
        else "n/a"
    )
    console.print(
        f"[dim]mean fold return {verdict.mean_return_pct:+.2f}%, worst "
        f"{verdict.worst_return_pct:+.2f}%, excess {excess} "
        f"({'beats' if verdict.beats_benchmark else 'lags'} benchmark).[/]"
    )
    console.print(
        f"[dim]manifest {manifest.configuration_fingerprint[:12]} · "
        f"data through {manifest.last_data_date:%Y-%m-%d} · {manifest.code_revision}[/]"
        if manifest.last_data_date is not None
        else "[yellow]Manifest has no tested data end date; it cannot authorize live use.[/]"
    )
    cleared_gates = verdict.pass_rate >= verdict.min_pass_rate
    if not verdict.validated and cleared_gates and not verdict.beats_benchmark:
        console.print(
            "[yellow]Not validated:[/] clears the risk gates but does not beat the benchmark - "
            "a low-drawdown market-matcher, not an index-beater. Live trading (agent live / "
            "agent propose) requires beating the benchmark."
        )
    console.print(
        "[dim]Historical; point-in-time folds (no backfill), but the universe is still "
        "today's survivors - inclusion/delisting bias remains. See 'backtest survivorship'.[/]"
    )


@validate_app.command("explain")
def validate_explain(
    strategy: str = typer.Option(..., "--strategy", help="Strategy name."),
    symbols: str = typer.Option("large-cap", "--symbols", help="Validated universe label."),
    factor: str = typer.Option(
        "earnings-yield", "--factor", help="Active factor for value-momentum/fundamental."
    ),
) -> None:
    """Explain why the latest verdict is or is not authorized for live/propose."""
    settings = get_settings()
    setup_logging(settings)
    universe = _resolve_universe(settings, symbols)
    universe_label = symbols.strip() or "default"
    verdict = storage_factory.promotion_store(settings).latest(strategy, universe_label)
    if verdict is None:
        _fail(f"No validation on record for '{strategy}' on '{universe_label}'.")

    active_revision = promotion.current_code_revision()
    assessment = promotion.assess_verdict(
        verdict,
        max_age_days=settings.promotion_max_age_days,
        expected_configuration_fingerprint=_validation_fingerprint(
            settings, strategy, universe, factor
        ),
        active_code_revision=active_revision,
    )
    manifest = verdict.manifest
    table = Table(title=f"Validation: {strategy} / {universe_label}", show_header=False)
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Value")
    table.add_row("quality gates", "PASS" if verdict.validated else "FAIL")
    table.add_row("live authorized", "YES" if assessment.authorized else "NO")
    table.add_row("reason", assessment.reason)
    table.add_row("recorded", verdict.created_at.strftime("%Y-%m-%d %H:%M UTC"))
    table.add_row("maximum age", f"{settings.promotion_max_age_days} days")
    if manifest is None:
        table.add_row("manifest", "missing (legacy record; revalidate)")
    else:
        table.add_row("manifest schema", str(manifest.schema_version))
        table.add_row("symbols", ",".join(manifest.universe_symbols))
        table.add_row("universe hash", manifest.universe_hash)
        table.add_row("configuration", manifest.configuration_fingerprint)
        table.add_row("benchmark", manifest.benchmark or "none")
        table.add_row("factor", manifest.factor or "n/a")
        table.add_row(
            "fold design",
            f"{manifest.observed_folds}/{manifest.requested_folds} folds · "
            f"window {manifest.window} · step {manifest.step}",
        )
        table.add_row(
            "execution",
            f"{manifest.cost_bps:g} bps · next-open={manifest.next_bar_fill} · "
            f"T+1={manifest.settlement_t1} · leverage={manifest.leverage}",
        )
        table.add_row("dividend approximation", "on" if manifest.dividends else "off")
        table.add_row(
            "data through",
            manifest.last_data_date.strftime("%Y-%m-%d")
            if manifest.last_data_date is not None
            else "missing",
        )
        table.add_row("validated code", manifest.code_revision)
    table.add_row("active code", active_revision)
    console.print(table)


@validate_app.command("list")
def validate_list() -> None:
    """Show quality and current live authorization for each latest verdict."""
    settings = get_settings()
    setup_logging(settings)
    verdicts = storage_factory.promotion_store(settings).all_latest()
    if not verdicts:
        console.print("[dim]No verdicts yet. Run 'schwab-trader validate run --strategy ...'.[/]")
        return
    table = Table(title="Strategy validation verdicts", show_header=True)
    table.add_column("Strategy", style="cyan")
    table.add_column("Universe")
    table.add_column("Quality")
    table.add_column("Authorized")
    table.add_column("Folds", justify="right")
    table.add_column("Data through")
    table.add_column("Reason")
    revision = promotion.current_code_revision()
    for verdict in verdicts:
        assessment = _assess_for_active_settings(settings, verdict, active_code_revision=revision)
        quality = "[green]pass[/]" if verdict.validated else "[red]fail[/]"
        authorized = "[green]yes[/]" if assessment.authorized else "[red]no[/]"
        data_through = (
            verdict.manifest.last_data_date.strftime("%Y-%m-%d")
            if verdict.manifest is not None and verdict.manifest.last_data_date is not None
            else "-"
        )
        table.add_row(
            verdict.strategy,
            verdict.universe,
            quality,
            authorized,
            f"{verdict.passing_folds}/{verdict.folds}",
            data_through,
            assessment.reason,
        )
    console.print(table)
    console.print(
        "[dim]'Quality' is the statistical result. 'Authorized' additionally requires "
        "fresh, reproducible provenance compatible with the active code and settings. "
        "Use 'validate explain' for details. Not a profit guarantee.[/]"
    )


# --- Price panel (long-history daily dataset) -------------------------------


@panel_app.command("build")
def panel_build(
    symbols: str = typer.Option(
        "large-cap", "--symbols", help="Universe to fetch: a preset (e.g. large-cap) or CSV."
    ),
    years: int = typer.Option(
        20, "--years", min=1, max=20, help="Years of daily history (max 20)."
    ),
) -> None:
    """Fetch long daily history for a universe into the local price panel (idempotent)."""
    settings = get_settings()
    setup_logging(settings)
    universe = _resolve_universe(settings, symbols)
    if not universe:
        _fail("No symbols to build. Pass --symbols large-cap or a CSV list.")
    panel = storage_factory.price_panel(settings)
    console.print(
        f"[dim]Building price panel: {len(universe)} symbols x up to {years}y daily "
        "(one request per symbol)...[/]"
    )

    def on_symbol(symbol: str, count: int) -> None:
        note = f"{count} bars" if count else "[yellow]no data[/]"
        console.print(f"  {symbol}: {note}")

    try:
        with _build_client(settings) as client:
            counts = panel.build(client, universe, years=years, on_symbol=on_symbol)
    except (oauth.OAuthError, api.ApiError) as exc:
        _fail(exc)

    got = sum(1 for value in counts.values() if value > 0)
    console.print(
        f"[green]Panel updated:[/] {got}/{len(universe)} symbols, "
        f"{panel.total_bars():,} total bars stored at {settings.price_panel_db_path}."
    )


@panel_app.command("intraday-build")
def panel_intraday_build(
    symbols: str = typer.Option("SPY", "--symbols", help="Symbols: a preset or CSV (default SPY)."),
    minutes: int = typer.Option(5, "--minutes", help="Bar size: 1, 5, 10, 15, or 30 minutes."),
    days: int = typer.Option(
        250, "--days", min=1, help="Lookback requested (Schwab caps ~8-9mo at 5min, ~6wk at 1min)."
    ),
) -> None:
    """Fetch/append intraday minute bars into the accumulating intraday panel.

    Run this regularly (e.g. daily): Schwab's intraday history is shallow, so depth
    only grows by appending the recent window over time.
    """
    settings = get_settings()
    setup_logging(settings)
    if minutes not in market_data._INTRADAY_FREQUENCIES:
        _fail(f"--minutes must be one of {market_data._INTRADAY_FREQUENCIES}.")
    universe = _resolve_universe(settings, symbols)
    if not universe:
        _fail("No symbols to build. Pass --symbols SPY or a CSV list.")
    panel = storage_factory.intraday_panel(settings)
    console.print(
        f"[dim]Fetching {minutes}-min bars for {len(universe)} symbol(s), ~{days}d requested...[/]"
    )
    try:
        with _build_client(settings) as client:
            counts = panel.build(client, universe, minutes=minutes, days=days)
    except (oauth.OAuthError, api.ApiError) as exc:
        _fail(exc)
    for symbol, count in counts.items():
        note = f"{count:,} bars" if count else "[yellow]no data[/]"
        console.print(f"  {symbol}: {note}")
    got = sum(1 for value in counts.values() if value > 0)
    console.print(
        f"[green]Intraday panel updated:[/] {got}/{len(universe)} symbols at {minutes}min, "
        f"{panel.total_bars():,} total bars stored."
    )


@panel_app.command("import-csv")
def panel_import_csv(
    path: str = typer.Argument(..., help="CSV/flat file of intraday bars from a vendor."),
    symbol: str = typer.Option(
        "", "--symbol", help="Symbol (default when the file has no symbol column)."
    ),
    minutes: int = typer.Option(1, "--minutes", help="Bar size these rows represent (1/5/...)."),
    tz: str = typer.Option(
        "eastern", "--tz", help="Timezone of naive timestamps: 'eastern' (default) or 'utc'."
    ),
    rth_only: bool = typer.Option(
        True, "--rth-only/--all-hours", help="Keep only regular-session (9:30-16:00 ET) bars."
    ),
    columns: str = typer.Option(
        "",
        "--columns",
        help="For HEADERLESS files (e.g. FirstRate): comma column order, "
        "e.g. 'timestamp,open,high,low,close,volume'.",
    ),
) -> None:
    """Import vendor intraday bars from a CSV into the panel (UTC-normalized, dedup'd).

    Tolerates common layouts (timestamp or date+time; open/high/low/close/volume; an
    optional symbol column). Headerless files (FirstRate Data) need --columns. Purchased
    deep history (e.g. SPY 1-min for years) drops straight in and every backtest uses it.
    """
    settings = get_settings()
    setup_logging(settings)
    csv_path = Path(path)
    if not csv_path.exists():
        _fail(f"No such file: {path}")
    if tz not in ("eastern", "utc"):
        _fail("--tz must be 'eastern' or 'utc'.")
    column_list = [c.strip().lower() for c in columns.split(",") if c.strip()] or None
    has_symbol_col = "symbol" in (column_list or _peek_header(csv_path))
    if not symbol and not has_symbol_col:
        _fail("Give --symbol (the file has no symbol/ticker column).")

    try:
        candles, summary = intraday_import.parse_intraday_csv(
            csv_path, symbol=symbol.upper(), tz=tz, rth_only=rth_only, columns=column_list
        )
    except intraday_import.ImportError_ as exc:
        _fail(exc)
    if not candles:
        _fail("No usable rows parsed. Check the file's columns and timestamp format.")

    panel = storage_factory.intraday_panel(settings)
    by_symbol: dict[str, list[market_data.Candle]] = {}
    for candle in candles:
        by_symbol.setdefault(candle.symbol, []).append(candle)
    written = sum(panel.upsert(minutes, group) for group in by_symbol.values())
    console.print(
        f"[green]Imported {written:,} {minutes}-min bars[/] for "
        f"{', '.join(sorted(summary.symbols))} "
        f"(parsed {summary.parsed:,}, skipped {summary.skipped:,}, "
        f"RTH-filtered {summary.filtered:,})."
    )


def _peek_header(path: Path) -> set[str]:
    """Lower-cased header column names of a CSV (empty set on failure)."""
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            first = handle.readline()
        return {name.strip().lower() for name in first.split(",")}
    except OSError:
        return set()


@panel_app.command("intraday-info")
def panel_intraday_info() -> None:
    """Show what the intraday panel holds (per symbol and bar size)."""
    settings = get_settings()
    setup_logging(settings)
    panel = storage_factory.intraday_panel(settings)
    rows = panel.coverage()
    if not rows:
        console.print(
            "[dim]Intraday panel is empty. Build it with 'schwab-trader panel intraday-build'.[/]"
        )
        return
    table = Table(title="Intraday panel coverage", show_header=True)
    table.add_column("Symbol", style="cyan")
    table.add_column("Bar", justify="right")
    table.add_column("Bars", justify="right")
    table.add_column("From", justify="right")
    table.add_column("To", justify="right")
    for row in rows:
        table.add_row(
            row.symbol,
            f"{row.minutes}min",
            f"{row.bars:,}",
            row.first_ts.date().isoformat() if row.first_ts else "-",
            row.last_ts.date().isoformat() if row.last_ts else "-",
        )
    console.print(table)
    console.print(f"[dim]{panel.total_bars():,} total intraday bars.[/]")


@panel_app.command("info")
def panel_info() -> None:
    """Show what the price panel currently holds (per-symbol coverage)."""
    settings = get_settings()
    setup_logging(settings)
    panel = storage_factory.price_panel(settings)
    rows = panel.coverage()
    if not rows:
        console.print("[dim]Price panel is empty. Build it with 'schwab-trader panel build'.[/]")
        return
    table = Table(title="Price panel coverage", show_header=True)
    table.add_column("Symbol", style="cyan")
    table.add_column("Bars", justify="right")
    table.add_column("First", justify="right")
    table.add_column("Last", justify="right")
    for row in rows:
        table.add_row(
            row.symbol,
            f"{row.bars:,}",
            row.first_day.isoformat() if row.first_day else "-",
            row.last_day.isoformat() if row.last_day else "-",
        )
    console.print(table)
    console.print(f"[dim]{len(rows)} symbols, {panel.total_bars():,} total bars.[/]")


@panel_app.command("show")
def panel_show(
    symbol: str = typer.Argument(..., help="Symbol to show recent bars for."),
    limit: int = typer.Option(10, "--limit", min=1, help="How many recent bars to show."),
) -> None:
    """Show the most recent stored daily bars for one symbol."""
    settings = get_settings()
    setup_logging(settings)
    panel = storage_factory.price_panel(settings)
    closes = panel.closes(symbol)
    if not closes:
        _fail(f"No panel data for '{symbol.upper()}'. Build it with 'schwab-trader panel build'.")
    table = Table(title=f"{symbol.upper()} recent daily closes", show_header=True)
    table.add_column("Day", style="cyan")
    table.add_column("Close", justify="right")
    for day, close in closes[-limit:]:
        table.add_row(day.isoformat(), _money(close))
    console.print(table)


# --- SEC EDGAR (point-in-time fundamentals) ---------------------------------

_EDGAR_REQUEST_DELAY = 0.15  # be polite: SEC allows <=10 req/s


@edgar_app.command("fetch")
def edgar_fetch(
    symbols: str = typer.Option(
        "large-cap", "--symbols", help="Universe to fetch: a preset (e.g. large-cap) or CSV."
    ),
) -> None:
    """Download SEC company facts for a universe into the local fundamentals store."""
    settings = get_settings()
    setup_logging(settings)
    universe = _resolve_universe(settings, symbols)
    if not universe:
        _fail("No symbols to fetch. Pass --symbols large-cap or a CSV list.")
    if not settings.sec_user_agent.strip():
        console.print(
            "[yellow]Note:[/] SCHWAB_SEC_USER_AGENT is unset. SEC asks for a contact "
            "('Your Name your-email@example.com'); requests may be throttled without it."
        )

    store = storage_factory.sec_store(settings)
    console.print(f"[dim]Fetching SEC facts for {len(universe)} symbols...[/]")
    try:
        with sec_edgar.build_client(settings.effective_sec_user_agent) as client:
            cik_map = sec_edgar.load_ticker_cik_map(client)
            total_facts = 0
            missing: list[str] = []
            for symbol in universe:
                cik = cik_map.get(symbol.upper())
                if cik is None:
                    missing.append(symbol)
                    console.print(f"  {symbol}: [yellow]no CIK in SEC map[/]")
                    continue
                try:
                    raw = sec_edgar.fetch_company_facts(client, cik)
                except sec_edgar.EdgarError as exc:
                    console.print(f"  {symbol}: [red]{exc}[/]")
                    continue
                facts = sec_edgar.parse_company_facts(symbol, raw)
                store.upsert(facts)
                total_facts += len(facts)
                console.print(f"  {symbol}: {len(facts):,} facts")
                time.sleep(_EDGAR_REQUEST_DELAY)
    except sec_edgar.EdgarError as exc:
        _fail(exc)

    console.print(
        f"[green]EDGAR store updated:[/] {total_facts:,} facts fetched, "
        f"{store.total_facts():,} total stored at {settings.sec_db_path}."
    )
    if missing:
        console.print(f"[dim]No CIK found for: {', '.join(missing)}[/]")


@edgar_app.command("concepts")
def edgar_concepts(
    symbol: str = typer.Argument(..., help="Symbol to list reported concepts for."),
    contains: str = typer.Option("", "--contains", help="Filter concepts containing this text."),
    limit: int = typer.Option(40, "--limit", min=1, help="Max concepts to show."),
) -> None:
    """List the us-gaap concepts a company reports (names vary, so discover them here)."""
    settings = get_settings()
    setup_logging(settings)
    store = storage_factory.sec_store(settings)
    rows = store.concepts(symbol)
    if not rows:
        _fail(
            f"No stored facts for '{symbol.upper()}'. Fetch them with 'schwab-trader edgar fetch'."
        )
    if contains:
        needle = contains.lower()
        rows = [row for row in rows if needle in row[0].lower()]
    table = Table(title=f"{symbol.upper()} reported concepts", show_header=True)
    table.add_column("Concept", style="cyan")
    table.add_column("Facts", justify="right")
    for concept, count in rows[:limit]:
        table.add_row(concept, str(count))
    console.print(table)
    console.print(f"[dim]{len(rows)} concepts (showing up to {limit}).[/]")


@edgar_app.command("facts")
def edgar_facts(
    symbol: str = typer.Argument(..., help="Symbol."),
    concept: str = typer.Option(..., "--concept", help="us-gaap concept, e.g. NetIncomeLoss."),
    limit: int = typer.Option(12, "--limit", min=1, help="How many recent facts to show."),
) -> None:
    """Show recent stored facts for one symbol/concept (newest period first)."""
    settings = get_settings()
    setup_logging(settings)
    store = storage_factory.sec_store(settings)
    facts = store.facts(symbol, concept, limit=limit)
    if not facts:
        _fail(
            f"No facts for {symbol.upper()} / {concept}. "
            f"Try 'schwab-trader edgar concepts {symbol}'."
        )
    table = Table(title=f"{symbol.upper()} {concept}", show_header=True)
    table.add_column("Period end", style="cyan")
    table.add_column("Value", justify="right")
    table.add_column("Unit")
    table.add_column("Form")
    table.add_column("Filed")
    for fact in facts:
        table.add_row(
            fact.period_end.isoformat(),
            f"{fact.value:,}",
            fact.unit,
            fact.form or "-",
            fact.filed.isoformat(),
        )
    console.print(table)


@edgar_app.command("point-in-time")
def edgar_point_in_time(
    symbol: str = typer.Argument(..., help="Symbol."),
    concept: str = typer.Option(..., "--concept", help="us-gaap concept, e.g. Revenues."),
    as_of: str = typer.Option(..., "--as-of", help="Date (YYYY-MM-DD): what was known then."),
    form: str = typer.Option("", "--form", help="Restrict to a form, e.g. 10-K (annual)."),
) -> None:
    """Show the value that was public as of a date - the no-look-ahead query for backtests."""
    settings = get_settings()
    setup_logging(settings)
    try:
        as_of_date = date.fromisoformat(as_of)
    except ValueError:
        _fail(f"Invalid --as-of '{as_of}'. Use YYYY-MM-DD.")
    store = storage_factory.sec_store(settings)
    fact = store.point_in_time(symbol, concept, as_of_date, form=form or None)
    if fact is None:
        _fail(
            f"No {concept} for {symbol.upper()} known as of {as_of}. "
            f"Fetch facts first ('schwab-trader edgar fetch') or check 'edgar concepts {symbol}'."
        )
    console.print(
        f"[cyan]{symbol.upper()} {concept}[/] as of {as_of}: [bold]{fact.value:,}[/] {fact.unit}"
    )
    console.print(
        f"[dim]from the {fact.form or '?'} for period ending {fact.period_end.isoformat()}, "
        f"filed {fact.filed.isoformat()}.[/]"
    )


@edgar_app.command("ratios")
def edgar_ratios(
    symbol: str = typer.Argument(..., help="Symbol."),
    as_of: str = typer.Option("", "--as-of", help="Date YYYY-MM-DD (default: today)."),
    price: str = typer.Option(
        "", "--price", help="Price to use (default: the panel's close on/after as-of)."
    ),
) -> None:
    """Show derived valuation/quality ratios (TTM) as of a date - point-in-time.

    Fundamentals come from the EDGAR store; the price comes from ``--price`` or the
    local price panel. Populate both with 'edgar fetch' and 'panel build'.
    """
    settings = get_settings()
    setup_logging(settings)
    try:
        as_of_date = date.fromisoformat(as_of) if as_of else date.today()
    except ValueError:
        _fail(f"Invalid --as-of '{as_of}'. Use YYYY-MM-DD.")
    store = storage_factory.sec_store(settings)
    if store.total_facts() == 0:
        _fail("EDGAR store is empty. Run 'schwab-trader edgar fetch' first.")

    if price:
        try:
            price_dec = Decimal(price)
        except InvalidOperation:
            _fail(f"Invalid --price '{price}'.")
    else:
        panel = storage_factory.price_panel(settings)
        found = panel.close_on_or_before(symbol, as_of_date)
        if found is None:
            _fail(
                f"No panel price for {symbol.upper()} on/before {as_of_date}. "
                "Pass --price or run 'schwab-trader panel build'."
            )
        price_dec = found[1]

    result = fundamentals.ratios(store, symbol, as_of_date, price_dec)

    def _fmt(value: Decimal | None, *, pct: bool = False, mult: bool = False) -> str:
        if value is None:
            return "[dim]n/a[/]"
        if pct:
            return f"{value * 100:.2f}%"
        if mult:
            return f"{value:.2f}x"
        return f"{value:,.2f}"

    table = Table(title=f"{symbol.upper()} ratios as of {as_of_date} (TTM)", show_header=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")
    table.add_row("Price used", _money(result.price))
    table.add_row("Market cap", _money(result.market_cap) if result.market_cap else "[dim]n/a[/]")
    table.add_row("P/E (TTM)", _fmt(result.pe_ttm, mult=True))
    table.add_row("Earnings yield (TTM)", _fmt(result.earnings_yield_ttm, pct=True))
    table.add_row("Price / book", _fmt(result.price_to_book, mult=True))
    table.add_row("Book / market", _fmt(result.book_to_market))
    table.add_row("ROE (TTM)", _fmt(result.roe_ttm, pct=True))
    table.add_row("Net margin (TTM)", _fmt(result.net_margin_ttm, pct=True))
    console.print(table)
    console.print("[dim]Point-in-time: only filings public on/before the as-of date.[/]")


@backtest_app.command("intraday")
def backtest_intraday(
    symbol: str = typer.Option(
        "SPY", "--symbol", help="Symbol to backtest (must be in the panel)."
    ),
    strategy: str = typer.Option(
        "vwap-trend", "--strategy", help=f"One of: {', '.join(intraday_strategies.available())}."
    ),
    minutes: int = typer.Option(5, "--minutes", help="Bar size to use: 1, 5, 10, 15, 30."),
    cost_bps: float = typer.Option(
        1.0, "--cost-bps", help="Per-side cost (spread+fees+slippage) in bps."
    ),
) -> None:
    """Backtest an intraday strategy on the local intraday panel (offline; no network).

    Populate the bars first with 'panel intraday-build --symbols SYMBOL --minutes N'.
    """
    settings = get_settings()
    setup_logging(settings)
    if strategy not in intraday_strategies.available():
        _fail(
            f"Unknown strategy '{strategy}'. Choose: {', '.join(intraday_strategies.available())}."
        )
    panel = storage_factory.intraday_panel(settings)
    bars = panel.bars(symbol, minutes)
    if not bars:
        _fail(
            f"No {minutes}-min bars for {symbol.upper()}. Run "
            f"'schwab-trader panel intraday-build --symbols {symbol.upper()} --minutes {minutes}'."
        )
    result = intraday_backtest.run_intraday_backtest(
        bars, intraday_strategies.build(strategy), minutes=minutes, cost_bps=cost_bps
    )

    console.print(
        f"[dim]Intraday backtest: [bold]{strategy}[/] on {symbol.upper()} {minutes}min, "
        f"{result.sessions} sessions ({result.first_day} -> {result.last_day}), "
        f"{cost_bps:g}bps/side.[/]"
    )
    table = Table(title=f"Intraday backtest: {strategy} / {symbol.upper()}", show_header=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")
    table.add_row("Total return", f"{result.total_return_pct:+.2f}%")
    table.add_row("Trades", str(result.num_trades))
    table.add_row("Win rate", f"{result.win_rate_pct}%")
    table.add_row("Avg trade", f"{result.avg_trade_pct:+.3f}%")
    table.add_row("Sharpe (daily)", str(result.sharpe) if result.sharpe is not None else "-")
    table.add_row("Max drawdown", f"-{result.max_drawdown_pct:.2f}%")
    table.add_row("Sessions", str(result.sessions))
    console.print(table)
    console.print(
        "[dim]Shallow intraday history (Schwab caps ~8mo at 5min); a first read, not "
        "validation. Costs modeled per-side; no borrow/queue effects.[/]"
    )


@backtest_app.command("orb-universe")
def backtest_orb_universe(
    symbols: str = typer.Option(
        "large-cap", "--symbols", help="Universe: a preset (e.g. large-cap) or CSV."
    ),
    minutes: int = typer.Option(5, "--minutes", help="Bar size in the intraday panel."),
    top_n: int = typer.Option(5, "--top", min=1, help="Stocks-in-play to trade each session."),
    min_rvol: float = typer.Option(
        1.5, "--min-rvol", help="Only trade names with opening volume >= this x their baseline."
    ),
    or_minutes: int = typer.Option(5, "--or-minutes", help="Opening-range window (minutes)."),
    rvol_lookback: int = typer.Option(
        20, "--rvol-lookback", min=2, help="Sessions of baseline for relative volume."
    ),
    cost_bps: float = typer.Option(1.0, "--cost-bps", help="Per-side cost in bps."),
) -> None:
    """Backtest multi-symbol ORB with a relative-volume 'stocks in play' filter (offline).

    Reads intraday bars for the universe from the panel; seed them first with
    'panel intraday-build --symbols <preset> --minutes N'.
    """
    settings = get_settings()
    setup_logging(settings)
    universe = _resolve_universe(settings, symbols)
    panel = storage_factory.intraday_panel(settings)
    bars_by_symbol: dict[str, list[market_data.Candle]] = {}
    for symbol in universe:
        bars = panel.bars(symbol, minutes)
        if bars:
            bars_by_symbol[symbol] = bars
    if not bars_by_symbol:
        _fail(
            f"No {minutes}-min bars for that universe. Seed them with "
            f"'schwab-trader panel intraday-build --symbols {symbols} --minutes {minutes}'."
        )
    missing = [s for s in universe if s not in bars_by_symbol]
    if missing:
        console.print(
            f"[dim]No intraday bars for: {', '.join(missing[:20])}"
            f"{' ...' if len(missing) > 20 else ''}[/]"
        )

    result = orb_universe.run_universe_orb_backtest(
        bars_by_symbol,
        minutes=minutes,
        or_minutes=or_minutes,
        top_n=top_n,
        rvol_lookback=rvol_lookback,
        min_rvol=min_rvol,
        cost_bps=cost_bps,
    )
    console.print(
        f"[dim]Universe ORB: {result.universe_size} names, top-{top_n} by rvol>={min_rvol}, "
        f"{result.sessions} sessions ({result.first_day} -> {result.last_day}), "
        f"{cost_bps:g}bps/side.[/]"
    )
    table = Table(title="Universe ORB backtest", show_header=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")
    table.add_row("Total return", f"{result.total_return_pct:+.2f}%")
    table.add_row("Trades", f"{result.num_trades} ({result.long_trades}L / {result.short_trades}S)")
    table.add_row("Win rate", f"{result.win_rate_pct}%")
    table.add_row("Avg trade", f"{result.avg_trade_pct:+.3f}%")
    table.add_row("Sharpe (daily)", str(result.sharpe) if result.sharpe is not None else "-")
    table.add_row("Max drawdown", f"-{result.max_drawdown_pct:.2f}%")
    table.add_row("Avg stocks-in-play/day", f"{result.avg_in_play}")
    console.print(table)
    console.print(
        "[dim]Shallow history + long-only borrow ignored + no queue/auction effects; "
        "directional research, not validation.[/]"
    )


@backtest_app.command("orb-walkforward")
def backtest_orb_walkforward(
    symbols: str = typer.Option("large-cap", "--symbols", help="Universe: a preset or CSV."),
    minutes: int = typer.Option(5, "--minutes", help="Bar size in the intraday panel."),
    top_n: int = typer.Option(10, "--top", min=1, help="Stocks-in-play to trade each session."),
    min_rvol: float = typer.Option(2.0, "--min-rvol", help="Relative-volume threshold."),
    or_minutes: int = typer.Option(5, "--or-minutes", help="Opening-range window (minutes)."),
    folds: int = typer.Option(4, "--folds", min=2, help="Sequential windows to split into."),
    cost_bps: float = typer.Option(3.0, "--cost-bps", help="Per-side cost in bps."),
) -> None:
    """Walk-forward the universe ORB: per-fold consistency across sub-periods (offline).

    Splits the traded sessions into sequential folds and reports each fold's return and
    Sharpe - is the edge uniform, or one lucky stretch? Temporal stability, not
    multi-regime unless your data spans regimes.
    """
    settings = get_settings()
    setup_logging(settings)
    universe = _resolve_universe(settings, symbols)
    panel = storage_factory.intraday_panel(settings)
    bars_by_symbol: dict[str, list[market_data.Candle]] = {}
    for symbol in universe:
        bars = panel.bars(symbol, minutes)
        if bars:
            bars_by_symbol[symbol] = bars
    if not bars_by_symbol:
        _fail(
            f"No {minutes}-min bars. Seed with 'schwab-trader panel intraday-build "
            f"--symbols {symbols} --minutes {minutes}'."
        )
    try:
        wf = orb_universe.walk_forward_orb(
            bars_by_symbol,
            folds=folds,
            minutes=minutes,
            or_minutes=or_minutes,
            top_n=top_n,
            min_rvol=min_rvol,
            cost_bps=cost_bps,
        )
    except ValueError as exc:
        _fail(exc)

    console.print(
        f"[dim]Universe ORB walk-forward: {len(bars_by_symbol)} names, top-{top_n} "
        f"rvol>={min_rvol}, {folds} folds, {cost_bps:g}bps/side.[/]"
    )
    table = Table(title="Universe ORB walk-forward folds", show_header=True)
    table.add_column("Fold", justify="right", style="cyan")
    table.add_column("Window")
    table.add_column("Sessions", justify="right")
    table.add_column("Return", justify="right")
    table.add_column("Sharpe", justify="right")
    for fold in wf.folds:
        table.add_row(
            str(fold.index),
            f"{fold.first_day} -> {fold.last_day}",
            str(fold.sessions),
            f"{fold.total_return_pct:+.2f}%",
            str(fold.sharpe) if fold.sharpe is not None else "-",
        )
    console.print(table)
    console.print(
        f"[bold]Consistency:[/] mean {wf.mean_return_pct:+.2f}%, median "
        f"{wf.median_return_pct:+.2f}%, worst {wf.worst_return_pct:+.2f}%, "
        f"{wf.positive_folds}/{len(wf.folds)} folds positive "
        f"(full-period {wf.full_return_pct:+.2f}%)."
    )
    console.print(
        "[dim]Temporal-stability check on one ~7-month window; not multi-regime or "
        "out-of-sample parameter selection. Same cost/survivorship caveats as orb-universe.[/]"
    )


@backtest_app.command("fundamental")
def backtest_fundamental(
    factor: str = typer.Option(
        "earnings-yield", "--factor", help=f"One of: {', '.join(fundamental_backtest.FACTORS)}."
    ),
    symbols: str = typer.Option("large-cap", "--symbols", help="Universe: a preset or CSV."),
    top_n: int = typer.Option(10, "--top", min=1, help="How many top-ranked names to hold."),
    start: str = typer.Option(..., "--start", help="Start date YYYY-MM-DD."),
    end: str = typer.Option("", "--end", help="End date YYYY-MM-DD (default: today)."),
    ttm: bool = typer.Option(
        True, "--ttm/--annual", help="Use trailing-twelve-month earnings (vs annual 10-K only)."
    ),
) -> None:
    """Backtest a monthly fundamental factor portfolio (offline; reads panel + EDGAR).

    Ranks the universe each month by a value/quality factor using point-in-time
    fundamentals, holds the top names equal-weighted, and compares to an equal-weight
    benchmark. Populate the data first: 'panel build' and 'edgar fetch'.
    """
    settings = get_settings()
    setup_logging(settings)
    if factor not in fundamental_backtest.FACTORS:
        _fail(f"Unknown factor '{factor}'. Choose: {', '.join(fundamental_backtest.FACTORS)}.")
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end) if end else date.today()
    except ValueError:
        _fail("Invalid --start/--end. Use YYYY-MM-DD.")

    panel = storage_factory.price_panel(settings)
    store = storage_factory.sec_store(settings)
    if panel.total_bars() == 0:
        _fail("Price panel is empty. Run 'schwab-trader panel build --symbols large-cap' first.")
    if store.total_facts() == 0:
        _fail("EDGAR store is empty. Run 'schwab-trader edgar fetch --symbols large-cap' first.")

    universe = _resolve_universe(settings, symbols)
    console.print(
        f"[dim]Fundamental backtest: [bold]{factor}[/] top-{top_n} of {len(universe)} names, "
        f"monthly, {start_date} -> {end_date} ({'TTM' if ttm else 'annual'} earnings).[/]"
    )
    try:
        result = fundamental_backtest.run_factor_backtest(
            panel,
            store,
            universe,
            factor=factor,
            start=start_date,
            end=end_date,
            top_n=top_n,
            use_ttm=ttm,
        )
    except ValueError as exc:
        _fail(exc)

    table = Table(title=f"Fundamental factor backtest: {factor}", show_header=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")
    table.add_row("Window", f"{result.first_day} -> {result.last_day} ({result.months} months)")
    table.add_row("Total return", f"{result.total_return_pct:+.2f}%")
    table.add_row("CAGR", f"{result.cagr_pct:+.2f}%" if result.cagr_pct is not None else "-")
    table.add_row("Sharpe", str(result.sharpe) if result.sharpe is not None else "-")
    table.add_row("Max drawdown", f"-{result.max_drawdown_pct:.2f}%")
    table.add_row("Benchmark (equal-weight)", f"{result.benchmark_return_pct:+.2f}%")
    table.add_row("Excess vs benchmark", f"{result.excess_pct:+.2f}%")
    table.add_row("Avg names ranked/mo", f"{result.avg_names_ranked} of {len(universe)}")
    console.print(table)
    if result.last_holdings:
        console.print(f"[dim]Last holdings: {', '.join(result.last_holdings)}[/]")
    if result.avg_names_ranked < top_n:
        console.print(
            "[yellow]Warning:[/] fewer names had usable fundamentals than --top; "
            "fetch more coverage with 'edgar fetch' or widen the date range."
        )
    console.print(
        "[dim]Point-in-time fundamentals (no look-ahead), but survivorship-biased "
        "(current listings only) - directional research, not validation.[/]"
    )
