"""Account discovery and sanitized account details.

Phase 4: retrieve the authorized account-number -> hash mapping and a sanitized
summary of a selected account. Account-specific API calls use the Schwab account
*hash*, never the visible account number. Account numbers and hashes are treated
as sensitive and are only ever displayed masked.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from schwab_trader import client as api


def _dec(value: Any) -> Decimal | None:
    """Coerce an API numeric value to Decimal, or None if absent/invalid."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _dec0(value: Any) -> Decimal:
    """Like :func:`_dec` but defaults missing/invalid values to 0."""
    return _dec(value) or Decimal(0)


def mask_tail(value: str) -> str:
    """Return a masked identifier showing only the last four characters."""
    if len(value) >= 4:
        return f"****{value[-4:]}"
    return "****"


class AccountMapping(BaseModel):
    """One authorized account: its visible number and its API hash.

    Both fields are sensitive; use the ``masked_*`` properties for any display.
    """

    model_config = ConfigDict(populate_by_name=True)

    account_number: str = Field(alias="accountNumber")
    hash_value: str = Field(alias="hashValue")

    @property
    def masked_number(self) -> str:
        return mask_tail(self.account_number)

    @property
    def masked_hash(self) -> str:
        return mask_tail(self.hash_value)


class AccountSummary(BaseModel):
    """A minimal, sanitized view of a single account (details land in Phase 5)."""

    account_type: str
    account_number_masked: str
    is_day_trader: bool | None = None


def get_account_numbers(client: api.SchwabClient) -> list[AccountMapping]:
    """Return the authorized account-number -> hash mappings."""
    data = client.get(api.ACCOUNT_NUMBERS_PATH)
    if not isinstance(data, list):
        return []
    return [AccountMapping.model_validate(item) for item in data]


def get_account_summary(client: api.SchwabClient, account_hash: str) -> AccountSummary:
    """Fetch a single account by hash and return a sanitized summary."""
    data: Any = client.get(f"{api.ACCOUNTS_PATH}/{account_hash}")
    securities = data.get("securitiesAccount", {}) if isinstance(data, dict) else {}
    number = securities.get("accountNumber", "")
    return AccountSummary(
        account_type=securities.get("type", "UNKNOWN"),
        account_number_masked=mask_tail(number),
        is_day_trader=securities.get("isDayTrader"),
    )


class Balances(BaseModel):
    """Key balance fields from an account's ``currentBalances`` (money as Decimal)."""

    account_type: str
    cash_balance: Decimal | None = None
    cash_available_for_trading: Decimal | None = None
    cash_available_for_withdrawal: Decimal | None = None
    unsettled_cash: Decimal | None = None
    total_cash: Decimal | None = None
    liquidation_value: Decimal | None = None
    long_market_value: Decimal | None = None

    @property
    def buying_power(self) -> Decimal | None:
        """Cash available to place new trades (for a cash account)."""
        return self.cash_available_for_trading


class Position(BaseModel):
    """A single open position (long quantities matter for cash-account sells)."""

    symbol: str
    asset_type: str
    long_quantity: Decimal = Decimal(0)
    short_quantity: Decimal = Decimal(0)
    settled_long_quantity: Decimal = Decimal(0)
    average_price: Decimal | None = None
    market_value: Decimal | None = None
    current_day_profit_loss: Decimal | None = None
    current_day_profit_loss_pct: Decimal | None = None


def get_balances(client: api.SchwabClient, account_hash: str) -> Balances:
    """Fetch the account and return its key balances."""
    data: Any = client.get(f"{api.ACCOUNTS_PATH}/{account_hash}")
    securities = data.get("securitiesAccount", {}) if isinstance(data, dict) else {}
    current = securities.get("currentBalances", {}) or {}
    return Balances(
        account_type=securities.get("type", "UNKNOWN"),
        cash_balance=_dec(current.get("cashBalance")),
        cash_available_for_trading=_dec(current.get("cashAvailableForTrading")),
        cash_available_for_withdrawal=_dec(current.get("cashAvailableForWithdrawal")),
        unsettled_cash=_dec(current.get("unsettledCash")),
        total_cash=_dec(current.get("totalCash")),
        liquidation_value=_dec(current.get("liquidationValue")),
        long_market_value=_dec(current.get("longMarketValue")),
    )


def get_positions(client: api.SchwabClient, account_hash: str) -> list[Position]:
    """Fetch the account's open positions."""
    data: Any = client.get(f"{api.ACCOUNTS_PATH}/{account_hash}", params={"fields": "positions"})
    securities = data.get("securitiesAccount", {}) if isinstance(data, dict) else {}
    positions: list[Position] = []
    for item in securities.get("positions", []) or []:
        instrument = item.get("instrument", {}) or {}
        positions.append(
            Position(
                symbol=instrument.get("symbol", ""),
                asset_type=instrument.get("assetType", ""),
                long_quantity=_dec0(item.get("longQuantity")),
                short_quantity=_dec0(item.get("shortQuantity")),
                settled_long_quantity=_dec0(item.get("settledLongQuantity")),
                average_price=_dec(item.get("averagePrice")),
                market_value=_dec(item.get("marketValue")),
                current_day_profit_loss=_dec(item.get("currentDayProfitLoss")),
                current_day_profit_loss_pct=_dec(item.get("currentDayProfitLossPercentage")),
            )
        )
    return positions
