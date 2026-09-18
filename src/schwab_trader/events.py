"""Operational event notifications (notification-layer idea #4).

Pure builders that turn order/safety events into
:class:`~schwab_trader.notify.NotifyMessage` objects, plus :func:`emit` - a
fail-safe send that swallows any :class:`~schwab_trader.notify.NotifyError`. An
order flow must never break because a notification could not be delivered, so
callers use ``emit`` rather than ``notifier.send`` directly.

Message bodies are sanitized: they carry the order description (side/qty/symbol/
price) and the masked account tail only - never the account hash or any secret.
"""

from __future__ import annotations

from decimal import Decimal

from schwab_trader.logging_config import get_logger
from schwab_trader.models import OrderDetail, OrderRequest
from schwab_trader.notify import Notifier, NotifyError, NotifyMessage

log = get_logger("schwab_trader.events")


def order_filled_message(
    request: OrderRequest, *, order_id: str | None, status: str, account_tail: str
) -> NotifyMessage:
    """A live order was placed/accepted."""
    oid = order_id or "unknown"
    return NotifyMessage(
        subject=f"Order placed: {request.describe()}",
        body=(
            f"A live order was placed on account {account_tail}.\n\n"
            f"  {request.describe()}\n"
            f"  order id: {oid}\n"
            f"  status:   {status}\n\n"
            "Verify this order in the Schwab app or website."
        ),
        category="alert",
    )


def order_rejected_message(
    request: OrderRequest, *, reason: str, account_tail: str
) -> NotifyMessage:
    """A live order was rejected by the broker."""
    return NotifyMessage(
        subject=f"Order REJECTED: {request.describe()}",
        body=(
            f"A live order on account {account_tail} was rejected and NOT placed.\n\n"
            f"  {request.describe()}\n"
            f"  reason: {reason}"
        ),
        category="alert",
    )


def order_ambiguous_message(request: OrderRequest, *, account_tail: str) -> NotifyMessage:
    """A submission timed out or lost its response - state is unknown."""
    return NotifyMessage(
        subject=f"Order AMBIGUOUS: {request.describe()}",
        body=(
            f"A live order on account {account_tail} returned an ambiguous result - it may or "
            "may not have been placed, and was NOT retried automatically.\n\n"
            f"  {request.describe()}\n\n"
            "Check recent orders in the Schwab app before doing anything else."
        ),
        category="alert",
    )


def order_lifecycle_message(
    detail: OrderDetail,
    *,
    previous_status: str,
    fill_delta: Decimal,
    account_tail: str,
) -> NotifyMessage:
    """A tracked broker order changed status or gained an execution fill."""
    symbol = detail.symbol or "unknown symbol"
    side = detail.side or "unknown side"
    fill_line = f"\n  new fill:  {fill_delta} shares" if fill_delta > 0 else ""
    return NotifyMessage(
        subject=f"Order update: {symbol} {detail.status.value}",
        body=(
            f"A tracked order on account {account_tail} changed at Schwab.\n\n"
            f"  order id: {detail.order_id}\n"
            f"  order:    {side} {detail.quantity or '?'} {symbol}\n"
            f"  status:   {previous_status} -> {detail.status.value}"
            f"{fill_line}\n\n"
            "The reconciliation process did not place, replace, or cancel an order."
        ),
        category="alert",
    )


def kill_switch_blocked_message(request: OrderRequest, *, account_tail: str) -> NotifyMessage:
    """A live order was blocked because the kill switch is engaged."""
    return NotifyMessage(
        subject="Live order blocked: kill switch engaged",
        body=(
            f"A live order on account {account_tail} was blocked because the kill switch is "
            "engaged; nothing was submitted.\n\n"
            f"  {request.describe()}\n\n"
            "Clear it with 'schwab-trader safety resume' once you have reviewed why it tripped."
        ),
        category="alert",
    )


def kill_switch_tripped_message(*, reason: str, account_tail: str) -> NotifyMessage:
    """The kill switch just auto-engaged (e.g. the daily-loss limit was breached)."""
    return NotifyMessage(
        subject="KILL SWITCH engaged - autonomous trading halted",
        body=(
            f"The kill switch just engaged on account {account_tail}. All autonomous/live "
            "trading is halted until a human resumes it.\n\n"
            f"  reason: {reason}\n\n"
            "Review, then clear with 'schwab-trader safety resume' if appropriate."
        ),
        category="alert",
    )


def daily_loss_warning_message(
    *, loss: Decimal, limit: Decimal, account_tail: str
) -> NotifyMessage:
    """Today's loss is approaching the configured daily-loss limit."""
    pct = (loss / limit * 100) if limit > 0 else Decimal(0)
    return NotifyMessage(
        subject=f"Daily loss approaching limit ({pct:.0f}%)",
        body=(
            f"Today's loss on account {account_tail} is ${loss:,.2f} against a "
            f"${limit:,.2f} daily-loss limit ({pct:.0f}% of the limit). At 100% the kill "
            "switch engages and halts trading."
        ),
        category="alert",
    )


def emit(notifier: Notifier, message: NotifyMessage) -> bool:
    """Send ``message``, swallowing any :class:`NotifyError`. Returns delivered?

    Notifications are best-effort: a failure here is logged and reported to the
    caller as ``False``, never raised - so an order or safety flow is never
    disrupted by a dark or failing notification channel.
    """
    try:
        notifier.send(message)
        return True
    except NotifyError as exc:
        log.warning("Event notification not delivered (%s): %s", message.category, exc)
        return False
