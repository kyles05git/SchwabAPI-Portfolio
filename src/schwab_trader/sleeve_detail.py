"""Read-only detail for exactly one paper sleeve, addressed by stable identity.

The cohort dashboard answers "which sleeve is ahead?". This module answers the next
question — "what *is* that sleeve, what does it hold, and what has it actually done?" —
for one sleeve at a time, and nothing else.

Design constraints, all of which are load-bearing:

- **Stable identity only.** A sleeve is selected by the identity published in
  ``/api/data`` (:attr:`~schwab_trader.sleeves.SleeveConfig.identity`) and by nothing
  else. Display names are not unique — the same name legitimately exists in an active
  cohort and in the superseded one it replaced — so this module never resolves a name.
  :func:`resolve_sleeve` matches identities exactly and refuses to guess, which is why
  it walks the registry instead of calling ``SleeveStore.resolve``: that helper falls
  back to a global name lookup, and a global name lookup is the exact defect this view
  exists to make impossible.

- **Lazily requested.** Nothing here runs during a ``/api/data`` poll. The dashboard's
  every-30-seconds payload stays the size it was; detail is fetched only when an
  operator opens one sleeve.

- **Recorded values are authoritative, and no new quote is ever fetched.** Positions
  carry quantity, average cost, and cost basis, because those are stored exactly.
  A *current* market value needs a mark this module is not allowed to go and get, so
  per-position market value and unrealized P&L are reported as unavailable rather than
  silently marked at cost. Marked equity comes from the last recorded valuation, with
  the timestamp it was recorded at.

- **Read-only, including the probe.** Opening a paper engine for a sleeve that has no
  paper account row would *create* one. :func:`_paper_state_exists` checks first, so a
  sleeve with no paper state reports "no paper state recorded" instead of quietly
  gaining an account. Nothing in this module writes.

- **Bounded.** Every history is windowed through :class:`PageInfo` with a conservative
  maximum page size, and each source is scanned only up to :data:`SOURCE_SCAN_LIMIT`
  records, which the page reports honestly via :attr:`PageInfo.truncated`.

- **Fails soft.** Each section carries ``available`` and ``message``. A missing or
  unreadable record produces an explanation, never a traceback and never a fabricated
  zero.

Nothing here can mutate a sleeve, cohort, position, order, cycle, or observation, and
there is deliberately no action, override, or repair path of any kind. A superseded
sleeve stays fully inspectable and is labelled closed.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError

from schwab_trader import cohort_lifecycle, evaluation, paper, sleeves
from schwab_trader.storage import factory as storage_factory

if TYPE_CHECKING:
    from schwab_trader.config import Settings

logger = logging.getLogger(__name__)

#: Contract version for ``GET /api/sleeve``. Bump on any breaking field change.
DETAIL_CONTRACT_VERSION = "1.0"

#: Default and maximum window size for every history section. The maximum is
#: deliberately conservative: this endpoint exists to inspect one sleeve, not to export
#: its whole history, and an unbounded page is how a "read-only" view becomes a way to
#: pull the entire store in one request.
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200

#: Hard ceiling on records read from any single source before windowing. The stores
#: expose ``limit``-style readers rather than offset queries, so the window is taken in
#: memory; this bounds that read. A page that hit the ceiling says so.
SOURCE_SCAN_LIMIT = 2_000

#: A stable sleeve identity is either a 64-character hex digest (shared storage) or a
#: validated sleeve name (the local-SQLite layout, where the name *is* the identity).
#: Both fit this character class, and anything outside it is rejected before any lookup
#: rather than being handed to the storage layer.
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: Storage faults this module treats as "this section is unavailable" rather than as a
#: crash. A legacy database missing newer columns must not take out the whole view.
_STORAGE_ERRORS = (OSError, sqlite3.DatabaseError, SQLAlchemyError, ValueError, RuntimeError)

_MARK_UNAVAILABLE = (
    "No current mark is recorded for this position, and this view never requests a "
    "quote. Cost basis is exact; market value and unrealized P&L are unavailable."
)


class SleeveScope(StrEnum):
    """Which comparability group a sleeve belongs to.

    Mirrors :class:`schwab_trader.dashboard.SleeveScope`, which imports these values so
    the leaderboard and the detail view can never disagree about what a sleeve is.
    """

    OFFICIAL_COHORT = "official-cohort"
    """A member of a persisted experiment cohort with a fixed definition and benchmark."""

    LEGACY = "legacy"
    """An unassigned sleeve carrying lifetime history from before the cohort design."""

    STANDALONE = "standalone"
    """An unassigned sleeve with no recorded history yet."""


def scope_of(config: sleeves.SleeveConfig, *, has_history: bool) -> SleeveScope:
    """Which comparability group this sleeve belongs to."""
    if config.cohort_id:
        return SleeveScope.OFFICIAL_COHORT
    return SleeveScope.LEGACY if has_history else SleeveScope.STANDALONE


class SleeveDetailError(Exception):
    """A request this module refuses, carrying the operator-facing reason.

    ``code`` is stable and machine-readable; ``message`` is safe to render. Neither ever
    contains an internal exception, a connection string, or a filesystem path.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class InvalidSleeveId(SleeveDetailError):
    """The requested identity is malformed, so no lookup was attempted."""

    def __init__(self, message: str) -> None:
        super().__init__("invalid-sleeve-id", message)


