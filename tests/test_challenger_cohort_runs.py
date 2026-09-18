"""Running the challenger-v1 cohort: cadence, all-or-nothing readiness, and scoping.

Synthetic bars, synthetic SEC facts, and disposable ``tmp_path`` stores throughout.
Nothing here reads ``.env``, opens a socket, or contacts Schwab, Neon, SEC EDGAR, SMTP,
or a broker, and the paper orchestrator has no order path at all.

The signal session is Monday **2026-08-31**, the last XNYS session of August, so the
monthly sleeves are due to rebalance and the execution session is Tuesday 2026-09-01.
Choosing a month end is what lets one run exercise all three cadences at once.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from schwab_trader import challenger_cohort as cc
from schwab_trader import challenger_strategies as cs
from schwab_trader import (
    cohort_lifecycle,
    cohort_preflight,
    comparison,
    scheduling,
    strategy_registry,
)
from schwab_trader import execution_timing as et
from schwab_trader import market_calendar as mc
from schwab_trader.data_readiness import (
    DataKind,
    DataRequirement,
    SourceProbe,
    evaluate_readiness,
)
from schwab_trader.evaluation import EvaluationStore
from schwab_trader.market_data import Candle, Quote
from schwab_trader.next_open_fill import validate_opening_bar
from schwab_trader.safety import KillSwitch
from schwab_trader.sec_edgar import Fact
from schwab_trader.sec_store import SecStore
from schwab_trader.sleeve_runs import (
    CohortSnapshot,
    MemberRunStatus,
    SleeveRunOrchestrator,
    SleeveRunStatus,
    SleeveRunStore,
)
from schwab_trader.storage.database import Database
from schwab_trader.storage.sleeves import SqlAlchemySleeveStore
from schwab_trader.strategies import contract

SIGNAL = date(2026, 8, 31)
EXECUTION = date(2026, 9, 1)
SIGNAL_CLOSE_UTC = datetime(2026, 8, 31, 20, 0, tzinfo=UTC)
EXECUTION_OPEN_UTC = datetime(2026, 9, 1, 13, 30, tzinfo=UTC)
RETRIEVED_UTC = EXECUTION_OPEN_UTC + timedelta(minutes=10)

#: Mid-morning on the execution session: the signal session's run is LATE, but the
#: scheduler's deadline (the next session's due time) has not passed, so it may retry.
NOW_ET = datetime(2026, 9, 1, 9, 45)
NOW_UTC = datetime(2026, 9, 1, 13, 45, tzinfo=UTC)

CLOSE_PRICE = Decimal("100")
OPEN_PRICE = Decimal("99")

PLAN_NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
PLAN_NOW_ET = datetime(2026, 8, 1, 8, 0)


# --- the cadence calendar -----------------------------------------------------


def test_the_monthly_cadence_matches_the_xnys_calendar_exactly() -> None:
    """One sweep covering weekends, holidays, early closes, and the year rollover.

    Hand-picked dates would prove the rule for the cases someone thought of. Comparing
    against the calendar itself, for every day of two years, proves it for the ones they
    did not — including a month whose last session is a Friday before a holiday Monday.
    """
    for year in (2026, 2027):
        for month in range(1, 13):
            days = [date(year, month, d) for d in range(1, monthrange(year, month)[1] + 1)]
            sessions = [day for day in days if mc.is_trading_day(day)]
            assert sessions, f"{year}-{month} has no sessions"
            last = sessions[-1]
            for day in days:
                assert cs.is_last_trading_session_of_month(day) is (day == last), day


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 8, 31), True),  # Monday month end
        (date(2026, 8, 28), False),  # ordinary Friday
        (date(2026, 5, 29), True),  # Friday month end; May 30/31 are a weekend
        (date(2026, 12, 31), True),  # year rollover
        (date(2026, 11, 27), False),  # early close, but Nov 30 is the month's last
        (date(2026, 9, 5), False),  # Saturday: not a session at all
        (date(2026, 9, 7), False),  # Labor Day
    ],
)
def test_named_cadence_boundaries(day: date, expected: bool) -> None:
    assert cs.is_last_trading_session_of_month(day) is expected


def test_a_monthly_gate_only_opens_on_a_month_end_and_a_daily_gate_opens_every_session() -> None:
    monthly = cs.CadenceGate(cs.RebalanceCadence.MONTHLY)
    daily = cs.CadenceGate(cs.RebalanceCadence.DAILY)
    never = cs.CadenceGate(cs.RebalanceCadence.NEVER)

    assert monthly.allows(SIGNAL) and daily.allows(SIGNAL) and not never.allows(SIGNAL)
    ordinary = date(2026, 8, 28)
    assert not monthly.allows(ordinary)
    assert daily.allows(ordinary)
    # A weekend is not a session for any cadence.
    assert not daily.allows(date(2026, 8, 29))
    # An unidentifiable decision instant is refused by every cadence.
    assert not daily.allows(None)
    assert not monthly.allows(None)


def test_a_naive_decision_instant_yields_no_session_and_therefore_no_orders() -> None:
    assert cs.signal_session_date(datetime(2026, 8, 31, 16, 0)) is None
    assert cs.signal_session_date(SIGNAL_CLOSE_UTC) == SIGNAL


def test_an_unknown_cadence_fails_closed() -> None:
    with pytest.raises(cs.ContractViolationError, match="not a known rebalance cadence"):
        cs.CadenceGate("fortnightly")


# --- synthetic evidence -------------------------------------------------------


def _sessions_before(end: date, count: int) -> list[date]:
    days: list[date] = []
    day = end
    while len(days) < count:
        if mc.is_trading_day(day):
            days.append(day)
        day -= timedelta(days=1)
    return sorted(days)


#: Enough closes for the longest frozen lookback (dual momentum needs 253).
_HISTORY_SESSIONS = _sessions_before(SIGNAL, 260)


def _bars(symbol: str, *, slope: Decimal = Decimal("0.10")) -> list[Candle]:
    """A gently rising series ending exactly on the signal session's close."""
    candles: list[Candle] = []
    for index, day in enumerate(_HISTORY_SESSIONS):
        close = CLOSE_PRICE + slope * index
        candles.append(
            Candle(
                symbol=symbol,
                date=mc.session_bounds_utc(day)[1],
                open=close,
                high=close,
                low=close,
                close=close,
                volume=1_000,
                source="test",
            )
        )
    return candles


