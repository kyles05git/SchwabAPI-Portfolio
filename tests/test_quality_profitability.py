"""``quality-profitability-v1`` obeys the frozen challenger-v1 contract (offline).

Every fact here is synthetic and every store is a disposable ``tmp_path`` SQLite
file. Nothing in this module reads ``.env``, contacts SEC EDGAR, Schwab, Neon,
SMTP, or an LLM provider, and no order path is reachable.

The tests are grouped by the contract property they defend: component arithmetic,
point-in-time discipline, normalization and determinism, the coverage floor,
selection and rebalance behavior, portfolio construction, and the promise that
nothing already running was disturbed.
"""

from __future__ import annotations

import ast
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from schwab_trader import fundamentals, universes
from schwab_trader.market_data import Quote
from schwab_trader.models import OrderSide
from schwab_trader.sec_edgar import Fact
from schwab_trader.sec_store import SecStore
from schwab_trader.strategies import contract
from schwab_trader.strategies import quality_profitability as qp

_END = "2022-12-31"
_FILED = "2023-02-01"
_AS_OF = date(2023, 6, 30)
_NOW = datetime(2023, 6, 30, 20, 0, tzinfo=UTC)


# --- synthetic fixtures -------------------------------------------------------


def _fact(ticker: str, concept: str, value, *, end: str = _END, filed: str = _FILED) -> Fact:
    return Fact(
        ticker=ticker,
        cik=1,
        concept=concept,
        unit="USD",
        period_start=None,
        period_end=date.fromisoformat(end),
        value=Decimal(str(value)),
        fiscal_year=None,
        fiscal_period="FY",
        form="10-K",
        filed=date.fromisoformat(filed),
        accession="a",
        frame=None,
    )


_CONCEPTS = {
    "gross_profit": "GrossProfit",
    "net_income": "NetIncomeLoss",
    "revenue": "Revenues",
    "assets": "Assets",
    "equity": "StockholdersEquity",
}


def _facts(ticker: str, *, end: str = _END, filed: str = _FILED, **fields) -> list[Fact]:
    """Annual 10-K facts for a company; omit a keyword to leave that field unfiled."""
    return [
        _fact(ticker, _CONCEPTS[field], value, end=end, filed=filed)
        for field, value in fields.items()
        if value is not None
    ]


def _store(tmp_path: Path, *companies: list[Fact]) -> SecStore:
    store = SecStore(tmp_path / "sec.sqlite3")
    for facts in companies:
        store.upsert(facts)
    return store


def _healthy(ticker: str, scale: int = 1, **overrides) -> list[Fact]:
    """A company with all five required facts present and every denominator positive."""
    fields = {
        "gross_profit": 30 * scale,
        "net_income": 12 * scale,
        "revenue": 100 * scale,
        "assets": 100,
        "equity": 60,
    }
    fields.update(overrides)
    return _facts(ticker, **fields)


def _quote(symbol: str, price: str = "90.00") -> Quote:
    value = Decimal(price)
    return Quote(
        symbol=symbol,
        bid=value,
        ask=value,
        last=value,
        mark=value,
        quote_time=_NOW,
    )


def _quotes(symbols, price: str = "90.00") -> dict[str, Quote]:
    return {symbol: _quote(symbol, price) for symbol in symbols}


def _ticker(index: int) -> str:
    """A synthetic but syntactically valid U.S. equity symbol (``QA``, ``QB``, ...)."""
    return "Q" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[index]


# --- component arithmetic -----------------------------------------------------


def test_all_three_components_are_computed_exactly(tmp_path) -> None:
    store = _store(
        tmp_path,
        _facts("AAA", gross_profit=300, net_income=120, revenue=1000, assets=1000, equity=600),
    )
    components = qp.components_as_of(store, "AAA", _AS_OF)

    assert components.gross_profitability == Decimal("0.3")  # 300 / 1000 assets
    assert components.return_on_equity == Decimal("0.2")  # 120 / 600 equity
    assert components.net_margin == Decimal("0.12")  # 120 / 1000 revenue
    assert components.complete
    assert components.missing == ()