class UnknownSleeve(SleeveDetailError):
    """The requested identity is well-formed but is not a registered sleeve."""

    def __init__(self, message: str) -> None:
        super().__init__("unknown-sleeve", message)


class RegistryUnavailable(SleeveDetailError):
    """The sleeve registry could not be read at all, so nothing can be resolved."""

    def __init__(self, message: str) -> None:
        super().__init__("registry-unavailable", message)


# --- Contract ---------------------------------------------------------------


class PageInfo(BaseModel):
    """The window a history section was returned through.

    ``offset`` counts back from the *most recent* record, so ``offset=0`` is always the
    newest page and pagination is stable as history grows at the recent end.
    """

    limit: int
    offset: int
    returned: int
    available: int
    """Records the source held within :data:`SOURCE_SCAN_LIMIT`, not necessarily the
    total ever recorded. See :attr:`truncated`."""

    has_more: bool
    truncated: bool = False
    """True when the source hit :data:`SOURCE_SCAN_LIMIT`, so ``available`` is a floor
    rather than an exact count. Stated rather than hidden."""


class SleeveLifecycleView(BaseModel):
    """Whether this sleeve's record is still collecting, or is kept as evidence."""

    lifecycle: str = cohort_lifecycle.CohortLifecycle.ACTIVE.value
    """``active`` or ``superseded``, from the cohort lifecycle registry."""

    historical: bool = False
    """True when the sleeve's cohort was withdrawn. The record stays readable."""

    label: str = ""
    """Badge text for a withdrawn cohort. Empty for an active one."""

    reason: str = ""
    superseded_by: str | None = None
    reference: str = ""
    """Repository-relative document holding the full record, when there is one."""

    actionable: bool = False
    """Always false. This view offers no run, order, repair, or reset action for any
    sleeve, superseded or not; the field exists so the UI states that rather than
    inferring it."""


class SleeveIdentityView(BaseModel):
    """Which exact sleeve this is, and what kind of record it belongs to."""

    sleeve_id: str
    """Stable storage/API identity. The only thing that selects this sleeve."""

    name: str
    """Display name. Not unique across cohorts; never an identity."""

    original_name: str = ""
    """The name the sleeve was created under, when it differs from ``name``."""

    strategy: str
    scope: SleeveScope
    cohort_id: str | None = None
    namespace_id: str = ""
    created_at: datetime
    lifecycle: SleeveLifecycleView = Field(default_factory=SleeveLifecycleView)


class SleeveStrategyView(BaseModel):
    """The versioned definition that makes this sleeve's decisions reproducible."""

    available: bool = False
    message: str | None = None
    strategy: str = ""
    strategy_id: str | None = None
    strategy_version: str | None = None
    implementation_name: str | None = None
    parameters: dict[str, object] = Field(default_factory=dict)
    universe_definition: object | None = None
    universe: list[str] = Field(default_factory=list)
    """The sleeve's resolved symbol list, when one was recorded on the sleeve itself."""

    factor: str = ""
    configuration_hash: str | None = None
    reproducible: bool = False
    """False for a legacy sleeve whose full parameters were never captured. Labelled,
    not faked."""

    decision_frequency: str = ""
    decision_time: str = ""
    benchmark_symbol_or_sleeve: str | None = None
    data_requirements: list[str] = Field(default_factory=list)
    long_only: bool | None = None
    leverage_allowed: bool | None = None


class SleeveCapitalView(BaseModel):
    """Starting capital and the settlement/leverage model the sleeve was configured with."""

    starting_capital: Decimal
    settlement_model: str
    """``t+1`` when settled-cash modeling is on, otherwise ``instant``."""

    leverage: Decimal
    max_positions: int
    max_position_fraction: Decimal


class SleevePositionRow(BaseModel):
    """One held paper position. Cost figures are exact; a current mark is not available."""

    symbol: str
    quantity: int
    average_cost: Decimal
    cost_basis: Decimal
    cost_basis_weight: Decimal | None = None
    """Share of the sleeve's total position cost basis. ``null`` when cost basis is
    zero. This is a cost-basis weight, not a market-value weight."""

    market_value: Decimal | None = None
    """Always ``null``: see :data:`_MARK_UNAVAILABLE`."""

    unrealized_pnl: Decimal | None = None
    """Always ``null`` for the same reason as :attr:`market_value`."""

    mark_status: str = "unavailable"


class SleevePositionsView(BaseModel):
    """Currently held paper positions, read straight from the paper store."""

    available: bool = False
    message: str | None = None
    as_of: datetime | None = None
    """When this read happened. Positions have no recorded timestamp of their own, so
    the honest ``as_of`` is the moment they were read, not a session date."""

    rows: list[SleevePositionRow] = Field(default_factory=list)
    total_cost_basis: Decimal | None = None
    mark_note: str = _MARK_UNAVAILABLE


class SleeveCashView(BaseModel):
    """Settled/unsettled cash, realized P&L, and buying power, as currently stored."""

    available: bool = False
    message: str | None = None
    as_of: datetime | None = None
    settled_cash: Decimal | None = None
    unsettled_cash: Decimal | None = None
    total_cash: Decimal | None = None
    realized_pnl: Decimal | None = None
    starting_capital: Decimal | None = None
    buying_power: Decimal | None = None
    opened_at: datetime | None = None
    """When the paper account was opened, as recorded."""


