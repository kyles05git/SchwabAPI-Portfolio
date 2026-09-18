"""Macro data from FRED (Federal Reserve Economic Data).

Free official macro series that complement price-derived signals: the VIX
(volatility/fear gauge), the 10y-3m yield-curve spread (recession signal when
negative), and the BAA-Treasury credit spread (credit stress). Used to give the
AI research step a real macro read - not wired into the strategy regime filter,
which stays purely price-derived so live and backtest agree.

Needs a free FRED API key (https://fredaccount.stlouisfed.org/apikeys) in
SCHWAB_FRED_API_KEY. Absent a key, callers skip macro gracefully. FRED is a
separate host from Schwab; this module uses its own httpx client (the OS trust
store is already injected globally).
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel

FRED_BASE = "https://api.stlouisfed.org/fred"

# Series IDs for the macro reads we care about.
_SERIES = {
    "vix": "VIXCLS",
    "yield_curve_10y_3m": "T10Y3M",
    "credit_spread": "BAA10Y",
}


class FredUnavailable(Exception):
    """Raised when no FRED API key is configured."""


class MacroSnapshot(BaseModel):
    """Latest values for the tracked macro series (any may be None if unavailable)."""

    vix: float | None = None
    yield_curve_10y_3m: float | None = None
    credit_spread: float | None = None
    as_of: str | None = None

    def summary(self) -> str:
        """A compact one-line macro read (for prompts and display)."""
        parts: list[str] = []
        if self.vix is not None:
            parts.append(f"VIX {self.vix:.1f}")
        if self.yield_curve_10y_3m is not None:
            parts.append(f"10y-3m {self.yield_curve_10y_3m:+.2f}%")
        if self.credit_spread is not None:
            parts.append(f"BAA credit spread {self.credit_spread:.2f}%")
        return ", ".join(parts) if parts else "(no macro data)"


def _latest(client: httpx.Client, series_id: str, api_key: str) -> tuple[float | None, str | None]:
    params: dict[str, str | int] = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 1,
    }
    response = client.get(f"{FRED_BASE}/series/observations", params=params)
    response.raise_for_status()
    data: Any = response.json()
    observations = data.get("observations") if isinstance(data, dict) else None
    if not observations:
        return None, None
    obs = observations[0]
    value, date = obs.get("value"), obs.get("date")
    if value in (None, ".", ""):  # FRED uses "." for missing observations
        return None, date
    try:
        return float(value), date
    except (TypeError, ValueError):
        return None, date


def get_macro(api_key: str, *, client: httpx.Client | None = None) -> MacroSnapshot:
    """Fetch the latest macro snapshot from FRED.

    Raises:
        FredUnavailable: if no API key is configured.
    """
    if not api_key:
        raise FredUnavailable(
            "No FRED API key configured. Set SCHWAB_FRED_API_KEY in .env "
            "(free at https://fredaccount.stlouisfed.org/apikeys)."
        )
    owns = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        values: dict[str, float | None] = {}
        as_of: str | None = None
        for field, series_id in _SERIES.items():
            value, date = _latest(client, series_id, api_key)
            values[field] = value
            as_of = as_of or date
        return MacroSnapshot(
            vix=values["vix"],
            yield_curve_10y_3m=values["yield_curve_10y_3m"],
            credit_spread=values["credit_spread"],
            as_of=as_of,
        )
    finally:
        if owns:
            client.close()
