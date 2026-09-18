"""SEC EDGAR client: point-in-time company fundamentals from XBRL company facts.

This is the *free* path to **historical, point-in-time** fundamentals (Schwab only
exposes current values). EDGAR's ``companyfacts`` endpoint returns every reported
XBRL concept for a company with the date each value was ``filed`` - so a backtest
can ask "what was known as of date D" and avoid look-ahead / restatement bias.

This module only fetches and parses; :mod:`schwab_trader.sec_store` persists the
facts and answers point-in-time queries. Network access uses a plain ``httpx``
client (EDGAR needs no auth, but its fair-access policy requires a descriptive
``User-Agent`` and allows <=10 requests/second - callers should stay well under).

Endpoints (per the SEC's documented developer resources):
- ticker -> CIK map: https://www.sec.gov/files/company_tickers.json
- company facts:      https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from pydantic import BaseModel

from schwab_trader import client as _client

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
_GAAP_TAXONOMY = "us-gaap"

# A few tickers whose company_tickers.json entry points to a CIK that has no XBRL
# facts (e.g. a newer/duplicate registrant), stranding the operating company's data
# under a different CIK. These overrides point to the CIK that actually holds the
# facts (each verified against data.sec.gov). Extend as such cases surface.
_CIK_OVERRIDES: dict[str, int] = {
    "XOM": 34088,  # SEC maps XOM to an empty CIK; 34088 = Exxon Mobil Corporation
}


class EdgarError(Exception):
    """Raised when an SEC EDGAR request fails or returns unusable data."""


class Fact(BaseModel):
    """One reported XBRL value, with the metadata needed for point-in-time queries."""

    ticker: str
    cik: int
    concept: str  # e.g. "Revenues", "NetIncomeLoss" (us-gaap taxonomy)
    unit: str  # e.g. "USD", "USD/shares", "shares"
    period_start: date | None
    period_end: date
    value: Decimal
    fiscal_year: int | None
    fiscal_period: str | None  # "FY", "Q1", ...
    form: str | None  # "10-K", "10-Q", ...
    filed: date  # when this value became public (the point-in-time key)
    accession: str
    frame: str | None


def build_client(user_agent: str, *, timeout: float = 30.0) -> httpx.Client:
    """An httpx client carrying the SEC-required ``User-Agent`` (TLS verification on).

    Uses the OS trust store for verification (matches the Schwab client), so it works
    behind a TLS-inspecting proxy without weakening certificate checks.
    """
    _client.enable_os_trust_store()
    return httpx.Client(
        headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
        timeout=timeout,
    )


def _get_json(client: httpx.Client, url: str) -> Any:
    try:
        response = client.get(url)
    except httpx.HTTPError as exc:
        raise EdgarError(f"SEC request failed: {exc}") from exc
    if response.status_code == 404:
        raise EdgarError(f"SEC returned 404 (not found) for {url}.")
    if response.status_code == 403:
        raise EdgarError(
            "SEC returned 403 (forbidden) - set SCHWAB_SEC_USER_AGENT to 'Your Name email'."
        )
    if response.status_code != 200:
        raise EdgarError(f"SEC returned HTTP {response.status_code}.")
    try:
        return response.json()
    except ValueError as exc:
        raise EdgarError("SEC returned a non-JSON response.") from exc


def load_ticker_cik_map(client: httpx.Client) -> dict[str, int]:
    """Fetch the SEC ticker->CIK map (upper-cased ticker keys)."""
    data = _get_json(client, SEC_TICKERS_URL)
    mapping: dict[str, int] = {}
    if isinstance(data, dict):
        for entry in data.values():
            if not isinstance(entry, dict):
                continue
            ticker = entry.get("ticker")
            cik = entry.get("cik_str")
            if ticker is None or cik is None:
                continue
            try:
                mapping[str(ticker).upper()] = int(cik)
            except (TypeError, ValueError):
                continue
    mapping.update(_CIK_OVERRIDES)  # correct known bad ticker->CIK entries
    return mapping


def fetch_company_facts(client: httpx.Client, cik: int) -> Any:
    """Fetch the raw ``companyfacts`` JSON for a CIK."""
    return _get_json(client, COMPANY_FACTS_URL.format(cik=cik))


def _to_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def parse_company_facts(ticker: str, raw: Any) -> list[Fact]:
    """Flatten a ``companyfacts`` document into typed us-gaap :class:`Fact` rows.

    Facts without a usable end date, filed date, or numeric value are skipped.
    """
    if not isinstance(raw, dict):
        return []
    cik_raw = raw.get("cik")
    try:
        cik = int(cik_raw) if cik_raw is not None else 0
    except (TypeError, ValueError):
        cik = 0
    facts = raw.get("facts")
    gaap = facts.get(_GAAP_TAXONOMY) if isinstance(facts, dict) else None
    if not isinstance(gaap, dict):
        return []

    out: list[Fact] = []
    for concept, body in gaap.items():
        units = body.get("units") if isinstance(body, dict) else None
        if not isinstance(units, dict):
            continue
        for unit, items in units.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                period_end = _to_date(item.get("end"))
                filed = _to_date(item.get("filed"))
                value = _to_decimal(item.get("val"))
                if period_end is None or filed is None or value is None:
                    continue
                fy = item.get("fy")
                out.append(
                    Fact(
                        ticker=ticker.upper(),
                        cik=cik,
                        concept=str(concept),
                        unit=str(unit),
                        period_start=_to_date(item.get("start")),
                        period_end=period_end,
                        value=value,
                        fiscal_year=fy if isinstance(fy, int) else None,
                        fiscal_period=item.get("fp"),
                        form=item.get("form"),
                        filed=filed,
                        accession=str(item.get("accn") or ""),
                        frame=item.get("frame"),
                    )
                )
    return out
