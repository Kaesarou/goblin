from datetime import datetime, timezone

import pytest

from app.brokers.paper.paper_broker import PaperBrokerClient
from app.market.models import MarketSnapshot
from app.runtime.broker_task_runner import BrokerTaskCompletion, BrokerTaskLane
from app.v3.book import InventoryBook
from app.v3.live_execution import POINT_M_DUST_NOTIONAL_USD, V3BrokerExecutor
from app.v3.models import ExecutionStyle, IntentPurpose, OrderIntent
from app.v3.persistence import InventoryEvent, InventoryEventStore

NOW = datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc)


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


def test_partial_close_scales_account_notional_and_event_replay_preserves_it(tmp_path):
    book = InventoryBook()
    book.apply_entry_fill(
        inventory_id="inv",
        symbol="AIR.PA",
        position_id="p1",
        units=1.25,
        price=200.0,
        account_notional=400.0,
        fee=0.0,
        filled_at=NOW,
    )

    updated = book.apply_exit_fill(
        position_id="p1",
        exit_price=205.0,
        units=1.0,
        fee=0.0,
        filled_at=NOW,
    )

    assert updated.total_units == pytest.approx(0.25)
    assert updated.total_notional == pytest.approx(80.0)
    assert updated.broker_legs[0].account_notional == pytest.approx(80.0)
    assert updated.average_entry_price == pytest.approx(200.0)

    store = InventoryEventStore(tmp_path / "events.sqlite")
    store.append(
        InventoryEvent(
            event_id="entry",
            inventory_id="inv",
            event_type="ENTRY_FILLED",
            occurred_at=NOW,
            payload={
                "symbol": "AIR.PA",
                "position_id": "p1",
                "units": 1.25,
                "price": 200.0,
                "notional": 400.0,
                "fee": 0.0,
            },
            strategy_version="INVENTORY_RR5_ETORO5_V1",
        )
    )
    store.append(
        InventoryEvent(
            event_id="exit",
            inventory_id="inv",
            event_type="EXIT_FILLED",
            occurred_at=NOW,
            payload={
                "symbol": "AIR.PA",
                "position_id": "p1",
                "units": 1.0,
                "price": 205.0,
                "fee": 0.0,
            },
            strategy_version="INVENTORY_RR5_ETORO5_V1",
        )
    )
    rebuilt = InventoryBook.from_events(store.events())
    rebuilt_inventory = rebuilt.active_for_symbol("AIR.PA")
    assert rebuilt_inventory is not None
    assert rebuilt_inventory.total_units == pytest.approx(0.25)
    assert rebuilt_inventory.total_notional == pytest.approx(80.0)
    assert rebuilt_inventory.broker_legs[0].account_notional == pytest.approx(80.0)


def test_point_m_dust_uses_account_currency_notional_not_asset_quote_value(tmp_path):
    # Asset quote residual would be 0.16 * 200 = 32, above the $10 threshold.
    # But the position represents only $50 of account exposure, so its 16%
    # residual is $8 and Point M dust policy must collapse it to a full close.
    book = InventoryBook()
    book.apply_entry_fill(
        inventory_id="inv",
        symbol="AIR.PA",
        position_id="p1",
        units=1.0,
        price=200.0,
        account_notional=50.0,
        fee=0.0,
        filled_at=NOW,
    )
    broker = PaperBrokerClient(equity=100_000)
    broker.positions["p1"] = {"position_id": "p1", "symbol": "AIR.PA"}
    store = InventoryEventStore(tmp_path / "v3.sqlite")
    executor = V3BrokerExecutor(
        broker=broker,
        task_runner=ImmediateRunner(),
        event_store=store,
        book=book,
        strategy_version="INVENTORY_RR5_ETORO5_V1",
        model_version=None,
    )
    inventory = book.active_for_symbol("AIR.PA")
    assert inventory is not None
    intent = OrderIntent(
        intent_id="dust-fx",
        purpose=IntentPurpose.PROFIT_EXIT,
        symbol="AIR.PA",
        side="SELL",
        notional=42.0,
        created_at=NOW,
        execution_style=ExecutionStyle.LIMIT,
        limit_price=200.0,
        inventory_id=inventory.inventory_id,
        reduce_only=True,
        metadata={"close_fraction_of_units": 0.84},
    )
    snapshot = MarketSnapshot(
        "AIR.PA",
        200.0,
        200.1,
        200.0,
        NOW,
    )

    assert POINT_M_DUST_NOTIONAL_USD == 10.0
    assert executor.schedule(intent, snapshot=snapshot)
    assert executor.drain() == ("dust-fx",)
    assert book.active_for_symbol("AIR.PA") is None

    started = next(
        event for event in store.events()
        if event.event_type == "CLOSE_SUBMISSION_STARTED"
    )
    assert started.payload["projected_remaining_notional_usd"] == pytest.approx(8.0)
    assert started.payload["dust_collapse"] is True
    assert started.payload["full_close"] is True
