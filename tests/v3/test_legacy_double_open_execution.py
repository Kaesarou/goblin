"""Exercise an INTC historical double-fill through complete paper close lifecycle.

This does not replace the mandatory eToro broker reconciliation before production.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.brokers.paper.paper_broker import PaperBrokerClient
from app.market.models import MarketSnapshot
from app.runtime.broker_task_runner import BrokerTaskCompletion, BrokerTaskLane
from app.v3.book import InventoryBook
from app.v3.execution import ProRataPartialCloseAllocator
from app.v3.live_execution import V3BrokerExecutor
from app.v3.models import ExecutionStyle, IntentPurpose, OrderIntent
from app.v3.persistence import InventoryEvent, InventoryEventStore

NOW = datetime(2026, 9, 15, 13, 40, tzinfo=timezone.utc)
POSITIONS = {"3599774868": 3.292115, "3599774883": 3.291612}


class ImmediateRunner:
    def __init__(self):
        self.items = []

    def submit(self, *, kind, operation, context=None, task_id=None, lane=None):
        try:
            value, error = operation(), None
        except Exception as exc:  # noqa: BLE001
            value, error = None, exc
        self.items.append(BrokerTaskCompletion(
            task_id=task_id or kind, kind=kind,
            lane=lane or BrokerTaskLane.STANDARD,
            context=context, value=value, error=error,
        ))
        return task_id or kind

    def drain(self):
        result = list(self.items)
        self.items.clear()
        return result


def _event(action, position, units, ts):
    return InventoryEvent(
        event_id=f"{action}:entry-fill:{position}", inventory_id=f"INTC:{action}",
        event_type="ENTRY_FILLED", occurred_at=ts,
        payload={"action_id": action, "symbol": "INTC", "position_id": position,
                 "units": units, "price": 99.27, "fee": 0.0,
                 "notional": units * 99.27},
        strategy_version="INVENTORY_RR5_ETORO5_V1",
    )


def _intent(inv, fraction, action):
    return OrderIntent(
        intent_id=action, purpose=IntentPurpose.PROFIT_EXIT,
        symbol="INTC", side="SELL", notional=inv.total_notional * fraction,
        created_at=NOW, execution_style=ExecutionStyle.LIMIT,
        limit_price=112.0, inventory_id=inv.inventory_id, reduce_only=True,
        metadata={"close_fraction_of_units": fraction},
    )


def test_two_historical_intc_legs_close_pro_rata_and_replay_without_loss(tmp_path):
    store = InventoryEventStore(tmp_path / "historical.sqlite")
    positions = sorted(POSITIONS.items())
    for index, (position, units) in enumerate(positions):
        assert store.append(_event(
            f"real-intc-{index}", position, units,
            NOW + timedelta(seconds=4 + 3 * index),
        ))
    original_entries = [(e.event_id, e.inventory_id, e.payload)
                        for e in store.events() if e.event_type == "ENTRY_FILLED"]
    book = InventoryBook.from_events(store.events())
    inv = book.active_for_symbol("INTC")
    assert inv is not None
    assert inv.total_units == pytest.approx(6.583727)
    assert inv.entry_fill_count == 2

    allocator = ProRataPartialCloseAllocator()
    plan = allocator.plan(inv, inv.total_units * 0.84)
    assert len(plan.requests) == 2
    for request in plan.requests:
        assert request.units == pytest.approx(POSITIONS[request.position_id] * 0.84)
        assert request.full_close is False

    broker = PaperBrokerClient(equity=100_000)
    for position in POSITIONS:
        broker.positions[position] = {"position_id": position, "symbol": "INTC"}
    executor = V3BrokerExecutor(
        broker=broker, task_runner=ImmediateRunner(), event_store=store,
        book=book, strategy_version="INVENTORY_RR5_ETORO5_V1", model_version=None,
    )
    quote = MarketSnapshot("INTC", 112.0, 112.1, 112.0, NOW)
    assert executor.schedule(_intent(inv, 0.84, "close84"), snapshot=quote)
    assert set(executor.drain()) == {"close84"}
    remaining = book.active_for_symbol("INTC")
    assert remaining is not None
    assert remaining.total_units == pytest.approx(6.583727 * 0.16)
    assert len(remaining.broker_legs) == 2
    assert set(book.active_broker_position_ids()) == set(POSITIONS)
    assert all(broker.is_position_open(position) for position in POSITIONS)
    assert InventoryBook.from_events(store.events()).active_for_symbol("INTC").total_units == pytest.approx(
        remaining.total_units
    )

    assert executor.schedule(_intent(remaining, 1.0, "close100"), snapshot=quote)
    assert set(executor.drain()) == {"close100"}
    assert book.active_for_symbol("INTC") is None
    assert InventoryBook.from_events(store.events()).active_for_symbol("INTC") is None
    assert all(not broker.is_position_open(position) for position in POSITIONS)
    assert [(e.event_id, e.inventory_id, e.payload)
            for e in store.events() if e.event_type == "ENTRY_FILLED"] == original_entries
    assert len([e for e in store.events() if e.event_type == "EXIT_FILLED"]) == 4