def test_components_reuse_the_shared_fundamentals_helpers(tmp_path) -> None:
    """No second fundamental-data system: the module must delegate the ratio math."""
    store = _store(
        tmp_path,
        _facts("AAA", gross_profit=300, net_income=120, revenue=1000, assets=1000, equity=600),
    )
    components = qp.components_as_of(store, "AAA", _AS_OF)

    assert components.gross_profitability == fundamentals.gross_profitability(
        Decimal("300"), Decimal("1000")
    )
    assert components.return_on_equity == fundamentals.return_on_equity(
        Decimal("120"), Decimal("600")
    )
    assert components.net_margin == fundamentals.net_margin(Decimal("120"), Decimal("1000"))


def test_negative_numerators_stay_valid_low_scores(tmp_path) -> None:
    """A loss-making company ranks last; it is not treated as missing data."""
    store = _store(
        tmp_path,
        _facts("LOSS", gross_profit=-300, net_income=-120, revenue=1000, assets=1000, equity=600),
    )
    components = qp.components_as_of(store, "LOSS", _AS_OF)

    assert components.gross_profitability == Decimal("-0.3")
    assert components.return_on_equity == Decimal("-0.2")
    assert components.net_margin == Decimal("-0.12")
    assert components.complete  # eligible, and it will simply rank at the bottom


def test_a_loss_making_name_is_eligible_and_ranks_last(tmp_path) -> None:
    store = _store(
        tmp_path,
        _healthy("GOOD", scale=3),
        _healthy("OKAY", scale=2),
        _facts("LOSS", gross_profit=-30, net_income=-12, revenue=100, assets=100, equity=60),
    )
    ranking = qp.rank(store, _AS_OF, universe=["GOOD", "OKAY", "LOSS"], coverage_floor=Decimal(0))

    assert "LOSS" in ranking.eligible
    assert ranking.ranked[-1] == "LOSS"


@pytest.mark.parametrize("bad", [0, -1000])
@pytest.mark.parametrize("denominator", ["assets", "equity", "revenue"])
def test_non_positive_denominators_make_a_component_unavailable(
    tmp_path, denominator, bad
) -> None:
    """Assets, equity, and revenue must be strictly positive or the ratio is unknown."""
    store = _store(tmp_path, _healthy("AAA", **{denominator: bad}))
    components = qp.components_as_of(store, "AAA", _AS_OF)

    assert not components.complete
    expected = {
        "assets": "gross-profitability",
        "equity": "return-on-equity",
        "revenue": "net-margin",
    }[denominator]
    assert components.missing == (expected,)


# --- eligibility and reporting ------------------------------------------------


@pytest.mark.parametrize(
    ("omitted", "component"),
    [
        ("gross_profit", "gross-profitability"),
        ("assets", "gross-profitability"),
        ("equity", "return-on-equity"),
        ("revenue", "net-margin"),
    ],
)
def test_missing_one_component_makes_the_name_ineligible(tmp_path, omitted, component) -> None:
    store = _store(tmp_path, _healthy("AAA", **{omitted: None}))
    ranking = qp.rank(store, _AS_OF, universe=["AAA"], coverage_floor=Decimal(0))

    assert ranking.eligible == ()
    assert ranking.ineligible == ("AAA",)
    assert component in ranking.missing_components["AAA"]
    assert "AAA" not in ranking.scores


def test_missing_net_income_removes_two_components_at_once(tmp_path) -> None:
    store = _store(tmp_path, _healthy("AAA", net_income=None))
    ranking = qp.rank(store, _AS_OF, universe=["AAA"], coverage_floor=Decimal(0))

    assert ranking.missing_components["AAA"] == ("return-on-equity", "net-margin")


def test_ineligible_names_are_reported_never_imputed_or_dropped(tmp_path) -> None:
    store = _store(tmp_path, _healthy("AAA"), _healthy("BBB", equity=None))
    ranking = qp.rank(store, _AS_OF, universe=["AAA", "BBB"], coverage_floor=Decimal(0))

    # BBB is visible in the record, absent from the scores, and given no substitute.
    assert ranking.ineligible == ("BBB",)
    assert set(ranking.universe) == {"AAA", "BBB"}
    assert "BBB" not in ranking.scores
    assert all("BBB" not in per_component for per_component in ranking.percentiles.values())


