"""Mandatory pre-trade risk checks.

The risk engine turns an :class:`~schwab_trader.models.OrderRequest` into a
:class:`~schwab_trader.models.ValidatedOrderIntent` only if *every* applicable
check passes. It fails closed: missing, malformed, or ambiguous inputs block the
order rather than allow it.

The evaluation is pure - it takes already-fetched account balances, positions,
and a quote, and performs no network calls. Callers fetch that data immediately
before evaluating so the checks reflect current state.

Two concerns are separated:

- :func:`evaluate_order` runs the trade-validity checks (limits, allow-list,
  settled cash/shares, fresh quote, supported params). Used by preview and submit.
- :func:`live_submission_blockers` reports the configuration gates that must be
  open for a *live* submission (checked additionally at submit time, alongside
  the interactive confirmation). Never bypassable by a single CLI flag.

Duplicate-order protection is added in Phase 10 and layered on at submit time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel

from schwab_trader.accounts import Balances, Position
from schwab_trader.config import Settings
from schwab_trader.market_data import Quote
from schwab_trader.models import (
    AssetType,
    OrderDuration,
    OrderRequest,
    OrderSession,
    OrderSide,
    OrderType,
    ValidatedOrderIntent,
)


class RiskCheck(BaseModel):
    """The outcome of a single named risk check."""

    name: str
    passed: bool
    detail: str


class RiskReport(BaseModel):
    """The aggregated result of evaluating an order against all risk checks."""

    checks: list[RiskCheck]
    intent: ValidatedOrderIntent | None = None

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)

    @property
    def failures(self) -> list[RiskCheck]:
        return [check for check in self.checks if not check.passed]


def _reference_price(quote: Quote) -> Decimal | None:
    """Pick a usable reference price: mark, then last, then ask, then bid."""
    for candidate in (quote.mark, quote.last, quote.ask, quote.bid):
        if candidate is not None and candidate > 0:
            return candidate
    return None


def _settled_long_quantity(positions: list[Position], symbol: str) -> Decimal:
    for position in positions:
        if position.symbol == symbol:
            return position.settled_long_quantity
    return Decimal(0)


def evaluate_order(
    request: OrderRequest,
    settings: Settings,
    *,
    balances: Balances,
    positions: list[Position],
    quote: Quote,
    now: datetime | None = None,
) -> RiskReport:
    """Run all trade-validity checks and, if all pass, produce a validated intent."""
    now = now or datetime.now(UTC)
    checks: list[RiskCheck] = []

    def add(name: str, passed: bool, detail: str) -> None:
        checks.append(RiskCheck(name=name, passed=passed, detail=detail))

    # Account selection and readability.
    add(
        "account_selected",
        settings.has_account_selected,
        "An account hash must be explicitly selected.",
    )
    add(
        "account_readable",
        balances.account_type not in ("", "UNKNOWN"),
        f"Account read returned type '{balances.account_type}'.",
    )

    # Symbol validity and allow-list.
    add(
        "symbol_matches_quote",
        quote.symbol == request.symbol,
        f"Quote symbol '{quote.symbol}' must match order symbol '{request.symbol}'.",
    )
    allowed = settings.allowed_symbol_set
    add(
        "symbol_allowed",
        (not allowed) or request.symbol in allowed,
        "Symbol allow-list is empty (all allowed)."
        if not allowed
        else f"'{request.symbol}' must be in the configured allow-list.",
    )

    # Quantity and notional limits.
    add("quantity_positive", request.quantity > 0, "Quantity must be a positive whole number.")
    add(
        "quantity_within_limit",
        request.quantity <= settings.max_order_quantity,
        f"Quantity {request.quantity} must be <= max {settings.max_order_quantity}.",
    )
    estimated = request.estimated_notional
    add(
        "notional_within_limit",
        estimated <= settings.max_order_notional,
        f"Estimated notional {estimated} must be <= max {settings.max_order_notional}.",
    )

    # Supported, documented order parameters only.
    supported = (
        request.order_type is OrderType.LIMIT
        and request.duration is OrderDuration.DAY
        and request.session is OrderSession.NORMAL
        and request.asset_type is AssetType.EQUITY
    )
    add("supported_order_params", supported, "Only equity LIMIT/DAY/NORMAL orders are supported.")

    # Quote availability and freshness.
    reference = _reference_price(quote)
    add("quote_available", reference is not None, "A usable quote price is required.")
    fresh = not quote.is_stale(settings.quote_max_age, now=now)
    add(
        "quote_fresh",
        fresh,
        f"Quote must be newer than {settings.quote_max_age_seconds}s "
        f"(age {int(quote.age(now=now).total_seconds())}s).",
    )

    # Side-specific funding / holdings checks.
    if request.side is OrderSide.BUY:
        cash = balances.cash_available_for_trading
        add(
            "sufficient_cash",
            cash is not None and cash >= estimated,
            f"Buy needs cash_available_for_trading >= {estimated} (have {cash}).",
        )
    else:  # SELL
        settled = _settled_long_quantity(positions, request.symbol)
        add(
            "sufficient_settled_shares",
            settled >= Decimal(request.quantity),
            f"Sell of {request.quantity} needs settled shares (have {settled}).",
        )

    passed = all(check.passed for check in checks)
    intent: ValidatedOrderIntent | None = None
    if passed:
        intent = ValidatedOrderIntent(
            request=request,
            account_hash=settings.account_hash,
            estimated_notional=estimated,
            quote_price=reference,
            quote_time=quote.quote_time,
            created_at=now,
        )
    return RiskReport(checks=checks, intent=intent)


def live_submission_blockers(settings: Settings) -> list[str]:
    """Return reasons a *live* submission is currently blocked by configuration.

    An empty list means the configuration gates are open. These are checked in
    addition to :func:`evaluate_order` and the interactive confirmation; no single
    CLI flag can open them.
    """
    blockers: list[str] = []
    if not settings.trading_enabled:
        blockers.append("SCHWAB_TRADING_ENABLED must be true.")
    if settings.dry_run:
        blockers.append("SCHWAB_DRY_RUN must be false.")
    if not settings.require_confirmation:
        blockers.append("SCHWAB_REQUIRE_CONFIRMATION must be true.")
    return blockers
