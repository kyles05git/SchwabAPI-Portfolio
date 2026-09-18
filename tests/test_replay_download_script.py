"""``scripts/replay_download.py`` must default to a plan, never to a download.

The overnight-safety requirement for issue #80 is that no acquisition command can start
without an operator deliberately asking for it. These tests pin that: the default and
the read paths never build a client, never open a socket, and never write; ``download``
refuses without the exact typed phrase.

Offline and hermetic: SQLite under ``tmp_path``, no ``.env``, no token, no socket.
"""

from __future__ import annotations

import importlib.util
import json
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from schwab_trader.config import Settings, get_settings

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_replay_download", REPO_ROOT / "scripts" / "replay_download.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The script, pointed at a throwaway local replay database."""
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        token_path=tmp_path / "tokens.json",
        state_db_path=tmp_path / "state.sqlite3",
        log_path=tmp_path / "logs" / "app.log",
        historical_replay_db_path=tmp_path / "replay.sqlite3",
    )
    module = _load_script()
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    return module


@pytest.fixture
def lines() -> list[str]:
    return []


def _out(lines: list[str]):
    def write(text: str) -> None:
        lines.append(text)

    return write


def _never(*args: object, **kwargs: object) -> str:
    raise AssertionError("this path must not prompt or connect")


# --- defaults ---------------------------------------------------------------------


def test_no_subcommand_means_preflight(script: Any, lines: list[str]) -> None:
    code = script.main(
        ["--symbols", "AAPL,MSFT", "--sessions", "3", "--through", "2026-07-30"],
        out=_out(lines),
        prompt=_never,
    )

    assert code == script.EXIT_OK
    payload = json.loads(lines[0])
    assert payload["mode"] == "preflight"
    assert payload["session_count"] == 3
    assert payload["request_count"] == 6
    assert payload["universe_symbols"] == ["AAPL", "MSFT"]


def test_preflight_never_opens_a_socket_or_writes_a_database(
    script: Any, lines: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("preflight must not open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    assert script.main(["--symbols", "AAPL"], out=_out(lines), prompt=_never) == script.EXIT_OK
    assert not (tmp_path / "replay.sqlite3").exists()


def test_the_planned_requests_are_the_documented_shape(script: Any, lines: list[str]) -> None:
    script.main(
        ["preflight", "--symbols", "AAPL", "--sessions", "1", "--through", "2026-07-30"],
        out=_out(lines),
        prompt=_never,
    )
    request = json.loads(lines[0])["requests"][0]

    assert request["request_params"]["needExtendedHoursData"] == "false"
    assert request["request_params"]["frequency"] == 5
    assert "startDate" in request["request_params"]
    assert "endDate" in request["request_params"]
    assert "periodType" not in request["request_params"]
    assert "period" not in request["request_params"]


def test_the_default_plan_depth_is_thirty_sessions(script: Any, lines: list[str]) -> None:
    script.main(
        ["--symbols", "AAPL", "--through", "2026-07-30"], out=_out(lines), prompt=_never
    )

    assert json.loads(lines[0])["session_count"] == 30


def test_a_nonpositive_session_count_is_refused(script: Any, lines: list[str]) -> None:
    code = script.main(["--symbols", "AAPL", "--sessions", "0"], out=_out(lines), prompt=_never)

    assert code == script.EXIT_USAGE
    assert "must be positive" in lines[0]


# --- download gating --------------------------------------------------------------


def test_download_without_the_exact_phrase_writes_nothing(
    script: Any, lines: list[str], tmp_path: Path
) -> None:
    code = script.main(
        ["download", "--symbols", "AAPL", "--sessions", "2", "--through", "2026-07-30"],
        out=_out(lines),
        prompt=lambda _: "yes",
    )

    assert code == script.EXIT_ABORTED
    assert any("Aborted" in line for line in lines)
    assert not (tmp_path / "replay.sqlite3").exists()


@pytest.mark.parametrize(
    "typed",
    ["", "y", "download replay research data", " DOWNLOAD REPLAY  RESEARCH DATA "],
)
def test_near_misses_are_still_refused(
    script: Any, lines: list[str], tmp_path: Path, typed: str
) -> None:
    code = script.main(
        ["download", "--symbols", "AAPL", "--sessions", "1", "--through", "2026-07-30"],
        out=_out(lines),
        prompt=lambda _: typed,
    )

    assert code == script.EXIT_ABORTED
    assert not (tmp_path / "replay.sqlite3").exists()


def test_download_shows_the_full_plan_before_asking(script: Any, lines: list[str]) -> None:
    script.main(
        ["download", "--symbols", "AAPL,MSFT", "--sessions", "2", "--through", "2026-07-30"],
        out=_out(lines),
        prompt=lambda _: "no",
    )

    plan = json.loads(lines[0])
    assert plan["mode"] == "preflight"
    assert plan["request_count"] == 4
    assert "4 Schwab price-history requests" in lines[1]
    assert script.CONFIRMATION_PHRASE in lines[1]


def test_a_confirmed_download_is_the_only_path_that_builds_a_client(
    script: Any, lines: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Confirmed here with a stub, so no real client, token, or request is involved."""
    built: list[str] = []

    class _StubClient:
        def __enter__(self) -> _StubClient:
            built.append("entered")
            return self

        def __exit__(self, *exc: object) -> None:
            built.append("exited")

    monkeypatch.setattr(script, "_authenticated_client", lambda: _StubClient())
    monkeypatch.setattr(
        script.historical_replay_acquire,
        "schwab_session_fetcher",
        lambda client: (lambda symbol, session: []),
    )

    code = script.main(
        ["download", "--symbols", "AAPL", "--sessions", "1", "--through", "2026-07-30"],
        out=_out(lines),
        prompt=lambda _: script.CONFIRMATION_PHRASE,
    )

    assert code == script.EXIT_OK
    assert built == ["entered", "exited"]
    report = json.loads(lines[-1])
    assert report["mode"] == "download"
    # An empty provider payload is an honest "incomplete", never a silent success.
    assert report["totals"]["complete"] == 0
    assert report["totals"]["incomplete"] == 1


