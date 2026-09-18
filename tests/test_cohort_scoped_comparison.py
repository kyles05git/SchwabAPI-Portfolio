"""A cohort-scoped report shows that cohort and nothing else.

``sleeve compare --cohort paper-first-2026-07-28 --benchmark bench-spy`` used the cohort
only to decide *which* ``bench-spy`` was the control, then ranked it against every sleeve
ever persisted: the superseded July 27 cohort, and legacy sleeves started on other dates
with other capital. ``digest --cohort`` had the identical split, so the emailed report and
the leaderboard could describe different populations. These tests pin the cohort as the
scope of the whole report.

Offline: a temporary SQLite registry per test, the CLI replaced with a recorder in the
runner tests. No network, no Schwab, no shared database, no email, no order, and no
cohort is ever run.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from schwab_trader import benchmark_scope, cli, cohort_scope, digest
from schwab_trader.config import Settings
from schwab_trader.storage import factory as storage_factory
from schwab_trader.storage.database import Database
from schwab_trader.storage.sleeves import SqlAlchemySleeveStore

#: Rich truncates cells to the terminal width, and the default 80 columns would cut the
#: ``name [cohort]`` qualifier these tests read. Widen it so the rows are legible.
runner = CliRunner(env={"COLUMNS": "220"})

ACTIVE = "paper-first-2026-07-28"
HISTORICAL = "paper-first-2026-07-27"
BENCH = "bench-spy"
NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)

#: The active cohort's seven members: the control plus six strategy sleeves.
JULY_28_MEMBERS = (
    BENCH,
    "trend-large",
    "trend-small",
    "mean-reversion",
    "quality-value",
    "low-vol",
    "momentum-12-1",
)
#: The superseded cohort reuses the same names — that duplication is the whole problem.
JULY_27_MEMBERS = JULY_28_MEMBERS
#: Standalone records predating cohorts, with different starting capital.
LEGACY_SLEEVES = ("legacy-buy-hold", "scratch-momentum")


def _seed(store: SqlAlchemySleeveStore, name: str, cohort_id: str, cash: str) -> None:
    store.create(
        name,
        strategy="buy-hold" if name == BENCH else "momentum",
        universe=["SPY"],
        starting_cash=Decimal(cash),
        max_positions=1,
        max_position_fraction=Decimal("1"),
        settlement_t1=True,
        cohort_id=cohort_id,
    )


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Two same-named cohorts plus unrelated legacy sleeves, in one temporary registry."""
    url = f"sqlite:///{tmp_path / 'shared.sqlite3'}"
    database = Database(url, create_schema=True)
    store = SqlAlchemySleeveStore(database)
    for name in JULY_27_MEMBERS:
        _seed(store, name, HISTORICAL, "10000.00")
    for name in JULY_28_MEMBERS:
        _seed(store, name, ACTIVE, "10000.00")
    for name in LEGACY_SLEEVES:
        _seed(store, name, "", "2500.00")
    database.dispose()
    storage_factory._shared_database.cache_clear()

    configured = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=SecretStr(url),
        sleeves_dir=tmp_path / "sleeves",
        token_path=tmp_path / "tokens.json",
        state_db_path=tmp_path / "state.sqlite3",
        log_path=tmp_path / "logs" / "schwab_trader.log",
    )
    monkeypatch.setattr(cli, "get_settings", lambda: configured)
    return configured


def _configs(settings: Settings) -> list:
    return storage_factory.sleeve_store(settings).list()


def _compare(*args: str):
    return runner.invoke(cli.app, ["sleeve", "compare", *args])


def _row_names(output: str) -> set[str]:
    """Sleeve names that actually appear as rows in the rendered table."""
    return {name for name in (*JULY_28_MEMBERS, *LEGACY_SLEEVES) if name in output}


# --- the scoping unit -------------------------------------------------------------


def test_members_returns_only_exact_cohort_matches(settings: Settings) -> None:
    scoped = cohort_scope.members(_configs(settings), ACTIVE)

    assert len(scoped) == 7
    assert {cfg.cohort_id for cfg in scoped} == {ACTIVE}


def test_members_reports_an_unknown_cohort_with_the_known_ones(settings: Settings) -> None:
    with pytest.raises(cohort_scope.CohortScopeError) as exc:
        cohort_scope.members(_configs(settings), "paper-nope-2026-01-01")

    assert ACTIVE in str(exc.value) and HISTORICAL in str(exc.value)


def test_known_cohorts_excludes_uncohorted_sleeves(settings: Settings) -> None:
    assert cohort_scope.known_cohorts(_configs(settings)) == [HISTORICAL, ACTIVE]


# --- sleeve compare ---------------------------------------------------------------


