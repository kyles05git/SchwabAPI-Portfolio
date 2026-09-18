"""Idempotent ingestion, correction history, and end-to-end acquisition reporting.

Offline and hermetic: every database is a throwaway SQLite file under ``tmp_path``,
every candle is synthetic, and the fetcher is injected. Nothing here opens a socket,
reads ``.env`` or a token, connects to Neon, runs Alembic against a user database, or
reaches a cohort, paper, notification, or order path.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from schwab_trader import historical_replay, historical_replay_acquire, market_calendar
from schwab_trader.historical_replay import ReplaySessionStatus
from schwab_trader.market_data import Candle
from schwab_trader.storage.database import Database
from schwab_trader.storage.historical_replay import (
    ReplayIngestOutcome,
    SqlAlchemyHistoricalReplayStore,
)

UNIVERSE = historical_replay.universe(["AAPL", "MSFT"], label="replay-test")
NORMAL = date(2026, 7, 29)
OTHER = date(2026, 7, 28)
START = datetime(2026, 7, 30, 21, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> SqlAlchemyHistoricalReplayStore:
    database = Database(f"sqlite:///{tmp_path / 'replay.sqlite3'}", create_schema=True)
    return SqlAlchemyHistoricalReplayStore(database)


def _synthetic(
    symbol: str,
    session: date,
    *,
    intervals: tuple[datetime, ...] | None = None,
    base: str = "100",
) -> list[Candle]:
    starts = intervals or market_calendar.session_interval_starts_utc(session, minutes=5)
    price = Decimal(base)
    return [
        Candle(
            symbol=symbol,
            date=moment,
            open=price + index,
            high=price + index + 1,
            low=price + index - 1,
            close=price + index,
            volume=1000 + index,
            source="schwab-regular-session-5m",
        )
        for index, moment in enumerate(starts)
    ]


def _evidence(symbol: str, session: date, candles: list[Candle], *, at: datetime = START):
    return historical_replay.validate_replay_session(
        UNIVERSE,
        symbol,
        session,
        candles,
        retrieved_at=at,
        source="schwab-regular-session-5m",
    )


class _Clock:
    """A deterministic monotonic clock, so retrieval times are never wall-clock."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


# --- ingestion ---------------------------------------------------------------------


def test_a_first_ingestion_records_revision_one(store: SqlAlchemyHistoricalReplayStore) -> None:
    store.register_universe(UNIVERSE, now=START)
    result = store.ingest(_evidence("AAPL", NORMAL, _synthetic("AAPL", NORMAL)), now=START)

    assert result.outcome is ReplayIngestOutcome.RECORDED
    assert result.revision == 1
    assert result.previous_replay_id is None
    assert result.status is ReplaySessionStatus.COMPLETE


def test_repeated_identical_ingestion_is_idempotent(
    store: SqlAlchemyHistoricalReplayStore,
) -> None:
    store.register_universe(UNIVERSE, now=START)
    candles = _synthetic("AAPL", NORMAL)
    first = store.ingest(_evidence("AAPL", NORMAL, candles), now=START)

    later = START + timedelta(hours=6)
    repeat = store.ingest(_evidence("AAPL", NORMAL, candles, at=later), now=later)

    assert repeat.outcome is ReplayIngestOutcome.UNCHANGED
    assert repeat.revision == first.revision == 1
    assert repeat.replay_id == first.replay_id
    assert len(store.history("AAPL", NORMAL)) == 1

    stored = store.get(first.replay_id)
    assert stored is not None
    assert len(stored.bars) == 78
    # The original retrieval timestamp is never rewritten by a later identical read.
    assert stored.retrieved_at == START


def test_reordered_data_is_the_same_observation(store: SqlAlchemyHistoricalReplayStore) -> None:
    store.register_universe(UNIVERSE, now=START)
    candles = _synthetic("AAPL", NORMAL)
    store.ingest(_evidence("AAPL", NORMAL, candles), now=START)
    reordered = store.ingest(_evidence("AAPL", NORMAL, list(reversed(candles))), now=START)

    assert reordered.outcome is ReplayIngestOutcome.UNCHANGED
    assert len(store.history("AAPL", NORMAL)) == 1


