"""Tests for the mandatory pre-trade risk checks (Phase 8). Pure, offline."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from schwab_trader.accounts import Balances, Position
from schwab_trader.config import Settings
from schwab_trader.market_data import Quote
from schwab_trader.models import OrderRequest, OrderSide
from schwab_trader.risk import evaluate_order, live_submission_blockers

NOW = datetime(2026, 7, 13, 15, 0, tzinfo=UTC)
ACCOUNT_HASH = "ABCDEF1234567890"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "client_id": "x",
        "client_secret": "y",
        "account_hash": ACCOUNT_HASH,
        "max_order_quantity": 10,
        "max_order_notional": "100.00",
        "quote_max_age_seconds": 60,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def _balances(cash: str = "500.00") -> Balances:
    return Balances(account_type="CASH", cash_available_for_trading=Decimal(cash))


def _positions(symbol: str = "SOFI", settled: str = "30") -> list[Position]:
    return [Position(symbol=symbol, asset_type="EQUITY", settled_long_quantity=Decimal(settled))]


def _quote(symbol: str = "AAPL", *, mark: str = "50.00", age_seconds: int = 5) -> Quote:
    return Quote(symbol=symbol, mark=Decimal(mark), quote_time=NOW - timedelta(seconds=age_seconds))


def _buy(**overrides: object) -> OrderRequest:
    base: dict[str, object] = {
        "side": OrderSide.BUY,
        "symbol": "AAPL",
        "quantity": 1,
        "limit_price": Decimal("50.00"),
    }
    base.update(overrides)
    return OrderRequest(**base)  # type: ignore[arg-type]


def _evaluate(request: OrderRequest, **kwargs: object) -> object:
    settings = kwargs.get("settings") or _settings()
    balances = kwargs.get("balances") or _balances()
    positions = kwargs.get("positions") or _positions()
    quote = kwargs.get("quote") or _quote(request.symbol)
    return evaluate_order(
        request,
        settings,  # type: ignore[arg-type]
        balances=balances,  # type: ignore[arg-type]
        positions=positions,  # type: ignore[arg-type]
        quote=quote,  # type: ignore[arg-type]
        now=NOW,
    )


# --- happy path -------------------------------------------------------------


def test_valid_buy_produces_intent() -> None:
    report = _evaluate(_buy())
    assert report.passed is True  # type: ignore[attr-defined]
    assert report.failures == []  # type: ignore[attr-defined]
    intent = report.intent  # type: ignore[attr-defined]
    assert intent is not None
    assert intent.masked_account == "****7890"
    assert intent.to_api_payload()["orderLegCollection"][0]["instruction"] == "BUY"


def test_valid_sell_within_settled_shares() -> None:
    request = _buy(side=OrderSide.SELL, symbol="SOFI", quantity=30, limit_price=Decimal("17.90"))
    generous = _settings(max_order_quantity=50, max_order_notional="1000.00")
    report = _evaluate(request, settings=generous, quote=_quote("SOFI", mark="17.90"))
    assert report.passed is True  # type: ignore[attr-defined]


# --- limit checks -----------------------------------------------------------


def test_quantity_over_limit_blocks() -> None:
    report = _evaluate(_buy(quantity=5), settings=_settings(max_order_quantity=1))
    assert report.passed is False  # type: ignore[attr-defined]
    assert any(c.name == "quantity_within_limit" for c in report.failures)  # type: ignore[attr-defined]
    assert report.intent is None  # type: ignore[attr-defined]


def test_notional_over_limit_blocks() -> None:
    # 3 x 50 = 150 > 100 max
    report = _evaluate(_buy(quantity=3), settings=_settings(max_order_notional="100.00"))
    assert report.passed is False  # type: ignore[attr-defined]
    assert any(c.name == "notional_within_limit" for c in report.failures)  # type: ignore[attr-defined]


# --- allow-list -------------------------------------------------------------


def test_symbol_not_in_allow_list_blocks() -> None:
    report = _evaluate(_buy(symbol="AAPL"), settings=_settings(allowed_symbols="MSFT,NVDA"))
    assert report.passed is False  # type: ignore[attr-defined]
    assert any(c.name == "symbol_allowed" for c in report.failures)  # type: ignore[attr-defined]


def test_symbol_in_allow_list_passes() -> None:
    report = _evaluate(
        _buy(symbol="NVDA"), settings=_settings(allowed_symbols="MSFT,NVDA"), quote=_quote("NVDA")
    )
    assert report.passed is True  # type: ignore[attr-defined]


# --- funding / holdings -----------------------------------------------------


def test_insufficient_cash_blocks_buy() -> None:
    report = _evaluate(_buy(quantity=1), balances=_balances(cash="10.00"))
    assert report.passed is False  # type: ignore[attr-defined]
    assert any(c.name == "sufficient_cash" for c in report.failures)  # type: ignore[attr-defined]


def test_missing_cash_fails_closed() -> None:
    report = _evaluate(_buy(), balances=Balances(account_type="CASH"))
    assert report.passed is False  # type: ignore[attr-defined]
    assert any(c.name == "sufficient_cash" for c in report.failures)  # type: ignore[attr-defined]


def test_oversized_sell_blocks() -> None:
    request = _buy(side=OrderSide.SELL, symbol="SOFI", quantity=40, limit_price=Decimal("17.90"))
    report = _evaluate(request, quote=_quote("SOFI", mark="17.90"))
    assert report.passed is False  # type: ignore[attr-defined]
    assert any(c.name == "sufficient_settled_shares" for c in report.failures)  # type: ignore[attr-defined]


def test_sell_with_no_position_blocks() -> None:
    request = _buy(side=OrderSide.SELL, symbol="TSLA", quantity=1, limit_price=Decimal("50.00"))
    report = _evaluate(request, positions=[], quote=_quote("TSLA"))
    assert report.passed is False  # type: ignore[attr-defined]
    assert any(c.name == "sufficient_settled_shares" for c in report.failures)  # type: ignore[attr-defined]


# --- quote checks -----------------------------------------------------------


def test_stale_quote_blocks() -> None:
    report = _evaluate(_buy(), quote=_quote("AAPL", age_seconds=3600))
    assert report.passed is False  # type: ignore[attr-defined]
    assert any(c.name == "quote_fresh" for c in report.failures)  # type: ignore[attr-defined]


def test_missing_quote_price_blocks() -> None:
    bare = Quote(symbol="AAPL", quote_time=NOW)
    report = _evaluate(_buy(), quote=bare)
    assert report.passed is False  # type: ignore[attr-defined]
    assert any(c.name == "quote_available" for c in report.failures)  # type: ignore[attr-defined]


def test_quote_symbol_mismatch_blocks() -> None:
    report = _evaluate(_buy(symbol="AAPL"), quote=_quote("MSFT"))
    assert report.passed is False  # type: ignore[attr-defined]
    assert any(c.name == "symbol_matches_quote" for c in report.failures)  # type: ignore[attr-defined]


# --- account selection ------------------------------------------------------


def test_no_account_selected_blocks() -> None:
    report = _evaluate(_buy(), settings=_settings(account_hash=""))
    assert report.passed is False  # type: ignore[attr-defined]
    assert any(c.name == "account_selected" for c in report.failures)  # type: ignore[attr-defined]


# --- live submission gates --------------------------------------------------


def test_live_gates_blocked_by_default() -> None:
    blockers = live_submission_blockers(_settings())
    assert "SCHWAB_TRADING_ENABLED must be true." in blockers
    assert "SCHWAB_DRY_RUN must be false." in blockers


def test_live_gates_open_when_configured() -> None:
    settings = _settings(trading_enabled=True, dry_run=False, require_confirmation=True)
    assert live_submission_blockers(settings) == []


def test_require_confirmation_false_blocks_live() -> None:
    settings = _settings(trading_enabled=True, dry_run=False, require_confirmation=False)
    assert "SCHWAB_REQUIRE_CONFIRMATION must be true." in live_submission_blockers(settings)
