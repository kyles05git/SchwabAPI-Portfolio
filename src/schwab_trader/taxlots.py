"""Local tax-lot ledger and realized-gain / holding-period calculator (idea #6).

A personal taxable account's after-tax return depends heavily on *which* shares you
sell: the holding period (short- vs long-term) and the cost basis of the lots you
relieve. Schwab's own system tracks this, but the Accounts/Trading API does not
expose per-lot acquisition dates, so this module keeps a local ledger built from the
fills this app records - a pragmatic start for an account traded mainly through it.

Two concerns, kept separate:

- :meth:`TaxLotStore.compute_sale` is **pure** - given a proposed sale it selects the
  lots that would be relieved (FIFO by default), and returns the proceeds, cost basis,
  and gain split into short- and long-term. It mutates nothing, so the order preview
  can show the tax consequence of a sell before anything happens.
- :meth:`TaxLotStore.record_purchase` and :meth:`TaxLotStore.apply_sale` mutate the
  ledger: a buy opens a lot; a sell relieves lots oldest-first (or per method) and
  records each realized piece for audit and later wash-sale checks.

Holding period follows the IRS rule (long-term = held *more than one year*), computed
by calendar date rather than a 365-day approximation. Estimates only - not tax advice;
they never gate an order on their own. No network calls happen here.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel


class LotMethod(StrEnum):
    """Lot-relief order. FIFO matches the common broker default."""

    FIFO = "FIFO"  # oldest lots first
    LIFO = "LIFO"  # newest lots first
    HIFO = "HIFO"  # highest cost-basis first (minimizes realized gain)

    @classmethod
    def parse(cls, value: str) -> LotMethod:
        """Parse a method name tolerantly, defaulting to FIFO."""
        try:
            return cls(value.strip().upper())
        except ValueError:
            return cls.FIFO


def is_long_term(acquired_at: datetime, sold_at: datetime) -> bool:
    """True if the holding period exceeds one year (IRS long-term rule).

    Long-term requires holding *more than* one year: the one-year anniversary itself
    is still short-term; one year and a day is long-term. Computed by calendar date to
    avoid a leap-year-sensitive day count.
    """
    try:
        anniversary = acquired_at.replace(year=acquired_at.year + 1)
    except ValueError:  # acquired on Feb 29 -> use Feb 28 the next (non-leap) year
        anniversary = acquired_at.replace(year=acquired_at.year + 1, day=28)
    return sold_at > anniversary


class TaxLot(BaseModel):
    """One purchase lot; ``quantity`` is the *remaining* open shares (0 = closed)."""

    id: int
    symbol: str
    quantity: Decimal
    original_quantity: Decimal
    cost_per_share: Decimal
    acquired_at: datetime


class LotConsumption(BaseModel):
    """The portion of one lot relieved by a sale, with its tax classification."""

    lot_id: int
    symbol: str
    quantity: Decimal
    cost_per_share: Decimal
    acquired_at: datetime
    proceeds_per_share: Decimal
    sold_at: datetime

    @property
    def proceeds(self) -> Decimal:
        return self.quantity * self.proceeds_per_share

    @property
    def cost_basis(self) -> Decimal:
        return self.quantity * self.cost_per_share

    @property
    def gain(self) -> Decimal:
        return self.proceeds - self.cost_basis

    @property
    def holding_days(self) -> int:
        return (self.sold_at - self.acquired_at).days

    @property
    def long_term(self) -> bool:
        return is_long_term(self.acquired_at, self.sold_at)


class RealizedGain(BaseModel):
    """Aggregate tax result of a (proposed or executed) sale across lots."""

    symbol: str
    quantity: Decimal
    proceeds: Decimal
    cost_basis: Decimal
    gain: Decimal
    short_term_gain: Decimal
    long_term_gain: Decimal
    consumptions: list[LotConsumption]
    covered_quantity: Decimal  # shares matched to open lots
    fully_covered: bool  # False if open lots did not cover the whole sale

    @property
    def is_loss(self) -> bool:
        return self.gain < 0


class RealizedSaleRecord(BaseModel):
    """A persisted realized lot sale (used for wash-sale look-back)."""

    symbol: str
    quantity: Decimal
    gain: Decimal
    sold_at: datetime
    long_term: bool


class WashSaleWarning(BaseModel):
    """An advisory that a proposed order may trigger the IRS wash-sale rule.

    A *warning only* - callers surface it but never block on it. The wash-sale rule
    disallows a loss when substantially identical shares are (re)bought within 30 days
    before or after the loss sale; this flags the observable half of that window from
    the local ledger (future trades can't be known in advance).
    """

    symbol: str
    side: str  # BUY or SELL
    window_days: int
    reason: str
    related_dates: list[datetime]


def _order_lots(lots: list[TaxLot], method: LotMethod) -> list[TaxLot]:
    """Order open lots for relief per ``method``."""
    if method is LotMethod.LIFO:
        return sorted(lots, key=lambda lot: lot.acquired_at, reverse=True)
    if method is LotMethod.HIFO:
        return sorted(lots, key=lambda lot: lot.cost_per_share, reverse=True)
    return sorted(lots, key=lambda lot: lot.acquired_at)  # FIFO


class TaxLotStore:
    """SQLite-backed ledger of open/closed tax lots and realized lot sales."""

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
                CREATE TABLE IF NOT EXISTS tax_lots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    original_quantity TEXT NOT NULL,
                    cost_per_share TEXT NOT NULL,
                    acquired_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tax_lots_symbol
                    ON tax_lots (symbol, acquired_at);

                CREATE TABLE IF NOT EXISTS realized_sales (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lot_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    cost_per_share TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    proceeds_per_share TEXT NOT NULL,
                    sold_at TEXT NOT NULL,
                    gain TEXT NOT NULL,
                    long_term INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_realized_symbol
                    ON realized_sales (symbol, sold_at);
                """
            )

    # --- purchases -----------------------------------------------------------

    def record_purchase(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        cost_per_share: Decimal,
        acquired_at: datetime,
    ) -> int:
        """Open a new tax lot for a buy fill; return its lot id."""
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO tax_lots (symbol, quantity, original_quantity, "
                "cost_per_share, acquired_at) VALUES (?, ?, ?, ?, ?)",
                (
                    symbol,
                    str(quantity),
                    str(quantity),
                    str(cost_per_share),
                    acquired_at.isoformat(),
                ),
            )
            return int(cursor.lastrowid or 0)

    def open_lots(self, symbol: str | None = None) -> list[TaxLot]:
        """All lots with remaining shares, oldest first (optionally one symbol)."""
        query = "SELECT * FROM tax_lots WHERE CAST(quantity AS REAL) > 0"
        params: tuple[str, ...] = ()
        if symbol is not None:
            query += " AND symbol = ?"
            params = (symbol,)
        query += " ORDER BY acquired_at ASC, id ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_lot(row) for row in rows]

    # --- sales ---------------------------------------------------------------

    def compute_sale(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        price: Decimal,
        sold_at: datetime,
        method: LotMethod = LotMethod.FIFO,
    ) -> RealizedGain:
        """Pure: the realized gain a sale *would* produce, without mutating anything.

        Relieves open lots in ``method`` order until ``quantity`` is filled, splitting
        the gain into short- and long-term. If open lots do not cover the full sale,
        ``fully_covered`` is False and only the covered portion is accounted.
        """
        remaining = quantity
        consumptions: list[LotConsumption] = []
        for lot in _order_lots(self.open_lots(symbol), method):
            if remaining <= 0:
                break
            take = min(lot.quantity, remaining)
            consumptions.append(
                LotConsumption(
                    lot_id=lot.id,
                    symbol=symbol,
                    quantity=take,
                    cost_per_share=lot.cost_per_share,
                    acquired_at=lot.acquired_at,
                    proceeds_per_share=price,
                    sold_at=sold_at,
                )
            )
            remaining -= take
        return self._aggregate(symbol, quantity, consumptions)

    def apply_sale(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        price: Decimal,
        sold_at: datetime,
        method: LotMethod = LotMethod.FIFO,
    ) -> RealizedGain:
        """Relieve lots for a sell fill (mutating) and persist each realized piece."""
        result = self.compute_sale(
            symbol=symbol, quantity=quantity, price=price, sold_at=sold_at, method=method
        )
        with self._connect() as conn:
            for piece in result.consumptions:
                row = conn.execute(
                    "SELECT quantity FROM tax_lots WHERE id = ?", (piece.lot_id,)
                ).fetchone()
                if row is None:
                    continue
                new_qty = Decimal(row["quantity"]) - piece.quantity
                conn.execute(
                    "UPDATE tax_lots SET quantity = ? WHERE id = ?",
                    (str(new_qty), piece.lot_id),
                )
                conn.execute(
                    "INSERT INTO realized_sales (lot_id, symbol, quantity, cost_per_share, "
                    "acquired_at, proceeds_per_share, sold_at, gain, long_term) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        piece.lot_id,
                        symbol,
                        str(piece.quantity),
                        str(piece.cost_per_share),
                        piece.acquired_at.isoformat(),
                        str(piece.proceeds_per_share),
                        sold_at.isoformat(),
                        str(piece.gain),
                        int(piece.long_term),
                    ),
                )
        return result

    # --- wash-sale look-back (advisory) --------------------------------------

    def recent_loss_sales(
        self, symbol: str, *, since: datetime, until: datetime
    ) -> list[RealizedSaleRecord]:
        """Realized *loss* sales of ``symbol`` with ``sold_at`` in [since, until]."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT symbol, quantity, gain, sold_at, long_term FROM realized_sales "
                "WHERE symbol = ? AND CAST(gain AS REAL) < 0 AND sold_at >= ? AND sold_at <= ? "
                "ORDER BY sold_at DESC",
                (symbol, since.isoformat(), until.isoformat()),
            ).fetchall()
        return [
            RealizedSaleRecord(
                symbol=row["symbol"],
                quantity=Decimal(row["quantity"]),
                gain=Decimal(row["gain"]),
                sold_at=datetime.fromisoformat(row["sold_at"]),
                long_term=bool(row["long_term"]),
            )
            for row in rows
        ]

    def purchases_between(self, symbol: str, *, start: datetime, end: datetime) -> list[TaxLot]:
        """Lots of ``symbol`` acquired in [start, end] (open or already closed)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tax_lots WHERE symbol = ? AND acquired_at >= ? AND acquired_at <= ? "
                "ORDER BY acquired_at DESC",
                (symbol, start.isoformat(), end.isoformat()),
            ).fetchall()
        return [self._row_to_lot(row) for row in rows]

    def wash_sale_on_buy(
        self, *, symbol: str, buy_at: datetime, window_days: int
    ) -> WashSaleWarning | None:
        """Warn if buying ``symbol`` now would wash a loss realized in the prior window.

        Buying substantially identical shares within 30 days of a loss sale disallows
        that loss. Advisory only.
        """
        if window_days <= 0:
            return None
        since = buy_at - timedelta(days=window_days)
        losses = self.recent_loss_sales(symbol, since=since, until=buy_at)
        if not losses:
            return None
        return WashSaleWarning(
            symbol=symbol,
            side="BUY",
            window_days=window_days,
            reason=(
                f"{symbol} was sold at a loss within the last {window_days} days; buying it "
                "back may trigger a wash sale and disallow that loss."
            ),
            related_dates=[loss.sold_at for loss in losses],
        )

    def wash_sale_on_sell(
        self, *, symbol: str, sold_at: datetime, is_loss: bool, window_days: int
    ) -> WashSaleWarning | None:
        """Warn if a *loss* sale of ``symbol`` follows a purchase in the prior window.

        Only a concern when the sale realizes a loss. Flags replacement shares bought
        within the window before the sale; a repurchase within 30 days *after* would
        also trigger, but that is unknowable in advance. Advisory only.
        """
        if not is_loss or window_days <= 0:
            return None
        start = sold_at - timedelta(days=window_days)
        buys = self.purchases_between(symbol, start=start, end=sold_at)
        if not buys:
            return None
        return WashSaleWarning(
            symbol=symbol,
            side="SELL",
            window_days=window_days,
            reason=(
                f"{symbol} was bought within the last {window_days} days before this loss "
                "sale; the loss may be disallowed as a wash sale (a repurchase within "
                f"{window_days} days after would also trigger it)."
            ),
            related_dates=[buy.acquired_at for buy in buys],
        )

    def _aggregate(
        self, symbol: str, quantity: Decimal, consumptions: list[LotConsumption]
    ) -> RealizedGain:
        proceeds = sum((c.proceeds for c in consumptions), Decimal(0))
        cost_basis = sum((c.cost_basis for c in consumptions), Decimal(0))
        short_term = sum((c.gain for c in consumptions if not c.long_term), Decimal(0))
        long_term = sum((c.gain for c in consumptions if c.long_term), Decimal(0))
        covered = sum((c.quantity for c in consumptions), Decimal(0))
        return RealizedGain(
            symbol=symbol,
            quantity=quantity,
            proceeds=proceeds,
            cost_basis=cost_basis,
            gain=proceeds - cost_basis,
            short_term_gain=short_term,
            long_term_gain=long_term,
            consumptions=consumptions,
            covered_quantity=covered,
            fully_covered=covered >= quantity,
        )

    def _row_to_lot(self, row: sqlite3.Row) -> TaxLot:
        return TaxLot(
            id=row["id"],
            symbol=row["symbol"],
            quantity=Decimal(row["quantity"]),
            original_quantity=Decimal(row["original_quantity"]),
            cost_per_share=Decimal(row["cost_per_share"]),
            acquired_at=datetime.fromisoformat(row["acquired_at"]),
        )
