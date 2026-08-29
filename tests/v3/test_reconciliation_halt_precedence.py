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


def _accepted_close_event(
    *,
    position_id: str = "p1",
    action_id: str | None = None,
    close_order_id: str | None = None,
) -> InventoryEvent:
    actual_action_id = action_id or f"close:{position_id}"
    return InventoryEvent(
        event_id=f"accepted:{position_id}",
        inventory_id="inv",
        event_type="CLOSE_SUBMISSION_ACCEPTED",
        occurred_at=NOW,
        payload={
            "action_id": actual_action_id,
            "intent_id": "close",
            "position_id": position_id,
            "symbol": "AAPL",
            "purpose": "profit_exit",
            "trigger_price": 105.0,
            "requested_units": 0.84,
            "full_close": False,
            "close_order_id": close_order_id or f"order-{position_id}",
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


def _multi_executor(tmp_path):
    book = InventoryBook()
    for position_id, price in (("p1", 100.0), ("p2", 102.0)):
        book.apply_entry_fill(
            inventory_id="inv",
            symbol="AAPL",
            position_id=position_id,
            units=1.0,
            price=price,
            fee=0.0,
            filled_at=NOW,
        )
    runner = QueueRunner()
    executor = V3BrokerExecutor(
        broker=Broker(),
        task_runner=runner,
        event_store=InventoryEventStore(tmp_path / "v3-multi.sqlite"),
        book=book,
        strategy_version="INVENTORY_RR5_ETORO5_V1",
        model_version=None,
    )
    executor.restore_pending_close_confirmations(
        (
            _accepted_close_event(position_id="p1"),
            _accepted_close_event(position_id="p2"),
        )
    )
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
        close_order_id="order-p1",
        executed_units=0.84,
    )
    assert executor.halted_reason == "broker_leg_reconciliation_failed"
    assert not executor.new_risk_allowed


def test_multi_leg_pending_economic_halt_clears_only_after_every_observed_fill(tmp_path):
    executor, runner = _multi_executor(tmp_path)
    base = time.monotonic()
    for pending in executor._pending_close_confirmations.values():
        pending.next_attempt_monotonic = base + 10_000

    # One coherent portfolio observation proves that both accepted partial closes
    # have already reduced broker quantity, while neither economic fill is known.
    executor.schedule_close_confirmation_checks(monotonic_now=base)
    executor.schedule_close_confirmation_checks(
        monotonic_now=base + BROKER_RECONCILIATION_INTERVAL_SECONDS + 0.001
    )
    reconciliation = _last_reconciliation(runner)
    runner.complete(reconciliation, value={"p1": 0.16, "p2": 0.16})
    executor.drain()

    assert executor.halted_reason == "broker_quantity_reduction_pending_economic_fill"
    assert not executor.new_risk_allowed
    assert executor.confirmation_metrics()["pending_economic_fill_action_ids"] == [
        "close:p1",
        "close:p2",
    ]

    first = executor._pending_close_confirmations["close:p1"]
    executor._confirm_close(
        context=first.context,
        exit_price=105.0,
        filled_at=NOW,
        close_order_id="order-p1",
        executed_units=0.84,
    )

    # Confirming one leg resolves only that leg. The second observed broker
    # reduction is still economically unknown, so BUY authority must stay blocked.
    assert executor.halted_reason == "broker_quantity_reduction_pending_economic_fill"
    assert not executor.new_risk_allowed
    assert executor.confirmation_metrics()["pending_economic_fill_action_ids"] == [
        "close:p2"
    ]

    second = executor._pending_close_confirmations["close:p2"]
    executor._confirm_close(
        context=second.context,
        exit_price=106.0,
        filled_at=NOW,
        close_order_id="order-p2",
        executed_units=0.84,
    )

    assert executor.confirmation_metrics()["pending_economic_fill_count"] == 0
    assert executor.halted_reason is None
    assert executor.new_risk_allowed
