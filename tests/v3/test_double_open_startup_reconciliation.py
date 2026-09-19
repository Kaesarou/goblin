"""Startup verification of the two historical INTC fills, without any broker mutation.

All data lives in pytest's disposable SQLite; the VPS SQLite and its WAL are
never read or modified by these tests. A real broker reconciliation remains a
separate prerequisite for production restart.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.v3.book import InventoryBook
from app.v3.live_execution import V3BrokerExecutor
from app.v3.persistence import InventoryEvent, InventoryEventStore
from app.v3.recovery import evaluate_restart_safety

NOW = datetime(2026, 9, 15, 13, 40, tzinfo=timezone.utc)
FIRST = "3599774868"
SECOND = "3599774883"
FIRST_INVENTORY = "INTC:55fcaea1e52b4ecb6f8935e4"
SECOND_INVENTORY = "INTC:97c14413004a4203fb61ea12"
STRATEGY = "INVENTORY_RR5_ETORO5_V1"


class NoMutationRunner:
    def submit(self, **kwargs):
        raise AssertionError("Startup must not schedule broker mutations")

    def drain(self):
        return []


class UnitsBroker:
    def __init__(self, units):
        self.units = dict(units)
        self.remembered = []
        self.reads = []

    def get_open_position_units(self, position_ids):
        ids = tuple(str(item) for item in position_ids)
        self.reads.append(ids)
        return {position_id: self.units.get(position_id) for position_id in ids}

    def remember_position_instrument(self, position_id, symbol):
        self.remembered.append((position_id, symbol))


def _event(inventory_id, action, position, units, offset):
    return InventoryEvent(
        event_id=f"{action}:entry-fill:{position}",
        inventory_id=inventory_id,
        event_type="ENTRY_FILLED",
        occurred_at=NOW + timedelta(seconds=offset),
        payload={
            "action_id": action, "intent_id": action, "symbol": "INTC",
            "position_id": position, "units": units, "price": 99.27,
            "notional": units * 99.27, "fee": 0.0,
            "purpose": "initial_entry",
        },
        strategy_version=STRATEGY,
    )


def _startup(tmp_path, broker_units):
    store = InventoryEventStore(tmp_path / "historical.sqlite")
    first = _event(FIRST_INVENTORY, "first", FIRST, 3.292115, 4)
    second = _event(SECOND_INVENTORY, "second", SECOND, 3.291612, 7)
    assert store.append(first)
    assert store.append(second)
    events = store.events()
    book = InventoryBook.from_events(events)
    broker = UnitsBroker(broker_units)
    executor = V3BrokerExecutor(
        broker=broker,
        task_runner=NoMutationRunner(),
        event_store=store,
        book=book,
        strategy_version=STRATEGY,
        model_version=None,
    )
    executor.restore_pending_close_confirmations(events)
    return executor, store, broker


def test_startup_exact_broker_units_keeps_both_original_legs_without_rewriting_events(tmp_path):
    executor, store, broker = _startup(
        tmp_path, {FIRST: 3.292115, SECOND: 3.291612}
    )
    before = [(e.event_id, e.inventory_id, e.event_type, e.payload)
              for e in store.events()]
    assert evaluate_restart_safety(store.events()).safe
    assert executor.verify_known_broker_legs() == ()
    assert set(broker.remembered) == {(FIRST, "INTC"), (SECOND, "INTC")}
    assert len(broker.reads) == 1
    assert set(broker.reads[0]) == {FIRST, SECOND}
    inventory = executor.book.active_for_symbol("INTC")
    assert inventory is not None
    assert inventory.inventory_id == FIRST_INVENTORY
    assert {leg.position_id: leg.units for leg in inventory.broker_legs} == {
        FIRST: pytest.approx(3.292115), SECOND: pytest.approx(3.291612)
    }
    assert inventory.total_units == pytest.approx(6.583727)
    assert executor.book.portfolio(equity=100_000).active_inventory_count == 1
    assert executor.halted_reason is None
    assert [(e.event_id, e.inventory_id, e.event_type, e.payload)
            for e in store.events()] == before


def test_startup_broker_quantity_mismatch_fails_closed_without_mutation(tmp_path):
    executor, store, broker = _startup(
        tmp_path, {FIRST: 3.292115, SECOND: 3.391612}
    )
    before = [(e.event_id, e.inventory_id, e.event_type, e.payload)
              for e in store.events()]
    issues = executor.verify_known_broker_legs()
    assert issues and any(SECOND in issue for issue in issues)
    assert executor.halted_reason == "broker_leg_reconciliation_failed"
    assert not executor.new_risk_allowed
    assert broker.reads and len(broker.reads) == 1
    assert [(e.event_id, e.inventory_id, e.event_type, e.payload)
            for e in store.events()] == before
