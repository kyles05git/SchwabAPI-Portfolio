"""Tests for the strategy validation / promotion registry (offline)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from schwab_trader.backtest import WalkForwardFold, WalkForwardResult
from schwab_trader.promotion import (
    PromotionStore,
    PromotionVerdict,
    assess_verdict,
    configuration_fingerprint,
    manifest_from_walkforward,
    require_validated,
    verdict_from_walkforward,
)

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


def _fold(passed: bool) -> WalkForwardFold:
    return WalkForwardFold(
        end_day=NOW,
        return_pct=Decimal("5"),
        sharpe=Decimal("1.2"),
        max_drawdown_pct=Decimal("8"),
        benchmark_return_pct=Decimal("6"),
        excess_pct=Decimal("-1"),
        passed=passed,
    )


def _wf(passed: int, total: int, *, mean_excess: str | None = "-1") -> WalkForwardResult:
    folds = [_fold(True) for _ in range(passed)] + [_fold(False) for _ in range(total - passed)]
    return WalkForwardResult(
        folds=folds,
        mean_return_pct=Decimal("10"),
        median_return_pct=Decimal("9"),
        worst_return_pct=Decimal("-0.5"),
        mean_excess_pct=Decimal(mean_excess) if mean_excess is not None else None,
        pass_rate=passed / total,
    )


def _manifest(
    result: WalkForwardResult,
    *,
    code_revision: str = "git:test",
    symbols: list[str] | None = None,
    requested_folds: int | None = None,
):
    return manifest_from_walkforward(
        result,
        strategy="trend",
        universe_symbols=symbols or ["AAA", "BBB"],
        factor="",
        max_positions=20,
        max_position_fraction=Decimal("0.10"),
        benchmark="SPY",
        window=180,
        step=90,
        requested_folds=requested_folds or len(result.folds),
        cost_bps=10.0,
        settlement_t1=True,
        leverage=Decimal("1"),
        dividends=False,
        code_revision=code_revision,
    )


def _fingerprint(symbols: list[str] | None = None) -> str:
    return configuration_fingerprint(
        strategy="trend",
        symbols=symbols or ["AAA", "BBB"],
        factor="",
        max_positions=20,
        max_position_fraction=Decimal("0.10"),
        benchmark="SPY",
        settlement_t1=True,
        leverage=Decimal("1"),
    )


def test_validated_requires_gates_and_beating_benchmark() -> None:
    # Clears the pass-rate bar AND beats the benchmark -> validated.
    v = verdict_from_walkforward(
        _wf(4, 6, mean_excess="3.5"), strategy="trend", universe="large-cap", min_pass_rate=0.6
    )
    assert v.passing_folds == 4 and v.folds == 6
    assert v.pass_rate == 4 / 6  # 0.67 >= 0.6
    assert v.beats_benchmark
    assert v.validated


def test_not_validated_when_lags_benchmark() -> None:
    # Clears the risk gates but lags the benchmark -> NOT validated (the fix).
    v = verdict_from_walkforward(
        _wf(4, 6, mean_excess="-1"), strategy="trend", universe="large-cap", min_pass_rate=0.6
    )
    assert v.pass_rate >= 0.6
    assert not v.beats_benchmark
    assert not v.validated


def test_not_validated_without_benchmark() -> None:
    # No benchmark comparison -> cannot confirm it beats the benchmark -> fail closed.
    v = verdict_from_walkforward(
        _wf(5, 6, mean_excess=None), strategy="x", universe="u", min_pass_rate=0.6
    )
    assert v.mean_excess_pct is None
    assert not v.validated


def test_not_validated_below_pass_rate_even_if_beats_benchmark() -> None:
    v = verdict_from_walkforward(
        _wf(2, 6, mean_excess="3.5"), strategy="mean-reversion", universe="lc", min_pass_rate=0.6
    )
    assert v.beats_benchmark
    assert not v.validated  # 0.33 < 0.6


def test_beats_benchmark_flag() -> None:
    v = verdict_from_walkforward(
        _wf(5, 6, mean_excess="3.5"), strategy="x", universe="u", min_pass_rate=0.6
    )
    assert v.beats_benchmark


def test_validated_recomputed_ignores_stale_stored_flag() -> None:
    # A verdict recorded under the OLD rule: stored validated=true even though it lags
    # the benchmark. Loading must recompute validated=False under the current rule, so
    # the tightened gate applies retroactively (the stored flag is ignored on input).
    stale_json = (
        '{"strategy":"trend","universe":"lc","created_at":"2026-01-01T00:00:00+00:00",'
        '"folds":6,"passing_folds":5,"pass_rate":0.83,"mean_return_pct":"10",'
        '"worst_return_pct":"-1","mean_excess_pct":"-2","min_pass_rate":0.6,'
        '"validated":true}'
    )
    v = PromotionVerdict.model_validate_json(stale_json)
    assert v.pass_rate >= v.min_pass_rate  # would have cleared the old rule
    assert not v.beats_benchmark
    assert v.validated is False  # recomputed; stale stored flag ignored


def test_validated_round_trips_through_json() -> None:
    v = verdict_from_walkforward(
        _wf(5, 6, mean_excess="3.5"), strategy="x", universe="u", min_pass_rate=0.6
    )
    assert v.validated
    reloaded = PromotionVerdict.model_validate_json(v.model_dump_json())
    assert reloaded.validated is True  # computed field serialized and recomputed alike


def test_store_records_and_returns_latest(tmp_path) -> None:
    store = PromotionStore(tmp_path / "promo.sqlite3")
    store.record(
        verdict_from_walkforward(_wf(2, 6), strategy="momentum", universe="lc", min_pass_rate=0.6)
    )
    store.record(
        verdict_from_walkforward(_wf(5, 6), strategy="momentum", universe="lc", min_pass_rate=0.6)
    )
    latest = store.latest("momentum", "lc")
    assert latest is not None and latest.passing_folds == 5  # newest wins
    assert len(store.all_latest()) == 1


def test_require_validated_gate(tmp_path) -> None:
    store = PromotionStore(tmp_path / "promo.sqlite3")
    # No verdict -> fails closed.
    ok, reason = require_validated(store, "trend", "lc")
    assert not ok and "no validation" in reason

    # Too few folds pass -> reason cites the pass-rate bar.
    store.record(
        verdict_from_walkforward(
            _wf(2, 6, mean_excess="3.5"), strategy="trend", universe="lc", min_pass_rate=0.6
        )
    )
    ok, reason = require_validated(store, "trend", "lc")
    assert not ok and "failed validation" in reason

    # Clears the gates but lags the benchmark -> reason cites the benchmark.
    store.record(
        verdict_from_walkforward(
            _wf(5, 6, mean_excess="-1"), strategy="trend", universe="lc", min_pass_rate=0.6
        )
    )
    ok, reason = require_validated(store, "trend", "lc")
    assert not ok and "does not beat the benchmark" in reason

    # No benchmark comparison -> its own reason, still fails closed.
    store.record(
        verdict_from_walkforward(
            _wf(5, 6, mean_excess=None), strategy="trend", universe="lc", min_pass_rate=0.6
        )
    )
    ok, reason = require_validated(store, "trend", "lc")
    assert not ok and "no benchmark comparison" in reason

    # Clears the statistical gates but has no manifest -> legacy record fails closed.
    passing = _wf(5, 6, mean_excess="3.5")
    store.record(
        verdict_from_walkforward(
            passing,
            strategy="trend",
            universe="lc",
            min_pass_rate=0.6,
            now=NOW,
        )
    )
    ok, reason = require_validated(store, "trend", "lc", now=NOW)
    assert not ok and "no provenance manifest" in reason

    # Fresh, reproducible, compatible manifest -> authorized.
    store.record(
        verdict_from_walkforward(
            passing,
            strategy="trend",
            universe="lc",
            min_pass_rate=0.6,
            manifest=_manifest(passing),
            now=NOW,
        )
    )
    ok, reason = require_validated(
        store,
        "trend",
        "lc",
        expected_configuration_fingerprint=_fingerprint(),
        active_code_revision="git:test",
        now=NOW,
    )
    assert ok and reason == "validated and compatible"


def test_assessment_expires_verdict_and_market_data() -> None:
    passing = _wf(5, 6, mean_excess="3.5")
    verdict = verdict_from_walkforward(
        passing,
        strategy="trend",
        universe="lc",
        min_pass_rate=0.6,
        manifest=_manifest(passing),
        now=NOW,
    )
    assessment = assess_verdict(
        verdict,
        max_age_days=45,
        expected_configuration_fingerprint=_fingerprint(),
        active_code_revision="git:test",
        now=NOW + timedelta(days=46),
    )
    assert not assessment.authorized
    assert "validation is 46 days old" in assessment.reason
    assert "tested market data is 46 days old" in assessment.reason


def test_assessment_detects_runtime_and_code_changes() -> None:
    passing = _wf(5, 6, mean_excess="3.5")
    verdict = verdict_from_walkforward(
        passing,
        strategy="trend",
        universe="lc",
        min_pass_rate=0.6,
        manifest=_manifest(passing),
        now=NOW,
    )
    assessment = assess_verdict(
        verdict,
        expected_configuration_fingerprint=_fingerprint(["AAA", "CCC"]),
        active_code_revision="git:new",
        now=NOW,
    )
    assert not assessment.authorized
    assert "code revision changed" in assessment.reason
    assert "settings, factor, or exact universe changed" in assessment.reason


def test_assessment_rejects_dirty_or_incomplete_validation() -> None:
    passing = _wf(5, 6, mean_excess="3.5")
    verdict = verdict_from_walkforward(
        passing,
        strategy="trend",
        universe="lc",
        min_pass_rate=0.6,
        manifest=_manifest(passing, code_revision="git:test+dirty", requested_folds=7),
        now=NOW,
    )
    assessment = assess_verdict(verdict, now=NOW)
    assert not assessment.authorized
    assert "6/7 requested folds" in assessment.reason
    assert "dirty working tree" in assessment.reason
