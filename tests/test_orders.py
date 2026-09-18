"""Tests for typed order models and payload building (Phase 7). Pure, offline."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import respx
from pydantic import SecretStr, ValidationError

from schwab_trader import client as api
from schwab_trader import orders
from schwab_trader.client import SchwabClient
from schwab_trader.config import Settings
from schwab_trader.models import (
    AssetType,
    OrderDuration,
    OrderRequest,
    OrderSession,
    OrderSide,
    OrderStatus,
    OrderType,
    SubmittedOrder,
    ValidatedOrderIntent,
    build_order_payload,
)

ORDERS_URL = f"{api.API_BASE_URL}{api.ACCOUNTS_PATH}/HASH/orders"


class _FakeTokens:
    def get_access_token(self) -> str:
        return "ACCESS-abc123"


def _client() -> SchwabClient:
    settings = Settings(_env_file=None, client_id="x", client_secret="y")  # type: ignore[call-arg]
    return SchwabClient(settings, _FakeTokens(), sleep=lambda _seconds: None)


def _request(**overrides: object) -> OrderRequest:
    base: dict[str, object] = {
        "side": OrderSide.BUY,
        "symbol": "AAPL",
        "quantity": 1,
        "limit_price": Decimal("100.00"),
    }
    base.update(overrides)
    return OrderRequest(**base)  # type: ignore[arg-type]


# --- OrderRequest -----------------------------------------------------------


def test_defaults_are_limit_day_normal_equity() -> None:
    request = _request()
    assert request.order_type is OrderType.LIMIT
    assert request.duration is OrderDuration.DAY
    assert request.session is OrderSession.NORMAL
    assert request.asset_type is AssetType.EQUITY


def test_symbol_is_normalized_to_uppercase() -> None:
    assert _request(symbol=" aapl ").symbol == "AAPL"


def test_estimated_notional_and_confirmation_phrase() -> None:
    request = _request(side=OrderSide.BUY, symbol="MSFT", quantity=3, limit_price=Decimal("50.00"))
    assert request.estimated_notional == Decimal("150.00")
    assert request.confirmation_phrase == "BUY 3 MSFT AT 50.00"


def test_request_is_immutable() -> None:
    request = _request()
    with pytest.raises(ValidationError):
        request.quantity = 5  # type: ignore[misc]


@pytest.mark.parametrize("bad_quantity", [0, -1])
def test_rejects_non_positive_quantity(bad_quantity: int) -> None:
    with pytest.raises(ValidationError):
        _request(quantity=bad_quantity)


def test_rejects_fractional_quantity() -> None:
    with pytest.raises(ValidationError):
        _request(quantity=1.5)


@pytest.mark.parametrize("bad_price", [Decimal("0"), Decimal("-1"), Decimal("10.001")])
def test_rejects_bad_prices(bad_price: Decimal) -> None:
    with pytest.raises(ValidationError):
        _request(limit_price=bad_price)


@pytest.mark.parametrize("bad_symbol", ["", "123", "TOOLONGSYMBOL", "AA PL", "AA;PL"])
def test_rejects_invalid_symbols(bad_symbol: str) -> None:
    with pytest.raises(ValidationError):
        _request(symbol=bad_symbol)


# --- payload ----------------------------------------------------------------


def test_build_order_payload_buy() -> None:
    payload = build_order_payload(_request(side=OrderSide.BUY))
    assert payload["orderType"] == "LIMIT"
    assert payload["session"] == "NORMAL"
    assert payload["duration"] == "DAY"
    assert payload["orderStrategyType"] == "SINGLE"
    assert payload["price"] == "100.00"
    leg = payload["orderLegCollection"][0]
    assert leg["instruction"] == "BUY"
    assert leg["quantity"] == 1
    assert leg["instrument"] == {"symbol": "AAPL", "assetType": "EQUITY"}


def test_build_order_payload_sell() -> None:
    payload = build_order_payload(_request(side=OrderSide.SELL, symbol="SOFI", quantity=30))
    leg = payload["orderLegCollection"][0]
    assert leg["instruction"] == "SELL"
    assert leg["quantity"] == 30
    assert leg["instrument"]["symbol"] == "SOFI"


# --- OrderStatus ------------------------------------------------------------


def test_order_status_from_api_known_and_unknown() -> None:
    assert OrderStatus.from_api("FILLED") is OrderStatus.FILLED
    assert OrderStatus.from_api("working") is OrderStatus.WORKING
    assert OrderStatus.from_api("SOMETHING_NEW") is OrderStatus.UNKNOWN
    assert OrderStatus.from_api(None) is OrderStatus.UNKNOWN


# --- ValidatedOrderIntent / SubmittedOrder ----------------------------------


def test_validated_intent_payload_and_masking() -> None:
    request = _request()
    intent = ValidatedOrderIntent(
        request=request,
        account_hash=SecretStr("ABCDEF1234567890"),
        estimated_notional=request.estimated_notional,
        created_at=datetime.now(UTC),
    )
    assert intent.to_api_payload() == build_order_payload(request)
    assert intent.masked_account == "****7890"
    assert "ABCDEF1234567890" not in repr(intent)


def test_submitted_order_defaults() -> None:
    order = SubmittedOrder(submitted_at=datetime.now(UTC))
    assert order.status is OrderStatus.UNKNOWN
    assert order.order_id is None


# --- submission (mocked HTTP) -----------------------------------------------


@respx.mock
def test_submit_order_success_extracts_order_id() -> None:
    location = f"{api.API_BASE_URL}{api.ACCOUNTS_PATH}/HASH/orders/1005812345"
    respx.post(ORDERS_URL).mock(return_value=httpx.Response(201, headers={"Location": location}))
    with _client() as client:
        result = orders.submit_order(client, "HASH", _request())
    assert result.order_id == "1005812345"
    assert result.status is OrderStatus.ACCEPTED
    # The stored location must not contain the account hash.
    assert result.location == "orders/1005812345"
    assert "HASH" not in (result.location or "")


@respx.mock
def test_submit_order_rejected_4xx_raises_order_rejected() -> None:
    respx.post(ORDERS_URL).mock(return_value=httpx.Response(400, json={"message": "invalid price"}))
    with _client() as client, pytest.raises(orders.OrderRejected) as exc:
        orders.submit_order(client, "HASH", _request())
    assert exc.value.status_code == 400


@respx.mock
def test_submit_order_5xx_is_ambiguous() -> None:
    respx.post(ORDERS_URL).mock(return_value=httpx.Response(503))
    with _client() as client, pytest.raises(orders.AmbiguousSubmission):
        orders.submit_order(client, "HASH", _request())


@respx.mock
def test_submit_order_network_error_is_ambiguous() -> None:
    respx.post(ORDERS_URL).mock(side_effect=httpx.ConnectError("boom"))
    with _client() as client, pytest.raises(orders.AmbiguousSubmission):
        orders.submit_order(client, "HASH", _request())


@respx.mock
def test_submit_order_is_attempted_exactly_once_on_failure() -> None:
    route = respx.post(ORDERS_URL).mock(return_value=httpx.Response(503))
    with _client() as client, pytest.raises(orders.AmbiguousSubmission):
        orders.submit_order(client, "HASH", _request())
    assert route.call_count == 1  # never retried


@respx.mock
def test_get_order_parses_status_and_fields() -> None:
    url = f"{api.API_BASE_URL}{api.ACCOUNTS_PATH}/HASH/orders/1005"
    body = {
        "orderId": 1005,
        "status": "WORKING",
        "quantity": 1,
        "filledQuantity": 0,
        "remainingQuantity": 1,
        "price": "18.00",
        "cancelable": True,
        "enteredTime": "2026-07-13T15:00:00+0000",
        "orderLegCollection": [
            {"instruction": "BUY", "instrument": {"symbol": "SOFI", "assetType": "EQUITY"}}
        ],
    }
    respx.get(url).mock(return_value=httpx.Response(200, json=body))
    with _client() as client:
        detail = orders.get_order(client, "HASH", "1005")
    assert detail.order_id == "1005"
    assert detail.status is OrderStatus.WORKING
    assert detail.symbol == "SOFI"
    assert detail.side == "BUY"
    assert detail.limit_price == Decimal("18.00")
    assert detail.cancelable is True


@respx.mock
def test_cancel_order_success() -> None:
    url = f"{api.API_BASE_URL}{api.ACCOUNTS_PATH}/HASH/orders/1005"
    route = respx.delete(url).mock(return_value=httpx.Response(200))
    with _client() as client:
        orders.cancel_order(client, "HASH", "1005")
    assert route.called


@respx.mock
def test_cancel_order_rejected_raises_apierror() -> None:
    url = f"{api.API_BASE_URL}{api.ACCOUNTS_PATH}/HASH/orders/1005"
    respx.delete(url).mock(return_value=httpx.Response(400, json={"message": "not cancelable"}))
    with _client() as client, pytest.raises(api.ApiError):
        orders.cancel_order(client, "HASH", "1005")


@respx.mock
def test_cancel_order_network_error_is_ambiguous() -> None:
    url = f"{api.API_BASE_URL}{api.ACCOUNTS_PATH}/HASH/orders/1005"
    respx.delete(url).mock(side_effect=httpx.ConnectError("boom"))
    with _client() as client, pytest.raises(orders.AmbiguousSubmission):
        orders.cancel_order(client, "HASH", "1005")


def test_parse_order_detail_handles_missing_fields() -> None:
    detail = orders.parse_order_detail({"status": "REJECTED"}, fallback_id="X1")
    assert detail.order_id == "X1"
    assert detail.status is OrderStatus.REJECTED
    assert detail.symbol is None


# --- replace ----------------------------------------------------------------


@respx.mock
def test_replace_order_success_returns_new_id() -> None:
    url = f"{api.API_BASE_URL}{api.ACCOUNTS_PATH}/HASH/orders/1005"
    new_location = f"{api.API_BASE_URL}{api.ACCOUNTS_PATH}/HASH/orders/2010"
    route = respx.put(url).mock(
        return_value=httpx.Response(201, headers={"Location": new_location})
    )
    with _client() as client:
        result = orders.replace_order(client, "HASH", "1005", _request())
    assert route.called
    assert result.order_id == "2010"
    assert result.status is OrderStatus.ACCEPTED


@respx.mock
def test_replace_order_rejected_4xx() -> None:
    url = f"{api.API_BASE_URL}{api.ACCOUNTS_PATH}/HASH/orders/1005"
    respx.put(url).mock(return_value=httpx.Response(400, json={"message": "cannot replace"}))
    with _client() as client, pytest.raises(orders.OrderRejected):
        orders.replace_order(client, "HASH", "1005", _request())


@respx.mock
def test_replace_order_5xx_is_ambiguous() -> None:
    url = f"{api.API_BASE_URL}{api.ACCOUNTS_PATH}/HASH/orders/1005"
    respx.put(url).mock(return_value=httpx.Response(502))
    with _client() as client, pytest.raises(orders.AmbiguousSubmission):
        orders.replace_order(client, "HASH", "1005", _request())


@respx.mock
def test_replace_order_network_error_is_ambiguous() -> None:
    url = f"{api.API_BASE_URL}{api.ACCOUNTS_PATH}/HASH/orders/1005"
    respx.put(url).mock(side_effect=httpx.ConnectError("boom"))
    with _client() as client, pytest.raises(orders.AmbiguousSubmission):
        orders.replace_order(client, "HASH", "1005", _request())
