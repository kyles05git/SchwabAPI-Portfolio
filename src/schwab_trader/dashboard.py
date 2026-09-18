"""Local web dashboard (``schwab-trader serve``): backend for the React frontend.

Pulls together everything the CLI surfaces - a live account summary, positions,
open/recent orders, the sleeve leaderboard + equity curves, strategy validation
verdicts, the notify-and-approve queue, market regime, tax lots, autonomous-safety
status, and a recent-audit tail - and serves it two ways:

- ``GET /api/data`` - the whole :class:`DashboardData` as JSON, consumed by the
  **React + TypeScript + Tailwind frontend** in ``frontend/`` (built with Vite to
  static files this server hosts; Node is needed only at build time).
- ``GET /api/sleeve`` - :mod:`schwab_trader.sleeve_detail`'s read-only record for one
  sleeve, addressed by stable identity. Deliberately a *separate, lazily requested*
  route: the detail record is large, and folding it into ``/api/data`` would make every
  30-second dashboard poll carry a per-sleeve history nobody asked for.
- The **legacy server-rendered page** as an automatic fallback when
  ``frontend/dist`` is absent (a machine without Node still gets a dashboard).

Design constraints (extends task 5):

- **Read-only, with one exception.** The *only* state-changing route is
  ``POST /kill``, which **engages** the kill switch (a safety halt that can only
  *stop* trading, never start it). There is no route that can place, replace,
  cancel, or approve an order, and resuming trading stays on the CLI.
- **Localhost only.** Binds to loopback by default; a non-loopback host is refused
  unless explicitly forced, because the page renders account data.
- **Stdlib server.** The Python side stays dependency-free; static files are served
  with a path-traversal guard and no directory listings.
- **Fails soft.** Local sections always render; network sections (positions, orders,
  regime) degrade to a message when there is no account or the network/auth is down.

Collectors are plain functions so they unit-test offline; the server just calls
them per request.
"""

from __future__ import annotations

import html
import ipaddress
import json
import logging
import sqlite3
import urllib.parse
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError

from schwab_trader import (
    accounts,
    approval,
    benchmark_scope,
    cohort_lifecycle,
    cohort_phase,
    cohort_review,
    comparison,
    data_readiness,
    evaluation,
    evidence_timing,
    experiments,
    history_cache,
    market_calendar,
    operational_gate,
    orders,
    reconciliation,
    safety,
    scheduling,
    signals,
    sleeve_detail,
    sleeve_runs,
    sleeves,
    state,
    taxlots,
    universes,
)
from schwab_trader import auth as oauth
from schwab_trader import client as api
from schwab_trader.sleeve_detail import SleeveScope
from schwab_trader.storage import factory as storage_factory

if TYPE_CHECKING:
    from schwab_trader.config import Settings

logger = logging.getLogger(__name__)

# A client factory yields an authenticated client used as a context manager.
ClientFactory = Callable[[], AbstractContextManager[api.SchwabClient]]

# Order statuses that mean the order is still live at the broker.
_WORKING_STATUSES = frozenset(
    {
        "NEW",
        "ACCEPTED",
        "WORKING",
        "QUEUED",
        "PENDING_ACTIVATION",
        "PENDING_CANCEL",
        "PENDING_REPLACE",
        "AWAITING_MANUAL_REVIEW",
    }
)

# Regime is network-heavy (history for a small universe); recompute at most this often.
_REGIME_TTL = timedelta(minutes=10)
_regime_memo: tuple[datetime, RegimeView] | None = None


# --- Data structures (plain, renderer-agnostic) -----------------------------


class SummaryView(BaseModel):
    account: str = ""
    liquidation_value: Decimal | None = None
    day_pl: Decimal | None = None
    cash_available: Decimal | None = None
    kill_engaged: bool = False


# ``SleeveScope`` is imported above rather than defined here. Sleeves in different
# scopes have different starting capital, start dates, strategy definitions, and evidence
# horizons, so they are ranked separately and never share a benchmark (see
# :func:`collect_sleeves`) — and the leaderboard and the single-sleeve detail view must
# classify a sleeve identically, which one shared enum is the only way to guarantee.


class SleeveRow(BaseModel):
    rank: int
    """Rank *within this row's scope group*, not across the whole leaderboard."""

    sleeve_id: str
    """Stable database identity. Use this to join rows, curves, and cohort evidence."""

    name: str
    """Human-readable sleeve name. Not unique across scopes; disambiguate by id."""

    strategy: str
    scope: SleeveScope
    cohort_id: str | None = None
    starting_capital: Decimal
    cycles: int
    trades: int
    value: Decimal
    return_pct: Decimal
    excess_pct: Decimal | None
    excess_benchmark: str | None = None
    """Name of the benchmark the excess was measured against, or null when none was
    unambiguously resolvable inside this row's scope group."""

    max_drawdown_pct: Decimal
    sharpe: Decimal | None
    is_benchmark: bool
    historical: bool = False
    """True when this row belongs to a superseded cohort. Kept in the payload for audit,
    ordered after the current collection, and never the default comparison."""


class EquitySeries(BaseModel):
    sleeve_id: str
    name: str
    points: list[Decimal]


class SafetyView(BaseModel):
    kill_engaged: bool
    kill_since: datetime | None
    kill_reason: str | None
    trades_today: int
    realized_pnl: Decimal
    start_equity: Decimal | None
    capital_cap: Decimal
    daily_loss_limit: Decimal
    max_trades_per_day: int
    max_order_notional: Decimal


class PositionRow(BaseModel):
    symbol: str
    quantity: Decimal
    settled: Decimal
    average_price: Decimal | None
    market_value: Decimal | None
    day_pl: Decimal | None


class PositionsView(BaseModel):
    account: str
    available: bool
    message: str | None = None
    positions: list[PositionRow] = []
    liquidation_value: Decimal | None = None
    cash_available_for_trading: Decimal | None = None
    cash_available_for_withdrawal: Decimal | None = None


class OrderRow(BaseModel):
    order_id: str
    status: str
    side: str | None
    symbol: str | None
    quantity: Decimal | None
    filled: Decimal | None
    limit_price: Decimal | None
    working: bool


class OrdersView(BaseModel):
    available: bool
    message: str | None = None
    hours: int = 48
    orders: list[OrderRow] = []


class ValidationRow(BaseModel):
    strategy: str
    universe: str
    validated: bool
    pass_rate: float
    min_pass_rate: float
    mean_excess_pct: Decimal | None
    beats_benchmark: bool


class ValidationView(BaseModel):
    rows: list[ValidationRow] = []


class ApprovalRow(BaseModel):
    token: str
    describe: str
    rationale: str | None
    account_tail: str
    expires_in_min: int


class ApprovalsView(BaseModel):
    rows: list[ApprovalRow] = []


class RegimeView(BaseModel):
    available: bool = False
    message: str | None = None
    score: int = 0
    gross_exposure_cap: Decimal = Decimal(0)
    spy_above_200dma: bool = False
    trend_50_over_200: bool = False
    breadth_above_50: bool = False
    calm_volatility: bool = False
    breadth_pct: float = 0.0


class TaxLotRow(BaseModel):
    symbol: str
    quantity: Decimal
    cost_per_share: Decimal
    acquired: date
    days_held: int
    long_term: bool


class TaxLotsView(BaseModel):
    rows: list[TaxLotRow] = []


class AuditRow(BaseModel):
    ts: datetime
    command: str
    event: str
    detail: str | None


class AuditView(BaseModel):
    rows: list[AuditRow] = []


class ReconciliationView(BaseModel):
    completed_at: datetime | None = None
    success: bool | None = None
    orders_seen: int = 0
    transitions: int = 0
    fills_applied: int = 0
    discrepancies: int = 0
    pending_fill_applications: int = 0


class HistoricalCohortView(BaseModel):
    """A cohort retained as evidence: readable on request, never the default.

    Rendered in the collapsed historical section, which is why it carries its own
    explanation — an operator who opens a superseded cohort must be told what they are
    looking at without leaving the page.
    """

    cohort_id: str
    lifecycle: str
    label: str
    reason: str
    superseded_by: str | None = None
    reference: str = ""


class ActiveCohortView(BaseModel):
    """One cohort that is still collecting, with the metadata that ordered it.

    ``start_session`` is the persisted immutable exchange start session — the only thing
    recency is decided on. It is ``None`` when the manifest does not record one, which is
    a reportable gap rather than a licence to fall back to the cohort id.
    """

    cohort_id: str
    start_session: date | None = None
    is_default: bool = False


class CohortSelectionView(BaseModel):
    requested: str | None = None
    selected: str | None = None
    available: list[str] = Field(default_factory=list)
    """Every persisted cohort, historical ones included. Audit never loses a cohort."""

    active: list[str] = Field(default_factory=list)
    """The subset still collecting. This is what the cohort picker offers, and the only
    set the default selection is ever drawn from. Ordered newest-first once recency has
    been established, so the frontend never has to re-derive an order of its own."""

    active_cohorts: list[ActiveCohortView] = Field(default_factory=list)
    """The same set with its start sessions, for the multiple-active warning."""

    historical: list[HistoricalCohortView] = Field(default_factory=list)
    """Superseded/retired cohorts, for the collapsed historical section."""

    selected_is_historical: bool = False
    """True only when the operator explicitly asked for a historical cohort, so the view
    can say so rather than presenting closed evidence as the running experiment."""

    multiple_active: bool = False
    """True when more than one cohort is collecting at once. The dashboard shows a
    warning: this is a state that needs an operator decision, not a steady state."""

    older_active: list[str] = Field(default_factory=list)
    """Active cohorts the default won against. Each is still running and still owed an
    explicit retire-or-keep decision — nothing here retires them."""

    ambiguous: bool = False
    """True when active cohorts exist but none could be defaulted to without guessing:
    a tie on the start session, or a missing one. The view shows no cohort and says why
    rather than picking the wrong experiment."""

    ambiguity_reason: str = ""
    """Operator-facing explanation, set exactly when ``ambiguous`` is true."""


class CohortIdentityView(BaseModel):
    cohort_id: str
    member_sleeves: list[str]
    """Stable sleeve identities. Render :attr:`member_names` instead."""

    member_names: list[str] = Field(default_factory=list)
    benchmark_sleeve: str | None = None
    benchmark_sleeve_name: str | None = None
    starting_capital_per_sleeve: Decimal | None = None
    """Set only when every member started from the same capital, which is what makes
    the members comparable to each other."""

    created_at: datetime
    first_session: date | None = None
    latest_session: date | None = None


class LatestRunView(BaseModel):
    """The minimum an operator needs about the most recent *due* run."""

    run_id: str
    scheduled_for: date
    status: str
    completed_members: int
    expected_members: int
    error_summary: str | None = None


