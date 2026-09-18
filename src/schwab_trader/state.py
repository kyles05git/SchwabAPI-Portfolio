"""SQLite storage for order-intent fingerprints and a local audit log.

Two responsibilities:

- **Duplicate protection**: a stable fingerprint is computed from the account,
  side, symbol, quantity, order type, limit price, session, and duration. Pending
  and submitted fingerprints are persisted so an identical order within a bounded
  window is blocked - and, because it is on disk, the block survives restarts.
- **Audit log**: a sanitized, append-only record of what happened.

Privacy: the raw account hash is never stored. It is folded into the fingerprint
(a one-way SHA-256 digest) and otherwise only the masked last-4 tail is kept.
Callers must pass already-sanitized audit details (no tokens, codes, or full
identifiers).
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from schwab_trader.models import OrderRequest

STATUS_PENDING = "pending"
STATUS_SUBMITTED = "submitted"
STATUS_FAILED = "failed"

_ACTIVE_STATUSES = (STATUS_PENDING, STATUS_SUBMITTED)


def compute_fingerprint(account_hash: str, request: OrderRequest) -> str:
    """Compute a stable SHA-256 fingerprint of an order's identity.

    Includes the account hash so orders for different accounts never collide, but
    the digest is one-way so the raw hash is not recoverable from storage.
    """
    parts = [
        account_hash,
        request.side.value,
        request.symbol,
        str(request.quantity),
        request.order_type.value,
        f"{request.limit_price:.2f}",
        request.session.value,
        request.duration.value,
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _mask_tail(account_hash: str) -> str:
    return f"****{account_hash[-4:]}" if len(account_hash) >= 4 else "****"


class DuplicateRecord(BaseModel):
    id: int
    status: str
    order_id: str | None
    created_at: datetime


class IntentRecord(BaseModel):
    id: int
    account_tail: str
    side: str
    symbol: str
    quantity: int
    order_type: str
    limit_price: str
    status: str
    created_at: datetime
    order_id: str | None


class AuditRecord(BaseModel):
    id: int
    ts: datetime
    command: str
    account_tail: str | None
    event: str
    detail: str | None


class StateStore:
    """SQLite-backed store for order intents and the audit log."""

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
                CREATE TABLE IF NOT EXISTS order_intents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL,
                    account_tail TEXT NOT NULL,
                    side TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    order_type TEXT NOT NULL,
                    limit_price TEXT NOT NULL,
                    session TEXT NOT NULL,
                    duration TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    order_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_intents_fp
                    ON order_intents (fingerprint, created_at);

                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    command TEXT NOT NULL,
                    account_tail TEXT,
                    event TEXT NOT NULL,
                    detail TEXT
                );
                """
            )

    # --- duplicate protection ------------------------------------------------

    def find_recent_duplicate(
        self,
        fingerprint: str,
        within: timedelta,
        *,
        now: datetime | None = None,
    ) -> DuplicateRecord | None:
        """Return the most recent active intent with this fingerprint, if any.

        Only pending/submitted intents within ``within`` count as duplicates.
        """
        now = now or datetime.now(UTC)
        cutoff = (now - within).isoformat()
        placeholders = ",".join("?" for _ in _ACTIVE_STATUSES)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT id, status, order_id, created_at
                FROM order_intents
                WHERE fingerprint = ?
                  AND status IN ({placeholders})
                  AND created_at >= ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (fingerprint, *_ACTIVE_STATUSES, cutoff),
            ).fetchone()
        if row is None:
            return None
        return DuplicateRecord(
            id=row["id"],
            status=row["status"],
            order_id=row["order_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def record_pending(
        self,
        *,
        account_hash: str,
        request: OrderRequest,
        now: datetime | None = None,
    ) -> int:
        """Insert a pending intent and return its row id."""
        now = now or datetime.now(UTC)
        stamp = now.isoformat()
        fingerprint = compute_fingerprint(account_hash, request)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO order_intents (
                    fingerprint, account_tail, side, symbol, quantity, order_type,
                    limit_price, session, duration, status, created_at, updated_at, order_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    fingerprint,
                    _mask_tail(account_hash),
                    request.side.value,
                    request.symbol,
                    request.quantity,
                    request.order_type.value,
                    f"{request.limit_price:.2f}",
                    request.session.value,
                    request.duration.value,
                    STATUS_PENDING,
                    stamp,
                    stamp,
                ),
            )
            return int(cursor.lastrowid or 0)

    def mark_submitted(
        self, intent_id: int, *, order_id: str | None, now: datetime | None = None
    ) -> None:
        self._update_status(intent_id, STATUS_SUBMITTED, order_id=order_id, now=now)

    def mark_failed(self, intent_id: int, *, now: datetime | None = None) -> None:
        self._update_status(intent_id, STATUS_FAILED, order_id=None, now=now)

    def _update_status(
        self, intent_id: int, status: str, *, order_id: str | None, now: datetime | None
    ) -> None:
        stamp = (now or datetime.now(UTC)).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE order_intents
                SET status = ?, updated_at = ?,
                    order_id = COALESCE(?, order_id)
                WHERE id = ?
                """,
                (status, stamp, order_id, intent_id),
            )

    def recent_intents(self, limit: int = 20) -> list[IntentRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, account_tail, side, symbol, quantity, order_type,
                       limit_price, status, created_at, order_id
                FROM order_intents
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            IntentRecord(
                id=row["id"],
                account_tail=row["account_tail"],
                side=row["side"],
                symbol=row["symbol"],
                quantity=row["quantity"],
                order_type=row["order_type"],
                limit_price=row["limit_price"],
                status=row["status"],
                created_at=datetime.fromisoformat(row["created_at"]),
                order_id=row["order_id"],
            )
            for row in rows
        ]

    # --- audit log -----------------------------------------------------------

    def append_audit(
        self,
        *,
        command: str,
        event: str,
        account_tail: str | None = None,
        detail: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Append a sanitized audit record. Callers must not pass secrets."""
        stamp = (now or datetime.now(UTC)).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO audit_log (ts, command, account_tail, event, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (stamp, command, account_tail, event, detail),
            )

    def recent_audit(self, limit: int = 50) -> list[AuditRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, ts, command, account_tail, event, detail "
                "FROM audit_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            AuditRecord(
                id=row["id"],
                ts=datetime.fromisoformat(row["ts"]),
                command=row["command"],
                account_tail=row["account_tail"],
                event=row["event"],
                detail=row["detail"],
            )
            for row in rows
        ]
