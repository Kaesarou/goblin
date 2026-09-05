from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.brokers.base import BrokerCloseExecution, ClosePositionSubmission
from app.market.models import MarketSnapshot
from app.runtime.broker_task_runner import BrokerTaskCompletion, BrokerTaskLane
from app.v3.book import InventoryBook
from app.v3.live_execution import V3BrokerExecutor, _confirmation_backoff_seconds
from app.v3.operator_reconciliation import acknowledge_broker_reconciliation
from app.v3.persistence import InventoryEventStore
from app.v3.recovery import evaluate_restart_safety
from app.v3.state_store import CloseRetryState
from tests.v3.test_partial_close_restart_safety import QueueRunner, _entry_event, _close_intent

NOW = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)


class Broker:
    def __init__(self):
        self.units = 1.0
        self.orders = []

    def get_open_position_units(self, ids):
        return {pid: self.units for pid in ids}

    def remember_position_instrument(self, *args):
        pass

    def forget_position_instrument(self, *args):
        pass

    def close_position(self, position_id, units_to_deduct=None):
        self.orders.append((position_id, units_to_deduct))
        return ClosePositionSubmission(position_id, f"order-{len(self.orders)}", None, NOW, NOW, {})


def make_executor(tmp_path, *, seed=True, broker=None):
    store = InventoryEventStore(tmp_path / "actions.sqlite")
    if seed:
        entry = _entry_event()
        store.append(replace(entry, occurred_at=NOW-timedelta(days=1),
                             payload={**entry.payload, "price": 1000.0}))
    events = store.events()
    executor = V3BrokerExecutor(broker=broker or Broker(), task_runner=QueueRunner(),
                                event_store=store, book=InventoryBook.from_events(events),
                                strategy_version="INVENTORY_RR5_ETORO5_V1", model_version=None)
    executor.restore_pending_close_confirmations(events)
    return executor


def submit_close(executor, name):
    inventory = executor.book.active_for_symbol("AAPL")
    intent = _close_intent(inventory, name)
    quote = MarketSnapshot("AAPL", 1100, 1101, 1100, NOW)
    assert executor.schedule(intent, snapshot=quote)
    kind, operation, context, task_id, lane = executor.task_runner.submissions[-1]
    executor._handle_close_submission(BrokerTaskCompletion(task_id, kind, lane, context, operation()))
    return executor._pending_close_confirmations[context.action_id]


def confirm(executor, pending, units, price):
    return executor._handle_close_lookup(BrokerTaskCompletion(
        "lookup", "v3_close_execution_lookup", BrokerTaskLane.QUERY, pending,
        BrokerCloseExecution("p1", pending.close_order_id, price, NOW, units, None, None, {}),
    ))


def test_two_sequential_partials_late_economics_and_exact_restart(tmp_path):
    executor = make_executor(tmp_path)
    first = submit_close(executor, "A")
    assert first.context.pre_close_units == 1.0
    executor.broker.units = 0.16
    assert executor.verify_known_broker_legs() == ()
    assert first.quantity_resolved and first.attribution_confident
    assert not first.mutation_active
    assert not executor._active_close_mutations_by_position
    assert executor.new_risk_allowed
    restarted = make_executor(tmp_path, seed=False, broker=executor.broker)
    first = restarted._pending_close_confirmations["A:p1"]
    assert first.quantity_resolved and not first.mutation_active
    second = submit_close(restarted, "B")
    assert second.context.pre_close_units == 0.16
    assert second.context.requested_units == pytest.approx(0.1344)
    assert not second.context.full_close
    assert restarted.broker.orders == [("p1", 0.84), ("p1", pytest.approx(0.1344))]
    assert restarted._broker_reconciliation_context().expectations[0].pending_requested_units == pytest.approx(0.1344)
    restarted.broker.units = 0.0256
    assert restarted.verify_known_broker_legs() == ()
    assert second.quantity_resolved and not second.mutation_active
    assert confirm(restarted, first, 0.84, 1100)
    assert restarted.book.active_for_symbol("AAPL").total_units == 0.0256
    assert confirm(restarted, second, 0.1344, 1200)
    before = restarted.book.inventories
    assert not confirm(restarted, first, 0.84, 1100)
    assert restarted.book.inventories == before
    assert not restarted.runtime_state_store.load_close_retries()
    assert InventoryBook.from_events(restarted.event_store.events()).inventories == before
    assert before[0].realized_pnl == pytest.approx(110.88)


def test_late_first_confirmation_cannot_unlock_second_active_mutation(tmp_path):
    executor = make_executor(tmp_path)
    first = submit_close(executor, "A")
    executor.broker.units = 0.16
    executor.verify_known_broker_legs()
    # The same action cannot be submitted again after its quantity is resolved.
    inventory = executor.book.active_for_symbol("AAPL")
    quote = MarketSnapshot("AAPL", 1100, 1101, 1100, NOW)
    assert not executor.schedule(_close_intent(inventory, "A"), snapshot=quote)
    submit_close(executor, "B")
    assert confirm(executor, first, 0.84, 1100)
    assert executor._active_close_mutations_by_position == {"p1": "B:p1"}
    assert not executor.schedule(_close_intent(inventory, "C"), snapshot=quote)
    assert executor.book.active_for_symbol("AAPL").total_units == 0.16


