import time
from datetime import datetime, timezone

from app.runtime.broker_task_runner import BrokerTaskCompletion, BrokerTaskLane
from app.v3.book import InventoryBook
from app.v3.live_execution import (
    BROKER_RECONCILIATION_INTERVAL_SECONDS,
    V3BrokerExecutor,
)
from app.v3.persistence import InventoryEvent, InventoryEventStore

# These tests exercise non-stale reconciliation semantics. Use a fresh wall-clock
# anchor so the separate 15-minute stale-confirmation policy cannot contaminate
# them when CI happens to run long after a hard-coded timestamp.
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
    # The scheduler receives time.monotonic() floats. Test the semantic contract
    # (the interval has elapsed) rather than exact IEEE-754 equality at +60.0.
    return base + multiple * BROKER_RECONCILIATION_INTERVAL_SECONDS + 0.001


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


def test_periodic_mismatch_halts_then_exact_match_recovers(tmp_path):
    executor, runner, _ = _executor(tmp_path, units={"p1": 0.5})
    base = time.monotonic()
    executor.schedule_close_confirmation_checks(monotonic_now=base)
    executor.schedule_close_confirmation_checks(monotonic_now=_after_interval(base))
    first = _reconciliation_task(runner)
    runner.complete(first, value={"p1": 0.5})
    executor.drain()

    assert not executor.new_risk_allowed
    assert executor.halted_reason == "broker_leg_reconciliation_failed"
    metrics = executor.confirmation_metrics()["broker_reconciliation"]
    assert metrics["mismatches"] == 1
    assert metrics["last_status"] == "mismatch"
    assert metrics["last_issues"] == ["p1:book=1:broker=0.5"]

    runner.tasks.clear()
    executor.schedule_close_confirmation_checks(
        monotonic_now=_after_interval(base, 2)
    )
    second = _reconciliation_task(runner)
    runner.complete(second, value={"p1": 1.0})
    executor.drain()

    assert executor.new_risk_allowed
    assert executor.halted_reason is None
    metrics = executor.confirmation_metrics()["broker_reconciliation"]
    assert metrics["recovered"] == 1
    assert metrics["last_status"] == "ok"


def test_pending_close_quantity_reduction_halts_for_economic_fill_not_mismatch(tmp_path):
    executor, runner, _ = _executor(tmp_path, units={"p1": 0.16})
    executor.restore_pending_close_confirmations((_accepted_close_event(),))
    # Keep confirmation in backoff so the periodic quantity audit can use QUERY.
    pending = next(iter(executor._pending_close_confirmations.values()))
    base = time.monotonic()
    pending.next_attempt_monotonic = base + 10_000

    executor.schedule_close_confirmation_checks(monotonic_now=base)
    executor.schedule_close_confirmation_checks(monotonic_now=_after_interval(base))
    task = _reconciliation_task(runner)
    runner.complete(task, value={"p1": 0.16})
    executor.drain()

    assert executor.halted_reason == "broker_quantity_reduction_pending_economic_fill"
    metrics = executor.confirmation_metrics()
    assert metrics["broker_reconciliation"]["mismatches"] == 0
    assert metrics["broker_reconciliation"]["last_status"] == "pending_economic_fill"
    assert metrics["broker_quantity_reductions_observed"] == 1
    event_types = [event.event_type for event in executor.event_store.events()]
    assert "BROKER_QUANTITY_REDUCTION_OBSERVED" in event_types


def test_confirmation_due_has_priority_over_periodic_reconciliation(tmp_path):
    executor, runner, _ = _executor(tmp_path, units={"p1": 1.0})
    executor.restore_pending_close_confirmations((_accepted_close_event(),))
    pending = next(iter(executor._pending_close_confirmations.values()))
    base = time.monotonic()
    pending.next_attempt_monotonic = base
    executor._last_broker_reconciliation_monotonic = (
        base - BROKER_RECONCILIATION_INTERVAL_SECONDS - 0.001
    )

    assert executor.schedule_close_confirmation_checks(monotonic_now=base) == 1
    assert runner.tasks[-1]["kind"] == "v3_close_execution_lookup"
    assert runner.tasks[-1]["lane"] is BrokerTaskLane.QUERY
    assert not any(
        task["kind"] == "v3_broker_reconciliation" for task in runner.tasks
    )


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