class CohortPhaseView(BaseModel):
    """Life-cycle phase and progress, layered beside the fail-closed gate.

    Presentation only: this never authorizes anything and never alters the gate's
    ``status``, ``operationally_useful``, ``investment_alpha_assessed``, or
    ``live_trading_authorized`` values.
    """

    cohort_id: str
    phase: str
    """One of ``scheduled``, ``collecting``, ``review-ready``, ``passed``,
    ``attention-needed``, ``failed``."""

    headline: str
    as_of: date
    """Calendar date of the clock the phase was assessed against."""

    timing_state: str = cohort_phase.SessionTimingState.NO_SESSION_SCHEDULED.value
    """Where the clock stands relative to the next owed session: ``upcoming``,
    ``awaiting-execution``, ``overdue``, ``settled``, or ``no-session-scheduled``.
    Independent of ``phase``: a pre-close session is upcoming, not failing."""

    now_et: datetime | None = None
    """The naive Eastern wall clock the assessment was pinned to, when one was
    injected. ``null`` on the legacy date-only path."""

    evidence_cutoff: date | None = None
    """Latest exchange session whose official close has already happened. Sessions
    after it cannot have produced evidence."""

    awaiting_execution_session: date | None = None
    """A closed session still inside the scheduler's normal execution grace period."""

    awaiting_provider_session: date | None = None
    """A closed session waiting on provider evidence and still safely retryable."""

    overdue_sessions: list[date] = Field(default_factory=list)
    """Sessions whose ordinary grace or provider-specific hard deadline expired."""

    start_session: date | None = None
    started: bool = False
    review_target: int = cohort_phase.DEFAULT_REVIEW_TARGET
    due_sessions: int = 0
    """Sessions scheduled on or before ``as_of``. Future sessions are never counted."""

    completed_due_sessions: int = 0
    sessions_remaining: int = 0
    progress_ratio: float = 0.0
    total_scheduled_sessions: int = 0
    next_scheduled_session: date | None = None
    latest_due_run: LatestRunView | None = None
    latest_observation_date: date | None = None
    official_observations: int = 0
    completion_reliability: float | None = None
    """Completed due sessions over due sessions; null before the first session is due."""

    next_action: str = ""
    integrity_alerts: list[str] = Field(default_factory=list)


class CohortSleeveView(BaseModel):
    sleeve_id: str
    sleeve_name: str = ""
    strategy: str
    reproducible: bool
    configuration_hash: str | None = None
    definition: dict[str, object] | None = None


class ReadinessEvidenceView(BaseModel):
    sleeve_id: str
    sleeve_name: str = ""
    session_date: date | None = None
    observation_status: str | None = None
    evidence_status: str
    ready: bool | None = None
    reason_codes: list[str] = Field(default_factory=list)
    quote_coverage: Decimal | None = None
    snapshot_ids: dict[str, str] = Field(default_factory=dict)


class ComparisonExclusionView(BaseModel):
    session_date: date
    reason: str


class RollingExcessView(BaseModel):
    start_date: date
    end_date: date
    sleeve_return: float
    benchmark_return: float
    excess_return: float


class ComparisonReliabilityView(BaseModel):
    total_observations: int
    official: int
    partial: int
    missing: int
    used: int
    excluded: int
    readiness_ready: int
    readiness_unready: int
    readiness_unknown: int
    reason_codes: list[str]


class SleeveComparisonView(BaseModel):
    sleeve_id: str
    sleeve_name: str = ""
    strategy: str
    maturity: str
    sample_count: int
    matched_dates: list[date]
    sleeve_return: float | None = None
    benchmark_return: float | None = None
    excess_return: float | None = None
    rolling_excess: dict[int, list[RollingExcessView]] = Field(default_factory=dict)
    max_drawdown_pct: Decimal
    volatility: float | None = None
    sharpe: float | None = None
    sortino: float | None = None
    beta: float | None = None
    correlation: float | None = None
    total_turnover: Decimal | None = None
    turnover_ratio: Decimal | None = None
    total_modeled_cost: Decimal | None = None
    modeled_cost_drag: Decimal | None = None
    num_filled: int
    num_rejected: int
    reject_rate: float | None = None
    coverage: float
    reliability: ComparisonReliabilityView
    exclusions: list[ComparisonExclusionView] = Field(default_factory=list)


class SleeveCorrelationView(BaseModel):
    sleeve_a: str
    sleeve_b: str
    sample_count: int
    correlation: float | None = None


class CohortComparisonView(BaseModel):
    cohort_id: str | None = None
    available: bool = False
    message: str | None = None
    benchmark_dates: list[date] = Field(default_factory=list)
    common_dates: list[date] = Field(default_factory=list)
    sleeves: list[SleeveComparisonView] = Field(default_factory=list)
    correlations: list[SleeveCorrelationView] = Field(default_factory=list)


class RunErrorView(BaseModel):
    code: str
    message: str
    member_id: str | None = None
    capability: str | None = None
    retryable: bool = False
    context: dict[str, object] = Field(default_factory=dict)


class RunMemberView(BaseModel):
    sleeve_id: str
    sleeve_name: str = ""
    status: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: RunErrorView | None = None


class CohortRunView(BaseModel):
    run_id: str
    run_key: str
    cohort_id: str
    session_id: str
    scheduled_for: date
    status: str
    timing: str = evidence_timing.RunTiming.EXECUTED.value
    """Clock-relative state: ``upcoming``, ``awaiting-execution``, ``overdue``,
    ``executed``, or ``closed-session``. A pending run before its session's close is
    ``upcoming``, never overdue."""

    expected_members: list[str]
    completed_members: list[str]
    started_at: datetime
    completed_at: datetime | None = None
    snapshot_id: str | None = None
    quote_snapshot_id: str | None = None
    data_snapshot_ids: dict[str, str] = Field(default_factory=dict)
    retry_deadline_et: datetime | None = None
    errors: list[RunErrorView] = Field(default_factory=list)
    members: list[RunMemberView] = Field(default_factory=list)


class CohortRunHealthView(BaseModel):
    available: bool = False
    message: str | None = None
    total_runs: int | None = None
    latest_status: str | None = None
    executed_runs: int = 0
    awaiting_execution_runs: int = 0
    """Runs whose session has closed and are still inside the scheduler's grace
    period. Due, not failed."""

    awaiting_provider_data_runs: int = 0
    """Runs held safely retryable because provider evidence is incomplete."""

    upcoming_runs: int = 0
    overdue_runs: int = 0
    runs: list[CohortRunView] = Field(default_factory=list)


class GateRuleView(BaseModel):
    rule: str
    """Stable rule identifier, e.g. ``completion-rate``."""

    label: str = ""
    """Plain-language rule name, e.g. ``Completion reliability``."""

    status: str
    reason: str
    awaiting_evidence: bool = False
    """The rule's evidence has not been produced or recorded yet."""

    presentation: str = "needs-attention"
    """Phase-aware display class: ``healthy``, ``awaiting-evidence``, or
    ``needs-attention``. Derived from ``status`` plus the cohort phase; it never
    replaces ``status``, which still fails closed."""

    evidence: list[str] = Field(default_factory=list)


class OperationalGateView(BaseModel):
    cohort_id: str | None = None
    available: bool = False
    message: str | None = None
    status: str | None = None
    summary: str | None = None
    operationally_useful: bool | None = None
    investment_alpha_assessed: bool = False
    live_trading_authorized: bool = False
    rules: list[GateRuleView] = Field(default_factory=list)


class AccountingCheckView(BaseModel):
    """One recorded accounting-review entry, exactly as it was written."""

    entry_id: str
    observation_key: str
    sleeve_id: str
    sleeve_name: str = ""
    session_date: date
    area: str
    finding: str
    summary: str | None = None
    explanation: str | None = None
    explained: bool = False
    recorded_at: datetime
    recorded_by: str
    revision: int
    supersedes: str | None = None


class PendingObservationView(BaseModel):
    """One official observation whose accounting review is not complete yet."""

    observation_key: str
    sleeve_id: str
    sleeve_name: str = ""
    session_date: date
    missing_areas: list[str] = Field(default_factory=list)


class ReviewNoteView(BaseModel):
    note_id: str
    sleeve_id: str | None = None
    sleeve_name: str = ""
    observation_key: str | None = None
    note: str
    recorded_at: datetime
    recorded_by: str


class OperatorDecisionView(BaseModel):
    """One keep/modify/pause/retire research disposition. Never an authorization."""

    decision_id: str
    sleeve_id: str
    sleeve_name: str = ""
    action: str
    rationale: str
    recorded_at: datetime
    recorded_by: str
    revision: int
    supersedes: str | None = None


class CohortReviewView(BaseModel):
    """Read-only presentation of the durable 30-session review.

    The dashboard never writes a review record. ``available`` is false when the review
    store could not be read, which is reported rather than rendered as "nothing recorded":
    an unreadable store and an unstarted review are different states and an operator has
    to be able to tell them apart.
    """

    available: bool = False
    message: str | None = None
    review_due: bool = False
    review_target: int = 0
    completed_due_sessions: int = 0
    official_observations: int = 0
    reviewed_observations: int = 0
    recorded_check_count: int = 0
    """Current accounting entries recorded, across every observation and area.

    A count rather than the entries themselves: a 30-session seven-sleeve cohort produces
    630 of them and almost all say "matched", which would triple the size of a payload the
    dashboard polls every 30 seconds to carry rows nobody reads. The entries an operator
    acts on — differences and corrections — are served in full below, and
    ``cohort review show --json`` remains the complete record.
    """

    unexplained_difference_count: int = 0
    decided_sleeve_count: int = 0
    member_count: int = 0
    differences: list[AccountingCheckView] = Field(default_factory=list)
    superseded_checks: list[AccountingCheckView] = Field(default_factory=list)
    pending_observations: list[PendingObservationView] = Field(default_factory=list)
    notes: list[ReviewNoteView] = Field(default_factory=list)
    decisions: list[OperatorDecisionView] = Field(default_factory=list)
    superseded_decisions: list[OperatorDecisionView] = Field(default_factory=list)


class CohortDashboardView(BaseModel):
    # 2.4 adds `cohort_review`: the durable accounting review and operator decisions
    # that the operational gate now consumes instead of always receiving `None`.
    contract_version: str = "2.4"
    available: bool = False
    message: str | None = None
    selection: CohortSelectionView = Field(default_factory=CohortSelectionView)
    identity: CohortIdentityView | None = None
    phase: CohortPhaseView | None = None
    sleeve_names: dict[str, str] = Field(default_factory=dict)
    """Stable sleeve id to human-readable name, for every id referenced in this view."""

    sleeve_definitions: list[CohortSleeveView] = Field(default_factory=list)
    comparison: CohortComparisonView = Field(default_factory=CohortComparisonView)
    run_health: CohortRunHealthView = Field(default_factory=CohortRunHealthView)
    readiness: list[ReadinessEvidenceView] = Field(default_factory=list)
    operational_gate: OperationalGateView = Field(default_factory=OperationalGateView)
    cohort_review: CohortReviewView = Field(default_factory=CohortReviewView)


class DashboardData(BaseModel):
    api_version: str = "3.5"
    generated_at: datetime
    live_enabled: bool = False
    """Whether this server was started with broker access. When false, broker-derived
    sections are unavailable rather than zero, and the UI should not lead with them."""

    benchmark: str
    sleeves: list[SleeveRow]
    curves: list[EquitySeries]
    safety: SafetyView
    positions: PositionsView
    summary: SummaryView = Field(default_factory=SummaryView)
    orders: OrdersView = Field(default_factory=lambda: OrdersView(available=False))
    validation: ValidationView = Field(default_factory=ValidationView)
    approvals: ApprovalsView = Field(default_factory=ApprovalsView)
    regime: RegimeView = Field(default_factory=RegimeView)
    tax_lots: TaxLotsView = Field(default_factory=TaxLotsView)
    audit: AuditView = Field(default_factory=AuditView)
    reconciliation: ReconciliationView = Field(default_factory=ReconciliationView)
    cohort: CohortDashboardView = Field(default_factory=CohortDashboardView)


