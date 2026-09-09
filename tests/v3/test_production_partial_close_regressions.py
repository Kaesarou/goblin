import time
from datetime import datetime, timezone

import pytest

from app.brokers.base import BrokerCloseExecution
from app.runtime.broker_task_runner import BrokerTaskCompletion, BrokerTaskLane
from app.v3.book import InventoryBook
from app.v3.live_execution import V3BrokerExecutor
from app.v3.persistence import InventoryEvent, InventoryEventStore

NOW = datetime(2026, 9, 9, 13, 31, 10, tzinfo=timezone.utc)
ACTION_ID = "46cbe4dff18526acd328f5f6:3590324772"
POSITION_ID = "3590324772"
CLOSE_ORDER_ID = "380196350"


class _Runner:
    def __init__(self):
        self.tasks = []
        self.completions = []

    def submit(self, *, kind, operation, context=None, task_id=None, lane=None):
        task = {
            "kind": kind,
            "operation": operation,
            "context": context,
            "task_id": task_id or kind,
            "lane": lane or BrokerTaskLane.STANDARD,
        }
        self.tasks.append(task)
        return task["task_id"]

    def drain(self):
        result = list(self.completions)
        self.completions.clear()
        return result

    def complete(self, task, *, value=None, error=None):
        self.completions.append(
            BrokerTaskCompletion(
                task_id=task["task_id"],
                kind=task["kind"],
                lane=task["lane"],
                context=task["context"],
                value=value,
                error=error,
            )
        )


class _Broker:
    def forget_position_instrument(self, position_id):
        return None

    def get_rate_limit_metrics(self):
        return {}


def _accepted_event():
    return InventoryEvent(
        event_id="accepted",
        inventory_id="MU:inventory",
        event_type="CLOSE_SUBMISSION_ACCEPTED",
        occurred_at=NOW,
        payload={
            "action_id": ACTION_ID,
            "intent_id": "46cbe4dff18526acd328f5f6",
            "position_id": POSITION_ID,
            "symbol": "MU",
            "purpose": "profit_exit",
            "trigger_price": 999.47,
            "requested_units": 0.06338807999999999,
            "pre_close_units": 0.075462,
            "full_close": False,
            "close_order_id": CLOSE_ORDER_ID,
        },
        strategy_version="INVENTORY_RR5_ETORO5_V1",
    )


def _historical_false_reconciliation_event():
    return InventoryEvent(
        event_id="quantity-reconciled",
        inventory_id="MU:inventory",
        event_type="BROKER_QUANTITY_RECONCILED",
        occurred_at=NOW,
        payload={
            "position_id": POSITION_ID,
            "symbol": "MU",
            "previous_book_units": 0.075462,
            "broker_units": 0.012074,
            "reconciled_book_units": 0.063388,
            "entry_price_basis": 923.33,
            "previous_account_notional": 69.68,
            "remaining_account_notional": 11.15,
            "pending_requested_units": 0.06338807999999999,
            "economic_fill_pending": True,
            "action_ids": [ACTION_ID],
            "attribution_confident": False,
            "source": "broker_portfolio",
        },
        strategy_version="INVENTORY_RR5_ETORO5_V1",
    )


def test_real_mu_rounding_and_fill_id_recover_persisted_false_unattributed_state(tmp_path):
    book = InventoryBook()
    # Runtime state has already followed broker truth after periodic reconciliation:
    # 0.075462 - 0.063388 = 0.012074 units remain on the original open leg.
    book.apply_entry_fill(
        inventory_id="MU:inventory",
        symbol="MU",
        position_id=POSITION_ID,
        units=0.012074,
        price=923.33,
        fee=0.0,
        filled_at=NOW,
    )
    store = InventoryEventStore(tmp_path / "v3.sqlite")
    runner = _Runner()
    executor = V3BrokerExecutor(
        broker=_Broker(),
        task_runner=runner,
        event_store=store,
        book=book,
        strategy_version="INVENTORY_RR5_ETORO5_V1",
        model_version=None,
    )
    events = (_accepted_event(), _historical_false_reconciliation_event())
    executor.restore_pending_close_confirmations(events)

    assert executor.halted_reason == "broker_quantity_reduction_unattributed"
    pending = executor._pending_close_confirmations[ACTION_ID]
    assert pending.quantity_resolved
    assert not pending.attribution_confident
    assert pending.mutation_active

    pending.next_attempt_monotonic = time.monotonic()
    assert executor.schedule_close_confirmation_checks(
        monotonic_now=pending.next_attempt_monotonic
    ) == 1
    task = runner.tasks[-1]
    assert task["kind"] == "v3_close_execution_lookup"
    runner.complete(
        task,
        value=BrokerCloseExecution(
            position_id=POSITION_ID,
            close_order_id=CLOSE_ORDER_ID,
            executed_exit_price=999.47,
            executed_at=NOW,
            units=0.063388,
            conversion_rate=1.0,
            amount=57.56,
            broker_response={},
            broker_execution_position_id="3597134264",
        ),
    )

    assert executor.drain() == ("46cbe4dff18526acd328f5f6",)
    assert executor.halted_reason is None
    assert executor.confirmation_metrics()["unattributed_reconciliation_count"] == 0
    assert not executor._pending_close_confirmations
    assert not executor._active_close_mutations_by_position
    assert book.active_for_symbol("MU").total_units == pytest.approx(0.012074)

    economics = [
        event for event in store.events()
        if event.event_type == "EXIT_ECONOMICS_CONFIRMED"
    ]
    assert len(economics) == 1
    assert economics[0].payload["attribution_confident"] is True
    assert economics[0].payload["broker_execution_position_id"] == "3597134264"
    assert economics[0].payload["units"] == pytest.approx(0.063388)

    # The corrective attribution must survive a second restart. The earlier false
    # BROKER_QUANTITY_RECONCILED event is historical evidence, while the later
    # authoritative economics event resolves that action and clears its old halt.
    restarted = V3BrokerExecutor(
        broker=_Broker(),
        task_runner=_Runner(),
        event_store=store,
        book=book,
        strategy_version="INVENTORY_RR5_ETORO5_V1",
        model_version=None,
    )
    restarted.restore_pending_close_confirmations((*events, *store.events()))
    assert restarted.halted_reason is None
    assert restarted.confirmation_metrics()["unattributed_reconciliation_count"] == 0
    assert not restarted._pending_close_confirmations
    assert not restarted._active_close_mutations_by_position
