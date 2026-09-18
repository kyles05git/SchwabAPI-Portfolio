"""The superseded July 27 cohort is archived, not deleted, and never the default.

`paper-first-2026-07-27` failed its shakedown on session 1 and was replaced by
`paper-first-2026-07-28`. These tests pin three separate guarantees:

* **Default selection.** With both cohorts persisted, the dashboard and its API land on
  the active cohort — even though the superseded id sorts first, which is exactly how it
  used to win.
* **Historical visibility.** The superseded cohort is still listed, still selectable by
  URL, and still renders its full record when asked for. Archiving is not hiding.
* **Immutability.** None of the above writes anything. Reading, defaulting, and refusing
  to run leave every stored row byte-identical.

Offline and deterministic: local SQLite under ``tmp_path``, no ``.env``, no network, no
broker, and an explicit Eastern instant instead of the wall clock.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from schwab_trader import cohort_lifecycle, dashboard, sleeves, strategy_registry
from schwab_trader.config import Settings

HISTORICAL = "paper-first-2026-07-27"
ACTIVE = "paper-first-2026-07-28"
MEMBERS = ("bench-spy", "control-cash")

# Pinned so phase assessment never depends on when the suite happens to run.
NOW_ET = datetime(2026, 8, 12, 9, 28)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        token_path=tmp_path / "tokens.json",
        state_db_path=tmp_path / "state.sqlite3",
        log_path=tmp_path / "logs" / "app.log",
        sleeves_dir=tmp_path / "sleeves",
        kill_switch_path=tmp_path / "KILL_SWITCH",
        agent_activity_db_path=tmp_path / "agent_activity.sqlite3",
        promotion_db_path=tmp_path / "promotion.sqlite3",
        approval_db_path=tmp_path / "approvals.sqlite3",
        tax_lots_db_path=tmp_path / "taxlots.sqlite3",
    )


def _create_cohort(store: sleeves.SleeveStore, cohort_id: str) -> None:
    """Two members per cohort, with the same display names in both — as in reality."""
    for name in MEMBERS:
        symbol = "SPY" if name == "bench-spy" else "CASH"
        store.create(
            f"{name}-{cohort_id[-2:]}",
            strategy="buy-hold",
            universe=[symbol],
            starting_cash=Decimal("10000.00"),
            max_positions=1,
            max_position_fraction=Decimal("1"),
            definition=strategy_registry.make_definition(
                "buy-hold",
                universe_definition=[symbol],
                benchmark_symbol_or_sleeve="bench-spy",
            ),
            cohort_id=cohort_id,
        )


@pytest.fixture
def both_cohorts(tmp_path: Path) -> Settings:
    """Both cohorts persisted, the superseded one created first."""
    settings = _settings(tmp_path)
    store = sleeves.SleeveStore(settings.sleeves_dir)
    _create_cohort(store, HISTORICAL)
    _create_cohort(store, ACTIVE)
    return settings


@contextmanager
def _running_server(settings: Settings, tmp_path: Path) -> Iterator[int]:
    server = dashboard.make_server(
        settings,
        host="127.0.0.1",
        port=0,
        client_factory=None,
        frontend_dir=tmp_path / "no-dist",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _api(settings: Settings, tmp_path: Path, path: str) -> dict[str, object]:
    with _running_server(settings, tmp_path) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read().decode("utf-8")
        conn.close()
    assert response.status == 200, body
    return json.loads(body)


def _registry_fingerprint(settings: Settings) -> str:
    """Every persisted sleeve record, serialized. Any write at all changes this."""
    configs = sorted(sleeves.SleeveStore(settings.sleeves_dir).list(), key=lambda c: c.identity)
    payload = json.dumps(
        [config.model_dump(mode="json") for config in configs], sort_keys=True, default=str
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- The lifecycle registry itself ---------------------------------------------


def test_the_incident_cohort_is_recorded_as_superseded() -> None:
    status = cohort_lifecycle.status_for(HISTORICAL)

    assert status.lifecycle is cohort_lifecycle.CohortLifecycle.SUPERSEDED
    assert status.label == "Superseded — partial incident, do not run"
    assert status.superseded_by == ACTIVE
    assert status.historical is True
    assert status.runnable is False
    assert "partial" in status.reason


def test_the_replacement_cohort_is_active() -> None:
    status = cohort_lifecycle.status_for(ACTIVE)

    assert status.lifecycle is cohort_lifecycle.CohortLifecycle.ACTIVE
    assert status.historical is False
    assert status.runnable is True
    assert cohort_lifecycle.run_refusal(ACTIVE) is None


def test_an_unregistered_cohort_is_active_by_default() -> None:
    """The registry records the exception. A cohort is not retired by being forgotten."""
    assert cohort_lifecycle.is_historical("paper-third-2027-01-04") is False
    assert cohort_lifecycle.is_historical("") is False


def test_the_default_is_drawn_from_active_cohorts_only() -> None:
    ordered = sorted([HISTORICAL, ACTIVE])
    # The superseded id sorts first, which is precisely how it used to be chosen.
    assert ordered[0] == HISTORICAL
    resolution = cohort_lifecycle.resolve_default_cohort(
        cohort_lifecycle.CohortRecency(cohort_id) for cohort_id in ordered
    )
    # One cohort is active, so no ordering metadata is needed to be sure of it.
    assert resolution.selected == ACTIVE
    assert resolution.multiple_active is False
    assert resolution.ambiguous is False


def test_there_is_no_default_when_every_cohort_is_historical() -> None:
    """A withdrawn experiment is never a fallback, even when it is the only record."""
    resolution = cohort_lifecycle.resolve_default_cohort(
        [cohort_lifecycle.CohortRecency(HISTORICAL)]
    )

    assert resolution.selected is None
    assert resolution.active == ()
    # Nothing is collecting, so there is no ambiguity to report — just an absence.
    assert resolution.ambiguous is False


def test_the_run_refusal_names_the_replacement_and_the_incident_record() -> None:
    refusal = cohort_lifecycle.run_refusal(HISTORICAL)

    assert refusal is not None
    assert HISTORICAL in refusal and ACTIVE in refusal
    assert "must not be run or scheduled" in refusal
    assert "docs/incidents/2026-07-27" in refusal


# --- Default selection ----------------------------------------------------------


def test_the_dashboard_defaults_to_the_active_cohort(both_cohorts: Settings) -> None:
    view = dashboard.collect_cohort_dashboard(
        both_cohorts, requested_cohort=None, benchmark="bench-spy", now_et=NOW_ET
    )

    assert view.selection.selected == ACTIVE
    assert view.selection.selected_is_historical is False
    assert view.identity is not None and view.identity.cohort_id == ACTIVE
    assert view.available is True


def test_the_api_defaults_to_the_active_cohort(
    both_cohorts: Settings, tmp_path: Path
) -> None:
    payload = _api(both_cohorts, tmp_path, "/api/data")
    selection = payload["cohort"]["selection"]  # type: ignore[index]

    assert selection["selected"] == ACTIVE
    assert selection["requested"] is None
    assert selection["active"] == [ACTIVE]


def test_the_active_cohort_is_the_only_one_the_picker_offers(
    both_cohorts: Settings,
) -> None:
    selection = dashboard.collect_cohort_dashboard(
        both_cohorts, requested_cohort=None, benchmark="bench-spy", now_et=NOW_ET
    ).selection

    assert selection.active == [ACTIVE]
    # ...while the full list keeps every cohort, so nothing disappears from audit.
    assert selection.available == sorted([HISTORICAL, ACTIVE])


# --- Historical visibility ------------------------------------------------------


def test_the_superseded_cohort_is_listed_with_its_label_and_replacement(
    both_cohorts: Settings,
) -> None:
    selection = dashboard.collect_cohort_dashboard(
        both_cohorts, requested_cohort=None, benchmark="bench-spy", now_et=NOW_ET
    ).selection

    assert [item.cohort_id for item in selection.historical] == [HISTORICAL]
    entry = selection.historical[0]
    assert entry.lifecycle == "superseded"
    assert entry.label == "Superseded — partial incident, do not run"
    assert entry.superseded_by == ACTIVE
    assert entry.reference.endswith(".md")


def test_an_explicit_request_for_the_superseded_cohort_is_honoured(
    both_cohorts: Settings,
) -> None:
    """Audit means rendering the record, not a placeholder that says it exists."""
    view = dashboard.collect_cohort_dashboard(
        both_cohorts, requested_cohort=HISTORICAL, benchmark="bench-spy", now_et=NOW_ET
    )

    assert view.available is True
    assert view.selection.selected == HISTORICAL
    assert view.selection.selected_is_historical is True
    assert view.identity is not None and view.identity.cohort_id == HISTORICAL
    # The full view is assembled, not a stub: members, phase, and gate are all present.
    assert len(view.identity.member_names) == len(MEMBERS)
    assert view.phase is not None
    assert view.sleeve_definitions


def test_the_url_query_preserves_a_historical_selection(
    both_cohorts: Settings, tmp_path: Path
) -> None:
    payload = _api(both_cohorts, tmp_path, f"/api/data?cohort={HISTORICAL}")
    selection = payload["cohort"]["selection"]  # type: ignore[index]

    assert selection["requested"] == HISTORICAL
    assert selection["selected"] == HISTORICAL
    assert selection["selected_is_historical"] is True


def test_when_only_a_historical_cohort_exists_it_is_named_but_not_selected(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _create_cohort(sleeves.SleeveStore(settings.sleeves_dir), HISTORICAL)

    view = dashboard.collect_cohort_dashboard(
        settings, requested_cohort=None, benchmark="bench-spy", now_et=NOW_ET
    )

    assert view.selection.selected is None
    assert view.available is False
    assert view.message is not None
    assert "No active paper cohort" in view.message
    assert HISTORICAL in view.message
    # Still reachable on request — the point of not selecting it is not to hide it.
    assert [item.cohort_id for item in view.selection.historical] == [HISTORICAL]
    explicit = dashboard.collect_cohort_dashboard(
        settings, requested_cohort=HISTORICAL, benchmark="bench-spy", now_et=NOW_ET
    )
    assert explicit.selection.selected == HISTORICAL


def test_an_unknown_cohort_is_still_reported_as_unavailable(
    both_cohorts: Settings,
) -> None:
    view = dashboard.collect_cohort_dashboard(
        both_cohorts, requested_cohort="paper-typo", benchmark="bench-spy", now_et=NOW_ET
    )

    assert view.selection.selected is None
    assert view.message == "Requested cohort 'paper-typo' is unavailable."


# --- Nothing stored ever moves --------------------------------------------------


def test_the_fingerprint_actually_detects_a_registry_write(both_cohorts: Settings) -> None:
    """Guard the guard: a fingerprint that cannot fail proves nothing below."""
    before = _registry_fingerprint(both_cohorts)
    sleeves.SleeveStore(both_cohorts.sleeves_dir).create(
        "late-arrival",
        strategy="buy-hold",
        universe=["SPY"],
        starting_cash=Decimal("10000.00"),
        max_positions=1,
        max_position_fraction=Decimal("1"),
        cohort_id=ACTIVE,
    )

    assert _registry_fingerprint(both_cohorts) != before


def test_selecting_defaulting_and_rendering_write_nothing(
    both_cohorts: Settings, tmp_path: Path
) -> None:
    """Archiving July 27 is a presentation decision, not a migration.

    The superseded state lives in a reviewed code constant precisely so that marking a
    cohort historical cannot touch the incident evidence. This drives every read path —
    default, explicit historical selection, and the HTTP API — and proves the stored
    records are byte-identical afterwards.
    """
    before = _registry_fingerprint(both_cohorts)

    for requested in (None, ACTIVE, HISTORICAL):
        dashboard.collect_cohort_dashboard(
            both_cohorts, requested_cohort=requested, benchmark="bench-spy", now_et=NOW_ET
        )
    dashboard.collect_sleeves(both_cohorts, "bench-spy")
    dashboard.benchmark_is_registered(both_cohorts, "bench-spy")
    _api(both_cohorts, tmp_path, "/api/data")
    _api(both_cohorts, tmp_path, f"/api/data?cohort={HISTORICAL}")

    assert _registry_fingerprint(both_cohorts) == before


def test_the_lifecycle_lookup_is_pure(both_cohorts: Settings) -> None:
    """No storage, no clock, no network — so it cannot alter what it describes."""
    before = _registry_fingerprint(both_cohorts)

    for cohort_id in (HISTORICAL, ACTIVE, "", "unknown"):
        cohort_lifecycle.status_for(cohort_id)
        cohort_lifecycle.is_historical(cohort_id)
        cohort_lifecycle.run_refusal(cohort_id)

    assert _registry_fingerprint(both_cohorts) == before
