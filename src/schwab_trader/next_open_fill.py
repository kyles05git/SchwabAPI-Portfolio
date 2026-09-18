"""Deterministic paper fills at an exchange session's opening print.

A strategy that decides on session ``T``'s close cannot also *trade* on that close:
the close is the last print of the session, and acting on it is look-ahead. The
earliest instant a decision made from ``T``'s settled evidence can be executed is the
opening print of the next valid exchange session. This module models that fill.

Two things live here and nothing else:

* :func:`validate_opening_bar` — fail-closed validation of the exact opening interval
  bar for one symbol and one session, producing :class:`OpeningBarEvidence` only when
  every check passes. Missing, early-retrieved, ambiguous, duplicated, conflicting,
  wrong-session, or structurally invalid evidence yields reasons and **no** evidence.
* :class:`OpeningFillPolicy` and :func:`plan_buy` / :func:`plan_sell` — the arithmetic
  that turns an opening print into a whole-share paper fill with an explicit spread,
  slippage, and commission treatment.

There is deliberately no fallback. If the opening bar for the execution session is not
usable, the caller must refuse to execute; substituting the previous close, the last
quote, a neighbouring interval, or a partially observed bar would silently change the
experiment into the one it was created to replace. The exchange calendar is imported
from :mod:`schwab_trader.market_calendar`; no second calendar is defined here.

The module is pure: no I/O, no clock reads, no broker path, no persistence. Every
output carries symbols, sessions, timestamps, prices, and reason codes only.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator

from schwab_trader import market_calendar
from schwab_trader.market_data import Candle
from schwab_trader.models import OrderSide

#: Stable JSON/schema identity for the evidence and fill contracts in this module.
PAYLOAD_SCHEMA = "paper-next-open-fill/1"

#: The opening interval length. Five minutes matches the intraday evidence the
#: provider seam already validates (see :mod:`schwab_trader.market_bar_evidence`).
OPENING_INTERVAL_MINUTES = 5

#: Basis points per unit. Named so the arithmetic below reads as intent, not magic.
_BPS = Decimal(10_000)

#: Default price increment for a US-listed equity or ETF.
_DEFAULT_INCREMENT = Decimal("0.01")


class OpeningBarReason(StrEnum):
    """Why opening evidence cannot be used. Stable, sanitized, machine-readable."""

    CLOSED_SESSION = "closed_session"
    """The requested execution date is not an exchange trading session at all."""

    MISSING_BAR = "missing_bar"
    """No candle was supplied at the session's exact opening interval start."""

    DUPLICATE_BAR = "duplicate_bar"
    """More than one candle claims the opening interval. Even byte-identical copies
    fail closed: two rows for one interval means the upstream join is wrong, and
    picking either one is a guess about which feed is authoritative."""

    CONFLICTING_BAR = "conflicting_bar"
    """Several candles claim the opening interval with different values."""

    WRONG_SESSION = "wrong_session"
    """A candle carries a timestamp outside the requested session's regular hours."""

    WRONG_SYMBOL = "wrong_symbol"
    """A candle or persisted evidence record names a different symbol."""

    MALFORMED_EVIDENCE = "malformed_evidence"
    """Persisted evidence has inconsistent interval metadata, source, or timestamps."""

    INVALID_EVIDENCE_DIGEST = "invalid_evidence_digest"
    """The content-addressed digest does not reproduce from the evidence fields."""

    RETRIEVED_BEFORE_INTERVAL_CLOSE = "retrieved_before_interval_close"
    """The evidence was retrieved before the opening interval had finished printing,
    so the bar cannot yet be complete."""

    STALE_RETRIEVAL = "stale_retrieval"
    """The evidence was retrieved too long before the caller's reference instant to be
    trusted as the current view of the opening interval."""

    MISSING_OPEN = "missing_open"
    """The opening interval candle carries no opening price."""

    INVALID_OHLC = "invalid_ohlc"
    """Non-positive prices, or a high/low that does not bracket the open and close."""

    NEGATIVE_VOLUME = "negative_volume"


class OpeningFillReason(StrEnum):
    """Why a fill could not be planned from otherwise-valid opening evidence."""

    NON_POSITIVE_PRICE = "non_positive_price"
    """Spread and slippage reduced the effective price to zero or below."""

    INSUFFICIENT_BUDGET = "insufficient_budget"
    """The cash budget cannot fund even one whole share plus its costs."""

    INSUFFICIENT_QUANTITY = "insufficient_quantity"
    """A sell was requested for a non-positive whole-share quantity."""