def _quote(symbol: str, price: Decimal, *, quote_time: datetime = SIGNAL_CLOSE_UTC) -> Quote:
    return Quote(
        symbol=symbol,
        bid=price,
        ask=price,
        last=price,
        mark=price,
        previous_close=price,
        quote_time=quote_time,
    )


def _opening_bar(symbol: str, *, session: date = EXECUTION, price: Decimal = OPEN_PRICE):
    opened = mc.session_bounds_utc(session)[0]
    candle = Candle(
        symbol=symbol,
        date=opened,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=5_000,
        source="test-intraday",
    )
    return validate_opening_bar(symbol, session, [candle], retrieved_at=RETRIEVED_UTC).require()


_CONCEPTS = {
    "gross_profit": "GrossProfit",
    "net_income": "NetIncomeLoss",
    "revenue": "Revenues",
    "assets": "Assets",
    "equity": "StockholdersEquity",
}


def _facts(ticker: str, scale: int) -> list[Fact]:
    values = {
        "gross_profit": 30 * scale,
        "net_income": 12 * scale,
        "revenue": 100 * scale,
        "assets": 100,
        "equity": 60,
    }
    return [
        Fact(
            ticker=ticker,
            cik=1,
            concept=_CONCEPTS[field],
            unit="USD",
            period_start=None,
            period_end=date(2025, 12, 31),
            value=Decimal(str(value)),
            fiscal_year=None,
            fiscal_period="FY",
            form="10-K",
            filed=date(2026, 2, 2),
            accession=f"{ticker}-1",
            frame=None,
        )
        for field, value in values.items()
    ]