def test_a_provider_correction_appends_a_revision(
    store: SqlAlchemyHistoricalReplayStore,
) -> None:
    store.register_universe(UNIVERSE, now=START)
    original = _synthetic("AAPL", NORMAL)
    first = store.ingest(_evidence("AAPL", NORMAL, original), now=START)

    corrected = list(original)
    corrected[40] = corrected[40].model_copy(update={"close": Decimal("123.45")})
    later = START + timedelta(days=1)
    second = store.ingest(_evidence("AAPL", NORMAL, corrected, at=later), now=later)

    assert second.outcome is ReplayIngestOutcome.CORRECTED
    assert second.revision == 2
    assert second.previous_replay_id == first.replay_id
    assert second.replay_id != first.replay_id

    history = store.history("AAPL", NORMAL)
    assert [item.revision for item in history] == [1, 2]
    assert [item.outcome for item in history] == [
        ReplayIngestOutcome.RECORDED,
        ReplayIngestOutcome.CORRECTED,
    ]
    # Both versions stay readable: a correction is never an overwrite.
    assert store.get(first.replay_id) is not None
    assert store.latest("AAPL", NORMAL) is not None
    assert store.latest("AAPL", NORMAL).replay_id == second.replay_id  # type: ignore[union-attr]


def test_an_incomplete_session_later_completed_is_a_correction(
    store: SqlAlchemyHistoricalReplayStore,
) -> None:
    store.register_universe(UNIVERSE, now=START)
    starts = market_calendar.session_interval_starts_utc(NORMAL, minutes=5)
    partial = _synthetic("AAPL", NORMAL, intervals=starts[:-1])
    first = store.ingest(_evidence("AAPL", NORMAL, partial), now=START)
    assert first.status is ReplaySessionStatus.INCOMPLETE

    later = START + timedelta(days=1)
    full = store.ingest(_evidence("AAPL", NORMAL, _synthetic("AAPL", NORMAL), at=later), now=later)

    assert full.outcome is ReplayIngestOutcome.CORRECTED
    assert full.status is ReplaySessionStatus.COMPLETE


def test_a_reverted_payload_is_still_an_explicit_correction(
    store: SqlAlchemyHistoricalReplayStore,
) -> None:
    """A provider that flips back to earlier content must not violate the content PK."""
    store.register_universe(UNIVERSE, now=START)
    original = _synthetic("AAPL", NORMAL)
    changed = list(original)
    changed[40] = changed[40].model_copy(update={"close": Decimal("123.45")})

    first = store.ingest(_evidence("AAPL", NORMAL, original), now=START)
    store.ingest(_evidence("AAPL", NORMAL, changed), now=START + timedelta(days=1))
    reverted = store.ingest(_evidence("AAPL", NORMAL, original), now=START + timedelta(days=2))

    assert reverted.outcome is ReplayIngestOutcome.CORRECTED
    assert reverted.revision == 3
    assert reverted.replay_id == first.replay_id
    assert [item.revision for item in store.history("AAPL", NORMAL)] == [1, 2, 3]


def test_evidence_round_trips_with_its_validation_detail(
    store: SqlAlchemyHistoricalReplayStore,
) -> None:
    store.register_universe(UNIVERSE, now=START)
    starts = market_calendar.session_interval_starts_utc(NORMAL, minutes=5)
    gapped = _synthetic("AAPL", NORMAL, intervals=starts[:20] + starts[23:])
    evidence = _evidence("AAPL", NORMAL, gapped)
    store.ingest(evidence, now=START)

    stored = store.get(evidence.replay_id)
    assert stored is not None
    assert stored.status is ReplaySessionStatus.INCOMPLETE
    assert stored.missing_intervals == starts[20:23]
    assert stored.issues == evidence.issues
    assert stored.request_params == evidence.request_params
    assert stored.normalized_bar_digest == evidence.normalized_bar_digest
    assert stored.universe_symbols == ("AAPL", "MSFT")
    assert stored.bars == evidence.bars


