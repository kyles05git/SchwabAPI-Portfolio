"""Paper-trading engine: a simulated cash sleeve traded on live quotes.

This is the risk-free substrate for evaluating a strategy or agent before any
real money is used. It shares the typed :class:`~schwab_trader.models.OrderRequest`
with live trading, so the same orders can be routed to either.

v1 fill model (intentionally simple):

- A limit order fills immediately if it is *marketable* against the current quote
  (BUY fills at the ask, SELL fills at the bid - i.e. you pay the spread), and is
  otherwise rejected with a reason. There are no resting orders yet.
- Paper cash and share holdings are enforced exactly (no buying with money you do
  not have; no selling shares you do not hold).

State is persisted in its own SQLite file so the sleeve survives restarts. This
module performs no network calls; callers pass in live quotes.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel

from schwab_trader.market_data import Quote
from schwab_trader.models import OrderRequest, OrderSide

STATUS_FILLED = "FILLED"
STATUS_REJECTED = "REJECTED"

_COST_QUANT = Decimal("0.0001")


class PaperAccount(BaseModel):
    starting_cash: Decimal
    cash: Decimal  # settled cash (usable for buys)
    realized_pnl: Decimal
    created_at: datetime
    unsettled_cash: Decimal = Decimal(0)  # sale proceeds not yet settled (T+1)

    @property
    def total_cash(self) -> Decimal:
        return self.cash + self.unsettled_cash


class PaperPosition(BaseModel):
    symbol: str
    quantity: int
    avg_cost: Decimal


class PaperOrder(BaseModel):
    id: int
    side: str
    symbol: str
    quantity: int
    limit_price: Decimal
    status: str
    reason: str | None
    fill_price: Decimal | None
    created_at: datetime
    filled_at: datetime | None


class PaperValuation(BaseModel):
    starting_cash: Decimal
    cash: Decimal  # settled cash
    positions_value: Decimal
    total_value: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    unsettled_cash: Decimal = Decimal(0)

    @property
    def total_return_pct(self) -> Decimal:
        if self.starting_cash == 0:
            return Decimal(0)
        return ((self.total_value - self.starting_cash) / self.starting_cash) * 100


def fill_reference(side: OrderSide, quote: Quote) -> Decimal | None:
    """The price a marketable order would fill at (ask for buys, bid for sells)."""
    candidates = (
        (quote.ask, quote.mark, quote.last)
        if side is OrderSide.BUY
        else (quote.bid, quote.mark, quote.last)
    )
    for candidate in candidates:
        if candidate is not None and candidate > 0:
            return candidate
    return None


def _is_marketable(side: OrderSide, limit_price: Decimal, reference: Decimal) -> bool:
    if side is OrderSide.BUY:
        return limit_price >= reference
    return limit_price <= reference


def _next_business_day(day: date) -> date:
    """The next weekday after ``day`` (T+1 settlement; holidays not modeled)."""
    nxt = day + timedelta(days=1)
    while nxt.weekday() >= 5:  # Saturday=5, Sunday=6
        nxt += timedelta(days=1)
    return nxt


class PaperEngine:
    """SQLite-backed simulated portfolio.

    With ``settle_t1=True``, sale proceeds are held as *unsettled* cash until the
    next business day (T+1) and cannot fund new buys until they settle - modeling
    a real cash account and penalizing high-turnover strategies that rebuy with
    unsettled proceeds. Default ``False`` keeps the original instant-settlement
    behavior (existing sleeves are unaffected).

    With ``leverage > 1`` the sleeve models a *margin* account: it may hold up to
    ``leverage`` times its equity in positions (Reg T initial margin is ``2``),
    borrowing the difference (cash goes negative). Margin interest accrues daily on
    that debit at ``margin_rate`` (annual), and if equity falls below
    ``maintenance_margin`` of position value the engine force-liquidates positions
    (a margin call). ``leverage == 1`` (the default) disables all of this and is an
    exact no-op, so cash-account sleeves are unaffected.
    """

    def __init__(
        self,
        path: Path,
        *,
        starting_cash: Decimal,
        settle_t1: bool = False,
        leverage: Decimal = Decimal(1),
        margin_rate: Decimal = Decimal("0.12"),
        maintenance_margin: Decimal = Decimal("0.25"),
    ) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._starting_cash = starting_cash
        self._settle_t1 = settle_t1
        # Leverage below 1 is nonsensical; clamp to a cash account.
        self._leverage = leverage if leverage >= 1 else Decimal(1)
        self._margin_rate = margin_rate
        self._maintenance_margin = maintenance_margin
        self._init_db()

    @property
    def leverage(self) -> Decimal:
        """Buying-power multiplier (1 = cash account, 2 = Reg T margin)."""
        return self._leverage

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_account (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    starting_cash TEXT NOT NULL,
                    cash TEXT NOT NULL,
                    realized_pnl TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_positions (
                    symbol TEXT PRIMARY KEY,
                    quantity INTEGER NOT NULL,
                    avg_cost TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    side TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    limit_price TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT,
                    fill_price TEXT,
                    created_at TEXT NOT NULL,
                    filled_at TEXT
                );
                CREATE TABLE IF NOT EXISTS paper_unsettled (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    amount TEXT NOT NULL,
                    settle_date TEXT NOT NULL
                );
                """
            )
            # Migration: track the last margin-interest accrual date (nullable).
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(paper_account)")}
            if "last_accrual" not in columns:
                conn.execute("ALTER TABLE paper_account ADD COLUMN last_accrual TEXT")
            row = conn.execute("SELECT id FROM paper_account WHERE id = 1").fetchone()
            if row is None:
                stamp = datetime.now(UTC).isoformat()
                conn.execute(
                    "INSERT INTO paper_account (id, starting_cash, cash, realized_pnl, created_at) "
                    "VALUES (1, ?, ?, '0', ?)",
                    (str(self._starting_cash), str(self._starting_cash), stamp),
                )

    # --- reads ---------------------------------------------------------------

    def account(self) -> PaperAccount:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT starting_cash, cash, realized_pnl, created_at "
                "FROM paper_account WHERE id = 1"
            ).fetchone()
            unsettled = self._unsettled_total(conn)
        return PaperAccount(
            starting_cash=Decimal(row["starting_cash"]),
            cash=Decimal(row["cash"]),
            realized_pnl=Decimal(row["realized_pnl"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            unsettled_cash=unsettled,
        )

    def _unsettled_total(self, conn: sqlite3.Connection) -> Decimal:
        rows = conn.execute("SELECT amount FROM paper_unsettled").fetchall()
        return sum((Decimal(row["amount"]) for row in rows), Decimal(0))

    def _settle_due(self, conn: sqlite3.Connection, now: datetime) -> None:
        """Move any unsettled proceeds whose settle date has arrived into settled cash."""
        today = now.date().isoformat()
        rows = conn.execute(
            "SELECT id, amount FROM paper_unsettled WHERE settle_date <= ?", (today,)
        ).fetchall()
        if not rows:
            return
        due = sum((Decimal(row["amount"]) for row in rows), Decimal(0))
        current = Decimal(conn.execute("SELECT cash FROM paper_account WHERE id = 1").fetchone()[0])
        conn.execute("UPDATE paper_account SET cash = ? WHERE id = 1", (str(current + due),))
        conn.execute("DELETE FROM paper_unsettled WHERE settle_date <= ?", (today,))

    def _cost_basis(self, conn: sqlite3.Connection) -> Decimal:
        """Total cost basis of open positions (avg cost x quantity)."""
        rows = conn.execute(
            "SELECT quantity, avg_cost FROM paper_positions WHERE quantity > 0"
        ).fetchall()
        return sum((Decimal(row["avg_cost"]) * row["quantity"] for row in rows), Decimal(0))

    def buying_power(self) -> Decimal:
        """Cash still deployable into new buys, accounting for leverage.

        For a cash account (``leverage == 1``) this is exactly the settled cash.
        For a margin account it is ``leverage x equity - position_value`` measured
        at *cost basis* (the same basis the buy gate enforces, so the two never
        disagree). Never negative.
        """
        with self._connect() as conn:
            cash = Decimal(
                conn.execute("SELECT cash FROM paper_account WHERE id = 1").fetchone()["cash"]
            )
            cost_basis = self._cost_basis(conn)
        # leverage x (cash + cost_basis) - cost_basis, rearranged to avoid a temp.
        power = self._leverage * cash + (self._leverage - 1) * cost_basis
        return power if power > 0 else Decimal(0)

    def credit_cash(self, amount: Decimal) -> None:
        """Add settled cash to the sleeve (e.g. a dividend payment). No-op if <= 0.

        Dividends land as cash and sit until the strategy redeploys them on its next
        rebalance - the same way a real cash account receives them.
        """
        if amount <= 0:
            return
        with self._connect() as conn:
            current = Decimal(
                conn.execute("SELECT cash FROM paper_account WHERE id = 1").fetchone()["cash"]
            )
            conn.execute("UPDATE paper_account SET cash = ? WHERE id = 1", (str(current + amount),))

    def accrue(self, now: datetime) -> None:
        """Advance time-based bookkeeping to ``now``: settle T+1 proceeds, charge interest.

        Idempotent per calendar day for interest, so it is safe to call from both the
        cycle runner and :meth:`place_order`.
        """
        with self._connect() as conn:
            if self._settle_t1:
                self._settle_due(conn, now)
            self._accrue_interest(conn, now)

    def _accrue_interest(self, conn: sqlite3.Connection, now: datetime) -> None:
        """Charge margin interest on any debit balance since the last accrual day."""
        if self._leverage <= 1 or self._margin_rate <= 0:
            return
        row = conn.execute("SELECT cash, last_accrual FROM paper_account WHERE id = 1").fetchone()
        today = now.date()
        last = row["last_accrual"]
        if last is None:
            conn.execute(
                "UPDATE paper_account SET last_accrual = ? WHERE id = 1", (today.isoformat(),)
            )
            return
        days = (today - date.fromisoformat(last)).days
        if days <= 0:
            return
        cash = Decimal(row["cash"])
        if cash < 0:  # borrowing on margin; interest accrues on the debit
            interest = (-cash * self._margin_rate * Decimal(days) / Decimal(365)).quantize(
                Decimal("0.01")
            )
            conn.execute("UPDATE paper_account SET cash = ? WHERE id = 1", (str(cash - interest),))
        conn.execute("UPDATE paper_account SET last_accrual = ? WHERE id = 1", (today.isoformat(),))

    def enforce_maintenance(
        self, marks: dict[str, Decimal | None], now: datetime | None = None
    ) -> list[PaperOrder]:
        """Force-liquidate whole positions if equity falls below the maintenance margin.

        Models a margin call: while ``equity < maintenance_margin x position_value``,
        sell the largest position (at its mark) until the account is compliant or flat.
        A no-op for cash accounts (``leverage == 1``), which cannot go on margin.
        """
        if self._leverage <= 1:
            return []
        now = now or datetime.now(UTC)
        liquidated: list[PaperOrder] = []
        with self._connect() as conn:
            while True:
                rows = conn.execute(
                    "SELECT symbol, quantity, avg_cost FROM paper_positions WHERE quantity > 0"
                ).fetchall()
                if not rows:
                    break
                cash = Decimal(
                    conn.execute("SELECT cash FROM paper_account WHERE id = 1").fetchone()["cash"]
                )
                marked = []
                position_value = Decimal(0)
                for row in rows:
                    mark = marks.get(row["symbol"])
                    price = mark if mark is not None and mark > 0 else Decimal(row["avg_cost"])
                    value = price * row["quantity"]
                    position_value += value
                    marked.append((value, price, row))
                equity = cash + position_value
                if position_value <= 0 or equity >= self._maintenance_margin * position_value:
                    break
                # Sell the largest position in full at its mark (a broker-forced sale).
                _, price, row = max(marked, key=lambda item: item[0])
                position = PaperPosition(
                    symbol=row["symbol"],
                    quantity=row["quantity"],
                    avg_cost=Decimal(row["avg_cost"]),
                )
                self._apply_sell(conn, position, position.quantity, price, None)
                request = OrderRequest(
                    side=OrderSide.SELL,
                    symbol=position.symbol,
                    quantity=position.quantity,
                    limit_price=price,
                )
                liquidated.append(
                    self._record(
                        request,
                        now,
                        STATUS_FILLED,
                        "margin-call liquidation",
                        price,
                        now,
                        conn=conn,
                    )
                )
        return liquidated

    def positions(self) -> list[PaperPosition]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT symbol, quantity, avg_cost FROM paper_positions "
                "WHERE quantity > 0 ORDER BY symbol"
            ).fetchall()
        return [
            PaperPosition(
                symbol=row["symbol"], quantity=row["quantity"], avg_cost=Decimal(row["avg_cost"])
            )
            for row in rows
        ]

    def _position(self, conn: sqlite3.Connection, symbol: str) -> PaperPosition | None:
        row = conn.execute(
            "SELECT symbol, quantity, avg_cost FROM paper_positions WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        if row is None:
            return None
        return PaperPosition(
            symbol=row["symbol"], quantity=row["quantity"], avg_cost=Decimal(row["avg_cost"])
        )

    def recent_orders(self, limit: int = 20) -> list[PaperOrder]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_orders ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_order(row) for row in rows]

    def _row_to_order(self, row: sqlite3.Row) -> PaperOrder:
        return PaperOrder(
            id=row["id"],
            side=row["side"],
            symbol=row["symbol"],
            quantity=row["quantity"],
            limit_price=Decimal(row["limit_price"]),
            status=row["status"],
            reason=row["reason"],
            fill_price=Decimal(row["fill_price"]) if row["fill_price"] else None,
            created_at=datetime.fromisoformat(row["created_at"]),
            filled_at=datetime.fromisoformat(row["filled_at"]) if row["filled_at"] else None,
        )

    # --- writes --------------------------------------------------------------

    def reset(self, starting_cash: Decimal | None = None) -> None:
        """Wipe the sleeve back to a fresh starting balance."""
        cash = starting_cash if starting_cash is not None else self._starting_cash
        stamp = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute("DELETE FROM paper_positions")
            conn.execute("DELETE FROM paper_orders")
            conn.execute("DELETE FROM paper_unsettled")
            conn.execute(
                "UPDATE paper_account SET starting_cash = ?, cash = ?, realized_pnl = '0', "
                "created_at = ?, last_accrual = NULL WHERE id = 1",
                (str(cash), str(cash), stamp),
            )

    def place_order(
        self, request: OrderRequest, quote: Quote, *, now: datetime | None = None
    ) -> PaperOrder:
        """Attempt to fill an order against the current quote; record the result."""
        now = now or datetime.now(UTC)
        reference = fill_reference(request.side, quote)
        if reference is None:
            return self._record(request, now, STATUS_REJECTED, "no usable quote price", None, None)
        if not _is_marketable(request.side, request.limit_price, reference):
            need = "ask" if request.side is OrderSide.BUY else "bid"
            reason = f"not marketable at {reference} ({need}); limit {request.limit_price}"
            return self._record(request, now, STATUS_REJECTED, reason, None, None)

        # Settle matured T+1 proceeds and charge any due margin interest first.
        self.accrue(now)
        with self._connect() as conn:
            account_cash = Decimal(
                conn.execute("SELECT cash FROM paper_account WHERE id = 1").fetchone()["cash"]
            )
            if request.side is OrderSide.BUY:
                cost = reference * request.quantity
                # Buying power = leverage x equity - position value (cost basis).
                cost_basis = self._cost_basis(conn)
                power = self._leverage * account_cash + (self._leverage - 1) * cost_basis
                if cost > power:
                    if self._leverage > 1:
                        reason = (
                            f"exceeds margin buying power "
                            f"(need {cost}, buying power {power if power > 0 else Decimal(0)})"
                        )
                    else:
                        shortfall = "settled paper cash" if self._settle_t1 else "paper cash"
                        reason = f"insufficient {shortfall} (need {cost}, have {account_cash})"
                    return self._record(request, now, STATUS_REJECTED, reason, None, None)
                self._apply_buy(conn, request.symbol, request.quantity, reference, cost)
            else:
                position = self._position(conn, request.symbol)
                held = position.quantity if position else 0
                if held < request.quantity:
                    return self._record(
                        request,
                        now,
                        STATUS_REJECTED,
                        f"insufficient paper shares (need {request.quantity}, have {held})",
                        None,
                        None,
                    )
                # T+1: proceeds are unsettled until the next business day.
                settle_date = _next_business_day(now.date()) if self._settle_t1 else None
                self._apply_sell(conn, position, request.quantity, reference, settle_date)

            return self._record(request, now, STATUS_FILLED, None, reference, now, conn=conn)

    def _apply_buy(
        self,
        conn: sqlite3.Connection,
        symbol: str,
        quantity: int,
        price: Decimal,
        cost: Decimal,
    ) -> None:
        current_cash = Decimal(
            conn.execute("SELECT cash FROM paper_account WHERE id = 1").fetchone()["cash"]
        )
        conn.execute("UPDATE paper_account SET cash = ? WHERE id = 1", (str(current_cash - cost),))
        existing = self._position(conn, symbol)
        if existing is None:
            conn.execute(
                "INSERT INTO paper_positions (symbol, quantity, avg_cost) VALUES (?, ?, ?)",
                (symbol, quantity, str(price)),
            )
        else:
            total_qty = existing.quantity + quantity
            new_avg = (
                (existing.avg_cost * existing.quantity + price * quantity) / total_qty
            ).quantize(_COST_QUANT)
            conn.execute(
                "UPDATE paper_positions SET quantity = ?, avg_cost = ? WHERE symbol = ?",
                (total_qty, str(new_avg), symbol),
            )

    def _apply_sell(
        self,
        conn: sqlite3.Connection,
        position: PaperPosition | None,
        quantity: int,
        price: Decimal,
        settle_date: date | None = None,
    ) -> None:
        assert position is not None  # guarded by caller
        proceeds = price * quantity
        realized = (price - position.avg_cost) * quantity
        # Realized P&L books immediately on trade date; cash timing depends on T+1.
        if settle_date is None:
            new_cash = (
                Decimal(
                    conn.execute("SELECT cash FROM paper_account WHERE id = 1").fetchone()["cash"]
                )
                + proceeds
            )
            conn.execute("UPDATE paper_account SET cash = ? WHERE id = 1", (str(new_cash),))
        else:
            conn.execute(
                "INSERT INTO paper_unsettled (amount, settle_date) VALUES (?, ?)",
                (str(proceeds), settle_date.isoformat()),
            )
        new_realized = (
            Decimal(
                conn.execute("SELECT realized_pnl FROM paper_account WHERE id = 1").fetchone()[
                    "realized_pnl"
                ]
            )
            + realized
        )
        conn.execute(
            "UPDATE paper_account SET realized_pnl = ? WHERE id = 1",
            (str(new_realized),),
        )
        remaining = position.quantity - quantity
        if remaining > 0:
            conn.execute(
                "UPDATE paper_positions SET quantity = ? WHERE symbol = ?",
                (remaining, position.symbol),
            )
        else:
            conn.execute("DELETE FROM paper_positions WHERE symbol = ?", (position.symbol,))

    def _record(
        self,
        request: OrderRequest,
        now: datetime,
        status: str,
        reason: str | None,
        fill_price: Decimal | None,
        filled_at: datetime | None,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> PaperOrder:
        owns_conn = conn is None
        conn = conn or self._connect()
        try:
            cursor = conn.execute(
                "INSERT INTO paper_orders (side, symbol, quantity, limit_price, status, reason, "
                "fill_price, created_at, filled_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request.side.value,
                    request.symbol,
                    request.quantity,
                    str(request.limit_price),
                    status,
                    reason,
                    str(fill_price) if fill_price is not None else None,
                    now.isoformat(),
                    filled_at.isoformat() if filled_at else None,
                ),
            )
            order_id = int(cursor.lastrowid or 0)
            if owns_conn:
                conn.commit()
        finally:
            if owns_conn:
                conn.close()
        return PaperOrder(
            id=order_id,
            side=request.side.value,
            symbol=request.symbol,
            quantity=request.quantity,
            limit_price=request.limit_price,
            status=status,
            reason=reason,
            fill_price=fill_price,
            created_at=now,
            filled_at=filled_at,
        )

    def value(self, marks: dict[str, Decimal | None]) -> PaperValuation:
        """Value the sleeve given current marks per symbol (falls back to avg cost)."""
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