class RecordedValuationView(BaseModel):
    """The most recent *recorded* valuation. Never recomputed, never re-marked."""

    available: bool = False
    message: str | None = None
    source: str | None = None
    """``official-observation`` or ``evaluation-cycle``: which record this came from."""

    as_of: datetime | None = None
    session_date: date | None = None
    total_equity: Decimal | None = None
    positions_value: Decimal | None = None
    cash: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    realized_pnl: Decimal | None = None
    return_pct: Decimal | None = None
    return_pct_basis: str | None = None
    """Whether :attr:`return_pct` is a ``ratio`` (0.0154) or already a ``percent``
    (1.54). The two recorded sources genuinely disagree — an official observation stores
    a ratio and an evaluation cycle stores a percentage — so the unit is stated rather
    than left for the renderer to guess and get wrong by a factor of a hundred."""

    benchmark_value: Decimal | None = None
    status: str | None = None
    """Observation status (``official``/``partial``/...) when the source is an
    observation. A partial record is labelled, not treated as complete."""


class SleevePerformanceView(BaseModel):
    """Lifetime recorded performance summary for this sleeve."""

    available: bool = False
    message: str | None = None
    as_of: datetime | None = None
    """Timestamp of the latest cycle the summary was computed from."""

    first_recorded_at: datetime | None = None
    cycles: int = 0
    trades_filled: int = 0
    starting_capital: Decimal | None = None
    latest_value: Decimal | None = None
    total_return_pct: Decimal | None = None
    realized_pnl: Decimal | None = None
    max_drawdown_pct: Decimal | None = None
    sharpe: Decimal | None = None


class EquityPointView(BaseModel):
    """One recorded equity value, with the timestamp and record it came from."""

    as_of: datetime
    session_date: date | None = None
    total_value: Decimal
    source: str
    """``official-observation`` or ``evaluation-cycle``."""


class EquityHistoryView(BaseModel):
    """Bounded recorded equity series, oldest-first inside the returned window."""

    available: bool = False
    message: str | None = None
    source: str | None = None
    page: PageInfo | None = None
    points: list[EquityPointView] = Field(default_factory=list)


class CycleRowView(BaseModel):
    """One recorded valuation cycle: what was proposed, filled, rejected, and valued."""

    cycle_id: int
    as_of: datetime
    strategy: str
    num_proposals: int
    num_filled: int
    num_rejected: int
    cash: Decimal
    positions_value: Decimal
    total_value: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    return_pct: Decimal
    """Already a percentage (``1.54`` means 1.54%), as the paper valuation records it."""


class CyclesView(BaseModel):
    """Bounded recent valuation cycles, newest first."""

    available: bool = False
    message: str | None = None
    page: PageInfo | None = None
    rows: list[CycleRowView] = Field(default_factory=list)


class SimulatedOrderRowView(BaseModel):
    """One simulated paper order, including its fill or its rejection reason."""

    paper_order_id: int
    as_of: datetime
    """When the order was created."""

    side: str
    symbol: str
    quantity: int
    limit_price: Decimal
    status: str
    """``filled`` or ``rejected``, as recorded by the paper engine."""

    reason: str | None = None
    """The recorded rejection reason, when the order was rejected."""

    fill_price: Decimal | None = None
    filled_at: datetime | None = None


class SimulatedOrdersView(BaseModel):
    """Bounded recent simulated orders, newest first. Never a live broker order."""

    available: bool = False
    message: str | None = None
    page: PageInfo | None = None
    rows: list[SimulatedOrderRowView] = Field(default_factory=list)


class ObservationRowView(BaseModel):
    """One official daily observation: the cohort's authoritative per-session record."""

    session_date: date
    as_of: datetime
    """The recorded valuation time for the session."""

    decision_time: datetime
    status: str
    run_id: str
    strategy_hash: str = ""
    total_value: Decimal | None = None
    return_pct: Decimal | None = None
    """A ratio (``0.0154`` means 1.54%), as the observation records it. Deliberately a
    different unit from :attr:`CycleRowView.return_pct`; both say so."""

    benchmark_value: Decimal | None = None
    exposure: Decimal | None = None
    num_positions: int | None = None
    turnover: Decimal | None = None
    modeled_cost: Decimal | None = None
    num_filled: int = 0
    num_rejected: int = 0
    quote_coverage: Decimal | None = None
    readiness_ready: bool | None = None
    readiness_reasons: list[str] = Field(default_factory=list)
    snapshot_ids: dict[str, str] = Field(default_factory=dict)


class ObservationsView(BaseModel):
    """Bounded official observations, newest session first."""

    available: bool = False
    message: str | None = None
    page: PageInfo | None = None
    rows: list[ObservationRowView] = Field(default_factory=list)


class RunRowView(BaseModel):
    """One cohort run this sleeve was expected in, and how this sleeve fared in it."""

    run_id: str
    run_key: str
    session_id: str
    scheduled_for: date
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    member_status: str | None = None
    member_started_at: datetime | None = None
    member_completed_at: datetime | None = None
    member_error_code: str | None = None
    member_error_message: str | None = None
    """The recorded sanitized failure message for *this* member, when there was one."""

    snapshot_id: str | None = None
    quote_snapshot_id: str | None = None
    data_snapshot_ids: dict[str, str] = Field(default_factory=dict)


