"""Read-only broker reconciliation and local order-lifecycle tracking.

The broker is always the source of truth.  This module reads recent Schwab
orders and positions, records sanitized lifecycle observations locally, and
reports differences from the app's intent and tax-lot ledgers.  It never places,
replaces, or cancels an order.

Fill bookkeeping is deliberately conservative:

* a newly discovered order is *baselined* at its current filled quantity, so a
  fill that may already exist in the legacy tax-lot database is never duplicated;
* orders created by this version are tracked at zero fills immediately after
  submission, allowing later fill deltas to be ingested;
* each cumulative fill quantity is reserved before tax-lot mutation.  An
  interrupted reservation remains ``pending`` and is surfaced for manual review
  rather than retried at the risk of double-counting.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, Field

from schwab_trader import accounts, events, notify, orders, safety, state, taxlots
from schwab_trader import auth as oauth
from schwab_trader import client as api
from schwab_trader.client import SchwabClient
from schwab_trader.config import Settings
from schwab_trader.models import OrderDetail, OrderRequest, OrderSide, OrderStatus

_ZERO = Decimal(0)
_WORKING_STATUSES = {
    OrderStatus.NEW,
    OrderStatus.AWAITING_MANUAL_REVIEW,
    OrderStatus.ACCEPTED,
    OrderStatus.PENDING_ACTIVATION,
    OrderStatus.QUEUED,
    OrderStatus.WORKING,
    OrderStatus.PENDING_CANCEL,
    OrderStatus.PENDING_REPLACE,
}


def _decimal(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None else None


def _filled(detail: OrderDetail) -> Decimal:
    return detail.filled_quantity or _ZERO


def _fill_notional(detail: OrderDetail) -> Decimal | None:
    filled = _filled(detail)
    if filled <= 0:
        return _ZERO
    if detail.average_fill_price is None:
        return None
    return filled * detail.average_fill_price


def _event_time(detail: OrderDetail, fallback: datetime) -> datetime:
    raw = detail.close_time or detail.entered_time
    if not raw:
        return fallback
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class LifecycleRecord(BaseModel):
    order_id: str
    account_tail: str
    status: str
    side: str | None = None
    symbol: str | None = None
    quantity: Decimal | None = None
    filled_quantity: Decimal = _ZERO
    fill_notional: Decimal | None = None
    remaining_quantity: Decimal | None = None
    limit_price: Decimal | None = None
    entered_time: str | None = None
    close_time: str | None = None
    first_seen_at: datetime
    last_seen_at: datetime
    baseline_only: bool = False


class Observation(BaseModel):
    record: LifecycleRecord
    previous_status: str | None = None
    status_changed: bool = False
    fill_delta: Decimal = _ZERO
    incremental_fill_price: Decimal | None = None
    baselined_fill: Decimal = _ZERO
    fill_regression: Decimal = _ZERO


class Discrepancy(BaseModel):
    severity: str
    kind: str
    subject: str
    detail: str


class ReconciliationReport(BaseModel):
    started_at: datetime
    completed_at: datetime
    success: bool = True
    orders_seen: int = 0
    transitions: int = 0
    fills_applied: int = 0
    discrepancies: list[Discrepancy] = Field(default_factory=list)

    @property
    def discrepancy_count(self) -> int:
        return len(self.discrepancies)


class ReconciliationSummary(BaseModel):
    completed_at: datetime | None = None
    success: bool | None = None
    orders_seen: int = 0
    transitions: int = 0
    fills_applied: int = 0
    discrepancies: int = 0
    pending_fill_applications: int = 0


class ReconciliationStore:
    """SQLite persistence for lifecycle observations and reconciliation runs."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS reconciled_orders (
                    order_id TEXT PRIMARY KEY,
                    account_tail TEXT NOT NULL,
                    status TEXT NOT NULL,
                    side TEXT,
                    symbol TEXT,
                    quantity TEXT,
                    filled_quantity TEXT NOT NULL,
                    fill_notional TEXT,
                    remaining_quantity TEXT,
                    limit_price TEXT,
                    entered_time TEXT,
                    close_time TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    baseline_only INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS order_lifecycle_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    previous_status TEXT,
                    status TEXT NOT NULL,
                    previous_filled_quantity TEXT NOT NULL,
                    filled_quantity TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_lifecycle_order
                    ON order_lifecycle_events (order_id, observed_at);

                CREATE TABLE IF NOT EXISTS fill_applications (
                    source_key TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    cumulative_quantity TEXT NOT NULL,
                    delta_quantity TEXT NOT NULL,
                    price TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reserved_at TEXT NOT NULL,
                    applied_at TEXT
                );

                CREATE TABLE IF NOT EXISTS reconciliation_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    orders_seen INTEGER NOT NULL,
                    transitions INTEGER NOT NULL,
                    fills_applied INTEGER NOT NULL,
                    discrepancies INTEGER NOT NULL
                );
                """
            )

    def track_submission(
        self,
        *,
        order_id: str,
        account_tail: str,
        request: OrderRequest,
        now: datetime | None = None,
    ) -> None:
        """Track a newly submitted order at zero fills without overwriting observations."""
        stamp = (now or datetime.now(UTC)).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO reconciled_orders (
                    order_id, account_tail, status, side, symbol, quantity,
                    filled_quantity, fill_notional, remaining_quantity, limit_price,
                    entered_time, close_time, first_seen_at, last_seen_at, baseline_only
                ) VALUES (?, ?, ?, ?, ?, ?, '0', '0', ?, ?, NULL, NULL, ?, ?, 0)
                """,
                (
                    order_id,
                    account_tail,
                    OrderStatus.ACCEPTED.value,
                    request.side.value,
                    request.symbol,
                    str(request.quantity),
                    str(request.quantity),
                    str(request.limit_price),
                    stamp,
                    stamp,
                ),
            )

    def get_order(self, order_id: str) -> LifecycleRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM reconciled_orders WHERE order_id = ?", (order_id,)
            ).fetchone()
        return self._row_to_lifecycle(row) if row is not None else None

    def observe(
        self,
        detail: OrderDetail,
        *,
        account_tail: str,
        now: datetime | None = None,
    ) -> Observation:
        """Persist one broker observation and return its lifecycle/fill delta."""
        observed_at = now or datetime.now(UTC)
        prior = self.get_order(detail.order_id)
        current_filled = _filled(detail)
        current_notional = _fill_notional(detail)

        if prior is None:
            record = LifecycleRecord(
                order_id=detail.order_id,
                account_tail=account_tail,
                status=detail.status.value,
                side=detail.side,
                symbol=detail.symbol,
                quantity=detail.quantity,
                filled_quantity=current_filled,
                fill_notional=current_notional,
                remaining_quantity=detail.remaining_quantity,
                limit_price=detail.limit_price,
                entered_time=detail.entered_time,
                close_time=detail.close_time,
                first_seen_at=observed_at,
                last_seen_at=observed_at,
                baseline_only=current_filled > 0,
            )
            self._insert_observation(record, previous=None)
            return Observation(
                record=record,
                previous_status=None,
                status_changed=True,
                baselined_fill=current_filled,
            )

        raw_fill_delta = current_filled - prior.filled_quantity
        fill_delta = max(_ZERO, raw_fill_delta)
        fill_regression = max(_ZERO, -raw_fill_delta)
        incremental_price: Decimal | None = None
        if fill_delta > 0 and current_notional is not None and prior.fill_notional is not None:
            delta_notional = current_notional - prior.fill_notional
            if delta_notional > 0:
                incremental_price = delta_notional / fill_delta

        # Never move the locally processed watermark backward. Also retain the
        # previous watermark when a new fill has no usable cumulative notional;
        # a later observation with execution pricing can then ingest it safely.
        effective_filled = current_filled
        effective_notional = current_notional
        if fill_regression > 0 or (fill_delta > 0 and current_notional is None):
            effective_filled = prior.filled_quantity
            effective_notional = prior.fill_notional

        record = LifecycleRecord(
            order_id=detail.order_id,
            account_tail=prior.account_tail,
            status=detail.status.value,
            side=detail.side or prior.side,
            symbol=detail.symbol or prior.symbol,
            quantity=detail.quantity if detail.quantity is not None else prior.quantity,
            filled_quantity=effective_filled,
            fill_notional=effective_notional,
            remaining_quantity=(
                detail.remaining_quantity
                if detail.remaining_quantity is not None
                else prior.remaining_quantity
            ),
            limit_price=detail.limit_price if detail.limit_price is not None else prior.limit_price,
            entered_time=detail.entered_time or prior.entered_time,
            close_time=detail.close_time or prior.close_time,
            first_seen_at=prior.first_seen_at,
            last_seen_at=observed_at,
            baseline_only=prior.baseline_only,
        )
        self._update_observation(record, previous=prior)
        return Observation(
            record=record,
            previous_status=prior.status,
            status_changed=prior.status != record.status,
            fill_delta=fill_delta,
            incremental_fill_price=incremental_price,
            fill_regression=fill_regression,
        )

    def _insert_observation(
        self, record: LifecycleRecord, *, previous: LifecycleRecord | None
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO reconciled_orders (
                    order_id, account_tail, status, side, symbol, quantity,
                    filled_quantity, fill_notional, remaining_quantity, limit_price,
                    entered_time, close_time, first_seen_at, last_seen_at, baseline_only
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._record_values(record),
            )
            self._insert_event(conn, record, previous)

    def _update_observation(self, record: LifecycleRecord, *, previous: LifecycleRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE reconciled_orders SET
                    account_tail = ?, status = ?, side = ?, symbol = ?, quantity = ?,
                    filled_quantity = ?, fill_notional = ?, remaining_quantity = ?,
                    limit_price = ?, entered_time = ?, close_time = ?, first_seen_at = ?,
                    last_seen_at = ?, baseline_only = ?
                WHERE order_id = ?
                """,
                (*self._record_values(record)[1:], record.order_id),
            )
            changed = (
                previous.status != record.status
                or previous.filled_quantity != record.filled_quantity
            )
            if changed:
                self._insert_event(conn, record, previous)

    @staticmethod
    def _record_values(record: LifecycleRecord) -> tuple[object, ...]:
        return (
            record.order_id,
            record.account_tail,
            record.status,
            record.side,
            record.symbol,
            str(record.quantity) if record.quantity is not None else None,
            str(record.filled_quantity),
            str(record.fill_notional) if record.fill_notional is not None else None,
            str(record.remaining_quantity) if record.remaining_quantity is not None else None,
            str(record.limit_price) if record.limit_price is not None else None,
            record.entered_time,
            record.close_time,
            record.first_seen_at.isoformat(),
            record.last_seen_at.isoformat(),
            int(record.baseline_only),
        )

    @staticmethod
    def _insert_event(
        conn: sqlite3.Connection,
        record: LifecycleRecord,
        previous: LifecycleRecord | None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO order_lifecycle_events (
                order_id, observed_at, previous_status, status,
                previous_filled_quantity, filled_quantity
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                record.order_id,
                record.last_seen_at.isoformat(),
                previous.status if previous else None,
                record.status,
                str(previous.filled_quantity if previous else _ZERO),
                str(record.filled_quantity),
            ),
        )

    def reserve_fill(
        self,
        *,
        order_id: str,
        cumulative_quantity: Decimal,
        delta_quantity: Decimal,
        price: Decimal,
        now: datetime,
    ) -> str:
        """Reserve a fill application and return ``new``, ``pending``, or ``applied``."""
        source_key = f"schwab-order:{order_id}:cumulative:{cumulative_quantity}"
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM fill_applications WHERE source_key = ?", (source_key,)
            ).fetchone()
            if row is not None:
                return str(row["status"])
            conn.execute(
                """
                INSERT INTO fill_applications (
                    source_key, order_id, cumulative_quantity, delta_quantity,
                    price, status, reserved_at, applied_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, NULL)
                """,
                (
                    source_key,
                    order_id,
                    str(cumulative_quantity),
                    str(delta_quantity),
                    str(price),
                    now.isoformat(),
                ),
            )
        return "new"

    def mark_fill_applied(
        self, order_id: str, cumulative_quantity: Decimal, *, now: datetime
    ) -> None:
        source_key = f"schwab-order:{order_id}:cumulative:{cumulative_quantity}"
        with self._connect() as conn:
            conn.execute(
                "UPDATE fill_applications SET status = 'applied', applied_at = ? "
                "WHERE source_key = ? AND status = 'pending'",
                (now.isoformat(), source_key),
            )

    def pending_fill_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM fill_applications WHERE status = 'pending'"
            ).fetchone()
        return int(row["count"] if row else 0)

    def record_run(self, report: ReconciliationReport) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO reconciliation_runs (
                    started_at, completed_at, success, orders_seen, transitions,
                    fills_applied, discrepancies
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.started_at.isoformat(),
                    report.completed_at.isoformat(),
                    int(report.success),
                    report.orders_seen,
                    report.transitions,
                    report.fills_applied,
                    report.discrepancy_count,
                ),
            )

    def latest_summary(self) -> ReconciliationSummary:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM reconciliation_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return ReconciliationSummary(pending_fill_applications=self.pending_fill_count())
        return ReconciliationSummary(
            completed_at=datetime.fromisoformat(row["completed_at"]),
            success=bool(row["success"]),
            orders_seen=row["orders_seen"],
            transitions=row["transitions"],
            fills_applied=row["fills_applied"],
            discrepancies=row["discrepancies"],
            pending_fill_applications=self.pending_fill_count(),
        )

    @staticmethod
    def _row_to_lifecycle(row: sqlite3.Row) -> LifecycleRecord:
        return LifecycleRecord(
            order_id=row["order_id"],
            account_tail=row["account_tail"],
            status=row["status"],
            side=row["side"],
            symbol=row["symbol"],
            quantity=_decimal(row["quantity"]),
            filled_quantity=Decimal(row["filled_quantity"]),
            fill_notional=_decimal(row["fill_notional"]),
            remaining_quantity=_decimal(row["remaining_quantity"]),
            limit_price=_decimal(row["limit_price"]),
            entered_time=row["entered_time"],
            close_time=row["close_time"],
            first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
            last_seen_at=datetime.fromisoformat(row["last_seen_at"]),
            baseline_only=bool(row["baseline_only"]),
        )


def track_submission(
    settings: Settings,
    *,
    order_id: str,
    request: OrderRequest,
    now: datetime | None = None,
) -> None:
    ReconciliationStore(settings.state_db_path).track_submission(
        order_id=order_id,
        account_tail=settings.masked_account_tail(),
        request=request,
        now=now,
    )


def _apply_observed_fill(
    settings: Settings,
    store: ReconciliationStore,
    observation: Observation,
    detail: OrderDetail,
    *,
    now: datetime,
) -> tuple[bool, Discrepancy | None]:
    if observation.fill_delta <= 0:
        return False, None
    record = observation.record
    if record.symbol is None or record.side not in {OrderSide.BUY.value, OrderSide.SELL.value}:
        return False, Discrepancy(
            severity="error",
            kind="fill_missing_identity",
            subject=f"order {record.order_id}",
            detail="Fill increased, but the supported equity side/symbol was unavailable.",
        )
    price = observation.incremental_fill_price
    if price is None or price <= 0:
        return False, Discrepancy(
            severity="error",
            kind="fill_price_unavailable",
            subject=f"order {record.order_id}",
            detail=(
                f"Filled quantity increased by {observation.fill_delta}, but an incremental "
                "execution price could not be derived; local tax lots were not changed."
            ),
        )
    reservation = store.reserve_fill(
        order_id=record.order_id,
        cumulative_quantity=record.filled_quantity,
        delta_quantity=observation.fill_delta,
        price=price,
        now=now,
    )
    if reservation == "applied":
        return False, None
    if reservation == "pending":
        return False, Discrepancy(
            severity="error",
            kind="fill_application_uncertain",
            subject=f"order {record.order_id}",
            detail=(
                "A prior fill-bookkeeping attempt was interrupted. It was not retried to "
                "avoid double-counting; review the local tax-lot ledger manually."
            ),
        )

    occurred_at = _event_time(detail, now)
    lot_store = taxlots.TaxLotStore(settings.tax_lots_db_path)
    try:
        if record.side == OrderSide.BUY.value:
            lot_store.record_purchase(
                symbol=record.symbol,
                quantity=observation.fill_delta,
                cost_per_share=price,
                acquired_at=occurred_at,
            )
            uncovered = False
        else:
            result = lot_store.apply_sale(
                symbol=record.symbol,
                quantity=observation.fill_delta,
                price=price,
                sold_at=occurred_at,
                method=taxlots.LotMethod.parse(settings.tax_lot_method),
            )
            uncovered = not result.fully_covered
            safety.SafetyLedger(settings.agent_activity_db_path).record(
                realized_pnl_delta=result.gain,
                now=occurred_at,
            )
    except sqlite3.Error as exc:
        return False, Discrepancy(
            severity="error",
            kind="fill_application_failed",
            subject=f"order {record.order_id}",
            detail=f"Local fill bookkeeping failed ({type(exc).__name__}); reservation retained.",
        )

    store.mark_fill_applied(record.order_id, record.filled_quantity, now=now)
    if uncovered:
        return True, Discrepancy(
            severity="warning",
            kind="tax_lots_incomplete",
            subject=record.symbol,
            detail=(
                f"A sell fill of {observation.fill_delta} shares exceeded locally tracked lots; "
                "the broker position remains authoritative."
            ),
        )
    return True, None


def record_immediate_detail(
    settings: Settings,
    request: OrderRequest,
    detail: OrderDetail,
    audit: Callable[[str, str | None], None],
    *,
    now: datetime | None = None,
) -> None:
    """Observe an immediate post-submit status through the idempotent fill path."""
    if not detail.order_id:
        return
    observed_at = now or datetime.now(UTC)
    store = ReconciliationStore(settings.state_db_path)
    store.track_submission(
        order_id=detail.order_id,
        account_tail=settings.masked_account_tail(),
        request=request,
        now=observed_at,
    )
    observation = store.observe(
        detail, account_tail=settings.masked_account_tail(), now=observed_at
    )
    applied, discrepancy = _apply_observed_fill(
        settings, store, observation, detail, now=observed_at
    )
    if discrepancy is not None:
        audit(discrepancy.kind, discrepancy.detail)
    if applied:
        price = observation.incremental_fill_price
        audit(
            "tax_lot_recorded",
            f"{request.side.value} {observation.fill_delta} {request.symbol} @ {price}",
        )


def _matching_candidates(
    intent: state.IntentRecord, details: list[OrderDetail]
) -> list[OrderDetail]:
    return [
        detail
        for detail in details
        if detail.side == intent.side
        and detail.symbol == intent.symbol
        and detail.quantity == Decimal(intent.quantity)
        and detail.limit_price == Decimal(intent.limit_price)
    ]


def _position_discrepancies(
    live_positions: list[accounts.Position], lot_store: taxlots.TaxLotStore
) -> list[Discrepancy]:
    live = {p.symbol: p.long_quantity for p in live_positions if p.symbol}
    local: dict[str, Decimal] = {}
    for lot in lot_store.open_lots():
        local[lot.symbol] = local.get(lot.symbol, _ZERO) + lot.quantity
    rows: list[Discrepancy] = []
    for symbol in sorted(set(live) | set(local)):
        live_qty = live.get(symbol, _ZERO)
        local_qty = local.get(symbol, _ZERO)
        if live_qty != local_qty:
            rows.append(
                Discrepancy(
                    severity="warning",
                    kind="position_quantity_mismatch",
                    subject=symbol,
                    detail=f"Schwab quantity {live_qty}; local tax-lot quantity {local_qty}.",
                )
            )
    for position in live_positions:
        if position.short_quantity > 0:
            rows.append(
                Discrepancy(
                    severity="error",
                    kind="unsupported_short_position",
                    subject=position.symbol,
                    detail=f"Schwab reports a short quantity of {position.short_quantity}.",
                )
            )
    return rows


def reconcile_account(
    client: SchwabClient,
    settings: Settings,
    account_hash: str,
    *,
    hours: int = 168,
    send_notifications: bool = False,
    now: datetime | None = None,
) -> ReconciliationReport:
    """Read broker state once, update local observations, and report discrepancies."""
    started = now or datetime.now(UTC)
    account_tail = settings.masked_account_tail()
    store = ReconciliationStore(settings.state_db_path)
    notifier = notify.build_notifier(settings)
    from_time = started - timedelta(hours=hours)
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    report = ReconciliationReport(started_at=started, completed_at=started)
    try:
        raw_orders = orders.get_recent_orders(
            client,
            account_hash,
            from_time=from_time.strftime(fmt),
            to_time=started.strftime(fmt),
        )
        live_positions = accounts.get_positions(client, account_hash)
    except (oauth.OAuthError, api.ApiError, OSError) as exc:
        report.success = False
        report.completed_at = datetime.now(UTC) if now is None else started
        report.discrepancies.append(
            Discrepancy(
                severity="error",
                kind="broker_read_failed",
                subject="reconciliation",
                detail=f"Broker state could not be read ({type(exc).__name__}).",
            )
        )
        store.record_run(report)
        state.StateStore(settings.state_db_path).append_audit(
            command="reconcile",
            event="failed",
            account_tail=account_tail,
            detail=f"broker_read_failed={type(exc).__name__}",
            now=report.completed_at,
        )
        raise
    details = [
        orders.parse_order_detail(item, fallback_id=str(item.get("orderId", "?")))
        for item in raw_orders
        if isinstance(item, dict)
    ]
    report.orders_seen = len(details)

    if len(raw_orders) >= 50:
        report.discrepancies.append(
            Discrepancy(
                severity="warning",
                kind="order_window_may_be_truncated",
                subject="recent orders",
                detail=(
                    "The API returned the configured 50-order maximum. Missing-order checks "
                    "were suppressed because the window may be incomplete."
                ),
            )
        )

    seen_ids: set[str] = set()
    for detail in details:
        seen_ids.add(detail.order_id)
        observation = store.observe(detail, account_tail=account_tail, now=started)
        if observation.status_changed and observation.previous_status is not None:
            report.transitions += 1
        if observation.baselined_fill > 0:
            report.discrepancies.append(
                Discrepancy(
                    severity="info",
                    kind="legacy_fill_baselined",
                    subject=f"order {detail.order_id}",
                    detail=(
                        f"Existing filled quantity {observation.baselined_fill} was baselined "
                        "without changing tax lots, preventing rollout-time duplication."
                    ),
                )
            )
        if observation.fill_regression > 0:
            report.discrepancies.append(
                Discrepancy(
                    severity="error",
                    kind="filled_quantity_regressed",
                    subject=f"order {detail.order_id}",
                    detail=(
                        f"Schwab's filled quantity decreased by {observation.fill_regression}. "
                        "The local fill watermark was not reduced; review the order manually."
                    ),
                )
            )
        applied, discrepancy = _apply_observed_fill(
            settings, store, observation, detail, now=started
        )
        if applied:
            report.fills_applied += 1
        if discrepancy is not None:
            report.discrepancies.append(discrepancy)

        should_notify = (
            send_notifications
            and observation.previous_status is not None
            and (observation.status_changed or observation.fill_delta > 0)
        )
        if should_notify:
            assert observation.previous_status is not None
            events.emit(
                notifier,
                events.order_lifecycle_message(
                    detail,
                    previous_status=observation.previous_status,
                    fill_delta=observation.fill_delta,
                    account_tail=account_tail,
                ),
            )

    report.discrepancies.extend(
        _position_discrepancies(live_positions, taxlots.TaxLotStore(settings.tax_lots_db_path))
    )

    if len(raw_orders) < 50:
        intents = state.StateStore(settings.state_db_path).recent_intents(limit=1000)
        for intent in intents:
            if intent.created_at < from_time or intent.status not in {
                state.STATUS_PENDING,
                state.STATUS_SUBMITTED,
            }:
                continue
            if intent.order_id and intent.order_id not in seen_ids:
                report.discrepancies.append(
                    Discrepancy(
                        severity="error",
                        kind="local_order_missing_at_broker",
                        subject=f"intent {intent.id}",
                        detail=(
                            f"Local order id {intent.order_id} was not returned in the "
                            f"{hours}-hour broker window. Do not resubmit without review."
                        ),
                    )
                )
            elif intent.order_id is None:
                candidates = _matching_candidates(intent, details)
                candidate_text = (
                    f" One matching broker candidate exists: {candidates[0].order_id}."
                    if len(candidates) == 1
                    else f" Matching broker candidates: {len(candidates)}."
                )
                report.discrepancies.append(
                    Discrepancy(
                        severity="error",
                        kind="ambiguous_local_intent",
                        subject=f"intent {intent.id}",
                        detail=(
                            "A pending local intent has no broker order id; it was not "
                            f"automatically matched or resubmitted.{candidate_text}"
                        ),
                    )
                )

    pending = store.pending_fill_count()
    if pending:
        report.discrepancies.append(
            Discrepancy(
                severity="error",
                kind="pending_fill_applications",
                subject="local bookkeeping",
                detail=(
                    f"{pending} fill application(s) remain uncertain and require manual review."
                ),
            )
        )

    report.completed_at = datetime.now(UTC) if now is None else started
    store.record_run(report)
    state.StateStore(settings.state_db_path).append_audit(
        command="reconcile",
        event="completed",
        account_tail=account_tail,
        detail=(
            f"orders={report.orders_seen} transitions={report.transitions} "
            f"fills={report.fills_applied} discrepancies={report.discrepancy_count}"
        ),
        now=report.completed_at,
    )
    return report