# --- point-in-time discipline -------------------------------------------------


def test_facts_are_invisible_before_they_are_filed(tmp_path) -> None:
    store = _store(tmp_path, _healthy("AAA"))
    assert qp.components_as_of(store, "AAA", date(2023, 1, 31)).complete is False
    assert qp.components_as_of(store, "AAA", date(2023, 2, 1)).complete is True


def test_a_restatement_filed_after_t_is_invisible_to_t(tmp_path) -> None:
    """The later filing wins only once it is public - never retroactively."""
    store = _store(tmp_path, _healthy("AAA"))
    original = qp.components_as_of(store, "AAA", _AS_OF)
    assert original.net_margin == Decimal("0.12")  # 12 / 100

    # The same fiscal period is restated downward, filed well after session T.
    store.upsert(_facts("AAA", net_income=6, end=_END, filed="2023-08-01"))

    # Session T's answer is unchanged, permanently: adding the restatement to the
    # store cannot move a decision that was already made on 2023-06-30 evidence.
    assert qp.components_as_of(store, "AAA", _AS_OF).net_margin == Decimal("0.12")
    # Only a later session sees the restated figure.
    assert qp.components_as_of(store, "AAA", date(2023, 9, 1)).net_margin == Decimal("0.06")


def test_a_restatement_cannot_change_a_past_sessions_selection(tmp_path) -> None:
    """The whole ranking, not just one ratio, is stable against a later filing."""
    store = _store(tmp_path, _healthy("AAA", scale=1), _healthy("BBB", scale=2))
    universe = ["AAA", "BBB"]
    before = qp.rank(store, _AS_OF, universe=universe, coverage_floor=Decimal(0))

    # A restatement that would have reversed the ordering, filed after session T.
    store.upsert(_facts("AAA", gross_profit=9000, net_income=9000, filed="2023-08-01"))

    after = qp.rank(store, _AS_OF, universe=universe, coverage_floor=Decimal(0))
    assert after.ranked == before.ranked
    assert after.scores == before.scores
    # ... while the later session does see it.
    later = qp.rank(store, date(2023, 9, 1), universe=universe, coverage_floor=Decimal(0))
    assert later.ranked[0] == "AAA"


def test_no_future_filing_leaks_into_the_signal_session(tmp_path) -> None:
    """A newer fiscal period filed after T is not evidence available at T."""
    store = _store(tmp_path, _healthy("AAA"))
    store.upsert(_facts("AAA", gross_profit=999, end="2023-06-30", filed="2023-08-15"))

    # 30 / 100 assets, the figure that was public on 2023-06-30 - not 999.
    assert qp.components_as_of(store, "AAA", _AS_OF).gross_profitability == Decimal("0.3")


def test_a_name_whose_only_filing_is_in_the_future_is_ineligible(tmp_path) -> None:
    store = _store(tmp_path, _healthy("AAA"), _healthy("LATE", filed="2023-08-01"))
    ranking = qp.rank(store, _AS_OF, universe=["AAA", "LATE"], coverage_floor=Decimal(0))

    assert ranking.eligible == ("AAA",)
    assert ranking.missing_components["LATE"] == qp.COMPONENTS


# --- normalization, weighting, determinism ------------------------------------


def test_percentile_ranks_span_zero_to_one() -> None:
    ranks = qp.percentile_ranks({"A": Decimal(1), "B": Decimal(2), "C": Decimal(3)})
    assert ranks == {"A": Decimal(0), "B": Decimal("0.5"), "C": Decimal(1)}


def test_percentile_ranks_give_tied_values_one_shared_percentile() -> None:
    """Tied names must not be separated by an accident of insertion order."""
    ranks = qp.percentile_ranks(
        {"A": Decimal(1), "B": Decimal(1), "C": Decimal(3), "D": Decimal(4)}
    )
    # A and B span 0-based positions 0 and 1 -> mean 0.5 -> 0.5/3.
    assert ranks["A"] == ranks["B"] == Decimal("0.5") / 3
    assert ranks["D"] == Decimal(1)


