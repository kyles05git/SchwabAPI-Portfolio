"""Offline tests for the durable scheduler script entry point."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

from typer.main import get_command

from schwab_trader.cli import app

_SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "run_sleeves.py"
_SPEC = importlib.util.spec_from_file_location("run_sleeves_script", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
run_sleeves = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(run_sleeves)


def test_sleeve_run_help_exposes_durable_options():
    root_command = get_command(app)
    run_command = root_command.commands["sleeve"].commands["run"]
    option_names = {
        name for parameter in run_command.params for name in getattr(parameter, "opts", ())
    }

    assert {"--official", "--cohort", "--scheduled-for"} <= option_names


def test_default_invocation_delegates_to_durable_all_cohort_command(monkeypatch):
    calls = []
    monkeypatch.setattr(run_sleeves.market_calendar, "eastern_now", lambda: datetime(2026, 7, 20))
    monkeypatch.setattr(run_sleeves, "_run_cli", lambda *args: calls.append(args) or 0)
    monkeypatch.setattr(sys, "argv", ["run_sleeves.py"])

    assert run_sleeves.main() == 0
    assert calls == [("sleeve", "run", "--official", "--all")]


def test_explicit_cohort_and_session_are_forwarded_without_calendar_bypass(monkeypatch):
    calls = []
    monkeypatch.setattr(run_sleeves.market_calendar, "eastern_now", lambda: datetime(2026, 7, 18))
    monkeypatch.setattr(run_sleeves, "_run_cli", lambda *args: calls.append(args) or 0)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_sleeves.py",
            "--cohort",
            "cohort-a",
            "--scheduled-for",
            "2026-07-18",
            "--force",
        ],
    )

    assert run_sleeves.main() == 0
    assert calls == [
        (
            "sleeve",
            "run",
            "--official",
            "--cohort",
            "cohort-a",
            "--scheduled-for",
            "2026-07-18",
        )
    ]


def test_compare_and_email_run_only_after_successful_scheduler(monkeypatch):
    calls = []
    results = iter([0, 0, 0])
    monkeypatch.setattr(run_sleeves.market_calendar, "eastern_now", lambda: datetime(2026, 7, 20))
    monkeypatch.setattr(run_sleeves, "_run_cli", lambda *args: calls.append(args) or next(results))
    monkeypatch.setattr(sys, "argv", ["run_sleeves.py", "--compare", "--email"])

    assert run_sleeves.main() == 0
    assert calls[1] == ("sleeve", "compare", "--benchmark", "bench-spy")
    assert calls[2] == ("digest", "--send", "--benchmark", "bench-spy")
