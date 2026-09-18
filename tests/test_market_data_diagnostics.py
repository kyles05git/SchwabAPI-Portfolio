"""Offline read-only diagnostics, monitoring, and reconciliation tests."""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from schwab_trader import (
    cli,
    market_bar_evidence,
    market_calendar,
    market_data,
    market_data_cli,
    market_data_diagnostics,
)
from schwab_trader.config import Settings
from schwab_trader.market_bar_evidence import DerivedDailyEvidence
from schwab_trader.market_data import Candle

SESSION = date(2026, 7, 28)
AFTER_CLOSE = datetime(2026, 7, 28, 20, 1, tzinfo=UTC)


def _daily(session: date, *, symbol: str = "SPY") -> Candle:
    return Candle(
        symbol=symbol,
        date=datetime.combine(session, datetime.min.time(), UTC),
        open=Decimal("100"),
        high=Decimal("179"),
        low=Decimal("99"),
        close=Decimal("178"),
        volume=sum(range(1, 79)),
        source=market_data.SCHWAB_DAILY_HISTORY_SOURCE,
    )


def _complete(session: date = SESSION, *, symbol: str = "SPY") -> list[Candle]:
    result: list[Candle] = []
    for index, stamp in enumerate(market_calendar.session_interval_starts_utc(session)):
        value = Decimal(100 + index)
        result.append(
            Candle(
                symbol=symbol,
                date=stamp,
                open=value,
                high=value + 2,
                low=value - 1,
                close=value + 1,
                volume=index + 1,
                source=market_data.SCHWAB_REGULAR_SESSION_SOURCE,
            )
        )
    return result


def _evidence() -> DerivedDailyEvidence:
    result = market_bar_evidence.validate_regular_session(
        "SPY",
        SESSION,
        _complete(),
        retrieved_at=AFTER_CLOSE,
    )
    assert result.evidence is not None
    return result.evidence


class _Store:
    def __init__(self, evidence: DerivedDailyEvidence) -> None:
        self.evidence = evidence
        self.save_calls = 0

    def save(self, evidence: DerivedDailyEvidence) -> DerivedDailyEvidence:
        del evidence
        self.save_calls += 1
        raise AssertionError("read-only reconciliation must not save")

    def get(self, dataset_id: str) -> DerivedDailyEvidence | None:
        return self.evidence if dataset_id == self.evidence.dataset_id else None

    def for_session(
        self,
        symbol: str,
        session_date: date,
    ) -> tuple[DerivedDailyEvidence, ...]:
        del symbol, session_date
        return ()

    def reproduce(self, dataset_id: str) -> DerivedDailyEvidence | None:
        return self.get(dataset_id)


def test_diagnostic_uses_official_daily_without_intraday_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        market_data,
        "get_price_history",
        lambda client, symbol, days: [_daily(SESSION)],
    )

    def forbidden(*args: object, **kwargs: object) -> list[Candle]:
        del args, kwargs
        raise AssertionError("intraday request was unnecessary")

    monkeypatch.setattr(market_data, "get_regular_session_history", forbidden)
    payload = market_data_diagnostics.diagnose_symbol(
        object(),  # type: ignore[arg-type]
        "spy",
        SESSION,
        clock=lambda: AFTER_CLOSE,
    )

    assert payload["state"] == "official_daily"
    assert payload["latest_official_session"] == SESSION.isoformat()
    assert payload["intraday_requested"] is False
    assert payload["evidence_source"] == "schwab-official-daily"


