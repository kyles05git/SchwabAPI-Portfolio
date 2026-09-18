"""The scheduler wrapper must key post-run reporting on the outcome, not on success.

The documented scheduled command polls every 30 minutes through the afternoon. Gating
`--compare` and `--email` on "exit code 0" alone means a cohort that spends its window
awaiting data prints a leaderboard — and emails a digest — on every poll, each one
reporting the *previous* session's numbers as though they were this one's.

Offline: the CLI is replaced with a recorder, so nothing is executed, no `.env` is read,
and no database, broker, or SMTP connection is opened.
"""

from __future__ import annotations

import pytest

from schwab_trader.cli import EXIT_NOTHING_EXECUTED

run_sleeves = pytest.importorskip("scripts.run_sleeves")


@pytest.fixture
def recorder(monkeypatch):
    """Capture the CLI invocations the wrapper makes, returning scripted exit codes."""
    calls: list[tuple[str, ...]] = []
    codes: dict[str, int] = {}

    def fake_run_cli(*args: str) -> int:
        calls.append(args)
        return codes.get(args[0], 0)

    monkeypatch.setattr(run_sleeves, "_run_cli", fake_run_cli)
    return calls, codes


def _main(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr("sys.argv", ["run_sleeves.py", *argv])
    return run_sleeves.main()


def test_nothing_executed_skips_reporting_and_still_reports_success(monkeypatch, recorder):
    calls, codes = recorder
    codes["sleeve"] = EXIT_NOTHING_EXECUTED

    exit_code = _main(monkeypatch, "--cohort", "paper-x", "--compare", "--email")

    assert exit_code == 0, "a normal wait is not a scheduled-task failure"
    assert [call[0] for call in calls] == ["sleeve"], "no leaderboard, no digest"


def test_a_completed_run_still_reports(monkeypatch, recorder):
    calls, _codes = recorder

    exit_code = _main(monkeypatch, "--cohort", "paper-x", "--compare", "--email")

    assert exit_code == 0
    assert [call[0] for call in calls] == ["sleeve", "sleeve", "digest"]
    assert calls[1][:2] == ("sleeve", "compare")


def test_a_failed_run_propagates_and_skips_reporting(monkeypatch, recorder):
    calls, codes = recorder
    codes["sleeve"] = 1

    exit_code = _main(monkeypatch, "--cohort", "paper-x", "--compare", "--email")

    assert exit_code == 1
    assert [call[0] for call in calls] == ["sleeve"]


def test_the_nothing_executed_code_is_distinct_from_success_and_failure():
    assert EXIT_NOTHING_EXECUTED not in (0, 1, 2)
