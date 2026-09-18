"""Schwab OAuth 2.0: authorization URL and manual callback parsing.

Phase 2 scope: build the Schwab authorization URL and safely parse/validate the
redirected callback URL to extract the (short-lived) authorization ``code``.

Token exchange and secure token storage are implemented in Phase 3. No network
calls happen in this module.

Endpoints (from CLAUDE.md; confirmed against reference implementations):

- Authorization: ``https://api.schwabapi.com/v1/oauth/authorize``
- Token:         ``https://api.schwabapi.com/v1/oauth/token``

Schwab's authorization-code flow uses ``client_id``, ``redirect_uri`` and
``response_type=code``. The callback returns ``code`` and a Schwab ``session``
value (Schwab does not use the OAuth ``state`` parameter).
"""

from __future__ import annotations

import base64
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from schwab_trader import client
from schwab_trader.logging_config import get_logger
from schwab_trader.token_store import TokenSet, TokenStore

if TYPE_CHECKING:
    from schwab_trader.config import Settings

AUTHORIZE_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"

_log = get_logger("schwab_trader.auth")


class OAuthError(Exception):
    """Base error for OAuth problems. Never carries secret values."""


class CallbackValidationError(OAuthError):
    """The redirected callback URL failed validation."""


class NotAuthenticatedError(OAuthError):
    """No stored tokens are available; an interactive login is required."""


class ReauthRequiredError(OAuthError):
    """The refresh token was rejected or has expired; re-run ``auth login``."""


@dataclass(frozen=True)
class AuthorizationResult:
    """A parsed, validated authorization callback.

    ``code`` is a short-lived, sensitive authorization code and must never be
    logged or printed; its ``repr`` is redacted.
    """

    code: str
    session: str | None = None

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"AuthorizationResult(code='[REDACTED]', session={'set' if self.session else None})"


