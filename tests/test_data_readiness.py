"""Tests for data-readiness evaluation (offline)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from schwab_trader.data_contracts import (
    FakeDailyBarSource,
    FakeFundamentalSource,
    FakeMacroSource,
)
from schwab_trader.data_readiness import (
    DataKind,
    DataRequirement,
    ReasonCode,
    SourceProbe,
    evaluate_readiness,
    evaluate_requirement,
)

_NOW = datetime(2026, 7, 21, 20, 0, tzinfo=UTC)
_DAY = date(2026, 7, 21)


def _bars(symbols: dict[str, Decimal], *, as_of: datetime = _NOW) -> object:
    source = FakeDailyBarSource(symbols=symbols, as_of=as_of)
    return source.fetch_daily_bars(tuple(symbols), _DAY, _DAY)


def test_empty_requirements_are_trivially_ready() -> None:
    result = evaluate_readiness([], now=_NOW)
    assert result.ready is True
    assert result.requirements == ()


def test_price_only_ready_while_macro_unavailable() -> None:
    bars_req = DataRequirement(kind=DataKind.DAILY_BARS, keys=("AAPL", "MSFT"))
    batch = _bars({"AAPL": Decimal("100"), "MSFT": Decimal("200")})
    result = evaluate_readiness([(bars_req, SourceProbe.of(batch))], now=_NOW)
    assert result.ready is True
    assert result.requirements[0].reasons == (ReasonCode.OK,)
    assert result.requirements[0].coverage == 1.0
    assert result.snapshot_ids[DataKind.DAILY_BARS.value] == batch.provenance.snapshot_id


def test_missing_source_is_unready() -> None:
    req = DataRequirement(kind=DataKind.MACRO, keys=("VIXCLS",))
    result = evaluate_requirement(req, SourceProbe.missing(), now=_NOW)
    assert result.ready is False
    assert result.reasons == (ReasonCode.SOURCE_MISSING,)
    assert result.coverage == 0.0
    assert result.snapshot_id is None


def test_disabled_source_is_unready() -> None:
    req = DataRequirement(kind=DataKind.FUNDAMENTALS, keys=("AAPL",))
    result = evaluate_requirement(req, SourceProbe.disabled(), now=_NOW)
    assert result.reasons == (ReasonCode.SOURCE_DISABLED,)
    assert result.ready is False


def test_empty_batch_is_no_data() -> None:
    req = DataRequirement(kind=DataKind.DAILY_BARS, keys=("AAPL",))
    empty = _bars({})
    result = evaluate_requirement(req, SourceProbe.of(empty), now=_NOW)
    assert result.reasons == (ReasonCode.NO_DATA,)
    assert result.ready is False


def test_partial_coverage_reports_missing_keys() -> None:
    req = DataRequirement(kind=DataKind.DAILY_BARS, keys=("AAPL", "MSFT", "TSLA"))
    batch = _bars({"AAPL": Decimal("100"), "MSFT": Decimal("200")})
    result = evaluate_requirement(req, SourceProbe.of(batch), now=_NOW)
    assert result.ready is False
    assert ReasonCode.MISSING_KEYS in result.reasons
    assert result.missing_keys == ("TSLA",)
    assert result.present_keys == ("AAPL", "MSFT")
    assert abs(result.coverage - 2 / 3) < 1e-9


def test_stale_batch_is_flagged() -> None:
    req = DataRequirement(
        kind=DataKind.DAILY_BARS, keys=("AAPL",), max_staleness=timedelta(hours=1)
    )
    old = _bars({"AAPL": Decimal("100")}, as_of=_NOW - timedelta(days=3))
    result = evaluate_requirement(req, SourceProbe.of(old), now=_NOW)
    assert result.ready is False
    assert ReasonCode.STALE in result.reasons


def test_vintage_requirement_rejects_latest_revised_macro() -> None:
    req = DataRequirement(kind=DataKind.MACRO, keys=("VIXCLS",), require_vintage_safe=True)
    source = FakeMacroSource(series={"VIXCLS": Decimal("15")}, as_of=_NOW, available_at=_NOW)
    batch = source.fetch_macro(("VIXCLS",), _DAY)  # latest-revised by default
    result = evaluate_requirement(req, SourceProbe.of(batch), now=_NOW)
    assert result.ready is False
    assert ReasonCode.NOT_VINTAGE_SAFE in result.reasons


def test_vintage_requirement_accepts_point_in_time_macro() -> None:
    req = DataRequirement(kind=DataKind.MACRO, keys=("VIXCLS",), require_vintage_safe=True)
    source = FakeMacroSource(
        series={"VIXCLS": Decimal("15")}, as_of=_NOW, available_at=_NOW, vintage_safe=True
    )
    batch = source.fetch_macro(("VIXCLS",), _DAY)
    result = evaluate_requirement(req, SourceProbe.of(batch), now=_NOW)
    assert result.ready is True


def test_fundamentals_ready_end_to_end() -> None:
    source = FakeFundamentalSource(
        records={"AAPL": {"pe": Decimal("30")}}, as_of=_NOW, available_at=_NOW
    )
    batch = source.fetch_fundamentals(("AAPL",), _DAY)
    req = DataRequirement(kind=DataKind.FUNDAMENTALS, keys=("AAPL",))
    result = evaluate_requirement(req, SourceProbe.of(batch), now=_NOW)
    assert result.ready is True
    assert result.reasons == (ReasonCode.OK,)


def test_aggregate_ready_only_when_all_ready() -> None:
    bars_req = DataRequirement(kind=DataKind.DAILY_BARS, keys=("AAPL",))
    macro_req = DataRequirement(kind=DataKind.MACRO, keys=("VIXCLS",))
    bars_batch = _bars({"AAPL": Decimal("100")})
    result = evaluate_readiness(
        [
            (bars_req, SourceProbe.of(bars_batch)),
            (macro_req, SourceProbe.missing()),
        ],
        now=_NOW,
    )
    assert result.ready is False
    assert DataKind.MACRO in result.missing_capabilities
    assert DataKind.DAILY_BARS not in result.missing_capabilities
    assert len(result.unready()) == 1


def test_stale_capabilities_property() -> None:
    req = DataRequirement(
        kind=DataKind.DAILY_BARS, keys=("AAPL",), max_staleness=timedelta(hours=1)
    )
    old = _bars({"AAPL": Decimal("100")}, as_of=_NOW - timedelta(days=2))
    result = evaluate_readiness([(req, SourceProbe.of(old))], now=_NOW)
    assert result.stale_capabilities == (DataKind.DAILY_BARS,)
    assert result.ready is False
