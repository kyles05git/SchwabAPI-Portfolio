"""Tests for the agent evaluation harness (cycle persistence + scorecard)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from schwab_trader.agent import AgentRunner, DipBuyerStrategy, HoldStrategy
from schwab_trader.data_contracts import FakeDailyBarSource
from schwab_trader.data_readiness import (
    DataKind,
    DataRequirement,
    SourceProbe,
    evaluate_readiness,
)
from schwab_trader.evaluation import (
    SCHEMA_VERSION,
    EvaluationStore,
    ObservationStatus,
    OfficialDailyObservation,
    official_observation_key,
    summarize_readiness,
)
from schwab_trader.market_data import Quote
from schwab_trader.paper import PaperEngine

NOW = datetime(2026, 7, 14, 15, 0, tzinfo=UTC)
SESSION = date(2026, 7, 14)


def _quote(symbol: str, *, last: str, prev_close: str, ask: str) -> Quote:
    return Quote(
        symbol=symbol,
        last=Decimal(last),
        previous_close=Decimal(prev_close),
        ask=Decimal(ask),
        bid=Decimal(ask),
        mark=Decimal(ask),
        quote_time=NOW,
    )


def _engine(tmp_path: Path) -> PaperEngine:
    return PaperEngine(tmp_path / "paper.sqlite3", starting_cash=Decimal("1000.00"))


def _store(tmp_path: Path) -> EvaluationStore:
    return EvaluationStore(tmp_path / "eval.sqlite3")


def test_summary_empty(tmp_path: Path) -> None:
    summary = _store(tmp_path).summary()
    assert summary.cycles == 0
    assert summary.first_ts is None


def test_records_cycle_with_decisions(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    quotes = {"AAA": _quote("AAA", last="9.00", prev_close="10.00", ask="9.00")}
    strategy = DipBuyerStrategy(["AAA"], dip_pct=Decimal("0.02"), per_trade_cash=Decimal("90"))
    report = AgentRunner(strategy, engine, lambda s: quotes[s]).run_cycle(now=NOW)

    store = _store(tmp_path)
    cycle_id = store.record_cycle(report)
    assert cycle_id == 1

    cycles = store.recent_cycles()
    assert len(cycles) == 1
    assert cycles[0].strategy == "dip-buyer"
    assert cycles[0].num_filled == 1
    assert cycles[0].total_value == report.valuation.total_value


def test_summary_tracks_return_and_trades(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    store = _store(tmp_path)

    # Cycle 1: a hold cycle (no trades), value ~ 1000.
    hold = AgentRunner(
        HoldStrategy([]), engine, lambda s: _quote(s, last="1", prev_close="1", ask="1")
    )
    store.record_cycle(hold.run_cycle(now=NOW))

    # Cycle 2: buy a dip.
    quotes = {"AAA": _quote("AAA", last="9.00", prev_close="10.00", ask="9.00")}
    strategy = DipBuyerStrategy(["AAA"], dip_pct=Decimal("0.02"), per_trade_cash=Decimal("90"))
    store.record_cycle(AgentRunner(strategy, engine, lambda s: quotes[s]).run_cycle(now=NOW))

    summary = store.summary()
    assert summary.cycles == 2
    assert summary.trades_filled == 1
    assert summary.starting_cash == Decimal("1000.00")


def test_equity_curve_is_chronological(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    store = _store(tmp_path)
    runner = AgentRunner(
        HoldStrategy([]), engine, lambda s: _quote(s, last="1", prev_close="1", ask="1")
    )
    store.record_cycle(runner.run_cycle(now=NOW))
    store.record_cycle(runner.run_cycle(now=NOW))
    curve = store.equity_curve()
    assert len(curve) == 2
    assert all(value == Decimal("1000.00") for _, value in curve)


# ---- Official daily observations (EvaluationStore v2) ----


def _official(
    *,
    run_id: str = "run-1",
    session: date = SESSION,
    status: ObservationStatus = ObservationStatus.OFFICIAL,
    total_value: Decimal | None = Decimal("1005.00"),
    return_pct: Decimal | None = Decimal("0.50"),
) -> OfficialDailyObservation:
    return OfficialDailyObservation(
        cohort_id="cohort-a",
        run_id=run_id,
        sleeve_id="dip-buyer",
        strategy="dip-buyer",
        strategy_hash="sha256:abc",
        session_date=session,
        decision_time=datetime.combine(session, datetime.min.time(), tzinfo=UTC),
        valuation_time=datetime.combine(session, datetime.min.time(), tzinfo=UTC)
        + timedelta(hours=16),
        status=status,
        total_value=total_value,
        return_pct=return_pct,
        benchmark_value=Decimal("500.00"),
        exposure=Decimal("0.80"),
        num_positions=3,
        turnover=Decimal("120.00"),
        modeled_cost=Decimal("0.35"),
        num_filled=2,
        num_rejected=1,
        quote_coverage=Decimal("1.0"),
        snapshot_ids={"daily_bars": "bars:fake:2026-07-14"},
    )


def test_official_observation_round_trips(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_official_observation(_official())

    observations = store.official_observations()
    assert len(observations) == 1
    obs = observations[0]
    assert obs.cohort_id == "cohort-a"
    assert obs.run_id == "run-1"
    assert obs.strategy_hash == "sha256:abc"
    assert obs.status is ObservationStatus.OFFICIAL
    assert obs.total_value == Decimal("1005.00")
    assert obs.turnover == Decimal("120.00")
    assert obs.snapshot_ids == {"daily_bars": "bars:fake:2026-07-14"}
    assert obs.observation_key == official_observation_key("cohort-a", "dip-buyer", SESSION)


def test_official_observation_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Two intraday runs on the same session promote to the same official key.
    first = store.record_official_observation(_official(run_id="run-1"))
    second = store.record_official_observation(_official(run_id="run-2"))
    assert first == second
    assert len(store.official_observations()) == 1


def test_missing_session_is_status_not_zero(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_official_observation(
        _official(status=ObservationStatus.MISSING, total_value=None, return_pct=None)
    )
    obs = store.official_observations()[0]
    assert obs.status is ObservationStatus.MISSING
    assert obs.total_value is None
    assert obs.return_pct is None
    # A missing session contributes no fabricated point to the equity series.
    assert store.official_equity_curve() == []


def test_official_status_validation() -> None:
    with pytest.raises(ValueError, match="official observation must carry"):
        _official(total_value=None)
    with pytest.raises(ValueError, match="missing session cannot carry"):
        _official(status=ObservationStatus.MISSING, total_value=Decimal("1000.00"))


def test_official_equity_curve_is_chronological(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_official_observation(
        _official(session=date(2026, 7, 15), total_value=Decimal("1010.00"))
    )
    store.record_official_observation(_official(session=date(2026, 7, 14)))
    curve = store.official_equity_curve()
    assert [d for d, _ in curve] == [date(2026, 7, 14), date(2026, 7, 15)]
    assert [v for _, v in curve] == [Decimal("1005.00"), Decimal("1010.00")]


def test_readiness_summary_persists(tmp_path: Path) -> None:
    source = FakeDailyBarSource(symbols={"AAA": Decimal("10")}, as_of=NOW)
    batch = source.fetch_daily_bars(("AAA", "BBB"), SESSION, SESSION)
    requirement = DataRequirement(kind=DataKind.DAILY_BARS, keys=("AAA", "BBB"))
    readiness = evaluate_readiness([(requirement, SourceProbe.of(batch))], now=NOW)
    ready, reasons, snapshot_ids = summarize_readiness(readiness)
    assert ready is False  # BBB is missing
    assert reasons  # carries at least one "<kind>:<reason>"

    store = _store(tmp_path)
    obs = _official(status=ObservationStatus.PARTIAL).model_copy(
        update={
            "readiness_ready": ready,
            "readiness_reasons": reasons,
            "snapshot_ids": snapshot_ids,
        }
    )
    store.record_official_observation(obs)
    stored = store.official_observations()[0]
    assert stored.readiness_ready is False
    assert stored.readiness_reasons == reasons
    assert stored.snapshot_ids == snapshot_ids


def test_old_database_remains_readable(tmp_path: Path) -> None:
    # Simulate a pre-v2 database: only the original tables, no user_version.
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE agent_cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL, strategy TEXT NOT NULL,
                num_proposals INTEGER NOT NULL, num_filled INTEGER NOT NULL,
                num_rejected INTEGER NOT NULL, cash TEXT NOT NULL,
                positions_value TEXT NOT NULL, total_value TEXT NOT NULL,
                realized_pnl TEXT NOT NULL, unrealized_pnl TEXT NOT NULL,
                starting_cash TEXT NOT NULL, return_pct TEXT NOT NULL
            );
            CREATE TABLE agent_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER NOT NULL,
                side TEXT NOT NULL, symbol TEXT NOT NULL, quantity INTEGER NOT NULL,
                limit_price TEXT NOT NULL, status TEXT NOT NULL,
                fill_price TEXT, rationale TEXT
            );
            INSERT INTO agent_cycles (
                ts, strategy, num_proposals, num_filled, num_rejected, cash,
                positions_value, total_value, realized_pnl, unrealized_pnl,
                starting_cash, return_pct
            ) VALUES ('2026-07-14T15:00:00+00:00', 'hold', 0, 0, 0, '1000', '0',
                      '1000', '0', '0', '1000', '0');
            """
        )

    store = EvaluationStore(path)  # migrates in place
    cycles = store.recent_cycles()
    assert len(cycles) == 1
    assert cycles[0].strategy == "hold"

    # The new table is available and the version stamp advanced.
    store.record_official_observation(_official())
    assert len(store.official_observations()) == 1
    with sqlite3.connect(path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_VERSION