def build_authorization_url(settings: Settings) -> str:
    """Construct the Schwab authorization URL for the interactive login step.

    The URL contains the client id (app key) but never the client secret.

    Raises:
        OAuthError: if client credentials are not configured.
    """
    if not settings.has_credentials:
        msg = (
            "Client credentials are not configured (set SCHWAB_CLIENT_ID and SCHWAB_CLIENT_SECRET)."
        )
        raise OAuthError(msg)

    params = {
        "client_id": settings.client_id,
        "redirect_uri": settings.callback_url,
        "response_type": "code",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def parse_authorization_response(raw_url: str, expected_callback: str) -> AuthorizationResult:
    """Validate a redirected callback URL and extract the authorization code.

    The callback's scheme, host, port, and path must match ``expected_callback``
    (trailing slashes on the path are ignored). Error messages never include the
    raw URL, code, or query string.

    Raises:
        CallbackValidationError: on any scheme/host/port/path mismatch or a
            missing authorization code.
        OAuthError: if the callback carries an OAuth error response.
    """
    got = urlsplit(raw_url.strip())
    expected = urlsplit(expected_callback.strip())

    if got.scheme.lower() != expected.scheme.lower():
        raise CallbackValidationError("Callback scheme does not match the configured callback URL.")
    if (got.hostname or "").lower() != (expected.hostname or "").lower():
        raise CallbackValidationError("Callback host does not match the configured callback URL.")
    if got.port != expected.port:
        raise CallbackValidationError("Callback port does not match the configured callback URL.")
    if got.path.rstrip("/") != expected.path.rstrip("/"):
        raise CallbackValidationError("Callback path does not match the configured callback URL.")

    query = parse_qs(got.query, keep_blank_values=True)

    errors = query.get("error")
    if errors and errors[0]:
        description = query.get("error_description") or [""]
        message = f"Authorization was not granted: {errors[0]} {description[0]}".strip()
        raise OAuthError(message)

    codes = query.get("code")
    if not codes or not codes[0]:
        raise CallbackValidationError("No authorization code was found in the callback URL.")

    sessions = query.get("session")
    session = sessions[0] if sessions and sessions[0] else None
    return AuthorizationResult(code=codes[0], session=session)


# --- Token exchange and refresh (HTTPS) -------------------------------------


def _basic_auth_header(settings: Settings) -> str:
    raw = f"{settings.client_id}:{settings.client_secret.get_secret_value()}"
    encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    return f"Basic {encoded}"


def _safe_error_detail(response: httpx.Response) -> str:
    """Extract a sanitized error description from a token-endpoint response.

    Returns the OAuth ``error``/``error_description`` when present; never returns
    tokens or the raw body.
    """
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(body, dict):
        error = body.get("error") or ""
        description = body.get("error_description") or ""
        detail = f"{error} {description}".strip()
        return detail or f"HTTP {response.status_code}"
    return f"HTTP {response.status_code}"


def _post_token_request(settings: Settings, data: dict[str, str]) -> dict[str, Any]:
    headers = {
        "Authorization": _basic_auth_header(settings),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        with httpx.Client(timeout=client.DEFAULT_TIMEOUT) as http:
            response = http.post(TOKEN_URL, data=data, headers=headers)
    except httpx.HTTPError as exc:
        raise OAuthError("Network error contacting the Schwab token endpoint.") from exc

    if response.status_code == 200:
        payload: dict[str, Any] = response.json()
        return payload

    detail = _safe_error_detail(response)
    if response.status_code in (400, 401):
        # A rejected grant: the caller decides whether this means re-login.
        raise _GrantRejected(response.status_code, detail)
    raise OAuthError(f"Token request failed (HTTP {response.status_code}): {detail}")


class _GrantRejected(OAuthError):
    """Internal: the authorization grant was rejected (HTTP 400/401)."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def exchange_code_for_tokens(settings: Settings, code: str) -> TokenSet:
    """Exchange an authorization code for a fresh token set.

    Raises:
        OAuthError: if the exchange fails (e.g. the code expired or is invalid).
    """
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.callback_url,
    }
    try:
        payload = _post_token_request(settings, data)
    except _GrantRejected as exc:
        raise OAuthError(
            f"Authorization code was rejected ({exc.detail}). "
            "Authorization codes expire within ~30 seconds - run 'auth login' again."
        ) from exc
    _log.info("Exchanged authorization code for tokens.")
    return TokenSet.from_token_response(payload)


def refresh_tokens(settings: Settings, previous: TokenSet) -> TokenSet:
    """Obtain a new access token using the stored refresh token.

    Preserves the original 7-day refresh window (refreshing does not extend it).

    Raises:
        ReauthRequiredError: if the refresh token is rejected/expired.
        OAuthError: on other transport/server failures.
    """
    data = {
        "grant_type": "refresh_token",
        "refresh_token": previous.refresh_token.get_secret_value(),
    }
    try:
        payload = _post_token_request(settings, data)
    except _GrantRejected as exc:
        raise ReauthRequiredError(
            f"The refresh token was rejected ({exc.detail}). "
            "Run 'python -m schwab_trader auth login' to reauthorize."
        ) from exc
    _log.info("Refreshed access token.")
    return TokenSet.from_token_response(
        payload,
        refresh_token_fallback=previous.refresh_token.get_secret_value(),
        refresh_token_expires_at=previous.refresh_token_expires_at,
    )


def verify_authorization(settings: Settings, access_token: str) -> bool:
    """Call a harmless read-only endpoint to confirm the access token works.

    Returns True on HTTP 200. Does not parse or store any account data.
    """
    url = f"{client.API_BASE_URL}{client.ACCOUNT_NUMBERS_PATH}"
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    try:
        with httpx.Client(timeout=client.DEFAULT_TIMEOUT) as http:
            response = http.get(url, headers=headers)
    except httpx.HTTPError:
        return False
    return response.status_code == 200


class TokenManager:
    """Provides valid access tokens, refreshing (serialized) as needed.

    A single lock serializes refresh attempts so concurrent callers do not
    refresh simultaneously. After a successful refresh the new tokens are
    persisted atomically. An invalid refresh token is not retried; it raises
    :class:`ReauthRequiredError`.
    """

    def __init__(self, settings: Settings, store: TokenStore) -> None:
        self._settings = settings
        self._store = store
        self._lock = threading.Lock()
        self._tokens: TokenSet | None = store.load()

    @property
    def tokens(self) -> TokenSet | None:
        return self._tokens

    def set_tokens(self, tokens: TokenSet) -> None:
        """Persist and cache a new token set (used right after login)."""
        with self._lock:
            self._store.save(tokens)
            self._tokens = tokens

    def get_access_token(self) -> str:
        """Return a currently-valid access token, refreshing if necessary.

        Raises:
            NotAuthenticatedError: if no tokens are stored.
            ReauthRequiredError: if the refresh token has expired/been rejected.
        """
        with self._lock:
            tokens = self._tokens or self._store.load()
            if tokens is None:
                raise NotAuthenticatedError(
                    "Not authenticated. Run 'python -m schwab_trader auth login'."
                )
            if not tokens.is_access_expired():
                self._tokens = tokens
                return tokens.access_token.get_secret_value()
            if tokens.is_refresh_expired():
                raise ReauthRequiredError(
                    "The 7-day refresh window has expired. "
                    "Run 'python -m schwab_trader auth login' to reauthorize."
                )
            refreshed = refresh_tokens(self._settings, tokens)
            self._store.save(refreshed)
            self._tokens = refreshed
            return refreshed.access_token.get_secret_value()
