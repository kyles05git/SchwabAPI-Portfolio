"""Tests for concept normalization and the fundamental factor backtest (offline)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from schwab_trader import fundamental_backtest, fundamentals
from schwab_trader.market_data import Candle
from schwab_trader.pricepanel import PricePanel
from schwab_trader.sec_edgar import Fact
from schwab_trader.sec_store import SecStore


def _fact(ticker, concept, end, val, filed, *, form="10-K", unit="USD", start=None) -> Fact:
    return Fact(
        ticker=ticker,
        cik=1,
        concept=concept,
        unit=unit,
        period_start=date.fromisoformat(start) if start else None,
        period_end=date.fromisoformat(end),
        value=Decimal(str(val)),
        fiscal_year=None,
        fiscal_period="FY",
        form=form,
        filed=date.fromisoformat(filed),
        accession="a",
        frame=None,
    )


def _quarter(ticker, concept, start, end, val, filed) -> Fact:
    return _fact(ticker, concept, end, val, filed, form="10-Q", start=start)


def test_normalization_prefers_first_available_alias(tmp_path) -> None:
    store = SecStore(tmp_path / "sec.sqlite3")
    # No "Revenues"; only the newer contract-revenue tag is present.
    store.upsert(
        [
            _fact(
                "AAA",
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "2022-12-31",
                500,
                "2023-02-01",
            )
        ]
    )
    value = fundamentals.point_in_time_value(store, "AAA", "revenue", date(2023, 6, 30))
    assert value == Decimal("500")


def test_flow_field_uses_annual_10k_not_quarterly(tmp_path) -> None:
    store = SecStore(tmp_path / "sec.sqlite3")
    store.upsert(
        [
            _fact("AAA", "NetIncomeLoss", "2022-12-31", 100, "2023-02-01", form="10-K"),
            _fact("AAA", "NetIncomeLoss", "2023-03-31", 30, "2023-04-30", form="10-Q"),
        ]
    )
    # Even though the 10-Q period is more recent, the flow field takes the annual 10-K.
    value = fundamentals.point_in_time_value(store, "AAA", "net_income", date(2023, 6, 30))
    assert value == Decimal("100")


def test_ttm_sums_four_quarters(tmp_path) -> None:
    store = SecStore(tmp_path / "sec.sqlite3")
    # Four discrete quarters of net income (25 each = 100 TTM), each filed ~1mo after.
    store.upsert(
        [
            _quarter("AAA", "NetIncomeLoss", "2022-07-01", "2022-09-30", 25, "2022-10-25"),
            _quarter("AAA", "NetIncomeLoss", "2022-10-01", "2022-12-31", 25, "2023-01-25"),
            _quarter("AAA", "NetIncomeLoss", "2023-01-01", "2023-03-31", 25, "2023-04-25"),
            _quarter("AAA", "NetIncomeLoss", "2023-04-01", "2023-06-30", 25, "2023-07-25"),
        ]
    )
    ttm = fundamentals.ttm_value(store, "AAA", "net_income", date(2023, 8, 1))
    assert ttm == Decimal("100")


def test_ttm_ignores_future_quarters_and_falls_back_to_annual(tmp_path) -> None:
    store = SecStore(tmp_path / "sec.sqlite3")
    # Only an annual 10-K exists (no discrete quarters) -> TTM falls back to it.
    store.upsert([_fact("BBB", "NetIncomeLoss", "2022-12-31", 400, "2023-02-01", form="10-K")])
    assert fundamentals.ttm_value(store, "BBB", "net_income", date(2023, 6, 30)) == Decimal("400")


def test_ttm_excludes_quarters_not_yet_filed(tmp_path) -> None:
    store = SecStore(tmp_path / "sec.sqlite3")
    store.upsert(
        [
            _quarter("AAA", "NetIncomeLoss", "2022-07-01", "2022-09-30", 25, "2022-10-25"),
            _quarter("AAA", "NetIncomeLoss", "2022-10-01", "2022-12-31", 25, "2023-01-25"),
            _quarter("AAA", "NetIncomeLoss", "2023-01-01", "2023-03-31", 25, "2023-04-25"),
            _quarter("AAA", "NetIncomeLoss", "2023-04-01", "2023-06-30", 25, "2023-07-25"),
        ]
    )
    # As of 2023-05-01, only 3 quarters are public -> can't form a clean TTM -> None
    # (no annual fallback present either).
    assert fundamentals.ttm_value(store, "AAA", "net_income", date(2023, 5, 1)) is None


def test_ratios_uses_ttm_and_price(tmp_path) -> None:
    store = SecStore(tmp_path / "sec.sqlite3")
    store.upsert(
        [
            _quarter("AAA", "NetIncomeLoss", "2022-07-01", "2022-09-30", 25, "2022-10-25"),
            _quarter("AAA", "NetIncomeLoss", "2022-10-01", "2022-12-31", 25, "2023-01-25"),
            _quarter("AAA", "NetIncomeLoss", "2023-01-01", "2023-03-31", 25, "2023-04-25"),
            _quarter("AAA", "NetIncomeLoss", "2023-04-01", "2023-06-30", 25, "2023-07-25"),
            _fact("AAA", "CommonStockSharesOutstanding", "2023-06-30", 100, "2023-07-25"),
            _fact("AAA", "StockholdersEquity", "2023-06-30", 500, "2023-07-25"),
        ]
    )
    r = fundamentals.ratios(store, "AAA", date(2023, 8, 1), Decimal("10"))
    assert r.market_cap == Decimal("1000")  # 10 x 100 shares
    assert r.pe_ttm == Decimal("10")  # 1000 / 100 TTM net income
    assert r.earnings_yield_ttm == Decimal("0.1")
    assert r.roe_ttm == Decimal("0.2")  # 100 / 500 equity


def test_factor_math() -> None:
    assert fundamentals.market_cap(Decimal("10"), Decimal("100")) == Decimal("1000")
    assert fundamentals.market_cap(Decimal("10"), None) is None
    assert fundamentals.earnings_yield(Decimal("50"), Decimal("1000")) == Decimal("0.05")
    assert fundamentals.return_on_equity(Decimal("50"), Decimal("0")) is None


# --- quality components (issue #93) -------------------------------------------


def test_gross_profitability_divides_gross_profit_by_assets() -> None:
    assert fundamentals.gross_profitability(Decimal("300"), Decimal("1000")) == Decimal("0.3")


def test_net_margin_divides_net_income_by_revenue() -> None:
    assert fundamentals.net_margin(Decimal("120"), Decimal("1000")) == Decimal("0.12")


@pytest.mark.parametrize(
    "helper",
    [fundamentals.gross_profitability, fundamentals.net_margin, fundamentals.return_on_equity],
)
def test_quality_helpers_keep_a_negative_numerator_as_a_valid_low_score(helper) -> None:
    """A loss is a real, poor reading - not a missing one. It must rank, not vanish."""
    assert helper(Decimal("-250"), Decimal("1000")) == Decimal("-0.25")


@pytest.mark.parametrize(
    "helper",
    [fundamentals.gross_profitability, fundamentals.net_margin, fundamentals.return_on_equity],
)
@pytest.mark.parametrize("denominator", [Decimal("0"), Decimal("-1000"), None])
def test_quality_helpers_reject_non_positive_or_missing_denominators(helper, denominator) -> None:
    assert helper(Decimal("100"), denominator) is None


@pytest.mark.parametrize(
    "helper",
    [fundamentals.gross_profitability, fundamentals.net_margin, fundamentals.return_on_equity],
)
def test_quality_helpers_return_none_for_a_missing_numerator(helper) -> None:
    assert helper(None, Decimal("1000")) is None


def test_ratios_net_margin_matches_the_extracted_helper(tmp_path) -> None:
    """``Ratios`` and the standalone helper must stay one implementation, not two."""
    store = SecStore(tmp_path / "sec.sqlite3")
    store.upsert(
        [
            _fact("AAA", "NetIncomeLoss", "2022-12-31", 120, "2023-02-01"),
            _fact("AAA", "Revenues", "2022-12-31", 1000, "2023-02-01"),
        ]
    )
    computed = fundamentals.ratios(store, "AAA", date(2023, 6, 30), Decimal("10"))
    assert computed.net_margin_ttm == fundamentals.net_margin(Decimal("120"), Decimal("1000"))
    assert computed.net_margin_ttm == Decimal("0.12")


def test_gross_profit_ttm_reads_the_canonical_concept(tmp_path) -> None:
    """``gross_profit`` is already a mapped flow field, so TTM assembly works on it."""
    store = SecStore(tmp_path / "sec.sqlite3")
    store.upsert(
        [
            _quarter("AAA", "GrossProfit", "2022-07-01", "2022-09-30", 50, "2022-10-25"),
            _quarter("AAA", "GrossProfit", "2022-10-01", "2022-12-31", 50, "2023-01-25"),
            _quarter("AAA", "GrossProfit", "2023-01-01", "2023-03-31", 50, "2023-04-25"),
            _quarter("AAA", "GrossProfit", "2023-04-01", "2023-06-30", 50, "2023-07-25"),
        ]
    )
    assert fundamentals.ttm_value(store, "AAA", "gross_profit", date(2023, 8, 1)) == Decimal("200")


def _candle(symbol, day, close) -> Candle:
    return Candle(
        symbol=symbol,
        date=datetime.fromisoformat(f"{day}T00:00:00+00:00"),
        open=Decimal(str(close)),
        high=Decimal(str(close)),
        low=Decimal(str(close)),
        close=Decimal(str(close)),
        volume=100,
    )


def test_factor_backtest_picks_higher_earnings_yield(tmp_path) -> None:
    panel = PricePanel(tmp_path / "prices.sqlite3")
    # Two names, same shares & price at entry so market caps are equal ($1000 each);
    # CHEAP has 2x the net income -> higher earnings yield -> should be selected.
    for day, cheap, rich in [
        ("2022-01-31", 10, 10),
        ("2022-02-28", 12, 11),  # CHEAP rises more over the month
        ("2022-03-31", 12, 11),
    ]:
        panel.upsert([_candle("CHEAP", day, cheap), _candle("RICH", day, rich)])

    store = SecStore(tmp_path / "sec.sqlite3")
    store.upsert(
        [
            _fact("CHEAP", "NetIncomeLoss", "2021-12-31", 200, "2022-01-15"),
            _fact("CHEAP", "CommonStockSharesOutstanding", "2021-12-31", 100, "2022-01-15"),
            _fact("RICH", "NetIncomeLoss", "2021-12-31", 100, "2022-01-15"),
            _fact("RICH", "CommonStockSharesOutstanding", "2021-12-31", 100, "2022-01-15"),
        ]
    )

    result = fundamental_backtest.run_factor_backtest(
        panel,
        store,
        ["CHEAP", "RICH"],
        factor="earnings-yield",
        start=date(2022, 1, 1),
        end=date(2022, 3, 31),
        top_n=1,
    )
    assert result.last_holdings == ["CHEAP"]  # the higher-yield name
    assert result.total_return_pct > 0  # CHEAP rose 10->12
    # Beat the equal-weight benchmark (which also holds RICH).
    assert result.excess_pct > 0
    assert result.avg_names_ranked == 2.0


def test_live_fundamental_strategy_targets_top_factor(tmp_path) -> None:
    from datetime import UTC

    from schwab_trader.agent import FundamentalStrategy, MarketContext
    from schwab_trader.market_data import Quote
    from schwab_trader.models import OrderSide

    store = SecStore(tmp_path / "sec.sqlite3")
    # CHEAP: equity 1000; RICH: equity 500. Same price & shares -> CHEAP has the
    # higher book-to-market and should be the top pick.
    store.upsert(
        [
            _fact("CHEAP", "StockholdersEquity", "2022-12-31", 1000, "2023-02-01"),
            _fact("CHEAP", "CommonStockSharesOutstanding", "2022-12-31", 100, "2023-02-01"),
            _fact("RICH", "StockholdersEquity", "2022-12-31", 500, "2023-02-01"),
            _fact("RICH", "CommonStockSharesOutstanding", "2022-12-31", 100, "2023-02-01"),
        ]
    )
    now = datetime(2023, 6, 30, 15, 0, tzinfo=UTC)

    def _q(sym: str) -> Quote:
        p = Decimal("10")
        return Quote(symbol=sym, bid=p, ask=p, last=p, mark=p, previous_close=p, quote_time=now)

    strat = FundamentalStrategy(
        ["CHEAP", "RICH"], store=store, factor="book-to-market", max_positions=1
    )
    context = MarketContext(
        now=now,
        cash=Decimal("1000"),
        positions={},
        quotes={"CHEAP": _q("CHEAP"), "RICH": _q("RICH")},
        equity=Decimal("1000"),
    )
    proposals = strat.decide(context)
    buys = [p for p in proposals if p.request.side is OrderSide.BUY]
    assert [p.request.symbol for p in buys] == ["CHEAP"]  # higher book-to-market


def test_factor_backtest_needs_two_months(tmp_path) -> None:
    panel = PricePanel(tmp_path / "prices.sqlite3")
    panel.upsert([_candle("AAA", "2022-01-31", 10)])
    store = SecStore(tmp_path / "sec.sqlite3")
    try:
        fundamental_backtest.run_factor_backtest(
            panel, store, ["AAA"], factor="roe", start=date(2022, 1, 1), end=date(2022, 1, 31)
        )
    except ValueError as exc:
        assert "two month-ends" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")
