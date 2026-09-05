from datetime import datetime, timezone

import pytest

from app.market.models import MarketSnapshot
from app.v3.book import InventoryBook
from app.v3.live_execution import V3BrokerExecutor
from app.v3.models import ExecutionStyle, IntentPurpose, OrderIntent
from app.v3.persistence import InventoryEvent, InventoryEventStore

NOW = datetime(2026, 8, 25, 18, 0, tzinfo=timezone.utc)


class QueueRunner:
    def __init__(self):
        self.submissions = []

    def submit(self, *, kind, operation, context=None, task_id=None, lane=None):
        self.submissions.append((kind, operation, context, task_id, lane))
        return task_id or kind

    def drain(self):
        return []


def _entry_event(units=1.0):
    return InventoryEvent(
        event_id="entry",
        inventory_id="inv",
        event_type="ENTRY_FILLED",
        occurred_at=NOW,
        payload={
            "symbol": "AAPL",
            "position_id": "p1",
            "units": units,
            "price": 100.0,
            "fee": 0.0,
        },
        strategy_version="INVENTORY_RR5_ETORO5_V1",
    )


def _executor(tmp_path, book, events=()):
    store = InventoryEventStore(tmp_path / "v3.sqlite")
    for event in events:
        store.append(event)
    return V3BrokerExecutor(
        broker=object(),
        task_runner=QueueRunner(),
        event_store=store,
        book=book,
        strategy_version="INVENTORY_RR5_ETORO5_V1",
        model_version=None,
    )


def _close_intent(inventory, intent_id="close"):
    return OrderIntent(
        intent_id=intent_id,
        purpose=IntentPurpose.PROFIT_EXIT,
        symbol=inventory.symbol,
        side="SELL",
        notional=inventory.total_notional * 0.84,
        created_at=NOW,
        execution_style=ExecutionStyle.LIMIT,
        limit_price=100.0,
        inventory_id=inventory.inventory_id,
        reduce_only=True,
        metadata={"close_fraction_of_units": 0.84},
    )


def test_from_events_rebuilds_remaining_partial_broker_leg():
    exit_event = InventoryEvent(
        event_id="exit",
        inventory_id="inv",
        event_type="EXIT_FILLED",
        occurred_at=NOW,
        payload={
            "symbol": "AAPL",
            "position_id": "p1",
            "units": 0.84,
            "price": 105.0,
            "fee": 0.0,
        },
        strategy_version="INVENTORY_RR5_ETORO5_V1",
    )

    rebuilt = InventoryBook.from_events((_entry_event(), exit_event))
    inventory = rebuilt.active_for_symbol("AAPL")

    assert inventory is not None
    assert inventory.total_units == pytest.approx(0.16)
    assert inventory.average_entry_price == pytest.approx(100.0)
    assert len(inventory.broker_legs) == 1
    assert inventory.broker_legs[0].position_id == "p1"
    assert inventory.broker_legs[0].units == pytest.approx(0.16)
    assert rebuilt.active_broker_position_ids() == ("p1",)


def test_restore_legacy_full_close_recovers_units_from_persisted_leg(tmp_path):
    book = InventoryBook.from_events((_entry_event(),))
    accepted = InventoryEvent(
        event_id="accepted",
        inventory_id="inv",
        event_type="CLOSE_SUBMISSION_ACCEPTED",
        occurred_at=NOW,
        payload={
            "action_id": "legacy-close:p1",
            "intent_id": "legacy-close",
            "position_id": "p1",
            "symbol": "AAPL",
            "trigger_price": 101.0,
            "close_order_id": "order-1",
        },
        strategy_version="INVENTORY_RR5_V1",
    )
    executor = _executor(tmp_path, book)

    executor.restore_pending_close_confirmations((accepted,))

    pending = executor._pending_close_confirmations["legacy-close:p1"]
    assert pending.context.full_close is True
    assert pending.context.requested_units == pytest.approx(1.0)
    assert "p1" in executor._active_close_mutations_by_position


def test_pending_broker_leg_cannot_be_scheduled_twice(tmp_path):
    book = InventoryBook.from_events((_entry_event(),))
    executor = _executor(tmp_path, book)
    inventory = book.active_for_symbol("AAPL")
    snapshot = MarketSnapshot(
        symbol="AAPL",
        bid=101.0,
        ask=101.1,
        last=101.05,
        timestamp=NOW,
        received_at=NOW,
    )

    assert executor.schedule(_close_intent(inventory, "first"), snapshot=snapshot)
    assert not executor.schedule(_close_intent(inventory, "second"), snapshot=snapshot)
    assert len(executor.task_runner.submissions) == 1
    assert "p1" in executor._active_close_mutations_by_position
