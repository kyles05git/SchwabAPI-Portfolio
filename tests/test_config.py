"""Tests for configuration loading, defaults, and safe-by-default behavior."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from schwab_trader.config import Settings, set_env_value


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_safety_gates_default_closed() -> None:
    settings = _settings()
    assert settings.dry_run is True
    assert settings.trading_enabled is False
    assert settings.require_confirmation is True


def test_conservative_risk_defaults() -> None:
    settings = _settings()
    assert settings.max_order_quantity == 1
    assert settings.max_order_notional == Decimal("100.00")
    assert settings.rate_limit_per_minute == 100
    assert settings.promotion_max_age_days == 45


def test_validation_max_age_is_bounded() -> None:
    assert _settings(promotion_max_age_days=30).promotion_max_age_days == 30
    with pytest.raises(ValueError):
        _settings(promotion_max_age_days=0)
    with pytest.raises(ValueError):
        _settings(promotion_max_age_days=366)


def test_env_overrides_are_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCHWAB_DRY_RUN", "false")
    monkeypatch.setenv("SCHWAB_MAX_ORDER_QUANTITY", "5")
    settings = Settings()  # reads env, not the file overrides above
    assert settings.dry_run is False
    assert settings.max_order_quantity == 5


def test_allowed_symbol_set_parses_and_normalizes() -> None:
    settings = _settings(allowed_symbols=" aapl, msft ,, Nvda ")
    assert settings.allowed_symbol_set == frozenset({"AAPL", "MSFT", "NVDA"})


def test_empty_allowed_symbols_means_no_restriction() -> None:
    assert _settings().allowed_symbol_set == frozenset()


def test_rate_limit_capped_at_portal_headroom() -> None:
    with pytest.raises(ValueError):
        _settings(rate_limit_per_minute=200)


def test_callback_url_must_be_https() -> None:
    with pytest.raises(ValueError):
        _settings(callback_url="http://127.0.0.1:8182/callback")


def test_has_credentials_reflects_presence() -> None:
    assert _settings().has_credentials is False
    assert _settings(client_id="abc", client_secret="shh").has_credentials is True


def test_masked_account_tail_never_reveals_full_hash() -> None:
    settings = _settings(account_hash="ABCDEF1234567890")
    tail = settings.masked_account_tail()
    assert tail == "****7890"
    assert "ABCDEF1234567890" not in tail


def test_secrets_do_not_render_in_repr() -> None:
    settings = _settings(
        client_secret="topsecretvalue",
        account_hash="hashsecretvalue",
        database_url="postgresql+psycopg://user:database-secret@localhost/app",
    )
    text = repr(settings)
    assert "topsecretvalue" not in text
    assert "hashsecretvalue" not in text
    assert "database-secret" not in text


def test_database_url_defaults_to_local_sqlite_mode() -> None:
    assert _settings().has_shared_database is False


def test_remote_postgres_requires_tls_without_echoing_url() -> None:
    secret_url = "postgresql+psycopg://user:never-echo@db.example/app"
    with pytest.raises(ValueError) as exc_info:
        _settings(database_url=secret_url)
    assert "sslmode" in str(exc_info.value)
    assert secret_url not in str(exc_info.value)
    assert "never-echo" not in str(exc_info.value)


def test_remote_postgres_accepts_required_tls() -> None:
    settings = _settings(
        database_url=(
            "postgresql+psycopg://user:secret@db.example/app?sslmode=verify-full"
        )
    )
    assert settings.has_shared_database is True


def test_set_env_value_appends_new_key(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("SCHWAB_DRY_RUN=true\n", encoding="utf-8")
    set_env_value("SCHWAB_ACCOUNT_HASH", "abc123", env_path=env)
    text = env.read_text(encoding="utf-8")
    assert "SCHWAB_ACCOUNT_HASH=abc123" in text
    assert "SCHWAB_DRY_RUN=true" in text


def test_set_env_value_replaces_existing_key(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("SCHWAB_ACCOUNT_HASH=\nSCHWAB_DRY_RUN=true\n", encoding="utf-8")
    set_env_value("SCHWAB_ACCOUNT_HASH", "newhash", env_path=env)
    text = env.read_text(encoding="utf-8")
    assert "SCHWAB_ACCOUNT_HASH=newhash" in text
    assert text.count("SCHWAB_ACCOUNT_HASH=") == 1
    assert "SCHWAB_DRY_RUN=true" in text


def test_set_env_value_creates_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    set_env_value("SCHWAB_ACCOUNT_HASH", "h", env_path=env)
    assert env.exists()
    assert "SCHWAB_ACCOUNT_HASH=h" in env.read_text(encoding="utf-8")