class RunsView(BaseModel):
    """Bounded cohort runs referencing this sleeve, newest session first."""

    available: bool = False
    message: str | None = None
    page: PageInfo | None = None
    rows: list[RunRowView] = Field(default_factory=list)


class SleeveLineageView(BaseModel):
    """The immutable identities that let this record be traced back and reconstructed."""

    namespace_id: str = ""
    cohort_id: str | None = None
    configuration_hash: str | None = None
    strategy_hashes: list[str] = Field(default_factory=list)
    """Distinct strategy hashes seen across this sleeve's observations. More than one
    means the recorded definition changed mid-collection, which is worth seeing."""

    run_ids: list[str] = Field(default_factory=list)
    """Runs referenced by the observations in this response's window."""

    snapshot_ids: dict[str, str] = Field(default_factory=dict)
    """Data/readiness snapshot identities from the most recent observation."""

    cohort_start_session: date | None = None
    notes: list[str] = Field(default_factory=list)
    """Honest gaps: fields the operator might expect that storage does not record."""


class SleeveDetail(BaseModel):
    """Everything this view can honestly say about one paper sleeve."""

    contract_version: str = DETAIL_CONTRACT_VERSION
    generated_at: datetime
    read_only: bool = True
    """Always true. There is no action path in this contract."""

    identity: SleeveIdentityView
    strategy: SleeveStrategyView = Field(default_factory=SleeveStrategyView)
    capital: SleeveCapitalView
    positions: SleevePositionsView = Field(default_factory=SleevePositionsView)
    cash: SleeveCashView = Field(default_factory=SleeveCashView)
    recorded_valuation: RecordedValuationView = Field(default_factory=RecordedValuationView)
    performance: SleevePerformanceView = Field(default_factory=SleevePerformanceView)
    equity_history: EquityHistoryView = Field(default_factory=EquityHistoryView)
    cycles: CyclesView = Field(default_factory=CyclesView)
    simulated_orders: SimulatedOrdersView = Field(default_factory=SimulatedOrdersView)
    observations: ObservationsView = Field(default_factory=ObservationsView)
    runs: RunsView = Field(default_factory=RunsView)
    lineage: SleeveLineageView = Field(default_factory=SleeveLineageView)
    warnings: list[str] = Field(default_factory=list)
    """Section-level problems collected for one prominent place in the UI."""


# --- Request validation -----------------------------------------------------


def normalize_sleeve_id(raw: str | None) -> str:
    """Validate a requested identity, or raise :class:`InvalidSleeveId`.

    Validation happens before any storage call so a malformed value never reaches a
    query, and the refusal never echoes the raw input back into the response.
    """
    candidate = (raw or "").strip()
    if not candidate:
        raise InvalidSleeveId(
            "A stable sleeve identifier is required. Open a sleeve from the dashboard "
            "rather than requesting one by name."
        )
    if not _IDENTITY_RE.match(candidate):
        raise InvalidSleeveId(
            "That sleeve identifier is not a valid stable identity. Expected up to 64 "
            "characters of letters, digits, '-' or '_'."
        )
    return candidate


def normalize_page(limit: int | None, offset: int | None) -> tuple[int, int]:
    """Clamp a requested window to the contract's bounds.

    Clamping rather than rejecting keeps a stale bookmark working, and the response
    reports the window actually used, so a clamped request is never silently
    misrepresented as the one that was asked for.
    """
    resolved_limit = DEFAULT_PAGE_LIMIT if limit is None else limit
    resolved_offset = 0 if offset is None else offset
    resolved_limit = max(1, min(MAX_PAGE_LIMIT, resolved_limit))
    resolved_offset = max(0, resolved_offset)
    return resolved_limit, resolved_offset


def _window[T](newest_first: list[T], *, limit: int, offset: int, scanned: int) -> tuple[
    list[T], PageInfo
]:
    """Take one page from a newest-first list and describe it honestly."""
    available = len(newest_first)
    page = newest_first[offset : offset + limit]
    return page, PageInfo(
        limit=limit,
        offset=offset,
        returned=len(page),
        available=available,
        has_more=offset + len(page) < available,
        truncated=scanned >= SOURCE_SCAN_LIMIT,
    )


# --- Resolution -------------------------------------------------------------


def resolve_sleeve(settings: Settings, sleeve_id: str) -> sleeves.SleeveConfig:
    """The one registered sleeve whose stable identity is exactly ``sleeve_id``.

    Deliberately implemented as an exact scan of the registry rather than through
    ``SleeveStore.resolve``, which falls back to matching a *display name* across every
    scope. That fallback is what would let a request for one sleeve return a different
    cohort's sleeve that happens to share a name, and refusing it is the point of this
    whole module.
    """
    try:
        configs = storage_factory.sleeve_store(settings).list()
    except _STORAGE_ERRORS as exc:
        logger.warning("Sleeve registry unreadable for detail request: %s", type(exc).__name__)
        raise RegistryUnavailable(
            "The sleeve registry could not be read, so no sleeve can be resolved. "
            "Check the dashboard's storage configuration."
        ) from exc

    matches = [config for config in configs if config.identity == sleeve_id]
    if not matches:
        raise UnknownSleeve(
            "No sleeve with that stable identifier is registered. It may belong to a "
            "different storage backend, or have been removed."
        )
    if len(matches) > 1:
        # Two registered sleeves sharing one stable identity would mean the identity is
        # not an identity. Refuse rather than pick, because picking is unverifiable.
        raise RegistryUnavailable(
            "That stable identifier resolves to more than one registered sleeve, which "
            "means the registry is inconsistent. Nothing is shown for it."
        )
    return matches[0]