class OpeningBarUnavailable(RuntimeError):
    """Raised when a caller demands opening evidence that failed validation."""

    def __init__(self, symbol: str, session: date, reasons: Sequence[OpeningBarReason]) -> None:
        self.symbol = symbol
        self.session = session
        self.reasons = tuple(reasons)
        codes = ", ".join(reason.value for reason in self.reasons) or "unknown"
        super().__init__(
            f"No usable opening bar for {symbol} on {session.isoformat()} [{codes}]."
        )


class OpeningFillUnavailable(RuntimeError):
    """Raised when opening evidence is valid but no whole-share fill is possible."""

    def __init__(self, symbol: str, reason: OpeningFillReason, detail: str) -> None:
        self.symbol = symbol
        self.reason = reason
        super().__init__(f"{symbol}: {detail}")


class OpeningBarEvidence(BaseModel):
    """The validated opening interval bar for one symbol and one exchange session."""

    model_config = ConfigDict(frozen=True)

    schema_name: str = PAYLOAD_SCHEMA
    symbol: str
    session_date: date
    exchange: str = market_calendar.EXCHANGE_MIC
    interval_minutes: int = OPENING_INTERVAL_MINUTES
    interval_start_at: datetime
    """Aware UTC instant the opening interval began (the session's official open)."""

    interval_end_at: datetime
    """Aware UTC instant the opening interval finished printing."""

    retrieved_at: datetime
    source: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    evidence_digest: str
    """SHA-256 over the canonical bar identity, so a persisted fill can be re-verified."""

    @property
    def reference_price(self) -> Decimal:
        """The opening print. The only price this methodology executes against."""
        return self.open


class OpeningBarDiagnostic(BaseModel):
    """The full, sanitized verdict for one symbol/session opening-evidence check."""

    model_config = ConfigDict(frozen=True)

    schema_name: str = PAYLOAD_SCHEMA
    symbol: str
    session_date: date
    exchange: str = market_calendar.EXCHANGE_MIC
    interval_minutes: int = OPENING_INTERVAL_MINUTES
    interval_start_at: datetime | None = None
    interval_end_at: datetime | None = None
    retrieved_at: datetime
    candidate_count: int = 0
    """How many supplied candles claimed the opening interval."""

    outside_session_count: int = 0
    usable: bool = False
    reasons: tuple[OpeningBarReason, ...] = ()
    evidence: OpeningBarEvidence | None = None

    def require(self) -> OpeningBarEvidence:
        """Return the evidence or fail closed. Never substitutes another price."""
        if self.evidence is None or not self.usable:
            raise OpeningBarUnavailable(self.symbol, self.session_date, self.reasons)
        return self.evidence


