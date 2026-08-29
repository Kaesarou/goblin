import time
from datetime import datetime, timedelta, timezone

from app.runtime.broker_task_runner import BrokerTaskCompletion, BrokerTaskLane
from app.v3.book import InventoryBook
from app.v3.live_execution import (
    BROKER_RECONCILIATION_INTERVAL_SECONDS,
    CONFIRMATION_STALE_HALT_SECONDS,
    V3BrokerExecutor,
)
from app.v3.persistence import InventoryEvent, InventoryEventStore

NOW = datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)


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
    def get_open_position_units(self, position_ids):
        return {str(position_id): 1.0 for position_id in position_ids}

    def forget_position_instrument(self, position_id):
        return None


def _accepted_event() -> InventoryEvent:
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
    executor.restore_pending_close_confirmations((_accepted_event(),))
    pending = executor._pending_close_confirmations["close:p1"]
    pending.next_attempt_monotonic = time.monotonic() + 100_000
    return executor, runner


def test_pending_close_only_halts_after_explicit_stale_age(tmp_path):
    executor, _ = _executor(tmp_path)
    base = time.monotonic()

    executor.schedule_close_confirmation_checks(
        monotonic_now=base,
        utc_now=NOW + timedelta(seconds=CONFIRMATION_STALE_HALT_SECONDS - 1),
    )
    assert executor.new_risk_allowed

    executor.schedule_close_confirmation_checks(
        monotonic_now=base + 1,
        utc_now=NOW + timedelta(seconds=CONFIRMATION_STALE_HALT_SECONDS),
    )
    assert not executor.new_risk_allowed
    assert executor.halted_reason == "stale_close_confirmation"

    executor.schedule_close_confirmation_checks(
        monotonic_now=base + 2,
        utc_now=NOW + timedelta(seconds=CONFIRMATION_STALE_HALT_SECONDS + 60),
    )
    stale_events = [
        event
        for event in executor.event_store.events()
        if event.event_type == "CLOSE_CONFIRMATION_STALE"
    ]
    assert len(stale_events) == 1
    assert stale_events[0].payload["action_id"] == "close:p1"
    assert executor.confirmation_metrics()["stale_pending_count"] == 1


def test_confirmed_fill_clears_stale_halt_when_no_other_uncertainty_remains(tmp_path):
    executor, _ = _executor(tmp_path)
    base = time.monotonic()
    executor.schedule_close_confirmation_checks(
        monotonic_now=base,
        utc_now=NOW + timedelta(seconds=CONFIRMATION_STALE_HALT_SECONDS),
    )
    assert executor.halted_reason == "stale_close_confirmation"

    pending = executor._pending_close_confirmations["close:p1"]
    executor._confirm_close(
        context=pending.context,
        exit_price=105.0,
        filled_at=NOW + timedelta(minutes=15),
        close_order_id="order-1",
        executed_units=0.84,
    )

    assert executor.halted_reason is None
    assert executor.new_risk_allowed
    assert executor.book.active_for_symbol("AAPL").total_units == 0.16


def test_reconciliation_mismatch_supersedes_stale_and_is_not_cleared_by_fill(tmp_path):
    executor, runner = _executor(tmp_path)
    base = time.monotonic()
    executor.schedule_close_confirmation_checks(
        monotonic_now=base,
        utc_now=NOW + timedelta(seconds=CONFIRMATION_STALE_HALT_SECONDS),
    )
    assert executor.halted_reason == "stale_close_confirmation"

    pending = executor._pending_close_confirmations["close:p1"]
    pending.next_attempt_monotonic = base + 100_000
    executor._last_broker_reconciliation_monotonic = (
        base - BROKER_RECONCILIATION_INTERVAL_SECONDS - 1
    )
    assert executor.schedule_close_confirmation_checks(
        monotonic_now=base + 1,
        utc_now=NOW + timedelta(minutes=16),
    ) == 1
    task = runner.tasks[-1]
    assert task["kind"] == "v3_broker_reconciliation"
    runner.complete(task, value={"p1": 0.10})
    executor.drain()

    assert executor.halted_reason == "broker_leg_reconciliation_failed"

    executor._confirm_close(
        context=pending.context,
        exit_price=105.0,
        filled_at=NOW + timedelta(minutes=16),
        close_order_id="order-1",
        executed_units=0.84,
    )
    assert executor.halted_reason == "broker_leg_reconciliation_failed"
    assert not executor.new_risk_allowed


def test_reconciliation_recovery_reapplies_stale_halt_if_close_still_pending(tmp_path):
    executor, runner = _executor(tmp_path)
    base = time.monotonic()
    executor.halted_reason = "broker_leg_reconciliation_failed"
    executor._last_broker_reconciliation_status = "mismatch"
    executor._last_broker_reconciliation_monotonic = (
        base - BROKER_RECONCILIATION_INTERVAL_SECONDS - 1
    )

    assert executor.schedule_close_confirmation_checks(
        monotonic_now=base,
        utc_now=NOW + timedelta(minutes=16),
    ) == 1
    task = runner.tasks[-1]
    runner.complete(task, value={"p1": 1.0})
    executor.drain()

    assert executor.halted_reason == "stale_close_confirmation"
    assert not executor.new_risk_allowed
