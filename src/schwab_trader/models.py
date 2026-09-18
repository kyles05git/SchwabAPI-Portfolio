"""Typed order models and the Schwab order-payload builder.

Phase 7 scope: U.S.-listed **equity/ETF** orders only, and only BUY/SELL,
LIMIT, DAY, the NORMAL session, and whole-share quantities. Options, short
selling, margin, extended hours, and complex/multi-leg orders are intentionally
excluded until explicitly requested with their own models, validation, and tests.

The three order states are modeled as distinct types so they cannot be confused:

1. :class:`OrderRequest` - an unvalidated, structurally-typed request (from a
   person or the agent). Enforces shape only, not policy (policy = risk checks).
2. :class:`ValidatedOrderIntent` - produced by the risk engine once every check
   passes; the only thing allowed to be submitted.
3. :class:`SubmittedOrder` - the sanitized result of a submission attempt.

No network calls happen here.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

_SYMBOL_RE = re.compile(r"^[A-Z][A-Z.]{0,9}$")


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    LIMIT = "LIMIT"


class OrderDuration(StrEnum):
    DAY = "DAY"


class OrderSession(StrEnum):
    NORMAL = "NORMAL"


class AssetType(StrEnum):
    EQUITY = "EQUITY"


class OrderStatus(StrEnum):
    """Subset of Schwab order statuses relevant to this app, plus UNKNOWN."""

    NEW = "NEW"
    AWAITING_MANUAL_REVIEW = "AWAITING_MANUAL_REVIEW"
    ACCEPTED = "ACCEPTED"
    PENDING_ACTIVATION = "PENDING_ACTIVATION"
    QUEUED = "QUEUED"
    WORKING = "WORKING"
    PENDING_CANCEL = "PENDING_CANCEL"
    PENDING_REPLACE = "PENDING_REPLACE"
    REPLACED = "REPLACED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def from_api(cls, value: Any) -> OrderStatus:
        """Parse a status string tolerantly, defaulting to UNKNOWN."""
        try:
            return cls(str(value).upper())
        except ValueError:
            return cls.UNKNOWN


class OrderRequest(BaseModel):
    """An unvalidated, structurally-typed order request.

    Validates *shape* only (types, positive quantity/price, symbol syntax, at
    most two decimal places). Policy limits (max quantity/notional, allow-list,
    sufficient cash, position size) are enforced separately by the risk engine.
    """

    model_config = ConfigDict(frozen=True)

    side: OrderSide
    symbol: str
    quantity: int = Field(gt=0)
    limit_price: Decimal = Field(gt=0)
    order_type: OrderType = OrderType.LIMIT
    session: OrderSession = OrderSession.NORMAL
    duration: OrderDuration = OrderDuration.DAY
    asset_type: AssetType = AssetType.EQUITY

    @field_validator("symbol", mode="before")
    @classmethod
    def _normalize_symbol(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        if not _SYMBOL_RE.match(value):
            msg = f"'{value}' is not a syntactically valid U.S. equity symbol."
            raise ValueError(msg)
        return value

    @field_validator("limit_price")
    @classmethod
    def _validate_price_precision(cls, value: Decimal) -> Decimal:
        exponent = value.as_tuple().exponent  # int for finite values
        if not isinstance(exponent, int) or -exponent > 2:
            msg = "limit_price must be a finite value with at most 2 decimal places."
            raise ValueError(msg)
        return value

    @property
    def estimated_notional(self) -> Decimal:
        """Worst-case notional for this order (limit price x quantity)."""
        return self.limit_price * self.quantity

    @property
    def confirmation_phrase(self) -> str:
        """The exact phrase the user must type to confirm a live order."""
        return f"{self.side.value} {self.quantity} {self.symbol} AT {self.limit_price:.2f}"

    def describe(self) -> str:
        """A one-line human-readable summary."""
        return (
            f"{self.side.value} {self.quantity} {self.symbol} "
            f"{self.order_type.value} {self.limit_price:.2f} "
            f"{self.session.value}/{self.duration.value}"
        )


def build_order_payload(request: OrderRequest) -> dict[str, Any]:
    """Build the Schwab single-leg equity LIMIT order JSON payload."""
    return {
        "orderType": request.order_type.value,
        "session": request.session.value,
        "duration": request.duration.value,
        "orderStrategyType": "SINGLE",
        "price": f"{request.limit_price:.2f}",
        "orderLegCollection": [
            {
                "instruction": request.side.value,
                "quantity": request.quantity,
                "instrument": {
                    "symbol": request.symbol,
                    "assetType": request.asset_type.value,
                },
            }
        ],
    }


class ValidatedOrderIntent(BaseModel):
    """A risk-approved order intent - the only thing allowed to be submitted.

    Constructed by the risk engine (Phase 8) after every check passes. Carries
    the approved request plus the context used to approve it. ``account_hash`` is
    stored as a secret so it is not rendered by ``repr``/logging.
    """

    model_config = ConfigDict(frozen=True)

    request: OrderRequest
    account_hash: SecretStr
    estimated_notional: Decimal
    quote_price: Decimal | None = None
    quote_time: datetime | None = None
    created_at: datetime

    def to_api_payload(self) -> dict[str, Any]:
        return build_order_payload(self.request)

    @property
    def masked_account(self) -> str:
        raw = self.account_hash.get_secret_value()
        return f"****{raw[-4:]}" if len(raw) >= 4 else "****"


class SubmittedOrder(BaseModel):
    """The sanitized result of an order-submission attempt (Phase 11)."""

    model_config = ConfigDict(frozen=True)

    order_id: str | None = None
    status: OrderStatus = OrderStatus.UNKNOWN
    submitted_at: datetime
    location: str | None = None


class OrderDetail(BaseModel):
    """A parsed, sanitized view of an existing order for status/cancel display."""

    order_id: str
    status: OrderStatus = OrderStatus.UNKNOWN
    symbol: str | None = None
    side: str | None = None
    quantity: Decimal | None = None
    filled_quantity: Decimal | None = None
    remaining_quantity: Decimal | None = None
    limit_price: Decimal | None = None
    average_fill_price: Decimal | None = None  # quantity-weighted execution price, if filled
    cancelable: bool | None = None
    entered_time: str | None = None
    close_time: str | None = None
