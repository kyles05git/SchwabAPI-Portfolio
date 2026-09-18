"""Offline CLI tests for explainable validation authorization."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from schwab_trader import cli, promotion
from schwab_trader.backtest import WalkForwardFold, WalkForwardResult
from schwab_trader.config import Settings

runner = CliRunner()
NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _freeze_assessment_clock(monkeypatch) -> None:
    """Keep CLI assessment and recorded evidence on the same deterministic clock."""

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)

    monkeypatch.setattr(promotion, "datetime", FrozenDatetime)


def _result(*, data_as_of: datetime = NOW) -> WalkForwardResult:
    folds = [
        WalkForwardFold(
            end_day=data_as_of,
            return_pct=Decimal("8"),
            sharpe=Decimal("1.2"),
            max_drawdown_pct=Decimal("8"),
            benchmark_return_pct=Decimal("6"),
            excess_pct=Decimal("2"),
            passed=True,
        )
        for _ in range(3)
    ]
    return WalkForwardResult(
        folds=folds,
        mean_return_pct=Decimal("8"),
        median_return_pct=Decimal("8"),
        worst_return_pct=Decimal("8"),
        mean_excess_pct=Decimal("2"),
        pass_rate=1.0,
    )


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        promotion_db_path=tmp_path / "promotion.sqlite3",
        log_path=tmp_path / "logs" / "schwab_trader.log",
        agent_max_positions=20,
        agent_max_position_fraction=Decimal("0.10"),
    )


def _record(
    settings: Settings,
    *,
    manifest: bool,
    created_at: datetime = NOW,
    data_as_of: datetime = NOW,
) -> None:
    result = _result(data_as_of=data_as_of)
    provenance = (
        promotion.manifest_from_walkforward(
            result,
            strategy="trend",
            universe_symbols=["AAA", "BBB"],
            factor="",
            max_positions=20,
            max_position_fraction=Decimal("0.10"),
            benchmark="SPY",
            window=180,
            step=90,
            requested_folds=3,
            cost_bps=10.0,
            settlement_t1=True,
            leverage=Decimal("1"),
            dividends=False,
            code_revision="git:test",
        )
        if manifest
        else None
    )
    promotion.PromotionStore(settings.promotion_db_path).record(
        promotion.verdict_from_walkforward(
            result,
            strategy="trend",
            universe="AAA,BBB",
            min_pass_rate=0.6,
            manifest=provenance,
            now=created_at,
        )
    )


@pytest.mark.parametrize("age_days", [0, 45])
def test_validate_explain_authorized_manifest(tmp_path, monkeypatch, age_days) -> None:
    settings = _settings(tmp_path)
    evidence_date = NOW - timedelta(days=age_days)
    _record(settings, manifest=True, created_at=evidence_date, data_as_of=evidence_date)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(promotion, "current_code_revision", lambda _root=None: "git:test")

    result = runner.invoke(
        cli.app,
        ["validate", "explain", "--strategy", "trend", "--symbols", "AAA,BBB"],
    )

    assert result.exit_code == 0, result.output
    assert "live authorized" in result.output.lower()
    assert "YES" in result.output
    assert "validated and compatible" in result.output
    assert "git:test" in result.output


def test_validate_explain_blocks_legacy_record(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    _record(settings, manifest=False)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(promotion, "current_code_revision", lambda _root=None: "git:test")

    result = runner.invoke(
        cli.app,
        ["validate", "explain", "--strategy", "trend", "--symbols", "AAA,BBB"],
    )

    assert result.exit_code == 0, result.output
    assert "NO" in result.output
    assert "no provenance manifest" in result.output


@pytest.mark.parametrize(
    ("verdict_age", "data_age", "reason"),
    [
        (46, 0, "validation is 46 days old"),
        (0, 46, "tested market data is 46 days old"),
    ],
)
def test_validate_explain_blocks_stale_evidence(
    tmp_path, monkeypatch, verdict_age, data_age, reason
) -> None:
    settings = _settings(tmp_path)
    _record(
        settings,
        manifest=True,
        created_at=NOW - timedelta(days=verdict_age),
        data_as_of=NOW - timedelta(days=data_age),
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(promotion, "current_code_revision", lambda _root=None: "git:test")

    result = runner.invoke(
        cli.app,
        ["validate", "explain", "--strategy", "trend", "--symbols", "AAA,BBB"],
    )

    assert result.exit_code == 0, result.output
    assert "NO" in result.output
    assert reason in result.output
