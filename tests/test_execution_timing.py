"""Signal session versus execution session, and the methodology registry.

Pure and offline: synthetic exchange sessions only, no I/O, no clock reads.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from schwab_trader import execution_timing as et
from schwab_trader import market_calendar as mc
from schwab_trader.market_data import Candle
from schwab_trader.next_open_fill import (
    OpeningBarEvidence,
    OpeningFillPolicy,
    validate_opening_bar,
)

NEXT_OPEN = et.SIGNAL_T_CLOSE_EXECUTE_T1_OPEN_V1
CLOSE = et.MARK_TO_CLOSE_V1


def _evidence(symbol: str, session: date, price: str = "100.00") -> OpeningBarEvidence:
    start = datetime.combine(session, mc.MARKET_OPEN)
    opened = mc.eastern_to_utc(start)
    candle = Candle(
        symbol=symbol,
        date=opened,
        open=Decimal(price),
        high=Decimal(price),
        low=Decimal(price),
        close=Decimal(price),
        volume=1,
        source="test-intraday",
    )
    return validate_opening_bar(
        symbol,
        session,
        [candle],
        retrieved_at=opened + timedelta(minutes=10),
    ).require()


# --- session pairing ----------------------------------------------------------


@pytest.mark.parametrize(
    ("signal", "execution", "why"),
    [
        (date(2026, 7, 20), date(2026, 7, 21), "consecutive weekdays"),
        (date(2026, 7, 24), date(2026, 7, 27), "Friday signal executes Monday"),
        (date(2026, 7, 2), date(2026, 7, 6), "skips the July 3 holiday and the weekend"),
        (date(2026, 12, 23), date(2026, 12, 24), "executes into an early-close session"),
        (date(2026, 12, 24), date(2026, 12, 28), "early-close signal executes the next Monday"),
    ],
)
def test_execution_session_is_the_next_valid_exchange_session(signal, execution, why):
    plan = et.plan_sessions(signal, methodology=NEXT_OPEN)
    assert plan.signal_session_date == signal, why
    assert plan.execution_session_date == execution, why
    assert mc.is_trading_day(execution)


def test_the_close_marked_methodology_keeps_one_session():
    plan = et.plan_sessions(date(2026, 7, 20), methodology=CLOSE)
    assert plan.signal_session_date == plan.execution_session_date == date(2026, 7, 20)
    assert plan.decision_et == plan.execution_et == plan.valuation_et
    assert plan.opening_interval_utc is None


@pytest.mark.parametrize("closed", [date(2026, 7, 25), date(2026, 7, 3)])
def test_a_closed_date_cannot_be_a_signal_session(closed):
    with pytest.raises(et.ClosedSessionError):
        et.plan_sessions(closed, methodology=NEXT_OPEN)


# --- the four instants --------------------------------------------------------


def test_the_four_instants_are_separate_and_ordered():
    plan = et.plan_sessions(date(2026, 7, 24), methodology=NEXT_OPEN)
    assert plan.signal_utc == plan.decision_utc == datetime(2026, 7, 24, 20, 0, tzinfo=UTC)
    assert plan.execution_utc == plan.valuation_utc == datetime(2026, 7, 27, 13, 30, tzinfo=UTC)
    assert plan.decision_utc < plan.execution_utc
    stamps = plan.timestamps()
    assert set(stamps) == {"signal_time", "decision_time", "execution_time", "valuation_time"}
    assert stamps["decision_time"] != stamps["execution_time"]


def test_an_early_close_signal_session_decides_at_thirteen_hundred():
    plan = et.plan_sessions(date(2026, 12, 24), methodology=NEXT_OPEN)
    # 13:00 ET in December is 18:00 UTC; the next open is the ordinary 09:30.
    assert plan.decision_utc == datetime(2026, 12, 24, 18, 0, tzinfo=UTC)
    assert plan.execution_utc == datetime(2026, 12, 28, 14, 30, tzinfo=UTC)


def test_an_early_close_execution_session_still_fills_at_the_ordinary_open():
    plan = et.plan_sessions(date(2026, 12, 23), methodology=NEXT_OPEN)
    assert plan.execution_session.is_early_close is True
    assert plan.execution_utc == datetime(2026, 12, 24, 14, 30, tzinfo=UTC)


def test_evidence_is_ready_only_once_the_opening_interval_has_finished():
    plan = et.plan_sessions(date(2026, 7, 24), methodology=NEXT_OPEN)
    interval = plan.opening_interval_utc
    assert interval is not None
    assert plan.evidence_ready_at_utc == interval[1] > plan.execution_utc


def test_the_wait_fits_inside_the_schedulers_existing_deadline():
    """A next-open cohort introduces no second timer.

    The signal session's catch-up deadline is the next session's due time. The
    opening interval finishes hours before that, so the ordinary retryable
    ``awaiting-data`` wait always has room to resolve.
    """
    from schwab_trader import scheduling

    signal = date(2026, 7, 24)
    plan = et.plan_sessions(signal, methodology=NEXT_OPEN)
    deadline_et = scheduling.execution_deadline_et(signal)
    assert deadline_et is not None
    assert plan.evidence_ready_at_utc < mc.eastern_to_utc(deadline_et)


# --- methodology identity -----------------------------------------------------


def test_both_methodologies_are_registered_with_distinct_hashes():
    assert et.METHODOLOGIES[et.DEFAULT_METHODOLOGY_KEY] is CLOSE
    assert et.METHODOLOGIES[et.NEXT_OPEN_METHODOLOGY_KEY] is NEXT_OPEN
    assert CLOSE.methodology_hash != NEXT_OPEN.methodology_hash
    assert len(NEXT_OPEN.methodology_hash) == 64


def test_the_methodology_hash_is_derived_and_verified_not_supplied():
    payload = NEXT_OPEN.model_dump()
    payload["methodology_hash"] = "0" * 64
    with pytest.raises(ValueError, match="does not match"):
        et.ExecutionMethodology.model_validate(payload)


def test_the_hash_moves_when_a_cost_assumption_moves():
    altered = et.ExecutionMethodology(
        methodology_id=NEXT_OPEN.methodology_id,
        methodology_version="v-test",
        timing=NEXT_OPEN.timing,
        signal_evidence=NEXT_OPEN.signal_evidence,
        execution_reference=NEXT_OPEN.execution_reference,
        valuation_reference=NEXT_OPEN.valuation_reference,
        fill_policy=OpeningFillPolicy(slippage_bps=Decimal(99)),
    )
    assert altered.methodology_hash != NEXT_OPEN.methodology_hash


def test_a_next_open_methodology_must_declare_its_fill_policy():
    with pytest.raises(ValueError, match="must declare its fill policy"):
        et.ExecutionMethodology(
            methodology_id="broken",
            methodology_version="v1",
            timing=et.ExecutionTiming.NEXT_SESSION_OPEN,
            signal_evidence="session-close",
            execution_reference="next-session-open",
            valuation_reference="next-session-open",
            fill_policy=None,
        )


@pytest.mark.parametrize("absent", ["", "   ", None])
def test_an_absent_methodology_resolves_to_the_close_marked_model(absent):
    """Every record written before #79 ran that model, so this states a fact."""
    assert et.resolve_methodology(absent) is CLOSE