# --- Collectors -------------------------------------------------------------


def _scope_of(config: sleeves.SleeveConfig, *, has_history: bool) -> SleeveScope:
    """Which comparability group this sleeve belongs to."""
    return sleeve_detail.scope_of(config, has_history=has_history)


# Official cohorts first, then lifetime legacy history, then unstarted standalone state.
_SCOPE_ORDER = {
    SleeveScope.OFFICIAL_COHORT: 0,
    SleeveScope.LEGACY: 1,
    SleeveScope.STANDALONE: 2,
}


def benchmark_is_registered(settings: Settings, benchmark: str) -> bool:
    """Whether any *currently collected* sleeve matches ``benchmark``, ignoring scope.

    Deliberately tolerant: a benchmark name shared by several cohorts is normal (the
    dashboard resolves it inside each one), so this reports existence rather than
    resolving, and never raises. An unreadable registry answers True, because a
    registry problem is reported per section by the collectors, not by refusing to
    start the server over an optional column.

    Members of historical cohorts do not count. A benchmark that exists only inside a
    superseded experiment is not a benchmark the running collection can be measured
    against, and reporting it as registered would suppress the warning that says so.
    Explicitly selecting that cohort still resolves its own benchmark internally, in
    :func:`collect_sleeves`, which never crosses cohort boundaries.
    """
    try:
        configs = storage_factory.sleeve_store(settings).list()
    except (OSError, sqlite3.DatabaseError, SQLAlchemyError, ValueError):
        return True
    current = [cfg for cfg in configs if not cohort_lifecycle.is_historical(cfg.cohort_id)]
    return bool(benchmark_scope.candidates(current, benchmark))


def collect_sleeves(
    settings: Settings, benchmark: str
) -> tuple[list[SleeveRow], list[EquitySeries]]:
    """Build the sleeve leaderboard and per-sleeve equity curves (local; no network).

    Sleeves are ranked *within a comparability group*, never across groups. Each
    official cohort is its own group; every unassigned sleeve shares a single
    legacy/standalone group. Cohort members start from the same capital on the same
    date under fixed definitions, and legacy sleeves carry unmatched lifetime history
    from different capital and start dates, so one combined ranking would be
    meaningless.

    Excess return is measured only against a benchmark resolved *inside the same
    group*, and only when exactly one member of that group matches ``benchmark``. An
    ambiguous or absent benchmark yields ``excess_pct=None`` rather than a number
    computed against some other group's sleeve. ``benchmark`` may be a display name or
    a stable sleeve identity; the identity form is the way to name one specific sleeve
    when the same display name exists in more than one group.
    """
    store = storage_factory.sleeve_store(settings)
    configs = store.list()
    summaries = {
        cfg.identity: storage_factory.evaluation_store(settings, cfg).summary() for cfg in configs
    }

    groups: dict[str, list[sleeves.SleeveConfig]] = {}
    for config in configs:
        groups.setdefault(config.cohort_id or "", []).append(config)

    rows: list[SleeveRow] = []
    for group_key, members in groups.items():
        # Resolve the benchmark only within this group, and only when unambiguous.
        matches = benchmark_scope.candidates(members, benchmark)
        benchmark_config = matches[0] if len(matches) == 1 else None
        benchmark_id = benchmark_config.identity if benchmark_config is not None else None
        bench_return = (
            summaries[benchmark_id].total_return_pct if benchmark_id is not None else None
        )
        benchmark_name = benchmark_config.name if benchmark_config is not None else None

        ordered = sorted(
            members,
            key=lambda cfg: summaries[cfg.identity].total_return_pct,
            reverse=True,
        )
        for rank, cfg in enumerate(ordered, start=1):
            summary = summaries[cfg.identity]
            is_benchmark = cfg.identity == benchmark_id
            excess: Decimal | None = None
            if bench_return is not None and not is_benchmark:
                excess = summary.total_return_pct - bench_return
            rows.append(
                SleeveRow(
                    rank=rank,
                    sleeve_id=cfg.identity,
                    name=cfg.name,
                    strategy=cfg.strategy,
                    scope=_scope_of(cfg, has_history=summary.cycles > 0),
                    cohort_id=group_key or None,
                    starting_capital=cfg.starting_cash,
                    cycles=summary.cycles,
                    trades=summary.trades_filled,
                    value=summary.latest_value if summary.cycles > 0 else cfg.starting_cash,
                    return_pct=summary.total_return_pct,
                    excess_pct=excess,
                    excess_benchmark=None if is_benchmark else benchmark_name,
                    max_drawdown_pct=summary.max_drawdown_pct,
                    sharpe=summary.sharpe,
                    is_benchmark=is_benchmark,
                    historical=cohort_lifecycle.is_historical(group_key),
                )
            )

    # Historical cohorts sort after the collection that replaced them, inside the
    # official-cohort scope. They stay in the payload — withdrawing an experiment does
    # not withdraw its evidence — but they never head the leaderboard.
    rows.sort(
        key=lambda row: (_SCOPE_ORDER[row.scope], row.historical, row.cohort_id or "", row.rank)
    )

    curves: list[EquitySeries] = []
    for cfg in configs:
        points = storage_factory.evaluation_store(settings, cfg).equity_curve(limit=500)
        if len(points) >= 2:
            curves.append(
                EquitySeries(
                    sleeve_id=cfg.identity,
                    name=cfg.name,
                    points=[value for _, value in points],
                )
            )
    return rows, curves


def collect_safety(settings: Settings) -> SafetyView:
    """Kill-switch state, today's activity, and the configured limits (local)."""
    status = safety.KillSwitch(settings.kill_switch_path).status()
    day = safety.SafetyLedger(settings.agent_activity_db_path).day()
    return SafetyView(
        kill_engaged=status.engaged,
        kill_since=status.since,
        kill_reason=status.reason,
        trades_today=day.trades,
        realized_pnl=day.realized_pnl,
        start_equity=day.start_equity,
        capital_cap=settings.agent_capital_cap,
        daily_loss_limit=settings.agent_daily_loss_limit,
        max_trades_per_day=settings.agent_max_trades_per_day,
        max_order_notional=settings.max_order_notional,
    )


def collect_positions(settings: Settings, client_factory: ClientFactory | None) -> PositionsView:
    """Live positions + balances, or a graceful message if unavailable (network).

    Never raises: no account selected, no client factory, or an auth/network error
    all degrade to ``available=False`` with an explanatory message.
    """
    account = settings.masked_account_tail()
    if not settings.has_account_selected:
        return PositionsView(account=account, available=False, message="No account selected.")
    if client_factory is None:
        return PositionsView(
            account=account, available=False, message="Live data disabled for this server."
        )
    account_hash = settings.account_hash.get_secret_value()
    try:
        with client_factory() as client:
            holdings = accounts.get_positions(client, account_hash)
            balances = accounts.get_balances(client, account_hash)
    except (oauth.OAuthError, api.ApiError, OSError) as exc:
        return PositionsView(
            account=account, available=False, message=f"Live data unavailable: {exc}"
        )
    rows = [
        PositionRow(
            symbol=p.symbol,
            quantity=p.long_quantity,
            settled=p.settled_long_quantity,
            average_price=p.average_price,
            market_value=p.market_value,
            day_pl=p.current_day_profit_loss,
        )
        for p in sorted(holdings, key=lambda p: p.symbol)
    ]
    return PositionsView(
        account=account,
        available=True,
        positions=rows,
        liquidation_value=balances.liquidation_value,
        cash_available_for_trading=balances.cash_available_for_trading,
        cash_available_for_withdrawal=balances.cash_available_for_withdrawal,
    )


def collect_orders(
    settings: Settings, client_factory: ClientFactory | None, *, hours: int = 48
) -> OrdersView:
    """Recent orders (working ones highlighted), or a graceful message (network)."""
    if not settings.has_account_selected:
        return OrdersView(available=False, message="No account selected.", hours=hours)
    if client_factory is None:
        return OrdersView(
            available=False, message="Live data disabled for this server.", hours=hours
        )
    account_hash = settings.account_hash.get_secret_value()
    to_time = datetime.now(UTC)
    from_time = to_time - timedelta(hours=hours)
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    try:
        with client_factory() as client:
            raw = orders.get_recent_orders(
                client,
                account_hash,
                from_time=from_time.strftime(fmt),
                to_time=to_time.strftime(fmt),
            )
    except (oauth.OAuthError, api.ApiError, OSError) as exc:
        return OrdersView(available=False, message=f"Live data unavailable: {exc}", hours=hours)
    rows: list[OrderRow] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        detail = orders.parse_order_detail(item, fallback_id=str(item.get("orderId", "?")))
        rows.append(
            OrderRow(
                order_id=detail.order_id,
                status=detail.status.value,
                side=detail.side,
                symbol=detail.symbol,
                quantity=detail.quantity,
                filled=detail.filled_quantity,
                limit_price=detail.limit_price,
                working=detail.status.value in _WORKING_STATUSES,
            )
        )
    # Working orders first, then the rest (newest kept in API order otherwise).
    rows.sort(key=lambda r: (not r.working,))
    return OrdersView(available=True, hours=hours, orders=rows)


def collect_validation(settings: Settings) -> ValidationView:
    """Latest walk-forward promotion verdict per strategy/universe (local)."""
    verdicts = storage_factory.promotion_store(settings).all_latest()
    rows = [
        ValidationRow(
            strategy=v.strategy,
            universe=v.universe,
            validated=v.validated,
            pass_rate=v.pass_rate,
            min_pass_rate=v.min_pass_rate,
            mean_excess_pct=v.mean_excess_pct,
            beats_benchmark=v.beats_benchmark,
        )
        for v in verdicts
    ]
    # Validated first, then by mean excess descending (Nones last).
    rows.sort(
        key=lambda r: (not r.validated, -(float(r.mean_excess_pct) if r.mean_excess_pct else -1e9))
    )
    return ValidationView(rows=rows)