def test_percentile_ranks_are_insertion_order_independent() -> None:
    forward = qp.percentile_ranks({"A": Decimal(1), "B": Decimal(1), "C": Decimal(2)})
    reverse = qp.percentile_ranks({"C": Decimal(2), "B": Decimal(1), "A": Decimal(1)})
    assert forward == reverse


def test_percentile_ranks_handle_degenerate_inputs() -> None:
    assert qp.percentile_ranks({}) == {}
    assert qp.percentile_ranks({"A": Decimal(7)}) == {"A": Decimal(1)}


def test_components_are_equally_weighted_at_one_third_each(tmp_path) -> None:
    """A worked example, so the composite is pinned to arithmetic and not to itself.

    ============ ===== ===== ======  ==============================
    name         gp/a  roe   margin  percentiles (gp, roe, margin)
    ============ ===== ===== ======  ==============================
    AAA          0.30  0.20  0.12    1.0, 0.5, 0.5 -> 2/3
    BBB          0.20  0.30  0.10    0.5, 1.0, 0.0 -> 1/2
    CCC          0.10  0.10  0.20    0.0, 0.0, 1.0 -> 1/3
    ============ ===== ===== ======  ==============================
    """
    store = _store(
        tmp_path,
        _facts("AAA", gross_profit=30, net_income=12, revenue=100, assets=100, equity=60),
        _facts("BBB", gross_profit=20, net_income=30, revenue=300, assets=100, equity=100),
        _facts("CCC", gross_profit=10, net_income=20, revenue=100, assets=100, equity=200),
    )
    universe = ["AAA", "BBB", "CCC"]
    ranking = qp.rank(store, _AS_OF, universe=universe, coverage_floor=Decimal(0))

    assert ranking.components["AAA"].gross_profitability == Decimal("0.3")
    assert ranking.components["BBB"].return_on_equity == Decimal("0.3")
    assert ranking.components["CCC"].net_margin == Decimal("0.2")

    assert ranking.percentiles["gross-profitability"] == {
        "AAA": Decimal(1),
        "BBB": Decimal("0.5"),
        "CCC": Decimal(0),
    }
    assert ranking.scores["AAA"] == (Decimal(1) + Decimal("0.5") + Decimal("0.5")) / 3
    assert ranking.scores["BBB"] == Decimal("0.5")
    assert ranking.scores["CCC"] == (Decimal(0) + Decimal(0) + Decimal(1)) / 3
    assert ranking.ranked == ("AAA", "BBB", "CCC")


def test_one_dominant_component_cannot_outvote_the_other_two(tmp_path) -> None:
    """Equal weighting is on *ranks*, so an extreme raw ratio buys only one rank."""
    store = _store(
        tmp_path,
        # HUGE wins gross profitability by a mile but loses the other two.
        _facts("HUGE", gross_profit=99999, net_income=1, revenue=100, assets=100, equity=100),
        _facts("EVEN", gross_profit=10, net_income=50, revenue=100, assets=100, equity=100),
    )
    ranking = qp.rank(store, _AS_OF, universe=["HUGE", "EVEN"], coverage_floor=Decimal(0))
    assert ranking.ranked[0] == "EVEN"


def test_ties_are_broken_by_frozen_universe_order(tmp_path) -> None:
    store = _store(tmp_path, _healthy("CCC"), _healthy("AAA"), _healthy("BBB"))
    universe = ["BBB", "CCC", "AAA"]  # the frozen order, deliberately not alphabetical
    ranking = qp.rank(store, _AS_OF, universe=universe, coverage_floor=Decimal(0))

    assert len(set(ranking.scores.values())) == 1  # genuinely tied
    assert ranking.ranked == ("BBB", "CCC", "AAA")


