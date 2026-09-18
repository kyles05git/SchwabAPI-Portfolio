"""Secure, atomic token storage and the persisted token model.

The :class:`TokenSet` holds the OAuth tokens (wrapped in
:class:`~pydantic.SecretStr` so they never appear in ``repr``/logs) plus computed
expiry timestamps. :class:`TokenStore` persists them with an atomic
write-temp-then-replace strategy and restrictive file permissions.

Notes on expiry:

- Access tokens are short-lived (~30 minutes).
- Schwab refresh tokens have a hard 7-day limit from the *initial* authorization
  that cannot be extended by refreshing. We track that as a best-effort local
  estimate so we can warn before it lapses; refreshing preserves the original
  window rather than resetting it.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, SecretStr

REFRESH_TOKEN_LIFETIME = timedelta(days=7)
_ACCESS_LEEWAY = timedelta(seconds=60)


class TokenStoreError(Exception):
    """Raised when the token file cannot be read or parsed."""


class TokenSet(BaseModel):
    """OAuth tokens plus computed expiry metadata.

    Secret values are wrapped so they are not rendered by ``repr`` or logging.
    Persist with :meth:`to_storage_dict` / :meth:`from_storage_dict`, which
    intentionally expose the raw values only for writing the local token file.
    """

    access_token: SecretStr
    refresh_token: SecretStr
    token_type: str = "Bearer"
    scope: str = ""
    id_token: SecretStr | None = None
    expires_at: datetime
    refresh_token_expires_at: datetime | None = None
    obtained_at: datetime

    def is_access_expired(self, leeway: timedelta = _ACCESS_LEEWAY) -> bool:
        """True if the access token is expired (or within ``leeway`` of expiry)."""
        return datetime.now(UTC) >= (self.expires_at - leeway)

    def is_refresh_expired(self) -> bool:
        """True if the refresh token's 7-day window has (estimated) lapsed."""
        if self.refresh_token_expires_at is None:
            return False
        return datetime.now(UTC) >= self.refresh_token_expires_at

    @classmethod
    def from_token_response(
        cls,
        data: dict[str, Any],
        *,
        obtained_at: datetime | None = None,
        refresh_token_fallback: str | None = None,
        refresh_token_expires_at: datetime | None = None,
    ) -> TokenSet:
        """Build a :class:`TokenSet` from a Schwab token-endpoint response.

        For a refresh, pass ``refresh_token_fallback`` (used if the response
        omits a new refresh token) and ``refresh_token_expires_at`` (to preserve
        the original 7-day window rather than resetting it).
        """
        obtained_at = obtained_at or datetime.now(UTC)
        expires_in = int(data.get("expires_in", 1800))

        refresh_value = data.get("refresh_token") or refresh_token_fallback
        if not refresh_value:
            msg = "Token response did not include a refresh token."
            raise TokenStoreError(msg)

        access_value = data.get("access_token")
        if not access_value:
            msg = "Token response did not include an access token."
            raise TokenStoreError(msg)

        if refresh_token_expires_at is None:
            refresh_token_expires_at = obtained_at + REFRESH_TOKEN_LIFETIME

        id_token = data.get("id_token")
        return cls(
            access_token=SecretStr(access_value),
            refresh_token=SecretStr(refresh_value),
            token_type=data.get("token_type", "Bearer"),
            scope=data.get("scope", ""),
            id_token=SecretStr(id_token) if id_token else None,
            expires_at=obtained_at + timedelta(seconds=expires_in),
            refresh_token_expires_at=refresh_token_expires_at,
            obtained_at=obtained_at,
        )

    def to_storage_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict, exposing raw secrets for local storage."""
        return {
            "access_token": self.access_token.get_secret_value(),
            "refresh_token": self.refresh_token.get_secret_value(),
            "token_type": self.token_type,
            "scope": self.scope,
            "id_token": self.id_token.get_secret_value() if self.id_token else None,
            "expires_at": self.expires_at.isoformat(),
            "refresh_token_expires_at": (
                self.refresh_token_expires_at.isoformat() if self.refresh_token_expires_at else None
            ),
            "obtained_at": self.obtained_at.isoformat(),
        }

    @classmethod
    def from_storage_dict(cls, data: dict[str, Any]) -> TokenSet:
        """Inverse of :meth:`to_storage_dict`."""
        raw_id = data.get("id_token")
        raw_refresh_exp = data.get("refresh_token_expires_at")
        return cls(
            access_token=SecretStr(data["access_token"]),
            refresh_token=SecretStr(data["refresh_token"]),
            token_type=data.get("token_type", "Bearer"),
            scope=data.get("scope", ""),
            id_token=SecretStr(raw_id) if raw_id else None,
            expires_at=datetime.fromisoformat(data["expires_at"]),
            refresh_token_expires_at=(
                datetime.fromisoformat(raw_refresh_exp) if raw_refresh_exp else None
            ),
            obtained_at=datetime.fromisoformat(data["obtained_at"]),
        )


class TokenStore:
    """Reads and writes the local token file atomically with 0600 permissions."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> TokenSet | None:
        """Load tokens, or ``None`` if the file does not exist.

        Raises:
            TokenStoreError: if the file exists but cannot be read/parsed.
        """
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            msg = "Token file exists but could not be read; run 'auth login' to recreate it."
            raise TokenStoreError(msg) from exc
        try:
            return TokenSet.from_storage_dict(data)
        except (KeyError, ValueError) as exc:
            msg = "Token file is malformed; run 'auth login' to recreate it."
            raise TokenStoreError(msg) from exc

    def save(self, tokens: TokenSet) -> None:
        """Atomically write tokens: temp file -> flush/fsync -> chmod 600 -> replace."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(tokens.to_storage_dict(), indent=2)

        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix=".tokens-", suffix=".tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            _restrict_permissions(tmp_path)
            os.replace(tmp_path, self.path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()
        # Re-assert permissions on the final path (best effort).
        _restrict_permissions(self.path)

    def delete(self) -> None:
        """Remove the token file if present."""
        self.path.unlink(missing_ok=True)


def _restrict_permissions(path: Path) -> None:
    """Restrict a file to the owner (0600). Best-effort on non-POSIX platforms."""
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - platform dependent
        if sys.platform != "win32":
            raise
