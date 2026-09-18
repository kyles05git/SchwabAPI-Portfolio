"""Tests for the authenticated client and account discovery (Phase 4).

All offline: mocked HTTP via respx, no network or credentials.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
import respx

from schwab_trader import accounts as accounts_mod
from schwab_trader import client as api
from schwab_trader.client import ApiError, SchwabClient
from schwab_trader.config import Settings

ACCOUNT_URL = f"{api.API_BASE_URL}{api.ACCOUNTS_PATH}/HASH"
ACCOUNT_BODY = {
    "securitiesAccount": {
        "type": "CASH",
        "accountNumber": "12345678",
        "isDayTrader": False,
        "currentBalances": {
            "cashBalance": 100.50,
            "cashAvailableForTrading": 80.25,
            "cashAvailableForWithdrawal": 80.25,
            "unsettledCash": 20.0,
            "totalCash": 100.50,
            "liquidationValue": 250.75,
            "longMarketValue": 150.25,
        },
        "positions": [
            {
                "instrument": {"symbol": "AAPL", "assetType": "EQUITY"},
                "longQuantity": 1,
                "shortQuantity": 0,
                "settledLongQuantity": 1,
                "averagePrice": 95.0,
                "marketValue": 100.0,
                "currentDayProfitLoss": 5.0,
                "currentDayProfitLossPercentage": 5.0,
            }
        ],
    }
}

NUMBERS = [
    {"accountNumber": "12345678", "hashValue": "ABCDEF0000HASH1234"},
    {"accountNumber": "87654321", "hashValue": "ZZZZ11112222H9999"},
]
NUMBERS_URL = api.API_BASE_URL + api.ACCOUNT_NUMBERS_PATH


class _FakeTokens:
    def __init__(self, token: str = "ACCESS-abc123") -> None:
        self._token = token

    def get_access_token(self) -> str:
        return self._token


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"client_id": "x", "client_secret": "y"}
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def _client() -> SchwabClient:
    # sleep is a no-op so retry backoff does not slow the tests.
    return SchwabClient(_settings(), _FakeTokens(), sleep=lambda _seconds: None)


# --- masking ----------------------------------------------------------------


def test_mask_tail_shows_last_four() -> None:
    assert accounts_mod.mask_tail("12345678") == "****5678"
    assert accounts_mod.mask_tail("ab") == "****"


# --- account discovery ------------------------------------------------------


@respx.mock
def test_get_account_numbers_parses_and_masks() -> None:
    respx.get(NUMBERS_URL).mock(return_value=httpx.Response(200, json=NUMBERS))
    with _client() as client:
        mappings = accounts_mod.get_account_numbers(client)
    assert len(mappings) == 2
    assert mappings[0].masked_number == "****5678"
    assert mappings[0].hash_value == "ABCDEF0000HASH1234"
    assert mappings[0].masked_hash == "****1234"


@respx.mock
def test_get_account_summary_parses_sanitized_fields() -> None:
    account_hash = "HASH1234"
    body = {
        "securitiesAccount": {
            "type": "CASH",
            "accountNumber": "12345678",
            "isDayTrader": False,
        }
    }
    respx.get(f"{api.API_BASE_URL}{api.ACCOUNTS_PATH}/{account_hash}").mock(
        return_value=httpx.Response(200, json=body)
    )
    with _client() as client:
        summary = accounts_mod.get_account_summary(client, account_hash)
    assert summary.account_type == "CASH"
    assert summary.account_number_masked == "****5678"
    assert summary.is_day_trader is False


# --- client behavior --------------------------------------------------------


@respx.mock
def test_client_sends_bearer_token() -> None:
    route = respx.get(NUMBERS_URL).mock(return_value=httpx.Response(200, json=[]))
    with _client() as client:
        client.get(api.ACCOUNT_NUMBERS_PATH)
    assert route.calls.last.request.headers["authorization"] == "Bearer ACCESS-abc123"


@respx.mock
def test_client_retries_on_429_then_succeeds() -> None:
    route = respx.get(NUMBERS_URL).mock(
        side_effect=[httpx.Response(429), httpx.Response(200, json=[])]
    )
    with _client() as client:
        assert client.get(api.ACCOUNT_NUMBERS_PATH) == []
    assert route.call_count == 2


@respx.mock
def test_client_retries_on_503_then_succeeds() -> None:
    route = respx.get(NUMBERS_URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=[])]
    )
    with _client() as client:
        client.get(api.ACCOUNT_NUMBERS_PATH)
    assert route.call_count == 2


@respx.mock
def test_client_retries_transport_error_then_succeeds() -> None:
    route = respx.get(NUMBERS_URL).mock(
        side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json=[])]
    )
    with _client() as client:
        client.get(api.ACCOUNT_NUMBERS_PATH)
    assert route.call_count == 2


@respx.mock
def test_client_gives_up_after_max_attempts() -> None:
    route = respx.get(NUMBERS_URL).mock(return_value=httpx.Response(503))
    with _client() as client, pytest.raises(ApiError) as exc:
        client.get(api.ACCOUNT_NUMBERS_PATH)
    assert exc.value.status_code == 503
    assert route.call_count == 3


@respx.mock
def test_client_raises_sanitized_apierror_on_404() -> None:
    respx.get(NUMBERS_URL).mock(return_value=httpx.Response(404, json={"message": "Not found"}))
    with _client() as client, pytest.raises(ApiError) as exc:
        client.get(api.ACCOUNT_NUMBERS_PATH)
    assert exc.value.status_code == 404
    assert "Not found" in str(exc.value)


@respx.mock
def test_apierror_masks_account_hash_in_path() -> None:
    # Deliberately synthetic: never paste a brokerage account identifier here.
    account_hash = "AB" * 30 + "CC99"
    url = f"{api.API_BASE_URL}{api.ACCOUNTS_PATH}/{account_hash}/orders/999"
    respx.get(url).mock(return_value=httpx.Response(404, json={"message": "Order not found"}))
    with _client() as client, pytest.raises(ApiError) as exc:
        client.get(f"{api.ACCOUNTS_PATH}/{account_hash}/orders/999")
    message = str(exc.value)
    assert account_hash not in message
    assert "****CC99" in message


@respx.mock
def test_client_does_not_retry_4xx() -> None:
    route = respx.get(NUMBERS_URL).mock(return_value=httpx.Response(403, json={"error": "denied"}))
    with _client() as client, pytest.raises(ApiError):
        client.get(api.ACCOUNT_NUMBERS_PATH)
    assert route.call_count == 1


# --- balances and positions -------------------------------------------------


@respx.mock
def test_get_balances_parses_decimals() -> None:
    respx.get(ACCOUNT_URL).mock(return_value=httpx.Response(200, json=ACCOUNT_BODY))
    with _client() as client:
        balances = accounts_mod.get_balances(client, "HASH")
    assert balances.account_type == "CASH"
    assert balances.cash_available_for_trading == Decimal("80.25")
    assert balances.buying_power == Decimal("80.25")
    assert balances.unsettled_cash == Decimal("20.0")
    assert balances.liquidation_value == Decimal("250.75")


@respx.mock
def test_get_positions_parses() -> None:
    respx.get(ACCOUNT_URL).mock(return_value=httpx.Response(200, json=ACCOUNT_BODY))
    with _client() as client:
        holdings = accounts_mod.get_positions(client, "HASH")
    assert len(holdings) == 1
    position = holdings[0]
    assert position.symbol == "AAPL"
    assert position.asset_type == "EQUITY"
    assert position.long_quantity == Decimal("1")
    assert position.settled_long_quantity == Decimal("1")
    assert position.average_price == Decimal("95.0")


@respx.mock
def test_get_positions_empty_when_none() -> None:
    body = {"securitiesAccount": {"type": "CASH"}}
    respx.get(ACCOUNT_URL).mock(return_value=httpx.Response(200, json=body))
    with _client() as client:
        assert accounts_mod.get_positions(client, "HASH") == []
