"""Regression tests proving offline storage isolation from a shared/remote database.

These lock in the fix for the incident where running ``pytest`` with a real ``.env``
present caused bootstrap tests to write to the production Neon database. They prove a
PostgreSQL URL in either the host environment or a dotenv file cannot make offline
tests use PostgreSQL, that bootstrap writes stay under ``tmp_path``, that no network
socket is opened, and that ``get_settings`` caching cannot leak configuration.
"""

from __future__ import annotations

import importlib.util
import os
import socket
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

import schwab_trader.storage.factory as storage_factory
from schwab_trader import data_contracts, data_readiness, universes
from schwab_trader.config import Settings, get_settings
from schwab_trader.sleeve_runs import SnapshotCoverage
from schwab_trader.sleeves import SleeveStore

_PG_URL = "postgresql+psycopg://user:pw@ep-fake.neon.tech/neondb?sslmode=require"

# Import the bootstrap script module the same way the bootstrap suite does.
_SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "bootstrap_paper_cohort.py"
_SPEC = importlib.util.spec_from_file_location("bootstrap_isolation_script", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
bootstrap = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = bootstrap
_SPEC.loader.exec_module(bootstrap)

_START = date(2026, 7, 24)
_NOW = datetime(2026, 7, 22, 16, 0, tzinfo=UTC)
_NOW_ET = datetime(2026, 7, 22, 12, 0)


def _offline_plan(tmp_path: Path) -> object:
    large_cap = tuple(universes.get_preset("large-cap") or ())
    probe = data_readiness.SourceProbe.of(
        SnapshotCoverage(
            provenance=data_contracts.Provenance(
                source="fake-edgar",
                snapshot_id="fake-edgar:ready",
                retrieved_at=_NOW,
                as_of=_NOW,
                available_at=_NOW,
                timing=data_contracts.TimingPolicy.POINT_IN_TIME,
                vintage_safe=True,
            ),
            keys=frozenset(large_cap),
        )
    )
    return bootstrap.build_bootstrap_plan(
        start_session=_START,
        sec_db_path=tmp_path / "sec.sqlite3",
        cohort_id="isolation-cohort",
        now=_NOW,
        now_et=_NOW_ET,
        edgar_probe=probe,
    )


def test_dotenv_database_url_cannot_select_postgres(tmp_path: Path) -> None:
    """A ``.env`` in the working directory must not select a shared database."""
    (tmp_path / ".env").write_text(f"SCHWAB_DATABASE_URL={_PG_URL}\n", encoding="utf-8")
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        get_settings.cache_clear()
        settings = get_settings()
    finally:
        os.chdir(cwd)
    assert settings.database_url.get_secret_value().strip() == ""
    assert settings.has_shared_database is False


def test_host_env_database_url_is_caught_by_fail_fast_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host ``SCHWAB_DATABASE_URL`` cannot silently open a non-SQLite DB."""
    monkeypatch.setenv("SCHWAB_DATABASE_URL", _PG_URL)
    get_settings.cache_clear()
    settings = get_settings()
    # The env var is read, but the factory must refuse to open it.
    assert settings.has_shared_database is True
    with pytest.raises(RuntimeError, match="isolation breach"):
        storage_factory.database(settings)


def test_bootstrap_writes_only_under_tmp_path(tmp_path: Path) -> None:
    """Bootstrap uses the local SQLite sleeves_dir, never a shared database."""
    sleeves_dir = tmp_path / "sleeves"
    plan = _offline_plan(tmp_path)

    result = bootstrap.apply_bootstrap(plan, sleeves_dir=sleeves_dir)

    assert get_settings().has_shared_database is False
    assert result.created  # sleeves were created
    assert SleeveStore(sleeves_dir).list()  # ...and they live under tmp_path
    assert (sleeves_dir).exists()
    # The registry SQLite must be under tmp_path, not the repo's data/ directory.
    assert any(sleeves_dir.rglob("*.sqlite3"))


def test_no_network_socket_is_opened_during_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bootstrap must complete without attempting any outbound connection."""
    attempts: list[object] = []

    def _forbid(self: socket.socket, *args: object, **kwargs: object) -> None:
        attempts.append(args)
        raise AssertionError("network connection attempted during offline bootstrap")

    monkeypatch.setattr(socket.socket, "connect", _forbid)
    monkeypatch.setattr(socket.socket, "connect_ex", _forbid)

    bootstrap.apply_bootstrap(_offline_plan(tmp_path), sleeves_dir=tmp_path / "sleeves")
    assert attempts == []


def test_get_settings_cache_does_not_leak_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cached shared-DB configuration must not survive into a clean state."""
    monkeypatch.setenv("SCHWAB_DATABASE_URL", _PG_URL)
    get_settings.cache_clear()
    assert get_settings().has_shared_database is True

    monkeypatch.delenv("SCHWAB_DATABASE_URL", raising=False)
    get_settings.cache_clear()
    assert get_settings().has_shared_database is False


def test_shared_database_is_opt_in_sqlite_by_default() -> None:
    """With no configured URL, the factory yields a local SQLite-backed store."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.has_shared_database is False
    assert storage_factory.database(settings) is None
    assert isinstance(storage_factory.sleeve_store(settings), SleeveStore)
