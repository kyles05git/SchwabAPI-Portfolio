"""The shared strategy seam is pure, boundary-respecting, and behavior-preserving.

Issue #91 extracted the portfolio-construction helpers out of ``agent.py`` so that
#92, #93, and #94 can each own one module without editing a shared file. Two
things must hold for that to be safe:

* the extraction changed nothing about the existing strategies, and
* the new package cannot grow into the central files reserved for #95.

Both are asserted here with synthetic inputs only. Nothing in this module touches
a network, a credential, a real database, or an order path.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from schwab_trader import agent, strategy_registry
from schwab_trader.agent import MarketContext, MeanReversionStrategy, MomentumStrategy
from schwab_trader.market_data import Candle, Quote
from schwab_trader.models import OrderSide
from schwab_trader.strategies import sizing

_PACKAGE = Path(agent.__file__).parent / "strategies"
_REPO_ROOT = Path(agent.__file__).parents[2]

# Definition digests of the July 28 cohort (``paper-first-2026-07-28``), captured
# from ``origin/main`` at bc50bbd before the helper extraction. That cohort is
# running evidence: its definitions must reconstruct to the same hash forever.
JULY_COHORT_HASHES = {
    "control-cash": "150d0d01fb2b5de052896de6683436a7ea81dbd7bf0a354cc7809ca68150b11e",
    "bench-spy": "1852156d8194ee4b707f791167cfda04bf09ee66b2eac239ceb9650e6e9ae243",
    "sector-momentum": "1570b08dd56bd96d2d9de8e9ec8987731b270594eea20343c1c5366785643a95",
    "trend-large": "021bd967366ee64ff6fab1bfe71b707fbb716a2014d4142bb7231ab1cd5f1738",
    "low-vol-large": "c5d8bc25e7fa26d1ff243de1eea9daf54c89b4f14afdcab6265f86d988d791a2",
    "momentum-large": "9d7c5ae22d1b90a6aeb8d667e6104c8f7e897191ef7cc8549697dbe4279aae00",
    "value-momentum-edgar": "2876fbe4e926764b71972a1ebb7a8ec1e83277daf9e1b1b67b49b37e70543bdc",
}


def _load_bootstrap_script() -> object:
    path = _REPO_ROOT / "scripts" / "bootstrap_paper_cohort.py"
    spec = importlib.util.spec_from_file_location("bootstrap_script_for_seam_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --- the existing experiment is unchanged ------------------------------------


def test_july_cohort_definition_hashes_are_unchanged() -> None:
    """The running cohort's frozen identities survive the helper extraction."""
    bootstrap = _load_bootstrap_script()
    actual = {
        spec.name: spec.definition.configuration_hash
        for spec in (bootstrap._make_spec(t) for t in bootstrap._TEMPLATES)  # type: ignore[attr-defined]
    }
    assert actual == JULY_COHORT_HASHES


def _frozen_universe(name: str) -> list[str] | None:
    """The frozen universe of a challenger-v1 sleeve, or ``None`` if it has none."""
    from schwab_trader import challenger_strategies
    from schwab_trader.strategies import contract

    if name not in challenger_strategies.ADAPTERS:
        return None
    sleeve = contract.sleeve(name)
    return [*sleeve.universe, *sleeve.defensive_universe]


def test_every_registered_strategy_still_reconstructs() -> None:
    """A stored definition must still rebuild the same implementation class.

    The challenger-v1 sleeves registered by #95 refuse any universe but their frozen
    one — refusing is the point of a frozen experiment, so each is supplied its own
    universe rather than having that guard relaxed. ``_frozen_universe`` returns
    ``None`` for every ordinary strategy, which keeps taking the arbitrary pair.
    """
    default = ["AAPL", "MSFT"]
    for name in strategy_registry.paper_strategy_names():
        entry = strategy_registry.entry(name)
        if entry.is_llm or strategy_registry.CAP_SEC_EDGAR in entry.capabilities:
            continue  # need an EDGAR store / LLM provider, out of scope here
        universe = _frozen_universe(name) or default
        resources = strategy_registry.StrategyResources(
            history={symbol: _flat_candles(symbol) for symbol in universe},
            benchmark_history=_flat_candles("SPY"),
            store=None,
            llm_builder=None,
        )
        definition = strategy_registry.make_definition(
            name, universe_definition={"symbols": universe}
        )
        rebuilt = strategy_registry.reconstruct(definition, universe, resources=resources)
        assert type(rebuilt) is entry.implementation
        # ``tactical`` deliberately routes only the first asset, so the rebuilt
        # universe is a prefix of the supplied one rather than equal to it.
        assert rebuilt.universe == universe[: len(rebuilt.universe)]
        assert rebuilt.universe, f"{name} rebuilt with an empty universe"


# --- the extraction preserved behavior ---------------------------------------


