"""Tests for SQLite duplicate protection and audit logging (Phase 10)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from schwab_trader.models import OrderRequest, OrderSide
from schwab_trader.state import (
    StateStore,
    compute_fingerprint,
)

ACCOUNT_HASH = "ABCDEF1234567890"
NOW = datetime(2026, 7, 13, 15, 0, tzinfo=UTC)


def _request(**overrides: object) -> OrderRequest:
    base: dict[str, object] = {
        "side": OrderSide.BUY,
        "symbol": "SOFI",
        "quantity": 1,
        "limit_price": Decimal("18.00"),
    }
    base.update(overrides)
    return OrderRequest(**base)  # type: ignore[arg-type]


# --- fingerprint ------------------------------------------------------------


def test_fingerprint_is_stable_and_hex() -> None:
    fp1 = compute_fingerprint(ACCOUNT_HASH, _request())
    fp2 = compute_fingerprint(ACCOUNT_HASH, _request())
    assert fp1 == fp2
    assert len(fp1) == 64  # sha256 hex


def test_fingerprint_changes_with_any_field() -> None:
    base = compute_fingerprint(ACCOUNT_HASH, _request())
    assert compute_fingerprint(ACCOUNT_HASH, _request(quantity=2)) != base
    assert compute_fingerprint(ACCOUNT_HASH, _request(side=OrderSide.SELL)) != base
    assert compute_fingerprint(ACCOUNT_HASH, _request(limit_price=Decimal("18.01"))) != base
    assert compute_fingerprint("OTHERHASH9999", _request()) != base


# --- duplicate detection ----------------------------------------------------


def test_records_and_detects_duplicate_within_window(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    fingerprint = compute_fingerprint(ACCOUNT_HASH, _request())
    assert store.find_recent_duplicate(fingerprint, timedelta(seconds=300), now=NOW) is None

    store.record_pending(account_hash=ACCOUNT_HASH, request=_request(), now=NOW)
    dup = store.find_recent_duplicate(fingerprint, timedelta(seconds=300), now=NOW)
    assert dup is not None
    assert dup.status == "pending"


def test_duplicate_not_detected_outside_window(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    store.record_pending(account_hash=ACCOUNT_HASH, request=_request(), now=NOW)
    fingerprint = compute_fingerprint(ACCOUNT_HASH, _request())
    later = NOW + timedelta(seconds=301)
    assert store.find_recent_duplicate(fingerprint, timedelta(seconds=300), now=later) is None


def test_failed_intent_is_not_a_duplicate(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    intent_id = store.record_pending(account_hash=ACCOUNT_HASH, request=_request(), now=NOW)
    store.mark_failed(intent_id, now=NOW)
    fingerprint = compute_fingerprint(ACCOUNT_HASH, _request())
    assert store.find_recent_duplicate(fingerprint, timedelta(seconds=300), now=NOW) is None


def test_mark_submitted_sets_order_id_and_still_blocks(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    intent_id = store.record_pending(account_hash=ACCOUNT_HASH, request=_request(), now=NOW)
    store.mark_submitted(intent_id, order_id="ORD-123", now=NOW)
    fingerprint = compute_fingerprint(ACCOUNT_HASH, _request())
    dup = store.find_recent_duplicate(fingerprint, timedelta(seconds=300), now=NOW)
    assert dup is not None
    assert dup.status == "submitted"
    assert dup.order_id == "ORD-123"


def test_duplicate_block_persists_across_restart(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    StateStore(db).record_pending(account_hash=ACCOUNT_HASH, request=_request(), now=NOW)
    # A brand-new store instance (as if the process restarted) still sees it.
    reopened = StateStore(db)
    fingerprint = compute_fingerprint(ACCOUNT_HASH, _request())
    assert reopened.find_recent_duplicate(fingerprint, timedelta(seconds=300), now=NOW) is not None


# --- privacy ----------------------------------------------------------------


def test_raw_account_hash_is_never_stored(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    store = StateStore(db)
    store.record_pending(account_hash=ACCOUNT_HASH, request=_request(), now=NOW)
    store.append_audit(command="order submit", event="test", account_tail="****7890")
    raw = db.read_bytes()
    assert ACCOUNT_HASH.encode() not in raw
    # Only the masked tail is present.
    intents = store.recent_intents()
    assert intents[0].account_tail == "****7890"


# --- audit log --------------------------------------------------------------


def test_audit_append_and_read(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    store.append_audit(
        command="order submit",
        event="risk_evaluated",
        account_tail="****7890",
        detail="passed=True",
        now=NOW,
    )
    records = store.recent_audit()
    assert len(records) == 1
    assert records[0].event == "risk_evaluated"
    assert records[0].detail == "passed=True"