def test_an_unknown_methodology_fails_closed_rather_than_defaulting():
    with pytest.raises(et.UnknownMethodologyError, match="not a registered"):
        et.resolve_methodology("signal-t-close-execute-t1-open/v99")


# --- the frozen-cohort guard --------------------------------------------------


@pytest.mark.parametrize("cohort", sorted(et.PROTECTED_LEGACY_COHORTS))
def test_new_timing_cannot_be_applied_to_an_existing_official_cohort(cohort):
    with pytest.raises(et.ProtectedCohortError, match="cannot be changed"):
        et.ensure_methodology_allowed(cohort, NEXT_OPEN)


@pytest.mark.parametrize("cohort", sorted(et.PROTECTED_LEGACY_COHORTS))
def test_the_frozen_cohorts_keep_running_their_own_methodology(cohort):
    et.ensure_methodology_allowed(cohort, CLOSE)


def test_a_new_cohort_may_use_the_new_methodology():
    et.ensure_methodology_allowed("paper-t1-open-2026-08-03", NEXT_OPEN)


def test_the_protected_set_names_exactly_the_july_cohorts():
    assert et.PROTECTED_LEGACY_COHORTS == {
        "paper-first-2026-07-27",
        "paper-first-2026-07-28",
    }


# --- opening-evidence assessment ----------------------------------------------


