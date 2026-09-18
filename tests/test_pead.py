"""Tests for the post-earnings-drift (PEAD) time-series surprise signal (offline)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from schwab_trader.pead import (
    pead_signal,
    quarterly_earnings,
    standardized_unexpected_earnings,
)
from schwab_trader.sec_edgar import Fact
from schwab_trader.sec_store import SecStore

# Ten consecutive quarter-ends (2.5 years), oldest first.
_QUARTER_ENDS = [
    date(2023, 3, 31),
    date(2023, 6, 30),
    date(2023, 9, 30),
    date(2023, 12, 31),
    date(2024, 3, 31),
    date(2024, 6, 30),
    date(2024, 9, 30),
    date(2024, 12, 31),
    date(2025, 3, 31),
    date(2025, 6, 30),
]


def _fact(ticker: str, period_end: date, value: int, *, filed: date | None = None) -> Fact:
    filed = filed or (period_end + timedelta(days=40))
    return Fact(
        ticker=ticker,
        cik=1,
        concept="NetIncomeLoss",
        unit="USD",
        period_start=period_end - timedelta(days=89),  # ~discrete quarter
        period_end=period_end,
        value=Decimal(value),
        fiscal_year=period_end.year,
        fiscal_period="Q",
        form="10-Q",
        filed=filed,
        accession=f"{period_end.isoformat()}-{filed.isoformat()}",
        frame=None,
    )


def _store(tmp_path: Path, ticker: str, values: list[int]) -> SecStore:
    store = SecStore(tmp_path / "sec.sqlite3")
    store.upsert([_fact(ticker, end, val) for end, val in zip(_QUARTER_ENDS, values, strict=True)])
    return store


def test_quarterly_series_is_point_in_time(tmp_path: Path) -> None:
    store = _store(tmp_path, "AAA", [100, 110, 120, 130, 200, 220, 240, 260, 400, 440])
    # As of mid-2025, the last quarter (filed ~2025-08-09) is not yet public.
    quarters = quarterly_earnings(store, "AAA", date(2025, 7, 1))
    assert quarters[-1].period_end == date(2025, 3, 31)  # 2025-06-30 filing is future
    assert [q.value for q in quarters][-1] == Decimal(400)


def test_accelerating_earnings_gives_positive_sue(tmp_path: Path) -> None:
    # Year-over-year jumps grow (100.. -> 200.. -> 400..): latest surprise is large.
    store = _store(tmp_path, "UP", [100, 110, 120, 130, 200, 220, 240, 260, 400, 440])
    sig = pead_signal(store, "UP", date(2025, 12, 31))
    assert sig is not None
    assert sig.sue > 0
    assert sig.latest_period_end == date(2025, 6, 30)


def test_year_over_year_decline_gives_negative_sue(tmp_path: Path) -> None:
    # The latest two quarters fall BELOW the prior-year same quarters (200<220, 180<250),
    # so the seasonal difference is negative -> negative SUE.
    store = _store(tmp_path, "DN", [100, 120, 140, 160, 220, 250, 280, 310, 200, 180])
    sue = standardized_unexpected_earnings(quarterly_earnings(store, "DN", date(2025, 12, 31)))
    assert sue is not None
    assert sue < 0  # latest YoY diff = 180 - 250 = -70


def test_flat_earnings_zero_volatility_returns_none(tmp_path: Path) -> None:
    # Identical seasonal differences -> zero surprise volatility -> not computable.
    store = _store(tmp_path, "FLAT", [100, 100, 100, 100, 150, 150, 150, 150, 200, 200])
    assert (
        standardized_unexpected_earnings(quarterly_earnings(store, "FLAT", date(2026, 1, 1)))
        is None
    )


def test_insufficient_history_returns_none(tmp_path: Path) -> None:
    store = SecStore(tmp_path / "sec.sqlite3")
    store.upsert([_fact("SHORT", end, 100 + i) for i, end in enumerate(_QUARTER_ENDS[:4])])
    assert pead_signal(store, "SHORT", date(2025, 1, 1)) is None


def test_days_since_filed_and_dedup_latest_filing(tmp_path: Path) -> None:
    store = _store(tmp_path, "AGE", [100, 110, 120, 130, 200, 220, 240, 260, 400, 440])
    # A restatement of the latest known quarter, filed later, must win (point-in-time).
    store.upsert([_fact("AGE", date(2025, 3, 31), 999, filed=date(2025, 6, 1))])
    as_of = date(2025, 7, 1)
    quarters = quarterly_earnings(store, "AGE", as_of)
    latest = quarters[-1]
    assert latest.period_end == date(2025, 3, 31)
    assert latest.value == Decimal(999)  # the later restatement filing wins
    assert latest.filed == date(2025, 6, 1)

    sig = pead_signal(store, "AGE", as_of)
    assert sig is not None
    assert sig.days_since_filed == (as_of - date(2025, 6, 1)).days
