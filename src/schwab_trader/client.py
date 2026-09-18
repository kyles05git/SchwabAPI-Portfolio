"""Shared authenticated HTTP client and centralized API paths.

`SchwabClient` injects a bearer token (via a token provider that refreshes as
needed), enforces a conservative request-rate limit, applies finite timeouts,
parses errors into a typed :class:`ApiError` without leaking secrets, and retries
only *read-only* GETs on transient failures (network errors, HTTP 429, and
eligible 5xx). Order submissions (POST) are never automatically retried.
"""

from __future__ import annotations

import contextlib
import random
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Any, Protocol

import httpx

if TYPE_CHECKING:
    from schwab_trader.config import Settings

_trust_store_enabled = False


def enable_os_trust_store() -> None:
    """Use the OS certificate store for TLS verification (idempotent).

    Delegates certificate verification to the platform's native trust store
    (e.g. Windows SChannel) via ``truststore``. This is required on machines
    behind a TLS-inspecting proxy whose root CA is installed in the OS store but
    is absent from httpx's bundled CA list. It does not weaken TLS - verification
    still happens, against the OS-trusted roots.
    """
    global _trust_store_enabled
    if _trust_store_enabled:
        return
    with contextlib.suppress(Exception):
        import truststore

        truststore.inject_into_ssl()
    _trust_store_enabled = True


# Production API host (Accounts & Trading + Market Data share this host).
API_BASE_URL = "https://api.schwabapi.com"

TRADER_API = "/trader/v1"
MARKETDATA_API = "/marketdata/v1"

ACCOUNT_NUMBERS_PATH = f"{TRADER_API}/accounts/accountNumbers"
ACCOUNTS_PATH = f"{TRADER_API}/accounts"

# Finite connect/read/write/pool timeouts for every request.
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 8.0


class ApiError(Exception):
    """A non-success API response. Carries the HTTP status and a sanitized message."""

    def __init__(self, status_code: int, message: str, *, method: str = "", path: str = "") -> None:
        super().__init__(f"{method} {path} -> HTTP {status_code}: {message}".strip())
        self.status_code = status_code
        self.message = message


class TransportFailure(Exception):
    """A network-level failure where the request may or may not have been delivered.

    Used for non-idempotent operations (order submission) so callers can treat the
    outcome as *ambiguous* and never blindly retry.
    """


class TokenProvider(Protocol):
    """Anything that can supply a currently-valid bearer access token."""

    def get_access_token(self) -> str: ...


class RateLimiter:
    """A simple thread-safe sliding-window limiter (requests per minute)."""

    def __init__(self, max_per_minute: int, *, sleep: Any = time.sleep) -> None:
        self._capacity = max(1, max_per_minute)
        self._window = 60.0
        self._events: deque[float] = deque()
        self._lock = threading.Lock()
        self._sleep = sleep

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._evict(now)
            if len(self._events) >= self._capacity:
                sleep_for = self._window - (now - self._events[0])
                if sleep_for > 0:
                    self._sleep(sleep_for)
                self._evict(time.monotonic())
            self._events.append(time.monotonic())

    def _evict(self, now: float) -> None:
        while self._events and now - self._events[0] >= self._window:
            self._events.popleft()


def _backoff_seconds(attempt: int) -> float:
    base = min(_BACKOFF_BASE * (2.0 ** (attempt - 1)), _BACKOFF_CAP)
    return base + random.uniform(0.0, 0.25)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _sanitize_path(path: str) -> str:
    """Mask long hash-like path segments (e.g. the account hash) in error text."""
    segments = path.split("/")
    masked = [
        f"****{segment[-4:]}" if len(segment) >= 20 and segment.isalnum() else segment
        for segment in segments
    ]
    return "/".join(masked)


def _sanitize_message(response: httpx.Response) -> str:
    """Extract a short, non-sensitive error description from a response body."""
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(body, dict):
        for key in ("message", "error", "error_description", "fault"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
    return f"HTTP {response.status_code}"


class SchwabClient:
    """Authenticated HTTP client for the Schwab production API."""

    def __init__(
        self,
        settings: Settings,
        token_provider: TokenProvider,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self._settings = settings
        self._tokens = token_provider
        self._limiter = RateLimiter(settings.rate_limit_per_minute, sleep=sleep)
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=API_BASE_URL, timeout=DEFAULT_TIMEOUT, transport=transport
        )

    def __enter__(self) -> SchwabClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._tokens.get_access_token()}",
            "Accept": "application/json",
        }

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """Perform an authenticated GET and return the parsed JSON body."""
        response = self._request("GET", path, params=params, retry=True)
        return response.json()

    def request_once(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        """Perform a single authenticated request with no retries.

        For non-idempotent operations (e.g. order submission). Raises
        :class:`TransportFailure` on a network error (the request may or may not
        have been delivered) and :class:`ApiError` on any 4xx/5xx response.
        """
        self._limiter.acquire()
        try:
            response = self._http.request(
                method, path, params=params, json=json, headers=self._headers()
            )
        except httpx.TransportError as exc:
            raise TransportFailure(f"network error ({type(exc).__name__})") from exc
        if response.status_code >= 400:
            raise ApiError(
                response.status_code,
                _sanitize_message(response),
                method=method,
                path=_sanitize_path(path),
            )
        return response

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        retry: bool,
    ) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            self._limiter.acquire()
            try:
                response = self._http.request(
                    method, path, params=params, json=json, headers=self._headers()
                )
            except httpx.TransportError as exc:
                if retry and attempt < _MAX_ATTEMPTS:
                    self._sleep(_backoff_seconds(attempt))
                    continue
                raise ApiError(
                    0,
                    f"network error ({type(exc).__name__})",
                    method=method,
                    path=_sanitize_path(path),
                ) from exc

            if response.status_code in _RETRYABLE_STATUS and retry and attempt < _MAX_ATTEMPTS:
                self._sleep(_retry_after_seconds(response) or _backoff_seconds(attempt))
                continue

            if response.status_code >= 400:
                raise ApiError(
                    response.status_code,
                    _sanitize_message(response),
                    method=method,
                    path=_sanitize_path(path),
                )
            return response
