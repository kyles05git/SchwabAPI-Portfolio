"""Tests for operational event notifications (offline; no network)."""

from __future__ import annotations

from decimal import Decimal

from schwab_trader import events
from schwab_trader.models import OrderDetail, OrderRequest, OrderSide, OrderStatus
from schwab_trader.notify import NotifyError, NotifyMessage


def _request() -> OrderRequest:
    return OrderRequest(
        side=OrderSide.SELL, symbol="SOFI", quantity=30, limit_price=Decimal("17.05")
    )


class _RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[NotifyMessage] = []

    def send(self, message: NotifyMessage) -> None:
        self.sent.append(message)


class _FailingNotifier:
    def send(self, message: NotifyMessage) -> None:
        raise NotifyError("SMTP delivery failed: SMTPAuthenticationError")


# --- Builders ---------------------------------------------------------------


def test_fill_message_describes_order_and_masks_account() -> None:
    msg = events.order_filled_message(
        _request(), order_id="ABC123", status="FILLED", account_tail="****CC99"
    )
    assert msg.category == "alert"
    assert "SELL 30 SOFI" in msg.body
    assert "ABC123" in msg.body
    assert "****CC99" in msg.body


def test_reject_message_carries_reason() -> None:
    msg = events.order_rejected_message(
        _request(), reason="insufficient shares", account_tail="****CC99"
    )
    assert "REJECTED" in msg.subject
    assert "insufficient shares" in msg.body


def test_ambiguous_message_warns_to_check_schwab() -> None:
    msg = events.order_ambiguous_message(_request(), account_tail="****CC99")
    assert "AMBIGUOUS" in msg.subject
    assert "Schwab" in msg.body


def test_lifecycle_message_describes_transition_and_fill() -> None:
    msg = events.order_lifecycle_message(
        OrderDetail(
            order_id="1001",
            status=OrderStatus.FILLED,
            side="BUY",
            symbol="AAPL",
            quantity=Decimal(5),
        ),
        previous_status="WORKING",
        fill_delta=Decimal(5),
        account_tail="****CC99",
    )
    assert "AAPL FILLED" in msg.subject
    assert "WORKING -> FILLED" in msg.body
    assert "new fill:  5 shares" in msg.body
    assert "did not place, replace, or cancel" in msg.body


def test_kill_switch_tripped_message() -> None:
    msg = events.kill_switch_tripped_message(
        reason="daily loss limit breached", account_tail="****CC99"
    )
    assert "KILL SWITCH" in msg.subject
    assert "daily loss limit breached" in msg.body


def test_daily_loss_warning_reports_percentage() -> None:
    msg = events.daily_loss_warning_message(
        loss=Decimal("80"), limit=Decimal("100"), account_tail="****CC99"
    )
    assert "80%" in msg.subject
    assert "$80.00" in msg.body and "$100.00" in msg.body


# --- emit (fail-safe) -------------------------------------------------------


def test_emit_delivers_and_reports_true() -> None:
    notifier = _RecordingNotifier()
    ok = events.emit(notifier, events.order_ambiguous_message(_request(), account_tail="****CC99"))
    assert ok is True
    assert len(notifier.sent) == 1


def test_emit_swallows_notify_error_and_reports_false() -> None:
    # A failing channel must never raise out of emit - an order flow can't break
    # because an email failed.
    ok = events.emit(
        _FailingNotifier(),
        events.order_filled_message(
            _request(), order_id="X", status="FILLED", account_tail="****CC99"
        ),
    )
    assert ok is False
