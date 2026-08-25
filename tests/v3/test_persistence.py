from datetime import datetime, timedelta, timezone

from app.v3.persistence import InventoryEvent, InventoryEventStore

UTC = timezone.utc
NOW = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)


def _event(event_id: str, event_type: str, *, action_id: str) -> InventoryEvent:
    return InventoryEvent(
        event_id=event_id,
        inventory_id="inv-1",
        event_type=event_type,
        occurred_at=NOW,
        payload={"action_id": action_id, "symbol": "AAPL"},
        strategy_version="INVENTORY_RR5_V1",
    )


def test_inventory_event_sink_only_receives_newly_inserted_events(tmp_path):
    mirrored = []
    store = InventoryEventStore(
        tmp_path / "state.sqlite",
        event_sink=mirrored.append,
    )
    event = _event("e1", "ENTRY_FILLED", action_id="open-1")
    assert store.append(event)
    assert not store.append(event)
    assert mirrored == [event]
    assert len(store.events()) == 1


def test_repeated_close_confirmation_errors_are_deduplicated_by_action(tmp_path):
    mirrored = []
    store = InventoryEventStore(
        tmp_path / "state.sqlite",
        event_sink=mirrored.append,
    )
    first = _event(
        "close-1:error:first",
        "CLOSE_CONFIRMATION_ERROR",
        action_id="close-1",
    )
    second = InventoryEvent(
        event_id="close-1:error:second",
        inventory_id="inv-1",
        event_type="CLOSE_CONFIRMATION_ERROR",
        occurred_at=NOW + timedelta(seconds=10),
        payload={"action_id": "close-1", "symbol": "AAPL"},
        strategy_version="INVENTORY_RR5_V1",
    )

    assert store.append(first)
    assert not store.append(second)
    events = store.events()
    assert len(events) == 1
    assert events[0].event_id == "close-1:close-confirmation-error"
    assert len(mirrored) == 1
