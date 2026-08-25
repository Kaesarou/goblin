from datetime import datetime, timezone

from app.brokers.base import ClosePositionSubmissionUnknownError
from app.brokers.paper.paper_broker import PaperBrokerClient
from app.market.models import MarketSnapshot
from app.runtime.broker_task_runner import BrokerTaskCompletion, BrokerTaskLane
from app.v3.book import InventoryBook
from app.v3.live_execution import V3BrokerExecutor
from app.v3.models import ExecutionStyle, IntentPurpose, OrderIntent
from app.v3.persistence import InventoryEventStore
from app.v3.recovery import evaluate_restart_safety

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


class ImmediateRunner:
    def __init__(self):
        self.items = []

    def submit(self, *, kind, operation, context=None, task_id=None, lane=None):
        try:
            value = operation()
            error = None
        except Exception as exc:  # noqa: BLE001
            value = None
            error = exc
        self.items.append(
            BrokerTaskCompletion(
                task_id=task_id or kind,
                kind=kind,
                lane=lane or BrokerTaskLane.STANDARD,
                context=context,
                value=value,
                error=error,
            )
        )
        return task_id or kind

    def drain(self):
        result = list(self.items)
        self.items.clear()
        return result


class UnknownClosePaperBroker(PaperBrokerClient):
    def close_position(self, position_id):
        raise ClosePositionSubmissionUnknownError(
            position_id=position_id,
            submitted_at=NOW,
            cause=RuntimeError("connection lost after close submit"),
        )


def _snapshot(price=100.0):
    return MarketSnapshot("AAPL", price - 0.1, price + 0.1, price, NOW)


def _open_intent():
    return OrderIntent(
        "i1",
        IntentPurpose.INITIAL_ENTRY,
        "AAPL",
        "BUY",
        100.0,
        NOW,
        ExecutionStyle.MARKET,
    )


def _full_close_intent(inventory):
    return OrderIntent(
        "c1",
        IntentPurpose.PROFIT_EXIT,
        "AAPL",
        "SELL",
        84.0,
        NOW,
        ExecutionStyle.MARKET,
        inventory_id=inventory.inventory_id,
        reduce_only=True,
        metadata={"close_fraction_of_units": 1.0},
    )


def test_paper_entry_is_ledgered_and_rebuildable(tmp_path):
    store = InventoryEventStore(tmp_path / "v3.sqlite")
    book = InventoryBook()
    executor = V3BrokerExecutor(
        broker=PaperBrokerClient(equity=100_000),
        task_runner=ImmediateRunner(),
        event_store=store,
        book=book,
        strategy_version="RR5",
        model_version=None,
    )
    assert executor.schedule(_open_intent(), snapshot=_snapshot())
    assert not evaluate_restart_safety(store.events()).safe
    resolved = executor.drain()
    assert resolved == ("i1",)
    assert evaluate_restart_safety(store.events()).safe
    inventory = book.active_for_symbol("AAPL")
    assert inventory is not None
    assert len(inventory.broker_legs) == 1
    rebuilt = InventoryBook.from_events(store.events())
    assert rebuilt.active_for_symbol("AAPL").entry_fill_count == 1


def test_paper_close_translates_aggregate_exit_to_broker_leg_and_confirms(tmp_path):
    store = InventoryEventStore(tmp_path / "v3.sqlite")
    broker = PaperBrokerClient(equity=100_000)
    runner = ImmediateRunner()
    book = InventoryBook()
    executor = V3BrokerExecutor(
        broker=broker,
        task_runner=runner,
        event_store=store,
        book=book,
        strategy_version="RR5",
        model_version=None,
    )
    executor.schedule(_open_intent(), snapshot=_snapshot())
    executor.drain()
    inventory = book.active_for_symbol("AAPL")
    close = _full_close_intent(inventory)
    assert executor.schedule(close, snapshot=_snapshot(105.0))
    resolved = executor.drain()
    assert resolved == ("c1",)
    assert book.active_for_symbol("AAPL") is None
    assert evaluate_restart_safety(store.events()).safe


def test_unknown_close_submission_halts_new_risk_and_is_restart_unsafe(tmp_path):
    store = InventoryEventStore(tmp_path / "v3.sqlite")
    broker = UnknownClosePaperBroker(equity=100_000)
    runner = ImmediateRunner()
    book = InventoryBook()
    executor = V3BrokerExecutor(
        broker=broker,
        task_runner=runner,
        event_store=store,
        book=book,
        strategy_version="RR5",
        model_version=None,
    )
    executor.schedule(_open_intent(), snapshot=_snapshot())
    assert executor.drain() == ("i1",)
    inventory = book.active_for_symbol("AAPL")
    assert inventory is not None

    assert executor.schedule(
        _full_close_intent(inventory),
        snapshot=_snapshot(105.0),
    )
    assert executor.drain() == ()
    assert executor.halted_reason == "close_submission_outcome_unknown"
    assert not executor.new_risk_allowed
    assert book.active_for_symbol("AAPL") is not None

    event_types = [event.event_type for event in store.events()]
    assert "CLOSE_SUBMISSION_UNKNOWN" in event_types
    assert "CLOSE_SUBMISSION_FAILED" not in event_types
    safety = evaluate_restart_safety(store.events())
    assert not safety.safe
    assert safety.reason == "broker_submission_outcome_unknown"
