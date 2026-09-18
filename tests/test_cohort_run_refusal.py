"""A superseded cohort can be read forever and scheduled never.

`paper-first-2026-07-27` is closed evidence. Appending a session to it would rewrite the
history of an incident, so the run path refuses it — loudly when the operator names it,
and by skipping it when a broad selection sweeps it up, which is what the scheduled job
does. The active cohort in the same invocation must still run: a withdrawn experiment
stopping the running collection would be its own outage.

Offline: local SQLite under ``tmp_path``, no ``.env``, no broker client, no network. The
orchestrator itself is replaced by a recorder, so nothing is executed or persisted.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from schwab_trader import cli, sleeves, strategy_registry
from schwab_trader.config import Settings

runner = CliRunner()

HISTORICAL = "paper-first-2026-07-27"
ACTIVE = "paper-first-2026-07-28"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        token_path=tmp_path / "tokens.json",
        state_db_path=tmp_path / "state.sqlite3",
        log_path=tmp_path / "logs" / "app.log",
        sleeves_dir=tmp_path / "sleeves",
        kill_switch_path=tmp_path / "KILL_SWITCH",
    )


def _create(store: sleeves.SleeveStore, cohort_id: str) -> None:
    for name in ("bench-spy", "control-cash"):
        store.create(
            f"{name}-{cohort_id[-2:]}",
            strategy="buy-hold",
            universe=["SPY"],
            starting_cash=Decimal("10000.00"),
            max_positions=1,
            max_position_fraction=Decimal("1"),
            definition=strategy_registry.make_definition(
                "buy-hold", universe_definition=["SPY"], benchmark_symbol_or_sleeve="bench-spy"
            ),
            cohort_id=cohort_id,
        )


@pytest.fixture
def executed(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record which cohorts the orchestrator would have been handed, and run nothing."""
    calls: list[list[str]] = []

    def _record(settings, store, targets, *, scheduled_for):  # type: ignore[no-untyped-def]
        calls.append(sorted({cfg.cohort_id for cfg in targets}))

    monkeypatch.setattr(cli, "_run_official_sleeve_cohorts", _record)
    return calls


def _invoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *args: str, cohorts: tuple[str, ...]
) -> tuple[int, str]:
    settings = _settings(tmp_path)
    store = sleeves.SleeveStore(settings.sleeves_dir)
    for cohort_id in cohorts:
        _create(store, cohort_id)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    result = runner.invoke(cli.app, ["sleeve", "run", *args])
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result.exit_code, result.output


def test_naming_the_superseded_cohort_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, executed: list[list[str]]
) -> None:
    exit_code, output = _invoke(
        tmp_path,
        monkeypatch,
        "--official",
        "--cohort",
        HISTORICAL,
        cohorts=(HISTORICAL, ACTIVE),
    )

    assert exit_code != 0
    assert "superseded" in output
    assert ACTIVE in output, "the refusal has to name the cohort to run instead"
    # Nothing reached the orchestrator: the refusal happens before any session is
    # planned, any lock is taken, or any snapshot is captured.
    assert executed == []


def test_a_broad_selection_skips_it_and_still_runs_the_active_cohort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, executed: list[list[str]]
) -> None:
    """`--all` is what the scheduled job uses. It must not become a no-op."""
    exit_code, output = _invoke(
        tmp_path, monkeypatch, "--official", "--all", cohorts=(HISTORICAL, ACTIVE)
    )

    assert exit_code == 0, output
    assert "Skipping superseded cohort(s): " + HISTORICAL in output
    assert executed == [[ACTIVE]]


def test_a_broad_selection_with_nothing_but_superseded_cohorts_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, executed: list[list[str]]
) -> None:
    exit_code, output = _invoke(
        tmp_path, monkeypatch, "--official", "--all", cohorts=(HISTORICAL,)
    )

    assert exit_code != 0
    assert "Every selected cohort is superseded" in output
    assert executed == []


def test_the_orchestrator_refuses_a_superseded_cohort_even_if_it_is_reached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defense in depth: the real entry point refuses regardless of who called it.

    The command-level filter above is the operator-facing behaviour; this is the guard
    that makes a future caller unable to bypass it. It runs the genuine function, with
    no recorder, and it must fail before constructing a client or a run store.
    """
    settings = _settings(tmp_path)
    store = sleeves.SleeveStore(settings.sleeves_dir)
    _create(store, HISTORICAL)

    with pytest.raises(typer.Exit):
        cli._run_official_sleeve_cohorts(
            settings,
            store,
            [cfg for cfg in store.list() if cfg.cohort_id == HISTORICAL],
            scheduled_for=None,
        )

    # No durable run store was created, so no session was even planned for it.
    assert not (settings.sleeves_dir / "runs.sqlite3").exists()
