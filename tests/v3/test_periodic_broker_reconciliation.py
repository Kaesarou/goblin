import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from app.brokers.base import BrokerCloseExecution
from app.runtime.broker_task_runner import BrokerTaskCompletion, BrokerTaskLane
from app.v3.book import InventoryBook
from app.v3.live_execution import (
    BROKER_RECONCILIATION_INTERVAL_SECONDS,
    V3BrokerExecutor,
)
from app.v3.persistence import InventoryEvent, InventoryEventStore

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
    def __init__(self, units=None):
        self.units = dict(units or {})
        self.remembered: list[tuple[str, str]] = []

    def remember_position_instrument(self, position_id: str, symbol: str) -> None:
        self.remembered.append((position_id, symbol))

    def get_open_position_units(self, position_ids):
        return {
            str(position_id): self.units.get(str(position_id), 0.0)
            for position_id in position_ids
        }

    def get_close_execution(self, close_order_id, position_id):
        return None


def _book() -> InventoryBook:
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
    return book


def _executor(tmp_path, *, units=None):
    runner = QueueRunner()
    broker = Broker(units=units)
    executor = V3BrokerExecutor(
        broker=broker,
        task_runner=runner,
        event_store=InventoryEventStore(tmp_path / "v3.sqlite"),
        book=_book(),
        strategy_version="INVENTORY_RR5_ETORO5_V1",
        model_version=None,
    )
    return executor, runner, broker


def _after_interval(base: float, multiple: int = 1) -> float:
    # Keep a comfortable margin beyond the cadence boundary. The previous
    # +0.001 float equality was flaky on GitHub-hosted runners.
    return base + multiple * BROKER_RECONCILIATION_INTERVAL_SECONDS + 1.0


def _reconciliation_task(runner: QueueRunner) -> dict:
    return next(
        task for task in runner.tasks if task["kind"] == "v3_broker_reconciliation"
    )


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


def test_periodic_reconciliation_uses_shared_query_lane(tmp_path):
    executor, runner, _ = _executor(tmp_path, units={"p1": 1.0})
    base = time.monotonic()

    assert executor.schedule_close_confirmation_checks(monotonic_now=base) == 0
    assert executor.schedule_close_confirmation_checks(
        monotonic_now=_after_interval(base)
    ) == 1

    task = _reconciliation_task(runner)
    assert task["lane"] is BrokerTaskLane.QUERY
    assert executor.schedule_close_confirmation_checks(
        monotonic_now=_after_interval(base) + 1
    ) == 0


def test_matching_periodic_reconciliation_keeps_new_risk_enabled(tmp_path):
    executor, runner, _ = _executor(tmp_path, units={"p1": 1.0})
    base = time.monotonic()
    executor.schedule_close_confirmation_checks(monotonic_now=base)
    executor.schedule_close_confirmation_checks(monotonic_now=_after_interval(base))
    task = _reconciliation_task(runner)
    runner.complete(task, value={"p1": 1.0})

    assert executor.drain() == ()
    assert executor.new_risk_allowed
    metrics = executor.confirmation_metrics()["broker_reconciliation"]
    assert metrics["attempts"] == 1
    assert metrics["last_status"] == "ok"
    assert metrics["last_issues"] == []


def test_periodic_broker_unit_increase_halts_then_exact_match_recovers(tmp_path):
    executor, runner, _ = _executor(tmp_path, units={"p1": 1.5})
    base = time.monotonic()
    executor.schedule_close_confirmation_checks(monotonic_now=base)
    executor.schedule_close_confirmation_checks(monotonic_now=_after_interval(base))
    first = _reconciliation_task(runner)
    runner.complete(first, value={"p1": 1.5})
    executor.drain()

    assert not executor.new_risk_allowed
    assert executor.halted_reason == "broker_leg_reconciliation_failed"
    metrics = executor.confirmation_metrics()["broker_reconciliation"]
    assert metrics["mismatches"] == 1
    assert metrics["last_status"] == "mismatch"
    assert metrics["last_issues"] == ["p1:book=1:broker=1.5"]

    runner.tasks.clear()
    last_scheduled = executor._last_broker_reconciliation_monotonic
    assert last_scheduled is not None
    executor.schedule_close_confirmation_checks(
        monotonic_now=(
            last_scheduled + BROKER_RECONCILIATION_INTERVAL_SECONDS + 1.0
        )
    )
    second = _reconciliation_task(runner)
    runner.complete(second, value={"p1": 1.0})
    executor.drain()

    assert executor.new_risk_allowed
    assert executor.halted_reason is None
    metrics = executor.confirmation_metrics()["broker_reconciliation"]
    assert metrics["recovered"] == 1
    assert metrics["last_status"] == "ok"