def test_diagnostic_reports_complete_exact_session_derived_ohlcv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prior = market_calendar.previous_trading_day(SESSION)
    monkeypatch.setattr(
        market_data,
        "get_price_history",
        lambda client, symbol, days: [_daily(prior)],
    )
    monkeypatch.setattr(
        market_data,
        "get_regular_session_history",
        lambda client, symbol, session: _complete(session),
    )

    payload = market_data_diagnostics.diagnose_symbol(
        object(),  # type: ignore[arg-type]
        "SPY",
        SESSION,
        clock=lambda: AFTER_CLOSE,
    )

    assert payload["state"] == "intraday_derived_safe"
    assert payload["expected_interval_count"] == 78
    assert payload["observed_interval_count"] == 78
    assert payload["aggregation_safe"] is True
    derived = payload["derived"]
    assert isinstance(derived, dict)
    assert derived["source"] == "schwab-intraday-derived-daily"
    assert derived["volume"] == sum(range(1, 79))


def test_provider_error_payload_uses_only_sanitized_exception_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*args: object, **kwargs: object) -> list[Candle]:
        del args, kwargs
        raise RuntimeError("oauth_token=do-not-print")

    monkeypatch.setattr(market_data, "get_price_history", unavailable)
    payload = market_data_diagnostics.diagnose_symbol(
        object(),  # type: ignore[arg-type]
        "SPY",
        SESSION,
        clock=lambda: AFTER_CLOSE,
    )
    encoded = json.dumps(payload).casefold()

    assert payload["state"] == "provider_error"
    assert payload["error"] == {"phase": "daily", "kind": "RuntimeError"}
    assert "do-not-print" not in encoded
    for forbidden in ("oauth_token", "database_url", "connection_string"):
        assert forbidden not in encoded


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def test_monitor_records_first_seen_milestones_and_stops_when_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(AFTER_CLOSE)
    daily_calls = 0
    intraday_calls = 0
    prior = market_calendar.previous_trading_day(SESSION)

    def daily(client: Any, symbol: str, *, days: int) -> list[Candle]:
        nonlocal daily_calls
        del client, symbol, days
        daily_calls += 1
        return [_daily(prior)] if daily_calls == 1 else [_daily(SESSION)]

    def intraday(client: Any, symbol: str, session: date) -> list[Candle]:
        nonlocal intraday_calls
        del client, symbol
        intraday_calls += 1
        bars = _complete(session)
        return bars[:-1] if intraday_calls == 1 else bars

    monkeypatch.setattr(market_data, "get_price_history", daily)
    monkeypatch.setattr(market_data, "get_regular_session_history", intraday)
    rows = list(
        market_data_diagnostics.monitor_session_bars(
            object(),  # type: ignore[arg-type]
            ("SPY",),
            SESSION,
            poll_seconds=30,
            stop_at=AFTER_CLOSE + timedelta(minutes=5),
            clock=clock,
            sleeper=clock.sleep,
        )
    )

    assert len(rows) == 2
    assert rows[0]["complete"] is False
    assert rows[1]["complete"] is True
    symbol = rows[1]["symbols"][0]  # type: ignore[index]
    assert symbol["official_daily_first_seen_at"] == clock.now.isoformat()
    assert symbol["final_interval_first_seen_at"] == clock.now.isoformat()
    assert daily_calls == intraday_calls == 2


def test_monitor_rejects_aggressive_or_unbounded_configuration() -> None:
    with pytest.raises(ValueError, match="at least 30"):
        list(
            market_data_diagnostics.monitor_session_bars(
                object(),  # type: ignore[arg-type]
                ("SPY",),
                SESSION,
                poll_seconds=1,
                stop_at=AFTER_CLOSE + timedelta(minutes=5),
                clock=lambda: AFTER_CLOSE,
            )
        )
    with pytest.raises(ValueError, match="24 hours"):
        list(
            market_data_diagnostics.monitor_session_bars(
                object(),  # type: ignore[arg-type]
                ("SPY",),
                SESSION,
                poll_seconds=60,
                stop_at=AFTER_CLOSE + timedelta(hours=25),
                clock=lambda: AFTER_CLOSE,
            )
        )


