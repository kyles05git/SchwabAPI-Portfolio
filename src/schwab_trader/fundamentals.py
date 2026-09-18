"""Concept normalization + point-in-time canonical fundamentals.

SEC us-gaap concept names vary across companies and over time (revenue alone is
reported as ``Revenues``, ``SalesRevenueNet``, or ``RevenueFromContractWith...``).
This module maps a small set of **canonical fields** to ordered candidate concepts
and pulls the first one available, so callers ask for ``"revenue"`` rather than
guessing tags. Values are looked up *point-in-time* via
:meth:`~schwab_trader.sec_store.SecStore.point_in_time`, so a backtest never sees
a figure before it was filed.

Flow fields (revenue, net income, ...) are taken from the annual **10-K** for a
consistent yearly number; instant balance-sheet fields (equity, assets, shares)
take the latest available filing. Trailing-twelve-month assembly from quarterly
filings is a later refinement.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel

from schwab_trader.sec_edgar import Fact
from schwab_trader.sec_store import SecStore

# Canonical field -> ordered candidate us-gaap concepts (most common/specific first).
FIELD_CONCEPTS: dict[str, list[str]] = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
    ],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "eps_diluted": ["EarningsPerShareDiluted"],
    "assets": ["Assets"],
    "liabilities": ["Liabilities"],
    "equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
    "shares_outstanding": [
        "CommonStockSharesOutstanding",
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingBasic",
    ],
}

# Period ("flow") metrics use the annual 10-K; the rest are instant balance items.
_FLOW_FIELDS = frozenset(
    {"revenue", "net_income", "gross_profit", "operating_income", "eps_diluted"}
)

CANONICAL_FIELDS = tuple(FIELD_CONCEPTS)


def point_in_time_value(store: SecStore, ticker: str, field: str, as_of: date) -> Decimal | None:
    """Value of a canonical ``field`` for ``ticker`` known as of ``as_of``, or None.

    Tries each candidate concept in priority order and returns the first hit. Flow
    fields are restricted to the annual 10-K; instant fields take the latest filing.
    """
    concepts = FIELD_CONCEPTS.get(field)
    if not concepts:
        return None
    form = "10-K" if field in _FLOW_FIELDS else None
    for concept in concepts:
        fact = store.point_in_time(ticker, concept, as_of, form=form)
        if fact is not None:
            return fact.value
    return None


def _assemble_ttm(facts: list[Fact]) -> Decimal | None:
    """Sum the four most recent discrete quarters into a trailing-twelve-month value.

    Keeps only ~90-day (discrete-quarter) periods, takes the latest filing for each
    quarter end, and requires four of them spanning ~one year (guards against a
    missing quarter). Returns None if a clean TTM can't be built.
    """
    quarters: dict[date, tuple[date, Decimal, date]] = {}  # end -> (filed, value, start)
    for fact in facts:
        if fact.period_start is None:
            continue
        duration = (fact.period_end - fact.period_start).days
        if not (80 <= duration <= 100):  # discrete quarter only (not YTD/annual)
            continue
        prior = quarters.get(fact.period_end)
        if prior is None or fact.filed > prior[0]:  # latest filing wins (point-in-time)
            quarters[fact.period_end] = (fact.filed, fact.value, fact.period_start)
    if len(quarters) < 4:
        return None
    recent = sorted(quarters.items(), reverse=True)[:4]  # 4 most recent quarter ends
    newest_end = recent[0][0]
    oldest_start = min(entry[1][2] for entry in recent)
    if not (350 <= (newest_end - oldest_start).days <= 380):  # must cover ~1 year
        return None
    return sum((entry[1][1] for entry in recent), Decimal(0))


def ttm_value(store: SecStore, ticker: str, field: str, as_of: date) -> Decimal | None:
    """Trailing-twelve-month value of a flow ``field``, or the value for instant fields.

    For flow fields (revenue, net income, ...) this sums the last four quarterly
    filings known as of ``as_of``; if a company reports only annually (no discrete
    quarters), it falls back to the annual 10-K figure. Instant balance-sheet fields
    just defer to :func:`point_in_time_value`.
    """
    if field not in _FLOW_FIELDS:
        return point_in_time_value(store, ticker, field, as_of)
    for concept in FIELD_CONCEPTS.get(field, []):
        ttm = _assemble_ttm(store.facts_as_of(ticker, concept, as_of))
        if ttm is not None:
            return ttm
    return point_in_time_value(store, ticker, field, as_of)  # annual fallback


class FundamentalSnapshot(BaseModel):
    """Canonical fundamentals for one company as known on a date (missing = None)."""

    ticker: str
    as_of: date
    net_income: Decimal | None = None
    revenue: Decimal | None = None
    equity: Decimal | None = None
    assets: Decimal | None = None
    cash: Decimal | None = None
    shares_outstanding: Decimal | None = None


def snapshot(store: SecStore, ticker: str, as_of: date) -> FundamentalSnapshot:
    """Assemble the canonical fundamentals known for ``ticker`` as of ``as_of``."""
    return FundamentalSnapshot(
        ticker=ticker.upper(),
        as_of=as_of,
        net_income=point_in_time_value(store, ticker, "net_income", as_of),
        revenue=point_in_time_value(store, ticker, "revenue", as_of),
        equity=point_in_time_value(store, ticker, "equity", as_of),
        assets=point_in_time_value(store, ticker, "assets", as_of),
        cash=point_in_time_value(store, ticker, "cash", as_of),
        shares_outstanding=point_in_time_value(store, ticker, "shares_outstanding", as_of),
    )


# --- Factor math (pure; None when an input is missing or a denominator <= 0) --


def market_cap(price: Decimal, shares: Decimal | None) -> Decimal | None:
    if shares is None or shares <= 0:
        return None
    return price * shares


def earnings_yield(net_income: Decimal | None, mkt_cap: Decimal | None) -> Decimal | None:
    """Net income / market cap (higher = cheaper). A value signal."""
    if net_income is None or mkt_cap is None or mkt_cap <= 0:
        return None
    return net_income / mkt_cap


def book_to_market(equity: Decimal | None, mkt_cap: Decimal | None) -> Decimal | None:
    """Book equity / market cap (higher = cheaper). A value signal."""
    if equity is None or mkt_cap is None or mkt_cap <= 0:
        return None
    return equity / mkt_cap


def return_on_equity(net_income: Decimal | None, equity: Decimal | None) -> Decimal | None:
    """Net income / book equity (higher = higher quality)."""
    if net_income is None or equity is None or equity <= 0:
        return None
    return net_income / equity


def gross_profitability(gross_profit: Decimal | None, assets: Decimal | None) -> Decimal | None:
    """Gross profit / total assets (higher = higher quality).

    Novy-Marx's profitability measure: gross profit is the cleanest accounting line
    for economic productivity, and scaling by assets makes it comparable across
    companies of different sizes. A *negative* gross profit is a real (bad) reading,
    not a missing one, so only the denominator is screened.
    """
    if gross_profit is None or assets is None or assets <= 0:
        return None
    return gross_profit / assets


def net_margin(net_income: Decimal | None, revenue: Decimal | None) -> Decimal | None:
    """Net income / revenue (higher = higher quality).

    The same ratio :class:`Ratios` reports as ``net_margin_ttm``, exposed as a
    reusable function so a cross-sectional ranker does not have to re-derive it.
    As with :func:`return_on_equity`, a loss is a valid low score; only a
    non-positive denominator makes the ratio unavailable.
    """
    if net_income is None or revenue is None or revenue <= 0:
        return None
    return net_income / revenue


# Cross-sectional factors: name -> "higher is better" score. Shared by the
# fundamental backtest and the live `fundamental` sleeve strategy.
FACTORS = ("earnings-yield", "book-to-market", "roe")


def factor_score(
    store: SecStore,
    factor: str,
    symbol: str,
    as_of: date,
    price: Decimal,
    *,
    use_ttm: bool = True,
) -> Decimal | None:
    """Cross-sectional factor value for ``symbol`` as of ``as_of`` (None if data missing).

    Earnings and equity are point-in-time (TTM for earnings when ``use_ttm``);
    market cap uses the supplied ``price`` (a backtest close or a live quote).
    """
    net_income = (
        ttm_value(store, symbol, "net_income", as_of)
        if use_ttm
        else point_in_time_value(store, symbol, "net_income", as_of)
    )
    if factor == "roe":
        return return_on_equity(net_income, point_in_time_value(store, symbol, "equity", as_of))
    shares = point_in_time_value(store, symbol, "shares_outstanding", as_of)
    mkt_cap = market_cap(price, shares)
    if factor == "earnings-yield":
        return earnings_yield(net_income, mkt_cap)
    if factor == "book-to-market":
        return book_to_market(point_in_time_value(store, symbol, "equity", as_of), mkt_cap)
    msg = f"Unknown factor '{factor}'. Choose: {', '.join(FACTORS)}."
    raise ValueError(msg)


def _ratio(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


class Ratios(BaseModel):
    """Derived valuation/quality ratios for a company as of a date (TTM where noted)."""

    ticker: str
    as_of: date
    price: Decimal
    market_cap: Decimal | None = None
    pe_ttm: Decimal | None = None
    earnings_yield_ttm: Decimal | None = None
    price_to_book: Decimal | None = None
    book_to_market: Decimal | None = None
    roe_ttm: Decimal | None = None
    net_margin_ttm: Decimal | None = None


def ratios(store: SecStore, ticker: str, as_of: date, price: Decimal) -> Ratios:
    """Compute derived ratios from point-in-time fundamentals and a price.

    Flow inputs (earnings, revenue) are trailing-twelve-month; equity and shares are
    the latest instant. All fields are None when an input is missing.
    """
    net_income = ttm_value(store, ticker, "net_income", as_of)
    revenue = ttm_value(store, ticker, "revenue", as_of)
    equity = point_in_time_value(store, ticker, "equity", as_of)
    shares = point_in_time_value(store, ticker, "shares_outstanding", as_of)
    mkt_cap = market_cap(price, shares)
    return Ratios(
        ticker=ticker.upper(),
        as_of=as_of,
        price=price,
        market_cap=mkt_cap,
        pe_ttm=_ratio(mkt_cap, net_income),
        earnings_yield_ttm=earnings_yield(net_income, mkt_cap),
        price_to_book=_ratio(mkt_cap, equity),
        book_to_market=book_to_market(equity, mkt_cap),
        roe_ttm=return_on_equity(net_income, equity),
        net_margin_ttm=net_margin(net_income, revenue),
    )
