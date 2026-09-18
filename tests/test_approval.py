"""Tests for the single-use, time-boxed order-approval token store (offline)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from schwab_trader.approval import ApprovalError, ApprovalStore
from schwab_trader.models import OrderRequest, OrderSide

_ACCOUNT = "ACCOUNTHASH1234"
_OTHER_ACCOUNT = "DIFFERENTHASH99"
_TTL = timedelta(minutes=60)


def _request(**overrides: object) -> OrderRequest:
    base: dict[str, object] = {
        "side": OrderSide.BUY,
        "symbol": "AAPL",
        "quantity": 1,
        "limit_price": Decimal("100.00"),
    }
    base.update(overrides)
    return OrderRequest(**base)  # type: ignore[arg-type]


def _store(tmp_path: Path) -> ApprovalStore:
    return ApprovalStore(tmp_path / "approvals.sqlite3")


def test_issue_and_consume_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    request = _request()
    token = store.issue(account_hash=_ACCOUNT, request=request, ttl=_TTL, rationale="momentum")

    record = store.consume(token=token, account_hash=_ACCOUNT)
    assert record.to_request() == request
    assert record.rationale == "momentum"
    assert record.account_tail == "****1234"


def test_replay_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    token = store.issue(account_hash=_ACCOUNT, request=_request(), ttl=_TTL)
    store.consume(token=token, account_hash=_ACCOUNT)  # first use OK
    with pytest.raises(ApprovalError, match="already used"):
        store.consume(token=token, account_hash=_ACCOUNT)  # replay blocked


def test_expired_token_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    issued = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    token = store.issue(
        account_hash=_ACCOUNT, request=_request(), ttl=timedelta(minutes=5), now=issued
    )
    later = issued + timedelta(minutes=6)
    with pytest.raises(ApprovalError, match="expired"):
        store.consume(token=token, account_hash=_ACCOUNT, now=later)


def test_unknown_token_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ApprovalError, match="unknown"):
        store.consume(token="not-a-real-token", account_hash=_ACCOUNT)


def test_wrong_account_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    token = store.issue(account_hash=_ACCOUNT, request=_request(), ttl=_TTL)
    with pytest.raises(ApprovalError, match="match"):
        store.consume(token=token, account_hash=_OTHER_ACCOUNT)
    # A rejected consume must not burn the token: the right account still works.
    assert store.consume(token=token, account_hash=_ACCOUNT).symbol == "AAPL"


def test_tampered_order_breaks_fingerprint(tmp_path: Path) -> None:
    """Editing a stored order field so it no longer matches its fingerprint is caught."""
    store = _store(tmp_path)
    token = store.issue(account_hash=_ACCOUNT, request=_request(quantity=1), ttl=_TTL)
    # Simulate tampering: bump the stored quantity without recomputing the fingerprint.
    with store._connect() as conn:  # white-box test of the fingerprint guard
        conn.execute("UPDATE approvals SET quantity = 99 WHERE token = ?", (token,))
    with pytest.raises(ApprovalError, match="match"):
        store.consume(token=token, account_hash=_ACCOUNT)


def test_pending_excludes_consumed_and_expired(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    live = store.issue(account_hash=_ACCOUNT, request=_request(symbol="MSFT"), ttl=_TTL, now=now)
    consumed = store.issue(
        account_hash=_ACCOUNT, request=_request(symbol="NVDA"), ttl=_TTL, now=now
    )
    store.issue(
        account_hash=_ACCOUNT,
        request=_request(symbol="AMD"),
        ttl=timedelta(minutes=1),
        now=now,
    )
    store.consume(token=consumed, account_hash=_ACCOUNT, now=now)

    later = now + timedelta(minutes=30)  # the 1-min token has expired by now
    pending = store.pending(now=later)
    assert [r.token for r in pending] == [live]


def test_sweep_expired_removes_only_expired_pending(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    store.issue(
        account_hash=_ACCOUNT, request=_request(symbol="AMD"), ttl=timedelta(minutes=1), now=now
    )
    keep = store.issue(account_hash=_ACCOUNT, request=_request(symbol="MSFT"), ttl=_TTL, now=now)

    removed = store.sweep_expired(now=now + timedelta(minutes=30))
    assert removed == 1
    assert store.get(keep) is not None
