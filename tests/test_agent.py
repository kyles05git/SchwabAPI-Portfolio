"""Tests for the agent decision loop (strategies + runner). Pure, offline."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from schwab_trader.agent import (
    AgentRunner,
    DipBuyerStrategy,
    HoldStrategy,
    MarketContext,
    available_strategies,
    build_strategy,
)
from schwab_trader.market_data import Quote, QuoteError
from schwab_trader.paper import PaperEngine

NOW = datetime(2026, 7, 14, 15, 0, tzinfo=UTC)


def _quote(symbol: str, *, last: str, prev_close: str, ask: str, bid: str | None = None) -> Quote:
    return Quote(
        symbol=symbol,
        last=Decimal(last),
        previous_close=Decimal(prev_close),
        ask=Decimal(ask),
        bid=Decimal(bid) if bid else Decimal(ask),
        mark=Decimal(ask),
        quote_time=NOW,
    )


def _engine(tmp_path: Path, cash: str = "1000.00") -> PaperEngine:
    return PaperEngine(tmp_path / "paper.sqlite3", starting_cash=Decimal(cash))


# --- registry + hold --------------------------------------------------------


def test_registry_and_build() -> None:
    assert "hold" in available_strategies()
    assert "dip-buyer" in available_strategies()
    assert isinstance(build_strategy("hold", ["AAPL"]), HoldStrategy)


def test_hold_strategy_proposes_nothing() -> None:
    strategy = HoldStrategy(["AAPL"])
    context = MarketContext(now=NOW, cash=Decimal("1000"), positions={}, quotes={})
    assert strategy.decide(context) == []


# --- dip buyer --------------------------------------------------------------


def test_dip_buyer_proposes_buys_for_dips() -> None:
    strategy = DipBuyerStrategy(
        ["AAA", "BBB"], dip_pct=Decimal("0.02"), per_trade_cash=Decimal("100")
    )
    quotes = {
        "AAA": _quote("AAA", last="9.00", prev_close="10.00", ask="9.00"),  # -10% -> dip
        "BBB": _quote("BBB", last="10.10", prev_close="10.00", ask="10.10"),  # up -> no
    }
    context = MarketContext(now=NOW, cash=Decimal("1000"), positions={}, quotes=quotes)
    proposals = strategy.decide(context)
    assert len(proposals) == 1
    assert proposals[0].request.symbol == "AAA"
    assert proposals[0].request.quantity == 11  # 100 // 9.00


def test_dip_buyer_skips_held_and_respects_max_positions() -> None:
    strategy = DipBuyerStrategy(["AAA", "BBB"], dip_pct=Decimal("0.01"), max_positions=1)
    quotes = {
        "AAA": _quote("AAA", last="9.00", prev_close="10.00", ask="9.00"),
        "BBB": _quote("BBB", last="8.00", prev_close="10.00", ask="8.00"),
    }
    # Already holding one position -> no room left.
    context = MarketContext(now=NOW, cash=Decimal("1000"), positions={"ZZZ": 1}, quotes=quotes)
    assert strategy.decide(context) == []


# --- runner -----------------------------------------------------------------


def test_runner_executes_proposals_against_paper(tmp_path: Path) -> None:
    engine = _engine(tmp_path, "1000.00")
    quotes = {
        "AAA": _quote("AAA", last="9.00", prev_close="10.00", ask="9.00", bid="8.95"),
        "BBB": _quote("BBB", last="10.00", prev_close="10.00", ask="10.00"),  # flat -> no buy
    }
    strategy = DipBuyerStrategy(
        ["AAA", "BBB"], dip_pct=Decimal("0.02"), per_trade_cash=Decimal("90")
    )

    report = AgentRunner(strategy, engine, lambda s: quotes[s]).run_cycle(now=NOW)

    assert len(report.outcomes) == 1
    assert report.outcomes[0].status == "FILLED"
    assert engine.positions()[0].symbol == "AAA"
    # 90 // 9 = 10 shares bought at 9.00 -> cash 1000 - 90 = 910
    assert engine.account().cash == Decimal("910.00")


def test_runner_records_missing_quotes(tmp_path: Path) -> None:
    engine = _engine(tmp_path)

    def source(symbol: str) -> Quote:
        raise QuoteError(f"no quote for {symbol}")

    strategy = DipBuyerStrategy(["AAA"], dip_pct=Decimal("0.01"))
    report = AgentRunner(strategy, engine, source).run_cycle(now=NOW)
    assert report.missing_quotes == ["AAA"]
    assert report.outcomes == []


def test_runner_reports_value(tmp_path: Path) -> None:
    engine = _engine(tmp_path, "1000.00")
    quotes = {"AAA": _quote("AAA", last="9.00", prev_close="10.00", ask="9.00")}
    strategy = HoldStrategy(["AAA"])
    report = AgentRunner(strategy, engine, lambda s: quotes[s]).run_cycle(now=NOW)
    assert report.starting_value == Decimal("1000.00")
    assert report.ending_value == Decimal("1000.00")
