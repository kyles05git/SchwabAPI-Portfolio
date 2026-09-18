"""Session-aligned daily-bar freshness and the history cache's settled-evidence rule.

These are the two mechanism-level defects behind the ``paper-first-2026-07-27``
incident (see ``docs/incidents/2026-07-27-paper-first-cohort-daily-bars-stale.md``):
freshness judged by elapsed hours instead of exchange session, and a cache that could
serve a pre-close response as settled end-of-day evidence.

Everything here is offline and deterministic. No `.env` is read, no database, broker,
or SMTP connection is opened, and the only "provider" is a local stub.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from schwab_trader import history_cache as hc
from schwab_trader.data_contracts import (
    BarBatch,
    BarObservation,
    Provenance,
    TimingPolicy,
)
from schwab_trader.data_readiness import (
    DataKind,
    DataRequirement,
    ReasonCode,
    SourceProbe,
    evaluate_requirement,
    session_coverage,
)
from schwab_trader.market_data import Candle

FRIDAY = date(2026, 7, 24)
MONDAY = date(2026, 7, 27)
TUESDAY = date(2026, 7, 28)
SATURDAY = date(2026, 7, 25)

#: The day after Thanksgiving 2026 closes early at 13:00 ET. (2026-07-03 is *not* an
#: early close: July 4 falls on a Saturday, so July 3 is the observed holiday itself.)
EARLY_CLOSE = date(2026, 11, 27)
#: Christmas Day 2026 falls on a Friday and is a full closure.
HOLIDAY = date(2026, 12, 25)

MONDAY_PRE_CLOSE = datetime(2026, 7, 27, 14, 0, tzinfo=UTC)  # 10:00 ET
MONDAY_POST_CLOSE = datetime(2026, 7, 27, 20, 30, tzinfo=UTC)  # 16:30 ET


def _batch(*, session: date, symbols: tuple[str, ...] = ("AAPL", "MSFT")) -> BarBatch:
    return BarBatch(
        provenance=Provenance(
            source="test",
            snapshot_id=f"bars:{session.isoformat()}",
            retrieved_at=MONDAY_POST_CLOSE,
            as_of=datetime.combine(session, datetime.min.time(), tzinfo=UTC),
            timing=TimingPolicy.SETTLED_EOD,
            vintage_safe=True,
        ),
        bars=tuple(
            BarObservation(
                symbol=symbol,
                session_date=session,
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100.5"),
                volume=1_000,
            )
            for symbol in symbols
        ),
    )


def _mixed_batch(coverage: dict[str, date]) -> BarBatch:
    latest = max(coverage.values())
    return BarBatch(
        provenance=Provenance(
            source="test",
            snapshot_id="bars:mixed",
            retrieved_at=MONDAY_POST_CLOSE,
            as_of=datetime.combine(latest, datetime.min.time(), tzinfo=UTC),
            timing=TimingPolicy.SETTLED_EOD,
            vintage_safe=True,
        ),
        bars=tuple(
            BarObservation(
                symbol=symbol,
                session_date=session,
                open=Decimal("100"),
                high=Decimal("100"),
                low=Decimal("100"),
                close=Decimal("100"),
                volume=1,
            )
            for symbol, session in sorted(coverage.items())
        ),
    )


def _requirement(session: date | None, keys: tuple[str, ...] = ("AAPL", "MSFT")):
    return DataRequirement(kind=DataKind.DAILY_BARS, keys=keys, required_session=session)


# ---------------------------------------------------------------------------
# Session-aligned freshness
# ---------------------------------------------------------------------------


def test_friday_bars_do_not_satisfy_a_monday_session():
    """The incident, reduced to one assertion."""
    result = evaluate_requirement(
        _requirement(MONDAY),
        SourceProbe.of(_batch(session=FRIDAY)),
        now=MONDAY_POST_CLOSE,
    )

    assert not result.ready
    assert result.reasons == (ReasonCode.SESSION_NOT_COVERED,)
    assert result.required_session == MONDAY
    assert result.latest_session == FRIDAY
    assert result.uncovered_keys == ("AAPL", "MSFT")
    assert "2026-07-24" in result.detail and "2026-07-27" in result.detail


def test_monday_bars_satisfy_a_monday_session():
    result = evaluate_requirement(
        _requirement(MONDAY),
        SourceProbe.of(_batch(session=MONDAY)),
        now=MONDAY_POST_CLOSE,
    )

    assert result.ready
    assert result.reasons == (ReasonCode.OK,)
    assert result.latest_session == MONDAY


def test_friday_bars_satisfy_a_friday_session_checked_on_the_weekend():
    """Elapsed hours would call this stale. Session identity correctly does not.

    This is the direction the old elapsed-time rule got wrong in the *other* way: on a
    Saturday, Friday's close is the most recent settled session that exists.
    """
    saturday_evening = datetime(2026, 7, 25, 23, 0, tzinfo=UTC)
    result = evaluate_requirement(
        _requirement(FRIDAY),
        SourceProbe.of(_batch(session=FRIDAY)),
        now=saturday_evening,
    )

    assert result.ready, "Friday's bar is settled evidence about Friday, whatever day it is"


def test_bars_ahead_of_the_required_session_are_accepted():
    """A batch that reaches further than required still covers the required session."""
    result = evaluate_requirement(
        _requirement(FRIDAY),
        SourceProbe.of(_batch(session=MONDAY)),
        now=MONDAY_POST_CLOSE,
    )
    assert result.ready


def test_partial_provider_coverage_names_only_the_symbols_that_are_short():
    result = evaluate_requirement(
        _requirement(MONDAY, keys=("AAPL", "MSFT", "NVDA")),
        SourceProbe.of(
            _mixed_batch({"AAPL": MONDAY, "MSFT": FRIDAY, "NVDA": MONDAY}),
        ),
        now=MONDAY_POST_CLOSE,
    )

    assert not result.ready
    assert result.reasons == (ReasonCode.SESSION_NOT_COVERED,)
    assert result.uncovered_keys == ("MSFT",)
    assert result.missing_keys == ()


def test_missing_symbols_and_short_coverage_are_reported_separately():
    result = evaluate_requirement(
        _requirement(MONDAY, keys=("AAPL", "MSFT", "ABSENT")),
        SourceProbe.of(_mixed_batch({"AAPL": MONDAY, "MSFT": FRIDAY})),
        now=MONDAY_POST_CLOSE,
    )

    assert set(result.reasons) == {ReasonCode.MISSING_KEYS, ReasonCode.SESSION_NOT_COVERED}
    assert result.missing_keys == ("ABSENT",)
    assert result.uncovered_keys == ("MSFT",)
    assert result.coverage == pytest.approx(2 / 3)


def test_provider_exception_is_a_distinct_reason_with_sanitized_detail():
    """A failed provider must not read as a mere absence of data."""
    result = evaluate_requirement(
        _requirement(MONDAY),
        SourceProbe.failed("ConnectTimeout"),
        now=MONDAY_POST_CLOSE,
    )

    assert not result.ready
    assert result.reasons == (ReasonCode.PROVIDER_ERROR,)
    assert "ConnectTimeout" in result.detail
    for secret in ("http", "token", "password", "@"):
        assert secret not in result.detail.lower()


def test_reason_codes_are_never_duplicated():
    """The cosmetic half of the incident: `daily_bars:stale` was persisted twice."""
    result = evaluate_requirement(
        _requirement(MONDAY, keys=("AAPL", "MSFT", "NVDA", "ABSENT")),
        SourceProbe.of(_mixed_batch({"AAPL": FRIDAY, "MSFT": FRIDAY, "NVDA": FRIDAY})),
        now=MONDAY_POST_CLOSE,
    )

    assert len(result.reasons) == len(set(result.reasons))
    assert result.reasons.count(ReasonCode.SESSION_NOT_COVERED) == 1


def test_elapsed_time_freshness_is_unchanged_when_no_session_is_required():
    """Callers with no session context keep the previous behaviour exactly."""
    old = _batch(session=FRIDAY)
    stale = DataRequirement(
        kind=DataKind.DAILY_BARS, keys=("AAPL",), max_staleness=timedelta(hours=1)
    )
    result = evaluate_requirement(stale, SourceProbe.of(old), now=MONDAY_POST_CLOSE)

    assert result.reasons == (ReasonCode.STALE,)
    assert result.required_session is None


def test_session_coverage_is_derived_from_bar_observations():
    batch = _mixed_batch({"AAPL": MONDAY, "MSFT": FRIDAY})
    assert session_coverage(batch) == (("AAPL", MONDAY), ("MSFT", FRIDAY))
    assert session_coverage(None) == ()


def test_explicit_coverage_overrides_derivation_for_non_bar_batches():
    probe = SourceProbe.of(_batch(session=FRIDAY), covered_sessions={"AAPL": MONDAY})
    assert probe.covered_sessions == (("AAPL", MONDAY),)


# ---------------------------------------------------------------------------
# History cache: a pre-close response is not settled evidence
# ---------------------------------------------------------------------------


class _StubProvider:
    """Counts calls and returns candles through a configurable session."""

    def __init__(self, through: date) -> None:
        self.through = through
        self.calls = 0

    def __call__(self, client, symbol, *, days):
        self.calls += 1
        day = self.through
        out = []
        for _ in range(days):
            out.append(
                Candle(
                    symbol=symbol,
                    date=datetime.combine(day, datetime.min.time()),
                    open=Decimal("100"),
                    high=Decimal("100"),
                    low=Decimal("100"),
                    close=Decimal("100"),
                    volume=1,
                )
            )
            day -= timedelta(days=1)
        return list(reversed(out))


@pytest.fixture
def cache(tmp_path):
    return hc.HistoryCache(tmp_path / "history")


def _install(monkeypatch, provider):
    monkeypatch.setattr(hc.market_data, "get_price_history", provider)


def test_pre_close_response_is_not_reused_as_settled_end_of_day_data(cache, monkeypatch):
    """The cache defect, reduced to one assertion.

    A payload captured at 10:00 ET covers only Friday. At 16:30 ET the same day the old
    cache served it back because the UTC fetch date matched, so the post-close official
    run could never see Monday's settled bar.
    """
    pre_close = _StubProvider(through=FRIDAY)
    _install(monkeypatch, pre_close)
    cache.get(object(), "AAPL", days=5, now=MONDAY_PRE_CLOSE)
    assert pre_close.calls == 1

    post_close = _StubProvider(through=MONDAY)
    _install(monkeypatch, post_close)
    candles = cache.get(object(), "AAPL", days=5, settled_through=MONDAY, now=MONDAY_POST_CLOSE)

    assert post_close.calls == 1, "the pre-close payload must not be reused"
    assert candles[-1].date.date() == MONDAY


def test_post_close_response_is_reused_for_the_same_session(cache, monkeypatch):
    provider = _StubProvider(through=MONDAY)
    _install(monkeypatch, provider)
    cache.get(object(), "AAPL", days=5, settled_through=MONDAY, now=MONDAY_POST_CLOSE)
    cache.get(
        object(),
        "AAPL",
        days=5,
        settled_through=MONDAY,
        now=MONDAY_POST_CLOSE + timedelta(hours=1),
    )

    assert provider.calls == 1, "a genuinely settled payload is cached"


def test_a_payload_that_still_lacks_the_session_is_refetched_on_every_retry(cache, monkeypatch):
    """Retrying is how the runner picks the bar up once the provider publishes it."""
    behind = _StubProvider(through=FRIDAY)
    _install(monkeypatch, behind)
    cache.get(object(), "AAPL", days=5, settled_through=MONDAY, now=MONDAY_POST_CLOSE)
    cache.get(
        object(),
        "AAPL",
        days=5,
        settled_through=MONDAY,
        now=MONDAY_POST_CLOSE + timedelta(minutes=30),
    )
    assert behind.calls == 2

    caught_up = _StubProvider(through=MONDAY)
    _install(monkeypatch, caught_up)
    candles = cache.get(
        object(),
        "AAPL",
        days=5,
        settled_through=MONDAY,
        now=MONDAY_POST_CLOSE + timedelta(hours=1),
    )
    assert candles[-1].date.date() == MONDAY


def test_early_close_session_uses_the_thirteen_hundred_close(cache, monkeypatch):
    """A 13:15 ET fetch is post-close on an early-close day, and pre-close normally."""
    # 2026-11-27 is EST: 13:00 ET = 18:00 UTC. A regular July close is 16:00 EDT = 20:00.
    assert hc.session_close_utc(EARLY_CLOSE).hour == 18
    assert hc.session_close_utc(MONDAY).hour == 20

    provider = _StubProvider(through=EARLY_CLOSE)
    _install(monkeypatch, provider)
    after_early_close = datetime(2026, 11, 27, 18, 15, tzinfo=UTC)
    cache.get(object(), "AAPL", days=5, settled_through=EARLY_CLOSE, now=after_early_close)
    cache.get(
        object(),
        "AAPL",
        days=5,
        settled_through=EARLY_CLOSE,
        now=after_early_close + timedelta(minutes=5),
    )

    assert provider.calls == 1


def test_legacy_payload_without_a_retrieval_time_fails_closed(cache, monkeypatch):
    """An old cache file cannot prove it was captured after the close, so it is not used."""
    path = cache._path("AAPL")
    path.write_text(
        json.dumps(
            {
                "fetched": MONDAY.isoformat(),
                "count": 5,
                "candles": [
                    {
                        "d": datetime.combine(MONDAY, datetime.min.time()).isoformat(),
                        "o": "100",
                        "h": "100",
                        "l": "100",
                        "c": "100",
                        "v": 1,
                    }
                ]
                * 5,
            }
        ),
        encoding="utf-8",
    )
    provider = _StubProvider(through=MONDAY)
    _install(monkeypatch, provider)

    cache.get(object(), "AAPL", days=5, settled_through=MONDAY, now=MONDAY_POST_CLOSE)

    assert provider.calls == 1, "a payload with no retrieval time is never settled evidence"


def test_convenience_mode_keeps_the_per_day_behaviour(cache, monkeypatch):
    provider = _StubProvider(through=MONDAY)
    _install(monkeypatch, provider)
    cache.get(object(), "AAPL", days=5, now=MONDAY_PRE_CLOSE)
    cache.get(object(), "AAPL", days=5, now=MONDAY_POST_CLOSE)
    assert provider.calls == 1

    cache.get(object(), "AAPL", days=5, now=MONDAY_POST_CLOSE + timedelta(days=1))
    assert provider.calls == 2


def test_weekend_and_holiday_dates_never_yield_settled_evidence(cache, monkeypatch):
    """No exchange session closes on a Saturday or Christmas, so nothing settles."""
    provider = _StubProvider(through=FRIDAY)
    _install(monkeypatch, provider)
    for closed in (SATURDAY, HOLIDAY):
        provider.calls = 0
        cache.get(
            object(),
            "AAPL",
            days=5,
            settled_through=closed,
            now=datetime.combine(closed, datetime.min.time(), tzinfo=UTC) + timedelta(days=1),
        )
        assert provider.calls == 1
