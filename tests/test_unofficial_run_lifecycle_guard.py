"""A superseded cohort's sleeves are off limits to ordinary runs too, not just official ones.

An unofficial cycle is not an OFFICIAL observation, but it still writes the sleeve's paper
account and an evaluation row. For a withdrawn cohort those rows are incident evidence, so
``sleeve run <name>``, ``sleeve run --match``, and ``sleeve watch --match`` must respect the
lifecycle exactly as ``sleeve run --official`` does — otherwise "never re-run" only holds
for the invocation that happens to pass ``--official``.

The split mirrors the official path: naming one sleeve is refused, a broad selection skips.

Offline: no client is ever built. Each test asserts the guard fired *before* the command
reached the network, by failing if ``_build_client`` is called at all.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from schwab_trader import cli, cohort_lifecycle
from schwab_trader.config import Settings
from schwab_trader.storage import factory as storage_factory
from schwab_trader.storage.database import Database
from schwab_trader.storage.sleeves import SqlAlchemySleeveStore

runner = CliRunner()

HISTORICAL = "paper-first-2026-07-27"
ACTIVE = "paper-first-2026-07-28"


def _settings(tmp_path: Path, cohorts: tuple[str, ...]) -> Settings:
    url = f"sqlite:///{tmp_path / 'shared.sqlite3'}"
    database = Database(url, create_schema=True)
    store = SqlAlchemySleeveStore(database)
    for cohort_id in cohorts:
        # Distinct names per cohort so an explicit name is unambiguous to resolve.
        suffix = "hist" if cohort_id == HISTORICAL else "live"
        for base in ("trend", "bench"):
            store.create(
                f"{base}-{suffix}",
                strategy="buy-hold",
                universe=["SPY"],
                starting_cash=Decimal("10000.00"),
                max_positions=1,
                max_position_fraction=Decimal("1"),
                settlement_t1=True,
                cohort_id=cohort_id,
            )
    database.dispose()
    storage_factory._shared_database.cache_clear()
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=SecretStr(url),
        sleeves_dir=tmp_path / "sleeves",
        token_path=tmp_path / "tokens.json",
        state_db_path=tmp_path / "state.sqlite3",
        log_path=tmp_path / "logs" / "schwab_trader.log",
    )


@pytest.fixture
def no_client(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Fail loudly if a command reaches the network; record that it tried."""
    reached: list[bool] = []

    def _forbidden(settings: Settings) -> object:
        reached.append(True)
        raise AssertionError("the lifecycle guard let the run reach _build_client")

    monkeypatch.setattr(cli, "_build_client", _forbidden)
    return reached


@pytest.fixture
def both(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    settings = _settings(tmp_path, (HISTORICAL, ACTIVE))
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    return settings


@pytest.fixture
def only_historical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    settings = _settings(tmp_path, (HISTORICAL,))
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    return settings


# --- the helper ------------------------------------------------------------------


def test_the_helper_keeps_an_all_active_selection_untouched(both: Settings) -> None:
    configs = [
        cfg for cfg in storage_factory.sleeve_store(both).list() if cfg.cohort_id == ACTIVE
    ]
    assert cli._exclude_superseded_sleeves(configs, named=False) == configs


def test_the_helper_drops_only_the_retired_members(both: Settings) -> None:
    configs = storage_factory.sleeve_store(both).list()
    kept = cli._exclude_superseded_sleeves(configs, named=False)

    assert kept
    assert {cfg.cohort_id for cfg in kept} == {ACTIVE}
    assert len(kept) < len(configs)


# --- sleeve run, no --official ---------------------------------------------------


def test_naming_a_retired_sleeve_is_refused(both: Settings, no_client: list[bool]) -> None:
    result = runner.invoke(cli.app, ["sleeve", "run", "trend-hist"])

    assert result.exit_code == 1, result.output
    assert "superseded" in result.output
    assert HISTORICAL in result.output
    assert no_client == [], "refused too late: the client was built"


def test_naming_an_active_sleeve_is_not_refused(both: Settings, no_client: list[bool]) -> None:
    """The guard must not become a blanket block on ordinary research."""
    result = runner.invoke(cli.app, ["sleeve", "run", "trend-live"])

    # It gets past the guard and dies at the client, which is the fixture's whole job.
    assert no_client == [True]
    assert "superseded" not in result.output


def test_a_match_run_skips_the_retired_cohort_and_keeps_going(
    both: Settings, no_client: list[bool]
) -> None:
    result = runner.invoke(cli.app, ["sleeve", "run", "--match", "trend"])

    assert "Skipping superseded cohort(s): " + HISTORICAL in result.output
    assert no_client == [True], "the active sleeve should still have been run"
    assert result.output.count("Skipping superseded") == 1


def test_an_all_run_skips_the_retired_cohort(both: Settings, no_client: list[bool]) -> None:
    result = runner.invoke(cli.app, ["sleeve", "run", "--all"])

    assert "Skipping superseded cohort(s): " + HISTORICAL in result.output
    assert no_client == [True]


def test_a_match_run_with_only_retired_sleeves_fails_closed(
    only_historical: Settings, no_client: list[bool]
) -> None:
    result = runner.invoke(cli.app, ["sleeve", "run", "--match", "trend"])

    assert result.exit_code == 1, result.output
    # Rich hard-wraps console output, so normalise whitespace before matching.
    assert "nothing is eligible to run" in " ".join(result.output.split())
    assert no_client == [], "failed closed too late: the client was built"


# --- sleeve watch ----------------------------------------------------------------


def test_watch_refuses_when_every_match_is_retired(
    only_historical: Settings, no_client: list[bool]
) -> None:
    result = runner.invoke(cli.app, ["sleeve", "watch", "--match", "trend", "--once"])

    assert result.exit_code == 1, result.output
    assert "nothing is eligible to run" in " ".join(result.output.split())
    assert no_client == []


def test_watch_skips_the_retired_cohort_but_watches_the_active_one(
    both: Settings, no_client: list[bool]
) -> None:
    result = runner.invoke(
        cli.app, ["sleeve", "watch", "--match", "trend", "--once", "--force"]
    )

    assert "Skipping superseded cohort(s): " + HISTORICAL in result.output
    assert no_client == [True]


# --- the refusal text is the shared one -----------------------------------------


def test_the_refusal_is_the_registry_wording(both: Settings, no_client: list[bool]) -> None:
    """One message, so an operator sees the same explanation whichever command refused."""
    expected = cohort_lifecycle.run_refusal(HISTORICAL)
    assert expected is not None
    result = runner.invoke(cli.app, ["sleeve", "run", "trend-hist"])

    # Rich wraps the console output, so compare on a whitespace-normalised basis.
    assert " ".join(expected.split())[:60] in " ".join(result.output.split())