# --- Section collectors -----------------------------------------------------


def _lifecycle_view(cohort_id: str) -> SleeveLifecycleView:
    if not cohort_id:
        return SleeveLifecycleView()
    status = cohort_lifecycle.status_for(cohort_id)
    return SleeveLifecycleView(
        lifecycle=status.lifecycle.value,
        historical=status.historical,
        label=status.label,
        reason=status.reason,
        superseded_by=status.superseded_by,
        reference=status.reference,
    )


def _strategy_view(config: sleeves.SleeveConfig) -> SleeveStrategyView:
    definition = config.definition
    if definition is None:
        return SleeveStrategyView(
            available=True,
            message=(
                "This sleeve predates versioned strategy definitions, so its full "
                "parameters were never recorded. It is labelled non-reproducible "
                "rather than shown with invented values."
            ),
            strategy=config.strategy,
            universe=list(config.universe),
            factor=config.factor,
            configuration_hash=config.configuration_hash or None,
            reproducible=False,
            decision_frequency=config.decision_frequency,
            decision_time=config.decision_time,
        )
    return SleeveStrategyView(
        available=True,
        strategy=config.strategy,
        strategy_id=definition.strategy_id,
        strategy_version=definition.strategy_version,
        implementation_name=definition.implementation_name,
        parameters=dict(definition.parameters),
        universe_definition=definition.universe_definition,
        universe=list(config.universe),
        factor=config.factor,
        configuration_hash=config.configuration_hash or definition.configuration_hash or None,
        reproducible=True,
        decision_frequency=definition.decision_frequency or config.decision_frequency,
        decision_time=definition.decision_time.isoformat(),
        benchmark_symbol_or_sleeve=definition.benchmark_symbol_or_sleeve,
        data_requirements=list(definition.data_requirements),
        long_only=definition.long_only,
        leverage_allowed=definition.leverage_allowed,
    )


def _paper_state_exists(settings: Settings, config: sleeves.SleeveConfig) -> bool:
    """Whether paper state is already recorded, without creating any.

    Constructing a paper engine bootstraps a missing account row (shared storage) or a
    missing SQLite file (local storage). Both are writes, and this view must not write,
    so existence is checked first and a sleeve with no paper state is reported as such.
    """
    shared = storage_factory.database(settings)
    if shared is None:
        return (settings.sleeves_dir / config.name / "paper.sqlite3").is_file()
    # Imported here so the module-level import graph does not pull the ORM schema in
    # for callers that only need the contract.
    from schwab_trader.storage.schema import PaperAccount as PaperAccountRow

    with shared.session() as session:
        return session.get(PaperAccountRow, config.identity) is not None


def _positions_and_cash(
    settings: Settings, config: sleeves.SleeveConfig, *, now: datetime
) -> tuple[SleevePositionsView, SleeveCashView]:
    """Current paper holdings and cash, or a soft explanation for both."""
    unavailable = (
        "No paper state is recorded for this sleeve yet. Positions and cash appear "
        "after its first valuation cycle; nothing is created by viewing it."
    )
    try:
        if not _paper_state_exists(settings, config):
            return (
                SleevePositionsView(available=False, message=unavailable),
                SleeveCashView(available=False, message=unavailable),
            )
        engine = storage_factory.paper_engine(settings, config)
        held: list[paper.PaperPosition] = list(engine.positions())
        account = engine.account()
        buying_power = engine.buying_power()
    except _STORAGE_ERRORS as exc:
        logger.warning("Paper state unreadable for sleeve detail: %s", type(exc).__name__)
        soft = (
            "The paper store for this sleeve could not be read, so positions and cash "
            "are unavailable. The rest of the record is unaffected."
        )
        return (
            SleevePositionsView(available=False, message=soft),
            SleeveCashView(available=False, message=soft),
        )

    total_cost_basis = sum(
        (position.avg_cost * position.quantity for position in held), Decimal(0)
    )
    rows = [
        SleevePositionRow(
            symbol=position.symbol,
            quantity=position.quantity,
            average_cost=position.avg_cost,
            cost_basis=position.avg_cost * position.quantity,
            cost_basis_weight=(
                (position.avg_cost * position.quantity) / total_cost_basis
                if total_cost_basis
                else None
            ),
            mark_status="unavailable",
        )
        for position in held
    ]
    positions = SleevePositionsView(
        available=True,
        message=None if rows else "This sleeve currently holds no paper positions.",
        as_of=now,
        rows=rows,
        total_cost_basis=total_cost_basis,
    )
    cash = SleeveCashView(
        available=True,
        as_of=now,
        settled_cash=account.cash,
        unsettled_cash=account.unsettled_cash,
        total_cash=account.total_cash,
        realized_pnl=account.realized_pnl,
        starting_capital=account.starting_cash,
        buying_power=buying_power,
        opened_at=account.created_at,
    )
    return positions, cash


