"""Transactional SQLAlchemy implementation of the paper engine contract."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from schwab_trader.market_data import Quote
from schwab_trader.models import OrderRequest, OrderSide
from schwab_trader.paper import (
    STATUS_FILLED,
    STATUS_REJECTED,
    PaperAccount,
    PaperOrder,
    PaperPosition,
    PaperValuation,
    _is_marketable,
    _next_business_day,
    fill_reference,
)
from schwab_trader.storage.database import Database
from schwab_trader.storage.schema import (
    PaperAccount as PaperAccountRow,
)
from schwab_trader.storage.schema import (
    PaperFill as PaperFillRow,
)
from schwab_trader.storage.schema import (
    PaperOrder as PaperOrderRow,
)
from schwab_trader.storage.schema import (
    PaperPosition as PaperPositionRow,
)
from schwab_trader.storage.schema import (
    PaperUnsettledCash as PaperUnsettledRow,
)

_COST_QUANT = Decimal("0.0001")


class SqlAlchemyPaperEngine:
    """Paper engine using one shared database and a stable ``sleeve_id``."""

    def __init__(
        self,
        database: Database,
        sleeve_id: str,
        *,
        starting_cash: Decimal,
        settle_t1: bool = False,
        leverage: Decimal = Decimal(1),
        margin_rate: Decimal = Decimal("0.12"),
        maintenance_margin: Decimal = Decimal("0.25"),
    ) -> None:
        self.database = database
        self.sleeve_id = sleeve_id
        self._starting_cash = starting_cash
        self._settle_t1 = settle_t1
        self._leverage = leverage if leverage >= 1 else Decimal(1)
        self._margin_rate = margin_rate
        self._maintenance_margin = maintenance_margin
        with self.database.session() as session:
            if session.get(PaperAccountRow, sleeve_id) is None:
                session.add(
                    PaperAccountRow(
                        sleeve_id=sleeve_id,
                        starting_cash=starting_cash,
                        cash=starting_cash,
                        realized_pnl=Decimal(0),
                        created_at=datetime.now(UTC),
                        source_created_at=None,
                        last_accrual=None,
                        source_path=None,
                    )
                )

    @property
    def leverage(self) -> Decimal:
        return self._leverage

    def _account_row(self, session: Session, *, lock: bool = False) -> PaperAccountRow:
        statement = select(PaperAccountRow).where(PaperAccountRow.sleeve_id == self.sleeve_id)
        if lock:
            statement = statement.with_for_update()
        row = session.scalar(statement)
        if row is None:
            raise RuntimeError("paper account is missing for the selected sleeve")
        return row

    def account(self) -> PaperAccount:
        with self.database.session() as session:
            row = self._account_row(session)
            unsettled = self._unsettled_total(session)
            return PaperAccount(
                starting_cash=Decimal(row.starting_cash),
                cash=Decimal(row.cash),
                realized_pnl=Decimal(row.realized_pnl),
                created_at=row.created_at
                or datetime.fromisoformat(row.source_created_at or datetime.now(UTC).isoformat()),
                unsettled_cash=unsettled,
            )

    def _unsettled_total(self, session: Session) -> Decimal:
        amounts = session.scalars(
            select(PaperUnsettledRow.amount).where(
                PaperUnsettledRow.sleeve_id == self.sleeve_id
            )
        )
        return sum((Decimal(value) for value in amounts), Decimal(0))

    def _settle_due(self, session: Session, now: datetime) -> None:
        rows = list(
            session.scalars(
                select(PaperUnsettledRow)
                .where(
                    PaperUnsettledRow.sleeve_id == self.sleeve_id,
                    PaperUnsettledRow.settle_date <= now.date(),
                )
                .with_for_update()
            )
        )
        if not rows:
            return
        due = sum((Decimal(row.amount) for row in rows), Decimal(0))
        account = self._account_row(session, lock=True)
        account.cash = Decimal(account.cash) + due
        for row in rows:
            session.delete(row)

    def _cost_basis(self, session: Session) -> Decimal:
        rows = session.scalars(
            select(PaperPositionRow).where(
                PaperPositionRow.sleeve_id == self.sleeve_id,
                PaperPositionRow.quantity > 0,
            )
        )
        return sum(
            (Decimal(row.avg_cost) * row.quantity for row in rows),
            Decimal(0),
        )

    def buying_power(self) -> Decimal:
        with self.database.session() as session:
            account = self._account_row(session)
            cost_basis = self._cost_basis(session)
            power = self._leverage * Decimal(account.cash) + (
                self._leverage - 1
            ) * cost_basis
        return power if power > 0 else Decimal(0)

    def credit_cash(self, amount: Decimal) -> None:
        if amount <= 0:
            return
        with self.database.session() as session:
            account = self._account_row(session, lock=True)
            account.cash = Decimal(account.cash) + amount

    def accrue(self, now: datetime) -> None:
        with self.database.session() as session:
            if self._settle_t1:
                self._settle_due(session, now)
            self._accrue_interest(session, now)

    def _accrue_interest(self, session: Session, now: datetime) -> None:
        if self._leverage <= 1 or self._margin_rate <= 0:
            return
        account = self._account_row(session, lock=True)
        today = now.date()
        if account.last_accrual is None:
            account.last_accrual = today
            return
        days = (today - account.last_accrual).days
        if days <= 0:
            return
        cash = Decimal(account.cash)
        if cash < 0:
            interest = (-cash * self._margin_rate * Decimal(days) / Decimal(365)).quantize(
                Decimal("0.01")
            )
            account.cash = cash - interest
        account.last_accrual = today

    def enforce_maintenance(
        self,
        marks: dict[str, Decimal | None],
        now: datetime | None = None,
    ) -> list[PaperOrder]:
        if self._leverage <= 1:
            return []
        stamp = now or datetime.now(UTC)
        liquidated: list[PaperOrder] = []
        with self.database.session() as session:
            while True:
                rows = list(
                    session.scalars(
                        select(PaperPositionRow)
                        .where(
                            PaperPositionRow.sleeve_id == self.sleeve_id,
                            PaperPositionRow.quantity > 0,
                        )
                        .with_for_update()
                    )
                )
                if not rows:
                    break
                account = self._account_row(session, lock=True)
                marked: list[tuple[Decimal, Decimal, PaperPositionRow]] = []
                position_value = Decimal(0)
                for row in rows:
                    mark = marks.get(row.symbol)
                    price = mark if mark is not None and mark > 0 else Decimal(row.avg_cost)
                    value = price * row.quantity
                    position_value += value
                    marked.append((value, price, row))
                equity = Decimal(account.cash) + position_value
                if (
                    position_value <= 0
                    or equity >= self._maintenance_margin * position_value
                ):
                    break
                _, price, row = max(marked, key=lambda item: item[0])
                position = self._domain_position(row)
                self._apply_sell(session, position, position.quantity, price, None)
                request = OrderRequest(
                    side=OrderSide.SELL,
                    symbol=position.symbol,
                    quantity=position.quantity,
                    limit_price=price,
                )
                liquidated.append(
                    self._record(
                        session,
                        request,
                        stamp,
                        STATUS_FILLED,
                        "margin-call liquidation",
                        price,
                        stamp,
                    )
                )
        return liquidated

    def positions(self) -> list[PaperPosition]:
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(PaperPositionRow)
                    .where(
                        PaperPositionRow.sleeve_id == self.sleeve_id,
                        PaperPositionRow.quantity > 0,
                    )
                    .order_by(PaperPositionRow.symbol)
                )
            )
        return [self._domain_position(row) for row in rows]

    @staticmethod
    def _domain_position(row: PaperPositionRow) -> PaperPosition:
        return PaperPosition(
            symbol=row.symbol,
            quantity=row.quantity,
            avg_cost=Decimal(row.avg_cost),
        )

    def _position(
        self,
        session: Session,
        symbol: str,
        *,
        lock: bool = False,
    ) -> PaperPosition | None:
        statement = select(PaperPositionRow).where(
            PaperPositionRow.sleeve_id == self.sleeve_id,
            PaperPositionRow.symbol == symbol,
        )
        if lock:
            statement = statement.with_for_update()
        row = session.scalar(statement)
        return None if row is None else self._domain_position(row)

    def recent_orders(self, limit: int = 20) -> list[PaperOrder]:
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(PaperOrderRow)
                    .where(PaperOrderRow.sleeve_id == self.sleeve_id)
                    .order_by(PaperOrderRow.paper_order_id.desc())
                    .limit(limit)
                )
            )
        return [self._domain_order(row) for row in rows]

    @staticmethod
    def _domain_order(row: PaperOrderRow) -> PaperOrder:
        created = row.created_at
        if created is None and row.source_created_at:
            created = datetime.fromisoformat(row.source_created_at)
        filled = row.filled_at
        if filled is None and row.source_filled_at:
            filled = datetime.fromisoformat(row.source_filled_at)
        return PaperOrder(
            id=row.paper_order_id,
            side=row.side,
            symbol=row.symbol,
            quantity=row.quantity,
            limit_price=Decimal(row.limit_price),
            status=row.status,
            reason=row.reason,
            fill_price=Decimal(row.fill_price) if row.fill_price is not None else None,
            created_at=created or datetime.now(UTC),
            filled_at=filled,
        )

    def reset(self, starting_cash: Decimal | None = None) -> None:
        cash = starting_cash if starting_cash is not None else self._starting_cash
        with self.database.session() as session:
            order_ids = select(PaperOrderRow.paper_order_id).where(
                PaperOrderRow.sleeve_id == self.sleeve_id
            )
            session.execute(delete(PaperFillRow).where(PaperFillRow.paper_order_id.in_(order_ids)))
            session.execute(
                delete(PaperPositionRow).where(PaperPositionRow.sleeve_id == self.sleeve_id)
            )
            session.execute(
                delete(PaperOrderRow).where(PaperOrderRow.sleeve_id == self.sleeve_id)
            )
            session.execute(
                delete(PaperUnsettledRow).where(
                    PaperUnsettledRow.sleeve_id == self.sleeve_id
                )
            )
            account = self._account_row(session, lock=True)
            account.starting_cash = cash
            account.cash = cash
            account.realized_pnl = Decimal(0)
            account.created_at = datetime.now(UTC)
            account.source_created_at = None
            account.last_accrual = None

    def place_order(
        self,
        request: OrderRequest,
        quote: Quote,
        *,
        now: datetime | None = None,
    ) -> PaperOrder:
        stamp = now or datetime.now(UTC)
        reference = fill_reference(request.side, quote)
        if reference is None:
            with self.database.session() as session:
                return self._record(
                    session,
                    request,
                    stamp,
                    STATUS_REJECTED,
                    "no usable quote price",
                    None,
                    None,
                )
        if not _is_marketable(request.side, request.limit_price, reference):
            need = "ask" if request.side is OrderSide.BUY else "bid"
            reason = f"not marketable at {reference} ({need}); limit {request.limit_price}"
            with self.database.session() as session:
                return self._record(
                    session,
                    request,
                    stamp,
                    STATUS_REJECTED,
                    reason,
                    None,
                    None,
                )

        with self.database.session() as session:
            account = self._account_row(session, lock=True)
            if self._settle_t1:
                self._settle_due(session, stamp)
            self._accrue_interest(session, stamp)
            account_cash = Decimal(account.cash)
            if request.side is OrderSide.BUY:
                cost = reference * request.quantity
                cost_basis = self._cost_basis(session)
                power = self._leverage * account_cash + (
                    self._leverage - 1
                ) * cost_basis
                if cost > power:
                    if self._leverage > 1:
                        available = power if power > 0 else Decimal(0)
                        reason = (
                            f"exceeds margin buying power "
                            f"(need {cost}, buying power {available})"
                        )
                    else:
                        shortfall = "settled paper cash" if self._settle_t1 else "paper cash"
                        reason = (
                            f"insufficient {shortfall} (need {cost}, have {account_cash})"
                        )
                    return self._record(
                        session,
                        request,
                        stamp,
                        STATUS_REJECTED,
                        reason,
                        None,
                        None,
                    )
                self._apply_buy(
                    session,
                    request.symbol,
                    request.quantity,
                    reference,
                    cost,
                )
            else:
                position = self._position(session, request.symbol, lock=True)
                held = position.quantity if position is not None else 0
                if held < request.quantity:
                    return self._record(
                        session,
                        request,
                        stamp,
                        STATUS_REJECTED,
                        f"insufficient paper shares (need {request.quantity}, have {held})",
                        None,
                        None,
                    )
                settle_date = _next_business_day(stamp.date()) if self._settle_t1 else None
                self._apply_sell(
                    session,
                    position,
                    request.quantity,
                    reference,
                    settle_date,
                )
            return self._record(
                session,
                request,
                stamp,
                STATUS_FILLED,
                None,
                reference,
                stamp,
            )

    def _apply_buy(
        self,
        session: Session,
        symbol: str,
        quantity: int,
        price: Decimal,
        cost: Decimal,
    ) -> None:
        account = self._account_row(session, lock=True)
        account.cash = Decimal(account.cash) - cost
        row = session.scalar(
            select(PaperPositionRow)
            .where(
                PaperPositionRow.sleeve_id == self.sleeve_id,
                PaperPositionRow.symbol == symbol,
            )
            .with_for_update()
        )
        if row is None:
            session.add(
                PaperPositionRow(
                    sleeve_id=self.sleeve_id,
                    symbol=symbol,
                    quantity=quantity,
                    avg_cost=price,
                    source_path=None,
                )
            )
            return
        total_quantity = row.quantity + quantity
        row.avg_cost = (
            (Decimal(row.avg_cost) * row.quantity + price * quantity) / total_quantity
        ).quantize(_COST_QUANT)
        row.quantity = total_quantity

    def _apply_sell(
        self,
        session: Session,
        position: PaperPosition | None,
        quantity: int,
        price: Decimal,
        settle_date: date | None = None,
    ) -> None:
        assert position is not None
        proceeds = price * quantity
        realized = (price - position.avg_cost) * quantity
        account = self._account_row(session, lock=True)
        if settle_date is None:
            account.cash = Decimal(account.cash) + proceeds
        else:
            session.add(
                PaperUnsettledRow(
                    sleeve_id=self.sleeve_id,
                    source_path=None,
                    source_unsettled_id=None,
                    amount=proceeds,
                    settle_date=settle_date,
                )
            )
        account.realized_pnl = Decimal(account.realized_pnl) + realized
        row = session.get(PaperPositionRow, (self.sleeve_id, position.symbol))
        assert row is not None
        remaining = row.quantity - quantity
        if remaining > 0:
            row.quantity = remaining
        else:
            session.delete(row)

    def _record(
        self,
        session: Session,
        request: OrderRequest,
        now: datetime,
        status: str,
        reason: str | None,
        fill_price: Decimal | None,
        filled_at: datetime | None,
    ) -> PaperOrder:
        row = PaperOrderRow(
            sleeve_id=self.sleeve_id,
            source_path=None,
            source_order_id=None,
            side=request.side.value,
            symbol=request.symbol,
            quantity=request.quantity,
            limit_price=request.limit_price,
            status=status,
            reason=reason,
            fill_price=fill_price,
            created_at=now,
            source_created_at=None,
            filled_at=filled_at,
            source_filled_at=None,
        )
        session.add(row)
        session.flush()
        if status == STATUS_FILLED and fill_price is not None:
            session.add(
                PaperFillRow(
                    paper_order_id=row.paper_order_id,
                    fill_sequence=1,
                    quantity=request.quantity,
                    price=fill_price,
                    filled_at=filled_at,
                    source_filled_at=None,
                )
            )
        return self._domain_order(row)

    def value(self, marks: dict[str, Decimal | None]) -> PaperValuation:
        account = self.account()
        positions_value = Decimal(0)
        cost_basis = Decimal(0)
        for position in self.positions():
            mark = marks.get(position.symbol)
            price = mark if mark is not None and mark > 0 else position.avg_cost
            positions_value += price * position.quantity
            cost_basis += position.avg_cost * position.quantity
        return PaperValuation(
            starting_cash=account.starting_cash,
            cash=account.cash,
            positions_value=positions_value,
            total_value=account.cash + account.unsettled_cash + positions_value,
            unrealized_pnl=positions_value - cost_basis,
            realized_pnl=account.realized_pnl,
            unsettled_cash=account.unsettled_cash,
        )
