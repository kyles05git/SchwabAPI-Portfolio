"""Preset trading universes (named baskets of liquid U.S. symbols).

A momentum or ranking strategy needs a broad universe to choose from - ranking
"the top 8 of 10 tech names" is not real selection. These presets give backtests
and sleeves a wide, diversified, highly-liquid large-cap universe without typing
dozens of tickers.

These lists are curated liquid large caps, not a survivorship-free point-in-time
index. Backtests over them still carry survivorship bias (only currently-listed
names appear) - useful as a directional check, not institutional validation.
"""

from __future__ import annotations

# ~70 diversified, highly-liquid U.S. large caps across sectors. No dotted
# tickers (e.g. BRK.B) to keep market-data fetches simple.
_LARGE_CAP: list[str] = [
    # Technology & semis
    "AAPL",
    "MSFT",
    "NVDA",
    "GOOGL",
    "AMZN",
    "META",
    "AVGO",
    "ORCL",
    "CRM",
    "ADBE",
    "AMD",
    "CSCO",
    "ACN",
    "INTC",
    "IBM",
    "QCOM",
    "TXN",
    "INTU",
    "NOW",
    "AMAT",
    "MU",
    # Communications & media
    "NFLX",
    "DIS",
    "CMCSA",
    "TMUS",
    "VZ",
    "T",
    # Consumer discretionary
    "HD",
    "MCD",
    "NKE",
    "LOW",
    "SBUX",
    "BKNG",
    "TJX",
    # Consumer staples
    "WMT",
    "COST",
    "PG",
    "KO",
    "PEP",
    "PM",
    "MO",
    # Financials
    "JPM",
    "V",
    "MA",
    "BAC",
    "WFC",
    "GS",
    "MS",
    "AXP",
    "SPGI",
    "BLK",
    # Health care
    "UNH",
    "JNJ",
    "LLY",
    "ABBV",
    "MRK",
    "PFE",
    "TMO",
    "ABT",
    "DHR",
    "AMGN",
    # Industrials
    "CAT",
    "HON",
    "GE",
    "BA",
    "UPS",
    "RTX",
    "DE",
    "LMT",
    "UNP",
    # Energy & utilities
    "XOM",
    "CVX",
    "COP",
    "NEE",
]

# The 11 Select Sector SPDR ETFs - the whole U.S. equity market carved into sectors.
# Paired with the momentum strategy (and a small --max-positions, e.g. 4) this is a
# sector relative-strength *rotation*: hold the strongest sectors, rotate as leadership
# changes. Its backtest is unusually trustworthy - the original nine have traded
# continuously since 1998 (XLRE from 2015, XLC from 2018), so unlike single-name
# universes it barely suffers survivorship bias. It also trades a different instrument
# type than the single-name sleeves, diversifying their overlap.
_SECTOR_ETFS: list[str] = [
    "XLK",  # Technology
    "XLF",  # Financials
    "XLE",  # Energy
    "XLV",  # Health Care
    "XLI",  # Industrials
    "XLY",  # Consumer Discretionary
    "XLP",  # Consumer Staples
    "XLU",  # Utilities
    "XLB",  # Materials
    "XLRE",  # Real Estate (2015)
    "XLC",  # Communication Services (2018)
]

# The largest, most-liquid mega caps (a compact subset for quick tests).
_MEGA_CAP: list[str] = [
    "AAPL",
    "MSFT",
    "NVDA",
    "GOOGL",
    "AMZN",
    "META",
    "AVGO",
    "LLY",
    "JPM",
    "V",
    "UNH",
    "XOM",
    "WMT",
    "MA",
    "ORCL",
]

PRESETS: dict[str, list[str]] = {
    "large-cap": _LARGE_CAP,
    "mega-cap": _MEGA_CAP,
    "sector-etfs": _SECTOR_ETFS,
}


def get_preset(name: str) -> list[str] | None:
    """Return a copy of the named preset universe, or None if unknown."""
    preset = PRESETS.get(name.strip().lower())
    return list(preset) if preset else None


def available() -> list[str]:
    """Names of all available presets."""
    return sorted(PRESETS)
