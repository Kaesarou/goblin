from datetime import datetime, timezone

from app.v3.persistence import InventoryEvent, InventoryEventStore, InventorySnapshot

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


def test_event_append_is_idempotent(tmp_path):
    store = InventoryEventStore(tmp_path / "v3.sqlite")
    event = InventoryEvent(
        event_id="event-1",
        inventory_id="AAPL:1",
        event_type="ENTRY_FILLED",
        occurred_at=NOW,
        payload={"units": 1.25},
        strategy_version="RR5",
        model_version="RECOVERY_V1",
    )
    assert store.append(event)
    assert not store.append(event)
    assert store.events("AAPL:1") == [event]


def test_snapshot_is_replaceable_acceleration_state(tmp_path):
    store = InventoryEventStore(tmp_path / "v3.sqlite")
    store.save_snapshot(InventorySnapshot("AAPL:1", NOW, {"entry_fill_count": 2}))
    store.save_snapshot(InventorySnapshot("AAPL:1", NOW, {"entry_fill_count": 3}))
    snap = store.load_snapshot("AAPL:1")
    assert snap is not None
    assert snap.state["entry_fill_count"] == 3