def test_agent_private_helpers_are_the_extracted_functions() -> None:
    """The historical private names are the shared functions, not copies of them."""
    assert agent._buy_limit is sizing.buy_limit
    assert agent._sell_limit is sizing.sell_limit
    assert agent._asof is sizing.as_of_history


def test_agent_rebalance_matches_the_shared_planner() -> None:
    context = _context(cash=Decimal("10000"), positions={"MSFT": 4})
    proposals = agent._rebalance(
        context,
        ["AAPL"],
        Decimal("1.0"),
        Decimal("0.5"),
        exit_reason="out",
        enter_reason="in",
    )
    planned = sizing.plan_rebalance(
        targets=["AAPL"],
        positions=context.positions,
        quotes=context.quotes,
        equity=context.equity,
        cash=context.cash,
        spendable=context.spendable,
        leverage=context.leverage,
        gross_cap=Decimal("1.0"),
        max_position_fraction=Decimal("0.5"),
        exit_reason="out",
        enter_reason="in",
    )
    assert [(p.request, p.rationale) for p in proposals] == [
        (p.request, p.rationale) for p in planned
    ]


def test_mean_reversion_proposals_are_byte_stable() -> None:
    """A golden case so #94 can prove its version-1 rewrite against known output.

    AAPL sits 10% below its 20-day average while still above its 200-day average
    (a qualifying oversold-in-uptrend dip); MSFT is flat and does not qualify.
    """
    history = {"AAPL": _dipped_candles("AAPL"), "MSFT": _flat_candles("MSFT")}
    strategy = MeanReversionStrategy(
        ["AAPL", "MSFT"],
        history=history,
        benchmark_history=[],
        max_positions=5,
        max_position_fraction=Decimal("0.20"),
    )
    context = _context(cash=Decimal("10000"), positions={})
    proposals = strategy.decide(context)

    assert len(proposals) == 1
    only = proposals[0]
    assert only.request.side is OrderSide.BUY
    assert only.request.symbol == "AAPL"
    # $10,000 equity, 1/5 cap -> $2,000 budget at a $90.00 marketable limit.
    assert only.request.limit_price == Decimal("90.00")
    assert only.request.quantity == 22
    assert "oversold" in only.rationale


def test_sizing_never_proposes_a_fractional_or_short_position() -> None:
    context = _context(cash=Decimal("1000"), positions={})
    planned = sizing.plan_rebalance(
        targets=["AAPL", "MSFT"],
        positions=context.positions,
        quotes=context.quotes,
        equity=context.equity,
        cash=context.cash,
        spendable=context.spendable,
        leverage=Decimal("1"),
        gross_cap=Decimal("1.0"),
        max_position_fraction=Decimal("0.5"),
        exit_reason="out",
        enter_reason="in",
    )
    for order in planned:
        assert order.request.quantity >= 1
        assert order.request.quantity == int(order.request.quantity)
        assert order.request.side is OrderSide.BUY  # nothing held, so no shorts


def test_momentum_is_unaffected_by_the_extraction() -> None:
    """Golden output for a regime-capped momentum sleeve, through the extracted path.

    A flat series still ranks (its momentum features are zero, not missing), and a
    flat benchmark scores 0/4, so the regime cap is 15%. With $10,000 equity split
    over two targets that is $750 per name, or 8 shares at a $90.00 limit.
    """
    history = {s: _flat_candles(s) for s in ("AAPL", "MSFT")}
    strategy = MomentumStrategy(
        ["AAPL", "MSFT"], history=history, benchmark_history=_flat_candles("SPY")
    )
    proposals = strategy.decide(_context(cash=Decimal("10000"), positions={}))

    assert [(p.request.symbol, p.request.side, p.request.quantity) for p in proposals] == [
        ("AAPL", OrderSide.BUY, 8),
        ("MSFT", OrderSide.BUY, 8),
    ]
    assert all(p.request.limit_price == Decimal("90.00") for p in proposals)
    assert all("regime cap 15%" in p.rationale for p in proposals)


# --- deterministic ranking ----------------------------------------------------


def test_rank_desc_breaks_ties_by_frozen_universe_order() -> None:
    order = ("SPY", "EFA", "EEM", "VNQ")
    tied = {"VNQ": 1.0, "EFA": 1.0, "SPY": 1.0, "EEM": 1.0}
    assert sizing.rank_desc(tied, order) == ["SPY", "EFA", "EEM", "VNQ"]


def test_rank_desc_orders_by_score_before_tie_break() -> None:
    order = ("SPY", "EFA", "EEM")
    assert sizing.rank_desc({"SPY": 0.1, "EFA": 0.9, "EEM": 0.5}, order) == ["EFA", "EEM", "SPY"]


def test_rank_desc_is_insertion_order_independent() -> None:
    order = ("SPY", "EFA", "EEM")
    forward = sizing.rank_desc({"SPY": 1.0, "EFA": 1.0, "EEM": 1.0}, order)
    reverse = sizing.rank_desc({"EEM": 1.0, "EFA": 1.0, "SPY": 1.0}, order)
    assert forward == reverse