def test_pending_close_quantity_reduction_reconciles_book_and_releases_mutation(tmp_path):
    executor, runner, _ = _executor(tmp_path, units={"p1": 0.16})
    executor.restore_pending_close_confirmations((_accepted_close_event(),))
    pending = next(iter(executor._pending_close_confirmations.values()))
    base = time.monotonic()
    pending.next_attempt_monotonic = base + 10_000

    executor.schedule_close_confirmation_checks(monotonic_now=base)
    executor.schedule_close_confirmation_checks(monotonic_now=_after_interval(base))
    task = _reconciliation_task(runner)
    runner.complete(task, value={"p1": 0.16})
    executor.drain()

    assert executor.halted_reason is None
    inventory = executor.book.active_for_symbol("AAPL")
    assert inventory is not None
    assert inventory.total_units == 0.16
    assert inventory.total_notional == 16.0
    metrics = executor.confirmation_metrics()
    assert metrics["broker_reconciliation"]["mismatches"] == 0
    assert metrics["broker_reconciliation"]["last_status"] == "pending_economic_fill"
    assert metrics["broker_quantity_reductions_observed"] == 1
    event_types = [event.event_type for event in executor.event_store.events()]
    assert "BROKER_QUANTITY_RECONCILED" in event_types


def test_active_mutation_confirmation_due_has_priority_over_periodic_reconciliation(tmp_path):
    executor, runner, _ = _executor(tmp_path, units={"p1": 1.0})
    executor.restore_pending_close_confirmations((_accepted_close_event(),))
    pending = next(iter(executor._pending_close_confirmations.values()))
    base = time.monotonic()
    pending.next_attempt_monotonic = base
    executor._last_broker_reconciliation_monotonic = (
        base - BROKER_RECONCILIATION_INTERVAL_SECONDS - 1.0
    )

    assert executor.schedule_close_confirmation_checks(monotonic_now=base) == 1
    assert runner.tasks[-1]["kind"] == "v3_close_execution_lookup"
    assert runner.tasks[-1]["lane"] is BrokerTaskLane.QUERY
    assert not any(
        task["kind"] == "v3_broker_reconciliation" for task in runner.tasks
    )
    assert pending.mutation_active
    metrics = executor.confirmation_metrics()
    assert metrics["mutation_confirmation_attempts"] == 1
    assert metrics["economics_only_confirmation_attempts"] == 0
    assert metrics["broker_reconciliation"]["attempts"] == 0