def observation_row(observation: evaluation.OfficialDailyObservation) -> ObservationRowView:
    return ObservationRowView(
        session_date=observation.session_date,
        as_of=observation.valuation_time,
        decision_time=observation.decision_time,
        status=observation.status.value,
        run_id=observation.run_id,
        strategy_hash=observation.strategy_hash,
        total_value=observation.total_value,
        return_pct=observation.return_pct,
        benchmark_value=observation.benchmark_value,
        exposure=observation.exposure,
        num_positions=observation.num_positions,
        turnover=observation.turnover,
        modeled_cost=observation.modeled_cost,
        num_filled=observation.num_filled,
        num_rejected=observation.num_rejected,
        quote_coverage=observation.quote_coverage,
        readiness_ready=observation.readiness_ready,
        readiness_reasons=list(observation.readiness_reasons),
        snapshot_ids=dict(observation.snapshot_ids),
    )


def _cycle_row(record: evaluation.CycleRecord) -> CycleRowView:
    return CycleRowView(
        cycle_id=record.id,
        as_of=record.ts,
        strategy=record.strategy,
        num_proposals=record.num_proposals,
        num_filled=record.num_filled,
        num_rejected=record.num_rejected,
        cash=record.cash,
        positions_value=record.positions_value,
        total_value=record.total_value,
        realized_pnl=record.realized_pnl,
        unrealized_pnl=record.unrealized_pnl,
        return_pct=record.return_pct,
    )


def _order_row(order: paper.PaperOrder) -> SimulatedOrderRowView:
    return SimulatedOrderRowView(
        paper_order_id=order.id,
        as_of=order.created_at,
        side=order.side,
        symbol=order.symbol,
        quantity=order.quantity,
        limit_price=order.limit_price,
        status=order.status,
        reason=order.reason,
        fill_price=order.fill_price,
        filled_at=order.filled_at,
    )


def _recorded_valuation(
    observations: list[evaluation.OfficialDailyObservation],
    cycles: list[evaluation.CycleRecord],
) -> RecordedValuationView:
    """The latest recorded valuation, preferring the cohort's official record.

    An official observation is the cohort's authoritative per-session valuation, so it
    wins when one exists. A ``partial`` observation is still shown — with its status —
    because hiding it would leave the operator with no recorded value at all.
    """
    latest_official = next(
        (
            observation
            for observation in observations
            if observation.total_value is not None
        ),
        None,
    )
    if latest_official is not None:
        return RecordedValuationView(
            available=True,
            source="official-observation",
            as_of=latest_official.valuation_time,
            session_date=latest_official.session_date,
            total_equity=latest_official.total_value,
            return_pct=latest_official.return_pct,
            return_pct_basis="ratio",
            benchmark_value=latest_official.benchmark_value,
            status=latest_official.status.value,
            message=(
                None
                if latest_official.status is evaluation.ObservationStatus.OFFICIAL
                else (
                    "The most recent recorded valuation is not a complete official "
                    "observation. Its status is shown beside it."
                )
            ),
        )
    if cycles:
        latest = cycles[0]
        return RecordedValuationView(
            available=True,
            source="evaluation-cycle",
            as_of=latest.ts,
            total_equity=latest.total_value,
            positions_value=latest.positions_value,
            cash=latest.cash,
            unrealized_pnl=latest.unrealized_pnl,
            realized_pnl=latest.realized_pnl,
            return_pct=latest.return_pct,
            return_pct_basis="percent",
            message=(
                "No official cohort observation has been recorded, so this is the "
                "sleeve's own last valuation cycle."
            ),
        )
    return RecordedValuationView(
        available=False,
        message=(
            "No valuation has been recorded for this sleeve yet, so it has no equity, "
            "return, or P&L to report."
        ),
    )


def _performance_view(summary: evaluation.EvalSummary | None) -> SleevePerformanceView:
    if summary is None:
        return SleevePerformanceView(
            available=False,
            message=(
                "The evaluation history for this sleeve could not be read, so its "
                "lifetime performance summary is unavailable."
            ),
        )
    if summary.cycles == 0:
        return SleevePerformanceView(
            available=False,
            message=(
                "This sleeve has recorded no valuation cycles yet, so it has no "
                "performance history. This is not a zero return."
            ),
        )
    return SleevePerformanceView(
        available=True,
        as_of=summary.last_ts,
        first_recorded_at=summary.first_ts,
        cycles=summary.cycles,
        trades_filled=summary.trades_filled,
        starting_capital=summary.starting_cash,
        latest_value=summary.latest_value,
        total_return_pct=summary.total_return_pct,
        realized_pnl=summary.realized_pnl,
        max_drawdown_pct=summary.max_drawdown_pct,
        sharpe=summary.sharpe,
    )


