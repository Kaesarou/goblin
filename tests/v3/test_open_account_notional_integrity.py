"""Broker notional inconsistencies must never understate inventory risk."""

from datetime import datetime, timezone

from app.brokers.base import OpenPositionResult
from app.market.models import MarketSnapshot
from app.runtime.broker_task_runner import BrokerTaskCompletion
from app.v3.book import InventoryBook
from app.v3.live_execution import V3BrokerExecutor
from app.v3.models import ExecutionStyle, IntentPurpose, OrderIntent
from app.v3.persistence import InventoryEventStore

NOW = datetime(2026, 9, 15, 13, 40, tzinfo=timezone.utc)


class Runner:
    def __init__(self):
        self.queue = []

    def submit(self, *, kind, task_id, operation, context, lane):
        self.queue.append((kind, task_id, operation, context, lane))

    def drain(self):
        pending, self.queue = self.queue, []
        return [BrokerTaskCompletion(
            task_id=task_id, kind=kind, lane=lane,
            context=context, value=operation(),
        ) for kind, task_id, operation, context, lane in pending]


class Broker:
    def __init__(self, reported):
        self.reported = reported
        self.calls = 0

    def open_position(self, symbol, side, amount, stop_loss, take_profit):
        self.calls += 1
        return OpenPositionResult(
            position_id="position-1", executed_entry_price=100.0,
            executed_units=3.0, executed_notional=self.reported,
        )


def _exercise(tmp_path, reported):
    store = InventoryEventStore(tmp_path / "notional.sqlite")
    broker = Broker(reported)
    executor = V3BrokerExecutor(
        broker=broker, task_runner=Runner(), event_store=store,
        book=InventoryBook(),
        strategy_version="INVENTORY_RR5_ETORO5_V1", model_version=None,
    )
    intent = OrderIntent(
        intent_id="open-1", purpose=IntentPurpose.INITIAL_ENTRY,
        symbol="INTC", side="BUY", notional=300.0,
        created_at=NOW, execution_style=ExecutionStyle.MARKET,
    )
    snapshot = MarketSnapshot(
        symbol="INTC", bid=99.0, ask=100.0, last=100.0,
        timestamp=NOW, received_at=NOW,
    )
    assert executor.schedule(intent, snapshot=snapshot)
    assert executor.drain() == ("open-1",)
    return executor, broker, store, intent, snapshot


def test_one_dollar_broker_notional_books_requested_amount_and_blocks_new_risk(tmp_path):
    executor, broker, store, intent, snapshot = _exercise(tmp_path, 1.0)
    events = store.events()
    fill = next(event for event in events if event.event_type == "ENTRY_FILLED")
    anomaly = next(event for event in events
                   if event.event_type == "OPEN_ACCOUNT_NOTIONAL_MISMATCH")
    assert fill.payload["notional"] == 300.0
    assert fill.payload["notional_source"] == "requested_account_currency"
    assert anomaly.payload["reported_account_notional"] == 1.0
    assert executor.book.active_for_symbol("INTC").total_notional == 300.0
    assert not executor.new_risk_allowed
    assert not executor.schedule(intent, snapshot=snapshot)
    assert broker.calls == 1

    restarted = V3BrokerExecutor(
        broker=Broker(300.0), task_runner=Runner(), event_store=store,
        book=InventoryBook.from_events(events),
        strategy_version="INVENTORY_RR5_ETORO5_V1", model_version=None,
    )
    assert not restarted.new_risk_allowed
    assert restarted.halted_reason == "open_account_notional_mismatch_at_restart"


def test_large_broker_notional_keeps_larger_risk_and_halts(tmp_path):
    executor, _broker, store, _intent, _snapshot = _exercise(tmp_path, 700.0)
    fill = next(event for event in store.events() if event.event_type == "ENTRY_FILLED")
    assert fill.payload["notional"] == 700.0
    assert not executor.new_risk_allowed


def test_consistent_broker_amount_remains_authoritative(tmp_path):
    executor, _broker, store, _intent, _snapshot = _exercise(tmp_path, 290.0)
    fill = next(event for event in store.events() if event.event_type == "ENTRY_FILLED")
    assert fill.payload["notional"] == 290.0
    assert fill.payload["notional_source"] == "broker_confirmed_account_currency"
    assert not any(event.event_type == "OPEN_ACCOUNT_NOTIONAL_MISMATCH"
                   for event in store.events())
    assert executor.new_risk_allowed