@pytest.mark.parametrize("active,broker_units", [(False, 0.9), (True, 0.25), (True, 0.1597)])
def test_unattributed_or_mismatched_current_action_stays_fail_closed(tmp_path, active, broker_units):
    executor = make_executor(tmp_path)
    if active:
        submit_close(executor, "A")
    executor.broker.units = broker_units
    executor.verify_known_broker_legs()
    assert not executor.new_risk_allowed
    assert executor.halted_reason == "broker_quantity_reduction_unattributed"
    assert executor.confirmation_metrics()["unattributed_reconciliation_count"] == 1
    restarted = make_executor(tmp_path, seed=False, broker=executor.broker)
    assert not restarted.new_risk_allowed
    assert restarted.book.inventories == executor.book.inventories
    if active:
        assert restarted._active_close_mutations_by_position == {"p1": "A:p1"}


def test_operator_acknowledgment_is_read_only_audited_and_restart_safe(tmp_path):
    executor = make_executor(tmp_path)
    submit_close(executor, "A")
    executor.broker.units = 0.25
    executor.verify_known_broker_legs()
    before, events = executor.book.inventories, executor.event_store.events()
    args = dict(event_store=executor.event_store, state_store=executor.runtime_state_store,
                broker=executor.broker, position_id="p1", expected_broker_units=0.25,
                reason="Operator verified external reduction; abandon ambiguous close economics")
    assert not acknowledge_broker_reconciliation(**args)["applied"]
    assert executor.event_store.events() == events
    assert acknowledge_broker_reconciliation(**args, apply=True)["abandoned_action_ids"] == ["A:p1"]
    assert len(executor.broker.orders) == 1
    audit = executor.event_store.events()[-1]
    assert audit.event_type == "BROKER_RECONCILIATION_ACKNOWLEDGED"
    assert "fee" not in audit.payload and "realized_pnl" not in audit.payload
    assert evaluate_restart_safety(executor.event_store.events()).safe
    restarted = make_executor(tmp_path, seed=False, broker=executor.broker)
    assert restarted.new_risk_allowed
    assert restarted.book.inventories == before
    assert not restarted._active_close_mutations_by_position
    assert not restarted._pending_close_confirmations
    assert not restarted.runtime_state_store.load_close_retries()


@pytest.mark.parametrize("actual", [None, 0.251, float("nan"), -1, True])
def test_operator_rejects_unproven_current_units(tmp_path, actual):
    executor = make_executor(tmp_path)
    executor.broker.units = 0.25
    executor.verify_known_broker_legs()
    executor.broker.units = actual
    with pytest.raises(ValueError):
        acknowledge_broker_reconciliation(event_store=executor.event_store,
            state_store=executor.runtime_state_store, broker=executor.broker,
            position_id="p1", expected_broker_units=0.25, reason="verify", apply=True)
    assert not any(e.event_type == "BROKER_RECONCILIATION_ACKNOWLEDGED" for e in executor.event_store.events())


def test_none_retries_exponentially_and_persists_utc_deadline(tmp_path, monkeypatch):
    executor = make_executor(tmp_path)
    pending = submit_close(executor, "A")
    monkeypatch.setattr("app.v3.live_execution.time.monotonic", lambda: 100.0)
    monkeypatch.setattr("app.v3.live_execution._utc_now", lambda: NOW)
    for attempt, delay in [(1, 15), (2, 30), (3, 60)]:
        pending.attempt_count = attempt
        executor._handle_close_lookup(BrokerTaskCompletion("lookup", "v3_close_execution_lookup", BrokerTaskLane.QUERY, pending))
        assert pending.next_attempt_monotonic == 100 + delay
        assert pending.next_attempt_at == NOW + timedelta(seconds=delay)
    assert executor.runtime_state_store.load_close_retries()["A:p1"].last_result_state == "execution_unavailable"
    restarted = make_executor(tmp_path, seed=False, broker=executor.broker)
    recovered = restarted._pending_close_confirmations["A:p1"]
    assert recovered.attempt_count == 3 and recovered.next_attempt_monotonic == 160
    assert not executor.schedule_close_confirmation_checks(monotonic_now=120, utc_now=NOW)


def test_retry_restart_rebuilds_remaining_delay_and_economics_cadence(tmp_path, monkeypatch):
    executor = make_executor(tmp_path)
    submit_close(executor, "A")
    executor.broker.units = 0.16
    executor.verify_known_broker_legs()
    executor.runtime_state_store.save_close_retry(CloseRetryState(
        "A:p1", 12, NOW+timedelta(minutes=7), "Timeout", 504, "error"))
    monkeypatch.setattr("app.v3.live_execution._utc_now", lambda: NOW+timedelta(minutes=2))
    monkeypatch.setattr("app.v3.live_execution.time.monotonic", lambda: 100.0)
    restarted = make_executor(tmp_path, seed=False, broker=executor.broker)
    recovered = restarted._pending_close_confirmations["A:p1"]
    assert recovered.next_attempt_monotonic == 400 and recovered.attempt_count == 12
    restarted._handle_close_lookup(BrokerTaskCompletion("lookup", "v3_close_execution_lookup", BrokerTaskLane.QUERY, recovered))
    assert recovered.next_attempt_monotonic == 3700
    assert _confirmation_backoff_seconds(12) == 300
    assert _confirmation_backoff_seconds(12, economics_only=True) == 3600
    restarted._refresh_stale_confirmation_halt(NOW+timedelta(days=1))
    assert restarted.new_risk_allowed


def test_interrupted_submission_keeps_leg_locked_after_restart(tmp_path):
    executor = make_executor(tmp_path)
    inventory = executor.book.active_for_symbol("AAPL")
    assert executor.schedule(_close_intent(inventory, "A"), snapshot=MarketSnapshot("AAPL", 1100, 1101, 1100, NOW))
    restarted = make_executor(tmp_path, seed=False)
    assert restarted._active_close_mutations_by_position == {"p1": "A:p1"}
    assert not evaluate_restart_safety(restarted.event_store.events()).safe
