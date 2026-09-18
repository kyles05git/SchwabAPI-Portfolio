"""Bounded-autonomy safety layer: kill switch + per-day limits (CLAUDE.md rule 14).

Autonomous trading is only permitted inside enforced controls. This module provides
them as a small, testable unit that any autonomous path must consult:

- a **kill switch** (a marker file) that halts all autonomous loops immediately and
  persists across restarts,
- a **daily activity ledger** (trades, realized P&L, and the day's opening marked
  equity per day, persisted), and
- a **gate** that fails closed on a kill switch, a per-day trade cap, a per-day loss
  limit, a per-order notional cap, or a total-capital cap.

The daily-loss limit is measured **mark-to-market** (opening equity minus current
marked equity) when the caller supplies equity, so unrealized drawdown on open
positions counts, and breaching it engages the kill switch so trading halts until a
human resumes it. It falls back to realized-only P&L when no equity is given.

Everything is checked *before* an autonomous order and recorded *after* it. Limits of
``0`` mean "disabled" for now (the app convention), but autonomous *live* trading must
set them - that requirement is enforced where a live autonomous path is wired.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel


class KillSwitchStatus(BaseModel):
    engaged: bool
    reason: str | None = None
    since: datetime | None = None


class KillSwitch:
    """A persistent, file-based halt for all autonomous trading.

    Presence of the file means *engaged*. It survives restarts and is trivial to
    trigger out-of-band (delete/create the file) in an emergency.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def is_engaged(self) -> bool:
        return self.path.exists()

    def engage(self, reason: str = "") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).isoformat()
        self.path.write_text(f"{stamp}\n{reason}\n", encoding="utf-8")

    def disengage(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.remove(self.path)

    def status(self) -> KillSwitchStatus:
        if not self.path.exists():
            return KillSwitchStatus(engaged=False)
        since: datetime | None = None
        reason: str | None = None
        with contextlib.suppress(OSError, ValueError):
            lines = self.path.read_text(encoding="utf-8").splitlines()
            if lines:
                since = datetime.fromisoformat(lines[0].strip())
            if len(lines) > 1:
                reason = lines[1].strip() or None
        return KillSwitchStatus(engaged=True, reason=reason, since=since)


class DayActivity(BaseModel):
    day: date
    trades: int = 0
    realized_pnl: Decimal = Decimal(0)  # negative = net loss
    start_equity: Decimal | None = None  # marked account equity at the day's first check


class SafetyLedger:
    """SQLite record of autonomous trades + realized P&L per calendar day (UTC)."""

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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_activity (
                    day TEXT PRIMARY KEY,
                    trades INTEGER NOT NULL DEFAULT 0,
                    realized_pnl TEXT NOT NULL DEFAULT '0'
                )
                """
            )
            # Migration: mark-to-market loss limit needs the day's opening equity.
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(agent_activity)")}
            if "start_equity" not in cols:
                conn.execute("ALTER TABLE agent_activity ADD COLUMN start_equity TEXT")

    def day(self, now: datetime | None = None) -> DayActivity:
        today = (now or datetime.now(UTC)).date()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT trades, realized_pnl, start_equity FROM agent_activity WHERE day = ?",
                (today.isoformat(),),
            ).fetchone()
        if row is None:
            return DayActivity(day=today)
        start_equity = row["start_equity"]
        return DayActivity(
            day=today,
            trades=row["trades"],
            realized_pnl=Decimal(row["realized_pnl"]),
            start_equity=Decimal(start_equity) if start_equity is not None else None,
        )

    def note_start_equity(self, equity: Decimal, now: datetime | None = None) -> Decimal:
        """Record today's opening marked equity if not already set; return the stored value.

        Idempotent: the first observation of the day wins, so the loss basis is the
        equity *before* the day's trading, not a value that drifts with each order.
        """
        today = (now or datetime.now(UTC)).date().isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO agent_activity (day, start_equity) VALUES (?, ?) "
                "ON CONFLICT(day) DO UPDATE SET start_equity = COALESCE(start_equity, ?)",
                (today, str(equity), str(equity)),
            )
            row = conn.execute(
                "SELECT start_equity FROM agent_activity WHERE day = ?", (today,)
            ).fetchone()
        return Decimal(row["start_equity"])

    def record(
        self,
        *,
        trades: int = 1,
        realized_pnl_delta: Decimal = Decimal(0),
        now: datetime | None = None,
    ) -> None:
        """Add ``trades`` and a realized-P&L delta to today's row (upsert)."""
        today = (now or datetime.now(UTC)).date().isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO agent_activity (day, trades, realized_pnl) VALUES (?, ?, ?) "
                "ON CONFLICT(day) DO UPDATE SET trades = trades + ?, "
                "realized_pnl = CAST(CAST(realized_pnl AS REAL) + ? AS TEXT)",
                (today, trades, str(realized_pnl_delta), trades, str(realized_pnl_delta)),
            )


