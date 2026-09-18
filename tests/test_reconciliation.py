"""Offline tests for broker reconciliation and idempotent fill ingestion."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

from schwab_trader import reconciliation, taxlots
from schwab_trader.client import SchwabClient
from schwab_trader.config import Settings
from schwab_trader.models import OrderRequest, OrderSide
from schwab_trader.safety import SafetyLedger

NOW = datetime(2026, 7, 21, 15, 0, tzinfo=UTC)
ACCOUNT_HASH = "SANITIZED_TEST_HASH_1234"


class FakeClient:
    def __init__(self, order_rows: list[dict[str, Any]], positions: dict[str, Decimal]) -> None:
        self.order_rows = order_rows
        self.positions = positions

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        if path.endswith("/orders"):
            return self.order_rows
        if params == {"fields": "positions"}:
            return {
                "securitiesAccount": {
                    "positions": [
                        {
                            "instrument": {"symbol": symbol, "assetType": "EQUITY"},
                            "longQuantity": str(quantity),
                            "shortQuantity": "0",
                            "settledLongQuantity": str(quantity),
                        }
                        for symbol, quantity in self.positions.items()
                    ]
                }
            }
        raise AssertionError(f"Unexpected GET {path} {params}")


def _client(rows: list[dict[str, Any]], positions: dict[str, Decimal]) -> SchwabClient:
    return cast(SchwabClient, FakeClient(rows, positions))


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        account_hash=ACCOUNT_HASH,
        state_db_path=tmp_path / "state.sqlite3",
        tax_lots_db_path=tmp_path / "taxlots.sqlite3",
        agent_activity_db_path=tmp_path / "activity.sqlite3",
    )


def _request(
    *, side: OrderSide = OrderSide.BUY, quantity: int = 10, price: str = "100.00"
) -> OrderRequest:
    return OrderRequest(
        side=side,
        symbol="AAPL",
        quantity=quantity,
        limit_price=Decimal(price),
    )


def _order(
    *,
    order_id: str = "1001",
    status: str = "WORKING",
    side: str = "BUY",
    quantity: str = "10",
    filled: str = "4",
    remaining: str = "6",
    executions: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    execution_rows = [("100.00", filled)] if executions is None else executions
    legs = [{"price": price, "quantity": qty} for price, qty in execution_rows]
    return {
        "orderId": order_id,
        "status": status,
        "quantity": quantity,
        "filledQuantity": filled,
        "remainingQuantity": remaining,
        "price": "100.00",
        "enteredTime": "2026-07-21T14:30:00+00:00",
        "orderLegCollection": [
            {"instruction": side, "instrument": {"symbol": "AAPL", "assetType": "EQUITY"}}
        ],
        "orderActivityCollection": [{"executionLegs": legs}],
    }


def test_tracked_partial_fill_is_applied_once(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    reconciliation.track_submission(settings, order_id="1001", request=_request(), now=NOW)
    client = _client([_order()], {"AAPL": Decimal(4)})

    first = reconciliation.reconcile_account(client, settings, ACCOUNT_HASH, now=NOW)
    second = reconciliation.reconcile_account(client, settings, ACCOUNT_HASH, now=NOW)

    assert first.fills_applied == 1
    assert first.transitions == 1
    assert second.fills_applied == 0
    lots = taxlots.TaxLotStore(settings.tax_lots_db_path).open_lots("AAPL")
    assert [(lot.quantity, lot.cost_per_share) for lot in lots] == [(Decimal(4), Decimal("100.00"))]


def test_second_partial_fill_uses_incremental_execution_price(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    reconciliation.track_submission(settings, order_id="1001", request=_request(), now=NOW)
    first_client = _client([_order()], {"AAPL": Decimal(4)})
    reconciliation.reconcile_account(first_client, settings, ACCOUNT_HASH, now=NOW)

    completed = _order(
        status="FILLED",
        filled="10",
        remaining="0",
        executions=[("100.00", "4"), ("104.00", "6")],
    )
    second = reconciliation.reconcile_account(
        _client([completed], {"AAPL": Decimal(10)}),
        settings,
        ACCOUNT_HASH,
        now=NOW,
    )

    assert second.fills_applied == 1
    lots = taxlots.TaxLotStore(settings.tax_lots_db_path).open_lots("AAPL")
    assert [(lot.quantity, lot.cost_per_share) for lot in lots] == [
        (Decimal(4), Decimal("100.00")),
        (Decimal(6), Decimal("104.00")),
    ]


def test_preexisting_fill_is_baselined_without_tax_mutation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    report = reconciliation.reconcile_account(
        _client([_order(status="FILLED", filled="10", remaining="0")], {"AAPL": Decimal(10)}),
        settings,
        ACCOUNT_HASH,
        now=NOW,
    )

    assert report.fills_applied == 0
    assert any(item.kind == "legacy_fill_baselined" for item in report.discrepancies)
    assert taxlots.TaxLotStore(settings.tax_lots_db_path).open_lots() == []


def test_sell_fill_flags_missing_local_lots_and_records_covered_gain(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    lots = taxlots.TaxLotStore(settings.tax_lots_db_path)
    lots.record_purchase(
        symbol="AAPL",
        quantity=Decimal(3),
        cost_per_share=Decimal(80),
        acquired_at=NOW,
    )
    request = _request(side=OrderSide.SELL, quantity=5, price="100.00")
    reconciliation.track_submission(settings, order_id="2001", request=request, now=NOW)
    row = _order(
        order_id="2001",
        status="FILLED",
        side="SELL",
        quantity="5",
        filled="5",
        remaining="0",
        executions=[("100.00", "5")],
    )

    report = reconciliation.reconcile_account(_client([row], {}), settings, ACCOUNT_HASH, now=NOW)

    assert report.fills_applied == 1
    assert any(item.kind == "tax_lots_incomplete" for item in report.discrepancies)
    assert lots.open_lots("AAPL") == []
    assert SafetyLedger(settings.agent_activity_db_path).day(NOW).realized_pnl == Decimal(60)


def test_position_quantity_mismatch_is_reported(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    taxlots.TaxLotStore(settings.tax_lots_db_path).record_purchase(
        symbol="AAPL",
        quantity=Decimal(2),
        cost_per_share=Decimal(90),
        acquired_at=NOW,
    )
    report = reconciliation.reconcile_account(
        _client([], {"AAPL": Decimal(3)}), settings, ACCOUNT_HASH, now=NOW
    )
    mismatch = next(
        item for item in report.discrepancies if item.kind == "position_quantity_mismatch"
    )
    assert "Schwab quantity 3" in mismatch.detail
    assert "local tax-lot quantity 2" in mismatch.detail


def test_pending_fill_reservation_fails_closed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = reconciliation.ReconciliationStore(settings.state_db_path)
    reconciliation.track_submission(settings, order_id="1001", request=_request(), now=NOW)
    store.reserve_fill(
        order_id="1001",
        cumulative_quantity=Decimal(4),
        delta_quantity=Decimal(4),
        price=Decimal(100),
        now=NOW,
    )

    report = reconciliation.reconcile_account(
        _client([_order()], {"AAPL": Decimal(4)}), settings, ACCOUNT_HASH, now=NOW
    )

    assert report.fills_applied == 0
    assert any(item.kind == "fill_application_uncertain" for item in report.discrepancies)
    assert taxlots.TaxLotStore(settings.tax_lots_db_path).open_lots() == []


def test_fill_without_price_waits_for_later_priced_observation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    reconciliation.track_submission(settings, order_id="1001", request=_request(), now=NOW)
    missing_price = _order(executions=[])
    first = reconciliation.reconcile_account(
        _client([missing_price], {"AAPL": Decimal(4)}),
        settings,
        ACCOUNT_HASH,
        now=NOW,
    )
    assert first.fills_applied == 0
    assert any(item.kind == "fill_price_unavailable" for item in first.discrepancies)

    second = reconciliation.reconcile_account(
        _client([_order()], {"AAPL": Decimal(4)}),
        settings,
        ACCOUNT_HASH,
        now=NOW,
    )
    assert second.fills_applied == 1
    assert taxlots.TaxLotStore(settings.tax_lots_db_path).open_lots("AAPL")[0].quantity == 4


def test_failed_broker_read_is_recorded_as_unhealthy(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    class FailingClient:
        def get(self, _path: str, *, params: dict[str, Any] | None = None) -> Any:
            raise OSError("offline")

    with pytest.raises(OSError):
        reconciliation.reconcile_account(
            cast(SchwabClient, FailingClient()),
            settings,
            ACCOUNT_HASH,
            now=NOW,
        )
    summary = reconciliation.ReconciliationStore(settings.state_db_path).latest_summary()
    assert summary.success is False
    assert summary.discrepancies == 1


def test_latest_summary_records_health(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    reconciliation.reconcile_account(_client([], {}), settings, ACCOUNT_HASH, now=NOW)
    summary = reconciliation.ReconciliationStore(settings.state_db_path).latest_summary()
    assert summary.completed_at == NOW
    assert summary.success is True
    assert summary.orders_seen == 0