@pytest.fixture(scope="module")
def plan() -> cc.ChallengerPlan:
    return cc.build_plan(start_session=SIGNAL, now=PLAN_NOW, now_et=PLAN_NOW_ET)


@pytest.fixture(scope="module")
def symbols(plan: cc.ChallengerPlan) -> tuple[str, ...]:
    return tuple(sorted({symbol for spec in plan.specs for symbol in spec.universe}))


@pytest.fixture(scope="module")
def history(symbols: tuple[str, ...]) -> dict[str, list[Candle]]:
    # A distinct slope per symbol so the momentum ranking is total and deterministic.
    return {
        symbol: _bars(symbol, slope=Decimal("0.10") + Decimal(index) / 1000)
        for index, symbol in enumerate(symbols)
    }


def _sec_store(tmp_path: Path, symbols: tuple[str, ...], *, covered: int | None = None) -> SecStore:
    store = SecStore(tmp_path / "sec.sqlite3")
    large_cap = [s for s in symbols if s in contract.QUALITY_PROFITABILITY.universe]
    limit = len(large_cap) if covered is None else covered
    for index, ticker in enumerate(large_cap[:limit]):
        store.upsert(_facts(ticker, index + 1))
    return store


def _cohort_store(tmp_path: Path) -> SqlAlchemySleeveStore:
    """Shared storage, which is what the challenger cohort requires in production."""
    database = Database(f"sqlite:///{tmp_path / 'shared.sqlite3'}", create_schema=True)
    return SqlAlchemySleeveStore(database)


def _snapshot(
    members,
    history: dict[str, list[Candle]],
    sec_store: SecStore,
    *,
    opening_bars=None,
    unready_member: str | None = None,
    snapshot_id: str = "snapshot:challenger",
):
    symbols = sorted({symbol for cfg in members for symbol in cfg.universe})
    if opening_bars is None:
        opening_bars = {symbol: _opening_bar(symbol) for symbol in symbols}

    def _readiness(name: str):
        if name != unready_member:
            return evaluate_readiness([], now=SIGNAL_CLOSE_UTC)
        # A genuinely unmet requirement: the source has nothing for this member yet.
        return evaluate_readiness(
            [
                (
                    DataRequirement(kind=DataKind.DAILY_BARS, keys=("SPY",)),
                    SourceProbe.of(None),
                )
            ],
            now=SIGNAL_CLOSE_UTC,
        )

    return CohortSnapshot(
        snapshot_id=snapshot_id,
        quote_snapshot_id="quotes:challenger",
        captured_at=SIGNAL_CLOSE_UTC,
        quotes={symbol: _quote(symbol, CLOSE_PRICE) for symbol in symbols},
        resources=strategy_registry.StrategyResources(
            history=history,
            benchmark_history=history.get("SPY", []),
            store=sec_store,
        ),
        readiness_by_member={cfg.name: _readiness(cfg.name) for cfg in members},
        data_snapshot_ids={"daily_bars": "bars:challenger"},
        opening_bars=opening_bars,
    )


def _harness(tmp_path: Path, plan: cc.ChallengerPlan, snapshot):
    sleeve_store = _cohort_store(tmp_path)
    cc.create(plan, store=sleeve_store)
    members = tuple(
        cfg for cfg in sleeve_store.list() if cfg.cohort_id == plan.cohort_id
    )
    run_store = SleeveRunStore(tmp_path / "runs.sqlite3")
    built = snapshot(members)
    calls: list[str | None] = []

    def provider(configs, session, required_snapshot_id):
        calls.append(required_snapshot_id)
        return built

    orchestrator = SleeveRunOrchestrator(
        run_store=run_store,
        sleeve_store=sleeve_store,
        kill_switch=KillSwitch(tmp_path / "KILL_SWITCH"),
        snapshot_provider=provider,
        universe_resolver=lambda cfg: list(cfg.universe),
        engine_factory=lambda cfg: __import__(
            "schwab_trader.paper", fromlist=["PaperEngine"]
        ).PaperEngine(
            tmp_path / "paper" / f"{cfg.name}.sqlite3",
            starting_cash=cfg.starting_cash,
            settle_t1=cfg.settlement_t1,
            leverage=cfg.leverage,
        ),
        evaluation_factory=lambda cfg: EvaluationStore(tmp_path / "eval" / f"{cfg.name}.sqlite3"),
    )
    return sleeve_store, run_store, orchestrator, members, calls


