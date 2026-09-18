"""Opening-bar evidence and next-open fill arithmetic.

Every fixture is synthetic. No network, database, clock read, or broker path is
involved, and no test can reach `.env`, Schwab, Neon, or an order operation.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from schwab_trader import market_calendar as mc
from schwab_trader.market_data import Candle
from schwab_trader.models import OrderSide
from schwab_trader.next_open_fill import (
    OpeningBarReason,
    OpeningBarUnavailable,
    OpeningFillPolicy,
    OpeningFillReason,
    OpeningFillUnavailable,
    evidence_payload,
    opening_interval_utc,
    plan_buy,
    plan_sell,
    validate_opening_bar,
)

#: A Monday. Its next-open execution follows the Friday 2026-07-24 signal session.
SESSION = date(2026, 7, 27)
OPEN_UTC = datetime(2026, 7, 27, 13, 30, tzinfo=UTC)
AFTER_INTERVAL = datetime(2026, 7, 27, 13, 40, tzinfo=UTC)

#: An early-close session (13:00 ET). The auction still opens at 09:30 ET.
EARLY_CLOSE_SESSION = date(2026, 12, 24)


def _bar(
    *,
    at: datetime = OPEN_UTC,
    symbol: str = "AAA",
    open_: str | None = "100.00",
    high: str | None = "101.00",
    low: str | None = "99.00",
    close: str = "100.50",
    volume: int = 5_000,
    source: str = "test-intraday",
) -> Candle:
    return Candle(
        symbol=symbol,
        date=at,
        open=None if open_ is None else Decimal(open_),
        high=None if high is None else Decimal(high),
        low=None if low is None else Decimal(low),
        close=Decimal(close),
        volume=volume,
        source=source,
    )


def _validate(candles, **kwargs):
    params = {"retrieved_at": AFTER_INTERVAL, **kwargs}
    return validate_opening_bar("aaa", kwargs.pop("session", SESSION), candles, **params)


def _evidence(**kwargs):
    return _validate([_bar(**kwargs)]).require()


# --- opening-interval identity ------------------------------------------------


def test_opening_interval_comes_from_the_canonical_calendar():
    start, end = opening_interval_utc(SESSION)
    assert start == OPEN_UTC
    assert end - start == timedelta(minutes=5)
    assert (start, mc.session_bounds_utc(SESSION)[0]) == (OPEN_UTC, OPEN_UTC)


def test_early_close_session_still_opens_at_the_ordinary_auction():
    start, end = opening_interval_utc(EARLY_CLOSE_SESSION)
    assert mc.is_early_close(EARLY_CLOSE_SESSION)
    # 09:30 ET in December is 14:30 UTC (EST). An early close shortens the afternoon.
    assert start == datetime(2026, 12, 24, 14, 30, tzinfo=UTC)
    assert end == datetime(2026, 12, 24, 14, 35, tzinfo=UTC)


@pytest.mark.parametrize("closed", [date(2026, 7, 25), date(2026, 7, 3)])
def test_weekend_and_holiday_have_no_opening_interval(closed):
    with pytest.raises(ValueError):
        opening_interval_utc(closed)
    result = validate_opening_bar("AAA", closed, [], retrieved_at=AFTER_INTERVAL)
    assert result.usable is False
    assert result.reasons == (OpeningBarReason.CLOSED_SESSION,)


# --- fail-closed validation ---------------------------------------------------


def test_a_clean_opening_bar_produces_reproducible_evidence():
    result = _validate([_bar()])
    assert result.usable is True
    assert result.reasons == ()
    evidence = result.require()
    assert evidence.symbol == "AAA"
    assert evidence.session_date == SESSION
    assert evidence.reference_price == Decimal("100.00")
    assert evidence.interval_start_at == OPEN_UTC
    # The digest is a pure function of the bar, so revalidating reproduces it exactly.
    assert _validate([_bar()]).require().evidence_digest == evidence.evidence_digest
    assert _validate([_bar(close="100.75")]).require().evidence_digest != (evidence.evidence_digest)


def test_a_missing_opening_bar_never_falls_back_to_a_later_interval():
    later = _bar(at=OPEN_UTC + timedelta(minutes=5))
    result = _validate([later])
    assert result.usable is False
    assert OpeningBarReason.MISSING_BAR in result.reasons
    assert result.evidence is None
    with pytest.raises(OpeningBarUnavailable, match="No usable opening bar"):
        result.require()


def test_identical_duplicate_bars_still_fail_closed():
    result = _validate([_bar(), _bar()])
    assert result.usable is False
    assert OpeningBarReason.DUPLICATE_BAR in result.reasons


def test_conflicting_bars_are_distinguished_from_duplicates():
    result = _validate([_bar(), _bar(close="100.75")])
    assert result.usable is False
    assert OpeningBarReason.CONFLICTING_BAR in result.reasons
    assert OpeningBarReason.DUPLICATE_BAR not in result.reasons


def test_evidence_retrieved_before_the_interval_finished_is_refused():
    result = _validate([_bar()], retrieved_at=OPEN_UTC + timedelta(minutes=2))
    assert result.usable is False
    assert OpeningBarReason.RETRIEVED_BEFORE_INTERVAL_CLOSE in result.reasons


def test_evidence_retrieved_exactly_at_the_interval_close_is_accepted():
    result = _validate([_bar()], retrieved_at=OPEN_UTC + timedelta(minutes=5))
    assert result.usable is True


def test_evidence_older_than_the_declared_allowance_is_stale():
    result = _validate(
        [_bar()],
        as_of=AFTER_INTERVAL + timedelta(hours=6),
        max_retrieval_age=timedelta(hours=1),
    )
    assert result.usable is False
    assert OpeningBarReason.STALE_RETRIEVAL in result.reasons


def test_extended_hours_contamination_is_reported_not_filtered():
    pre_market = _bar(at=OPEN_UTC - timedelta(minutes=30))
    result = _validate([pre_market, _bar()])
    assert result.usable is False
    assert OpeningBarReason.WRONG_SESSION in result.reasons
    assert result.outside_session_count == 1


def test_a_different_symbols_bar_cannot_be_relabelled_by_the_caller():
    result = _validate([_bar(symbol="BBB")])
    assert result.usable is False
    assert OpeningBarReason.WRONG_SYMBOL in result.reasons


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"open_": None}, OpeningBarReason.MISSING_OPEN),
        ({"high": "98.00"}, OpeningBarReason.INVALID_OHLC),
        ({"low": "101.50"}, OpeningBarReason.INVALID_OHLC),
        ({"open_": "0.00"}, OpeningBarReason.INVALID_OHLC),
        ({"volume": -1}, OpeningBarReason.NEGATIVE_VOLUME),
        ({"source": ""}, OpeningBarReason.MALFORMED_EVIDENCE),
    ],
)
def test_structurally_invalid_bars_are_refused(kwargs, reason):
    result = _validate([_bar(**kwargs)])
    assert result.usable is False
    assert reason in result.reasons


def test_a_naive_retrieval_timestamp_is_rejected_outright():
    with pytest.raises(ValueError, match="must include a timezone"):
        validate_opening_bar("AAA", SESSION, [_bar()], retrieved_at=datetime(2026, 7, 27, 13, 40))


def test_diagnostic_payload_is_json_primitive_and_secret_free():
    payload = evidence_payload(_validate([_bar()]))
    assert payload["symbol"] == "AAA"
    assert payload["usable"] is True
    assert payload["reasons"] == []
    assert isinstance(payload["evidence"], dict)
    flat = repr(payload).lower()
    for forbidden in ("token", "password", "postgres://", "authorization", "account"):
        assert forbidden not in flat


# --- fill arithmetic ----------------------------------------------------------


def test_costs_always_move_against_the_trader():
    policy = OpeningFillPolicy(half_spread_bps=Decimal(10), slippage_bps=Decimal(10))
    evidence = _evidence()
    buy = plan_buy(evidence, cash_budget=Decimal("10000"), policy=policy)
    sell = plan_sell(evidence, quantity=10, policy=policy)
    # 20 bps on 100.00 is 0.20, rounded away from the trader in both directions.
    assert buy.effective_price == Decimal("100.20")
    assert sell.effective_price == Decimal("99.80")
    assert buy.reference_price == sell.reference_price == Decimal("100.00")
    assert buy.effective_price > evidence.reference_price > sell.effective_price


def test_rounding_never_flatters_the_fill():
    """Any sub-cent adverse move costs a whole increment, in both directions.

    Nearest-cent rounding would hand back a fill better than the observed print
    whenever the modeled adjustment lands below half a cent, which is precisely the
    case a small ``bps`` setting produces. Ceiling for buys and floor for sells means
    the modeled fill is never better than what actually traded.
    """
    policy = OpeningFillPolicy(half_spread_bps=Decimal(0), slippage_bps=Decimal(1))
    evidence = _evidence(open_="100.02", high="101.00", low="99.00")
    buy = plan_buy(evidence, cash_budget=Decimal("1000"), policy=policy)
    sell = plan_sell(evidence, quantity=1, policy=policy)
    assert buy.effective_price == Decimal("100.04")  # 100.030002 -> ceiling
    assert sell.effective_price == Decimal("100.00")  # 100.009998 -> floor


def test_buys_are_whole_share_and_bounded_by_the_budget():
    policy = OpeningFillPolicy(half_spread_bps=Decimal(0), slippage_bps=Decimal(0))
    fill = plan_buy(_evidence(), cash_budget=Decimal("1050.00"), policy=policy)
    assert fill.quantity == 10
    assert fill.gross_notional == Decimal("1000.00")
    assert fill.cash_delta == Decimal("-1000.00")
    assert fill.side is OrderSide.BUY
    assert fill.executed_at == OPEN_UTC


def test_a_max_quantity_caps_the_buy_without_changing_the_price():
    policy = OpeningFillPolicy(half_spread_bps=Decimal(0), slippage_bps=Decimal(0))
    fill = plan_buy(_evidence(), cash_budget=Decimal("10000"), policy=policy, max_quantity=3)
    assert fill.quantity == 3
    assert fill.effective_price == Decimal("100.00")


def test_commission_is_funded_from_the_budget_not_added_on_top():
    policy = OpeningFillPolicy(
        half_spread_bps=Decimal(0),
        slippage_bps=Decimal(0),
        commission_per_share=Decimal("1.00"),
    )
    fill = plan_buy(_evidence(), cash_budget=Decimal("1000.00"), policy=policy)
    # 10 shares would cost 1000 + 10 commission, so the fill steps down to 9.
    assert fill.quantity == 9
    assert fill.commission == Decimal("9.00")
    assert -fill.cash_delta <= Decimal("1000.00")


def test_a_commission_minimum_is_honored():
    policy = OpeningFillPolicy(
        commission_per_share=Decimal("0.01"), commission_minimum=Decimal("5.00")
    )
    fill = plan_buy(_evidence(), cash_budget=Decimal("500"), policy=policy)
    assert fill.commission == Decimal("5.00")


def test_a_budget_too_small_for_one_share_fails_closed():
    with pytest.raises(OpeningFillUnavailable) as excinfo:
        plan_buy(_evidence(), cash_budget=Decimal("50.00"), policy=OpeningFillPolicy())
    assert excinfo.value.reason is OpeningFillReason.INSUFFICIENT_BUDGET


@pytest.mark.parametrize("budget", [Decimal(0), Decimal("-1")])
def test_a_non_positive_budget_fails_closed(budget):
    with pytest.raises(OpeningFillUnavailable):
        plan_buy(_evidence(), cash_budget=budget, policy=OpeningFillPolicy())


@pytest.mark.parametrize("quantity", [0, -5])
def test_a_non_positive_sell_quantity_fails_closed(quantity):
    with pytest.raises(OpeningFillUnavailable) as excinfo:
        plan_sell(_evidence(), quantity=quantity, policy=OpeningFillPolicy())
    assert excinfo.value.reason is OpeningFillReason.INSUFFICIENT_QUANTITY


def test_sell_proceeds_are_net_of_commission():
    policy = OpeningFillPolicy(
        half_spread_bps=Decimal(0),
        slippage_bps=Decimal(0),
        commission_per_order=Decimal("2.00"),
    )
    fill = plan_sell(_evidence(), quantity=4, policy=policy)
    assert fill.gross_notional == Decimal("400.00")
    assert fill.cash_delta == Decimal("398.00")


def test_spread_and_slippage_cost_is_reported_separately_from_commission():
    policy = OpeningFillPolicy(half_spread_bps=Decimal(50), slippage_bps=Decimal(0))
    fill = plan_buy(_evidence(), cash_budget=Decimal("1005.00"), policy=policy)
    assert fill.effective_price == Decimal("100.50")
    assert fill.spread_and_slippage_cost == Decimal("0.50") * fill.quantity
    assert fill.commission == Decimal(0)


def test_negative_cost_assumptions_are_rejected():
    with pytest.raises(ValueError, match="must not be negative"):
        OpeningFillPolicy(slippage_bps=Decimal(-1))


def test_fractional_shares_cannot_be_configured():
    with pytest.raises(ValueError, match="whole-share"):
        OpeningFillPolicy(whole_shares_only=False)


def test_a_cost_assumption_that_erases_the_price_fails_closed():
    policy = OpeningFillPolicy(half_spread_bps=Decimal(10_000), slippage_bps=Decimal(0))
    with pytest.raises(OpeningFillUnavailable) as excinfo:
        plan_sell(_evidence(), quantity=1, policy=policy)
    assert excinfo.value.reason is OpeningFillReason.NON_POSITIVE_PRICE