def _equity_history(
    observations: list[evaluation.OfficialDailyObservation],
    cycles: list[evaluation.CycleRecord],
    *,
    limit: int,
    offset: int,
    scanned: int,
) -> EquityHistoryView:
    """Bounded recorded equity series, preferring official session valuations.

    Points inside the returned window are ordered oldest-first so the window plots
    directly, while the window itself is taken from the newest end like every other
    section.
    """
    official = [
        EquityPointView(
            as_of=observation.valuation_time,
            session_date=observation.session_date,
            total_value=observation.total_value,
            source="official-observation",
        )
        for observation in observations
        if observation.total_value is not None
    ]
    if official:
        page, info = _window(official, limit=limit, offset=offset, scanned=scanned)
        return EquityHistoryView(
            available=True,
            source="official-observation",
            page=info,
            points=list(reversed(page)),
        )
    from_cycles = [
        EquityPointView(as_of=record.ts, total_value=record.total_value, source="evaluation-cycle")
        for record in cycles
    ]
    if from_cycles:
        page, info = _window(from_cycles, limit=limit, offset=offset, scanned=scanned)
        return EquityHistoryView(
            available=True,
            source="evaluation-cycle",
            message=(
                "No official cohort observations exist for this sleeve, so its equity "
                "history is drawn from its own recorded valuation cycles."
            ),
            page=info,
            points=list(reversed(page)),
        )
    return EquityHistoryView(
        available=False,
        message="No equity values have been recorded for this sleeve yet.",
    )


def _runs_view(
    settings: Settings,
    config: sleeves.SleeveConfig,
    *,
    limit: int,
    offset: int,
) -> RunsView:
    """Cohort runs that expected this sleeve, with this sleeve's own member outcome."""
    if not config.cohort_id:
        return RunsView(
            available=False,
            message=(
                "This sleeve is not a member of an official cohort, so no scheduled "
                "cohort runs reference it."
            ),
        )
    try:
        runs = storage_factory.run_store(settings).list(
            cohort_id=config.cohort_id, limit=SOURCE_SCAN_LIMIT
        )
    except _STORAGE_ERRORS as exc:
        logger.warning("Run history unreadable for sleeve detail: %s", type(exc).__name__)
        return RunsView(
            available=False,
            message=(
                "The cohort run history could not be read, so runs for this sleeve are "
                "unavailable. The rest of the record is unaffected."
            ),
        )

    # Only runs that actually expected this sleeve. A cohort-scoped read plus this
    # filter is what keeps another cohort's sessions out of one sleeve's record.
    mine = [run for run in runs if config.identity in run.expected_members]
    mine.sort(key=lambda run: (run.scheduled_for, run.started_at), reverse=True)
    rows: list[RunRowView] = []
    for run in mine:
        member = next(
            (entry for entry in run.members if entry.sleeve_id == config.identity), None
        )
        error = member.error if member is not None else None
        rows.append(
            RunRowView(
                run_id=run.run_id,
                run_key=run.run_key,
                session_id=run.session_id,
                scheduled_for=run.scheduled_for,
                status=run.status.value,
                started_at=run.started_at,
                completed_at=run.completed_at,
                member_status=member.status.value if member is not None else None,
                member_started_at=member.started_at if member is not None else None,
                member_completed_at=member.completed_at if member is not None else None,
                member_error_code=error.code if error is not None else None,
                member_error_message=error.message if error is not None else None,
                snapshot_id=run.snapshot_id,
                quote_snapshot_id=run.quote_snapshot_id,
                data_snapshot_ids=dict(run.data_snapshot_ids),
            )
        )
    page, info = _window(rows, limit=limit, offset=offset, scanned=len(runs))
    return RunsView(
        available=True,
        message=None if page else "No cohort run has referenced this sleeve yet.",
        page=info,
        rows=page,
    )


def _lineage_view(
    settings: Settings,
    config: sleeves.SleeveConfig,
    observations: list[evaluation.OfficialDailyObservation],
) -> SleeveLineageView:
    hashes: list[str] = []
    run_ids: list[str] = []
    for observation in observations:
        if observation.strategy_hash and observation.strategy_hash not in hashes:
            hashes.append(observation.strategy_hash)
        if observation.run_id and observation.run_id not in run_ids:
            run_ids.append(observation.run_id)

    start_session: date | None = None
    if config.cohort_id:
        try:
            start_session = storage_factory.sleeve_store(settings).cohort_start_session(
                config.cohort_id
            )
        except _STORAGE_ERRORS as exc:
            logger.debug("Cohort start session unreadable: %s", type(exc).__name__)

    return SleeveLineageView(
        namespace_id=config.namespace_id,
        cohort_id=config.cohort_id or None,
        configuration_hash=config.configuration_hash or None,
        strategy_hashes=hashes,
        run_ids=run_ids,
        snapshot_ids=dict(observations[0].snapshot_ids) if observations else {},
        cohort_start_session=start_session,
        notes=[
            "Per-decision rationales are recorded but are not exposed by the evaluation "
            "store's read contract, so this view reports proposal/fill/rejection counts "
            "per cycle and the simulated orders themselves instead.",
        ],
    )


# --- Assembly ---------------------------------------------------------------