def _due(plan: cc.ChallengerPlan, now_et: datetime = NOW_ET, completed=frozenset()):
    return scheduling.evaluate_session(plan.cohort_id, SIGNAL, now_et, completed)


# --- the whole cohort runs, or nobody does -----------------------------------


def test_all_five_members_complete_one_session_together(
    tmp_path: Path, plan: cc.ChallengerPlan, history, symbols
) -> None:
    sec_store = _sec_store(tmp_path, symbols)
    _, _, orchestrator, members, _ = _harness(
        tmp_path, plan, lambda m: _snapshot(m, history, sec_store)
    )

    run = orchestrator.run(_due(plan), members, now=NOW_UTC)

    assert run.status is SleeveRunStatus.COMPLETED
    assert len(run.completed_members) == 5
    assert {m.status for m in run.members} == {MemberRunStatus.COMPLETED}
    # Every member recorded exactly one official observation for the signal session,
    # and every one of them executed against the T+1 open.
    for cfg in members:
        observations = EvaluationStore(
            tmp_path / "eval" / f"{cfg.name}.sqlite3"
        ).official_observations()
        assert len(observations) == 1, cfg.name
        assert observations[0].session_date == SIGNAL
        assert observations[0].execution_session_date == EXECUTION
        assert observations[0].execution_methodology == et.NEXT_OPEN_METHODOLOGY_KEY


def test_the_cadence_decides_which_members_actually_trade(
    tmp_path: Path, plan: cc.ChallengerPlan, history, symbols
) -> None:
    """A month end: the two monthly sleeves and the daily sleeve trade, cash never does."""
    sec_store = _sec_store(tmp_path, symbols)
    _, _, orchestrator, members, _ = _harness(
        tmp_path, plan, lambda m: _snapshot(m, history, sec_store)
    )

    orchestrator.run(_due(plan), members, now=NOW_UTC)

    traded = {
        cfg.name: len(orchestrator.engine_factory(cfg).recent_orders())
        for cfg in members
    }
    # The accounting control never trades, whatever the session.
    assert traded["control-cash"] == 0
    # The benchmark buys once.
    assert traded["bench-spy"] == 1
    # A month end opens both monthly gates, and the daily sleeve is always open. Each
    # must have actually traded, or this test would pass for the wrong reason.
    assert traded["dual-momentum-v1"] == 1  # one position, held at 100%
    assert traded["quality-profitability-v1"] == 10  # the top ten at 10% each
    # The daily sleeve's gate is open, but this history rises monotonically so nothing
    # is 5% below its 20-session average. Entering nothing is the correct answer, not a
    # suppressed one — `test_the_daily_sleeve_trades_when_a_name_is_oversold` shows the
    # same gate producing orders once a dip exists.
    assert traded["short-term-mean-reversion-v1"] == 0
    # All five sleeves were still evaluated and marked; only *trading* is gated.
    assert all(
        EvaluationStore(tmp_path / "eval" / f"{cfg.name}.sqlite3").official_observations()
        for cfg in members
    )


