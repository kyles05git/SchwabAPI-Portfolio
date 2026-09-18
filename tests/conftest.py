"""Shared test fixtures.

All tests run without Schwab credentials and without network access. Isolation is
enforced globally by :func:`_isolate_offline_storage`: offline tests never read the
repository's real ``.env`` and can never open a non-SQLite application database, even
if a ``SCHWAB_DATABASE_URL`` is present in the host environment or in ``.env``.

The opt-in PostgreSQL integration suite uses ``SCHWAB_TEST_DATABASE_URL`` and builds
``Database`` directly (not through the storage factory), so it is unaffected by the
fail-fast guard here.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

import schwab_trader.storage.factory as storage_factory
from schwab_trader.config import Settings, get_settings
from schwab_trader.logging_config import reset_logging


@pytest.fixture(autouse=True)
def _isolate_offline_storage(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Guarantee offline tests never read the real ``.env`` or open a shared DB.

    Setting an empty ``SCHWAB_DATABASE_URL`` alone is insufficient: the autouse
    cleanup removes it and pydantic re-reads the value from ``.env``. This fixture
    instead (1) strips ambient ``SCHWAB_*`` variables, (2) blanks the dotenv source
    so ``.env`` is never read, (3) clears cached settings/backends so configuration
    cannot leak between tests, and (4) fails closed if any code path tries to open a
    non-SQLite application database.
    """
    # 1. Remove ambient SCHWAB_* variables (including a host SCHWAB_DATABASE_URL).
    for key in list(os.environ):
        if key.startswith("SCHWAB_"):
            monkeypatch.delenv(key, raising=False)
    # 2. Never read the developer's real .env during offline tests.
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    # 3. Drop cached settings/backends so isolation applies immediately and
    #    configuration cannot leak between tests.
    get_settings.cache_clear()
    storage_factory._shared_database.cache_clear()

    # 4. Fail closed: opening the application's shared database must never select a
    #    non-SQLite backend in an offline test, whatever the URL's source. Guarding
    #    ``database()`` (the single entry point every factory getter calls) leaves the
    #    ``_shared_database`` lru_cache API intact for tests that use SQLite backends.
    real_database = storage_factory.database

    def _guarded_database(settings: Settings) -> object:
        raw = settings.database_url.get_secret_value().strip()
        if raw and not raw.startswith("sqlite"):
            raise RuntimeError(
                "Storage isolation breach: an offline test attempted to open a "
                "non-SQLite application database via SCHWAB_DATABASE_URL. Tests must "
                "never connect to a shared or remote database."
            )
        return real_database(settings)

    monkeypatch.setattr(storage_factory, "database", _guarded_database)

    yield

    get_settings.cache_clear()


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Iterator[Settings]:
    """A Settings instance isolated to a temporary directory (no real .env)."""
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        token_path=tmp_path / "tokens.json",
        state_db_path=tmp_path / "state.sqlite3",
        log_path=tmp_path / "logs" / "schwab_trader.log",
    )
    yield settings
    reset_logging()