def test_compare_lists_exactly_the_seven_july_28_members(settings: Settings) -> None:
    result = _compare("--cohort", ACTIVE, "--benchmark", BENCH)

    assert result.exit_code == 0, result.output
    assert "7 sleeve(s) in scope" in result.output
    # Seven member rows and no eighth: each renders one $10,000.00 starting-value cell,
    # and the legacy sleeves' $2,500.00 is absent entirely.
    assert result.output.count("$10,000.00") == 7
    assert "$2,500.00" not in result.output
    assert _row_names(result.output) == set(JULY_28_MEMBERS)


def test_compare_excludes_the_superseded_cohort_and_legacy_sleeves(settings: Settings) -> None:
    result = _compare("--cohort", ACTIVE, "--benchmark", BENCH)

    assert result.exit_code == 0, result.output
    assert HISTORICAL not in result.output
    for legacy in LEGACY_SLEEVES:
        assert legacy not in result.output
    # Every duplicated name is qualified, so a July 27 row would be visible as such.
    assert result.output.count(HISTORICAL) == 0


def test_compare_names_the_selected_cohort_for_the_operator(settings: Settings) -> None:
    result = _compare("--cohort", ACTIVE, "--benchmark", BENCH)

    assert ACTIVE in result.output
    assert "other cohorts and legacy sleeves excluded" in result.output


def test_compare_selects_the_july_28_benchmark(settings: Settings) -> None:
    """The control used for the excess column is this cohort's, not July 27's."""
    resolved = benchmark_scope.resolve(_configs(settings), BENCH, cohort_id=ACTIVE)

    assert resolved is not None
    assert resolved.cohort_id == ACTIVE
    result = _compare("--cohort", ACTIVE, "--benchmark", BENCH)
    # The benchmark's own row shows '-' rather than an excess against itself, which only
    # holds if the resolved control is inside the reported scope.
    assert result.exit_code == 0, result.output
    assert f"vs {BENCH}" in result.output


def test_compare_on_the_superseded_cohort_shows_only_its_records(settings: Settings) -> None:
    before = _configs(settings)

    result = _compare("--cohort", HISTORICAL, "--benchmark", BENCH)

    assert result.exit_code == 0, result.output
    assert HISTORICAL in result.output
    assert ACTIVE not in result.output
    for legacy in LEGACY_SLEEVES:
        assert legacy not in result.output
    # Reading incident evidence must not rewrite it.
    after = _configs(settings)
    assert [(c.sleeve_id, c.name, c.cohort_id, c.starting_cash) for c in before] == [
        (c.sleeve_id, c.name, c.cohort_id, c.starting_cash) for c in after
    ]


def test_compare_labels_the_superseded_cohort(settings: Settings) -> None:
    result = _compare("--cohort", HISTORICAL, "--benchmark", BENCH)

    assert "Superseded" in result.output


def test_compare_refuses_a_benchmark_from_another_cohort(settings: Settings) -> None:
    """A cross-cohort benchmark id fails closed instead of comparing across scopes."""
    historical_bench = next(
        cfg for cfg in _configs(settings) if cfg.cohort_id == HISTORICAL and cfg.name == BENCH
    )

    result = _compare("--cohort", ACTIVE, "--benchmark", historical_bench.sleeve_id)

    assert result.exit_code == 1
    assert "Error:" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_compare_reports_an_unknown_cohort_without_a_traceback(settings: Settings) -> None:
    result = _compare("--cohort", "paper-nope-2026-01-01", "--benchmark", BENCH)

    assert result.exit_code == 1
    assert "Error:" in result.output
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_compare_without_a_cohort_still_lists_everything(settings: Settings) -> None:
    """Backward compatible: no --cohort keeps the whole-registry leaderboard."""
    result = _compare("--benchmark", BENCH)

    assert result.exit_code == 0, result.output
    assert "in scope" not in result.output
    assert _row_names(result.output) == {*JULY_28_MEMBERS, *LEGACY_SLEEVES}
    # Both cohorts' duplicated rows are present and qualified.
    assert HISTORICAL in result.output and ACTIVE in result.output


# --- digest -----------------------------------------------------------------------


def test_digest_rows_are_scoped_to_the_cohort(settings: Settings) -> None:
    rows = digest.collect_digest_rows(settings, BENCH, cohort_id=ACTIVE)

    assert len(rows) == 7
    assert all(ACTIVE in row.name for row in rows if HISTORICAL in row.name or ACTIVE in row.name)
    assert not any(HISTORICAL in row.name for row in rows)
    assert not any(row.name in LEGACY_SLEEVES for row in rows)