def test_off_cadence_the_monthly_sleeves_propose_nothing(
    plan: cc.ChallengerPlan, history, tmp_path: Path, symbols
) -> None:
    """The gate suppresses the rebalance without suppressing the evaluation."""
    from schwab_trader.agent import MarketContext

    sec_store = _sec_store(tmp_path, symbols)
    strategy = strategy_registry.reconstruct(
        next(s for s in plan.specs if s.name == "dual-momentum-v1").definition,
        list(next(s for s in plan.specs if s.name == "dual-momentum-v1").universe),
        resources=strategy_registry.StrategyResources(
            history=history, benchmark_history=history["SPY"], store=sec_store
        ),
    )
    ordinary = mc.session_bounds_utc(date(2026, 8, 28))[1]
    context = MarketContext(
        now=ordinary,
        cash=Decimal("10000"),
        positions={},
        quotes={s: _quote(s, CLOSE_PRICE, quote_time=ordinary) for s in strategy.universe},
        equity=Decimal("10000"),
    )

    assert strategy.decide(context) == []
    # The decision itself is still computed and still auditable.
    assert strategy.evaluate(context).target is not None


def test_the_daily_sleeve_trades_when_a_name_is_oversold(
    tmp_path: Path, plan: cc.ChallengerPlan, history, symbols
) -> None:
    """The daily gate is genuinely open every session, not merely never consulted."""
    from schwab_trader.agent import MarketContext

    spec = next(s for s in plan.specs if s.name == "short-term-mean-reversion-v1")
    target = spec.universe[0]
    # A steeply rising series, so the 200-session average sits far below the current
    # price and a dip can be well under the 20-session average while the long-term
    # uptrend is still intact. On the gently rising fixture history the same dip would
    # break the 200-session average instead, which is an *exit*, not an entry.
    dipped = {symbol: _bars(symbol, slope=Decimal("1.0")) for symbol in spec.universe}
    bars = list(dipped[target])
    gapped = bars[-1].close * Decimal("0.89")
    bars[-1] = bars[-1].model_copy(
        update={"close": gapped, "open": gapped, "high": gapped, "low": gapped}
    )
    dipped[target] = bars

    strategy = strategy_registry.reconstruct(
        spec.definition,
        list(spec.universe),
        resources=strategy_registry.StrategyResources(
            history=dipped,
            # SPY is not in the large-cap universe, and this sleeve ranks cross-
            # sectionally rather than against a benchmark series, so the registry's
            # benchmark history is supplied and unused.
            benchmark_history=[],
            store=_sec_store(tmp_path, symbols),
        ),
    )
    context = MarketContext(
        now=SIGNAL_CLOSE_UTC,
        cash=Decimal("10000"),
        positions={},
        quotes={s: _quote(s, CLOSE_PRICE) for s in strategy.universe},
        equity=Decimal("10000"),
    )

    evaluation = strategy.evaluate(context)

    assert evaluation.ok
    assert evaluation.entered == (target,)
    # Only the dipped name qualified: the gate is open every session, but the entry
    # condition still has to be met by the evidence.
    assert strategy.decide(context)


def test_one_unready_member_prevents_every_member_from_executing(
    tmp_path: Path, plan: cc.ChallengerPlan, history, symbols
) -> None:
    """All-or-nothing: no cash, position, fill, or observation is touched by anybody."""
    sec_store = _sec_store(tmp_path, symbols)
    _, _, orchestrator, members, _ = _harness(
        tmp_path,
        plan,
        lambda m: _snapshot(m, history, sec_store, unready_member="short-term-mean-reversion-v1"),
    )

    run = orchestrator.run(_due(plan), members, now=NOW_UTC)

    assert run.status is SleeveRunStatus.AWAITING_DATA
    assert run.completed_members == ()
    assert {m.status for m in run.members} == {MemberRunStatus.PENDING}
    for cfg in members:
        engine = orchestrator.engine_factory(cfg)
        assert engine.recent_orders() == []
        assert engine.positions() == []
        assert engine.account().total_cash == contract.STARTING_CASH_PER_SLEEVE
        assert (
            EvaluationStore(tmp_path / "eval" / f"{cfg.name}.sqlite3").official_observations()
            == []
        )


