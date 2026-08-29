from datetime import datetime, timezone

import pytest

from app.v3.book import InventoryBook
from app.v3.live_execution import V3BrokerExecutor
from app.v3.persistence import InventoryEvent, InventoryEventStore

NOW = datetime.now(timezone.utc)
STRATEGY = "INVENTORY_RR5_ETORO5_V1"


class Runner:
    def drain(self):
        return []

    def submit(self, **kwargs):
        return kwargs.get("task_id", "task")


class Broker:
    def __init__(self, units):
        self.units = dict(units)
        self.remembered = []
        self.forgotten = []

    def get_open_position_units(self, position_ids):
        return {
            str(position_id): self.units.get(str(position_id), 0.0)
            for position_id in position_ids
        }

    def remember_position_instrument(self, position_id, symbol):
        self.remembered.append((position_id, symbol))

    def forget_position_instrument(self, position_id):
        self.forgotten.append(position_id)


def _entry_event(*, units=1.0003, account_notional=100.03):
    return InventoryEvent(
        event_id="entry",
        inventory_id="inv",
        event_type="ENTRY_FILLED",
        occurred_at=NOW,
        payload={
            "action_id": "entry-action",
            "intent_id": "entry-action",
            "symbol": "AAPL",
            "position_id": "p1",
            "units": units,
            "price": 100.0,
            "notional": account_notional,
            "fee": 0.0,
            "purpose": "initial_entry",
        },
        strategy_version=STRATEGY,
    )


def _accepted_close_event(*, requested_units=0.84, full_close=False):
    return InventoryEvent(
        event_id="accepted",
        inventory_id="inv",
        event_type="CLOSE_SUBMISSION_ACCEPTED",
        occurred_at=NOW,
        payload={
            "action_id": "close:p1",
            "intent_id": "close",
            "position_id": "p1",
            "symbol": "AAPL",
            "purpose": "profit_exit",
            "trigger_price": 105.0,
            "requested_units": requested_units,
            "full_close": full_close,
            "close_order_id": "order-1",
        },
        strategy_version=STRATEGY,
    )


def _executor(tmp_path, *, broker_units, accepted=True, full_close=False):
    store = InventoryEventStore(tmp_path / "v3.sqlite")
    store.append(
        _entry_event(
            units=(1.0 if full_close else 1.0003),
            account_notional=(100.0 if full_close else 100.03),
        )
    )
    if accepted:
        store.append(
            _accepted_close_event(
                requested_units=(1.0 if full_close else 0.84),
                full_close=full_close,
            )
        )
    events = store.events()
    book = InventoryBook.from_events(events)
    executor = V3BrokerExecutor(
        broker=Broker({"p1": broker_units}),
        task_runner=Runner(),
        event_store=store,
        book=book,
        strategy_version=STRATEGY,
        model_version=None,
    )
    executor.restore_pending_close_confirmations(events)
    return executor, store


def test_startup_reconciles_historical_partial_close_without_crashing(tmp_path):
    executor, store = _executor(tmp_path, broker_units=0.16)

    assert executor.verify_known_broker_legs() == ()

    inventory = executor.book.active_for_symbol("AAPL")
    assert inventory is not None
    assert inventory.total_units == pytest.approx(0.16)
    assert inventory.total_notional == pytest.approx(16.0)
    assert executor.halted_reason in {
        "broker_quantity_reduction_pending_economic_fill",
        "stale_close_confirmation",
    }
    assert not executor.new_risk_allowed

    reconciliations = [
        event
        for event in store.events()
        if event.event_type == "BROKER_QUANTITY_RECONCILED"
    ]
    assert len(reconciliations) == 1
    payload = reconciliations[0].payload
    assert payload["previous_book_units"] == pytest.approx(1.0003)
    assert payload["broker_units"] == pytest.approx(0.16)
    assert payload["reconciled_book_units"] == pytest.approx(0.8403)
    assert payload["action_ids"] == ["close:p1"]


def test_late_fill_finalizes_economics_without_subtracting_quantity_twice(tmp_path):
    executor, store = _executor(tmp_path, broker_units=0.16)
    executor.verify_known_broker_legs()
    pending = executor._pending_close_confirmations["close:p1"]

    # The historical local entry units were approximate. The broker execution
    # can legitimately report 0.84 while reconciliation removed 0.8403 from the
    # stale local book. Broker remaining quantity must stay authoritative.
    executor._confirm_close(
        context=pending.context,
        exit_price=105.0,
        filled_at=NOW,
        close_order_id="order-1",
        executed_units=0.84,
    )

    inventory = executor.book.active_for_symbol("AAPL")
    assert inventory is not None
    assert inventory.total_units == pytest.approx(0.16)
    assert inventory.realized_pnl == pytest.approx(4.2)
    assert "close:p1" not in executor._pending_close_confirmations

    economic_events = [
        event
        for event in store.events()
        if event.event_type == "EXIT_ECONOMICS_CONFIRMED"
    ]
    assert len(economic_events) == 1
    assert economic_events[0].payload["quantity_already_reconciled"] is True
    assert economic_events[0].payload["migration_unit_delta"] == pytest.approx(-0.0003)

    rebuilt = InventoryBook.from_events(store.events())
    rebuilt_inventory = rebuilt.active_for_symbol("AAPL")
    assert rebuilt_inventory is not None
    assert rebuilt_inventory.total_units == pytest.approx(0.16)
    assert rebuilt_inventory.realized_pnl == pytest.approx(4.2)


def test_full_close_reconciliation_can_finalize_economics_after_inventory_closed(tmp_path):
    executor, store = _executor(
        tmp_path,
        broker_units=0.0,
        full_close=True,
    )
    assert executor.verify_known_broker_legs() == ()
    assert executor.book.active_for_symbol("AAPL") is None

    pending = executor._pending_close_confirmations["close:p1"]
    executor._confirm_close(
        context=pending.context,
        exit_price=110.0,
        filled_at=NOW,
        close_order_id="order-1",
        executed_units=1.0,
    )

    inventory = next(item for item in executor.book.inventories if item.inventory_id == "inv")
    assert inventory.total_units == 0.0
    assert inventory.realized_pnl == pytest.approx(10.0)
    assert executor.broker.forgotten == ["p1"]

    rebuilt = InventoryBook.from_events(store.events())
    rebuilt_inventory = next(item for item in rebuilt.inventories if item.inventory_id == "inv")
    assert rebuilt_inventory.total_units == 0.0
    assert rebuilt_inventory.realized_pnl == pytest.approx(10.0)


def test_unattributed_broker_reduction_reconciles_exposure_but_keeps_new_risk_blocked(tmp_path):
    executor, store = _executor(
        tmp_path,
        broker_units=0.25,
        accepted=False,
    )

    assert executor.verify_known_broker_legs() == ()
    inventory = executor.book.active_for_symbol("AAPL")
    assert inventory is not None
    assert inventory.total_units == pytest.approx(0.25)
    assert executor.halted_reason == "broker_quantity_reduction_unattributed"
    assert not executor.new_risk_allowed
    metrics = executor.confirmation_metrics()
    assert metrics["unattributed_reconciled_position_ids"] == ["p1"]

    rebuilt = InventoryBook.from_events(store.events())
    rebuilt_inventory = rebuilt.active_for_symbol("AAPL")
    assert rebuilt_inventory is not None
    assert rebuilt_inventory.total_units == pytest.approx(0.25)
