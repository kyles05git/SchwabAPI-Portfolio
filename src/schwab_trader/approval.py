"""Pending live-order approvals: single-use, time-boxed tokens for the runner.

The notify-and-approve runner (``NEXT_TASKS.md`` idea #1) previews an order, stores
it here as a *pending approval* with an unguessable token, and notifies the user
out-of-band. Approving with that token reconstructs the exact order and hands it to
the normal gated live-submit flow. The token stands in for the interactive typed
confirmation phrase, so it must be at least as strong:

- **single-use** - consuming a token marks it, so a replay fails;
- **time-boxed** - a token past its expiry is invalid;
- **bound to the exact order** - the stored order fields must reproduce the recorded
  fingerprint under the caller's account hash, so a tampered order (different symbol,
  quantity, or price) or a token used against a different account is rejected.

No secret is stored: the account hash is folded one-way into the fingerprint (see
:func:`schwab_trader.state.compute_fingerprint`); only the masked tail is kept for
display. This module performs no network calls and no order submission - it only
guards *which* order the runner is permitted to submit. Fails closed: any doubt
(unknown token, expired, already used, mismatch) raises and blocks.
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel

from schwab_trader.models import (
    OrderDuration,
    OrderRequest,
    OrderSession,
    OrderSide,
    OrderType,
)
from schwab_trader.state import compute_fingerprint

STATUS_PENDING = "pending"
STATUS_CONSUMED = "consumed"

_TOKEN_BYTES = 32  # 256 bits of entropy via secrets.token_urlsafe


class ApprovalError(RuntimeError):
    """A token could not be consumed (unknown, expired, used, or mismatched)."""


class ApprovalRecord(BaseModel):
    """A stored pending approval (no secret - the account hash is not kept)."""

    id: int
    token: str
    fingerprint: str
    account_tail: str
    side: str
    symbol: str
    quantity: int
    limit_price: str
    order_type: str
    session: str
    duration: str
    rationale: str | None
    status: str
    created_at: datetime
    expires_at: datetime

    def to_request(self) -> OrderRequest:
        """Reconstruct the exact order this approval was issued for."""
        return OrderRequest(
            side=OrderSide(self.side),
            symbol=self.symbol,
            quantity=self.quantity,
            limit_price=Decimal(self.limit_price),
            order_type=OrderType(self.order_type),
            session=OrderSession(self.session),
            duration=OrderDuration(self.duration),
        )

    def is_expired(self, *, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) >= self.expires_at


def _mask_tail(account_hash: str) -> str:
    return f"****{account_hash[-4:]}" if len(account_hash) >= 4 else "****"


class ApprovalStore:
    """SQLite-backed store of single-use, time-boxed order-approval tokens."""

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
                CREATE TABLE IF NOT EXISTS approvals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token TEXT NOT NULL UNIQUE,
                    fingerprint TEXT NOT NULL,
                    account_tail TEXT NOT NULL,
                    side TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    limit_price TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    session TEXT NOT NULL,
                    duration TEXT NOT NULL,
                    rationale TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_approvals_token ON approvals (token);
                """
            )

    def issue(
        self,
        *,
        account_hash: str,
        request: OrderRequest,
        ttl: timedelta,
        rationale: str | None = None,
        now: datetime | None = None,
    ) -> str:
        """Store a pending approval for ``request`` and return its opaque token."""
        now = now or datetime.now(UTC)
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        fingerprint = compute_fingerprint(account_hash, request)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO approvals (
                    token, fingerprint, account_tail, side, symbol, quantity,
                    limit_price, order_type, session, duration, rationale, status,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token,
                    fingerprint,
                    _mask_tail(account_hash),
                    request.side.value,
                    request.symbol,
                    request.quantity,
                    f"{request.limit_price:.2f}",
                    request.order_type.value,
                    request.session.value,
                    request.duration.value,
                    rationale,
                    STATUS_PENDING,
                    now.isoformat(),
                    (now + ttl).isoformat(),
                ),
            )
        return token

    def get(self, token: str) -> ApprovalRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE token = ?", (token,)).fetchone()
        return self._row_to_record(row) if row is not None else None

    def consume(
        self, *, token: str, account_hash: str, now: datetime | None = None
    ) -> ApprovalRecord:
        """Validate and atomically single-use ``token``; return the approved order.

        Fails closed with :class:`ApprovalError` on an unknown, expired, already-used,
        or mismatched token. On success the token is marked consumed so any replay is
        rejected, and the returned record's order is guaranteed to reproduce the
        recorded fingerprint under ``account_hash``.
        """
        now = now or datetime.now(UTC)
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE token = ?", (token,)).fetchone()
            if row is None:
                raise ApprovalError("unknown approval token")
            record = self._row_to_record(row)
            if record.status != STATUS_PENDING:
                raise ApprovalError("approval token already used")
            if record.is_expired(now=now):
                raise ApprovalError("approval token expired")
            # The order must reproduce the recorded fingerprint under this account:
            # a tampered order or a different account breaks the match.
            expected = compute_fingerprint(account_hash, record.to_request())
            if not secrets.compare_digest(expected, record.fingerprint):
                raise ApprovalError("approval does not match this order/account")
            # Atomic single-use: only the row still pending flips to consumed.
            cursor = conn.execute(
                "UPDATE approvals SET status = ? WHERE id = ? AND status = ?",
                (STATUS_CONSUMED, record.id, STATUS_PENDING),
            )
            if cursor.rowcount != 1:
                raise ApprovalError("approval token already used")
        return record

    def pending(self, *, now: datetime | None = None) -> list[ApprovalRecord]:
        """All still-valid (pending and unexpired) approvals, newest first."""
        now = now or datetime.now(UTC)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM approvals WHERE status = ? AND expires_at > ? ORDER BY id DESC",
                (STATUS_PENDING, now.isoformat()),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def sweep_expired(self, *, now: datetime | None = None) -> int:
        """Delete expired pending tokens; return how many were removed."""
        now = now or datetime.now(UTC)
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM approvals WHERE status = ? AND expires_at <= ?",
                (STATUS_PENDING, now.isoformat()),
            )
            return int(cursor.rowcount)

    def _row_to_record(self, row: sqlite3.Row) -> ApprovalRecord:
        return ApprovalRecord(
            id=row["id"],
            token=row["token"],
            fingerprint=row["fingerprint"],
            account_tail=row["account_tail"],
            side=row["side"],
            symbol=row["symbol"],
            quantity=row["quantity"],
            limit_price=row["limit_price"],
            order_type=row["order_type"],
            session=row["session"],
            duration=row["duration"],
            rationale=row["rationale"],
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
        )
