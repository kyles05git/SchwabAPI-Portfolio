"""Submit, read, and (later) manage supported orders.

Phase 11 implements submission and status lookup for U.S. equity LIMIT/DAY/NORMAL
orders. Key safety property: an order POST is attempted **exactly once** and is
never automatically retried. A network error or 5xx is treated as *ambiguous*
(the order may or may not have reached Schwab), which callers must resolve by
inspection rather than resubmission.

Cancel and replace arrive in Phases 12-13.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from schwab_trader import client as api
from schwab_trader.models import (
    OrderDetail,
    OrderRequest,
    OrderStatus,
    SubmittedOrder,
    build_order_payload,
)


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


class OrderError(Exception):
    """Base class for order operation failures."""


class OrderRejected(OrderError):
    """The order was definitively rejected by Schwab (a 4xx response)."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"Order rejected (HTTP {status_code}): {detail}")
        self.status_code = status_code
        self.detail = detail


class AmbiguousSubmission(OrderError):
    """The submission outcome is unknown; the order may or may not have been placed.

    The caller must NOT resubmit; it must reconcile against recent orders.
    """


def _orders_path(account_hash: str) -> str:
    return f"{api.ACCOUNTS_PATH}/{account_hash}/orders"


def _extract_order_id(response: Any) -> str | None:
    """Extract the order id from the 201 response Location header, if present.

    Only the final path segment (the order id) is returned - never the full
    location, which contains the account hash.
    """
    location = response.headers.get("Location")
    if not location:
        return None
    return location.rstrip("/").rsplit("/", 1)[-1] or None


def submit_order(
    client: api.SchwabClient, account_hash: str, request: OrderRequest
) -> SubmittedOrder:
    """Submit an order exactly once.

    Raises:
        AmbiguousSubmission: on a network error or 5xx (do not resubmit).
        OrderRejected: on a 4xx rejection.
    """
    payload = build_order_payload(request)
    try:
        response = client.request_once("POST", _orders_path(account_hash), json=payload)
    except api.TransportFailure as exc:
        raise AmbiguousSubmission(
            "Network error during submission; the order may or may not have reached "
            "Schwab. Do not resubmit - reconcile against recent orders."
        ) from exc
    except api.ApiError as exc:
        if exc.status_code >= 500:
            raise AmbiguousSubmission(
                f"Server error (HTTP {exc.status_code}) during submission; the order may "
                "or may not have been placed. Do not resubmit - reconcile."
            ) from exc
        raise OrderRejected(exc.status_code, exc.message) from exc

    order_id = _extract_order_id(response)
    return SubmittedOrder(
        order_id=order_id,
        status=OrderStatus.ACCEPTED,
        submitted_at=datetime.now(UTC),
        location=f"orders/{order_id}" if order_id else None,
    )


def _average_fill_price(data: dict[str, Any]) -> Decimal | None:
    """Quantity-weighted average execution price across an order's fills, if any.

    Reads ``orderActivityCollection[].executionLegs[]`` (price, quantity). Returns
    ``None`` when nothing has executed - so the caller can fall back to the limit price.
    """
    total_qty = Decimal(0)
    total_notional = Decimal(0)
    for activity in data.get("orderActivityCollection") or []:
        if not isinstance(activity, dict):
            continue
        for leg in activity.get("executionLegs") or []:
            if not isinstance(leg, dict):
                continue
            price = _dec(leg.get("price"))
            qty = _dec(leg.get("quantity"))
            if price is None or qty is None or qty <= 0:
                continue
            total_qty += qty
            total_notional += price * qty
    return (total_notional / total_qty) if total_qty > 0 else None


def parse_order_detail(data: Any, *, fallback_id: str) -> OrderDetail:
    """Parse a Schwab order object into a sanitized :class:`OrderDetail`."""
    if not isinstance(data, dict):
        return OrderDetail(order_id=fallback_id)
    legs = data.get("orderLegCollection") or []
    leg = legs[0] if legs and isinstance(legs[0], dict) else {}
    instrument = leg.get("instrument", {}) if isinstance(leg, dict) else {}
    cancelable = data.get("cancelable")
    return OrderDetail(
        order_id=str(data.get("orderId", fallback_id)),
        status=OrderStatus.from_api(data.get("status")),
        symbol=instrument.get("symbol"),
        side=leg.get("instruction"),
        quantity=_dec(data.get("quantity")),
        filled_quantity=_dec(data.get("filledQuantity")),
        remaining_quantity=_dec(data.get("remainingQuantity")),
        limit_price=_dec(data.get("price")),
        average_fill_price=_average_fill_price(data),
        cancelable=bool(cancelable) if cancelable is not None else None,
        entered_time=data.get("enteredTime"),
        close_time=data.get("closeTime"),
    )


def replace_order(
    client: api.SchwabClient, account_hash: str, order_id: str, request: OrderRequest
) -> SubmittedOrder:
    """Atomically cancel-and-replace an existing order (PUT).

    Attempted exactly once, never auto-retried, with the same ambiguous/rejected
    handling as :func:`submit_order`. Returns the new order's details.
    """
    payload = build_order_payload(request)
    try:
        response = client.request_once(
            "PUT", f"{_orders_path(account_hash)}/{order_id}", json=payload
        )
    except api.TransportFailure as exc:
        raise AmbiguousSubmission(
            "Network error during replace; the original may have been canceled and/or the "
            "replacement placed. Do not retry - reconcile against recent orders."
        ) from exc
    except api.ApiError as exc:
        if exc.status_code >= 500:
            raise AmbiguousSubmission(
                f"Server error (HTTP {exc.status_code}) during replace; state is uncertain. "
                "Do not retry - reconcile."
            ) from exc
        raise OrderRejected(exc.status_code, exc.message) from exc

    new_id = _extract_order_id(response)
    return SubmittedOrder(
        order_id=new_id,
        status=OrderStatus.ACCEPTED,
        submitted_at=datetime.now(UTC),
        location=f"orders/{new_id}" if new_id else None,
    )


def get_order(client: api.SchwabClient, account_hash: str, order_id: str) -> OrderDetail:
    """Fetch a single order's current details."""
    data: Any = client.get(f"{_orders_path(account_hash)}/{order_id}")
    return parse_order_detail(data, fallback_id=order_id)


def cancel_order(client: api.SchwabClient, account_hash: str, order_id: str) -> None:
    """Cancel an order (DELETE). Attempted once; a network error is surfaced.

    Raises:
        AmbiguousSubmission: on a network error (verify in Schwab).
        api.ApiError: if Schwab rejects the cancel (e.g. not cancelable).
    """
    try:
        client.request_once("DELETE", f"{_orders_path(account_hash)}/{order_id}")
    except api.TransportFailure as exc:
        raise AmbiguousSubmission(
            "Network error during cancel; verify in Schwab whether the order was canceled."
        ) from exc


def get_recent_orders(
    client: api.SchwabClient,
    account_hash: str,
    *,
    from_time: str,
    to_time: str,
    max_results: int = 50,
) -> list[dict[str, Any]]:
    """Fetch recent orders in a time window (used to reconcile ambiguous results)."""
    params = {
        "fromEnteredTime": from_time,
        "toEnteredTime": to_time,
        "maxResults": max_results,
    }
    data: Any = client.get(_orders_path(account_hash), params=params)
    return data if isinstance(data, list) else []