def _economics_executor(tmp_path, monkeypatch, *, count=1):
    clock = [100.0]
    monkeypatch.setattr("app.v3.live_execution.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("app.v3.live_execution._utc_now", lambda: NOW + timedelta(seconds=clock[0] - 100))
    positions = [f"p{index}" for index in range(1, count + 1)]
    executor, runner, broker = _executor(tmp_path, units={position: 0.16 for position in positions})
    for position in positions[1:]:
        executor.book.apply_entry_fill(
            inventory_id="inv", symbol="AAPL", position_id=position,
            units=1, price=100, fee=0, filled_at=NOW,
        )
    accepted = _accepted_close_event()
    executor.restore_pending_close_confirmations(tuple(
        replace(accepted, event_id=f"accepted:{position}", payload={
            **accepted.payload, "action_id": f"close:{position}",
            "position_id": position, "close_order_id": f"order:{position}",
            "pre_close_units": 1.0,
        }) for position in positions
    ))
    assert executor.verify_known_broker_legs() == ()
    for pending in executor._pending_close_confirmations.values():
        assert pending.quantity_resolved and pending.attribution_confident
        assert pending.economics_pending and not pending.mutation_active
        pending.next_attempt_monotonic = clock[0]
        pending.next_attempt_at = NOW
        pending.attempt_count = 12
    assert executor.new_risk_allowed
    return executor, runner, broker, clock


def test_economics_only_confirmation_does_not_preempt_due_broker_reconciliation(tmp_path, monkeypatch):
    executor, runner, broker, clock = _economics_executor(tmp_path, monkeypatch)
    clock[0] = _after_interval(clock[0])
    pending = executor._pending_close_confirmations["close:p1"]

    assert executor.schedule_close_confirmation_checks(monotonic_now=clock[0]) == 1
    reconciliation = runner.tasks[-1]
    assert reconciliation["kind"] == "v3_broker_reconciliation"
    assert reconciliation["lane"] is BrokerTaskLane.QUERY
    assert pending.attempt_count == 12
    metrics = executor.confirmation_metrics()
    assert metrics["mutation_confirmation_attempts"] == 0
    assert metrics["economics_only_confirmation_attempts"] == 0
    assert metrics["broker_reconciliation"]["attempts"] == 2  # Startup plus periodic.
    runner.complete(reconciliation, value=broker.units)
    executor.drain()

    assert executor.schedule_close_confirmation_checks(monotonic_now=clock[0]) == 1
    lookup = runner.tasks[-1]
    assert lookup["kind"] == "v3_close_execution_lookup"
    assert lookup["context"] is pending
    runner.complete(lookup, value=BrokerCloseExecution(
        "p1", pending.close_order_id, 105.0, NOW, 0.84, None, None, {},
    ))
    assert executor.drain() == ("close",)
    assert not executor._pending_close_confirmations
    assert not executor.runtime_state_store.load_close_retries()
    assert executor.book.active_for_symbol("AAPL").total_units == 0.16
    assert executor.new_risk_allowed
    metrics = executor.confirmation_metrics()
    assert metrics["economics_only_confirmation_attempts"] == 1
    assert metrics["mutation_confirmation_attempts"] == 0
    assert metrics["broker_reconciliation"]["attempts"] == 2


def test_due_reconciliation_interrupts_economics_backlog_without_starvation(tmp_path, monkeypatch):
    executor, runner, broker, clock = _economics_executor(tmp_path, monkeypatch, count=4)
    action_ids = set(executor._pending_close_confirmations)
    assert executor.schedule_close_confirmation_checks(monotonic_now=clock[0]) == 1
    first = runner.tasks[-1]
    assert first["kind"] == "v3_close_execution_lookup"
    # Reconciliation becomes due during this GET. Do not queue more QUERY work.
    clock[0] = _after_interval(clock[0])
    assert executor.schedule_close_confirmation_checks(monotonic_now=clock[0]) == 0
    assert len(runner.tasks) == 1
    runner.complete(first, value=None)
    executor.drain()

    assert executor.schedule_close_confirmation_checks(monotonic_now=clock[0]) == 1
    reconciliation = runner.tasks[-1]
    assert reconciliation["kind"] == "v3_broker_reconciliation"
    assert executor.schedule_close_confirmation_checks(monotonic_now=clock[0]) == 0
    runner.complete(reconciliation, value=broker.units)
    executor.drain()

    for _ in range(3):
        assert executor.schedule_close_confirmation_checks(monotonic_now=clock[0]) == 1
        task = runner.tasks[-1]
        assert task["kind"] == "v3_close_execution_lookup"
        runner.complete(task, value=None)
        executor.drain()
    assert [task["kind"] for task in runner.tasks] == [
        "v3_close_execution_lookup", "v3_broker_reconciliation",
        "v3_close_execution_lookup", "v3_close_execution_lookup", "v3_close_execution_lookup",
    ]
    served = {task["context"].context.action_id for task in runner.tasks
              if task["kind"] == "v3_close_execution_lookup"}
    assert served == action_ids == set(executor._pending_close_confirmations)
    assert all(task["lane"] is BrokerTaskLane.QUERY for task in runner.tasks)
    for pending in executor._pending_close_confirmations.values():
        assert pending.next_attempt_monotonic == clock[0] + 3600
        assert pending.attempt_count == 13
    assert executor.schedule_close_confirmation_checks(monotonic_now=clock[0]) == 0
    metrics = executor.confirmation_metrics()
    assert metrics["attempts"] == metrics["economics_only_confirmation_attempts"] == 4
    assert metrics["mutation_confirmation_attempts"] == 0
    assert metrics["broker_reconciliation"]["attempts"] == 2
    assert executor.new_risk_allowed


def test_due_active_mutation_preempts_older_economics_and_due_reconciliation(tmp_path, monkeypatch):
    executor, runner, _, clock = _economics_executor(tmp_path, monkeypatch)
    executor.book.apply_entry_fill(
        inventory_id="inv", symbol="AAPL", position_id="p2",
        units=1, price=100, fee=0, filled_at=NOW,
    )
    accepted = _accepted_close_event()
    executor.restore_pending_close_confirmations((replace(accepted, payload={
        **accepted.payload, "action_id": "active:p2", "position_id": "p2",
    }),))
    clock[0] = _after_interval(clock[0])
    executor._pending_close_confirmations["active:p2"].next_attempt_monotonic = clock[0]
    assert executor.schedule_close_confirmation_checks(monotonic_now=clock[0]) == 1
    assert runner.tasks[-1]["kind"] == "v3_close_execution_lookup"
    assert runner.tasks[-1]["context"].context.action_id == "active:p2"
    metrics = executor.confirmation_metrics()
    assert metrics["mutation_confirmation_attempts"] == 1
    assert metrics["economics_only_confirmation_attempts"] == 0
    assert metrics["broker_reconciliation"]["attempts"] == 1


def test_inflight_book_change_discards_stale_reconciliation_result(tmp_path):
    executor, runner, _ = _executor(tmp_path, units={"p1": 1.0})
    base = time.monotonic()
    executor.schedule_close_confirmation_checks(monotonic_now=base)
    executor.schedule_close_confirmation_checks(monotonic_now=_after_interval(base))
    task = _reconciliation_task(runner)

    executor.book.apply_entry_fill(
        inventory_id="inv",
        symbol="AAPL",
        position_id="p2",
        units=0.5,
        price=90.0,
        fee=0.0,
        filled_at=NOW,
    )
    runner.complete(task, value={"p1": 1.0})
    executor.drain()

    assert executor.new_risk_allowed
    metrics = executor.confirmation_metrics()["broker_reconciliation"]
    assert metrics["stale_results"] == 1
    assert metrics["last_status"] == "stale"
