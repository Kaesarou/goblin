from datetime import datetime, timezone

import pytest

from app.brokers.paper.paper_broker import PaperBrokerClient
from app.market.models import MarketSnapshot
from app.runtime.broker_task_runner import BrokerTaskCompletion, BrokerTaskLane
from app.v3.book import InventoryBook
from app.v3.execution import ProRataPartialCloseAllocator
from app.v3.live_execution import POINT_M_DUST_NOTIONAL_USD, V3BrokerExecutor
from app.v3.models import ExecutionStyle, IntentPurpose, OrderIntent
from app.v3.persistence import InventoryEventStore

NOW = datetime(2026, 8, 25, 18, 0, tzinfo=timezone.utc)


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


def _snapshot(price=100.0):
    return MarketSnapshot("AAPL", price, price + 0.1, price, NOW)


def _close_intent(inventory, fraction, intent_id="close"):
    return OrderIntent(
        intent_id,
        IntentPurpose.PROFIT_EXIT,
        inventory.symbol,
        "SELL",
        inventory.total_notional * fraction,
        NOW,
        ExecutionStyle.LIMIT,
        limit_price=100.0,
        inventory_id=inventory.inventory_id,
        reduce_only=True,
        metadata={"close_fraction_of_units": fraction},
    )


def _executor(tmp_path, broker, book):
    return V3BrokerExecutor(
        broker=broker,
        task_runner=ImmediateRunner(),
        event_store=InventoryEventStore(tmp_path / "v3.sqlite"),
        book=book,
        strategy_version="INVENTORY_RR5_ETORO5_V1",
        model_version=None,
    )


def test_pro_rata_allocator_preserves_fraction_per_leg():
    book = InventoryBook()
    book.apply_entry_fill(
        inventory_id="inv",
        symbol="AAPL",
        position_id="p1",
        units=2.0,
        price=100.0,
        fee=0.0,
        filled_at=NOW,
    )
    book.apply_entry_fill(
        inventory_id="inv",
        symbol="AAPL",
        position_id="p2",
        units=1.0,
        price=80.0,
        fee=0.0,
        filled_at=NOW,
    )
    inventory = book.active_for_symbol("AAPL")
    plan = ProRataPartialCloseAllocator().plan(
        inventory,
        inventory.total_units * 0.84,
    )

    assert [request.position_id for request in plan.requests] == ["p1", "p2"]
    assert [request.units for request in plan.requests] == pytest.approx([1.68, 0.84])
    assert not any(request.full_close for request in plan.requests)
    assert plan.planned_units == pytest.approx(inventory.total_units * 0.84)


def test_partial_exit_fill_keeps_broker_leg_and_rebuilds(tmp_path):
    store = InventoryEventStore(tmp_path / "events.sqlite")
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
    updated = book.apply_exit_fill(
        position_id="p1",
        exit_price=105.0,
        units=0.84,
        fee=0.0,
        filled_at=NOW,
    )

    assert updated.total_units == pytest.approx(0.16)
    assert updated.average_entry_price == pytest.approx(100.0)
    assert updated.broker_legs[0].position_id == "p1"
    assert updated.broker_legs[0].units == pytest.approx(0.16)
    assert "p1" in book.active_broker_position_ids()


def test_paper_executor_closes_84_percent_then_full_remainder(tmp_path):
    book = InventoryBook()
    broker = PaperBrokerClient(equity=100_000)
    broker.positions["p1"] = {"position_id": "p1", "symbol": "AAPL"}
    book.apply_entry_fill(
        inventory_id="inv",
        symbol="AAPL",
        position_id="p1",
        units=1.0,
        price=100.0,
        fee=0.0,
        filled_at=NOW,
    )
    executor = _executor(tmp_path, broker, book)

    inventory = book.active_for_symbol("AAPL")
    assert executor.schedule(_close_intent(inventory, 0.84, "c84"), snapshot=_snapshot())
    assert executor.drain() == ("c84",)

    remaining = book.active_for_symbol("AAPL")
    assert remaining is not None
    assert remaining.total_units == pytest.approx(0.16)
    assert remaining.average_entry_price == pytest.approx(100.0)
    assert broker.is_position_open("p1")

    assert executor.schedule(_close_intent(remaining, 1.0, "c100"), snapshot=_snapshot())
    assert executor.drain() == ("c100",)
    assert book.active_for_symbol("AAPL") is None
    assert not broker.is_position_open("p1")


def test_profit_exit_collapses_point_m_dust_to_full_close(tmp_path):
    book = InventoryBook()
    broker = PaperBrokerClient(equity=100_000)
    broker.positions["p1"] = {"position_id": "p1", "symbol": "AAPL"}
    book.apply_entry_fill(
        inventory_id="inv",
        symbol="AAPL",
        position_id="p1",
        units=0.5,
        price=100.0,
        fee=0.0,
        filled_at=NOW,
    )
    executor = _executor(tmp_path, broker, book)
    inventory = book.active_for_symbol("AAPL")

    assert POINT_M_DUST_NOTIONAL_USD == 10.0
    assert executor.schedule(_close_intent(inventory, 0.84, "dust"), snapshot=_snapshot())
    assert executor.drain() == ("dust",)
    assert book.active_for_symbol("AAPL") is None
    assert not broker.is_position_open("p1")

    started = next(
        event
        for event in executor.event_store.events()
        if event.event_type == "CLOSE_SUBMISSION_STARTED"
    )
    assert started.payload["strategy_close_fraction"] == pytest.approx(0.84)
    assert started.payload["execution_close_fraction"] == pytest.approx(1.0)
    assert started.payload["projected_remaining_notional_usd"] == pytest.approx(8.0)
    assert started.payload["dust_collapse"] is True
    assert started.payload["full_close"] is True


def test_two_leg_pro_rata_partial_close_preserves_weighted_entry(tmp_path):
    book = InventoryBook()
    broker = PaperBrokerClient(equity=100_000)
    for position_id in ("p1", "p2"):
        broker.positions[position_id] = {"position_id": position_id, "symbol": "AAPL"}
    book.apply_entry_fill(
        inventory_id="inv",
        symbol="AAPL",
        position_id="p1",
        units=2.0,
        price=100.0,
        fee=0.0,
        filled_at=NOW,
    )
    book.apply_entry_fill(
        inventory_id="inv",
        symbol="AAPL",
        position_id="p2",
        units=1.0,
        price=80.0,
        fee=0.0,
        filled_at=NOW,
    )
    executor = _executor(tmp_path, broker, book)
    before = book.active_for_symbol("AAPL")
    before_average = before.average_entry_price

    assert executor.schedule(_close_intent(before, 0.84, "multi"), snapshot=_snapshot())
    assert executor.drain() == ("multi", "multi")

    remaining = book.active_for_symbol("AAPL")
    assert remaining.total_units == pytest.approx(0.48)
    assert remaining.average_entry_price == pytest.approx(before_average)
    assert [leg.units for leg in remaining.broker_legs] == pytest.approx([0.32, 0.16])
