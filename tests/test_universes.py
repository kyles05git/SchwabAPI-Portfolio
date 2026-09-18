"""Tests for preset universes (offline)."""

from __future__ import annotations

import re

from schwab_trader import universes

_SYMBOL_RE = re.compile(r"^[A-Z][A-Z.]{0,9}$")


def test_available_lists_presets() -> None:
    presets = universes.available()
    assert "large-cap" in presets
    assert "mega-cap" in presets


def test_get_preset_returns_copy() -> None:
    a = universes.get_preset("large-cap")
    b = universes.get_preset("large-cap")
    assert a is not None and b is not None
    assert a == b
    a.append("ZZZZ")
    assert "ZZZZ" not in (universes.get_preset("large-cap") or [])  # not mutated


def test_get_preset_is_case_insensitive() -> None:
    assert universes.get_preset("LARGE-CAP") == universes.get_preset("large-cap")


def test_unknown_preset_returns_none() -> None:
    assert universes.get_preset("nope") is None
    assert universes.get_preset("") is None


def test_presets_contain_valid_unique_tickers() -> None:
    for name in universes.available():
        symbols = universes.get_preset(name) or []
        assert symbols, f"{name} is empty"
        assert len(symbols) == len(set(symbols)), f"{name} has duplicates"
        for sym in symbols:
            assert _SYMBOL_RE.match(sym), f"{sym} in {name} is not a valid ticker shape"


def test_large_cap_is_broad() -> None:
    # A momentum universe should be wide enough for meaningful ranking.
    assert len(universes.get_preset("large-cap") or []) >= 50


def test_sector_etfs_preset() -> None:
    # The 11 Select Sector SPDRs, for momentum-based sector rotation.
    sectors = universes.get_preset("sector-etfs")
    assert sectors is not None
    assert len(sectors) == 11
    assert {"XLK", "XLF", "XLE", "XLV", "XLC", "XLRE"} <= set(sectors)