class OpeningFillPolicy(BaseModel):
    """Deterministic, fully declared cost assumptions for an opening fill.

    No quote exists at the opening print — the print *is* the auction result — so the
    spread is an assumption rather than an observation, and it is declared explicitly
    rather than hidden inside the fill arithmetic. ``half_spread_bps`` models paying
    half the touch; ``slippage_bps`` models the additional adverse move an order of
    this size experiences into the opening auction. Both are applied against the
    trader, in both directions.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_id: str = "next-open-fill-v1"
    half_spread_bps: Decimal = Decimal(2)
    slippage_bps: Decimal = Decimal(3)
    commission_per_share: Decimal = Decimal(0)
    commission_per_order: Decimal = Decimal(0)
    commission_minimum: Decimal = Decimal(0)
    whole_shares_only: bool = True
    price_increment: Decimal = Decimal("0.01")

    @field_validator(
        "half_spread_bps",
        "slippage_bps",
        "commission_per_share",
        "commission_per_order",
        "commission_minimum",
    )
    @classmethod
    def _non_negative(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("cost assumptions must not be negative")
        return value

    @field_validator("price_increment")
    @classmethod
    def _positive_increment(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("price_increment must be greater than zero")
        return value

    @field_validator("whole_shares_only")
    @classmethod
    def _require_whole_shares(cls, value: bool) -> bool:
        # Fractional paper shares would not correspond to anything the live order
        # path can submit, so the option exists only to be explicit about the rule.
        if not value:
            raise ValueError("this methodology models whole-share quantities only")
        return value

    @property
    def total_adverse_bps(self) -> Decimal:
        return self.half_spread_bps + self.slippage_bps

    def effective_price(self, side: OrderSide, reference: Decimal) -> Decimal:
        """The price one share fills at, rounded against the trader.

        Rounding direction is part of the model: a buy rounds up to the next
        increment and a sell rounds down, so no configuration of the spread can make
        the modeled fill better than the observed print. A consequence worth stating
        plainly is that any non-zero adjustment costs at least one whole increment,
        even when the bps figure implies less — nearest-cent rounding would otherwise
        hand back a fill better than what actually traded.
        """
        if reference <= 0:
            raise OpeningFillUnavailable(
                "", OpeningFillReason.NON_POSITIVE_PRICE, "opening reference price is not positive"
            )
        adjustment = self.total_adverse_bps / _BPS
        if side is OrderSide.BUY:
            raw = reference * (Decimal(1) + adjustment)
            rounding = ROUND_CEILING
        else:
            raw = reference * (Decimal(1) - adjustment)
            rounding = ROUND_FLOOR
        price = raw.quantize(self.price_increment, rounding=rounding)
        if price <= 0:
            raise OpeningFillUnavailable(
                "",
                OpeningFillReason.NON_POSITIVE_PRICE,
                "spread and slippage reduced the effective price to zero",
            )
        return price

    def commission(self, quantity: int) -> Decimal:
        """Total modeled commission for ``quantity`` shares. Never negative."""
        if quantity <= 0:
            return Decimal(0)
        charged = self.commission_per_order + self.commission_per_share * quantity
        return max(charged, self.commission_minimum)


class OpeningFill(BaseModel):
    """One fully costed, whole-share paper fill at the opening print."""

    model_config = ConfigDict(frozen=True)

    schema_name: str = PAYLOAD_SCHEMA
    policy_id: str
    symbol: str
    side: OrderSide
    session_date: date
    executed_at: datetime
    """The opening interval start — the instant this fill is modeled to occur."""

    reference_price: Decimal
    """The observed opening print, before any cost assumption."""

    effective_price: Decimal
    quantity: int
    gross_notional: Decimal
    """``effective_price * quantity``: what the shares cost or raised at the fill."""

    spread_and_slippage_cost: Decimal
    """The adverse difference from the observed print, in currency."""

    commission: Decimal
    cash_delta: Decimal
    """Signed change to settled paper cash: negative for a buy, positive for a sell."""

    evidence_digest: str


def _utc(value: datetime) -> datetime | None:
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


def _digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _opening_evidence_digest(
    *,
    exchange: str,
    symbol: str,
    session: date,
    interval_minutes: int,
    interval_start_at: datetime,
    source: str,
    open_: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    volume: int,
) -> str:
    """Reproduce the immutable identity of one accepted opening bar."""
    return _digest(
        {
            "schema": PAYLOAD_SCHEMA,
            "exchange": exchange,
            "symbol": symbol,
            "session": session.isoformat(),
            "interval_minutes": interval_minutes,
            "interval_start_at": interval_start_at.isoformat(),
            "source": source,
            "open": _decimal_text(open_),
            "high": _decimal_text(high),
            "low": _decimal_text(low),
            "close": _decimal_text(close),
            "volume": volume,
        }
    )


def opening_interval_utc(
    session: date,
    *,
    minutes: int = OPENING_INTERVAL_MINUTES,
    exchange: str = market_calendar.EXCHANGE_MIC,
) -> tuple[datetime, datetime]:
    """The ``(start, end)`` UTC instants of ``session``'s first regular-session bar.

    Delegates entirely to the canonical calendar, so weekends, holidays, early closes,
    and DST are decided in exactly one place. Raises :class:`ValueError` for a date the
    exchange was closed.
    """
    del exchange  # single-exchange calendar; accepted for call-site symmetry
    starts = market_calendar.session_interval_starts_utc(session, minutes=minutes)
    return starts[0], starts[0] + timedelta(minutes=minutes)


def _invalid_ohlc(candle: Candle) -> bool:
    if candle.open is None or candle.high is None or candle.low is None:
        return True
    prices = (candle.open, candle.high, candle.low, candle.close)
    if any(value <= 0 for value in prices):
        return True
    return (
        candle.high < candle.low
        or candle.high < max(candle.open, candle.close)
        or candle.low > min(candle.open, candle.close)
    )


def opening_evidence_reasons(
    evidence: OpeningBarEvidence,
    *,
    expected_symbol: str | None = None,
    expected_session: date | None = None,
    expected_minutes: int | None = None,
    expected_exchange: str | None = None,
) -> tuple[OpeningBarReason, ...]:
    """Re-validate a persisted evidence record before orchestration uses it.

    ``OpeningBarEvidence`` is a transport model, so callers can deserialize or even
    construct one without going through :func:`validate_opening_bar`. The cohort gate
    reproduces every structural invariant and the content digest instead of trusting
    the class name.
    """
    reasons: list[OpeningBarReason] = []
    symbol = evidence.symbol.strip().upper()
    wanted_symbol = None if expected_symbol is None else expected_symbol.strip().upper()
    if (
        not symbol
        or evidence.symbol != symbol
        or (wanted_symbol is not None and symbol != wanted_symbol)
    ):
        reasons.append(OpeningBarReason.WRONG_SYMBOL)

    session = evidence.session_date
    if expected_session is not None and session != expected_session:
        reasons.append(OpeningBarReason.WRONG_SESSION)
    minutes = evidence.interval_minutes
    exchange = evidence.exchange
    if (
        minutes <= 0
        or (expected_minutes is not None and minutes != expected_minutes)
        or not exchange.strip()
        or (expected_exchange is not None and exchange != expected_exchange)
        or not evidence.source.strip()
    ):
        reasons.append(OpeningBarReason.MALFORMED_EVIDENCE)

    start = _utc(evidence.interval_start_at)
    end = _utc(evidence.interval_end_at)
    retrieval = _utc(evidence.retrieved_at)
    expected_start: datetime | None
    expected_end: datetime | None
    try:
        expected_start, expected_end = opening_interval_utc(
            session, minutes=minutes, exchange=exchange
        )
    except (IndexError, ValueError):
        reasons.append(OpeningBarReason.CLOSED_SESSION)
        expected_start = expected_end = None
    if (
        start is None
        or end is None
        or retrieval is None
        or expected_start is None
        or start != expected_start
        or end != expected_end
    ):
        reasons.append(OpeningBarReason.MALFORMED_EVIDENCE)
    elif retrieval < end:
        reasons.append(OpeningBarReason.RETRIEVED_BEFORE_INTERVAL_CLOSE)

    if any(
        value <= 0 for value in (evidence.open, evidence.high, evidence.low, evidence.close)
    ) or (
        evidence.high < evidence.low
        or evidence.high < max(evidence.open, evidence.close)
        or evidence.low > min(evidence.open, evidence.close)
    ):
        reasons.append(OpeningBarReason.INVALID_OHLC)
    if evidence.volume < 0:
        reasons.append(OpeningBarReason.NEGATIVE_VOLUME)

    if start is not None:
        expected_digest = _opening_evidence_digest(
            exchange=exchange,
            symbol=symbol,
            session=session,
            interval_minutes=minutes,
            interval_start_at=start,
            source=evidence.source,
            open_=evidence.open,
            high=evidence.high,
            low=evidence.low,
            close=evidence.close,
            volume=evidence.volume,
        )
        if evidence.evidence_digest != expected_digest:
            reasons.append(OpeningBarReason.INVALID_EVIDENCE_DIGEST)
    return tuple(dict.fromkeys(reasons))


def _bar_identity(candle: Candle) -> tuple[str, str | None, str | None, str | None, str, int]:
    """Value identity of a candle, used to tell duplicates from conflicts."""
    return (
        candle.date.astimezone(UTC).isoformat(),
        None if candle.open is None else _decimal_text(candle.open),
        None if candle.high is None else _decimal_text(candle.high),
        None if candle.low is None else _decimal_text(candle.low),
        _decimal_text(candle.close),
        candle.volume,
    )


def validate_opening_bar(
    symbol: str,
    session: date,
    candles: Sequence[Candle],
    *,
    retrieved_at: datetime,
    minutes: int = OPENING_INTERVAL_MINUTES,
    as_of: datetime | None = None,
    max_retrieval_age: timedelta | None = None,
    exchange: str = market_calendar.EXCHANGE_MIC,
) -> OpeningBarDiagnostic:
    """Validate the exact opening interval bar, producing evidence only when clean.

    ``candles`` may contain the whole session; only the candle whose timestamp equals
    the session's opening interval start is considered the opening bar. Any candle
    outside the session's regular hours is reported as ``wrong_session`` rather than
    quietly ignored, because contaminated input is a reason to refuse, not to filter.

    ``as_of`` and ``max_retrieval_age`` are optional: supply both to reject evidence
    that was retrieved so long ago it may no longer describe the interval the caller
    is about to execute against.
    """
    normalized = symbol.strip().upper()
    retrieval = _utc(retrieved_at)
    if retrieval is None:
        raise ValueError("retrieved_at must include a timezone")

    try:
        interval_start, interval_end = opening_interval_utc(
            session, minutes=minutes, exchange=exchange
        )
        session_open, session_close = market_calendar.session_bounds_utc(session)
    except ValueError:
        return OpeningBarDiagnostic(
            symbol=normalized,
            session_date=session,
            exchange=exchange,
            interval_minutes=minutes,
            retrieved_at=retrieval,
            reasons=(OpeningBarReason.CLOSED_SESSION,),
        )

    timed = tuple(
        (found, candle) for candle in candles if (found := _utc(candle.date)) is not None
    )
    outside = tuple(
        found for found, _ in timed if found < session_open or found >= session_close
    )
    candidates = tuple(candle for found, candle in timed if found == interval_start)

    reasons: list[OpeningBarReason] = []
    if any(candle.symbol.strip().upper() != normalized for candle in candles):
        reasons.append(OpeningBarReason.WRONG_SYMBOL)
    if len(timed) != len(candles) or outside:
        reasons.append(OpeningBarReason.WRONG_SESSION)
    if retrieval < interval_end:
        reasons.append(OpeningBarReason.RETRIEVED_BEFORE_INTERVAL_CLOSE)
    if as_of is not None and max_retrieval_age is not None:
        reference = _utc(as_of)
        if reference is None:
            raise ValueError("as_of must include a timezone")
        if reference - retrieval > max_retrieval_age:
            reasons.append(OpeningBarReason.STALE_RETRIEVAL)

    if not candidates:
        reasons.append(OpeningBarReason.MISSING_BAR)
    elif len(candidates) > 1:
        identities = {_bar_identity(candle) for candle in candidates}
        reasons.append(
            OpeningBarReason.DUPLICATE_BAR
            if len(identities) == 1
            else OpeningBarReason.CONFLICTING_BAR
        )
    else:
        candle = candidates[0]
        if candle.open is None:
            reasons.append(OpeningBarReason.MISSING_OPEN)
        if _invalid_ohlc(candle):
            reasons.append(OpeningBarReason.INVALID_OHLC)
        if candle.volume < 0:
            reasons.append(OpeningBarReason.NEGATIVE_VOLUME)
        if not candle.source.strip():
            reasons.append(OpeningBarReason.MALFORMED_EVIDENCE)

    ordered = tuple(dict.fromkeys(reasons))
    base = {
        "symbol": normalized,
        "session_date": session,
        "exchange": exchange,
        "interval_minutes": minutes,
        "interval_start_at": interval_start,
        "interval_end_at": interval_end,
        "retrieved_at": retrieval,
        "candidate_count": len(candidates),
        "outside_session_count": len(outside),
    }
    if ordered:
        return OpeningBarDiagnostic.model_validate({**base, "reasons": ordered})

    bar = candidates[0]
    assert bar.open is not None and bar.high is not None and bar.low is not None
    evidence = OpeningBarEvidence(
        symbol=normalized,
        session_date=session,
        exchange=exchange,
        interval_minutes=minutes,
        interval_start_at=interval_start,
        interval_end_at=interval_end,
        retrieved_at=retrieval,
        source=bar.source,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        evidence_digest=_opening_evidence_digest(
            exchange=exchange,
            symbol=normalized,
            session=session,
            interval_minutes=minutes,
            interval_start_at=interval_start,
            source=bar.source,
            open_=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
        ),
    )
    return OpeningBarDiagnostic.model_validate(
        {**base, "usable": True, "evidence": evidence}
    )


def _fill(
    *,
    evidence: OpeningBarEvidence,
    policy: OpeningFillPolicy,
    side: OrderSide,
    quantity: int,
    effective_price: Decimal,
) -> OpeningFill:
    gross = effective_price * quantity
    commission = policy.commission(quantity)
    adverse = abs(effective_price - evidence.reference_price) * quantity
    cash_delta = -(gross + commission) if side is OrderSide.BUY else gross - commission
    return OpeningFill(
        policy_id=policy.policy_id,
        symbol=evidence.symbol,
        side=side,
        session_date=evidence.session_date,
        executed_at=evidence.interval_start_at,
        reference_price=evidence.reference_price,
        effective_price=effective_price,
        quantity=quantity,
        gross_notional=gross,
        spread_and_slippage_cost=adverse,
        commission=commission,
        cash_delta=cash_delta,
        evidence_digest=evidence.evidence_digest,
    )


def plan_buy(
    evidence: OpeningBarEvidence,
    *,
    cash_budget: Decimal,
    policy: OpeningFillPolicy,
    max_quantity: int | None = None,
) -> OpeningFill:
    """The largest whole-share buy ``cash_budget`` funds at the opening print.

    Commission is inside the budget, not on top of it: a sleeve cannot spend cash it
    does not have, so the quantity is reduced until price plus commission fits. The
    search starts from the un-commissioned bound and walks down, which terminates in
    at most a handful of steps for any realistic commission schedule.
    """
    price = policy.effective_price(OrderSide.BUY, evidence.reference_price)
    if cash_budget <= 0:
        raise OpeningFillUnavailable(
            evidence.symbol,
            OpeningFillReason.INSUFFICIENT_BUDGET,
            "no cash budget was available for an opening buy",
        )
    quantity = int((cash_budget / price).to_integral_value(rounding=ROUND_FLOOR))
    if max_quantity is not None:
        quantity = min(quantity, max_quantity)
    while quantity > 0 and price * quantity + policy.commission(quantity) > cash_budget:
        quantity -= 1
    if quantity <= 0:
        raise OpeningFillUnavailable(
            evidence.symbol,
            OpeningFillReason.INSUFFICIENT_BUDGET,
            (
                f"budget {cash_budget} cannot fund one whole share at {price} "
                "plus modeled commission"
            ),
        )
    return _fill(
        evidence=evidence,
        policy=policy,
        side=OrderSide.BUY,
        quantity=quantity,
        effective_price=price,
    )


def plan_sell(
    evidence: OpeningBarEvidence,
    *,
    quantity: int,
    policy: OpeningFillPolicy,
) -> OpeningFill:
    """A whole-share sell of ``quantity`` shares at the opening print."""
    if quantity <= 0:
        raise OpeningFillUnavailable(
            evidence.symbol,
            OpeningFillReason.INSUFFICIENT_QUANTITY,
            "an opening sell requires a positive whole-share quantity",
        )
    price = policy.effective_price(OrderSide.SELL, evidence.reference_price)
    return _fill(
        evidence=evidence,
        policy=policy,
        side=OrderSide.SELL,
        quantity=quantity,
        effective_price=price,
    )


def evidence_payload(diagnostic: OpeningBarDiagnostic) -> dict[str, object]:
    """Stable JSON primitives for diagnostics. No payloads, credentials, or accounts."""
    evidence = diagnostic.evidence
    return {
        "schema": diagnostic.schema_name,
        "symbol": diagnostic.symbol,
        "session": diagnostic.session_date.isoformat(),
        "exchange": diagnostic.exchange,
        "interval_minutes": diagnostic.interval_minutes,
        "interval_start_at": (
            None
            if diagnostic.interval_start_at is None
            else diagnostic.interval_start_at.isoformat()
        ),
        "interval_end_at": (
            None if diagnostic.interval_end_at is None else diagnostic.interval_end_at.isoformat()
        ),
        "retrieved_at": diagnostic.retrieved_at.isoformat(),
        "candidate_count": diagnostic.candidate_count,
        "outside_session_count": diagnostic.outside_session_count,
        "usable": diagnostic.usable,
        "reasons": [reason.value for reason in diagnostic.reasons],
        "evidence": (
            None
            if evidence is None
            else {
                "source": evidence.source,
                "open": str(evidence.open),
                "high": str(evidence.high),
                "low": str(evidence.low),
                "close": str(evidence.close),
                "volume": evidence.volume,
                "evidence_digest": evidence.evidence_digest,
            }
        ),
    }


__all__ = [
    "OPENING_INTERVAL_MINUTES",
    "PAYLOAD_SCHEMA",
    "OpeningBarDiagnostic",
    "OpeningBarEvidence",
    "OpeningBarReason",
    "OpeningBarUnavailable",
    "OpeningFill",
    "OpeningFillPolicy",
    "OpeningFillReason",
    "OpeningFillUnavailable",
    "evidence_payload",
    "opening_evidence_reasons",
    "opening_interval_utc",
    "plan_buy",
    "plan_sell",
    "validate_opening_bar",
]
