"""Smoke tests that the notify-and-approve commands are registered and well-formed.

These invoke only ``--help`` (never a command body), so they touch no settings,
network, or credentials - they catch wiring/option errors (bad typer.Option, missing
argument, import breakage) at the CLI boundary.
"""

from __future__ import annotations

import re

from typer.testing import CliRunner

from schwab_trader.cli import app

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _help(*args: str) -> str:
    """Rendered --help text with ANSI stripped and wrapping widened."""
    result = runner.invoke(app, [*args, "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    # Rich soft-wraps across a box border; drop the borders so flags stay contiguous.
    return _ANSI.sub("", result.output).replace("│", " ")


def test_agent_propose_help_lists_options() -> None:
    text = _help("agent", "propose")
    assert "--strategy" in text
    assert "--max-orders" in text


def test_agent_approve_help_takes_token_argument() -> None:
    text = _help("agent", "approve")
    assert "TOKEN" in text.upper()
    assert "--override" in text


def test_agent_group_lists_new_commands() -> None:
    text = _help("agent")
    assert "propose" in text
    assert "approve" in text


def test_validation_help_exposes_provenance_commands_and_assumptions() -> None:
    group = _help("validate")
    assert "explain" in group
    run = _help("validate", "run")
    assert "--factor" in run
    assert "--settlement" in run
    assert "--dividends" in run


def test_reconcile_help_is_read_only_and_exposes_window_and_notifications() -> None:
    text = _help("reconcile")
    assert "broker-read-only" in text
    assert "--hours" in text
    assert "--notify" in text
