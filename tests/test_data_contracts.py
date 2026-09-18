"""Tests for the data-provider seam contracts and offline fakes (offline)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from schwab_trader.data_contracts import (
    FRED_LATEST_REVISED_VINTAGE_SAFE,
    BarBatch,
    BarObservation,
    DailyBarSource,
    DataBatch,
    FakeDailyBarSource,
    FakeFundamentalSource,
    FakeMacroSource,
    FundamentalBatch,
    FundamentalSource,
    MacroBatch,
    MacroSource,
    Provenance,
    TimingPolicy,
)

_NOW = datetime(2026, 7, 21, 20, 0, tzinfo=UTC)
_DAY = date(2026, 7, 21)


def _bar_provenance() -> Provenance:
    return Provenance(
        source="unit",
        snapshot_id="snap-1",
        retrieved_at=_NOW,
        as_of=_NOW,
        timing=TimingPolicy.SETTLED_EOD,
        vintage_safe=True,
    )


def test_provenance_requires_nonempty_snapshot_id() -> None:
    with pytest.raises(ValidationError):
        Provenance(
            source="unit",
            snapshot_id="",
            retrieved_at=_NOW,
            as_of=_NOW,
            timing=TimingPolicy.SETTLED_EOD,
            vintage_safe=True,
        )


def test_latest_revised_cannot_be_vintage_safe() -> None:
    with pytest.raises(ValidationError):
        Provenance(
            source="fred",
            snapshot_id="snap",
            retrieved_at=_NOW,
            as_of=_NOW,
            available_at=_NOW,
            timing=TimingPolicy.LATEST_REVISED,
            vintage_safe=True,
        )


def test_point_in_time_requires_available_at() -> None:
    with pytest.raises(ValidationError):
        Provenance(
            source="fund",
            snapshot_id="snap",
            retrieved_at=_NOW,
            as_of=_NOW,
            available_at=None,
            timing=TimingPolicy.POINT_IN_TIME,
            vintage_safe=True,
        )


def test_fred_latest_revised_marked_not_vintage_safe() -> None:
    # Explicit, importable marker that the default FRED feed is not backtest-safe.
    assert FRED_LATEST_REVISED_VINTAGE_SAFE is False


def test_bar_batch_coverage_and_empty() -> None:
    batch = BarBatch(
        provenance=_bar_provenance(),
        bars=(
            BarObservation(
                symbol="AAPL",
                session_date=_DAY,
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100.5"),
                volume=1000,
            ),
        ),
    )
    assert batch.covered_keys == {"AAPL"}
    assert not batch.is_empty()
    assert BarBatch(provenance=_bar_provenance()).is_empty()


def test_fundamental_batch_requires_available_at() -> None:
    provenance = Provenance(
        source="fund",
        snapshot_id="snap",
        retrieved_at=_NOW,
        as_of=_NOW,
        available_at=None,
        timing=TimingPolicy.SETTLED_EOD,
        vintage_safe=False,
    )
    with pytest.raises(ValidationError):
        FundamentalBatch(provenance=provenance)


def test_macro_batch_requires_available_at() -> None:
    provenance = Provenance(
        source="macro",
        snapshot_id="snap",
        retrieved_at=_NOW,
        as_of=_NOW,
        available_at=None,
        timing=TimingPolicy.LATEST_REVISED,
        vintage_safe=False,
    )
    with pytest.raises(ValidationError):
        MacroBatch(provenance=provenance)


def test_fake_daily_bar_source_covers_known_symbols_only() -> None:
    source = FakeDailyBarSource(
        symbols={"AAPL": Decimal("100"), "MSFT": Decimal("200")},
        as_of=_NOW,
    )
    assert isinstance(source, DailyBarSource)
    batch = source.fetch_daily_bars(("AAPL", "TSLA"), _DAY, _DAY)
    assert batch.covered_keys == {"AAPL"}  # TSLA absent from the fake
    assert batch.provenance.timing is TimingPolicy.SETTLED_EOD
    assert batch.provenance.vintage_safe is True
    assert isinstance(batch, DataBatch)


def test_fake_fundamental_source_is_point_in_time() -> None:
    source = FakeFundamentalSource(
        records={"AAPL": {"pe": Decimal("30")}},
        as_of=_NOW,
        available_at=_NOW,
    )
    assert isinstance(source, FundamentalSource)
    batch = source.fetch_fundamentals(("AAPL",), _DAY)
    assert batch.covered_keys == {"AAPL"}
    assert batch.provenance.timing is TimingPolicy.POINT_IN_TIME
    assert batch.provenance.available_at is not None


def test_fake_macro_source_defaults_to_latest_revised() -> None:
    source = FakeMacroSource(series={"VIXCLS": Decimal("15")}, as_of=_NOW, available_at=_NOW)
    assert isinstance(source, MacroSource)
    batch = source.fetch_macro(("VIXCLS",), _DAY)
    assert batch.provenance.timing is TimingPolicy.LATEST_REVISED
    assert batch.provenance.vintage_safe is False


def test_fake_macro_source_can_simulate_vintage_source() -> None:
    source = FakeMacroSource(
        series={"VIXCLS": Decimal("15")},
        as_of=_NOW,
        available_at=_NOW,
        vintage_safe=True,
    )
    batch = source.fetch_macro(("VIXCLS",), _DAY)
    assert batch.provenance.timing is TimingPolicy.POINT_IN_TIME
    assert batch.provenance.vintage_safe is True


def test_disabled_fake_source_reports_disabled_flag() -> None:
    source = FakeDailyBarSource(symbols={"AAPL": Decimal("100")}, as_of=_NOW, enabled=False)
    assert source.enabled is False
