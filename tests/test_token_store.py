"""Tests for the token model and atomic, permission-restricted storage."""

from __future__ import annotations

import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from schwab_trader.token_store import (
    REFRESH_TOKEN_LIFETIME,
    TokenSet,
    TokenStore,
    TokenStoreError,
)

TOKEN_RESPONSE = {
    "access_token": "ACCESS-abc123",
    "refresh_token": "REFRESH-xyz789",
    "token_type": "Bearer",
    "scope": "api",
    "id_token": "IDTOKEN-000",
    "expires_in": 1800,
}


def _tokens() -> TokenSet:
    return TokenSet.from_token_response(TOKEN_RESPONSE)


def test_from_token_response_computes_expiry() -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
    tokens = TokenSet.from_token_response(TOKEN_RESPONSE, obtained_at=now)
    assert tokens.expires_at == now + timedelta(seconds=1800)
    assert tokens.refresh_token_expires_at == now + REFRESH_TOKEN_LIFETIME


def test_from_token_response_requires_refresh_token() -> None:
    with pytest.raises(TokenStoreError):
        TokenSet.from_token_response({"access_token": "a", "expires_in": 10})


def test_access_expiry_uses_leeway() -> None:
    soon = datetime.now(UTC) + timedelta(seconds=30)
    tokens = _tokens().model_copy(update={"expires_at": soon})
    # Within the default 60s leeway -> treated as expired.
    assert tokens.is_access_expired() is True
    # With no leeway it is still valid for another ~30s.
    assert tokens.is_access_expired(timedelta(0)) is False


def test_repr_and_dump_do_not_leak_secrets() -> None:
    tokens = _tokens()
    assert "ACCESS-abc123" not in repr(tokens)
    assert "REFRESH-xyz789" not in repr(tokens)
    assert "ACCESS-abc123" not in tokens.model_dump_json()


def test_storage_roundtrip_preserves_secret_values(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens.json")
    assert store.load() is None  # missing file

    original = _tokens()
    store.save(original)
    assert store.exists()

    loaded = store.load()
    assert loaded is not None
    assert loaded.access_token.get_secret_value() == "ACCESS-abc123"
    assert loaded.refresh_token.get_secret_value() == "REFRESH-xyz789"
    assert loaded.id_token is not None
    assert loaded.id_token.get_secret_value() == "IDTOKEN-000"
    assert loaded.expires_at == original.expires_at


def test_save_is_atomic_leaves_no_temp_files(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens.json")
    store.save(_tokens())
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "tokens.json"]
    assert leftovers == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permissions only")
def test_saved_file_is_owner_only(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens.json")
    store.save(_tokens())
    mode = stat.S_IMODE((tmp_path / "tokens.json").stat().st_mode)
    assert mode == 0o600


def test_load_raises_on_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    path.write_text("{ not valid json", encoding="utf-8")
    store = TokenStore(path)
    with pytest.raises(TokenStoreError):
        store.load()


def test_delete_removes_file(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens.json")
    store.save(_tokens())
    store.delete()
    assert not store.exists()
    store.delete()  # idempotent, no error