def test_preflight_and_report_never_build_a_client(
    script: Any, lines: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(script, "_authenticated_client", _never)

    assert script.main(["--symbols", "AAPL", "--sessions", "1"], out=_out(lines)) == script.EXIT_OK
    assert (
        script.main(["report", "--symbols", "AAPL", "--sessions", "1"], out=_out(lines))
        == script.EXIT_OK
    )


def test_report_states_what_is_missing_without_downloading(
    script: Any, lines: list[str]
) -> None:
    """It opens (and, locally, creates) the research database but fetches nothing."""
    code = script.main(
        ["report", "--symbols", "AAPL", "--sessions", "2", "--through", "2026-07-30"],
        out=_out(lines),
        prompt=_never,
    )

    assert code == script.EXIT_OK
    payload = json.loads(lines[0])
    assert payload["mode"] == "report"
    assert payload["status_counts"] == {}
    assert all(row["status"] is None for row in payload["stored"])
    assert all(row["revisions"] == 0 for row in payload["stored"])


def test_the_through_date_includes_its_own_completed_session(script: Any) -> None:
    planned = script._through("2026-07-30")

    assert planned == datetime(2026, 7, 30, 23, 59, 59, tzinfo=UTC)


def test_output_carries_no_credential_or_connection_detail(
    script: Any, lines: list[str]
) -> None:
    script.main(["--symbols", "AAPL", "--sessions", "2"], out=_out(lines), prompt=_never)
    text = "\n".join(lines).lower()

    for forbidden in ("token", "bearer", "authorization", "password", "sqlite:", "postgresql://"):
        assert forbidden not in text, forbidden


def test_the_script_module_reads_no_settings_at_import_time() -> None:
    """Importing it must not touch ``.env`` or cached settings."""
    get_settings.cache_clear()
    _load_script()

    assert get_settings.cache_info().currsize == 0
