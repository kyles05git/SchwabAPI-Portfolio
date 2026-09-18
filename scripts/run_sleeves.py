#!/usr/bin/env python
"""Durable scheduler entry point for official paper-sleeve cohort runs.

The SQLite run registry, not this process or a marker file, is the source of truth.
Repeated invocation is safe: scheduling resolves the eligible exchange session and
the cohort runner prevents duplicate official observations and paper fills.

This command is paper-only. It never submits, replaces, cancels, or approves a
broker order.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from schwab_trader import market_calendar
from schwab_trader.cli import EXIT_NOTHING_EXECUTED

BENCHMARK_SLEEVE = "bench-spy"
REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_cli(*args: str) -> int:
    """Invoke the project CLI from the repository root."""
    command = [sys.executable, "-m", "schwab_trader", *args]
    display = " ".join(command[2:])
    print(f"$ {display}", flush=True)
    return subprocess.run(command, cwd=REPO_ROOT, check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Durable paper cohort scheduler.")
    parser.add_argument("--cohort", default="", help="Run only this persisted cohort.")
    parser.add_argument(
        "--scheduled-for",
        default="",
        help="Explicit exchange session date; calendar and lateness rules still apply.",
    )
    parser.add_argument("--compare", action="store_true", help="Print the leaderboard after.")
    parser.add_argument(
        "--email",
        action="store_true",
        help="Email the sleeve digest afterwards (needs SCHWAB_SMTP_*).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Compatibility flag; durable calendar and kill-switch gates remain active.",
    )
    parser.add_argument(
        "--daily",
        action="store_true",
        help="Compatibility flag; every invocation now uses the durable scheduler.",
    )
    args = parser.parse_args()

    now_et = market_calendar.eastern_now()
    stamp = now_et.strftime("%Y-%m-%d %H:%M ET")
    if args.force or args.daily:
        print(f"[{stamp}] Compatibility flag accepted; durable safety gates remain active.")

    command = ["sleeve", "run", "--official"]
    if args.cohort:
        command.extend(["--cohort", args.cohort])
    else:
        command.append("--all")
    if args.scheduled_for:
        command.extend(["--scheduled-for", args.scheduled_for])

    print(f"[{stamp}] Evaluating durable paper-cohort schedule.")
    result = _run_cli(*command)
    if result == EXIT_NOTHING_EXECUTED:
        # No member executed — the cohort is waiting on data, or the session was
        # already resolved. The post-run steps are skipped deliberately: a leaderboard
        # or emailed digest here would report the *previous* session's numbers as
        # though they were this one's, once per poll for the whole waiting window.
        # Nothing failed, so the scheduled task still sees success.
        print(f"[{stamp}] No member executed; skipping post-run reporting.")
        return 0
    # Post-run reporting inherits the run's scope. A --cohort run that then printed or
    # emailed every persisted sleeve would rank this cohort against a superseded one and
    # against legacy sleeves with different starting capital — different start dates,
    # capital, and observation histories in one table, which is not a real comparison.
    # An --all run has no single scope to inherit, so it keeps reporting on everything.
    scope = ["--cohort", args.cohort] if args.cohort else []
    if result == 0 and args.compare:
        result |= _run_cli("sleeve", "compare", "--benchmark", BENCHMARK_SLEEVE, *scope)
    if result == 0 and args.email:
        result |= _run_cli("digest", "--send", "--benchmark", BENCHMARK_SLEEVE, *scope)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