def test_evidence_cannot_be_ingested_before_its_universe_is_registered(
    store: SqlAlchemyHistoricalReplayStore,
) -> None:
    with pytest.raises(ValueError, match="universe must be registered"):
        store.ingest(_evidence("AAPL", NORMAL, _synthetic("AAPL", NORMAL)), now=START)


def test_a_universe_whose_identity_does_not_match_its_symbols_is_refused(
    store: SqlAlchemyHistoricalReplayStore,
) -> None:
    forged = UNIVERSE.model_copy(update={"symbols": ("AAPL", "TSLA")})
    with pytest.raises(ValueError, match="universe identity"):
        store.register_universe(forged, now=START)


def test_registering_the_same_universe_twice_is_idempotent(
    store: SqlAlchemyHistoricalReplayStore,
) -> None:
    store.register_universe(UNIVERSE, now=START)
    store.register_universe(UNIVERSE, now=START + timedelta(days=1))

    restored = store.universe(UNIVERSE.universe_id)
    assert restored is not None
    assert restored.symbols == ("AAPL", "MSFT")


# --- acquisition ------------------------------------------------------------------


def _fetcher(payloads: dict[tuple[str, date], list[Candle] | Exception]):
    def fetch(symbol: str, session: date) -> list[Candle]:
        found = payloads.get((symbol, session), [])
        if isinstance(found, Exception):
            raise found
        return found

    return fetch


def test_acquisition_reports_each_bucket_clearly(
    store: SqlAlchemyHistoricalReplayStore,
) -> None:
    """Two symbols, two sessions: complete, incomplete, and unavailable side by side."""
    starts = market_calendar.session_interval_starts_utc(OTHER, minutes=5)
    payloads: dict[tuple[str, date], list[Candle] | Exception] = {
        ("AAPL", NORMAL): _synthetic("AAPL", NORMAL),
        ("MSFT", NORMAL): _synthetic("MSFT", NORMAL, base="200"),
        # Only MSFT is short a bar on the earlier session.
        ("AAPL", OTHER): _synthetic("AAPL", OTHER),
        ("MSFT", OTHER): _synthetic("MSFT", OTHER, intervals=starts[:-1], base="200"),
    }
    report = historical_replay_acquire.acquire(
        _fetcher(payloads),
        store,
        UNIVERSE,
        (OTHER, NORMAL),
        clock=_Clock(),
    )

    assert len(report.outcomes) == 4
    assert {(item.symbol, item.session_date) for item in report.complete} == {
        ("AAPL", NORMAL),
        ("MSFT", NORMAL),
        ("AAPL", OTHER),
    }
    assert [(item.symbol, item.session_date) for item in report.incomplete] == [("MSFT", OTHER)]
    assert report.unavailable == ()
    assert report.corrected == ()

    payload = historical_replay_acquire.report_payload(report)
    assert payload["totals"] == {
        "complete": 3,
        "incomplete": 1,
        "unavailable": 0,
        "corrected": 0,
        "unchanged": 0,
    }


def test_a_provider_failure_for_one_symbol_does_not_stop_the_run(
    store: SqlAlchemyHistoricalReplayStore,
) -> None:
    from schwab_trader import client as api

    payloads: dict[tuple[str, date], list[Candle] | Exception] = {
        ("AAPL", NORMAL): _synthetic("AAPL", NORMAL),
        ("MSFT", NORMAL): api.ApiError(503, "service unavailable", method="GET", path="/x"),
    }
    report = historical_replay_acquire.acquire(
        _fetcher(payloads),
        store,
        UNIVERSE,
        (NORMAL,),
        clock=_Clock(),
    )

    assert len(report.complete) == 1
    assert [item.symbol for item in report.unavailable] == ["MSFT"]
    failed = report.unavailable[0]
    assert failed.error == "ApiError:503"
    # The sanitized label carries the status and nothing else: no URL, no body.
    assert "/x" not in (failed.error or "")
    assert "service unavailable" not in (failed.error or "")