def test_ranking_is_independent_of_the_order_facts_were_provided(tmp_path) -> None:
    """Two stores with identical facts inserted in opposite order must agree."""
    universe = ["AAA", "BBB", "CCC"]
    forward = SecStore(tmp_path / "forward.sqlite3")
    for ticker, scale in (("AAA", 1), ("BBB", 2), ("CCC", 3)):
        forward.upsert(_healthy(ticker, scale=scale))

    reverse = SecStore(tmp_path / "reverse.sqlite3")
    for ticker, scale in (("CCC", 3), ("BBB", 2), ("AAA", 1)):
        reverse.upsert(_healthy(ticker, scale=scale))

    a = qp.rank(forward, _AS_OF, universe=universe, coverage_floor=Decimal(0))
    b = qp.rank(reverse, _AS_OF, universe=universe, coverage_floor=Decimal(0))
    assert a.ranked == b.ranked
    assert a.scores == b.scores


def test_ranking_is_reproducible_across_repeated_calls(tmp_path) -> None:
    store = _store(tmp_path, *[_healthy(f"N{i:02d}", scale=i) for i in range(1, 6)])
    universe = [f"N{i:02d}" for i in range(1, 6)]
    first = qp.rank(store, _AS_OF, universe=universe, coverage_floor=Decimal(0))
    second = qp.rank(store, _AS_OF, universe=universe, coverage_floor=Decimal(0))
    assert first.ranked == second.ranked
    assert first.scores == second.scores


# --- coverage floor -----------------------------------------------------------


def _mixed_universe(tmp_path, eligible_count: int, total: int = 10) -> tuple[SecStore, list[str]]:
    """``total`` names of which exactly ``eligible_count`` have all three components."""
    universe = [_ticker(i) for i in range(total)]
    store = SecStore(tmp_path / "sec.sqlite3")
    for index, ticker in enumerate(universe):
        if index < eligible_count:
            store.upsert(_healthy(ticker, scale=index + 1))
        else:
            store.upsert(_healthy(ticker, equity=None))  # one component short
    return store, universe


def test_exactly_the_coverage_floor_passes(tmp_path) -> None:
    """60% is the floor, not the exclusive lower bound: 6 of 10 must trade."""
    store, universe = _mixed_universe(tmp_path, eligible_count=6)
    ranking = qp.rank(store, _AS_OF, universe=universe)

    assert ranking.coverage == Decimal("0.6")
    assert ranking.coverage_floor == Decimal("0.60")
    assert ranking.meets_coverage
    assert ranking.failure_reason is None
    assert len(ranking.selected) == 6


def test_below_the_coverage_floor_fails_the_whole_sleeve_closed(tmp_path) -> None:
    store, universe = _mixed_universe(tmp_path, eligible_count=5)
    ranking = qp.rank(store, _AS_OF, universe=universe)

    assert ranking.coverage == Decimal("0.5")
    assert not ranking.meets_coverage
    assert ranking.selected == ()
    assert ranking.failure_reason is not None
    assert "fails closed" in ranking.failure_reason
    # The evidence is still recorded - failing closed is not the same as going blind.
    assert len(ranking.eligible) == 5
    assert len(ranking.ineligible) == 5


def test_failing_closed_proposes_no_orders_at_all(tmp_path) -> None:
    """Not even a liquidation: a failed session does not touch existing positions."""
    store, universe = _mixed_universe(tmp_path, eligible_count=5)
    strategy = qp.QualityProfitabilityStrategy(store, universe=universe)
    plan = strategy.plan(
        as_of=_AS_OF,
        positions={_ticker(0): 10, _ticker(1): 10},
        quotes=_quotes(universe),
        equity=Decimal("10000"),
        cash=Decimal("1000"),
    )

    assert plan.orders == ()
    assert plan.selection_changed is False
    assert "fails closed" in plan.reason


def test_an_empty_universe_fails_closed_rather_than_dividing_by_zero(tmp_path) -> None:
    store = _store(tmp_path)
    ranking = qp.rank(store, _AS_OF, universe=[])
    assert ranking.selected == ()
    assert ranking.failure_reason == "the universe is empty"


# --- selection and rebalance cadence ------------------------------------------