def test_a_missing_t1_opening_bar_stops_the_whole_cohort_and_stays_retryable(
    tmp_path: Path, plan: cc.ChallengerPlan, history, symbols
) -> None:
    """At T's close the T+1 open genuinely does not exist yet, so waiting is correct."""
    sec_store = _sec_store(tmp_path, symbols)

    def snapshot(members):
        every = sorted({symbol for cfg in members for symbol in cfg.universe})
        bars = {symbol: _opening_bar(symbol) for symbol in every if symbol != "IEF"}
        return _snapshot(members, history, sec_store, opening_bars=bars)

    _, _, orchestrator, members, _ = _harness(tmp_path, plan, snapshot)

    run = orchestrator.run(_due(plan), members, now=NOW_UTC)

    assert run.status is SleeveRunStatus.AWAITING_DATA
    preflight = orchestrator.last_preflight
    assert preflight is not None
    assert not preflight.structural  # waiting resolves it; it is not a wiring fault
    assert cohort_preflight.OPENING_BARS_KIND in preflight.kinds
    for cfg in members:
        assert orchestrator.engine_factory(cfg).recent_orders() == []


def test_the_session_retries_successfully_once_the_opening_bar_lands(
    tmp_path: Path, plan: cc.ChallengerPlan, history, symbols
) -> None:
    """The awaited retry is the whole point of `awaiting-data` being non-terminal."""
    sec_store = _sec_store(tmp_path, symbols)
    state: dict[str, object] = {"complete": False}

    def snapshot(members):
        every = sorted({symbol for cfg in members for symbol in cfg.universe})
        bars = {
            symbol: _opening_bar(symbol)
            for symbol in every
            if state["complete"] or symbol != "IEF"
        }
        return _snapshot(members, history, sec_store, opening_bars=bars)

    sleeve_store = _cohort_store(tmp_path)
    cc.create(plan, store=sleeve_store)
    members = tuple(cfg for cfg in sleeve_store.list() if cfg.cohort_id == plan.cohort_id)
    run_store = SleeveRunStore(tmp_path / "runs.sqlite3")

    def provider(configs, session, required_snapshot_id):
        return snapshot(members)

    from schwab_trader.paper import PaperEngine

    orchestrator = SleeveRunOrchestrator(
        run_store=run_store,
        sleeve_store=sleeve_store,
        kill_switch=KillSwitch(tmp_path / "KILL_SWITCH"),
        snapshot_provider=provider,
        universe_resolver=lambda cfg: list(cfg.universe),
        engine_factory=lambda cfg: PaperEngine(
            tmp_path / "paper" / f"{cfg.name}.sqlite3",
            starting_cash=cfg.starting_cash,
            settle_t1=cfg.settlement_t1,
            leverage=cfg.leverage,
        ),
        evaluation_factory=lambda cfg: EvaluationStore(tmp_path / "eval" / f"{cfg.name}.sqlite3"),
    )

    first = orchestrator.run(_due(plan), members, now=NOW_UTC)
    assert first.status is SleeveRunStatus.AWAITING_DATA

    state["complete"] = True
    second = orchestrator.run(_due(plan), members, now=NOW_UTC)

    assert second.status is SleeveRunStatus.COMPLETED
    assert len(second.completed_members) == 5


def test_after_the_deadline_the_waiting_session_becomes_a_durable_missed_result(
    tmp_path: Path, plan: cc.ChallengerPlan, history, symbols
) -> None:
    """Waiting is bounded by the scheduler's existing deadline, not a second timer."""
    sec_store = _sec_store(tmp_path, symbols)

    def snapshot(members):
        every = sorted({symbol for cfg in members for symbol in cfg.universe})
        return _snapshot(
            members,
            history,
            sec_store,
            opening_bars={s: _opening_bar(s) for s in every if s != "IEF"},
        )

    _, _, orchestrator, members, _ = _harness(tmp_path, plan, snapshot)

    waiting = orchestrator.run(_due(plan), members, now=NOW_UTC)
    assert waiting.status is SleeveRunStatus.AWAITING_DATA

    # The next session's decision time has passed, so the signal session is superseded.
    after_deadline = datetime(2026, 9, 2, 17, 0)
    decision = _due(plan, now_et=after_deadline)
    assert decision.status is scheduling.RunStatus.MISSED
    missed = orchestrator.run(decision, members, now=datetime(2026, 9, 2, 21, 0, tzinfo=UTC))

    assert missed.status is SleeveRunStatus.MISSED
    for cfg in members:
        assert orchestrator.engine_factory(cfg).recent_orders() == []