def test_full_coverage_of_the_execution_session_is_ready():
    session = date(2026, 7, 27)
    result = et.assess_opening_evidence(
        ["AAA", "BBB"],
        {"AAA": _evidence("AAA", session), "BBB": _evidence("BBB", session)},
        execution_session=session,
    )
    assert result.ready is True
    assert result.reason == "ok"


def test_a_missing_symbol_blocks_and_is_named():
    session = date(2026, 7, 27)
    result = et.assess_opening_evidence(
        ["AAA", "BBB"],
        {"AAA": _evidence("AAA", session)},
        execution_session=session,
    )
    assert result.ready is False
    assert result.reason == "missing_keys"
    assert result.missing_symbols == ("BBB",)


def test_evidence_for_the_wrong_session_is_never_silently_accepted():
    result = et.assess_opening_evidence(
        ["AAA"],
        {"AAA": _evidence("AAA", date(2026, 7, 21))},
        execution_session=date(2026, 7, 27),
    )
    assert result.ready is False
    assert result.reason == "session_not_covered"
    assert result.mismatched_symbols == ("AAA",)


def test_symbol_matching_is_case_normalized_but_nothing_else_is():
    session = date(2026, 7, 27)
    result = et.assess_opening_evidence(
        ["aaa"], {"AAA": _evidence("AAA", session)}, execution_session=session
    )
    assert result.ready is True
    assert result.required_symbols == ("AAA",)


def test_a_mismatch_outranks_a_missing_symbol_in_the_reason_code():
    session = date(2026, 7, 27)
    result = et.assess_opening_evidence(
        ["AAA", "BBB"],
        {"AAA": _evidence("AAA", date(2026, 7, 21))},
        execution_session=session,
    )
    assert result.reason == "session_not_covered"
    assert result.unready_symbols == ("AAA", "BBB")


def test_case_colliding_evidence_keys_are_ambiguous_not_deduplicated():
    session = date(2026, 7, 27)
    evidence = _evidence("AAA", session)
    result = et.assess_opening_evidence(
        ["AAA"],
        {"AAA": evidence, "aaa": evidence},
        execution_session=session,
    )
    assert result.ready is False
    assert result.reason == "ambiguous_keys"
    assert result.ambiguous_symbols == ("AAA",)


def test_a_tampered_evidence_digest_is_invalid_even_when_the_session_matches():
    session = date(2026, 7, 27)
    tampered = _evidence("AAA", session).model_copy(update={"evidence_digest": "0" * 64})
    result = et.assess_opening_evidence(["AAA"], {"AAA": tampered}, execution_session=session)
    assert result.ready is False
    assert result.reason == "invalid_evidence"
    assert result.invalid_symbols == ("AAA",)


def test_a_mapping_key_cannot_relabel_another_symbols_evidence():
    session = date(2026, 7, 27)
    result = et.assess_opening_evidence(
        ["AAA"], {"AAA": _evidence("BBB", session)}, execution_session=session
    )
    assert result.reason == "invalid_evidence"
    assert result.invalid_symbols == ("AAA",)


def test_the_plan_payload_is_json_primitive_and_secret_free():
    payload = et.plan_sessions(date(2026, 7, 24), methodology=NEXT_OPEN).payload()
    assert payload["signal_session"] == "XNYS:2026-07-24"
    assert payload["execution_session"] == "XNYS:2026-07-27"
    assert all(isinstance(value, (str, bool, int, type(None))) for value in payload.values())
    flat = repr(payload).lower()
    for forbidden in ("token", "password", "postgres://", "authorization", "account"):
        assert forbidden not in flat