def _twelve(tmp_path) -> tuple[SecStore, list[str]]:
    """Twelve eligible names with strictly increasing quality."""
    universe = [_ticker(i) for i in range(12)]
    store = SecStore(tmp_path / "sec.sqlite3")
    for index, ticker in enumerate(universe, start=1):
        store.upsert(_healthy(ticker, scale=index))
    return store, universe


def test_the_top_ten_are_selected(tmp_path) -> None:
    store, universe = _twelve(tmp_path)
    ranking = qp.rank(store, _AS_OF, universe=universe)

    assert ranking.coverage == Decimal(1)
    assert len(ranking.selected) == 10
    assert ranking.selected == tuple(_ticker(i) for i in range(11, 1, -1))
    # The two weakest names are left out, not silently squeezed in.
    assert set(ranking.ranked[-2:]) == {_ticker(0), _ticker(1)}


def test_max_positions_is_the_contract_value(tmp_path) -> None:
    store, universe = _twelve(tmp_path)
    strategy = qp.QualityProfitabilityStrategy(store, universe=universe)
    assert strategy.max_positions == 10
    assert strategy.max_position_fraction == Decimal("0.10")
    assert len(strategy.rank(_AS_OF).selected) == 10


def test_an_unchanged_selection_is_a_no_op(tmp_path) -> None:
    """Monthly cadence still trades only when the selected set actually changes."""
    store, universe = _twelve(tmp_path)
    strategy = qp.QualityProfitabilityStrategy(store, universe=universe)
    selected = strategy.rank(_AS_OF).selected

    plan = strategy.plan(
        as_of=_AS_OF,
        positions=dict.fromkeys(selected, 5),
        quotes=_quotes(universe),
        equity=Decimal("10000"),
        cash=Decimal("1000"),
    )
    assert plan.orders == ()
    assert plan.selection_changed is False
    assert "selection unchanged" in plan.reason


def test_a_changed_selection_rotates_the_book(tmp_path) -> None:
    store, universe = _twelve(tmp_path)
    strategy = qp.QualityProfitabilityStrategy(store, universe=universe)
    selected = strategy.rank(_AS_OF).selected

    # Hold the top nine plus one name that has since dropped out of the selection.
    positions = dict.fromkeys(selected[:9], 5)
    positions[_ticker(0)] = 5
    plan = strategy.plan(
        as_of=_AS_OF,
        positions=positions,
        quotes=_quotes(universe),
        equity=Decimal("10000"),
        cash=Decimal("10000"),
    )

    assert plan.selection_changed is True
    sells = [o for o in plan.orders if o.request.side is OrderSide.SELL]
    buys = [o for o in plan.orders if o.request.side is OrderSide.BUY]
    assert [o.request.symbol for o in sells] == [_ticker(0)]
    assert [o.request.symbol for o in buys] == [selected[9]]
    assert plan.orders[0].request.side is OrderSide.SELL  # sells precede buys


def test_order_rationales_expose_the_component_values(tmp_path) -> None:
    store, universe = _twelve(tmp_path)
    strategy = qp.QualityProfitabilityStrategy(store, universe=universe)
    plan = strategy.plan(
        as_of=_AS_OF,
        positions={},
        quotes=_quotes(universe),
        equity=Decimal("10000"),
        cash=Decimal("10000"),
    )
    rationale = plan.orders[0].rationale
    assert "top 10 by quality composite" in rationale
    for component in qp.COMPONENTS:
        assert component in rationale
    assert "composite" in rationale


# --- price plays no part in the ranking ---------------------------------------


def test_prices_cannot_change_the_ranking(tmp_path) -> None:
    """The selection is identical under wildly different, even inverted, quotes."""
    store, universe = _twelve(tmp_path)
    strategy = qp.QualityProfitabilityStrategy(store, universe=universe)

    cheap = {symbol: _quote(symbol, "1.00") for symbol in universe}
    dear = {symbol: _quote(symbol, "5000.00") for symbol in universe}
    # Price the best-ranked name absurdly and the worst cheaply; ranking must not care.
    skewed = dict(cheap)
    skewed[_ticker(11)] = _quote(_ticker(11), "9999.00")

    baseline = strategy.rank(_AS_OF)
    for quotes in (cheap, dear, skewed):
        plan = strategy.plan(
            as_of=_AS_OF,
            positions={},
            quotes=quotes,
            equity=Decimal("100000"),
            cash=Decimal("100000"),
        )
        assert plan.ranking.ranked == baseline.ranked
        assert plan.ranking.selected == baseline.selected
        assert plan.ranking.scores == baseline.scores


