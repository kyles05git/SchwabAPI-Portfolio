"""Tests for OAuth authorization-URL building and callback parsing (Phase 2).

All offline: no network, no credentials required.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from schwab_trader import client
from schwab_trader.auth import (
    AUTHORIZE_URL,
    TOKEN_URL,
    AuthorizationResult,
    CallbackValidationError,
    NotAuthenticatedError,
    OAuthError,
    ReauthRequiredError,
    TokenManager,
    build_authorization_url,
    exchange_code_for_tokens,
    parse_authorization_response,
    refresh_tokens,
    verify_authorization,
)
from schwab_trader.config import Settings
from schwab_trader.token_store import TokenSet, TokenStore

TOKEN_RESPONSE = {
    "access_token": "ACCESS-abc123",
    "refresh_token": "REFRESH-xyz789",
    "token_type": "Bearer",
    "scope": "api",
    "expires_in": 1800,
}

CALLBACK = "https://127.0.0.1:8182/callback"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "client_id": "APPKEY123",
        "client_secret": "topsecret",
        "callback_url": CALLBACK,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


# --- build_authorization_url ------------------------------------------------


def test_build_url_has_expected_params() -> None:
    url = build_authorization_url(_settings())
    assert url.startswith(AUTHORIZE_URL + "?")
    query = parse_qs(urlsplit(url).query)
    assert query["client_id"] == ["APPKEY123"]
    assert query["redirect_uri"] == [CALLBACK]
    assert query["response_type"] == ["code"]


def test_build_url_never_contains_secret() -> None:
    url = build_authorization_url(_settings())
    assert "topsecret" not in url


def test_build_url_requires_credentials() -> None:
    with pytest.raises(OAuthError):
        build_authorization_url(_settings(client_id="", client_secret=""))


# --- parse_authorization_response -------------------------------------------


def test_parse_valid_callback_extracts_code_and_session() -> None:
    result = parse_authorization_response(f"{CALLBACK}?code=ABC123&session=SESS789", CALLBACK)
    assert result == AuthorizationResult(code="ABC123", session="SESS789")


def test_parse_valid_callback_without_session() -> None:
    result = parse_authorization_response(f"{CALLBACK}?code=ABC123", CALLBACK)
    assert result.code == "ABC123"
    assert result.session is None


def test_parse_tolerates_trailing_slash_in_path() -> None:
    result = parse_authorization_response("https://127.0.0.1:8182/callback/?code=ABC123", CALLBACK)
    assert result.code == "ABC123"


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://127.0.0.1:8182/callback?code=ABC123",  # wrong scheme
        "https://evil.example.com:8182/callback?code=ABC123",  # wrong host
        "https://127.0.0.1:9999/callback?code=ABC123",  # wrong port
        "https://127.0.0.1:8182/wrong?code=ABC123",  # wrong path
        "https://127.0.0.1:8182/callback?session=SESS789",  # missing code
        "just-the-code-not-a-url",  # not a URL at all
    ],
)
def test_parse_rejects_invalid_callbacks(bad_url: str) -> None:
    with pytest.raises(CallbackValidationError):
        parse_authorization_response(bad_url, CALLBACK)


def test_parse_raises_on_oauth_error_response() -> None:
    with pytest.raises(OAuthError) as exc:
        parse_authorization_response(
            f"{CALLBACK}?error=access_denied&error_description=User+denied", CALLBACK
        )
    # The error message should not leak the raw callback query verbatim.
    assert "access_denied" in str(exc.value)


def test_authorization_result_repr_redacts_code() -> None:
    result = AuthorizationResult(code="SUPERSECRETCODE", session="SESS")
    assert "SUPERSECRETCODE" not in repr(result)
    assert "[REDACTED]" in repr(result)


# --- Token exchange / refresh (mocked HTTP) ---------------------------------


@respx.mock
def test_exchange_code_success_sends_basic_auth_and_grant() -> None:
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    tokens = exchange_code_for_tokens(_settings(), "AUTHCODE")

    assert route.called
    request = route.calls.last.request
    body = request.content.decode()
    assert "grant_type=authorization_code" in body
    assert "code=AUTHCODE" in body

    header = request.headers["authorization"]
    assert header.startswith("Basic ")
    decoded = base64.b64decode(header.split(" ", 1)[1]).decode()
    assert decoded == "APPKEY123:topsecret"

    assert tokens.access_token.get_secret_value() == "ACCESS-abc123"
    assert not tokens.is_access_expired(timedelta(0))


@respx.mock
def test_exchange_rejected_raises_without_leaking_secret() -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant", "error_description": "x"})
    )
    with pytest.raises(OAuthError) as exc:
        exchange_code_for_tokens(_settings(), "BADCODE")
    assert "topsecret" not in str(exc.value)


@respx.mock
def test_refresh_preserves_original_refresh_window() -> None:
    window_end = datetime(2026, 7, 20, tzinfo=UTC)
    previous = TokenSet.from_token_response(TOKEN_RESPONSE, refresh_token_expires_at=window_end)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={**TOKEN_RESPONSE, "access_token": "ACCESS-new"})
    )
    refreshed = refresh_tokens(_settings(), previous)
    assert refreshed.access_token.get_secret_value() == "ACCESS-new"
    assert refreshed.refresh_token_expires_at == window_end


@respx.mock
def test_refresh_falls_back_to_previous_refresh_token() -> None:
    previous = TokenSet.from_token_response(TOKEN_RESPONSE)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "ACCESS-new", "expires_in": 1800})
    )
    refreshed = refresh_tokens(_settings(), previous)
    assert refreshed.refresh_token.get_secret_value() == "REFRESH-xyz789"


@respx.mock
def test_refresh_rejected_requires_reauth() -> None:
    previous = TokenSet.from_token_response(TOKEN_RESPONSE)
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(ReauthRequiredError):
        refresh_tokens(_settings(), previous)


@respx.mock
def test_verify_authorization_true_on_200() -> None:
    url = client.API_BASE_URL + client.ACCOUNT_NUMBERS_PATH
    respx.get(url).mock(return_value=httpx.Response(200, json=[]))
    assert verify_authorization(_settings(), "TOKEN") is True


@respx.mock
def test_verify_authorization_false_on_401() -> None:
    url = client.API_BASE_URL + client.ACCOUNT_NUMBERS_PATH
    respx.get(url).mock(return_value=httpx.Response(401, json={}))
    assert verify_authorization(_settings(), "TOKEN") is False


# --- TokenManager -----------------------------------------------------------


@respx.mock
def test_manager_returns_valid_token_without_refresh(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens.json")
    store.save(TokenSet.from_token_response(TOKEN_RESPONSE))
    manager = TokenManager(_settings(), store)
    # No routes registered: any HTTP call would raise, proving no refresh happened.
    assert manager.get_access_token() == "ACCESS-abc123"


@respx.mock
def test_manager_refreshes_expired_and_persists(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens.json")
    expired = TokenSet.from_token_response(
        TOKEN_RESPONSE, obtained_at=datetime.now(UTC) - timedelta(hours=1)
    )
    store.save(expired)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={**TOKEN_RESPONSE, "access_token": "ACCESS-refreshed"}
        )
    )

    manager = TokenManager(_settings(), store)
    assert manager.get_access_token() == "ACCESS-refreshed"

    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.access_token.get_secret_value() == "ACCESS-refreshed"


def test_manager_not_authenticated_when_no_tokens(tmp_path: Path) -> None:
    manager = TokenManager(_settings(), TokenStore(tmp_path / "absent.json"))
    with pytest.raises(NotAuthenticatedError):
        manager.get_access_token()


@respx.mock
def test_manager_requires_reauth_when_refresh_window_expired(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens.json")
    old = TokenSet.from_token_response(
        TOKEN_RESPONSE, obtained_at=datetime.now(UTC) - timedelta(days=8)
    )
    store.save(old)
    manager = TokenManager(_settings(), store)
    with pytest.raises(ReauthRequiredError):
        manager.get_access_token()
