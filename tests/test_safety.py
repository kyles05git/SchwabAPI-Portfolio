"""Tests for the autonomous safety layer (offline)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from schwab_trader.safety import (
    KillSwitch,
    SafetyGate,
    SafetyLedger,
    SafetyLimits,
)

NOW = datetime(2026, 7, 16, 15, 0, tzinfo=UTC)


def test_kill_switch_engage_disengage(tmp_path) -> None:
    ks = KillSwitch(tmp_path / "KILL")
    assert not ks.is_engaged()
    ks.engage("manual halt")
    assert ks.is_engaged()
    status = ks.status()
    assert status.engaged and status.reason == "manual halt" and status.since is not None
    ks.disengage()
    assert not ks.is_engaged()
    ks.disengage()  # idempotent


def test_ledger_records_and_accumulates(tmp_path) -> None:
    ledger = SafetyLedger(tmp_path / "act.sqlite3")
    assert ledger.day(NOW).trades == 0
    ledger.record(trades=1, realized_pnl_delta=Decimal("-20"), now=NOW)
    ledger.record(trades=1, realized_pnl_delta=Decimal("5"), now=NOW)
    day = ledger.day(NOW)
    assert day.trades == 2
    assert day.realized_pnl == Decimal("-15")


def _gate(tmp_path, **limit_kwargs) -> tuple[SafetyGate, KillSwitch, SafetyLedger]:
    ks = KillSwitch(tmp_path / "KILL")
    ledger = SafetyLedger(tmp_path / "act.sqlite3")
    gate = SafetyGate(ks, ledger, SafetyLimits(**limit_kwargs))
    return gate, ks, ledger


def test_kill_switch_blocks_everything(tmp_path) -> None:
    gate, ks, _ = _gate(tmp_path)
    ks.engage("stop")
    decision = gate.check(order_notional=Decimal("10"), now=NOW)
    assert not decision.allowed and "kill switch" in decision.reason


def test_daily_trade_limit(tmp_path) -> None:
    gate, _, ledger = _gate(tmp_path, max_trades_per_day=2)
    ledger.record(trades=2, now=NOW)
    decision = gate.check(order_notional=Decimal("10"), now=NOW)
    assert not decision.allowed and "trade limit" in decision.reason


def test_daily_loss_limit(tmp_path) -> None:
    gate, _, ledger = _gate(tmp_path, daily_loss_limit=Decimal("50"))
    ledger.record(realized_pnl_delta=Decimal("-60"), now=NOW)  # already down $60
    decision = gate.check(order_notional=Decimal("10"), now=NOW)
    assert not decision.allowed and "loss limit" in decision.reason


def test_note_start_equity_is_idempotent(tmp_path) -> None:
    ledger = SafetyLedger(tmp_path / "act.sqlite3")
    # First observation of the day sets the opening equity; later ones don't move it.
    assert ledger.note_start_equity(Decimal("1000"), now=NOW) == Decimal("1000")
    assert ledger.note_start_equity(Decimal("880"), now=NOW) == Decimal("1000")
    assert ledger.day(NOW).start_equity == Decimal("1000")


def test_daily_loss_limit_is_mark_to_market(tmp_path) -> None:
    # Opening equity 1000; unrealized drawdown to 930 = -70 loss with zero realized P&L.
    # The realized-only check would pass; mark-to-market must block on the open loss.
    gate, _, ledger = _gate(tmp_path, daily_loss_limit=Decimal("50"))
    assert gate.check(order_notional=Decimal("10"), equity=Decimal("1000"), now=NOW).allowed
    decision = gate.check(order_notional=Decimal("10"), equity=Decimal("930"), now=NOW)
    assert not decision.allowed
    assert "loss limit" in decision.reason
    assert ledger.day(NOW).realized_pnl == Decimal("0")  # loss was purely unrealized


def test_loss_limit_breach_engages_kill_switch(tmp_path) -> None:
    # Breaching the mark-to-market loss limit halts ALL further trading, not just this
    # order - the kill switch trips and persists until a human resumes.
    gate, ks, _ = _gate(tmp_path, daily_loss_limit=Decimal("50"))
    gate.check(order_notional=Decimal("10"), equity=Decimal("1000"), now=NOW)  # opens at 1000
    assert not ks.is_engaged()
    breach = gate.check(order_notional=Decimal("10"), equity=Decimal("940"), now=NOW)  # -60
    assert not breach.allowed and "kill switch engaged" in breach.reason
    assert ks.is_engaged()
    # A later, recovered-equity order is still blocked because the switch stays engaged.
    later = gate.check(order_notional=Decimal("10"), equity=Decimal("1000"), now=NOW)
    assert not later.allowed and "kill switch" in later.reason


def test_mark_to_market_gain_does_not_block(tmp_path) -> None:
    gate, ks, _ = _gate(tmp_path, daily_loss_limit=Decimal("50"))
    gate.check(order_notional=Decimal("10"), equity=Decimal("1000"), now=NOW)
    up = gate.check(order_notional=Decimal("10"), equity=Decimal("1200"), now=NOW)
    assert up.allowed and not ks.is_engaged()


def test_order_notional_and_capital_caps(tmp_path) -> None:
    gate, _, _ = _gate(tmp_path, max_order_notional=Decimal("100"))
    assert not gate.check(order_notional=Decimal("150"), now=NOW).allowed  # per-order cap

    gate2, _, _ = _gate(tmp_path, capital_cap=Decimal("1000"))
    assert gate2.check(order_notional=Decimal("200"), deployed=Decimal("700"), now=NOW).allowed
    over = gate2.check(order_notional=Decimal("400"), deployed=Decimal("700"), now=NOW)
    assert not over.allowed and "capital cap" in over.reason


def test_allows_when_within_all_limits(tmp_path) -> None:
    gate, _, _ = _gate(
        tmp_path,
        max_trades_per_day=10,
        daily_loss_limit=Decimal("100"),
        max_order_notional=Decimal("500"),
        capital_cap=Decimal("5000"),
    )
    assert gate.check(order_notional=Decimal("200"), deployed=Decimal("1000"), now=NOW).allowed


def test_zero_limits_are_disabled(tmp_path) -> None:
    gate, _, ledger = _gate(tmp_path)  # all limits 0 = off
    ledger.record(trades=100, realized_pnl_delta=Decimal("-9999"), now=NOW)
    assert gate.check(order_notional=Decimal("1000000"), now=NOW).allowed