def test_a_completed_session_is_idempotent_across_repeated_invocations(
    tmp_path: Path, plan: cc.ChallengerPlan, history, symbols
) -> None:
    sec_store = _sec_store(tmp_path, symbols)
    _, run_store, orchestrator, members, _ = _harness(
        tmp_path, plan, lambda m: _snapshot(m, history, sec_store)
    )

    first = orchestrator.run(_due(plan), members, now=NOW_UTC)
    orders_after_first = {
        cfg.name: len(orchestrator.engine_factory(cfg).recent_orders()) for cfg in members
    }

    completed = run_store.completed_run_keys(cohort_id=plan.cohort_id)
    second = orchestrator.run(_due(plan, completed=completed), members, now=NOW_UTC)
    third = orchestrator.run(_due(plan, completed=completed), members, now=NOW_UTC)

    assert first.run_id == second.run_id == third.run_id
    assert second.status is third.status is SleeveRunStatus.COMPLETED
    assert {
        cfg.name: len(orchestrator.engine_factory(cfg).recent_orders()) for cfg in members
    } == orders_after_first
    for cfg in members:
        assert (
            len(
                EvaluationStore(
                    tmp_path / "eval" / f"{cfg.name}.sqlite3"
                ).official_observations()
            )
            == 1
        )


def test_the_cohort_agrees_on_one_execution_methodology(
    tmp_path: Path, plan: cc.ChallengerPlan, history, symbols
) -> None:
    sec_store = _sec_store(tmp_path, symbols)
    _, _, orchestrator, members, _ = _harness(
        tmp_path, plan, lambda m: _snapshot(m, history, sec_store)
    )

    resolved = orchestrator.resolve_plan(
        plan.cohort_id, members, scheduling.session_for_date(SIGNAL)
    )

    assert resolved.methodology.key == et.NEXT_OPEN_METHODOLOGY_KEY
    assert resolved.signal_session_date == SIGNAL
    assert resolved.execution_session_date == EXECUTION
    assert resolved.decision_utc < resolved.execution_utc


# --- cohort scoping and reporting --------------------------------------------


def test_two_active_cohorts_are_never_pooled_and_never_implicitly_chosen(
    tmp_path: Path, plan: cc.ChallengerPlan
) -> None:
    """Both experiments are live at once; a report must name which one it means."""
    store = _cohort_store(tmp_path)
    july = "paper-first-2026-07-28"
    store.create(
        "control-cash",
        strategy="hold",
        universe=["SPY"],
        starting_cash=Decimal("10000.00"),
        max_positions=1,
        max_position_fraction=Decimal("1"),
        definition=strategy_registry.make_definition("hold", universe_definition=["SPY"]),
        cohort_id=july,
    )
    cc.create(plan, store=store)

    from schwab_trader import cohort_scope

    known = cohort_scope.known_cohorts(store.list())
    assert set(known) == {july, plan.cohort_id}
    assert set(cohort_lifecycle.active_cohorts(known)) == {july, plan.cohort_id}
    # Scoping is exact: neither cohort's members leak into the other's report.
    challenger_members = cohort_scope.members(store.list(), plan.cohort_id)
    july_members = cohort_scope.members(store.list(), july)
    assert len(challenger_members) == 5
    assert len(july_members) == 1
    assert {cfg.sleeve_id for cfg in challenger_members}.isdisjoint(
        cfg.sleeve_id for cfg in july_members
    )


def test_the_default_cohort_is_ranked_by_persisted_start_session_not_by_id(
    plan: cc.ChallengerPlan,
) -> None:
    resolution = cohort_lifecycle.resolve_default_cohort(
        [
            cohort_lifecycle.CohortRecency("paper-first-2026-07-28", date(2026, 7, 28)),
            cohort_lifecycle.CohortRecency(plan.cohort_id, plan.start_session),
        ]
    )

    assert resolution.selected == plan.cohort_id
    assert resolution.older_active == ("paper-first-2026-07-28",)
    assert resolution.multiple_active
    # The older experiment is still active and still listed; nothing retired it.
    assert set(resolution.active) == {"paper-first-2026-07-28", plan.cohort_id}