def test_digest_picks_the_selected_cohorts_benchmark(settings: Settings) -> None:
    rows = digest.collect_digest_rows(settings, BENCH, cohort_id=ACTIVE)
    bench_rows = [row for row in rows if row.is_benchmark]

    assert len(bench_rows) == 1
    assert ACTIVE in bench_rows[0].name


def test_digest_on_the_superseded_cohort_shows_only_its_records(settings: Settings) -> None:
    rows = digest.collect_digest_rows(settings, BENCH, cohort_id=HISTORICAL)

    assert len(rows) == 7
    assert all(HISTORICAL in row.name for row in rows)


def test_digest_refuses_a_benchmark_from_another_cohort(settings: Settings) -> None:
    historical_bench = next(
        cfg for cfg in _configs(settings) if cfg.cohort_id == HISTORICAL and cfg.name == BENCH
    )

    with pytest.raises(benchmark_scope.BenchmarkScopeError):
        digest.collect_digest_rows(settings, historical_bench.sleeve_id, cohort_id=ACTIVE)


def test_digest_reports_an_unknown_cohort_as_a_lookup_error(settings: Settings) -> None:
    with pytest.raises(LookupError):
        digest.collect_digest_rows(settings, BENCH, cohort_id="paper-nope-2026-01-01")


def test_digest_command_reports_an_unknown_cohort_cleanly(settings: Settings) -> None:
    result = runner.invoke(cli.app, ["digest", "--benchmark", BENCH, "--cohort", "paper-nope"])

    assert result.exit_code == 1
    assert "Error:" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_digest_message_names_its_cohort_and_counts_only_its_members(settings: Settings) -> None:
    message = digest.build_daily_digest(settings, benchmark=BENCH, now=NOW, cohort_id=ACTIVE)

    assert ACTIVE in message.subject
    assert f"Cohort: {ACTIVE}" in message.body
    assert "across 7 sleeves" in message.body
    assert ACTIVE in (message.html_body or "")
    for legacy in LEGACY_SLEEVES:
        assert legacy not in message.body


def test_digest_without_a_cohort_is_unchanged(settings: Settings) -> None:
    """Backward compatible: the unscoped digest still covers every sleeve."""
    rows = digest.collect_digest_rows(settings, BENCH)
    message = digest.build_daily_digest(settings, benchmark=BENCH, now=NOW)

    assert len(rows) == 16  # 7 + 7 cohort members + 2 legacy
    assert "Cohort:" not in message.body
    # No scope marker between the date and the leader: the subject format is unchanged.
    assert message.subject.startswith("Sleeve digest 2026-07-30 - ")


# --- the scheduler wrapper --------------------------------------------------------


def _load_run_sleeves():
    path = Path(__file__).parents[1] / "scripts" / "run_sleeves.py"
    spec = importlib.util.spec_from_file_location("run_sleeves_scope_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def scheduler(monkeypatch: pytest.MonkeyPatch):
    """The wrapper with its CLI replaced by a recorder: nothing is executed."""
    module = _load_run_sleeves()
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(module, "_run_cli", lambda *args: calls.append(args) or 0)
    monkeypatch.setattr(module.market_calendar, "eastern_now", lambda: datetime(2026, 7, 30))
    return module, calls


def test_runner_passes_the_cohort_to_compare_and_digest(monkeypatch, scheduler) -> None:
    module, calls = scheduler
    monkeypatch.setattr(
        sys, "argv", ["run_sleeves.py", "--cohort", ACTIVE, "--compare", "--email"]
    )

    assert module.main() == 0
    assert calls[1] == ("sleeve", "compare", "--benchmark", BENCH, "--cohort", ACTIVE)
    assert calls[2] == ("digest", "--send", "--benchmark", BENCH, "--cohort", ACTIVE)


def test_runner_leaves_an_all_run_unscoped(monkeypatch, scheduler) -> None:
    module, calls = scheduler
    monkeypatch.setattr(sys, "argv", ["run_sleeves.py", "--compare", "--email"])

    assert module.main() == 0
    assert calls[0] == ("sleeve", "run", "--official", "--all")
    assert calls[1] == ("sleeve", "compare", "--benchmark", BENCH)
    assert calls[2] == ("digest", "--send", "--benchmark", BENCH)


def test_the_scoped_runner_command_is_the_one_that_reports_correctly(
    monkeypatch, scheduler, settings: Settings
) -> None:
    """End to end: the command the wrapper builds produces a July-28-only table."""
    module, calls = scheduler
    monkeypatch.setattr(sys, "argv", ["run_sleeves.py", "--cohort", ACTIVE, "--compare"])
    module.main()

    result = runner.invoke(cli.app, list(calls[1]))

    assert result.exit_code == 0, result.output
    assert _row_names(result.output) == set(JULY_28_MEMBERS)
    assert HISTORICAL not in result.output
