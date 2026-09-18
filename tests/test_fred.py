"""Tests for the FRED macro client (offline; respx-mocked)."""

from __future__ import annotations

import httpx
import pytest
import respx

from schwab_trader import fred
from schwab_trader.fred import FredUnavailable, MacroSnapshot, get_macro

OBS_URL = f"{fred.FRED_BASE}/series/observations"


def _obs(value: str, date: str = "2026-07-15") -> httpx.Response:
    return httpx.Response(200, json={"observations": [{"date": date, "value": value}]})


def test_no_key_raises() -> None:
    with pytest.raises(FredUnavailable):
        get_macro("")


@respx.mock
def test_get_macro_parses_series() -> None:
    # Route by the series_id query param.
    def handler(request: httpx.Request) -> httpx.Response:
        series = request.url.params["series_id"]
        return {
            "VIXCLS": _obs("14.2"),
            "T10Y3M": _obs("-0.35"),
            "BAA10Y": _obs("1.85"),
        }[series]

    respx.get(OBS_URL).mock(side_effect=handler)
    snap = get_macro("KEY")
    assert snap.vix == 14.2
    assert snap.yield_curve_10y_3m == -0.35
    assert snap.credit_spread == 1.85
    assert snap.as_of == "2026-07-15"


@respx.mock
def test_missing_observation_is_none() -> None:
    respx.get(OBS_URL).mock(return_value=_obs("."))  # FRED "." = missing
    snap = get_macro("KEY")
    assert snap.vix is None


def test_summary_formats_available_fields() -> None:
    snap = MacroSnapshot(vix=18.0, yield_curve_10y_3m=-0.2, credit_spread=2.1)
    text = snap.summary()
    assert "VIX 18.0" in text
    assert "10y-3m -0.20%" in text
    assert "credit spread 2.10%" in text


def test_summary_handles_empty() -> None:
    assert MacroSnapshot().summary() == "(no macro data)"