@dataclass(frozen=True)
class SafetyLimits:
    """Enforced caps for autonomous trading (0 = disabled, per app convention)."""

    capital_cap: Decimal = Decimal(0)
    daily_loss_limit: Decimal = Decimal(0)
    max_trades_per_day: int = 0
    max_order_notional: Decimal = Decimal(0)


class SafetyDecision(BaseModel):
    allowed: bool
    reason: str = ""


class SafetyGate:
    """Fails closed: an order is allowed only if every applicable limit passes."""

    def __init__(self, kill_switch: KillSwitch, ledger: SafetyLedger, limits: SafetyLimits) -> None:
        self.kill_switch = kill_switch
        self.ledger = ledger
        self.limits = limits

    def check(
        self,
        *,
        order_notional: Decimal,
        deployed: Decimal = Decimal(0),
        equity: Decimal | None = None,
        now: datetime | None = None,
    ) -> SafetyDecision:
        """Decide whether an autonomous order may proceed.

        ``deployed`` is the capital already at work in the agent sleeve (for the
        total-capital cap). ``equity`` is the current *marked* account equity; when
        given, the daily-loss limit is measured mark-to-market (opening equity minus
        current equity), so open-position losses count - not only realized P&L - and
        breaching it **engages the kill switch** so all autonomous trading halts until
        a human resumes it. Trade count for the day comes from the ledger. Any tripped
        limit blocks; a kill switch blocks unconditionally.
        """
        if self.kill_switch.is_engaged():
            return SafetyDecision(allowed=False, reason="kill switch engaged")
        day = self.ledger.day(now)
        limits = self.limits
        if limits.max_trades_per_day and day.trades >= limits.max_trades_per_day:
            return SafetyDecision(
                allowed=False,
                reason=f"daily trade limit reached ({day.trades}/{limits.max_trades_per_day})",
            )
        if limits.daily_loss_limit:
            if equity is not None:
                # Mark-to-market: realized + unrealized loss since the day opened.
                start = self.ledger.note_start_equity(equity, now)
                loss = start - equity
            else:
                loss = -day.realized_pnl  # fallback: realized-only when no equity given
            if loss >= limits.daily_loss_limit:
                self.kill_switch.engage(
                    reason=f"daily loss limit hit: loss {loss} >= cap {limits.daily_loss_limit}"
                )
                return SafetyDecision(
                    allowed=False,
                    reason=(
                        f"daily loss limit reached (loss {loss}, cap {limits.daily_loss_limit}); "
                        "kill switch engaged"
                    ),
                )
        if limits.max_order_notional and order_notional > limits.max_order_notional:
            return SafetyDecision(
                allowed=False,
                reason=f"order notional {order_notional} exceeds cap {limits.max_order_notional}",
            )
        if limits.capital_cap and deployed + order_notional > limits.capital_cap:
            return SafetyDecision(
                allowed=False,
                reason=(
                    f"capital cap exceeded (deployed {deployed} + "
                    f"{order_notional} > {limits.capital_cap})"
                ),
            )
        return SafetyDecision(allowed=True)