def test_a_missing_quote_leaves_the_ranking_intact_and_is_reported(tmp_path) -> None:
    """No price at all still cannot distort the ranking; it only blocks the sizing."""
    store, universe = _twelve(tmp_path)
    strategy = qp.QualityProfitabilityStrategy(store, universe=universe)
    top = _ticker(11)
    quotes = _quotes(universe)
    del quotes[top]  # the top-ranked name cannot be priced

    plan = strategy.plan(
        as_of=_AS_OF,
        positions={},
        quotes=quotes,
        equity=Decimal("10000"),
        cash=Decimal("10000"),
    )
    assert top in plan.ranking.selected  # still selected on fundamentals
    assert top in plan.unpriced  # but reported as unsized rather than guessed at
    assert top not in [o.request.symbol for o in plan.orders]


def test_the_ranking_api_takes_no_price_input(tmp_path) -> None:
    """A structural guarantee: there is no parameter through which a price could enter."""
    store, universe = _twelve(tmp_path)
    ranking = qp.rank(store, _AS_OF, universe=universe)
    assert ranking.selected  # computed with no quotes in scope at all


# --- portfolio construction ---------------------------------------------------


def test_construction_is_long_only_whole_share_and_capped_at_ten_percent(tmp_path) -> None:
    store, universe = _twelve(tmp_path)
    strategy = qp.QualityProfitabilityStrategy(store, universe=universe)
    plan = strategy.plan(
        as_of=_AS_OF,
        positions={},
        quotes=_quotes(universe, "90.00"),
        equity=Decimal("10000"),
        cash=Decimal("10000"),
    )

    assert len(plan.orders) == 10
    for order in plan.orders:
        assert order.request.side is OrderSide.BUY  # nothing held -> no shorts
        assert order.request.quantity == int(order.request.quantity)
        assert order.request.quantity >= 1
        # 10% of $10,000 is $1,000; 11 shares at $90 = $990, and 12 would breach it.
        assert order.request.quantity == 11
        assert order.request.limit_price is not None
        notional = order.request.quantity * order.request.limit_price
        assert notional <= Decimal("10000") * Decimal("0.10")

    gross = sum(o.request.quantity * (o.request.limit_price or Decimal(0)) for o in plan.orders)
    assert gross <= Decimal("10000")  # gross exposure capped at 100%, no leverage


def test_a_sell_never_exceeds_the_held_quantity(tmp_path) -> None:
    store, universe = _twelve(tmp_path)
    strategy = qp.QualityProfitabilityStrategy(store, universe=universe)
    plan = strategy.plan(
        as_of=_AS_OF,
        positions={_ticker(0): 7},
        quotes=_quotes(universe),
        equity=Decimal("10000"),
        cash=Decimal("10000"),
    )
    sells = [o for o in plan.orders if o.request.side is OrderSide.SELL]
    assert [(o.request.symbol, o.request.quantity) for o in sells] == [(_ticker(0), 7)]


def test_fractional_remainders_stay_in_cash(tmp_path) -> None:
    """A $1,000 budget at $300 buys three shares, not 3.33."""
    store, universe = _twelve(tmp_path)
    strategy = qp.QualityProfitabilityStrategy(store, universe=universe)
    plan = strategy.plan(
        as_of=_AS_OF,
        positions={},
        quotes=_quotes(universe, "300.00"),
        equity=Decimal("10000"),
        cash=Decimal("10000"),
    )
    assert all(o.request.quantity == 3 for o in plan.orders)


# --- identity: the frozen contract --------------------------------------------


