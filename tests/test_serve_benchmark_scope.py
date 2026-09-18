"""``serve`` must start when several cohorts contain the same benchmark sleeve name.

Regression for issue #74: ``serve`` used to resolve ``--benchmark`` globally against the
sleeve registry before starting the dashboard, so two cohorts each holding a ``bench-spy``
sleeve raised ``AmbiguousSleeveName`` and the command refused to run. The dashboard itself
resolves the benchmark inside each comparability group, where the duplicate name is
unambiguous, so the global check was wrong for this command.

These tests never bind a socket: ``dashboard.serve`` is replaced by a recorder, so the
command body runs end to end without a server. Storage is a temporary SQLite file.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from schwab_trader import cli, dashboard
from schwab_trader.config import Settings
from schwab_trader.storage import factory as storage_factory
from schwab_trader.storage.database import Database
from schwab_trader.storage.sleeves import AmbiguousSleeveName, SqlAlchemySleeveStore

runner = CliRunner()

_HISTORICAL_COHORT = "paper-first-2026-07-27"
_ACTIVE_COHORT = "paper-first-2026-07-28"
_COHORTS = (_HISTORICAL_COHORT, _ACTIVE_COHORT)


def _shared_settings(tmp_path: Path) -> Settings:
    """Settings backed by a temporary SQLite database with two cohorts.

    Both cohorts contain a sleeve named ``bench-spy`` plus one member, mirroring the
    real registry that broke ``serve``.
    """
    url = f"sqlite:///{tmp_path / 'shared.sqlite3'}"
    database = Database(url, create_schema=True)
    store = SqlAlchemySleeveStore(database)
    for cohort_id in _COHORTS:
        for name in ("bench-spy", "momentum"):
            store.create(
                name,
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
def served(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture ``dashboard.serve`` calls instead of starting a real server."""
    calls: list[dict[str, Any]] = []

    def _record(settings: Settings, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(dashboard, "serve", _record)
    return calls


def _run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *args: str
) -> tuple[int, str, Settings]:
    settings = _shared_settings(tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    result = runner.invoke(cli.app, ["serve", "--no-live", *args])
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result.exit_code, result.output, settings


def test_serve_starts_when_two_cohorts_share_a_benchmark_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, served: list[dict[str, Any]]
) -> None:
    exit_code, output, settings = _run(tmp_path, monkeypatch, "--benchmark", "bench-spy")

    assert exit_code == 0, output
    # The command reached the server with the requested benchmark intact...
    assert [call["benchmark"] for call in served] == ["bench-spy"]
    # ...and did not warn, because the name does exist - it exists twice.
    assert "No sleeve matches" not in output

    # The duplicate name is genuinely ambiguous at global scope; the point of the fix is
    # that starting the dashboard no longer asks that question.
    store = storage_factory.sleeve_store(settings)
    with pytest.raises(AmbiguousSleeveName):
        store.resolve("bench-spy")
    # Both cohorts, and every sleeve in them, are untouched by starting the server.
    assert sorted({config.cohort_id for config in store.list()}) == sorted(_COHORTS)
    assert len(store.list()) == 4


def test_serve_resolves_the_benchmark_inside_each_cohort(tmp_path: Path) -> None:
    """The scoped resolution the global check was standing in for still works."""
    settings = _shared_settings(tmp_path)

    rows, _ = dashboard.collect_sleeves(settings, "bench-spy")

    benchmarks = {row.cohort_id for row in rows if row.is_benchmark}
    assert benchmarks == set(_COHORTS)
    # Each non-benchmark member gets an excess measured against its own cohort's
    # benchmark, never against the other cohort's identically named sleeve.
    others = [row for row in rows if not row.is_benchmark]
    assert len(others) == 2
    assert all(row.excess_pct is not None for row in others)
    assert all(row.excess_benchmark == "bench-spy" for row in others)


def test_serve_warns_but_starts_when_the_benchmark_matches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, served: list[dict[str, Any]]
) -> None:
    """A typo'd benchmark is reported, not fatal: it only blanks a column."""
    exit_code, output, _ = _run(tmp_path, monkeypatch, "--benchmark", "bench-typo")

    assert exit_code == 0, output
    assert "No sleeve matches" in output
    assert [call["benchmark"] for call in served] == ["bench-typo"]


def test_benchmark_is_registered_accepts_names_and_identities(tmp_path: Path) -> None:
    settings = _shared_settings(tmp_path)
    configs = storage_factory.sleeve_store(settings).list()
    active_bench = next(
        config
        for config in configs
        if config.name == "bench-spy" and config.cohort_id == _ACTIVE_COHORT
    )

    assert dashboard.benchmark_is_registered(settings, "bench-spy") is True
    assert dashboard.benchmark_is_registered(settings, active_bench.identity) is True
    assert dashboard.benchmark_is_registered(settings, "bench-typo") is False
    assert dashboard.benchmark_is_registered(settings, "") is False


def test_a_benchmark_that_exists_only_in_a_historical_cohort_is_not_registered(
    tmp_path: Path,
) -> None:
    """Default benchmark resolution ignores withdrawn experiments.

    The superseded cohort still holds a ``bench-spy`` sleeve with a real stable identity.
    Naming that identity as the dashboard-wide benchmark must not silently resolve
    against closed evidence — it names nothing the running collection can be measured
    against, and the operator is told so rather than shown a column computed from it.
    """
    settings = _shared_settings(tmp_path)
    configs = storage_factory.sleeve_store(settings).list()
    retired_bench = next(
        config
        for config in configs
        if config.name == "bench-spy" and config.cohort_id == _HISTORICAL_COHORT
    )

    assert dashboard.benchmark_is_registered(settings, retired_bench.identity) is False
    # The row itself is untouched: it is still in the registry, still that cohort's
    # benchmark, and still resolvable inside its own group.
    rows, _ = dashboard.collect_sleeves(settings, "bench-spy")
    retired_rows = [row for row in rows if row.cohort_id == _HISTORICAL_COHORT]
    assert [row.is_benchmark for row in retired_rows].count(True) == 1
    assert all(row.historical for row in retired_rows)


def test_historical_cohort_rows_are_ordered_after_the_running_collection(
    tmp_path: Path,
) -> None:
    """Same evidence, lower down: the leaderboard leads with what is being collected."""
    settings = _shared_settings(tmp_path)

    rows, _ = dashboard.collect_sleeves(settings, "bench-spy")
    official = [row.cohort_id for row in rows if row.cohort_id in _COHORTS]

    assert official == [_ACTIVE_COHORT] * 2 + [_HISTORICAL_COHORT] * 2
