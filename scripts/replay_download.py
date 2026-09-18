#!/usr/bin/env python
"""Plan, download, and report historical-replay RESEARCH evidence.

**Preflight is the default and only zero-argument behavior.** Running this script with
no subcommand computes the plan from the exchange calendar and prints it. It opens no
socket, constructs no Schwab client, reads no token, and writes nothing.

``download`` is the only path that contacts the provider or writes evidence. It
requires the explicit subcommand *and* an exact typed confirmation phrase, so no
scheduled job, alias, or mistyped command can start an acquisition.

Boundary: everything this script writes is research evidence in the
``historical_replay_*`` tables. It never creates or alters an official cohort
observation, a paper fill, order, position, valuation, or cash movement, never
satisfies forward cohort readiness, and never touches ``paper-first-2026-07-27`` or
``paper-first-2026-07-28``. It also never places, previews, replaces, or cancels an
order, and never sends a notification.

Operator usage::

    python scripts/replay_download.py preflight --symbols AAPL,MSFT
    python scripts/replay_download.py report    --symbols AAPL,MSFT
    python scripts/replay_download.py download  --symbols AAPL,MSFT   # then type the phrase
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime

from schwab_trader import client as api
from schwab_trader import historical_replay, historical_replay_acquire
from schwab_trader.config import get_settings
from schwab_trader.storage import factory as storage_factory
from schwab_trader.storage.historical_replay import SqlAlchemyHistoricalReplayStore

#: Typed exactly, or the download does not start.
CONFIRMATION_PHRASE = "DOWNLOAD REPLAY RESEARCH DATA"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_ABORTED = 3


def _symbols(raw: str) -> list[str]:
    return [item for item in (part.strip() for part in raw.split(",")) if item]


def _through(raw: str | None) -> datetime:
    if raw is None:
        return datetime.now(UTC)
    parsed = date.fromisoformat(raw)
    # End of that calendar day in UTC, so a supplied date includes its own session.
    return datetime(parsed.year, parsed.month, parsed.day, 23, 59, 59, tzinfo=UTC)


def _plan(args: argparse.Namespace) -> historical_replay_acquire.ReplayPreflight:
    universe = historical_replay.universe(_symbols(args.symbols), label=args.label)
    sessions = historical_replay.completed_sessions(_through(args.through), count=args.sessions)
    return historical_replay_acquire.preflight(universe, sessions)


def _store() -> SqlAlchemyHistoricalReplayStore:
    return storage_factory.historical_replay_store(get_settings())


def _authenticated_client() -> api.SchwabClient:
    """Build a read-only market-data client. Only ``download`` ever calls this.

    Imports live inside the function so that ``preflight`` and ``report`` never load,
    let alone exercise, the OAuth token path. The client's configured ``RateLimiter``
    is the single request-rate control for the whole acquisition.
    """
    from schwab_trader import auth as oauth
    from schwab_trader.token_store import TokenStore

    settings = get_settings()
    manager = oauth.TokenManager(settings, TokenStore(settings.token_path))
    if manager.tokens is None:
        raise SystemExit("Not authenticated. Run 'python -m schwab_trader auth login' first.")
    return api.SchwabClient(settings, manager)


def _cmd_preflight(args: argparse.Namespace, out: Callable[[str], None]) -> int:
    payload = historical_replay_acquire.preflight_payload(_plan(args))
    out(json.dumps(payload, indent=2, sort_keys=True))
    return EXIT_OK


def _cmd_report(args: argparse.Namespace, out: Callable[[str], None]) -> int:
    """Read-only: what is already stored for the planned universe and sessions."""
    plan = _plan(args)
    store = _store()
    rows = []
    for session in plan.sessions:
        for symbol in plan.universe_symbols:
            evidence = store.latest(symbol, session)
            rows.append(
                {
                    "symbol": symbol,
                    "session": session.isoformat(),
                    "status": None if evidence is None else evidence.status.value,
                    "replay_id": None if evidence is None else evidence.replay_id,
                    "revisions": len(store.history(symbol, session)),
                }
            )
    out(
        json.dumps(
            {
                "mode": "report",
                "universe_id": plan.universe_id,
                "stored": rows,
                "status_counts": store.status_counts(plan.universe_id),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return EXIT_OK


def _cmd_download(
    args: argparse.Namespace,
    out: Callable[[str], None],
    prompt: Callable[[str], str],
) -> int:
    plan = _plan(args)
    out(json.dumps(historical_replay_acquire.preflight_payload(plan), indent=2, sort_keys=True))
    out(
        f"\nThis will issue {len(plan.requests)} Schwab price-history requests and write "
        f"research evidence.\nType exactly: {CONFIRMATION_PHRASE}\n"
    )
    if prompt("> ").strip() != CONFIRMATION_PHRASE:
        out("Aborted: confirmation phrase did not match. Nothing was downloaded or written.")
        return EXIT_ABORTED

    universe = historical_replay.universe(_symbols(args.symbols), label=args.label)
    store = _store()
    with _authenticated_client() as client:
        fetcher = historical_replay_acquire.schwab_session_fetcher(client)
        report = historical_replay_acquire.acquire(fetcher, store, universe, plan.sessions)
    out(json.dumps(historical_replay_acquire.report_payload(report), indent=2, sort_keys=True))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="replay_download",
        description="Historical-replay research acquisition (preflight by default).",
    )
    # A plain optional positional, not subparsers: omitting it must land on the safe
    # command, and `download` must be something the operator typed on purpose.
    parser.add_argument(
        "command",
        nargs="?",
        choices=("preflight", "report", "download"),
        default="preflight",
        help=(
            "preflight (default): print the plan; no network, no writes. "
            "report: read stored evidence. "
            "download: acquire and persist, after a typed confirmation phrase."
        ),
    )
    parser.add_argument("--symbols", required=True, help="Comma-separated universe.")
    parser.add_argument("--label", default="", help="Optional universe label.")
    parser.add_argument(
        "--sessions",
        type=int,
        default=historical_replay.DEFAULT_SESSION_COUNT,
        help="How many completed exchange sessions to plan (default 30).",
    )
    parser.add_argument(
        "--through",
        default=None,
        help="ISO date to plan back from; defaults to now.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    out: Callable[[str], None] = print,
    prompt: Callable[[str], str] = input,
) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    command = args.command
    if args.sessions <= 0:
        out("Aborted: --sessions must be positive.")
        return EXIT_USAGE
    if command == "download":
        return _cmd_download(args, out, prompt)
    if command == "report":
        return _cmd_report(args, out)
    return _cmd_preflight(args, out)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