def test_reconciliation_reports_equal_and_materially_different_official_candles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _evidence()
    store = _Store(evidence)
    monkeypatch.setattr(
        market_data,
        "get_price_history",
        lambda client, symbol, days: [
            evidence.candle.model_copy(update={"source": market_data.SCHWAB_DAILY_HISTORY_SOURCE})
        ],
    )
    equal = market_data_diagnostics.reconcile_derived_daily(
        object(),  # type: ignore[arg-type]
        store,
        evidence.dataset_id,
        clock=lambda: AFTER_CLOSE,
    )

    assert equal["status"] == "compared"
    assert equal["material_discrepancy"] is False
    assert store.save_calls == 0

    monkeypatch.setattr(
        market_data,
        "get_price_history",
        lambda client, symbol, days: [
            evidence.candle.model_copy(
                update={
                    "close": evidence.candle.close + Decimal("0.02"),
                    "volume": evidence.candle.volume + 1,
                    "source": market_data.SCHWAB_DAILY_HISTORY_SOURCE,
                }
            )
        ],
    )
    different = market_data_diagnostics.reconcile_derived_daily(
        object(),  # type: ignore[arg-type]
        store,
        evidence.dataset_id,
        clock=lambda: AFTER_CLOSE,
    )

    assert different["material_discrepancy"] is True
    assert different["material_fields"] == ["close", "volume"]
    assert different["tolerances"] == {
        "price_absolute": "0.01",
        "volume_absolute": 0,
    }
    assert store.evidence == evidence
    assert store.save_calls == 0


def test_diagnose_cli_runs_end_to_end_with_offline_fixture_and_no_mutation_paths(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, log_path=tmp_path / "diagnostic.log")
    monkeypatch.setattr(market_data_cli, "get_settings", lambda: settings)
    monkeypatch.setattr(
        market_data_cli,
        "_build_client",
        lambda settings: contextlib.nullcontext(object()),
    )
    monkeypatch.setattr(
        market_data,
        "get_price_history",
        lambda client, symbol, days: [_daily(SESSION, symbol=symbol)],
    )

    def forbidden(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("diagnostics must not reach a mutation path")

    monkeypatch.setattr(market_data_cli.storage_factory, "paper_engine", forbidden)
    monkeypatch.setattr(market_data_cli.storage_factory, "sleeve_store", forbidden)
    result = CliRunner().invoke(
        cli.app,
        [
            "market-data",
            "diagnose-bars",
            "--symbols",
            "SPY,XLB",
            "--session",
            SESSION.isoformat(),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert market_data_diagnostics.DIAGNOSTIC_SCHEMA in result.output
    assert "credential" not in result.output.casefold()


@pytest.mark.parametrize("raw", ["Infinity", "-Infinity", "inf", "NaN", "-nan", "sNaN"])
def test_non_finite_price_tolerance_is_rejected(raw: str) -> None:
    """A non-finite tolerance defeats the comparison it exists to make.

    ``Decimal`` parses both spellings happily. ``Infinity`` makes every price break
    "within tolerance" — an official open of 1.0 against a derived 100.0 reports no
    material discrepancy at all — and ``NaN`` passes the nonnegative check only to raise
    ``InvalidOperation`` on the first comparison.
    """
    with pytest.raises(typer.Exit):
        market_data_cli._decimal_option(raw, "--price-tolerance")


def test_finite_price_tolerances_are_still_accepted() -> None:
    assert market_data_cli._decimal_option("0", "--price-tolerance") == Decimal("0")
    assert market_data_cli._decimal_option("0.01", "--price-tolerance") == Decimal("0.01")
    assert market_data_cli._decimal_option("1E+2", "--price-tolerance") == Decimal("100")


def test_an_infinite_tolerance_would_have_hidden_a_material_price_break() -> None:
    """Pins why the guard matters: the comparison itself is what Infinity breaks."""
    official, derived = Decimal("1.0"), Decimal("100.0")
    assert abs(official - derived) <= Decimal("Infinity")  # every break "within tolerance"
    assert not abs(official - derived) <= Decimal("0.01")
