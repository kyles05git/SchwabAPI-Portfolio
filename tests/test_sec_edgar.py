"""Tests for SEC EDGAR parsing and the point-in-time fundamentals store (offline)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import respx

from schwab_trader import sec_edgar
from schwab_trader.sec_store import SecStore

# A trimmed companyfacts document: one concept reported twice, plus a restatement.
_FACTS_DOC = {
    "cik": 320193,
    "entityName": "Example Inc.",
    "facts": {
        "us-gaap": {
            "Revenues": {
                "units": {
                    "USD": [
                        {
                            "start": "2022-01-01",
                            "end": "2022-12-31",
                            "val": 1000,
                            "accn": "0001",
                            "fy": 2022,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2023-02-01",
                        },
                        {
                            "start": "2023-01-01",
                            "end": "2023-12-31",
                            "val": 1200,
                            "accn": "0002",
                            "fy": 2023,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2024-02-01",
                        },
                        {  # restatement of FY2022, filed later
                            "start": "2022-01-01",
                            "end": "2022-12-31",
                            "val": 950,
                            "accn": "0002",
                            "fy": 2023,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2024-02-01",
                        },
                    ]
                }
            },
            "NoUsableDates": {"units": {"USD": [{"val": 5, "end": None, "filed": None}]}},
        }
    },
}


def test_parse_company_facts_flattens_and_skips_bad_rows() -> None:
    facts = sec_edgar.parse_company_facts("AAPL", _FACTS_DOC)
    assert len(facts) == 3  # the NoUsableDates row is skipped
    assert all(f.ticker == "AAPL" and f.concept == "Revenues" for f in facts)
    assert {f.value for f in facts} == {Decimal("1000"), Decimal("1200"), Decimal("950")}


@respx.mock
def test_load_ticker_cik_map() -> None:
    body = {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft"},
    }
    respx.get(sec_edgar.SEC_TICKERS_URL).mock(return_value=httpx.Response(200, json=body))
    with sec_edgar.build_client("tester test@example.com") as client:
        mapping = sec_edgar.load_ticker_cik_map(client)
    assert mapping["AAPL"] == 320193
    assert mapping["MSFT"] == 789019
    assert mapping["XOM"] == 34088  # applied from the verified CIK-override map


def _store(tmp_path) -> SecStore:
    store = SecStore(tmp_path / "sec.sqlite3")
    store.upsert(sec_edgar.parse_company_facts("AAPL", _FACTS_DOC))
    return store


def test_point_in_time_ignores_future_filings(tmp_path) -> None:
    store = _store(tmp_path)
    # As of mid-2023: only FY2022 (filed 2023-02-01) is public; FY2023 isn't yet.
    fact = store.point_in_time("AAPL", "Revenues", date(2023, 6, 30))
    assert fact is not None
    assert fact.period_end == date(2022, 12, 31)
    assert fact.value == Decimal("1000")  # original, pre-restatement


def test_point_in_time_uses_latest_period_and_restatement(tmp_path) -> None:
    store = _store(tmp_path)
    # As of mid-2024: FY2023 is the most recent period known -> 1200.
    latest = store.point_in_time("AAPL", "Revenues", date(2024, 6, 30))
    assert latest is not None
    assert latest.period_end == date(2023, 12, 31)
    assert latest.value == Decimal("1200")

    # Restricting to FY2022's period is out of scope of point_in_time (it returns the
    # most recent period), but the restatement is present in the raw facts:
    fy2022 = [f for f in store.facts("AAPL", "Revenues") if f.period_end == date(2022, 12, 31)]
    assert {f.value for f in fy2022} == {Decimal("1000"), Decimal("950")}


def test_upsert_is_idempotent(tmp_path) -> None:
    store = _store(tmp_path)
    before = store.total_facts()
    store.upsert(sec_edgar.parse_company_facts("AAPL", _FACTS_DOC))  # again
    assert store.total_facts() == before  # no duplicates


def test_concepts_discovery(tmp_path) -> None:
    store = _store(tmp_path)
    concepts = dict(store.concepts("AAPL"))
    assert concepts == {"Revenues": 3}
