import time
from datetime import datetime, timezone

from app.runtime.broker_task_runner import BrokerTaskCompletion, BrokerTaskLane
from app.v3.book import InventoryBook
from app.v3.live_execution import (
    BROKER_RECONCILIATION_INTERVAL_SECONDS,
    V3BrokerExecutor,
)
from app.v3.persistence import InventoryEvent, InventoryEventStore

# This module verifies halt precedence before the dedicated stale threshold. Use a
# current anchor so CI wall time cannot accidentally turn the pending close stale.
NOW = datetime.now(timezone.utc)


class QueueRunner:
    def __init__(self):
        self.tasks: list[dict] = []
        self.completions: list[BrokerTaskCompletion] = []

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

    def complete(self, task: dict, *, value=None, error=None) -> None:
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


class Broker:
    def forget_position_instrument(self, position_id: str) -> None:
        return None

    def get_open_position_units(self, position_ids):
        return {str(position_id): 1.0 for position_id in position_ids}


def _accepted_close_event() -> InventoryEvent:
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
            "requested_units": 0.84,
            "full_close": False,
            "close_order_id": "order-1",
        },
        strategy_version="INVENTORY_RR5_ETORO5_V1",
    )


def _executor(tmp_path):
    book = InventoryBook()
    book.apply_entry_fill(
        inventory_id="inv",
        symbol="AAPL",
        position_id="p1",
        units=1.0,
        price=100.0,
        fee=0.0,
        filled_at=NOW,
    )
    runner = QueueRunner()
    executor = V3BrokerExecutor(
        broker=Broker(),
        task_runner=runner,
        event_store=InventoryEventStore(tmp_path / "v3.sqlite"),
        book=book,
        strategy_version="INVENTORY_RR5_ETORO5_V1",
        model_version=None,
    )
    executor.restore_pending_close_confirmations((_accepted_close_event(),))
    return executor, runner


def _last_reconciliation(runner: QueueRunner) -> dict:
    return next(
        task
        for task in reversed(runner.tasks)
        if task["kind"] == "v3_broker_reconciliation"
    )


def test_reconciliation_failure_supersedes_pending_fill_halt_and_survives_fill(tmp_path):
    executor, runner = _executor(tmp_path)
    pending = executor._pending_close_confirmations["close:p1"]
    base = time.monotonic()
    pending.next_attempt_monotonic = base + 10_000

    # First audit sees exactly the expected 84% broker reduction. Accounting must
    # wait for the economic fill price, so new risk is halted.
    executor.schedule_close_confirmation_checks(monotonic_now=base)
    executor.schedule_close_confirmation_checks(
        monotonic_now=base + BROKER_RECONCILIATION_INTERVAL_SECONDS + 0.001
    )
    first = _last_reconciliation(runner)
    runner.complete(first, value={"p1": 0.16})
    executor.drain()
    assert executor.halted_reason == "broker_quantity_reduction_pending_economic_fill"

    # A later audit detects an unrelated quantitative mismatch. This stronger
    # integrity halt must replace the temporary pending-fill reason.
    runner.tasks.clear()
    executor.schedule_close_confirmation_checks(
        monotonic_now=base + 2 * BROKER_RECONCILIATION_INTERVAL_SECONDS + 0.002
    )
    second = _last_reconciliation(runner)
    runner.complete(second, value={"p1": 0.10})
    executor.drain()
    assert executor.halted_reason == "broker_leg_reconciliation_failed"

    # Confirming the economic close must not accidentally clear the mismatch.
    pending = executor._pending_close_confirmations["close:p1"]
    executor._confirm_close(
        context=pending.context,
        exit_price=105.0,
        filled_at=NOW,
        close_order_id="order-1",
        executed_units=0.84,
    )
    assert executor.halted_reason == "broker_leg_reconciliation_failed"
    assert not executor.new_risk_allowed
