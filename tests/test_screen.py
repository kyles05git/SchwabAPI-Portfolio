"""Tests for fundamental universe screening (offline)."""

from __future__ import annotations

from schwab_trader.market_data import Fundamentals
from schwab_trader.screen import ScreenCriteria, apply_screen, passes


def _f(symbol: str, **kw: float) -> Fundamentals:
    return Fundamentals(symbol=symbol, **kw)  # type: ignore[arg-type]


def test_max_pe_filters_expensive() -> None:
    cheap = _f("CHEAP", pe_ratio=10.0)
    dear = _f("DEAR", pe_ratio=50.0)
    crit = ScreenCriteria(max_pe=25.0)
    assert passes(cheap, crit)
    assert not passes(dear, crit)


def test_min_roe_and_market_cap() -> None:
    good = _f("GOOD", return_on_equity=30.0, market_cap=5e9)
    weak_roe = _f("WEAK", return_on_equity=5.0, market_cap=5e9)
    small = _f("SMALL", return_on_equity=30.0, market_cap=1e8)
    crit = ScreenCriteria(min_roe=15.0, min_market_cap=2e9)
    assert passes(good, crit)
    assert not passes(weak_roe, crit)
    assert not passes(small, crit)


def test_profitable_min_pe_excludes_losers() -> None:
    profitable = _f("PRO", pe_ratio=20.0)
    loss = _f("LOSS", pe_ratio=-8.0)  # negative P/E = losing money
    crit = ScreenCriteria(min_pe=0.0)
    assert passes(profitable, crit)
    assert not passes(loss, crit)


def test_missing_data_fails_closed() -> None:
    # A criterion is set but the value is absent -> excluded.
    no_pe = _f("NOPE")  # pe_ratio is None
    assert not passes(no_pe, ScreenCriteria(max_pe=25.0))
    # No criteria set -> everything passes.
    assert passes(no_pe, ScreenCriteria())


def test_debt_and_growth_filters() -> None:
    strong = _f("STRONG", total_debt_to_equity=40.0, eps_change_pct_ttm=20.0)
    levered = _f("LEV", total_debt_to_equity=200.0, eps_change_pct_ttm=20.0)
    shrinking = _f("SHRINK", total_debt_to_equity=40.0, eps_change_pct_ttm=-10.0)
    crit = ScreenCriteria(max_debt_to_equity=100.0, min_eps_growth=0.0)
    assert passes(strong, crit)
    assert not passes(levered, crit)
    assert not passes(shrinking, crit)


def test_apply_screen_returns_subset() -> None:
    rows = [
        _f("A", pe_ratio=10.0, return_on_equity=20.0),
        _f("B", pe_ratio=40.0, return_on_equity=20.0),  # too expensive
        _f("C", pe_ratio=15.0, return_on_equity=5.0),  # weak ROE
    ]
    result = apply_screen(rows, ScreenCriteria(max_pe=25.0, min_roe=15.0))
    assert [f.symbol for f in result] == ["A"]
