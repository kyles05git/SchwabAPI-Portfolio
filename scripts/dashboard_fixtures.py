"""Deterministic offline dashboard fixtures.

Builds a :class:`~schwab_trader.dashboard.DashboardData` payload for each cohort state
the UI has to render correctly, using **real** domain objects run through the **real**
operational gate, phase assessment, and view assembly. Nothing here opens a database,
touches the network, reads ``.env``, or consults the wall clock: every scenario pins an
explicit Eastern ``now_et`` instant, so pre-close, awaiting-execution, executed, and
overdue sessions are all reproducible states rather than wall-clock accidents.

Run it to refresh the committed JSON the frontend serves in its fixture mode::

    python scripts/dashboard_fixtures.py

``tests/test_dashboard_fixtures.py`` regenerates these in memory and fails if the
committed files have drifted from the current API contract.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:  # allow ``python scripts/dashboard_fixtures.py``
    sys.path.insert(0, str(_SRC))

from pydantic import BaseModel  # noqa: E402

from schwab_trader import cohort_lifecycle, cohort_review, dashboard, sleeve_detail  # noqa: E402
from schwab_trader import market_calendar as mc  # noqa: E402
from schwab_trader.evaluation import ObservationStatus, OfficialDailyObservation  # noqa: E402
from schwab_trader.experiments import StrategyDefinition  # noqa: E402
from schwab_trader.operational_gate import AccountingArea, OperatorAction  # noqa: E402
from schwab_trader.sleeve_runs import (  # noqa: E402
    MemberRunStatus,
    SleeveRun,
    SleeveRunError,
    SleeveRunMember,
    SleeveRunStatus,
)
from schwab_trader.sleeves import SleeveConfig  # noqa: E402
from schwab_trader.storage.identity import (  # noqa: E402
    LEGACY_NAMESPACE_ID,
    sleeve_scope,
    stable_id,
    stable_sleeve_id,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "frontend" / "src" / "fixtures"

# Deliberately not a real cohort id. Most scenarios depict a cohort that is *running*,
# so they must not borrow an id the lifecycle registry has since marked superseded — the
# selection rule is real code here, and it would correctly refuse to default to one. The
# `historical-cohorts` scenario below uses the real ids, which is where they belong.
_COHORT = "paper-fixture-2026-07-27"
_START = date(2026, 7, 27)
_CREATED = datetime(2026, 7, 24, 15, 0, tzinfo=UTC)
_BENCHMARK = "bench-spy"

# The real superseded cohort and the collection that replaced it.
_HISTORICAL_COHORT = "paper-first-2026-07-27"
_ACTIVE_COHORT = "paper-first-2026-07-28"

# A second *active* cohort for the multiple-active scenarios. Its id carries no date and
# sorts after `_COHORT`, so any fixture that resolves correctly is resolving on the
# persisted start session rather than on the string.
_PILOT_COHORT = "paper-pilot-alpha"
_PILOT_START = date(2026, 6, 15)

# Eastern instants the timing states are pinned to. The regular XNYS close is 16:00 ET
# and the scheduler's default grace period is two hours, so 18:00 ET is the boundary
# between "due, awaiting execution" and "overdue".
_BEFORE_START = datetime(2026, 7, 24, 10, 0)
_START_MORNING = datetime(2026, 7, 27, 9, 28)
_START_JUST_AFTER_CLOSE = datetime(2026, 7, 27, 16, 5)
_START_RUN_RECORDED = datetime(2026, 7, 27, 16, 20)
_START_PAST_GRACE = datetime(2026, 7, 27, 18, 45)
_TWELVE_SESSIONS_IN = datetime(2026, 8, 12, 9, 28)
_THIRTY_SESSIONS_IN = datetime(2026, 9, 4, 18, 0)

# Seven members mirroring the real first cohort's shape.
_MEMBERS: tuple[tuple[str, str], ...] = (
    ("bench-spy", "benchmark"),
    ("trend-large", "trend"),
    ("value-momentum-edgar", "fundamental"),
    ("mean-reversion-mid", "meanrev"),
    ("quality-compounder", "fundamental"),
    ("low-vol-defensive", "trend"),
    ("dual-momentum-etf", "momentum"),
)


# Shared-storage namespace, so ``SleeveConfig.identity`` resolves to the 64-character
# stable id rather than the sleeve name (the local-SQLite fallback). This is the shape
# the redesign has to render: hashes as identity, names as labels.
_NAMESPACE_ID = stable_id("namespace", "official-cohorts")


def _sleeve_id(cohort_id: str | None, name: str) -> str:
    """The stable 64-character identity the shared store would assign."""
    namespace = _NAMESPACE_ID if cohort_id else LEGACY_NAMESPACE_ID
    return stable_sleeve_id(
        source_identity="fixtures",
        scope_key=sleeve_scope(namespace_id=namespace, cohort_id=cohort_id),
        name=name,
    )


def _definition(name: str, strategy: str) -> StrategyDefinition:
    return StrategyDefinition(
        strategy_id=f"{strategy}-v1",
        implementation_name=strategy,
        strategy_version="1.0.0",
        parameters={
            "lookback_days": 90,
            "max_positions": 12,
            "rebalance": "weekly",
            "sleeve": name,
        },
        universe_definition={"preset": "mega-cap"},
        decision_frequency="daily",
        decision_time="16:10",
        benchmark_symbol_or_sleeve=_BENCHMARK,
        data_requirements=["daily-bars", "quotes"],
        long_only=True,
        leverage_allowed=False,
    )


def _config(
    cohort_id: str,
    name: str,
    strategy: str,
    *,
    starting_cash: Decimal = Decimal(10_000),
    corrupt_hash: bool = False,
) -> SleeveConfig:
    """One cohort member.

    ``corrupt_hash`` stores a hash that no longer matches the persisted definition,
    which is exactly the kind of genuine reproducibility defect the gate must surface
    immediately rather than treat as missing evidence.
    """
    definition = _definition(name, strategy)
    return SleeveConfig(
        sleeve_id=_sleeve_id(cohort_id, name),
        namespace_id=_NAMESPACE_ID,
        name=name,
        strategy=strategy,
        universe=[],
        starting_cash=starting_cash,
        max_positions=12,
        max_position_fraction=Decimal("0.15"),
        created_at=_CREATED,
        settlement_t1=True,
        leverage=Decimal(1),
        definition=definition,
        cohort_id=cohort_id,
        configuration_hash="0" * 64 if corrupt_hash else definition.configuration_hash,
        decision_frequency="daily",
        decision_time="16:10",
    )


def _cohort_configs(cohort_id: str = _COHORT) -> list[SleeveConfig]:
    return [_config(cohort_id, name, strategy) for name, strategy in _MEMBERS]


def _sessions(start: date, count: int) -> list[date]:
    """``count`` consecutive XNYS trading sessions from ``start``.

    Uses the canonical calendar, so a fixture never schedules a run on an exchange
    holiday and then presents the closed session as a missing one.
    """
    days: list[date] = []
    cursor = start
    while len(days) < count:
        if mc.is_trading_day(cursor):
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _snapshots(session: date) -> dict[str, str]:
    stamp = session.isoformat()
    return {
        "cohort_snapshot": f"snap-{stamp}",
        "quotes": f"quote-{stamp}",
    }


def _run(
    cohort_id: str,
    session: date,
    members: Sequence[SleeveConfig],
    *,
    status: SleeveRunStatus,
    completed: Sequence[str] | None = None,
    errors: tuple[SleeveRunError, ...] = (),
) -> SleeveRun:
    ids = tuple(config.identity for config in members)
    done = tuple(completed) if completed is not None else ids
    started = datetime.combine(session, datetime.min.time(), tzinfo=UTC).replace(hour=20, minute=10)
    pending = status is SleeveRunStatus.PENDING
    snapshots = _snapshots(session)
    error_by_member = {error.member_id: error for error in errors}
    return SleeveRun(
        run_id=f"run-{cohort_id}-{session.isoformat()}",
        run_key=f"{cohort_id}|{session.isoformat()}",
        cohort_id=cohort_id,
        session_id=f"XNYS|{session.isoformat()}",
        scheduled_for=session,
        expected_members=ids,
        completed_members=() if pending else done,
        snapshot_id=None if pending else snapshots["cohort_snapshot"],
        quote_snapshot_id=None if pending else snapshots["quotes"],
        started_at=started,
        completed_at=None if pending else started + timedelta(minutes=4),
        status=status,
        errors=errors,
        members=tuple(
            SleeveRunMember(
                sleeve_id=identity,
                status=(
                    MemberRunStatus.PENDING
                    if pending
                    else MemberRunStatus.COMPLETED
                    if identity in done
                    else MemberRunStatus.FAILED
                ),
                started_at=None if pending else started,
                completed_at=None if pending or identity not in done else started
                + timedelta(minutes=3),
                error=error_by_member.get(identity),
            )
            for identity in ids
        ),
    )


def _benchmark_value(index: int) -> Decimal:
    """Shared benchmark equity for a session.

    Every sleeve records the *same* value for a session, because a disagreement is
    what ``comparison`` treats as a conflicted, unusable benchmark session.
    """
    drift = Decimal(4) / Decimal(1000)
    wobble = Decimal(index % 7 - 3) / Decimal(1000)
    return (Decimal(10_000) * (Decimal(1) + drift + wobble) ** (index + 1)).quantize(
        Decimal("0.01")
    )


def _observation(
    cohort_id: str,
    config: SleeveConfig,
    session: date,
    index: int,
    *,
    status: ObservationStatus = ObservationStatus.OFFICIAL,
    readiness_reasons: tuple[str, ...] = (),
) -> OfficialDailyObservation:
    """A deterministic observation; each sleeve follows its own repeatable path."""
    seed = sum(ord(char) for char in config.name)
    drift = Decimal(seed % 11 - 5) / Decimal(1000)
    wobble = Decimal((seed + index * 7) % 13 - 6) / Decimal(2000)
    return_pct = drift + wobble
    value = Decimal(10_000) * (Decimal(1) + return_pct * Decimal(index + 1))
    official = status is ObservationStatus.OFFICIAL
    midnight = datetime.combine(session, datetime.min.time(), tzinfo=UTC)
    decision = midnight.replace(hour=20, minute=10)
    return OfficialDailyObservation(
        cohort_id=cohort_id,
        run_id=f"run-{cohort_id}-{session.isoformat()}",
        sleeve_id=config.identity,
        strategy=config.strategy,
        strategy_hash=config.configuration_hash,
        session_date=session,
        decision_time=decision,
        valuation_time=decision + timedelta(minutes=5),
        status=status,
        total_value=value.quantize(Decimal("0.01")) if official else None,
        return_pct=return_pct if official else None,
        benchmark_value=_benchmark_value(index) if official else None,
        exposure=Decimal("0.90") - Decimal(seed % 5) / Decimal(20),
        num_positions=4 + seed % 7,
        turnover=Decimal(120 + seed % 90),
        modeled_cost=Decimal("1.15"),
        num_filled=2 + (seed + index) % 4,
        num_rejected=index % 3,
        quote_coverage=Decimal("1.00") if official else Decimal("0.72"),
        snapshot_ids=_snapshots(session),
        # An official result must be ready with no blocking reasons; an incomplete one
        # must keep its reasons, so a missing session is never fabricated as a zero.
        readiness_ready=official,
        readiness_reasons=() if official else (readiness_reasons or ("quotes:stale",)),
    )


def _dashboard(
    cohort: dashboard.CohortDashboardView, **overrides: object
) -> dashboard.DashboardData:
    """Wrap a cohort view in an otherwise minimal, no-live dashboard payload."""
    payload: dict[str, object] = {
        "generated_at": datetime(2026, 7, 24, 21, 30, tzinfo=UTC),
        "live_enabled": False,
        "benchmark": _BENCHMARK,
        "sleeves": [],
        "curves": [],
        "safety": dashboard.SafetyView(
            kill_engaged=False,
            kill_since=None,
            kill_reason=None,
            trades_today=0,
            realized_pnl=Decimal(0),
            start_equity=None,
            capital_cap=Decimal(2_000),
            daily_loss_limit=Decimal(100),
            max_trades_per_day=5,
            max_order_notional=Decimal(100),
        ),
        "positions": dashboard.PositionsView(
            account="****1234",
            available=False,
            message="Live data disabled for this server.",
        ),
        "summary": dashboard.SummaryView(account="****1234"),
        "orders": dashboard.OrdersView(
            available=False, message="Live data disabled for this server."
        ),
        "cohort": cohort,
    }
    payload.update(overrides)
    return dashboard.DashboardData(**payload)  # type: ignore[arg-type]


def _legacy_rows() -> tuple[list[dashboard.SleeveRow], list[dashboard.EquitySeries]]:
    """A legacy/standalone group: different capital, unmatched lifetime history."""
    specs = [
        ("trend-large", "trend", Decimal("18.42"), 41, 96),
        ("bench-spy", "benchmark", Decimal("11.03"), 41, 0),
        ("value-momentum-edgar", "fundamental", Decimal("6.71"), 41, 58),
        ("mean-reversion-mid", "meanrev", Decimal("-2.15"), 41, 132),
    ]
    rows: list[dashboard.SleeveRow] = []
    curves: list[dashboard.EquitySeries] = []
    ordered = sorted(specs, key=lambda item: item[2], reverse=True)
    for rank, (name, strategy, ret, cycles, trades) in enumerate(ordered, start=1):
        identity = _sleeve_id(None, name)
        is_benchmark = name == _BENCHMARK
        bench_return = next(item[2] for item in specs if item[0] == _BENCHMARK)
        rows.append(
            dashboard.SleeveRow(
                rank=rank,
                sleeve_id=identity,
                name=name,
                strategy=strategy,
                scope=dashboard.SleeveScope.LEGACY,
                cohort_id=None,
                starting_capital=Decimal(5_000),
                cycles=cycles,
                trades=trades,
                value=(Decimal(5_000) * (Decimal(1) + ret / Decimal(100))).quantize(
                    Decimal("0.01")
                ),
                return_pct=ret,
                excess_pct=None if is_benchmark else ret - bench_return,
                excess_benchmark=None if is_benchmark else _BENCHMARK,
                max_drawdown_pct=Decimal("4.80"),
                sharpe=Decimal("0.91"),
                is_benchmark=is_benchmark,
            )
        )
        growth = ret / Decimal(100) / Decimal(20)
        curves.append(
            dashboard.EquitySeries(
                sleeve_id=identity,
                name=name,
                points=[
                    (Decimal(5_000) * (Decimal(1) + growth * Decimal(step))).quantize(
                        Decimal("0.01")
                    )
                    for step in range(21)
                ],
            )
        )
    return rows, curves


def _scenario(
    *,
    now_et: datetime,
    session_count: int,
    cohort_id: str = _COHORT,
    available: Sequence[str] | None = None,
    start_sessions: Mapping[str, date | None] | None = None,
    requested: str | None = None,
    configs: list[SleeveConfig] | None = None,
    partial_session_index: int | None = None,
    corrupt_definition: bool = False,
    duplicate_observation: bool = False,
    include_pending_run: bool = True,
    record_review: bool = False,
) -> dashboard.CohortDashboardView:
    """Assemble one cohort view through the production gate and phase logic.

    ``now_et`` is a naive Eastern instant. Whether the trailing pending run reads as
    upcoming, awaiting execution, or overdue is decided by the real scheduler against
    that instant, never by the fixture.
    """
    members = configs if configs is not None else _cohort_configs(cohort_id)
    if corrupt_definition:
        members = [
            _config(cohort_id, name, strategy, corrupt_hash=index == 1)
            for index, (name, strategy) in enumerate(_MEMBERS)
        ]

    due = _sessions(_START, session_count)
    runs: list[SleeveRun] = []
    observations: list[OfficialDailyObservation] = []

    for index, session in enumerate(due):
        partial = index == partial_session_index
        failing = members[3].identity if partial else None
        completed = [c.identity for c in members if c.identity != failing]
        errors = (
            (
                SleeveRunError(
                    code="data-not-ready",
                    message="Quote snapshot was incomplete at the decision time.",
                    member_id=failing,
                    capability="quotes",
                    retryable=True,
                ),
            )
            if partial
            else ()
        )
        runs.append(
            _run(
                cohort_id,
                session,
                members,
                status=SleeveRunStatus.PARTIAL if partial else SleeveRunStatus.COMPLETED,
                completed=completed,
                errors=errors,
            )
        )
        for config in members:
            incomplete = partial and config.identity == failing
            observations.append(
                _observation(
                    cohort_id,
                    config,
                    session,
                    index,
                    status=ObservationStatus.PARTIAL
                    if incomplete
                    else ObservationStatus.OFFICIAL,
                )
            )

    if include_pending_run:
        # The next scheduled session, persisted as pending. Its timing is whatever the
        # scheduler says at ``now_et`` — upcoming before the close, due inside the
        # grace period, overdue after it — and never an invented state.
        pending_session = _sessions(max(due) + timedelta(days=1) if due else _START, 1)[0]
        runs.append(
            _run(cohort_id, pending_session, members, status=SleeveRunStatus.PENDING, completed=[])
        )

    if duplicate_observation and observations:
        observations.append(observations[0])

    runs.sort(key=lambda run: run.scheduled_for, reverse=True)
    names = sorted(available) if available is not None else [cohort_id]
    # Persisted start sessions, exactly as the manifest would supply them. The scenario's
    # own cohort starts on `_START` unless it says otherwise; a scenario with a second
    # active cohort has to declare that one's start too, because two active cohorts with
    # no ordering metadata is the ambiguous case and the rule refuses to guess through it.
    starts: dict[str, date | None] = {cohort_id: _START}
    if start_sessions is not None:
        starts.update(start_sessions)
    # The production selection rule, not a hand-built stand-in: which cohort is the
    # default, which are offered in the picker, and which are historical is exactly what
    # these fixtures exist to show the frontend.
    selection = dashboard.cohort_selection(requested, names, start_sessions=starts)
    assert selection.selected == cohort_id, (
        f"fixture cohort {cohort_id} is not what the selection rule resolves"
    )
    # Always built, even when nothing has been recorded: production always reads the
    # review store, so a fixture that omitted the section would show the frontend a state
    # the server never sends. ``record_review`` decides whether anything is *in* it.
    review = _recorded_review(
        cohort_id, members, runs, observations, now_et, record=record_review
    )
    return dashboard.assemble_cohort_view(
        selected=cohort_id,
        selection=selection,
        configs=members,
        runs=runs,
        observations=observations,
        benchmark=_BENCHMARK,
        now_et=now_et,
        review=review,
    )


class _MemoryReviewStore:
    """In-memory review repository with the real append-only revision rule.

    The fixtures must not open a database, but they also must not hand-build a
    :class:`~schwab_trader.cohort_review.CohortReview`: what the frontend has to render
    is whatever the real service and reducer produce. This is the smallest thing that
    lets both hold — the same insert-or-supersede semantics as the SQL adapter, with a
    list instead of a table.
    """

    def __init__(self) -> None:
        self._checks: list[cohort_review.AccountingCheck] = []
        self._notes: list[cohort_review.ReviewNote] = []
        self._decisions: list[cohort_review.SleeveDecision] = []

    def record_check(
        self, intent: cohort_review.CheckIntent, *, allow_supersede: bool
    ) -> cohort_review.CheckOutcome:
        prior = [
            item
            for item in self._checks
            if item.observation_key == intent.observation_key and item.area is intent.area
        ]
        latest = max(prior, key=lambda item: item.revision, default=None)
        if latest is not None:
            if intent.matches(latest):
                return cohort_review.CheckOutcome(cohort_review.WriteStatus.UNCHANGED, latest)
            if not allow_supersede:
                raise cohort_review.ReviewConflictError("already recorded")
        revision = 0 if latest is None else latest.revision + 1
        record = cohort_review.AccountingCheck(
            entry_id=cohort_review.check_entry_id(
                intent.cohort_id, intent.observation_key, intent.area, revision
            ),
            cohort_id=intent.cohort_id,
            sleeve_id=intent.sleeve_id,
            observation_key=intent.observation_key,
            session_date=intent.session_date,
            area=intent.area,
            finding=intent.finding,
            summary=intent.summary,
            explanation=intent.explanation,
            recorded_at=intent.recorded_at,
            recorded_by=intent.recorded_by,
            revision=revision,
            supersedes=None if latest is None else latest.entry_id,
        )
        self._checks.append(record)
        status = (
            cohort_review.WriteStatus.RECORDED
            if latest is None
            else cohort_review.WriteStatus.SUPERSEDED
        )
        return cohort_review.CheckOutcome(status, record)

    def add_note(self, intent: cohort_review.NoteIntent) -> cohort_review.NoteOutcome:
        note_id = cohort_review.note_entry_id(
            intent.cohort_id, intent.sleeve_id, intent.observation_key, intent.note
        )
        existing = next((item for item in self._notes if item.note_id == note_id), None)
        if existing is not None:
            return cohort_review.NoteOutcome(cohort_review.WriteStatus.UNCHANGED, existing)
        record = cohort_review.ReviewNote(
            note_id=note_id,
            cohort_id=intent.cohort_id,
            sleeve_id=intent.sleeve_id,
            observation_key=intent.observation_key,
            note=intent.note,
            recorded_at=intent.recorded_at,
            recorded_by=intent.recorded_by,
        )
        self._notes.append(record)
        return cohort_review.NoteOutcome(cohort_review.WriteStatus.RECORDED, record)

    def record_decision(
        self, intent: cohort_review.DecisionIntent, *, allow_supersede: bool
    ) -> cohort_review.DecisionOutcome:
        prior = [item for item in self._decisions if item.sleeve_id == intent.sleeve_id]
        latest = max(prior, key=lambda item: item.revision, default=None)
        if latest is not None:
            if intent.matches(latest):
                return cohort_review.DecisionOutcome(
                    cohort_review.WriteStatus.UNCHANGED, latest
                )
            if not allow_supersede:
                raise cohort_review.ReviewConflictError("already recorded")
        revision = 0 if latest is None else latest.revision + 1
        record = cohort_review.SleeveDecision(
            decision_id=cohort_review.decision_entry_id(
                intent.cohort_id, intent.sleeve_id, revision
            ),
            cohort_id=intent.cohort_id,
            sleeve_id=intent.sleeve_id,
            action=intent.action,
            rationale=intent.rationale,
            recorded_at=intent.recorded_at,
            recorded_by=intent.recorded_by,
            revision=revision,
            supersedes=None if latest is None else latest.decision_id,
        )
        self._decisions.append(record)
        status = (
            cohort_review.WriteStatus.RECORDED
            if latest is None
            else cohort_review.WriteStatus.SUPERSEDED
        )
        return cohort_review.DecisionOutcome(status, record)

    def checks(self, cohort_id: str) -> list[cohort_review.AccountingCheck]:
        return [item for item in self._checks if item.cohort_id == cohort_id]

    def notes(self, cohort_id: str) -> list[cohort_review.ReviewNote]:
        return [item for item in self._notes if item.cohort_id == cohort_id]

    def decisions(self, cohort_id: str) -> list[cohort_review.SleeveDecision]:
        return [item for item in self._decisions if item.cohort_id == cohort_id]


_REVIEW_AT = datetime(2026, 9, 4, 22, 30, tzinfo=UTC)


def _recorded_review(
    cohort_id: str,
    members: Sequence[SleeveConfig],
    runs: Sequence[SleeveRun],
    observations: Sequence[OfficialDailyObservation],
    now_et: datetime,
    *,
    record: bool,
) -> cohort_review.CohortReview:
    """The cohort's review, produced by the real service, reducer, and validation.

    With ``record`` false the review is genuinely empty — the state every cohort is in
    before anyone has looked at it, and the one the panel must not confuse with an
    unreadable store.

    With ``record`` true it is deliberately not a clean sweep. It contains one explained
    difference, one difference that is still outstanding, and one decision that was
    corrected, because those are the states the panel exists to distinguish and a fixture
    that only shows the happy path proves nothing about the others.
    """
    store = _MemoryReviewStore()
    service = cohort_review.CohortReviewService(store)
    context = cohort_review.build_context(
        cohort_id=cohort_id,
        configs=list(members),
        runs=list(runs),
        observations=list(observations),
        now_et=now_et,
        start_session=_START,
    )
    if not record:
        return service.review(context)
    keys = sorted(context.observations)
    for index, key in enumerate(keys):
        for area in cohort_review.REQUIRED_AREAS:
            difference = index == 3 and area is AccountingArea.CASH
            outstanding = index == 11 and area is AccountingArea.VALUATION
            service.record_check(
                context,
                observation_key=key,
                area=area,
                finding=(
                    cohort_review.ReviewFinding.DIFFERENCE
                    if difference or outstanding
                    else cohort_review.ReviewFinding.MATCHED
                ),
                summary=(
                    "Settled cash trailed the modeled balance by 0.04 after a partial fill."
                    if difference
                    else "Recorded equity differs from the recomputed valuation by 0.01."
                    if outstanding
                    else None
                ),
                explanation=(
                    "Expected: the T+1 settlement model releases the cash on the next "
                    "session, and the next session's record shows it settled."
                    if difference
                    else None
                ),
                recorded_at=_REVIEW_AT,
            )
    service.add_note(
        context,
        note=(
            "Reviewed every official session against the recorded snapshots. One "
            "outstanding valuation difference is still being traced to its quote source."
        ),
        recorded_at=_REVIEW_AT,
    )
    for config in members:
        service.record_decision(
            context,
            sleeve_id=config.identity,
            action=OperatorAction.KEEP,
            rationale=(
                "Operationally sound over 30 sessions: complete evidence, reproducible "
                "definition, no unexplained accounting state. Research disposition only."
            ),
            recorded_at=_REVIEW_AT,
        )
    # One correction, so the panel has a superseded decision to render beside a current
    # one. The original stays in the record; it is not replaced.
    service.record_decision(
        context,
        sleeve_id=members[3].identity,
        action=OperatorAction.MODIFY,
        rationale=(
            "Turnover is high enough that the modeled cost dominates the result. Revisit "
            "the rebalance cadence before the next cohort. Research disposition only."
        ),
        recorded_at=_REVIEW_AT + timedelta(hours=1),
        allow_supersede=True,
    )
    return service.review(context)


# --- Scenarios --------------------------------------------------------------


def no_cohort() -> dashboard.DashboardData:
    """Nothing persisted yet: the cohort view has to say so, not render an empty shell."""
    rows, curves = _legacy_rows()
    return _dashboard(
        dashboard.CohortDashboardView(
            message="No persisted paper cohorts are available.",
            selection=dashboard.CohortSelectionView(),
        ),
        sleeves=rows,
        curves=curves,
    )


def scheduled() -> dashboard.DashboardData:
    """Three days before the start: a future session, zero observations. Scheduled."""
    rows, curves = _legacy_rows()
    view = _scenario(now_et=_BEFORE_START, session_count=0)
    return _dashboard(view, sleeves=rows, curves=curves)


def pre_close() -> dashboard.DashboardData:
    """09:28 ET on the start date itself — the state that used to read as a failure.

    The July 27 run is correctly persisted as pending and its decision time is the
    16:00 ET close, which has not happened. Nothing is due, nothing is missing, and no
    snapshot lineage or comparison report is owed yet.
    """
    rows, curves = _legacy_rows()
    view = _scenario(now_et=_START_MORNING, session_count=0)
    return _dashboard(view, sleeves=rows, curves=curves)


def awaiting_execution() -> dashboard.DashboardData:
    """16:05 ET on the start date: the session closed and the runner has not run yet."""
    rows, curves = _legacy_rows()
    view = _scenario(now_et=_START_JUST_AFTER_CLOSE, session_count=0)
    return _dashboard(view, sleeves=rows, curves=curves)


def run_late() -> dashboard.DashboardData:
    """18:45 ET on the start date: past the two-hour grace period with no run."""
    rows, curves = _legacy_rows()
    view = _scenario(now_et=_START_PAST_GRACE, session_count=0)
    return _dashboard(view, sleeves=rows, curves=curves)


def first_session_complete() -> dashboard.DashboardData:
    """The first session ran to completion after its close: one completed due session."""
    rows, curves = _legacy_rows()
    view = _scenario(now_et=_START_RUN_RECORDED, session_count=1)
    return _dashboard(view, sleeves=rows, curves=curves)


def collecting() -> dashboard.DashboardData:
    """Twelve clean sessions, no incidents, the thirteenth still upcoming."""
    rows, curves = _legacy_rows()
    view = _scenario(now_et=_TWELVE_SESSIONS_IN, session_count=12)
    return _dashboard(view, sleeves=rows, curves=curves)


def collecting_partial() -> dashboard.DashboardData:
    """Twelve sessions where one run completed only part of its membership."""
    rows, curves = _legacy_rows()
    view = _scenario(now_et=_TWELVE_SESSIONS_IN, session_count=12, partial_session_index=8)
    return _dashboard(view, sleeves=rows, curves=curves)


def review_ready() -> dashboard.DashboardData:
    """Thirty due sessions: the formal review is owed but no evidence is recorded."""
    rows, curves = _legacy_rows()
    view = _scenario(now_et=_THIRTY_SESSIONS_IN, session_count=30)
    return _dashboard(view, sleeves=rows, curves=curves)


def review_recorded() -> dashboard.DashboardData:
    """Thirty due sessions with the accounting review and decisions actually recorded.

    The counterpart to ``review-ready``: same evidence, but a human has been through it.
    Rendered from real records produced by the real service, so it shows coverage, an
    explained difference, an outstanding one, a note, and a superseded decision beside
    the decision that replaced it. The gate still fails — one difference is deliberately
    left unexplained — which is exactly what recording an honest review looks like.
    """
    rows, curves = _legacy_rows()
    view = _scenario(now_et=_THIRTY_SESSIONS_IN, session_count=30, record_review=True)
    return _dashboard(view, sleeves=rows, curves=curves)


def passed() -> dashboard.DashboardData:
    """Thirty sessions with the gate satisfied.

    The dashboard never supplies accounting or operator evidence, so the real gate
    cannot report ``pass`` from this collector. This fixture therefore overrides the
    gate view directly to exercise the *rendering* of a passed review, and leaves the
    authorization fields false exactly as the contract guarantees.
    """
    rows, curves = _legacy_rows()
    view = _scenario(now_et=_THIRTY_SESSIONS_IN, session_count=30)
    gate = view.operational_gate
    gate.status = "pass"
    gate.summary = (
        "The cohort passed the operational-usefulness gate; this is not evidence of "
        "investment alpha and does not authorize live trading."
    )
    gate.operationally_useful = True
    for rule in gate.rules:
        rule.status = "pass"
        rule.awaiting_evidence = False
        rule.presentation = "healthy"
        rule.reason = f"{rule.label} met its configured requirement."
    if view.phase is not None:
        view.phase.phase = "passed"
        view.phase.headline = "Operational review passed"
        view.phase.next_action = "Review the passed evidence; this does not authorize live trading."
    return _dashboard(view, sleeves=rows, curves=curves)


def failed() -> dashboard.DashboardData:
    """Thirty sessions with genuine defects: a duplicate observation and a bad hash."""
    rows, curves = _legacy_rows()
    view = _scenario(
        now_et=_THIRTY_SESSIONS_IN,
        session_count=30,
        corrupt_definition=True,
        duplicate_observation=True,
    )
    return _dashboard(view, sleeves=rows, curves=curves)


def multiple_cohorts() -> dashboard.DashboardData:
    """Two cohorts collecting at once: the newer is shown, and the state is flagged.

    The older cohort's id is deliberately not date-shaped, and it sorts *after* the
    cohort that wins. Both facts matter: recency comes from the persisted start session,
    so an id that carries no date ranks perfectly well and an id that sorts later does
    not thereby win. Neither cohort is retired by rendering this screen — the warning
    exists precisely because that decision is still owed to a human.
    """
    rows, curves = _legacy_rows()
    view = _scenario(
        now_et=_TWELVE_SESSIONS_IN,
        session_count=12,
        available=[_COHORT, _PILOT_COHORT],
        start_sessions={_PILOT_COHORT: _PILOT_START},
    )
    assert view.selection.selected == _COHORT
    assert view.selection.multiple_active is True
    assert view.selection.older_active == [_PILOT_COHORT]
    return _dashboard(view, sleeves=rows, curves=curves)


def ambiguous_cohorts() -> dashboard.DashboardData:
    """Two active cohorts that began on the same session: no default, and it says so.

    There is no newest cohort here, so the view shows none at all. Picking either one
    would put a coin-flip on screen dressed as the current experiment, which is the exact
    defect this fixture exists to keep fixed.
    """
    rows, curves = _legacy_rows()
    names = sorted([_COHORT, _PILOT_COHORT])
    selection = dashboard.cohort_selection(
        None,
        names,
        start_sessions={_COHORT: _START, _PILOT_COHORT: _START},
    )
    assert selection.selected is None
    assert selection.ambiguous is True
    view = dashboard.CohortDashboardView(
        message=selection.ambiguity_reason,
        selection=selection,
    )
    return _dashboard(view, sleeves=rows, curves=curves)


def historical_cohorts() -> dashboard.DashboardData:
    """The real supersession: July 27 withdrawn, July 28 collecting.

    Two states in one payload. Without a request the default resolves to the active
    cohort even though the superseded one sorts first, and the superseded cohort is
    offered only through the collapsed Historical Cohorts section.
    """
    rows, curves = _legacy_rows()
    names = [_HISTORICAL_COHORT, _ACTIVE_COHORT]
    view = _scenario(
        now_et=_TWELVE_SESSIONS_IN,
        session_count=12,
        cohort_id=_ACTIVE_COHORT,
        available=names,
    )
    assert view.selection.selected == _ACTIVE_COHORT
    assert [item.cohort_id for item in view.selection.historical] == [_HISTORICAL_COHORT]
    return _dashboard(view, sleeves=rows, curves=curves)


def historical_cohort_selected() -> dashboard.DashboardData:
    """The superseded cohort opened deliberately, terminal `partial (6/7)` and all.

    An explicit request is honoured in full — this is how the incident stays auditable —
    and the view still says, above every figure, that the record is closed.
    """
    rows, curves = _legacy_rows()
    view = _scenario(
        now_et=_TWELVE_SESSIONS_IN,
        session_count=1,
        cohort_id=_HISTORICAL_COHORT,
        available=[_HISTORICAL_COHORT, _ACTIVE_COHORT],
        requested=_HISTORICAL_COHORT,
        partial_session_index=0,
        include_pending_run=False,
    )
    assert view.selection.selected_is_historical is True
    return _dashboard(view, sleeves=rows, curves=curves)


def duplicate_names() -> dashboard.DashboardData:
    """`trend-large` and `bench-spy` exist in both the official cohort and legacy state.

    The two groups must never be co-ranked, and the legacy group's excess must never be
    measured against the official cohort's ``bench-spy``.
    """
    legacy_rows, legacy_curves = _legacy_rows()
    cohort_configs = _cohort_configs()
    official_rows = [
        dashboard.SleeveRow(
            rank=rank,
            sleeve_id=config.identity,
            name=config.name,
            strategy=config.strategy,
            scope=dashboard.SleeveScope.OFFICIAL_COHORT,
            cohort_id=_COHORT,
            starting_capital=config.starting_cash,
            cycles=0,
            trades=0,
            value=config.starting_cash,
            return_pct=Decimal(0),
            excess_pct=None if config.name == _BENCHMARK else Decimal(0),
            excess_benchmark=None if config.name == _BENCHMARK else _BENCHMARK,
            max_drawdown_pct=Decimal(0),
            sharpe=None,
            is_benchmark=config.name == _BENCHMARK,
        )
        for rank, config in enumerate(cohort_configs, start=1)
    ]
    view = _scenario(now_et=_BEFORE_START, session_count=0)
    return _dashboard(view, sleeves=official_rows + legacy_rows, curves=legacy_curves)


# --- Sleeve detail (GET /api/sleeve) ----------------------------------------
#
# The drill-down's own fixtures. Kept in a separate map because they are a different
# contract served by a different, lazily requested route — the whole point of that route
# is that this record never rides along with the dashboard payload.
#
# Two of these deliberately share the display name `trend-large`: one in the active
# cohort and one in the superseded cohort it replaced. That collision is real, and a view
# that resolved sleeves by name would conflate them, so the fixtures make it renderable.

_DETAIL_GENERATED_AT = datetime(2026, 8, 12, 21, 30, tzinfo=UTC)
_DETAIL_SLEEVE = "trend-large"
_DETAIL_STRATEGY = "trend"


def _detail_positions(
    specs: Sequence[tuple[str, int, str]],
) -> tuple[list[sleeve_detail.SleevePositionRow], Decimal]:
    """Position rows plus their total cost basis, weighted exactly as the API weights them.

    Mirrors the collector's arithmetic rather than hand-writing plausible numbers, so a
    fixture can never drift into showing weights the real endpoint would not produce.
    """
    total = sum((Decimal(cost) * quantity for _, quantity, cost in specs), Decimal(0))
    rows = [
        sleeve_detail.SleevePositionRow(
            symbol=symbol,
            quantity=quantity,
            average_cost=Decimal(cost),
            cost_basis=Decimal(cost) * quantity,
            cost_basis_weight=(Decimal(cost) * quantity) / total if total else None,
        )
        for symbol, quantity, cost in specs
    ]
    return rows, total


def _detail_equity(
    sessions: Sequence[date], *, base: Decimal, step: Decimal
) -> list[sleeve_detail.EquityPointView]:
    """A deterministic official equity series, oldest-first as the endpoint returns it."""
    return [
        sleeve_detail.EquityPointView(
            as_of=datetime.combine(session, datetime.min.time(), tzinfo=UTC).replace(
                hour=20, minute=15
            ),
            session_date=session,
            total_value=(base + step * index).quantize(Decimal("0.01")),
            source="official-observation",
        )
        for index, session in enumerate(sessions)
    ]


def _detail_page(returned: int, available: int) -> sleeve_detail.PageInfo:
    return sleeve_detail.PageInfo(
        limit=sleeve_detail.DEFAULT_PAGE_LIMIT,
        offset=0,
        returned=returned,
        available=available,
        has_more=returned < available,
    )


def _at(session: date, *, hour: int, minute: int) -> datetime:
    return datetime.combine(session, datetime.min.time(), tzinfo=UTC).replace(
        hour=hour, minute=minute
    )


def active_cohort_sleeve() -> sleeve_detail.SleeveDetail:
    """An active `paper-first-2026-07-28` member with positions, history, and lineage."""
    config = _config(_ACTIVE_COHORT, _DETAIL_SLEEVE, _DETAIL_STRATEGY)
    sessions = _sessions(date(2026, 7, 28), 5)
    rows, total_basis = _detail_positions(
        [("MSFT", 12, "402.1500"), ("AAPL", 9, "221.4000"), ("NVDA", 4, "118.7500")]
    )
    equity = _detail_equity(sessions, base=Decimal("10000.00"), step=Decimal("38.40"))
    latest = equity[-1]
    # Newest session first, which is how the endpoint pages every history section.
    observations = [
        _observation(_ACTIVE_COHORT, config, session, index)
        for index, session in enumerate(sessions)
    ][::-1]
    return sleeve_detail.SleeveDetail(
        generated_at=_DETAIL_GENERATED_AT,
        identity=sleeve_detail.SleeveIdentityView(
            sleeve_id=config.identity,
            name=config.name,
            original_name=config.name,
            strategy=config.strategy,
            scope=sleeve_detail.SleeveScope.OFFICIAL_COHORT,
            cohort_id=_ACTIVE_COHORT,
            namespace_id=config.namespace_id,
            created_at=config.created_at,
        ),
        strategy=sleeve_detail.SleeveStrategyView(
            available=True,
            strategy=config.strategy,
            strategy_id=f"{_DETAIL_STRATEGY}-v1",
            strategy_version="1.0.0",
            implementation_name=_DETAIL_STRATEGY,
            parameters=dict(_definition(_DETAIL_SLEEVE, _DETAIL_STRATEGY).parameters),
            universe_definition={"preset": "mega-cap"},
            configuration_hash=config.configuration_hash,
            reproducible=True,
            decision_frequency="daily",
            decision_time="16:10",
            benchmark_symbol_or_sleeve=_BENCHMARK,
            data_requirements=["daily-bars", "quotes"],
            long_only=True,
            leverage_allowed=False,
        ),
        capital=sleeve_detail.SleeveCapitalView(
            starting_capital=config.starting_cash,
            settlement_model="t+1",
            leverage=Decimal(1),
            max_positions=config.max_positions,
            max_position_fraction=config.max_position_fraction,
        ),
        positions=sleeve_detail.SleevePositionsView(
            available=True,
            as_of=_DETAIL_GENERATED_AT,
            rows=rows,
            total_cost_basis=total_basis,
        ),
        cash=sleeve_detail.SleeveCashView(
            available=True,
            as_of=_DETAIL_GENERATED_AT,
            settled_cash=Decimal("2841.55"),
            unsettled_cash=Decimal("418.90"),
            total_cash=Decimal("3260.45"),
            realized_pnl=Decimal("64.20"),
            starting_capital=config.starting_cash,
            buying_power=Decimal("2841.55"),
            opened_at=config.created_at,
        ),
        recorded_valuation=sleeve_detail.RecordedValuationView(
            available=True,
            source="official-observation",
            as_of=latest.as_of,
            session_date=latest.session_date,
            total_equity=latest.total_value,
            return_pct=Decimal("0.0154"),
            return_pct_basis="ratio",
            benchmark_value=Decimal("10218.40"),
            status="official",
        ),
        performance=sleeve_detail.SleevePerformanceView(
            available=True,
            as_of=latest.as_of,
            first_recorded_at=equity[0].as_of,
            cycles=len(sessions),
            trades_filled=11,
            starting_capital=config.starting_cash,
            latest_value=latest.total_value,
            total_return_pct=Decimal("1.54"),
            realized_pnl=Decimal("64.20"),
            max_drawdown_pct=Decimal("0.62"),
            sharpe=Decimal("1.24"),
        ),
        equity_history=sleeve_detail.EquityHistoryView(
            available=True,
            source="official-observation",
            page=_detail_page(len(equity), len(equity)),
            points=equity,
        ),
        cycles=sleeve_detail.CyclesView(
            available=True,
            page=_detail_page(len(sessions), len(sessions)),
            rows=[
                sleeve_detail.CycleRowView(
                    cycle_id=len(sessions) - index,
                    as_of=point.as_of,
                    strategy=config.strategy,
                    num_proposals=3,
                    num_filled=2,
                    num_rejected=1,
                    cash=Decimal("2841.55"),
                    positions_value=point.total_value - Decimal("3260.45"),
                    total_value=point.total_value,
                    realized_pnl=Decimal("64.20"),
                    unrealized_pnl=Decimal("52.10"),
                    return_pct=Decimal("1.54"),
                )
                for index, point in enumerate(equity[::-1])
            ],
        ),
        simulated_orders=sleeve_detail.SimulatedOrdersView(
            available=True,
            page=_detail_page(3, 3),
            rows=[
                sleeve_detail.SimulatedOrderRowView(
                    paper_order_id=31,
                    as_of=latest.as_of,
                    side="BUY",
                    symbol="NVDA",
                    quantity=4,
                    limit_price=Decimal("119.0000"),
                    status="filled",
                    fill_price=Decimal("118.7500"),
                    filled_at=latest.as_of,
                ),
                # A recorded rejection with its recorded reason: the operator has to be
                # able to see *why* a simulated order did not fill.
                sleeve_detail.SimulatedOrderRowView(
                    paper_order_id=30,
                    as_of=latest.as_of,
                    side="BUY",
                    symbol="TSLA",
                    quantity=6,
                    limit_price=Decimal("240.0000"),
                    status="rejected",
                    reason="insufficient settled paper cash (need 1452.00, have 841.55)",
                ),
                sleeve_detail.SimulatedOrderRowView(
                    paper_order_id=29,
                    as_of=equity[-2].as_of,
                    side="SELL",
                    symbol="AAPL",
                    quantity=2,
                    limit_price=Decimal("219.0000"),
                    status="filled",
                    fill_price=Decimal("219.4500"),
                    filled_at=equity[-2].as_of,
                ),
            ],
        ),
        observations=sleeve_detail.ObservationsView(
            available=True,
            page=_detail_page(len(observations), len(observations)),
            rows=[sleeve_detail.observation_row(entry) for entry in observations],
        ),
        runs=sleeve_detail.RunsView(
            available=True,
            page=_detail_page(len(sessions), len(sessions)),
            rows=[
                sleeve_detail.RunRowView(
                    run_id=f"run-{_ACTIVE_COHORT}-{session.isoformat()}",
                    run_key=f"{_ACTIVE_COHORT}|{session.isoformat()}",
                    session_id=f"XNYS|{session.isoformat()}",
                    scheduled_for=session,
                    status="completed",
                    started_at=_at(session, hour=20, minute=10),
                    completed_at=_at(session, hour=20, minute=14),
                    member_status="completed",
                    member_started_at=_at(session, hour=20, minute=10),
                    member_completed_at=_at(session, hour=20, minute=13),
                    snapshot_id=_snapshots(session)["cohort_snapshot"],
                    quote_snapshot_id=_snapshots(session)["quotes"],
                    data_snapshot_ids=_snapshots(session),
                )
                for session in sessions[::-1]
            ],
        ),
        lineage=sleeve_detail.SleeveLineageView(
            namespace_id=config.namespace_id,
            cohort_id=_ACTIVE_COHORT,
            configuration_hash=config.configuration_hash,
            strategy_hashes=[config.configuration_hash],
            run_ids=[
                f"run-{_ACTIVE_COHORT}-{session.isoformat()}" for session in sessions[::-1]
            ],
            snapshot_ids=_snapshots(sessions[-1]),
            cohort_start_session=sessions[0],
        ),
    )


def superseded_duplicate_name() -> sleeve_detail.SleeveDetail:
    """The *same display name* inside the superseded cohort: a different sleeve entirely.

    Only the stable identity separates this record from ``active-cohort-sleeve``. It must
    render its own positions, cash, and history, be labelled closed, and offer nothing to
    act on.
    """
    config = _config(_HISTORICAL_COHORT, _DETAIL_SLEEVE, _DETAIL_STRATEGY)
    session = date(2026, 7, 27)
    rows, total_basis = _detail_positions([("XOM", 21, "112.8000"), ("KO", 33, "71.2500")])
    valuation_time = _at(session, hour=20, minute=15)
    lifecycle = cohort_lifecycle.status_for(_HISTORICAL_COHORT)
    return sleeve_detail.SleeveDetail(
        generated_at=_DETAIL_GENERATED_AT,
        identity=sleeve_detail.SleeveIdentityView(
            sleeve_id=config.identity,
            name=config.name,
            original_name=config.name,
            strategy=config.strategy,
            scope=sleeve_detail.SleeveScope.OFFICIAL_COHORT,
            cohort_id=_HISTORICAL_COHORT,
            namespace_id=config.namespace_id,
            created_at=_CREATED,
            lifecycle=sleeve_detail.SleeveLifecycleView(
                lifecycle=lifecycle.lifecycle.value,
                historical=True,
                label=lifecycle.label,
                reason=lifecycle.reason,
                superseded_by=lifecycle.superseded_by,
                reference=lifecycle.reference,
            ),
        ),
        strategy=sleeve_detail.SleeveStrategyView(
            available=True,
            strategy=config.strategy,
            strategy_id=f"{_DETAIL_STRATEGY}-v1",
            strategy_version="1.0.0",
            implementation_name=_DETAIL_STRATEGY,
            parameters=dict(_definition(_DETAIL_SLEEVE, _DETAIL_STRATEGY).parameters),
            universe_definition={"preset": "mega-cap"},
            configuration_hash=config.configuration_hash,
            reproducible=True,
            decision_frequency="daily",
            decision_time="16:10",
            benchmark_symbol_or_sleeve=_BENCHMARK,
            data_requirements=["daily-bars", "quotes"],
            long_only=True,
            leverage_allowed=False,
        ),
        capital=sleeve_detail.SleeveCapitalView(
            starting_capital=config.starting_cash,
            settlement_model="t+1",
            leverage=Decimal(1),
            max_positions=config.max_positions,
            max_position_fraction=config.max_position_fraction,
        ),
        positions=sleeve_detail.SleevePositionsView(
            available=True,
            as_of=_DETAIL_GENERATED_AT,
            rows=rows,
            total_cost_basis=total_basis,
        ),
        cash=sleeve_detail.SleeveCashView(
            available=True,
            as_of=_DETAIL_GENERATED_AT,
            settled_cash=Decimal("5289.55"),
            unsettled_cash=Decimal(0),
            total_cash=Decimal("5289.55"),
            realized_pnl=Decimal(0),
            starting_capital=config.starting_cash,
            buying_power=Decimal("5289.55"),
            opened_at=_CREATED,
        ),
        recorded_valuation=sleeve_detail.RecordedValuationView(
            available=True,
            source="official-observation",
            as_of=valuation_time,
            session_date=session,
            total_equity=Decimal("9981.40"),
            return_pct=Decimal("-0.0019"),
            return_pct_basis="ratio",
            status="partial",
            message=(
                "The most recent recorded valuation is not a complete official "
                "observation. Its status is shown beside it."
            ),
        ),
        performance=sleeve_detail.SleevePerformanceView(
            available=True,
            as_of=valuation_time,
            first_recorded_at=valuation_time,
            cycles=1,
            trades_filled=2,
            starting_capital=config.starting_cash,
            latest_value=Decimal("9981.40"),
            total_return_pct=Decimal("-0.19"),
            realized_pnl=Decimal(0),
            max_drawdown_pct=Decimal("0.19"),
            sharpe=None,
        ),
        equity_history=sleeve_detail.EquityHistoryView(
            available=True,
            source="official-observation",
            page=_detail_page(1, 1),
            points=[
                sleeve_detail.EquityPointView(
                    as_of=valuation_time,
                    session_date=session,
                    total_value=Decimal("9981.40"),
                    source="official-observation",
                )
            ],
        ),
        cycles=sleeve_detail.CyclesView(
            available=True,
            page=_detail_page(1, 1),
            rows=[
                sleeve_detail.CycleRowView(
                    cycle_id=1,
                    as_of=valuation_time,
                    strategy=config.strategy,
                    num_proposals=3,
                    num_filled=2,
                    num_rejected=1,
                    cash=Decimal("5289.55"),
                    positions_value=Decimal("4691.85"),
                    total_value=Decimal("9981.40"),
                    realized_pnl=Decimal(0),
                    unrealized_pnl=Decimal("-18.60"),
                    return_pct=Decimal("-0.19"),
                )
            ],
        ),
        simulated_orders=sleeve_detail.SimulatedOrdersView(
            available=True,
            page=_detail_page(2, 2),
            rows=[
                sleeve_detail.SimulatedOrderRowView(
                    paper_order_id=2,
                    as_of=valuation_time,
                    side="BUY",
                    symbol="KO",
                    quantity=33,
                    limit_price=Decimal("71.5000"),
                    status="filled",
                    fill_price=Decimal("71.2500"),
                    filled_at=valuation_time,
                ),
                sleeve_detail.SimulatedOrderRowView(
                    paper_order_id=1,
                    as_of=valuation_time,
                    side="BUY",
                    symbol="XOM",
                    quantity=21,
                    limit_price=Decimal("113.0000"),
                    status="filled",
                    fill_price=Decimal("112.8000"),
                    filled_at=valuation_time,
                ),
            ],
        ),
        observations=sleeve_detail.ObservationsView(
            available=True,
            page=_detail_page(1, 1),
            rows=[
                sleeve_detail.observation_row(
                    _observation(
                        _HISTORICAL_COHORT,
                        config,
                        session,
                        0,
                        status=ObservationStatus.PARTIAL,
                        readiness_reasons=("daily_bars:stale",),
                    )
                )
            ],
        ),
        runs=sleeve_detail.RunsView(
            available=True,
            page=_detail_page(1, 1),
            rows=[
                sleeve_detail.RunRowView(
                    run_id=f"run-{_HISTORICAL_COHORT}-{session.isoformat()}",
                    run_key=f"{_HISTORICAL_COHORT}|{session.isoformat()}",
                    session_id=f"XNYS|{session.isoformat()}",
                    scheduled_for=session,
                    status="partial",
                    started_at=_at(session, hour=20, minute=10),
                    completed_at=_at(session, hour=20, minute=14),
                    member_status="failed",
                    member_started_at=_at(session, hour=20, minute=10),
                    member_error_code="data_deadline_exceeded",
                    member_error_message=(
                        "daily bars for the session were stale past the retry deadline"
                    ),
                    snapshot_id=_snapshots(session)["cohort_snapshot"],
                    quote_snapshot_id=_snapshots(session)["quotes"],
                    data_snapshot_ids=_snapshots(session),
                )
            ],
        ),
        lineage=sleeve_detail.SleeveLineageView(
            namespace_id=config.namespace_id,
            cohort_id=_HISTORICAL_COHORT,
            configuration_hash=config.configuration_hash,
            strategy_hashes=[config.configuration_hash],
            run_ids=[f"run-{_HISTORICAL_COHORT}-{session.isoformat()}"],
            snapshot_ids=_snapshots(session),
            cohort_start_session=session,
        ),
        warnings=[
            f"{lifecycle.label}. The record is shown for review only and offers no actions."
        ],
    )


def _unassigned_config(name: str, *, reproducible: bool) -> SleeveConfig:
    """An unassigned sleeve: no cohort, so legacy or standalone rather than official."""
    definition = _definition(name, "trend") if reproducible else None
    return SleeveConfig(
        sleeve_id=_sleeve_id(None, name),
        namespace_id=LEGACY_NAMESPACE_ID,
        name=name,
        strategy="trend",
        universe=["SPY", "QQQ", "IWM"],
        starting_cash=Decimal(25_000),
        max_positions=8,
        max_position_fraction=Decimal("0.20"),
        created_at=datetime(2026, 3, 2, 14, 0, tzinfo=UTC),
        definition=definition,
        configuration_hash=definition.configuration_hash if definition else "",
    )


def legacy_sleeve() -> sleeve_detail.SleeveDetail:
    """An unassigned sleeve with lifetime history and no versioned definition.

    The pre-cohort case: real recorded cycles, no official observations, and parameters
    that were never captured — so it is labelled non-reproducible rather than shown with
    invented values, and its equity history falls back to its own valuation cycles.
    """
    config = _unassigned_config("legacy-trend", reproducible=False)
    stamps = [
        datetime(2026, 6, 1, 20, 15, tzinfo=UTC) + timedelta(days=index * 7)
        for index in range(4)
    ]
    values = [
        Decimal("25000.00"),
        Decimal("25418.30"),
        Decimal("25190.75"),
        Decimal("25864.10"),
    ]
    rows, total_basis = _detail_positions([("SPY", 30, "551.2000"), ("QQQ", 18, "482.6500")])
    return sleeve_detail.SleeveDetail(
        generated_at=_DETAIL_GENERATED_AT,
        identity=sleeve_detail.SleeveIdentityView(
            sleeve_id=config.identity,
            name=config.name,
            original_name=config.name,
            strategy=config.strategy,
            scope=sleeve_detail.SleeveScope.LEGACY,
            namespace_id=config.namespace_id,
            created_at=config.created_at,
        ),
        strategy=sleeve_detail.SleeveStrategyView(
            available=True,
            message=(
                "This sleeve predates versioned strategy definitions, so its full "
                "parameters were never recorded. It is labelled non-reproducible "
                "rather than shown with invented values."
            ),
            strategy=config.strategy,
            universe=list(config.universe),
            reproducible=False,
        ),
        capital=sleeve_detail.SleeveCapitalView(
            starting_capital=config.starting_cash,
            settlement_model="instant",
            leverage=Decimal(1),
            max_positions=config.max_positions,
            max_position_fraction=config.max_position_fraction,
        ),
        positions=sleeve_detail.SleevePositionsView(
            available=True,
            as_of=_DETAIL_GENERATED_AT,
            rows=rows,
            total_cost_basis=total_basis,
        ),
        cash=sleeve_detail.SleeveCashView(
            available=True,
            as_of=_DETAIL_GENERATED_AT,
            settled_cash=Decimal("35.30"),
            unsettled_cash=Decimal(0),
            total_cash=Decimal("35.30"),
            realized_pnl=Decimal("412.85"),
            starting_capital=config.starting_cash,
            buying_power=Decimal("35.30"),
            opened_at=config.created_at,
        ),
        recorded_valuation=sleeve_detail.RecordedValuationView(
            available=True,
            source="evaluation-cycle",
            as_of=stamps[-1],
            total_equity=values[-1],
            positions_value=Decimal("25828.80"),
            cash=Decimal("35.30"),
            unrealized_pnl=Decimal("451.40"),
            realized_pnl=Decimal("412.85"),
            return_pct=Decimal("3.46"),
            return_pct_basis="percent",
            message=(
                "No official cohort observation has been recorded, so this is the "
                "sleeve's own last valuation cycle."
            ),
        ),
        performance=sleeve_detail.SleevePerformanceView(
            available=True,
            as_of=stamps[-1],
            first_recorded_at=stamps[0],
            cycles=len(stamps),
            trades_filled=27,
            starting_capital=config.starting_cash,
            latest_value=values[-1],
            total_return_pct=Decimal("3.46"),
            realized_pnl=Decimal("412.85"),
            max_drawdown_pct=Decimal("0.89"),
            sharpe=Decimal("0.81"),
        ),
        equity_history=sleeve_detail.EquityHistoryView(
            available=True,
            source="evaluation-cycle",
            message=(
                "No official cohort observations exist for this sleeve, so its equity "
                "history is drawn from its own recorded valuation cycles."
            ),
            page=_detail_page(len(stamps), len(stamps)),
            points=[
                sleeve_detail.EquityPointView(
                    as_of=stamp, total_value=value, source="evaluation-cycle"
                )
                for stamp, value in zip(stamps, values, strict=True)
            ],
        ),
        cycles=sleeve_detail.CyclesView(
            available=True,
            page=_detail_page(len(stamps), len(stamps)),
            rows=[
                sleeve_detail.CycleRowView(
                    cycle_id=len(stamps) - index,
                    as_of=stamp,
                    strategy=config.strategy,
                    num_proposals=4,
                    num_filled=3,
                    num_rejected=1,
                    cash=Decimal("35.30"),
                    positions_value=value - Decimal("35.30"),
                    total_value=value,
                    realized_pnl=Decimal("412.85"),
                    unrealized_pnl=Decimal("451.40"),
                    return_pct=(
                        (value - config.starting_cash) / config.starting_cash * 100
                    ).quantize(Decimal("0.01")),
                )
                for index, (stamp, value) in enumerate(
                    zip(stamps[::-1], values[::-1], strict=True)
                )
            ],
        ),
        simulated_orders=sleeve_detail.SimulatedOrdersView(
            available=True,
            page=_detail_page(1, 1),
            rows=[
                sleeve_detail.SimulatedOrderRowView(
                    paper_order_id=54,
                    as_of=stamps[-1],
                    side="BUY",
                    symbol="QQQ",
                    quantity=18,
                    limit_price=Decimal("483.0000"),
                    status="filled",
                    fill_price=Decimal("482.6500"),
                    filled_at=stamps[-1],
                )
            ],
        ),
        observations=sleeve_detail.ObservationsView(
            available=True,
            message="No official observations are recorded for this sleeve yet.",
            page=_detail_page(0, 0),
        ),
        runs=sleeve_detail.RunsView(
            available=False,
            message=(
                "This sleeve is not a member of an official cohort, so no scheduled "
                "cohort runs reference it."
            ),
        ),
        lineage=sleeve_detail.SleeveLineageView(namespace_id=config.namespace_id),
    )


def standalone_empty_sleeve() -> sleeve_detail.SleeveDetail:
    """A registered sleeve that has never run: no positions, no cycles, no observations.

    Every empty section must say *why* it is empty. None of this is a zero return.
    """
    config = _unassigned_config("standalone-new", reproducible=True)
    no_paper = (
        "No paper state is recorded for this sleeve yet. Positions and cash appear "
        "after its first valuation cycle; nothing is created by viewing it."
    )
    return sleeve_detail.SleeveDetail(
        generated_at=_DETAIL_GENERATED_AT,
        identity=sleeve_detail.SleeveIdentityView(
            sleeve_id=config.identity,
            name=config.name,
            original_name=config.name,
            strategy=config.strategy,
            scope=sleeve_detail.SleeveScope.STANDALONE,
            namespace_id=config.namespace_id,
            created_at=config.created_at,
        ),
        strategy=sleeve_detail.SleeveStrategyView(
            available=True,
            strategy=config.strategy,
            strategy_id="trend-v1",
            strategy_version="1.0.0",
            implementation_name="trend",
            parameters=dict(_definition("standalone-new", "trend").parameters),
            universe_definition={"preset": "mega-cap"},
            universe=list(config.universe),
            configuration_hash=config.configuration_hash,
            reproducible=True,
            decision_frequency="daily",
            decision_time="16:10",
            benchmark_symbol_or_sleeve=_BENCHMARK,
            data_requirements=["daily-bars", "quotes"],
            long_only=True,
            leverage_allowed=False,
        ),
        capital=sleeve_detail.SleeveCapitalView(
            starting_capital=config.starting_cash,
            settlement_model="instant",
            leverage=Decimal(1),
            max_positions=config.max_positions,
            max_position_fraction=config.max_position_fraction,
        ),
        positions=sleeve_detail.SleevePositionsView(available=False, message=no_paper),
        cash=sleeve_detail.SleeveCashView(available=False, message=no_paper),
        recorded_valuation=sleeve_detail.RecordedValuationView(
            available=False,
            message=(
                "No valuation has been recorded for this sleeve yet, so it has no "
                "equity, return, or P&L to report."
            ),
        ),
        performance=sleeve_detail.SleevePerformanceView(
            available=False,
            message=(
                "This sleeve has recorded no valuation cycles yet, so it has no "
                "performance history. This is not a zero return."
            ),
        ),
        equity_history=sleeve_detail.EquityHistoryView(
            available=False,
            message="No equity values have been recorded for this sleeve yet.",
        ),
        cycles=sleeve_detail.CyclesView(
            available=True,
            message="No valuation cycles are recorded for this sleeve yet.",
            page=_detail_page(0, 0),
        ),
        simulated_orders=sleeve_detail.SimulatedOrdersView(available=False, message=no_paper),
        observations=sleeve_detail.ObservationsView(
            available=True,
            message="No official observations are recorded for this sleeve yet.",
            page=_detail_page(0, 0),
        ),
        runs=sleeve_detail.RunsView(
            available=False,
            message=(
                "This sleeve is not a member of an official cohort, so no scheduled "
                "cohort runs reference it."
            ),
        ),
        lineage=sleeve_detail.SleeveLineageView(
            namespace_id=config.namespace_id,
            configuration_hash=config.configuration_hash,
        ),
        warnings=[no_paper],
    )


def incomplete_record_sleeve() -> sleeve_detail.SleeveDetail:
    """A cohort member whose stores are partly unreadable and whose history has gaps.

    The failure mode this exists to pin: an unreadable paper store and an unreadable
    evaluation history must each degrade to their own explanation while the identity,
    strategy, and capital sections still render. No traceback, no fabricated zero.
    """
    config = _config(_ACTIVE_COHORT, "quality-compounder", "fundamental")
    paper_soft = (
        "The paper store for this sleeve could not be read, so positions and cash "
        "are unavailable. The rest of the record is unaffected."
    )
    eval_soft = (
        "The evaluation history for this sleeve could not be read, so its "
        "observations, cycles, and equity history are unavailable."
    )
    return sleeve_detail.SleeveDetail(
        generated_at=_DETAIL_GENERATED_AT,
        identity=sleeve_detail.SleeveIdentityView(
            sleeve_id=config.identity,
            name=config.name,
            original_name=config.name,
            strategy=config.strategy,
            scope=sleeve_detail.SleeveScope.OFFICIAL_COHORT,
            cohort_id=_ACTIVE_COHORT,
            namespace_id=config.namespace_id,
            created_at=config.created_at,
        ),
        strategy=sleeve_detail.SleeveStrategyView(
            available=True,
            strategy=config.strategy,
            strategy_id="fundamental-v1",
            strategy_version="1.0.0",
            implementation_name="fundamental",
            parameters=dict(_definition("quality-compounder", "fundamental").parameters),
            universe_definition={"preset": "mega-cap"},
            configuration_hash=config.configuration_hash,
            reproducible=True,
            decision_frequency="daily",
            decision_time="16:10",
            benchmark_symbol_or_sleeve=_BENCHMARK,
            data_requirements=["daily-bars", "quotes"],
            long_only=True,
            leverage_allowed=False,
        ),
        capital=sleeve_detail.SleeveCapitalView(
            starting_capital=config.starting_cash,
            settlement_model="t+1",
            leverage=Decimal(1),
            max_positions=config.max_positions,
            max_position_fraction=config.max_position_fraction,
        ),
        positions=sleeve_detail.SleevePositionsView(available=False, message=paper_soft),
        cash=sleeve_detail.SleeveCashView(available=False, message=paper_soft),
        recorded_valuation=sleeve_detail.RecordedValuationView(
            available=False,
            message=(
                "No valuation has been recorded for this sleeve yet, so it has no "
                "equity, return, or P&L to report."
            ),
        ),
        performance=sleeve_detail.SleevePerformanceView(
            available=False,
            message=(
                "The evaluation history for this sleeve could not be read, so its "
                "lifetime performance summary is unavailable."
            ),
        ),
        equity_history=sleeve_detail.EquityHistoryView(available=False, message=eval_soft),
        cycles=sleeve_detail.CyclesView(available=False, message=eval_soft),
        simulated_orders=sleeve_detail.SimulatedOrdersView(available=False, message=paper_soft),
        observations=sleeve_detail.ObservationsView(available=False, message=eval_soft),
        runs=sleeve_detail.RunsView(
            available=True,
            page=_detail_page(0, 0),
            message="No cohort run has referenced this sleeve yet.",
        ),
        lineage=sleeve_detail.SleeveLineageView(
            namespace_id=config.namespace_id,
            cohort_id=_ACTIVE_COHORT,
            configuration_hash=config.configuration_hash,
        ),
        warnings=[eval_soft, paper_soft],
    )


SLEEVE_DETAIL_FIXTURES: dict[str, Callable[[], sleeve_detail.SleeveDetail]] = {
    "active-cohort-sleeve": active_cohort_sleeve,
    "superseded-duplicate-name": superseded_duplicate_name,
    "legacy-sleeve": legacy_sleeve,
    "standalone-empty-sleeve": standalone_empty_sleeve,
    "incomplete-record-sleeve": incomplete_record_sleeve,
}


FIXTURES: dict[str, Callable[[], dashboard.DashboardData]] = {
    "no-cohort": no_cohort,
    "scheduled": scheduled,
    "pre-close": pre_close,
    "awaiting-execution": awaiting_execution,
    "run-late": run_late,
    "first-session-complete": first_session_complete,
    "collecting": collecting,
    "collecting-partial": collecting_partial,
    "review-ready": review_ready,
    "review-recorded": review_recorded,
    "passed": passed,
    "failed": failed,
    "multiple-cohorts": multiple_cohorts,
    "ambiguous-cohorts": ambiguous_cohorts,
    "historical-cohorts": historical_cohorts,
    "historical-cohort-selected": historical_cohort_selected,
    "duplicate-names": duplicate_names,
}


def _encode(model: BaseModel) -> str:
    """Stable pretty JSON: sorted keys and a trailing newline, so diffs are meaningful."""
    return json.dumps(json.loads(model.model_dump_json()), indent=2, sort_keys=True) + "\n"


def build_all() -> dict[str, str]:
    """Every dashboard fixture as pretty-printed JSON, keyed by fixture name."""
    return {name: _encode(builder()) for name, builder in FIXTURES.items()}


def build_all_sleeve_details() -> dict[str, str]:
    """Every sleeve-detail fixture as pretty-printed JSON, keyed by fixture name.

    Filenames are prefixed ``sleeve-detail-`` so the two contracts never collide in the
    one fixture directory the frontend imports from.
    """
    return {
        f"sleeve-detail-{name}": _encode(builder())
        for name, builder in SLEEVE_DETAIL_FIXTURES.items()
    }


def write_all(directory: Path = FIXTURE_DIR) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, payload in (build_all() | build_all_sleeve_details()).items():
        path = directory / f"{name}.json"
        path.write_text(payload, encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    for path in write_all():
        print(f"wrote {path}")
