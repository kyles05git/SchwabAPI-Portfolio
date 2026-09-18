"""A benchmark name reused by every cohort must resolve, not raise.

Regression for the failure that followed the first good session of the replacement
cohort: ``sleeve compare --benchmark bench-spy`` — the command
``scripts/run_sleeves.py --compare`` runs after every successful run — resolved the name
against the whole registry, matched ``bench-spy`` in both the July 27 and July 28
cohorts, and exited non-zero with ``AmbiguousSleeveName``. ``digest`` did the same and
let the exception escape as a traceback. PR #75 fixed the equivalent problem in ``serve``;
these tests pin the same guarantee for the leaderboard and the digest.

Offline: a temporary SQLite registry, no network, no orders, no cohort is ever run.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from schwab_trader import benchmark_scope, cli, digest
from schwab_trader.config import Settings
from schwab_trader.storage import factory as storage_factory
from schwab_trader.storage.database import Database
from schwab_trader.storage.sleeves import SqlAlchemySleeveStore

runner = CliRunner()

HISTORICAL = "paper-first-2026-07-27"
ACTIVE = "paper-first-2026-07-28"
BENCH = "bench-spy"


def _settings(tmp_path: Path, cohorts: tuple[str, ...]) -> Settings:
    """A registry where every cohort in ``cohorts`` holds ``bench-spy`` and one member."""
    url = f"sqlite:///{tmp_path / 'shared.sqlite3'}"
    database = Database(url, create_schema=True)
    store = SqlAlchemySleeveStore(database)
    for cohort_id in cohorts:
        for name in (BENCH, "trend-large"):
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
def both(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    settings = _settings(tmp_path, (HISTORICAL, ACTIVE))
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    return settings


def _configs(settings: Settings) -> list:
    return storage_factory.sleeve_store(settings).list()


# --- the unit that decides -------------------------------------------------------


def test_a_duplicated_name_resolves_to_the_active_cohort(both: Settings) -> None:
    resolved = benchmark_scope.resolve(_configs(both), BENCH)
    assert resolved is not None
    assert resolved.cohort_id == ACTIVE


def test_an_explicit_cohort_wins_even_when_it_is_superseded(both: Settings) -> None:
    """Archiving is not hiding: the historical control stays measurable on request."""
    resolved = benchmark_scope.resolve(_configs(both), BENCH, cohort_id=HISTORICAL)
    assert resolved is not None
    assert resolved.cohort_id == HISTORICAL


def test_a_stable_sleeve_id_resolves_regardless_of_lifecycle(both: Settings) -> None:
    configs = _configs(both)
    historical_bench = next(
        cfg for cfg in configs if cfg.cohort_id == HISTORICAL and cfg.name == BENCH
    )
    resolved = benchmark_scope.resolve(configs, historical_bench.sleeve_id)
    assert resolved is not None
    assert resolved.sleeve_id == historical_bench.sleeve_id


def test_several_active_cohorts_are_reported_not_guessed(tmp_path: Path) -> None:
    settings = _settings(tmp_path, (ACTIVE, "paper-second-2026-09-01"))
    with pytest.raises(benchmark_scope.BenchmarkScopeError) as exc:
        benchmark_scope.resolve(_configs(settings), BENCH)
    assert "several active cohorts" in str(exc.value)
    assert "--cohort" in str(exc.value)


def test_only_superseded_matches_is_reported(tmp_path: Path) -> None:
    settings = _settings(tmp_path, (HISTORICAL, "paper-zero-2026-07-01"))
    # Two historical cohorts: nothing active to narrow to, so refuse rather than pick.
    import schwab_trader.cohort_lifecycle as lifecycle

    original = lifecycle._REGISTRY
    patched = dict(original)
    patched["paper-zero-2026-07-01"] = lifecycle.CohortStatus(
        cohort_id="paper-zero-2026-07-01",
        lifecycle=lifecycle.CohortLifecycle.SUPERSEDED,
        label=lifecycle.SUPERSEDED_LABEL,
        reason="Test-only second retired cohort.",
    )
    lifecycle._REGISTRY = patched  # type: ignore[assignment]
    try:
        with pytest.raises(benchmark_scope.BenchmarkScopeError) as exc:
            benchmark_scope.resolve(_configs(settings), BENCH)
        assert "only in superseded" in str(exc.value)
    finally:
        lifecycle._REGISTRY = original  # type: ignore[assignment]


def test_a_missing_benchmark_is_none_not_an_error(both: Settings) -> None:
    assert benchmark_scope.resolve(_configs(both), "no-such-sleeve") is None
    assert benchmark_scope.resolve(_configs(both), "") is None


def test_an_unknown_cohort_scope_names_where_it_does_exist(both: Settings) -> None:
    with pytest.raises(benchmark_scope.BenchmarkScopeError) as exc:
        benchmark_scope.resolve(_configs(both), BENCH, cohort_id="paper-nope-2026-01-01")
    assert HISTORICAL in str(exc.value) and ACTIVE in str(exc.value)


# --- the commands that broke ----------------------------------------------------


def test_sleeve_compare_succeeds_with_a_duplicated_benchmark(both: Settings) -> None:
    """The exact invocation `scripts/run_sleeves.py --compare` makes."""
    result = runner.invoke(cli.app, ["sleeve", "compare", "--benchmark", BENCH])

    assert result.exit_code == 0, result.output
    assert "ambiguous" not in result.output.lower()
    assert "Sleeve comparison" in result.output


def test_sleeve_compare_can_target_the_superseded_cohort(both: Settings) -> None:
    result = runner.invoke(
        cli.app, ["sleeve", "compare", "--benchmark", BENCH, "--cohort", HISTORICAL]
    )

    assert result.exit_code == 0, result.output


def test_sleeve_compare_reports_an_unknown_cohort_cleanly(both: Settings) -> None:
    result = runner.invoke(
        cli.app, ["sleeve", "compare", "--benchmark", BENCH, "--cohort", "paper-nope"]
    )

    assert result.exit_code == 1
    assert "Error:" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_digest_succeeds_with_a_duplicated_benchmark(both: Settings) -> None:
    result = runner.invoke(cli.app, ["digest", "--benchmark", BENCH])

    assert result.exit_code == 0, result.output
    # The whole point: no traceback escaped the command.
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_digest_rows_pick_the_active_control(both: Settings) -> None:
    rows = digest.collect_digest_rows(both, BENCH)
    bench_rows = [row for row in rows if row.is_benchmark]

    assert len(bench_rows) == 1
    # Duplicated names are rendered with their cohort, so this pins *which* one won.
    assert ACTIVE in bench_rows[0].name


def test_digest_can_target_the_superseded_cohort(both: Settings) -> None:
    rows = digest.collect_digest_rows(both, BENCH, cohort_id=HISTORICAL)
    bench_rows = [row for row in rows if row.is_benchmark]

    assert len(bench_rows) == 1
    assert HISTORICAL in bench_rows[0].name
