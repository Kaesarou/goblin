"""Regressions for the executor-level 2026-09-15 INTC race.

These are intentionally strict xfails until the executor prevents a second
broker mutation for a symbol while the first open is unresolved. The intent
book alone is not a sufficient concurrency boundary.
"""

from datetime import datetime, timezone

import pytest

from app.brokers.base import OpenPositionResult
from app.market.models import MarketSnapshot
from app.runtime.broker_task_runner import BrokerTaskCompletion, BrokerTaskLane
from app.v3.book import InventoryBook
from app.v3.live_execution import V3BrokerExecutor
from app.v3.models import ExecutionStyle, IntentPurpose, OrderIntent
from app.v3.persistence import InventoryEventStore
from app.v3.recovery import evaluate_restart_safety

NOW = datetime(2026, 9, 15, 13, 40, tzinfo=timezone.utc)


class DeferredRunner:
    def __init__(self):
        self.queued = []

    def submit(self, *, kind, operation, context=None, task_id=None, lane=None):
        self.queued.append((kind, operation, context, task_id, lane))
        return task_id or kind

    def drain(self):
        queued, self.queued = self.queued, []
        completions = []
        for kind, operation, context, task_id, lane in queued:
            try:
                value, error = operation(), None
            except Exception as exc:  # noqa: BLE001 - test broker completion
                value, error = None, exc
            completions.append(BrokerTaskCompletion(
                task_id=task_id or kind, kind=kind,
                lane=lane or BrokerTaskLane.STANDARD,
                context=context, value=value, error=error,
            ))
        return completions


class Broker:
    def __init__(self):
        self.open_calls = []

    def open_position(self, symbol, side, amount, stop_loss, take_profit):
        self.open_calls.append((symbol, amount))
        return OpenPositionResult(
            position_id=f"position-{len(self.open_calls)}",
            executed_entry_price=99.27,
            executed_units=3.0,
            executed_notional=amount,
        )


def _intent(intent_id, symbol="INTC"):
    return OrderIntent(
        intent_id=intent_id, purpose=IntentPurpose.INITIAL_ENTRY,
        symbol=symbol, side="BUY", notional=300.0,
        created_at=NOW, execution_style=ExecutionStyle.MARKET,
    )


def _snapshot(symbol="INTC"):
    return MarketSnapshot(
        symbol=symbol, bid=99.26, ask=99.27, last=99.27,
        timestamp=NOW, received_at=NOW,
    )


def _executor(tmp_path):
    store = InventoryEventStore(tmp_path / "v3.sqlite")
    runner, broker = DeferredRunner(), Broker()
    executor = V3BrokerExecutor(
        broker=broker, task_runner=runner, event_store=store,
        book=InventoryBook(),
        strategy_version="INVENTORY_RR5_ETORO5_V1",
        model_version=None,
    )
    return executor, broker, runner, store


@pytest.mark.xfail(strict=True, reason="executor still reserves by intent ID rather than symbol")
def test_distinct_intent_ids_cannot_enqueue_two_intc_broker_opens(tmp_path):
    executor, broker, runner, store = _executor(tmp_path)
    assert executor.schedule(_intent("first"), snapshot=_snapshot())
    assert executor.schedule(_intent("second"), snapshot=_snapshot()) is False
    assert len(runner.queued) == 1
    assert [event.payload["action_id"] for event in store.events()
            if event.event_type == "ORDER_SUBMISSION_STARTED"] == ["first"]
    assert broker.open_calls == []
    assert executor.drain() == ("first",)
    assert len(broker.open_calls) == 1
    assert evaluate_restart_safety(store.events()).safe


def test_other_symbol_is_not_blocked_by_pending_intc_open(tmp_path):
    executor, broker, runner, store = _executor(tmp_path)
    assert executor.schedule(_intent("first"), snapshot=_snapshot())
    assert executor.schedule(_intent("other", "AMD"), snapshot=_snapshot("AMD"))
    assert len(runner.queued) == 2
    assert executor.drain() == ("first", "other")
    assert [symbol for symbol, _ in broker.open_calls] == ["INTC", "AMD"]
    assert evaluate_restart_safety(store.events()).safe


@pytest.mark.xfail(strict=True, reason="executor currently permits two same-symbol queued opens")
def test_queued_second_open_never_reaches_broker_or_crashes_ledger(tmp_path):
    executor, broker, runner, store = _executor(tmp_path)
    assert executor.schedule(_intent("first"), snapshot=_snapshot())
    assert executor.schedule(_intent("second"), snapshot=_snapshot()) is False
    assert executor.drain() == ("first",)
    assert len(broker.open_calls) == 1
    assert executor.book.active_for_symbol("INTC").entry_fill_count == 1
    assert InventoryBook.from_events(store.events()).active_for_symbol("INTC").entry_fill_count == 1