def test_eligible_by_history_reports_rather_than_drops() -> None:
    history = {"AAPL": _flat_candles("AAPL", count=10), "MSFT": _flat_candles("MSFT", count=2)}
    eligible, ineligible = sizing.eligible_by_history(history, ["AAPL", "MSFT", "NVDA"], 5)
    assert eligible == ["AAPL"]
    # A short history and an absent symbol are both surfaced, never swallowed.
    assert ineligible == ["MSFT", "NVDA"]


def test_as_of_history_excludes_future_candles() -> None:
    now = datetime(2026, 7, 30, tzinfo=UTC)
    candles = _flat_candles("AAPL", count=5)
    future = [*candles, _candle("AAPL", now + timedelta(days=5), Decimal("999"))]
    sliced = sizing.as_of_history({"AAPL": future}, now)
    assert all(c.date <= now for c in sliced["AAPL"])
    assert Decimal("999") not in [c.close for c in sliced["AAPL"]]


# --- module boundaries --------------------------------------------------------


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def _package_files() -> list[Path]:
    files = sorted(_PACKAGE.glob("*.py"))
    assert files, "the strategies package should contain modules"
    return files


# Central files reserved for the integration issue (#95). A strategy module that
# imported one of these would couple the three parallel agents back together.
RESERVED = {
    "schwab_trader.strategy_registry",
    "schwab_trader.cli",
    "schwab_trader.dashboard",
    "schwab_trader.cohort_lifecycle",
    "schwab_trader.cohort_ops",
    "schwab_trader.cohort_readiness",
    "schwab_trader.scheduling",
    "schwab_trader.sleeves",
    "schwab_trader.sleeve_runs",
}


@pytest.mark.parametrize("path", _package_files(), ids=lambda p: p.name)
def test_package_does_not_import_reserved_integration_modules(path: Path) -> None:
    assert not (_imported_modules(path) & RESERVED), f"{path.name} reaches into #95's files"


def test_sizing_does_not_import_agent() -> None:
    """The dependency must stay one-directional, or the extraction cycles."""
    assert "schwab_trader.agent" not in _imported_modules(_PACKAGE / "sizing.py")


@pytest.mark.parametrize("path", _package_files(), ids=lambda p: p.name)
def test_package_performs_no_dynamic_code_loading(path: Path) -> None:
    """No plugin discovery: a challenger's definition must be readable, not resolved."""
    forbidden = {"importlib", "pkgutil", "runpy"}
    assert not (_imported_modules(path) & forbidden)

    tree = ast.parse(path.read_text(encoding="utf-8"))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not (called & {"eval", "exec", "compile", "__import__"})


@pytest.mark.parametrize("path", _package_files(), ids=lambda p: p.name)
def test_package_performs_no_io(path: Path) -> None:
    """Data is injected, never fetched: these helpers must stay pure and offline."""
    forbidden = {"httpx", "requests", "sqlite3", "socket", "smtplib", "urllib", "os"}
    assert not (_imported_modules(path) & forbidden)


# --- synthetic fixtures -------------------------------------------------------

_START = datetime(2025, 1, 1, tzinfo=UTC)


def _candle(symbol: str, when: datetime, close: Decimal) -> Candle:
    return Candle(
        symbol=symbol,
        date=when,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1_000_000,
    )


def _flat_candles(symbol: str, count: int = 260) -> list[Candle]:
    """A perfectly flat series: no momentum, no dip, no trend."""
    return [_candle(symbol, _START + timedelta(days=i), Decimal("100")) for i in range(count)]


def _dipped_candles(symbol: str, count: int = 260) -> list[Candle]:
    """A steady uptrend that pulls back sharply on the final session.

    Closes ramp 50 -> ~127 so the 200-day average sits near 98, then the last close
    drops to 110. That is ~11% below the 20-day average (past the 5% entry
    threshold) while still comfortably above the 200-day average - the
    oversold-inside-an-uptrend shape the strategy is meant to detect.
    """
    candles = [
        _candle(symbol, _START + timedelta(days=i), Decimal(50) + Decimal("0.3") * i)
        for i in range(count - 1)
    ]
    candles.append(_candle(symbol, _START + timedelta(days=count - 1), Decimal("110")))
    return candles


def _context(*, cash: Decimal, positions: dict[str, int]) -> MarketContext:
    quotes = {
        symbol: Quote(
            symbol=symbol,
            bid=Decimal("89.99"),
            ask=Decimal("90.00"),
            last=Decimal("90.00"),
            mark=Decimal("90.00"),
            quote_time=_START + timedelta(days=400),
        )
        for symbol in ("AAPL", "MSFT")
    }
    equity = cash + sum(
        (Decimal(qty) * Decimal("90.00") for qty in positions.values()), Decimal(0)
    )
    return MarketContext(
        now=_START + timedelta(days=400),
        cash=cash,
        positions=positions,
        quotes=quotes,
        equity=equity,
    )