def test_the_universe_is_the_frozen_seventy_four_name_large_cap_list() -> None:
    assert len(qp.UNIVERSE) == 74
    assert qp.UNIVERSE == contract.QUALITY_PROFITABILITY.universe
    assert qp.UNIVERSE == tuple(universes.get_preset("large-cap") or ())


def test_the_module_reads_the_frozen_universe_not_the_mutable_preset() -> None:
    """Importing ``universes`` at runtime would let a preset edit redefine the sleeve."""
    source = Path(qp.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "schwab_trader.universes" not in imported
    assert "schwab_trader.agent" not in imported  # #94 owns agent.py


def test_the_strategy_defaults_come_from_the_contract(tmp_path) -> None:
    strategy = qp.QualityProfitabilityStrategy(_store(tmp_path))
    spec = contract.QUALITY_PROFITABILITY

    assert strategy.name == spec.name == "quality-profitability-v1"
    assert strategy.version == spec.version == "1"
    assert strategy.universe == spec.universe
    assert strategy.max_positions == spec.max_positions == 10
    assert strategy.max_position_fraction == spec.max_position_fraction == Decimal("0.10")
    assert strategy.coverage_floor == spec.coverage_floor == Decimal("0.60")
    assert qp.GROSS_EXPOSURE_CAP == Decimal("1.00")


def test_components_match_the_contracts_declared_components() -> None:
    assert list(qp.COMPONENTS) == contract.QUALITY_PROFITABILITY.parameters["components"]
    assert qp.COMPONENT_COUNT == 3
    assert contract.QUALITY_PROFITABILITY.parameters["component_weights"] == ["1/3", "1/3", "1/3"]


def test_the_contract_hash_is_untouched() -> None:
    """This issue implements the contract; it must not edit it."""
    assert (
        contract.contract_hash()
        == "47965e0f12dede2e74ba7100276eeafc09d9a7ae8577788d8906f5594f9bd981"
    )


# --- nothing already running was disturbed ------------------------------------


def test_july_cohort_definition_hashes_are_unchanged() -> None:
    """The running July 27/28 cohort's frozen identities must not move."""
    from test_strategy_seam import JULY_COHORT_HASHES, _load_bootstrap_script

    bootstrap = _load_bootstrap_script()
    actual = {
        spec.name: spec.definition.configuration_hash
        for spec in (bootstrap._make_spec(t) for t in bootstrap._TEMPLATES)  # type: ignore[attr-defined]
    }
    assert actual == JULY_COHORT_HASHES


def test_the_existing_factor_surface_is_unwidened() -> None:
    """The new helpers are functions, not new ``FACTORS`` entries or CLI choices."""
    assert fundamentals.FACTORS == ("earnings-yield", "book-to-market", "roe")


def test_existing_fundamental_ratios_still_compute(tmp_path) -> None:
    """``ratios()`` is unchanged after ``net_margin_ttm`` was repointed at the helper."""
    store = _store(
        tmp_path,
        _facts("AAA", net_income=100, revenue=1000, equity=500),
    )
    store.upsert([_fact("AAA", "CommonStockSharesOutstanding", 100)])
    computed = fundamentals.ratios(store, "AAA", _AS_OF, Decimal("10"))

    assert computed.market_cap == Decimal("1000")
    assert computed.pe_ttm == Decimal("10")
    assert computed.earnings_yield_ttm == Decimal("0.1")
    assert computed.roe_ttm == Decimal("0.2")
    assert computed.net_margin_ttm == Decimal("0.1")


def test_the_existing_fundamental_strategy_still_ranks_by_roe(tmp_path) -> None:
    """The legacy EDGAR-backed sleeve is untouched by the new quality module."""
    store = _store(
        tmp_path,
        _facts("HIGH", net_income=200, equity=500),
        _facts("LOW", net_income=50, equity=500),
    )
    assert fundamentals.factor_score(
        store, "roe", "HIGH", _AS_OF, Decimal("10")
    ) == Decimal("0.4")
    assert fundamentals.factor_score(
        store, "roe", "LOW", _AS_OF, Decimal("10")
    ) == Decimal("0.1")