def test_a_cohort_without_a_persisted_start_session_is_ambiguous_not_guessed(
    plan: cc.ChallengerPlan,
) -> None:
    resolution = cohort_lifecycle.resolve_default_cohort(
        [
            cohort_lifecycle.CohortRecency("paper-first-2026-07-28", None),
            cohort_lifecycle.CohortRecency(plan.cohort_id, plan.start_session),
        ]
    )

    assert resolution.selected is None
    assert resolution.ambiguous
    assert "no persisted start_session" in resolution.problem


def test_the_created_cohort_persists_a_start_session_for_that_ranking(
    tmp_path: Path, plan: cc.ChallengerPlan
) -> None:
    """Without this the challenger would make the dashboard's default ambiguous."""
    store = _cohort_store(tmp_path)
    cc.create(plan, store=store)

    assert store.cohort_start_session(plan.cohort_id) == SIGNAL


def test_comparison_refuses_to_mix_two_cohorts_observations(
    tmp_path: Path, plan: cc.ChallengerPlan, history, symbols
) -> None:
    """Observations spanning cohorts must select one explicitly, never silently pool."""
    sec_store = _sec_store(tmp_path, symbols)
    _, _, orchestrator, members, _ = _harness(
        tmp_path, plan, lambda m: _snapshot(m, history, sec_store)
    )
    orchestrator.run(_due(plan), members, now=NOW_UTC)

    observations = [
        obs
        for cfg in members
        for obs in EvaluationStore(
            tmp_path / "eval" / f"{cfg.name}.sqlite3"
        ).official_observations()
    ]
    assert observations
    foreign = observations[0].model_copy(update={"cohort_id": "paper-first-2026-07-28"})

    with pytest.raises(ValueError, match="span multiple cohorts"):
        comparison.compare_sleeves([*observations, foreign])

    # Naming the cohort scopes the comparison to it alone.
    scoped = comparison.compare_sleeves([*observations, foreign], cohort_id=plan.cohort_id)
    assert scoped.cohort_id == plan.cohort_id
    # The foreign observation contributed nothing: only this cohort's five sleeves.
    assert len(scoped.sleeves) <= 5


def test_every_observation_carries_the_challenger_cohort_id(
    tmp_path: Path, plan: cc.ChallengerPlan, history, symbols
) -> None:
    sec_store = _sec_store(tmp_path, symbols)
    _, _, orchestrator, members, _ = _harness(
        tmp_path, plan, lambda m: _snapshot(m, history, sec_store)
    )
    orchestrator.run(_due(plan), members, now=NOW_UTC)

    for cfg in members:
        for obs in EvaluationStore(
            tmp_path / "eval" / f"{cfg.name}.sqlite3"
        ).official_observations():
            assert obs.cohort_id == plan.cohort_id


# --- coverage floors ----------------------------------------------------------


def test_the_quality_sleeve_fails_closed_below_its_coverage_floor(
    tmp_path: Path, plan: cc.ChallengerPlan, history, symbols
) -> None:
    """Below 60% eligible names it proposes nothing rather than ranking a partial set."""
    thin = _sec_store(tmp_path, symbols, covered=10)  # 10 of 74 is well under the floor
    spec = next(s for s in plan.specs if s.name == "quality-profitability-v1")
    strategy = strategy_registry.reconstruct(
        spec.definition,
        list(spec.universe),
        resources=strategy_registry.StrategyResources(
            history=history, benchmark_history=history["SPY"], store=thin
        ),
    )

    ranking = strategy.rank(SIGNAL)

    assert not ranking.meets_coverage
    assert ranking.selected == ()
    assert ranking.failure_reason is not None
    assert "fails closed" in ranking.failure_reason