def collect_approvals(settings: Settings) -> ApprovalsView:
    """Pending notify-and-approve tokens awaiting a human (local)."""
    now = datetime.now(UTC)
    pending = approval.ApprovalStore(settings.approval_db_path).pending(now=now)
    rows = [
        ApprovalRow(
            token=record.token,
            describe=record.to_request().describe(),
            rationale=record.rationale,
            account_tail=record.account_tail,
            expires_in_min=max(0, int((record.expires_at - now).total_seconds() // 60)),
        )
        for record in pending
    ]
    return ApprovalsView(rows=rows)


def collect_tax_lots(settings: Settings) -> TaxLotsView:
    """Open tax lots with short-/long-term classification as of today (local)."""
    now = datetime.now(UTC)
    lots = taxlots.TaxLotStore(settings.tax_lots_db_path).open_lots()
    rows = [
        TaxLotRow(
            symbol=lot.symbol,
            quantity=lot.quantity,
            cost_per_share=lot.cost_per_share,
            acquired=lot.acquired_at.date(),
            days_held=(now - lot.acquired_at).days,
            long_term=taxlots.is_long_term(lot.acquired_at, now),
        )
        for lot in lots
    ]
    rows.sort(key=lambda r: (r.symbol, r.acquired))
    return TaxLotsView(rows=rows)


def collect_audit(settings: Settings, *, limit: int = 15) -> AuditView:
    """The most recent audit-log entries (local)."""
    records = state.StateStore(settings.state_db_path).recent_audit(limit=limit)
    rows = [AuditRow(ts=r.ts, command=r.command, event=r.event, detail=r.detail) for r in records]
    return AuditView(rows=rows)


def collect_reconciliation(settings: Settings) -> ReconciliationView:
    """Latest local reconciliation health; no broker call is made here."""
    summary = reconciliation.ReconciliationStore(settings.state_db_path).latest_summary()
    return ReconciliationView(**summary.model_dump())


def _comparison_view(
    report: comparison.ComparisonReport, names: dict[str, str]
) -> CohortComparisonView:
    payload = asdict(report)
    payload.pop("cohort_id")
    view = CohortComparisonView(cohort_id=report.cohort_id, available=True, **payload)
    for sleeve in view.sleeves:
        sleeve.sleeve_name = names.get(sleeve.sleeve_id, sleeve.sleeve_id)
    return view


def _run_error_view(error: sleeve_runs.SleeveRunError) -> RunErrorView:
    return RunErrorView(**error.model_dump())


def _run_view(
    run: sleeve_runs.SleeveRun,
    names: dict[str, str],
    timing: evidence_timing.RunTiming = evidence_timing.RunTiming.EXECUTED,
) -> CohortRunView:
    return CohortRunView(
        run_id=run.run_id,
        run_key=run.run_key,
        cohort_id=run.cohort_id,
        session_id=run.session_id,
        scheduled_for=run.scheduled_for,
        status=run.status.value,
        timing=timing.value,
        expected_members=list(run.expected_members),
        completed_members=list(run.completed_members),
        started_at=run.started_at,
        completed_at=run.completed_at,
        snapshot_id=run.snapshot_id,
        quote_snapshot_id=run.quote_snapshot_id,
        data_snapshot_ids=dict(run.data_snapshot_ids),
        retry_deadline_et=scheduling.execution_deadline_et(run.scheduled_for),
        errors=[_run_error_view(error) for error in sleeve_runs.active_errors(run)],
        members=[
            RunMemberView(
                sleeve_id=member.sleeve_id,
                sleeve_name=names.get(member.sleeve_id, member.sleeve_id),
                status=member.status.value,
                started_at=member.started_at,
                completed_at=member.completed_at,
                error=_run_error_view(member.error) if member.error is not None else None,
            )
            for member in run.members
        ],
    )


#: Reason codes meaning "the data was present but did not reach the required
#: freshness", which the dashboard shows as its own state rather than generic
#: not-ready. Both spellings of that condition belong here: ``stale`` is the
#: elapsed-time judgement, ``session_not_covered`` the session-aligned one that
#: replaced it for daily bars.
_BEHIND_REASONS = frozenset(
    {
        data_readiness.ReasonCode.STALE.value,
        data_readiness.ReasonCode.SESSION_NOT_COVERED.value,
    }
)


def _readiness_status(observation: evaluation.OfficialDailyObservation) -> str:
    """Classify one observation's readiness for display.

    Reasons are persisted as qualified ``"<kind>:<reason>"`` codes, e.g.
    ``daily_bars:session_not_covered``, so this compares the *reason* half. Testing the
    whole string against a bare code never matched real data and silently downgraded
    every stale session to generic ``not_ready``.
    """
    if observation.readiness_ready is None:
        return "unknown"
    if any(
        reason.rsplit(":", 1)[-1] in _BEHIND_REASONS for reason in observation.readiness_reasons
    ):
        return "stale"
    return "ready" if observation.readiness_ready else "not_ready"


def _gate_view(
    result: operational_gate.OperationalGateResult,
    phase: cohort_phase.CohortPhase,
) -> OperationalGateView:
    """Serialize the gate verbatim, adding only phase-aware *display* classification.

    ``status``, ``operationally_useful``, ``investment_alpha_assessed``, and
    ``live_trading_authorized`` are passed through unchanged, so the authorization
    contract still fails closed.
    """
    return OperationalGateView(
        cohort_id=result.cohort_id,
        available=True,
        status=result.status.value,
        summary=result.summary,
        operationally_useful=result.operationally_useful,
        investment_alpha_assessed=result.investment_alpha_assessed,
        live_trading_authorized=result.live_trading_authorized,
        rules=[
            GateRuleView(
                rule=rule.rule.value,
                label=rule.label,
                status=rule.status.value,
                reason=rule.reason,
                awaiting_evidence=rule.awaiting_evidence,
                presentation=cohort_phase.presentation_for(rule, phase).value,
                evidence=list(rule.evidence),
            )
            for rule in result.rules
        ],
    )


def _phase_view(assessment: cohort_phase.CohortPhaseAssessment) -> CohortPhaseView:
    latest = assessment.latest_due_run
    return CohortPhaseView(
        cohort_id=assessment.cohort_id,
        phase=assessment.phase.value,
        headline=assessment.headline,
        as_of=assessment.as_of,
        timing_state=assessment.timing_state.value,
        now_et=assessment.now_et,
        evidence_cutoff=assessment.evidence_cutoff,
        awaiting_execution_session=assessment.awaiting_execution_session,
        awaiting_provider_session=assessment.awaiting_provider_session,
        overdue_sessions=list(assessment.overdue_sessions),
        start_session=assessment.start_session,
        started=assessment.started,
        review_target=assessment.review_target,
        due_sessions=assessment.due_sessions,
        completed_due_sessions=assessment.completed_due_sessions,
        sessions_remaining=assessment.sessions_remaining,
        progress_ratio=assessment.progress_ratio,
        total_scheduled_sessions=assessment.total_scheduled_sessions,
        next_scheduled_session=assessment.next_scheduled_session,
        latest_due_run=(
            None
            if latest is None
            else LatestRunView(
                run_id=latest.run_id,
                scheduled_for=latest.scheduled_for,
                status=latest.status,
                completed_members=latest.completed_members,
                expected_members=latest.expected_members,
                error_summary=latest.error_summary,
            )
        ),
        latest_observation_date=assessment.latest_observation_date,
        official_observations=assessment.official_observations,
        completion_reliability=assessment.completion_reliability,
        next_action=assessment.next_action,
        integrity_alerts=list(assessment.integrity_alerts),
    )


def _check_view(
    record: cohort_review.AccountingCheck, names: Mapping[str, str]
) -> AccountingCheckView:
    return AccountingCheckView(
        entry_id=record.entry_id,
        observation_key=record.observation_key,
        sleeve_id=record.sleeve_id,
        sleeve_name=names.get(record.sleeve_id, ""),
        session_date=record.session_date,
        area=record.area.value,
        finding=record.finding.value,
        summary=record.summary,
        explanation=record.explanation,
        explained=record.explained,
        recorded_at=record.recorded_at,
        recorded_by=record.recorded_by,
        revision=record.revision,
        supersedes=record.supersedes,
    )


def _decision_view(
    record: cohort_review.SleeveDecision, names: Mapping[str, str]
) -> OperatorDecisionView:
    return OperatorDecisionView(
        decision_id=record.decision_id,
        sleeve_id=record.sleeve_id,
        sleeve_name=names.get(record.sleeve_id, ""),
        action=record.action.value,
        rationale=record.rationale,
        recorded_at=record.recorded_at,
        recorded_by=record.recorded_by,
        revision=record.revision,
        supersedes=record.supersedes,
    )


def _review_view(
    review: cohort_review.CohortReview | None,
    names: Mapping[str, str],
    *,
    review_error: str | None,
) -> CohortReviewView:
    """Serialize the durable review for display. Read-only; writes nothing.

    Three distinct empty states are kept distinct, because conflating them is what makes
    an operator trust a review that never happened: the store could not be read
    (``available`` false with a message), the store is readable and holds nothing yet
    (``available`` true, empty lists), and the review is under way but incomplete
    (``pending_observations`` populated).
    """
    if review_error is not None:
        return CohortReviewView(message=review_error)
    if review is None:
        return CohortReviewView(
            available=True,
            message="No durable cohort review is available for this view.",
        )
    return CohortReviewView(
        available=True,
        message=(
            None
            if review.has_records
            else "No accounting review or operator decision has been recorded yet."
        ),
        review_due=review.review_due,
        review_target=review.review_target,
        completed_due_sessions=review.completed_due_sessions,
        official_observations=review.official_observations,
        reviewed_observations=review.reviewed_observations,
        recorded_check_count=len(review.checks),
        unexplained_difference_count=len(review.unexplained_differences),
        decided_sleeve_count=len(review.decided_sleeves),
        member_count=len(names),
        differences=[_check_view(item, names) for item in review.differences],
        superseded_checks=[_check_view(item, names) for item in review.superseded_checks],
        pending_observations=[
            PendingObservationView(
                observation_key=item.observation_key,
                sleeve_id=item.sleeve_id,
                sleeve_name=names.get(item.sleeve_id, ""),
                session_date=item.session_date,
                missing_areas=[area.value for area in item.missing_areas],
            )
            for item in review.pending_observations
        ],
        notes=[
            ReviewNoteView(
                note_id=item.note_id,
                sleeve_id=item.sleeve_id,
                sleeve_name="" if item.sleeve_id is None else names.get(item.sleeve_id, ""),
                observation_key=item.observation_key,
                note=item.note,
                recorded_at=item.recorded_at,
                recorded_by=item.recorded_by,
            )
            for item in review.notes
        ],
        decisions=[_decision_view(item, names) for item in review.decisions],
        superseded_decisions=[_decision_view(item, names) for item in review.superseded_decisions],
    )


def _cohort_contract_for_gate(
    cohort_id: str,
    configs: list[sleeves.SleeveConfig],
    benchmark: str,
    start_session: date | None,
) -> experiments.ExperimentCohort | None:
    """Rebuild the gate's cohort contract from persisted sleeve definitions.

    Members are identified by ``SleeveConfig.identity`` because that is what
    ``SleeveRun.expected_members`` and ``OfficialDailyObservation.sleeve_id`` record.
    ``benchmark`` arrives as a display name and is resolved to its identity here; an
    ambiguous name yields no contract rather than a guess.

    ``start_session`` is supplied by the caller from
    :func:`schwab_trader.sleeves.resolve_cohort_start` rather than inferred from runs
    here, so the gate and the phase view cannot disagree about when the cohort began.
    """
    members = tuple(config.identity for config in configs)
    benchmark_matches = [config for config in configs if config.name == benchmark]
    benchmark_id = benchmark_matches[0].identity if len(benchmark_matches) == 1 else None
    cash = {config.starting_cash for config in configs}
    settlement = {config.settlement_t1 for config in configs}
    leverage = {config.leverage for config in configs}
    schedules = {(config.decision_frequency, config.decision_time) for config in configs}
    uniform = all(len(values) == 1 for values in (cash, settlement, leverage, schedules))
    if len(members) < 2 or benchmark_id is None or not uniform:
        return None
    frequency, decision_time = next(iter(schedules))
    if not frequency or not decision_time:
        return None
    if start_session is None:
        return None
    return experiments.ExperimentCohort(
        cohort_id=cohort_id,
        name=cohort_id,
        created_at=min(item.created_at for item in configs),
        start_session=start_session,
        starting_cash_per_sleeve=next(iter(cash)),
        settlement_model="T+1" if next(iter(settlement)) else "T+0",
        leverage=next(iter(leverage)),
        benchmark_sleeve=benchmark_id,
        decision_schedule=f"{frequency}@{decision_time}",
        cost_model_id="not-persisted",
        member_sleeves=members,
        status="persisted",
    )


def cohort_selection(
    requested: str | None,
    available: list[str],
    *,
    start_sessions: Mapping[str, date | None] | None = None,
) -> CohortSelectionView:
    """Resolve which cohort the view shows, and describe the alternatives.

    An explicit request always wins, historical or not: a superseded cohort has to stay
    fully readable for audit, and the only honest way to do that is to render it exactly
    as it was recorded. Absent a request, the default is the newest *active* cohort —
    never a withdrawn experiment, and never the one whose id happens to sort first.

    ``start_sessions`` maps cohort id to its persisted immutable start session, which is
    what "newest" is decided on. Omitting it (or mapping a cohort to ``None``) means the
    caller has no authoritative ordering metadata: harmless while one cohort is active,
    and a visible failure the moment two are, because that is the case where a guess
    would silently put the wrong experiment on screen. Callers holding the store should
    pass :meth:`SleeveStore.cohort_start_session` for every available cohort.

    Note this deliberately does *not* use :func:`schwab_trader.sleeves.resolve_cohort_start`,
    whose created-at fallback is right for "when was this cohort first owed a run" and
    wrong for ranking two experiments: creation time records when somebody ran the
    bootstrap command, not which collection is newer.
    """
    starts = start_sessions or {}
    resolution = cohort_lifecycle.resolve_default_cohort(
        cohort_lifecycle.CohortRecency(cohort_id=cohort_id, start_session=starts.get(cohort_id))
        for cohort_id in available
    )
    # Newest-first once ranking succeeded; otherwise the caller's order, unchanged.
    active = list(resolution.active) or cohort_lifecycle.active_cohorts(available)
    selection = CohortSelectionView(
        requested=requested,
        available=available,
        active=active,
        active_cohorts=[
            ActiveCohortView(
                cohort_id=cohort_id,
                start_session=starts.get(cohort_id),
                is_default=cohort_id == resolution.selected,
            )
            for cohort_id in active
        ],
        historical=[
            HistoricalCohortView(
                cohort_id=status.cohort_id,
                lifecycle=status.lifecycle.value,
                label=status.label,
                reason=status.reason,
                superseded_by=status.superseded_by,
                reference=status.reference,
            )
            for status in cohort_lifecycle.historical_cohorts(available)
        ],
        multiple_active=resolution.multiple_active,
        older_active=list(resolution.older_active),
        ambiguous=resolution.ambiguous,
        ambiguity_reason=resolution.problem,
    )
    chosen = requested or resolution.selected
    if chosen is not None and chosen in available:
        selection.selected = chosen
        selection.selected_is_historical = cohort_lifecycle.is_historical(chosen)
    return selection


def _selection_problem(selection: CohortSelectionView) -> str:
    """Why nothing is being shown, given cohorts exist but none was selected."""
    if selection.requested:
        return f"Requested cohort {selection.requested!r} is unavailable."
    if selection.ambiguous:
        # Several cohorts are collecting and the records cannot say which is newest.
        # Showing one anyway is the failure mode this refuses to reproduce.
        return selection.ambiguity_reason
    names = ", ".join(item.cohort_id for item in selection.historical)
    return (
        "No active paper cohort is available. Every persisted cohort is historical "
        f"({names}); select one explicitly to review its records."
    )


def collect_cohort_dashboard(
    settings: Settings,
    *,
    requested_cohort: str | None,
    benchmark: str,
    as_of: date | None = None,
    now_et: datetime | None = None,
) -> CohortDashboardView:
    """Assemble the official-cohort view (local; no network).

    ``now_et`` is the explicit Eastern wall clock used for phase, run-timing, and gate
    assessment; it defaults to :func:`schwab_trader.market_calendar.eastern_now`. A
    session whose close has not happened is upcoming, never missing. ``as_of`` is the
    legacy date-only clock and is used only when ``now_et`` is not supplied.
    """
    clock = now_et
    if clock is None and as_of is None:
        clock = market_calendar.eastern_now()
    try:
        store = storage_factory.sleeve_store(settings)
        all_configs = store.list()
    except (OSError, sqlite3.DatabaseError, SQLAlchemyError, ValueError):
        return CohortDashboardView(message="Cohort registry unavailable.")
    grouped: dict[str, list[sleeves.SleeveConfig]] = {}
    for config in all_configs:
        if config.cohort_id:
            grouped.setdefault(config.cohort_id, []).append(config)
    available = sorted(grouped)
    # Recency comes from the immutable manifest, read per cohort, before anything is
    # selected. `available` stays sorted purely so the picker and the audit list are
    # stable to look at — the sort no longer decides anything.
    try:
        start_sessions: dict[str, date | None] = {
            cohort_id: store.cohort_start_session(cohort_id) for cohort_id in available
        }
    except (OSError, sqlite3.DatabaseError, SQLAlchemyError, ValueError):
        # Unreadable ordering metadata is the ambiguous case, not an excuse to guess.
        start_sessions = dict.fromkeys(available)
    selection = cohort_selection(requested_cohort, available, start_sessions=start_sessions)
    if not available:
        return CohortDashboardView(
            message="No persisted paper cohorts are available.", selection=selection
        )
    if selection.selected is None:
        return CohortDashboardView(message=_selection_problem(selection), selection=selection)
    selected = selection.selected
    configs = grouped[selected]
    runs_path = settings.sleeves_dir / "runs.sqlite3"
    runs: list[sleeve_runs.SleeveRun] = []
    run_store_error: str | None = None
    if not settings.has_shared_database and not runs_path.exists():
        run_store_error = "Durable cohort-run store is unavailable."
    else:
        try:
            runs = storage_factory.run_store(settings).list(cohort_id=selected, limit=10_000)
        except (OSError, sqlite3.DatabaseError, SQLAlchemyError, ValueError):
            run_store_error = "Durable cohort-run evidence is unavailable."
    observations: list[evaluation.OfficialDailyObservation] = []
    observation_error: str | None = None
    try:
        for config in configs:
            stored = storage_factory.evaluation_store(settings, config).official_observations(
                limit=10_000
            )
            observations.extend(item for item in stored if item.cohort_id == selected)
    except (OSError, sqlite3.DatabaseError, SQLAlchemyError, ValueError):
        observation_error = "Official cohort observations are unavailable."
        observations = []

    review, review_error = collect_cohort_review(
        settings,
        cohort_id=selected,
        configs=configs,
        runs=runs,
        observations=observations,
        as_of=as_of,
        now_et=clock,
        start_session=sleeves.resolve_cohort_start(store, selected, configs),
    )

    return assemble_cohort_view(
        selected=selected,
        selection=selection,
        configs=configs,
        runs=runs,
        observations=observations,
        benchmark=benchmark,
        as_of=as_of,
        now_et=clock,
        run_store_error=run_store_error,
        observation_error=observation_error,
        # This caller has the store, so it can supply the authoritative persisted start
        # instead of leaving the view to infer one from recorded sessions.
        cohort_start=sleeves.resolve_cohort_start(store, selected, configs),
        review=review,
        review_error=review_error,
    )


def collect_cohort_review(
    settings: Settings,
    *,
    cohort_id: str,
    configs: list[sleeves.SleeveConfig],
    runs: list[sleeve_runs.SleeveRun],
    observations: list[evaluation.OfficialDailyObservation],
    as_of: date | None,
    now_et: datetime | None,
    start_session: date | None,
) -> tuple[cohort_review.CohortReview | None, str | None]:
    """Read the durable review for one cohort. Read-only, and fails soft.

    Returns ``(review, error)``. A readable store with nothing recorded still returns a
    review — it carries the coverage denominator an operator needs — and the gate is
    unaffected, because :func:`schwab_trader.cohort_review.accounting_evidence` and
    :func:`~schwab_trader.cohort_review.operator_decisions` each yield ``None`` until a
    real record exists. An unreadable or un-migrated store returns an ``error`` instead,
    which the view shows as a distinct state: the dashboard must never present "could
    not read the review" as "nothing has been reviewed".
    """
    try:
        missing = storage_factory.missing_cohort_review_tables(settings)
    except (OSError, sqlite3.DatabaseError, SQLAlchemyError, ValueError):
        return None, "The cohort review store could not be inspected."
    if missing:
        return None, (
            "The cohort review tables are missing from the shared database. "
            "Apply the Alembic migration before recording or reading a review."
        )
    try:
        store = storage_factory.cohort_review_store(settings)
        context = cohort_review.build_context(
            cohort_id=cohort_id,
            configs=configs,
            runs=runs,
            observations=observations,
            now_et=now_et,
            as_of=as_of,
            start_session=start_session,
        )
        review = cohort_review.assemble_review(context, store)
    except (OSError, sqlite3.DatabaseError, SQLAlchemyError, ValueError):
        return None, "The durable cohort review is unavailable."
    return review, None


def assemble_cohort_view(
    *,
    selected: str,
    selection: CohortSelectionView,
    configs: list[sleeves.SleeveConfig],
    runs: list[sleeve_runs.SleeveRun],
    observations: list[evaluation.OfficialDailyObservation],
    benchmark: str,
    as_of: date | None = None,
    now_et: datetime | None = None,
    run_store_error: str | None = None,
    observation_error: str | None = None,
    cohort_start: date | None = None,
    review: cohort_review.CohortReview | None = None,
    review_error: str | None = None,
) -> CohortDashboardView:
    """Build the cohort view from already-loaded records (pure; no storage or network).

    Split out of :func:`collect_cohort_dashboard` so the same assembly — including the
    real operational gate and phase assessment — can be exercised by deterministic
    offline fixtures and tests without touching a database.

    Pass ``now_et`` (naive Eastern wall clock) so pre-close sessions read as upcoming.
    ``as_of`` is the legacy date-only clock, kept for existing callers.

    ``cohort_start`` is the cohort's *persisted* first official session, resolved by the
    caller through :func:`schwab_trader.sleeves.resolve_cohort_start` because this
    function deliberately holds no store. When it is omitted the start is inferred from
    recorded sessions, which is only correct once the cohort has actually run: a created
    but unrun cohort has nothing to infer from and would report as never scheduled.

    ``review`` is the durable accounting review and operator decisions, already read by
    the caller. When it is ``None`` — no review store, or nothing recorded yet — the gate
    is called with no accounting or operator evidence exactly as before, so an unreviewed
    cohort still reports those two rules as awaiting evidence rather than as satisfied.
    ``review_error`` says the store existed but could not be read, which is a different
    state from "nothing recorded" and is displayed as such.
    """
    if now_et is None and as_of is None:
        raise ValueError("assemble_cohort_view requires now_et (preferred) or as_of")
    # Stable id -> human-readable name for every identity this view can reference.
    names = {config.identity: config.name for config in configs}
    window = None if now_et is None else evidence_timing.assess_runs(runs, now_et=now_et)
    timings: dict[str, evidence_timing.RunTiming] = (
        {} if window is None else {item.run.run_id: item.timing for item in window.assessments}
    )
    run_health = (
        CohortRunHealthView(message=run_store_error)
        if run_store_error is not None
        else CohortRunHealthView(
            available=True,
            total_runs=len(runs),
            latest_status=runs[0].status.value if runs else None,
            executed_runs=len(window.executed) if window else len(runs),
            awaiting_execution_runs=len(window.awaiting_execution) if window else 0,
            awaiting_provider_data_runs=(len(window.awaiting_provider_data) if window else 0),
            upcoming_runs=len(window.upcoming) if window else 0,
            overdue_runs=len(window.overdue) if window else 0,
            runs=[
                _run_view(
                    run,
                    names,
                    timings.get(run.run_id, evidence_timing.RunTiming.EXECUTED),
                )
                for run in runs
            ],
        )
    )

    report: comparison.ComparisonReport | None = None
    if observation_error is not None:
        comparison_view = CohortComparisonView(message=observation_error)
    elif not observations:
        comparison_view = CohortComparisonView(message="No official cohort observations exist.")
    else:
        try:
            report = comparison.compare_sleeves(observations, cohort_id=selected)
            comparison_view = _comparison_view(report, names)
        except ValueError as exc:
            comparison_view = CohortComparisonView(message=f"Matched comparison unavailable: {exc}")
    by_sleeve: dict[str, list[evaluation.OfficialDailyObservation]] = {}
    for observation in observations:
        by_sleeve.setdefault(observation.sleeve_id, []).append(observation)
    readiness: list[ReadinessEvidenceView] = []
    for config in configs:
        evidence = by_sleeve.get(config.identity, [])
        if not evidence:
            readiness.append(
                ReadinessEvidenceView(
                    sleeve_id=config.identity,
                    sleeve_name=config.name,
                    evidence_status="unavailable",
                )
            )
            continue
        latest = max(evidence, key=lambda item: (item.session_date, item.valuation_time))
        readiness.append(
            ReadinessEvidenceView(
                sleeve_id=config.identity,
                sleeve_name=config.name,
                session_date=latest.session_date,
                observation_status=latest.status.value,
                evidence_status=_readiness_status(latest),
                ready=latest.readiness_ready,
                reason_codes=list(latest.readiness_reasons),
                quote_coverage=latest.quote_coverage,
                snapshot_ids=dict(latest.snapshot_ids),
            )
        )
    # Every session this cohort has actually recorded. This is the *observed range*,
    # which the identity view reports as first/latest session - deliberately not the
    # same thing as when the cohort was scheduled to begin.
    session_dates = [run.scheduled_for for run in runs]
    session_dates.extend(item.session_date for item in observations)

    # Resolved once and shared, so the gate contract and the phase view can never
    # disagree about when this cohort began. Falling back to the observed range keeps
    # callers that pass no persisted start working exactly as before.
    resolved_start = cohort_start
    if resolved_start is None:
        resolved_start = min(session_dates) if session_dates else None
    gate_contract = _cohort_contract_for_gate(selected, configs, benchmark, resolved_start)
    gate_result: operational_gate.OperationalGateResult | None = None
    gate_message: str | None = None
    if gate_contract is None:
        gate_message = "Persisted cohort identity is insufficient for the operational gate."
    else:
        try:
            gate_result = operational_gate.assess_operational_usefulness(
                cohort=gate_contract,
                runs=runs,
                observations=observations,
                sleeve_configs=configs,
                recorded_comparison=report,
                # Only *recorded* evidence is supplied. `accounting_evidence` and
                # `operator_decisions` each return None until a real record exists, so
                # the hard-coded None is gone without ever asserting that an unreviewed
                # cohort was reviewed and found clean.
                accounting_evidence=(
                    None if review is None else cohort_review.accounting_evidence(review)
                ),
                operator_decisions=(
                    None if review is None else cohort_review.operator_decisions(review)
                ),
                as_of=as_of,
                now_et=now_et,
            )
        except ValueError as exc:
            gate_message = f"Operational gate unavailable: {exc}"

    phase_assessment = cohort_phase.assess_cohort_phase(
        cohort_id=selected,
        runs=runs,
        observations=observations,
        gate=gate_result,
        as_of=as_of,
        now_et=now_et,
        # The persisted start where the caller supplied one, not the earliest recorded
        # session. A cohort created and not yet run has no recorded session to infer
        # from, so inference reported it as never scheduled during precisely the window
        # an operator looks at it to confirm the setup.
        start_session=resolved_start,
    )
    gate_view = (
        OperationalGateView(message=gate_message)
        if gate_result is None
        else _gate_view(gate_result, phase_assessment.phase)
    )

    member_names = [config.name for config in configs]
    starting_cash = {config.starting_cash for config in configs}
    benchmark_config = next((cfg for cfg in configs if cfg.name == benchmark), None)
    identity = CohortIdentityView(
        cohort_id=selected,
        member_sleeves=[config.identity for config in configs],
        member_names=member_names,
        benchmark_sleeve=benchmark_config.identity if benchmark_config else None,
        benchmark_sleeve_name=benchmark_config.name if benchmark_config else None,
        starting_capital_per_sleeve=(
            next(iter(starting_cash)) if len(starting_cash) == 1 else None
        ),
        created_at=min(config.created_at for config in configs),
        first_session=min(session_dates) if session_dates else None,
        latest_session=max(session_dates) if session_dates else None,
    )
    definitions = [
        CohortSleeveView(
            sleeve_id=config.identity,
            sleeve_name=config.name,
            strategy=config.strategy,
            reproducible=config.reproducible,
            configuration_hash=config.configuration_hash or None,
            definition=config.definition.model_dump(mode="json") if config.definition else None,
        )
        for config in configs
    ]
    return CohortDashboardView(
        available=True,
        selection=selection,
        identity=identity,
        phase=_phase_view(phase_assessment),
        sleeve_names=names,
        sleeve_definitions=definitions,
        comparison=comparison_view,
        run_health=run_health,
        readiness=readiness,
        operational_gate=gate_view,
        cohort_review=_review_view(review, names, review_error=review_error),
    )


def collect_regime(settings: Settings, client_factory: ClientFactory | None) -> RegimeView:
    """The market-regime score + gross-exposure cap (network; memoized, fails soft).

    Needs SPY history plus a small breadth universe. History is day-cached and the
    result is memoized for a few minutes, so the frequent page refreshes don't refetch.
    """
    global _regime_memo
    if client_factory is None:
        return RegimeView(available=False, message="Live data disabled for this server.")
    now = datetime.now(UTC)
    if _regime_memo is not None and now - _regime_memo[0] < _REGIME_TTL:
        return _regime_memo[1]
    breadth_universe = universes.get_preset("mega-cap") or []
    try:
        cache = history_cache.HistoryCache(settings.history_cache_dir)
        with client_factory() as client:
            spy = cache.get(client, "SPY", days=260)
            universe = {symbol: cache.get(client, symbol, days=260) for symbol in breadth_universe}
    except (oauth.OAuthError, api.ApiError, OSError) as exc:
        return RegimeView(available=False, message=f"Regime unavailable: {exc}")
    universe = {symbol: bars for symbol, bars in universe.items() if bars}
    if len(spy) < 200 or not universe:
        return RegimeView(available=False, message="Not enough history for a regime read.")
    sig = signals.regime_signal(spy, universe)
    view = RegimeView(
        available=True,
        score=sig.score,
        gross_exposure_cap=sig.gross_exposure_cap,
        spy_above_200dma=sig.spy_above_200dma,
        trend_50_over_200=sig.trend_50_over_200,
        breadth_above_50=sig.breadth_above_50,
        calm_volatility=sig.calm_volatility,
        breadth_pct=sig.breadth_pct,
    )
    _regime_memo = (now, view)
    return view


def build_dashboard_data(
    settings: Settings,
    *,
    client_factory: ClientFactory | None,
    benchmark: str,
    requested_cohort: str | None = None,
) -> DashboardData:
    """Assemble every section fresh (called on each page request)."""
    rows, curves = collect_sleeves(settings, benchmark)
    positions = collect_positions(settings, client_factory)
    safety_view = collect_safety(settings)
    day_pl: Decimal | None = None
    if positions.available:
        day_pl = sum((p.day_pl for p in positions.positions if p.day_pl is not None), Decimal(0))
    summary = SummaryView(
        account=positions.account,
        liquidation_value=positions.liquidation_value,
        day_pl=day_pl,
        cash_available=positions.cash_available_for_trading,
        kill_engaged=safety_view.kill_engaged,
    )
    return DashboardData(
        generated_at=datetime.now(UTC),
        live_enabled=client_factory is not None,
        benchmark=benchmark,
        sleeves=rows,
        curves=curves,
        safety=safety_view,
        positions=positions,
        summary=summary,
        orders=collect_orders(settings, client_factory),
        validation=collect_validation(settings),
        approvals=collect_approvals(settings),
        regime=collect_regime(settings, client_factory),
        tax_lots=collect_tax_lots(settings),
        audit=collect_audit(settings),
        reconciliation=collect_reconciliation(settings),
        cohort=collect_cohort_dashboard(
            settings, requested_cohort=requested_cohort, benchmark=benchmark
        ),
    )


# --- HTML rendering (pure) --------------------------------------------------


def _money(value: Decimal | None) -> str:
    if value is None:
        return "-"
    return f"${value:,.2f}"


def _pct(value: Decimal | None, *, signed: bool = False) -> str:
    if value is None:
        return "-"
    return f"{value:+.2f}%" if signed else f"{value:.2f}%"


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _sign_class(value: Decimal) -> str:
    if value > 0:
        return "up"
    if value < 0:
        return "down"
    return "muted"


def sparkline_svg(points: list[Decimal], *, width: int = 160, height: int = 36) -> str:
    """A minimal inline-SVG line of the equity curve (no axes, no libraries).

    Returns a dash when there are fewer than two points. The line is green when the
    series ends above where it started, red when below.
    """
    if len(points) < 2:
        return '<span class="muted">-</span>'
    values = [float(v) for v in points]
    lo, hi = min(values), max(values)
    span = hi - lo or 1.0
    step = width / (len(values) - 1)
    pad = 3
    inner = height - 2 * pad
    coords = []
    for i, value in enumerate(values):
        x = i * step
        y = pad + inner * (1 - (value - lo) / span)  # invert: SVG y grows downward
        coords.append(f"{x:.1f},{y:.1f}")
    up = values[-1] >= values[0]
    stroke = "#2f9e44" if up else "#e03131"
    pts = " ".join(coords)
    return (
        f'<svg class="spark" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="none" role="img" aria-label="equity curve">'
        f'<polyline points="{pts}" fill="none" stroke="{stroke}" stroke-width="1.5" '
        f'stroke-linejoin="round" stroke-linecap="round"/></svg>'
    )


def _table(headers: list[str], rows: list[list[str]], *, empty: str) -> str:
    """A right-aligned data grid; the first two columns are left-aligned by CSS."""
    if not rows:
        return f'<p class="muted">{_esc(empty)}</p>'
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<table class="grid"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def _summary_html(view: SummaryView) -> str:
    def card(label: str, value: str, cls: str = "") -> str:
        return (
            f'<div class="stat"><span class="k">{_esc(label)}</span>'
            f'<span class="v {cls}">{value}</span></div>'
        )

    pl_cls = _sign_class(view.day_pl) if view.day_pl is not None else "muted"
    kill = (
        '<span class="down">ENGAGED</span>'
        if view.kill_engaged
        else '<span class="up">clear</span>'
    )
    return (
        '<div class="hero">'
        + card("Account", _esc(view.account) or "-")
        + card("Liquidation value", _money(view.liquidation_value))
        + card("Day P/L", _pct_money(view.day_pl), pl_cls)
        + card("Cash for trading", _money(view.cash_available))
        + card("Kill switch", kill)
        + "</div>"
    )


def _pct_money(value: Decimal | None) -> str:
    if value is None:
        return "-"
    return f"{value:+,.2f}"


def _sleeves_html(rows: list[SleeveRow], curves: list[EquitySeries], benchmark: str) -> str:
    if not rows:
        return '<p class="muted">No sleeves yet.</p>'
    # Join by stable id: display names are not unique across scopes.
    curve_by_id = {c.sleeve_id: c.points for c in curves}
    has_excess = any(r.excess_pct is not None for r in rows)
    headers = ["#", "name", "scope", "strategy", "cycles", "trades", "value", "return"]
    if has_excess:
        headers.append(f"vs {benchmark}")
    headers += ["max DD", "sharpe", "equity"]

    body: list[list[str]] = []
    for r in rows:
        cells = [
            str(r.rank),
            f'<span class="name">{_esc(r.name)}</span>',
            _esc(r.cohort_id or r.scope.value),
            _esc(r.strategy),
            str(r.cycles),
            str(r.trades),
            _money(r.value),
            f'<span class="{_sign_class(r.return_pct)}">{_pct(r.return_pct, signed=True)}</span>',
        ]
        if has_excess:
            if r.is_benchmark or r.excess_pct is None:
                cells.append('<span class="muted">-</span>')
            else:
                cells.append(
                    f'<span class="{_sign_class(r.excess_pct)}">'
                    f"{_pct(r.excess_pct, signed=True)}</span>"
                )
        cells += [
            f"-{r.max_drawdown_pct:.1f}%",
            str(r.sharpe) if r.sharpe is not None else "-",
            sparkline_svg(curve_by_id.get(r.sleeve_id, [])),
        ]
        body.append(cells)
    return _table(headers, body, empty="No sleeves yet.")


def _positions_html(view: PositionsView) -> str:
    if not view.available:
        return f'<p class="muted">{_esc(view.message or "Unavailable.")}</p>'
    body = [
        [
            f'<span class="name">{_esc(p.symbol)}</span>',
            f"{p.quantity:g}",
            f"{p.settled:g}",
            _money(p.average_price),
            _money(p.market_value),
            (
                f'<span class="{_sign_class(p.day_pl)}">{_money(p.day_pl)}</span>'
                if p.day_pl is not None
                else "-"
            ),
        ]
        for p in view.positions
    ]
    table = _table(
        ["symbol", "qty", "settled", "avg", "mkt value", "day P/L"],
        body,
        empty="No open positions.",
    )
    summary = (
        '<p class="summary">'
        f"Liquidation {_money(view.liquidation_value)} &middot; "
        f"cash {_money(view.cash_available_for_trading)} &middot; "
        f"withdrawable {_money(view.cash_available_for_withdrawal)}</p>"
    )
    return table + summary


def _orders_html(view: OrdersView) -> str:
    if not view.available:
        return f'<p class="muted">{_esc(view.message or "Unavailable.")}</p>'
    body = []
    for o in view.orders:
        status = f'<span class="badge">{_esc(o.status)}</span>' if o.working else _esc(o.status)
        body.append(
            [
                _esc(o.order_id),
                status,
                _esc(o.side or "-"),
                f'<span class="name">{_esc(o.symbol or "-")}</span>',
                f"{o.quantity:g}" if o.quantity is not None else "-",
                f"{o.filled:g}" if o.filled is not None else "-",
                _money(o.limit_price),
            ]
        )
    return _table(
        ["id", "status", "side", "symbol", "qty", "filled", "limit"],
        body,
        empty=f"No orders in the last {view.hours}h.",
    )


def _validation_html(view: ValidationView) -> str:
    body = []
    for r in view.rows:
        verdict = (
            '<span class="up">VALIDATED</span>' if r.validated else '<span class="down">no</span>'
        )
        if r.mean_excess_pct is not None:
            excess = (
                f'<span class="{_sign_class(r.mean_excess_pct)}">'
                f"{_pct(r.mean_excess_pct, signed=True)}</span>"
            )
        else:
            excess = '<span class="muted">-</span>'
        body.append(
            [
                f'<span class="name">{_esc(r.strategy)}</span>',
                _esc(r.universe),
                verdict,
                f"{r.pass_rate:.0%} / {r.min_pass_rate:.0%}",
                excess,
            ]
        )
    return _table(
        ["strategy", "universe", "validated", "pass / min", "excess"],
        body,
        empty="No validation verdicts yet. Run 'validate run'.",
    )


def _approvals_html(view: ApprovalsView) -> str:
    if not view.rows:
        return '<p class="muted">No pending approvals.</p>'
    body = []
    for r in view.rows:
        body.append(
            [
                f'<span class="name">{_esc(r.describe)}</span>',
                f'<code class="tok">{_esc(r.token)}</code>',
                f"{r.expires_in_min}m",
            ]
        )
    table = _table(["order", "approve token", "expires"], body, empty="No pending approvals.")
    note = (
        '<p class="summary">Approve on the CLI: '
        "<code>schwab-trader agent approve &lt;token&gt;</code> "
        "(the dashboard never places orders).</p>"
    )
    return table + note


def _regime_html(view: RegimeView) -> str:
    if not view.available:
        return f'<p class="muted">{_esc(view.message or "Unavailable.")}</p>'

    def flag(ok: bool, label: str) -> str:
        cls = "up" if ok else "down"
        mark = "on" if ok else "off"
        return f'<tr><th>{_esc(label)}</th><td><span class="{cls}">{mark}</span></td></tr>'

    rows = (
        f"<tr><th>Score</th><td>{view.score} / 4</td></tr>"
        f"<tr><th>Gross-exposure cap</th><td>{view.gross_exposure_cap * 100:.0f}%</td></tr>"
        + flag(view.spy_above_200dma, "SPY &gt; 200DMA")
        + flag(view.trend_50_over_200, "50DMA &gt; 200DMA")
        + flag(view.breadth_above_50, f"Breadth &gt; 50% ({view.breadth_pct * 100:.0f}%)")
        + flag(view.calm_volatility, "Calm volatility")
    )
    return f'<table class="kv">{rows}</table>'


def _tax_lots_html(view: TaxLotsView) -> str:
    body = [
        [
            f'<span class="name">{_esc(r.symbol)}</span>',
            f"{r.quantity:g}",
            _money(r.cost_per_share),
            r.acquired.isoformat(),
            str(r.days_held),
            ('<span class="up">long</span>' if r.long_term else '<span class="muted">short</span>'),
        ]
        for r in view.rows
    ]
    return _table(
        ["symbol", "qty", "cost/sh", "acquired", "days", "term"],
        body,
        empty="No tax lots recorded (they accrue from fills).",
    )


def _audit_html(view: AuditView) -> str:
    body = [
        [
            _esc(r.ts.astimezone().strftime("%m-%d %H:%M:%S")),
            _esc(r.command),
            _esc(r.event),
            _esc(r.detail or ""),
        ]
        for r in view.rows
    ]
    return _table(["time", "command", "event", "detail"], body, empty="No audit entries yet.")


def _reconciliation_html(view: ReconciliationView) -> str:
    if view.completed_at is None:
        return '<p class="summary">Never run. Start with <code>schwab-trader reconcile</code>.</p>'
    result = (
        '<span class="up">complete</span>' if view.success else '<span class="down">failed</span>'
    )
    rows = [
        ("Last run", _esc(view.completed_at.astimezone().strftime("%Y-%m-%d %H:%M:%S"))),
        ("Result", result),
        ("Orders seen", str(view.orders_seen)),
        ("Transitions", str(view.transitions)),
        ("Fills applied", str(view.fills_applied)),
        ("Discrepancies", str(view.discrepancies)),
        ("Uncertain fill writes", str(view.pending_fill_applications)),
    ]
    body = "".join(f"<tr><th>{key}</th><td>{value}</td></tr>" for key, value in rows)
    return f'<table class="kv">{body}</table>'


def _safety_html(view: SafetyView) -> str:
    if view.kill_engaged:
        since = view.kill_since.isoformat() if view.kill_since else "?"
        kill = f'<span class="down">ENGAGED</span> <span class="muted">({_esc(since)})</span>'
    else:
        kill = '<span class="up">clear</span>'

    def limit(value: Decimal) -> str:
        return _money(value) if value else '<span class="muted">off</span>'

    rows = [
        ("Kill switch", kill),
        ("Reason", _esc(view.kill_reason) if view.kill_reason else '<span class="muted">-</span>'),
        ("Trades today", str(view.trades_today)),
        ("Realized P&amp;L today", _money(view.realized_pnl)),
        (
            "Opening equity today",
            _money(view.start_equity)
            if view.start_equity is not None
            else '<span class="muted">-</span>',
        ),
        ("Capital cap", limit(view.capital_cap)),
        ("Daily loss limit", limit(view.daily_loss_limit)),
        (
            "Max trades/day",
            str(view.max_trades_per_day)
            if view.max_trades_per_day
            else '<span class="muted">off</span>',
        ),
        ("Max order notional", _money(view.max_order_notional)),
    ]
    body = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows)
    table = f'<table class="kv">{body}</table>'
    if view.kill_engaged:
        action = (
            '<p class="summary">Halted. Resume on the CLI: '
            "<code>schwab-trader safety resume</code>.</p>"
        )
    else:
        action = (
            '<form class="panic" method="post" action="/kill">'
            '<input type="text" name="reason" placeholder="reason (optional)" maxlength="120">'
            '<button type="submit">Engage kill switch</button>'
            "</form>"
        )
    return table + action


_STYLE = """
:root { color-scheme: light dark; --up:#2f9e44; --down:#e03131; --muted:#97a1ac; }
* { box-sizing: border-box; }
body { margin: 0; font: 14px/1.5 system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
  background: #f6f7f9; color: #1a1d21; }
header { padding: 14px 22px; background: #11161c; color: #e9edf1; display: flex;
  align-items: baseline; gap: 14px; flex-wrap: wrap; position: sticky; top: 0; z-index: 5; }
header h1 { font-size: 16px; margin: 0; font-weight: 650; }
header .tag { font-size: 12px; padding: 2px 8px; border-radius: 999px; background: #223; }
header .muted { color: #8b98a5; font-size: 12px; }
header .readonly { margin-left: auto; font-size: 12px; color: #7bdff2; }
.killbar { background: #e03131; color: #fff; text-align: center; padding: 8px;
  font-weight: 650; letter-spacing: .02em; }
main { padding: 20px; max-width: 1280px; margin: 0 auto; display: grid; gap: 18px; }
.hero { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.stat { background: #fff; border: 1px solid #e3e7eb; border-radius: 10px; padding: 12px 14px;
  display: flex; flex-direction: column; gap: 3px; }
.stat .k { font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: #5b6672; }
.stat .v { font-size: 20px; font-weight: 650; font-variant-numeric: tabular-nums; }
.panels { display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 18px; }
section { background: #fff; border: 1px solid #e3e7eb; border-radius: 10px; padding: 16px 18px;
  overflow-x: auto; }
section.wide { grid-column: 1 / -1; }
section h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .04em;
  color: #5b6672; margin: 0 0 12px; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
.grid th, .grid td { text-align: right; padding: 6px 10px; border-bottom: 1px solid #eef1f4;
  white-space: nowrap; }
.grid th:nth-child(-n+2), .grid td:nth-child(-n+2) { text-align: left; }
.grid thead th { color: #5b6672; font-weight: 600; font-size: 12px; }
.kv th { text-align: left; color: #5b6672; font-weight: 500; padding: 5px 14px 5px 0; }
.kv td { text-align: right; padding: 5px 0; font-variant-numeric: tabular-nums; }
.name { font-weight: 600; }
.up { color: var(--up); } .down { color: var(--down); } .muted { color: var(--muted); }
.badge { font-size: 11px; padding: 1px 7px; border-radius: 999px;
  background: #e7f5ff; color: #1971c2; }
.tok { font-size: 11px; word-break: break-all; }
.summary { color: #5b6672; margin: 12px 0 0; font-size: 13px; }
.summary code, section code { background: #eef1f4; padding: 1px 5px;
  border-radius: 4px; font-size: 12px; }
.spark { vertical-align: middle; }
.panic { margin-top: 12px; display: flex; gap: 8px; }
.panic input { flex: 1; padding: 6px 9px; border: 1px solid #d6dbe1;
  border-radius: 7px; background: #fff; color: inherit; }
.panic button { padding: 6px 12px; border: 0; border-radius: 7px; background: #e03131; color: #fff;
  font-weight: 600; cursor: pointer; }
footer { text-align: center; color: #97a1ac; font-size: 12px; padding: 8px 0 26px; }
@media (prefers-color-scheme: dark) {
  body { background: #0d1117; color: #e6edf3; }
  .stat, section { background: #161b22; border-color: #26303b; }
  section h2, .grid thead th, .kv th, .stat .k { color: #8b98a5; }
  .grid th, .grid td { border-color: #21272e; }
  .badge { background: #0b2b45; color: #74c0fc; }
  .summary code, section code { background: #21272e; }
  .panic input { background: #0d1117; border-color: #30363d; }
}
"""


def render_page(data: DashboardData, *, refresh: int = 30) -> str:
    """Render the whole dashboard as one self-contained HTML document."""
    ts = data.generated_at.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    killbar = (
        '<div class="killbar">KILL SWITCH ENGAGED &mdash; autonomous trading is halted</div>'
        if data.safety.kill_engaged
        else ""
    )
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{refresh}">
<title>schwab-trader dashboard</title>
<style>{_STYLE}</style>
</head><body>
<header>
  <h1>schwab-trader</h1>
  <span class="tag">{_esc(data.summary.account)}</span>
  <span class="muted">updated {_esc(ts)} &middot; auto-refresh {refresh}s</span>
  <span class="readonly">read-only (kill switch excepted)</span>
</header>
{killbar}
<main>
  {_summary_html(data.summary)}
  <div class="panels">
    <section><h2>Positions (live)</h2>{_positions_html(data.positions)}</section>
    <section><h2>Orders (live)</h2>{_orders_html(data.orders)}</section>
    <section class="wide"><h2>Sleeve comparison</h2>
      {_sleeves_html(data.sleeves, data.curves, data.benchmark)}</section>
    <section><h2>Strategy validation</h2>{_validation_html(data.validation)}</section>
    <section><h2>Pending approvals</h2>{_approvals_html(data.approvals)}</section>
    <section><h2>Market regime</h2>{_regime_html(data.regime)}</section>
    <section><h2>Autonomous safety</h2>{_safety_html(data.safety)}</section>
    <section><h2>Reconciliation health</h2>{_reconciliation_html(data.reconciliation)}</section>
    <section><h2>Tax lots</h2>{_tax_lots_html(data.tax_lots)}</section>
    <section class="wide"><h2>Recent activity (audit)</h2>{_audit_html(data.audit)}</section>
  </div>
</main>
<footer>Local dashboard &middot; read-only except the kill switch</footer>
</body></html>"""


def _error_page(message: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>dashboard error</title></head><body>"
        f"<h1>Dashboard error</h1><p>{_esc(message)}</p></body></html>"
    )


# --- HTTP server (read-only, plus a kill-switch-engage POST) ----------------

# Where the built React frontend lives (cwd-relative, like the app's other paths).
DEFAULT_FRONTEND_DIR = Path("frontend") / "dist"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".map": "application/json",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}


#: HTTP status for each :class:`~schwab_trader.sleeve_detail.SleeveDetailError` code. A
#: malformed identity is the caller's fault, an unregistered one is simply absent, and an
#: unreadable registry is a server-side condition — all three are distinguishable without
#: any of them revealing why.
_DETAIL_ERROR_STATUS = {
    "invalid-sleeve-id": 400,
    "unknown-sleeve": 404,
    "registry-unavailable": 503,
}


def _detail_error_body(code: str, message: str) -> str:
    """A sanitized JSON error for ``/api/sleeve``.

    Built through the JSON encoder rather than an f-string so a message containing a
    quote cannot produce malformed JSON, and carrying only a stable code plus a
    pre-written operator sentence — never an exception, path, or connection string.
    """
    return json.dumps({"error": {"code": code, "message": message}})


def _int_param(raw: str | None) -> int | None:
    """Parse an optional integer query parameter, treating junk as absent.

    Absent means "use the contract default", which is the useful behavior for a
    bookmarked or hand-edited URL; the response always reports the window it actually
    applied, so a discarded value is never mistaken for an honored one.
    """
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _make_handler(
    settings: Settings,
    client_factory: ClientFactory | None,
    benchmark: str,
    refresh: int,
    frontend_dir: Path,
) -> type[BaseHTTPRequestHandler]:
    dist_root = frontend_dir.resolve()

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "schwab-trader-dashboard/3.0"

        def _send(
            self, status: int, body: str, content_type: str = "text/html; charset=utf-8"
        ) -> None:
            self._send_bytes(status, body.encode("utf-8"), content_type)

        def _send_bytes(self, status: int, payload: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(payload)

        def _serve_static(self, path: str) -> bool:
            """Serve a file from the built frontend; False if it can't be served.

            Resolves the candidate inside the dist root (path-traversal safe) and
            only ever reads files — no directory listings.
            """
            rel = path.lstrip("/") or "index.html"
            candidate = (dist_root / rel).resolve()
            if not candidate.is_relative_to(dist_root):  # traversal guard
                return False
            if not candidate.is_file():
                return False
            content_type = _CONTENT_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
            self._send_bytes(200, candidate.read_bytes(), content_type)
            return True

        def _serve_sleeve_detail(self, params: dict[str, list[str]]) -> None:
            """One sleeve's read-only detail record, selected by stable identity.

            Never resolves a display name, and never widens to "all sleeves": the
            identity is required, and an absent or malformed one is refused instead of
            being interpreted. Errors carry a stable code and a pre-written sentence, so
            no internal exception, path, or connection string can reach the client.
            """
            try:
                detail = sleeve_detail.collect_sleeve_detail(
                    settings,
                    params.get("sleeve_id", [""])[0],
                    limit=_int_param(params.get("limit", [None])[0]),
                    offset=_int_param(params.get("offset", [None])[0]),
                )
            except sleeve_detail.SleeveDetailError as exc:
                self._send(
                    _DETAIL_ERROR_STATUS.get(exc.code, 400),
                    _detail_error_body(exc.code, exc.message),
                    "application/json; charset=utf-8",
                )
                return
            except Exception:
                logger.exception("Sleeve detail collection failed")
                self._send(
                    500,
                    _detail_error_body(
                        "detail-unavailable",
                        "The sleeve detail record could not be assembled. See the local "
                        "dashboard log for the cause.",
                    ),
                    "application/json; charset=utf-8",
                )
                return
            self._send(200, detail.model_dump_json(), "application/json; charset=utf-8")

        def do_GET(self) -> None:
            query = urllib.parse.urlsplit(self.path).query
            params = urllib.parse.parse_qs(query)
            requested_cohort = params.get("cohort", [""])[0].strip() or None
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                self._send(200, "ok", "text/plain; charset=utf-8")
                return
            if path == "/api/sleeve":
                self._serve_sleeve_detail(params)
                return
            if path == "/api/data":
                try:
                    data = build_dashboard_data(
                        settings,
                        client_factory=client_factory,
                        benchmark=benchmark,
                        requested_cohort=requested_cohort,
                    )
                    self._send(200, data.model_dump_json(), "application/json; charset=utf-8")
                except Exception as exc:
                    logger.exception("Dashboard data collection failed")
                    self._send(
                        500,
                        f'{{"error": "{exc.__class__.__name__}"}}',
                        "application/json; charset=utf-8",
                    )
                return
            # Built React frontend first; fall back to the legacy server-rendered
            # page when it isn't built (e.g. a machine without Node).
            if self._serve_static(path):
                return
            if path not in ("/", "/index.html"):
                self._send(404, _error_page("Not found."))
                return
            try:
                data = build_dashboard_data(
                    settings,
                    client_factory=client_factory,
                    benchmark=benchmark,
                    requested_cohort=requested_cohort,
                )
                self._send(200, render_page(data, refresh=refresh))
            except Exception as exc:
                logger.exception("Dashboard render failed")
                self._send(500, _error_page(f"Render failed: {exc}"))

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            # The ONLY state-changing route: engage the kill switch (a safety halt that
            # can only stop trading). Resuming stays on the CLI; nothing here places an order.
            if path == "/kill":
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
                fields = urllib.parse.parse_qs(raw)
                reason = (fields.get("reason", [""])[0] or "engaged from dashboard").strip()[:120]
                safety.KillSwitch(settings.kill_switch_path).engage(reason)
                logger.warning("Kill switch engaged from dashboard: %s", reason)
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()
                return
            self._send(405, _error_page("Read-only except POST /kill (engage kill switch)."))

        def _reject_mutation(self) -> None:
            self._send(405, _error_page("Read-only except POST /kill (engage kill switch)."))

        do_PUT = _reject_mutation
        do_DELETE = _reject_mutation
        do_PATCH = _reject_mutation

        def log_message(self, format: str, *args: object) -> None:
            logger.debug("dashboard %s - %s", self.address_string(), format % args)

    return DashboardHandler


def _is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class NonLoopbackHostError(Exception):
    """Raised when asked to bind the dashboard to a non-loopback host without force."""


def make_server(
    settings: Settings,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    client_factory: ClientFactory | None = None,
    benchmark: str = "bench-spy",
    allow_remote: bool = False,
    refresh: int = 30,
    frontend_dir: Path = DEFAULT_FRONTEND_DIR,
) -> ThreadingHTTPServer:
    """Build (but do not start) the dashboard HTTP server.

    Refuses a non-loopback ``host`` unless ``allow_remote`` is set, because the page
    renders account positions and balances - it is meant for the local machine only.
    Serves the built React frontend from ``frontend_dir`` when present, otherwise the
    legacy server-rendered page.
    """
    if not allow_remote and not _is_loopback(host):
        raise NonLoopbackHostError(
            f"Refusing to bind the dashboard to non-loopback host '{host}'. It serves account "
            "data and is meant for localhost. Pass allow_remote=True only if you understand this."
        )
    handler = _make_handler(settings, client_factory, benchmark, refresh, frontend_dir)
    return ThreadingHTTPServer((host, port), handler)


def serve(
    settings: Settings,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    client_factory: ClientFactory | None = None,
    benchmark: str = "bench-spy",
    allow_remote: bool = False,
    refresh: int = 30,
    frontend_dir: Path = DEFAULT_FRONTEND_DIR,
) -> None:
    """Serve the dashboard until interrupted (blocking)."""
    server = make_server(
        settings,
        host=host,
        port=port,
        client_factory=client_factory,
        benchmark=benchmark,
        allow_remote=allow_remote,
        refresh=refresh,
        frontend_dir=frontend_dir,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
