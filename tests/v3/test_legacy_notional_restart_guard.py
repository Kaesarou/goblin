"""Historical risk corruption must not be hidden by the absence of new anomaly events."""

from datetime import datetime, timezone

import pytest

from app.v3.book import InventoryBook
from app.v3.live_execution import V3BrokerExecutor
from app.v3.persistence import InventoryEvent, InventoryEventStore
from app.v3.recovery import evaluate_restart_safety

NOW = datetime(2026, 9, 15, 13, 40, tzinfo=timezone.utc)


@pytest.mark.parametrize("notional,source,should_halt", (
    (1.0, "broker_confirmed_account_currency", True),
    (300.0, "broker_confirmed_account_currency", False),
    (300.0, "requested_account_currency", False),
))
def test_active_historical_fill_notional_replayed_without_rewriting_sqlite(
    tmp_path, notional, source, should_halt
):
    store = InventoryEventStore(tmp_path / "history.sqlite")
    event = InventoryEvent(
        event_id="historical-entry", inventory_id="INTC:historical",
        event_type="ENTRY_FILLED", occurred_at=NOW,
        payload={
            "action_id": "historical-open", "symbol": "INTC",
            "position_id": "3599774868", "units": 3.0, "price": 100.0,
            "notional": notional, "requested_notional": 300.0,
            "notional_source": source, "fee": 0.0,
        },
        strategy_version="INVENTORY_RR5_ETORO5_V1",
    )
    assert store.append(event)
    original_events = store.events()
    assert evaluate_restart_safety(original_events).safe
    executor = V3BrokerExecutor(
        broker=object(), task_runner=object(), event_store=store,
        book=InventoryBook.from_events(original_events),
        strategy_version="INVENTORY_RR5_ETORO5_V1", model_version=None,
    )
    assert executor.new_risk_allowed is (not should_halt)
    assert executor.confirmation_metrics()["account_notional_anomaly_action_ids"] == (
        ["historical-open"] if should_halt else []
    )
    if should_halt:
        assert executor.halted_reason == "open_account_notional_mismatch_at_restart"
    assert store.events() == original_events