def collect_sleeve_detail(
    settings: Settings,
    sleeve_id: str,
    *,
    limit: int | None = None,
    offset: int | None = None,
    now: datetime | None = None,
) -> SleeveDetail:
    """Assemble the read-only detail record for one sleeve.

    Raises :class:`SleeveDetailError` only for a request that cannot identify a sleeve
    at all. Everything past resolution fails soft into a section-level explanation, so
    an incomplete record still renders the parts that exist.
    """
    resolved_limit, resolved_offset = normalize_page(limit, offset)
    stamp = now or datetime.now(UTC)
    config = resolve_sleeve(settings, normalize_sleeve_id(sleeve_id))
    warnings: list[str] = []

    observations: list[evaluation.OfficialDailyObservation] = []
    cycles: list[evaluation.CycleRecord] = []
    summary: evaluation.EvalSummary | None = None
    observations_message: str | None = None
    cycles_message: str | None = None
    observations_scanned = 0
    cycles_scanned = 0

    try:
        store = storage_factory.evaluation_store(settings, config)
        # ``official_observations`` reads oldest-first, so the newest-first ordering
        # every section pages from is applied here rather than assumed.
        recorded = store.official_observations(limit=SOURCE_SCAN_LIMIT)
        observations_scanned = len(recorded)
        observations = sorted(recorded, key=lambda obs: obs.session_date, reverse=True)
        cycles = store.recent_cycles(limit=SOURCE_SCAN_LIMIT)
        cycles_scanned = len(cycles)
        summary = store.summary()
    except _STORAGE_ERRORS as exc:
        logger.warning("Evaluation history unreadable for sleeve detail: %s", type(exc).__name__)
        soft = (
            "The evaluation history for this sleeve could not be read, so its "
            "observations, cycles, and equity history are unavailable."
        )
        observations_message = soft
        cycles_message = soft
        warnings.append(soft)

    positions, cash = _positions_and_cash(settings, config, now=stamp)
    if not positions.available and positions.message:
        warnings.append(positions.message)

    orders_view = SimulatedOrdersView(
        available=False,
        message=positions.message
        or "No simulated orders are recorded for this sleeve yet.",
    )
    if positions.available:
        try:
            engine = storage_factory.paper_engine(settings, config)
            recorded_orders = list(engine.recent_orders(limit=SOURCE_SCAN_LIMIT))
            rows = [_order_row(order) for order in recorded_orders]
            page, info = _window(
                rows, limit=resolved_limit, offset=resolved_offset, scanned=len(recorded_orders)
            )
            orders_view = SimulatedOrdersView(
                available=True,
                message=(
                    None
                    if page
                    else "No simulated orders are recorded for this sleeve yet."
                ),
                page=info,
                rows=page,
            )
        except _STORAGE_ERRORS as exc:
            logger.warning("Paper orders unreadable for sleeve detail: %s", type(exc).__name__)
            orders_view = SimulatedOrdersView(
                available=False,
                message=(
                    "The simulated order history for this sleeve could not be read. "
                    "The rest of the record is unaffected."
                ),
            )

    observation_rows, observation_page = _window(
        [observation_row(observation) for observation in observations],
        limit=resolved_limit,
        offset=resolved_offset,
        scanned=observations_scanned,
    )
    cycle_rows, cycle_page = _window(
        [_cycle_row(record) for record in cycles],
        limit=resolved_limit,
        offset=resolved_offset,
        scanned=cycles_scanned,
    )

    lifecycle = _lifecycle_view(config.cohort_id)
    if lifecycle.historical:
        warnings.append(
            f"{lifecycle.label or 'This cohort has been superseded'}. The record is "
            "shown for review only and offers no actions."
        )

    return SleeveDetail(
        generated_at=stamp,
        identity=SleeveIdentityView(
            sleeve_id=config.identity,
            name=config.name,
            original_name=config.original_name,
            strategy=config.strategy,
            scope=scope_of(config, has_history=bool(summary and summary.cycles > 0)),
            cohort_id=config.cohort_id or None,
            namespace_id=config.namespace_id,
            created_at=config.created_at,
            lifecycle=lifecycle,
        ),
        strategy=_strategy_view(config),
        capital=SleeveCapitalView(
            starting_capital=config.starting_cash,
            settlement_model="t+1" if config.settlement_t1 else "instant",
            leverage=config.leverage,
            max_positions=config.max_positions,
            max_position_fraction=config.max_position_fraction,
        ),
        positions=positions,
        cash=cash,
        recorded_valuation=_recorded_valuation(observations, cycles),
        performance=_performance_view(summary),
        equity_history=_equity_history(
            observations,
            cycles,
            limit=resolved_limit,
            offset=resolved_offset,
            scanned=max(observations_scanned, cycles_scanned),
        ),
        cycles=CyclesView(
            available=cycles_message is None,
            message=cycles_message
            or (None if cycle_rows else "No valuation cycles are recorded for this sleeve yet."),
            page=None if cycles_message else cycle_page,
            rows=cycle_rows,
        ),
        simulated_orders=orders_view,
        observations=ObservationsView(
            available=observations_message is None,
            message=observations_message
            or (
                None
                if observation_rows
                else "No official observations are recorded for this sleeve yet."
            ),
            page=None if observations_message else observation_page,
            rows=observation_rows,
        ),
        runs=_runs_view(settings, config, limit=resolved_limit, offset=resolved_offset),
        lineage=_lineage_view(settings, config, observations),
        warnings=warnings,
    )


__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "DETAIL_CONTRACT_VERSION",
    "MAX_PAGE_LIMIT",
    "SOURCE_SCAN_LIMIT",
    "InvalidSleeveId",
    "RegistryUnavailable",
    "SleeveDetail",
    "SleeveDetailError",
    "SleeveScope",
    "UnknownSleeve",
    "collect_sleeve_detail",
    "normalize_page",
    "normalize_sleeve_id",
    "observation_row",
    "resolve_sleeve",
    "scope_of",
]
