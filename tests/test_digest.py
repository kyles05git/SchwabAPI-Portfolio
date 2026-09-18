"""Tests for the weekly performance digest (offline; no network)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from schwab_trader import digest, sleeves
from schwab_trader.config import Settings
from schwab_trader.notify import NotifyMessage

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        token_path=tmp_path / "tokens.json",
        state_db_path=tmp_path / "state.sqlite3",
        log_path=tmp_path / "logs" / "app.log",
        sleeves_dir=tmp_path / "sleeves",
    )


def _seed_two_sleeves(settings: Settings) -> None:
    store = sleeves.SleeveStore(settings.sleeves_dir)
    store.create(
        "bench-spy",
        strategy="buy-hold",
        universe=["SPY"],
        starting_cash=Decimal("5000"),
        max_positions=1,
        max_position_fraction=Decimal("1.0"),
    )
    store.create(
        "momentum",
        strategy="momentum",
        universe=[],
        starting_cash=Decimal("5000"),
        max_positions=8,
        max_position_fraction=Decimal("0.10"),
    )


def test_digest_with_no_sleeves(tmp_path: Path) -> None:
    message = digest.build_daily_digest(_settings(tmp_path), now=NOW)
    assert isinstance(message, NotifyMessage)
    assert "No sleeves" in message.body
    assert "2026-07-20" in message.subject


def test_digest_is_a_notify_message_with_info_category(tmp_path: Path) -> None:
    _seed_two_sleeves(settings := _settings(tmp_path))
    message = digest.build_daily_digest(settings, benchmark="bench-spy", now=NOW)
    assert isinstance(message, NotifyMessage)
    assert message.category == "info"


def test_digest_lists_sleeves_and_benchmark(tmp_path: Path) -> None:
    _seed_two_sleeves(settings := _settings(tmp_path))
    rows = digest.collect_digest_rows(settings, "bench-spy")
    names = {r.name for r in rows}
    assert names == {"bench-spy", "momentum"}
    bench = next(r for r in rows if r.name == "bench-spy")
    assert bench.is_benchmark is True
    assert bench.excess_pct is None
    # No cycles recorded yet -> value falls back to the sleeve's starting cash.
    assert all(r.value == Decimal("5000") for r in rows)


def test_digest_states_when_nothing_beats_benchmark(tmp_path: Path) -> None:
    # With no recorded cycles every sleeve is flat (0%), so none *beats* the benchmark;
    # the digest must say so honestly rather than imply an edge.
    _seed_two_sleeves(settings := _settings(tmp_path))
    message = digest.build_daily_digest(settings, benchmark="bench-spy", now=NOW)
    assert "None of" in message.body
    assert "bench-spy" in message.body
    assert "less drawdown" in message.body  # the honest framing


def test_digest_reports_beaters_when_a_sleeve_leads(tmp_path: Path) -> None:
    rows = [
        digest.DigestRow(
            name="momentum",
            strategy="momentum",
            cycles=4,
            trades=3,
            value=Decimal("5300"),
            return_pct=Decimal("6.00"),
            excess_pct=Decimal("2.00"),
            max_drawdown_pct=Decimal("1.5"),
            sharpe=Decimal("1.30"),
            is_benchmark=False,
        ),
        digest.DigestRow(
            name="bench-spy",
            strategy="buy-hold",
            cycles=4,
            trades=1,
            value=Decimal("5200"),
            return_pct=Decimal("4.00"),
            excess_pct=None,
            max_drawdown_pct=Decimal("2.0"),
            sharpe=Decimal("1.10"),
            is_benchmark=True,
        ),
    ]
    subject, body = digest.render_digest_text(rows, "bench-spy", now=NOW)
    assert "1 of 1 sleeves beat bench-spy" in body
    assert "momentum" in body
    assert "momentum leads +6.00%" in subject


def test_daily_digest_includes_html_body(tmp_path: Path) -> None:
    _seed_two_sleeves(settings := _settings(tmp_path))
    message = digest.build_daily_digest(settings, benchmark="bench-spy", now=NOW)
    assert message.html_body is not None
    assert "<!doctype html>" in message.html_body
    assert "Sleeve digest" in message.html_body
    assert "momentum" in message.html_body  # a sleeve row rendered in the table
    # Plain-text body remains as the fallback.
    assert "momentum" in message.body
