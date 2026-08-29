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
    def get_open_position_units(self, position_ids):
        return {str(position_id): 0.16 for position_id in position_ids}

    def remember_position_instrument(self, position_id, symbol):
        return None

    def forget_position_instrument(self, position_id):
        return None


def test_confirmed_reconciliation_does_not_reopen_uncertainty_after_restart(tmp_path):
    store = InventoryEventStore(tmp_path / "v3.sqlite")
    store.append(
        InventoryEvent(
            event_id="entry",
            inventory_id="inv",
            event_type="ENTRY_FILLED",
            occurred_at=NOW,
            payload={
                "action_id": "entry",
                "intent_id": "entry",
                "symbol": "AAPL",
                "position_id": "p1",
                "units": 1.0003,
                "price": 100.0,
                "notional": 100.03,
                "fee": 0.0,
                "purpose": "initial_entry",
            },
            strategy_version=STRATEGY,
        )
    )
    store.append(
        InventoryEvent(
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
                "requested_units": 0.84,
                "full_close": False,
                "close_order_id": "order-1",
            },
            strategy_version=STRATEGY,
        )
    )

    events = store.events()
    first = V3BrokerExecutor(
        broker=Broker(),
        task_runner=Runner(),
        event_store=store,
        book=InventoryBook.from_events(events),
        strategy_version=STRATEGY,
        model_version=None,
    )
    first.restore_pending_close_confirmations(events)
    assert first.verify_known_broker_legs() == ()
    pending = first._pending_close_confirmations["close:p1"]
    first._confirm_close(
        context=pending.context,
        exit_price=105.0,
        filled_at=NOW,
        close_order_id="order-1",
        executed_units=0.84,
    )
    assert first.book.active_for_symbol("AAPL").total_units == pytest.approx(0.16)

    replayed_events = store.events()
    restarted = V3BrokerExecutor(
        broker=Broker(),
        task_runner=Runner(),
        event_store=store,
        book=InventoryBook.from_events(replayed_events),
        strategy_version=STRATEGY,
        model_version=None,
    )
    restarted.restore_pending_close_confirmations(replayed_events)

    assert restarted._pending_close_confirmations == {}
    assert restarted.confirmation_metrics()["unattributed_reconciled_position_ids"] == []
    assert restarted.confirmation_metrics()["pending_economic_fill_action_ids"] == []
    assert restarted.halted_reason is None
    assert restarted.new_risk_allowed
    inventory = restarted.book.active_for_symbol("AAPL")
    assert inventory is not None
    assert inventory.total_units == pytest.approx(0.16)
    assert inventory.realized_pnl == pytest.approx(4.2)