def test_a_transport_failure_is_recorded_without_its_message(
    store: SqlAlchemyHistoricalReplayStore,
) -> None:
    boom = RuntimeError("https://api.schwabapi.com/v1/x?token=SECRET")
    report = historical_replay_acquire.acquire(
        _fetcher({("AAPL", NORMAL): boom, ("MSFT", NORMAL): boom}),
        store,
        UNIVERSE,
        (NORMAL,),
        clock=_Clock(),
    )

    for item in report.outcomes:
        assert item.error == "RuntimeError"
        assert "schwabapi" not in (item.error or "")
        assert "SECRET" not in (item.error or "")


def test_rerunning_the_same_acquisition_writes_nothing_new(
    store: SqlAlchemyHistoricalReplayStore,
) -> None:
    payloads: dict[tuple[str, date], list[Candle] | Exception] = {
        ("AAPL", NORMAL): _synthetic("AAPL", NORMAL),
        ("MSFT", NORMAL): _synthetic("MSFT", NORMAL, base="200"),
    }
    first = historical_replay_acquire.acquire(
        _fetcher(payloads), store, UNIVERSE, (NORMAL,), clock=_Clock()
    )
    second = historical_replay_acquire.acquire(
        _fetcher(payloads), store, UNIVERSE, (NORMAL,), clock=_Clock(START + timedelta(days=1))
    )

    assert len(first.corrected) == 0
    assert len(second.unchanged) == 2
    assert len(second.corrected) == 0
    assert store.status_counts(UNIVERSE.universe_id) == {"complete": 2}
    for symbol in ("AAPL", "MSFT"):
        assert len(store.history(symbol, NORMAL)) == 1


def test_a_correction_on_a_later_ingestion_is_surfaced_in_the_report(
    store: SqlAlchemyHistoricalReplayStore,
) -> None:
    original = _synthetic("AAPL", NORMAL)
    revised = list(original)
    revised[3] = revised[3].model_copy(update={"volume": 999_999})
    unchanged = _synthetic("MSFT", NORMAL, base="200")

    historical_replay_acquire.acquire(
        _fetcher({("AAPL", NORMAL): original, ("MSFT", NORMAL): unchanged}),
        store,
        UNIVERSE,
        (NORMAL,),
        clock=_Clock(),
    )
    second = historical_replay_acquire.acquire(
        _fetcher({("AAPL", NORMAL): revised, ("MSFT", NORMAL): unchanged}),
        store,
        UNIVERSE,
        (NORMAL,),
        clock=_Clock(START + timedelta(days=1)),
    )

    assert [item.symbol for item in second.corrected] == ["AAPL"]
    assert [item.symbol for item in second.unchanged] == ["MSFT"]
    assert second.corrected[0].revision == 2
    assert second.corrected[0].previous_replay_id is not None


def test_preflight_performs_no_io_and_describes_every_request() -> None:
    sessions = (OTHER, NORMAL)
    plan = historical_replay_acquire.preflight(UNIVERSE, sessions)

    assert plan.sessions == sessions
    assert len(plan.requests) == 4
    assert plan.total_expected_bars == 4 * 78
    assert all(item.request_params["needExtendedHoursData"] == "false" for item in plan.requests)
    assert not any(item.early_close for item in plan.requests)

    payload = historical_replay_acquire.preflight_payload(plan)
    assert payload["mode"] == "preflight"
    assert payload["request_count"] == 4


def test_preflight_marks_an_early_close_session_with_its_reduced_count() -> None:
    plan = historical_replay_acquire.preflight(UNIVERSE, (date(2026, 11, 27),))

    assert all(item.early_close for item in plan.requests)
    assert all(item.expected_bar_count == 42 for item in plan.requests)
