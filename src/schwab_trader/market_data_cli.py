"""Read-only CLI commands for Schwab daily and five-minute bar evidence."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import NoReturn, cast

import typer
from rich.console import Console
from rich.table import Table

from schwab_trader import auth as oauth
from schwab_trader import client as api
from schwab_trader import market_calendar, market_data_diagnostics
from schwab_trader.config import Settings, get_settings
from schwab_trader.logging_config import setup_logging
from schwab_trader.storage import factory as storage_factory
from schwab_trader.token_store import TokenStore, TokenStoreError

market_data_app = typer.Typer(
    help="Read-only Schwab bar diagnostics and evidence reconciliation.",
    no_args_is_help=True,
)
console = Console()


def _fail(message: object) -> NoReturn:
    console.print(f"[red]Error:[/] {message}")
    raise typer.Exit(code=1)


def _build_client(settings: Settings) -> api.SchwabClient:
    """Build an authenticated market-data client without exposing credentials."""
    store = TokenStore(settings.token_path)
    try:
        manager = oauth.TokenManager(settings, store)
    except TokenStoreError as exc:
        _fail(exc)
    if manager.tokens is None:
        _fail("Not authenticated. Run 'python -m schwab_trader auth login' first.")
    return api.SchwabClient(settings, manager)


def _symbols(raw: str) -> tuple[str, ...]:
    try:
        return market_data_diagnostics.normalize_symbols(raw.split(","))
    except ValueError as exc:
        _fail(exc)


def _session(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        _fail("--session must be a date in YYYY-MM-DD form")


def _json(payload: object) -> None:
    console.print_json(json.dumps(payload, sort_keys=True))


def _print_diagnostics(payload: dict[str, object]) -> None:
    table = Table(title=f"Schwab bar readiness — {payload['session']}")
    table.add_column("Symbol", style="cyan")
    table.add_column("Latest official")
    table.add_column("Intervals")
    table.add_column("Evidence")
    table.add_column("State")
    for item in cast(list[dict[str, object]], payload["symbols"]):
        expected = item["expected_interval_count"]
        observed = item["observed_interval_count"]
        intervals = "not requested" if expected is None else f"{observed}/{expected}"
        table.add_row(
            str(item["symbol"]),
            str(item["latest_official_session"] or "none"),
            intervals,
            str(item["evidence_source"] or "none"),
            str(item["state"]),
        )
    console.print(table)


@market_data_app.command("diagnose-bars")
def diagnose_bars(
    symbols: str = typer.Option(
        ...,
        "--symbols",
        help="Comma-separated symbols, for example SPY,XLB,AAPL,ABBV.",
    ),
    session: str = typer.Option(..., "--session", help="Target XNYS session (YYYY-MM-DD)."),
    as_json: bool = typer.Option(False, "--json", help="Emit the stable JSON contract."),
) -> None:
    """Diagnose exact-session daily-first readiness without writing business state."""
    settings = get_settings()
    setup_logging(settings)
    target_session = _session(session)
    with _build_client(settings) as client:
        payload = market_data_diagnostics.diagnose_symbols(
            client,
            _symbols(symbols),
            target_session,
        )
    if as_json:
        _json(payload)
    else:
        _print_diagnostics(payload)
    if not payload["all_ready"]:
        raise typer.Exit(code=1)


def _stop_at_utc(raw: str) -> datetime:
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        _fail("--stop-at must be an ISO 8601 datetime")
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        return market_calendar.eastern_to_utc(stamp)
    return stamp.astimezone(UTC)


@market_data_app.command("monitor-bars")
def monitor_bars(
    symbols: str = typer.Option(..., "--symbols", help="Comma-separated symbols."),
    session: str = typer.Option(..., "--session", help="Target XNYS session (YYYY-MM-DD)."),
    stop_at: str = typer.Option(
        ...,
        "--stop-at",
        help="Hard stop as ISO 8601; a naive value is interpreted as Eastern.",
    ),
    poll_seconds: float = typer.Option(
        60.0,
        "--poll-seconds",
        min=market_data_diagnostics.MIN_POLL_SECONDS,
        help="Polling interval; minimum 30 seconds.",
    ),
    jsonl: bool = typer.Option(
        False,
        "--jsonl",
        help="Emit one stable compact JSON object per poll.",
    ),
) -> None:
    """Empirically record first-seen final-interval and official-daily availability."""
    settings = get_settings()
    setup_logging(settings)
    target_session = _session(session)
    last: dict[str, object] | None = None
    with _build_client(settings) as client:
        try:
            rows = market_data_diagnostics.monitor_session_bars(
                client,
                _symbols(symbols),
                target_session,
                poll_seconds=poll_seconds,
                stop_at=_stop_at_utc(stop_at),
            )
            for last in rows:
                if jsonl:
                    typer.echo(
                        json.dumps(
                            last,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
                else:
                    state = "complete" if last["complete"] else "waiting"
                    console.print(f"{last['polled_at']} {target_session.isoformat()} — {state}")
        except ValueError as exc:
            _fail(exc)
    if last is None or not last["complete"]:
        raise typer.Exit(code=1)


def _decimal_option(raw: str, name: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation:
        _fail(f"{name} must be a decimal number")
    # `Decimal` parses "Infinity" and "NaN" happily, and both defeat the comparison the
    # tolerance exists to make: Infinity makes every price break "within tolerance", so
    # an official open of 1.0 against a derived 100.0 reports no material discrepancy,
    # and NaN passes the range check here only to raise InvalidOperation on first use.
    if not value.is_finite():
        _fail(f"{name} must be a finite decimal number")
    if value < 0:
        _fail(f"{name} must be nonnegative")
    return value


def _print_reconciliation(payload: dict[str, object]) -> None:
    console.print(
        f"[cyan]{payload['symbol']}[/] {payload['session']}: {payload['status']} "
        f"(dataset {payload['dataset_id']})"
    )
    differences = payload.get("differences")
    if isinstance(differences, dict):
        table = Table(show_header=True)
        table.add_column("Field")
        table.add_column("Official - derived")
        table.add_column("Within tolerance")
        for field, raw in differences.items():
            item = cast(dict[str, object], raw)
            table.add_row(
                str(field),
                str(item["difference"]),
                str(item["within_tolerance"]),
            )
        console.print(table)


@market_data_app.command("reconcile-bars")
def reconcile_bars(
    dataset_id: str = typer.Option(..., "--dataset-id", help="Persisted derived dataset ID."),
    price_tolerance: str = typer.Option(
        str(market_data_diagnostics.DEFAULT_PRICE_TOLERANCE),
        "--price-tolerance",
        help="Absolute tolerance independently applied to O/H/L/C.",
    ),
    volume_tolerance: int = typer.Option(
        market_data_diagnostics.DEFAULT_VOLUME_TOLERANCE,
        "--volume-tolerance",
        min=0,
        help="Absolute integer volume tolerance.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit the stable JSON contract."),
) -> None:
    """Compare immutable derived evidence with the later official daily candle."""
    settings = get_settings()
    setup_logging(settings)
    try:
        evidence_store = storage_factory.market_data_evidence_reader(settings)
    except (FileNotFoundError, OSError) as exc:
        _fail(exc)
    with _build_client(settings) as client:
        try:
            payload = market_data_diagnostics.reconcile_derived_daily(
                client,
                evidence_store,
                dataset_id,
                price_tolerance=_decimal_option(price_tolerance, "--price-tolerance"),
                volume_tolerance=volume_tolerance,
            )
        except KeyError as exc:
            _fail(exc.args[0])
    if as_json:
        _json(payload)
    else:
        _print_reconciliation(payload)
    if payload["status"] != "compared" or payload["material_discrepancy"]:
        raise typer.Exit(code=1)


__all__ = ["market_data_app"]
