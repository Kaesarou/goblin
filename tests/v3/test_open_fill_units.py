from datetime import datetime, timezone

import pytest

from app.brokers.base import OpenPositionResult
from app.market.models import MarketSnapshot
from app.runtime.broker_task_runner import BrokerTaskCompletion, BrokerTaskLane
from app.v3.book import InventoryBook
from app.v3.live_execution import V3BrokerExecutor
from app.v3.models import ExecutionStyle, IntentPurpose, OrderIntent
from app.v3.persistence import InventoryEventStore
from app.v3.recovery import evaluate_restart_safety

NOW = datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)


class ImmediateRunner:
    def __init__(self):
        self.items: list[BrokerTaskCompletion] = []

    def submit(self, *, kind, operation, context=None, task_id=None, lane=None):
        try:
            value = operation()
            error = None
        except Exception as exc:  # noqa: BLE001 - structured completion under test
            value = None
            error = exc
        self.items.append(
            BrokerTaskCompletion(
                task_id=task_id or kind,
                kind=kind,
                lane=lane or BrokerTaskLane.STANDARD,
                context=context,
                value=value,
                error=error,
            )
        )
        return task_id or kind

    def drain(self):
        result = list(self.items)
        self.items.clear()
        return result


class OpenBroker:
    def __init__(self, result: OpenPositionResult):
        self.result = result

    def open_position(self, symbol, side, amount, stop_loss, take_profit):
        return self.result


def _intent(notional: float = 400.0) -> OrderIntent:
    return OrderIntent(
        intent_id="open-1",
        purpose=IntentPurpose.INITIAL_ENTRY,
        symbol="AIR.PA",
        side="BUY",
        notional=notional,
        created_at=NOW,
        execution_style=ExecutionStyle.MARKET,
    )


def _snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        symbol="AIR.PA",
        bid=199.9,
        ask=200.0,
        last=199.95,
        timestamp=NOW,
        received_at=NOW,
    )


def _executor(tmp_path, result: OpenPositionResult):
    store = InventoryEventStore(tmp_path / "v3.sqlite")
    book = InventoryBook()
    executor = V3BrokerExecutor(
        broker=OpenBroker(result),
        task_runner=ImmediateRunner(),
        event_store=store,
        book=book,
        strategy_version="INVENTORY_RR5_ETORO5_V1",
        model_version=None,
    )
    return executor, book, store


def test_broker_confirmed_units_and_account_notional_are_preserved_independently(tmp_path):
    # 400 / 200 would be 2 units. The broker confirms 1.2345 units instead
    # (representative of FX conversion / broker rounding) while account-currency
    # invested amount remains about $400. V3 must preserve both independently.
    executor, book, store = _executor(
        tmp_path,
        OpenPositionResult(
            position_id="p1",
            executed_entry_price=200.0,
            executed_units=1.2345,
            executed_notional=399.25,
        ),
    )

    assert executor.schedule(_intent(), snapshot=_snapshot())
    assert executor.drain() == ("open-1",)

    inventory = book.active_for_symbol("AIR.PA")
    assert inventory is not None
    assert inventory.total_units == pytest.approx(1.2345)
    assert inventory.broker_legs[0].units == pytest.approx(1.2345)
    assert inventory.total_units != pytest.approx(2.0)
    assert inventory.total_notional == pytest.approx(399.25)
    assert inventory.broker_legs[0].account_notional == pytest.approx(399.25)

    entry = next(event for event in store.events() if event.event_type == "ENTRY_FILLED")
    assert entry.payload["units"] == pytest.approx(1.2345)
    assert entry.payload["units_source"] == "broker_confirmed"
    assert entry.payload["requested_notional"] == pytest.approx(400.0)
    assert entry.payload["notional"] == pytest.approx(399.25)
    assert entry.payload["notional_source"] == "broker_confirmed_account_currency"
    assert entry.payload["asset_notional_at_entry"] == pytest.approx(246.9)

    rebuilt = InventoryBook.from_events(store.events())
    rebuilt_inventory = rebuilt.active_for_symbol("AIR.PA")
    assert rebuilt_inventory is not None
    assert rebuilt_inventory.total_units == pytest.approx(1.2345)
    assert rebuilt_inventory.total_notional == pytest.approx(399.25)
    assert rebuilt_inventory.broker_legs[0].account_notional == pytest.approx(399.25)
    assert evaluate_restart_safety(store.events()).safe


def test_missing_broker_account_notional_falls_back_to_requested_cash_not_asset_value(tmp_path):
    executor, book, store = _executor(
        tmp_path,
        OpenPositionResult(
            position_id="p1",
            executed_entry_price=200.0,
            executed_units=1.2345,
            executed_notional=None,
        ),
    )

    assert executor.schedule(_intent(), snapshot=_snapshot())
    assert executor.drain() == ("open-1",)

    inventory = book.active_for_symbol("AIR.PA")
    assert inventory is not None
    assert inventory.total_notional == pytest.approx(400.0)
    assert inventory.total_notional != pytest.approx(1.2345 * 200.0)
    entry = next(event for event in store.events() if event.event_type == "ENTRY_FILLED")
    assert entry.payload["notional"] == pytest.approx(400.0)
    assert entry.payload["notional_source"] == "requested_account_currency"


def test_real_open_with_price_but_without_confirmed_units_fails_closed(tmp_path):
    executor, book, store = _executor(
        tmp_path,
        OpenPositionResult(
            position_id="p1",
            executed_entry_price=200.0,
            executed_units=None,
        ),
    )

    assert executor.schedule(_intent(), snapshot=_snapshot())
    assert executor.drain() == ()

    assert book.active_for_symbol("AIR.PA") is None
    assert executor.halted_reason == "open_execution_units_missing"
    assert not executor.new_risk_allowed
    event_types = [event.event_type for event in store.events()]
    assert "ENTRY_FILLED" not in event_types
    assert "ORDER_SUBMISSION_UNKNOWN" in event_types
    safety = evaluate_restart_safety(store.events())
    assert not safety.safe
    assert safety.reason == "broker_submission_outcome_unknown"
