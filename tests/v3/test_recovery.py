from datetime import datetime, timedelta, timezone

from app.v3.persistence import InventoryEvent
from app.v3.recovery import evaluate_restart_safety

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


def event(event_id, kind, minute, action):
    return InventoryEvent(
        event_id, "AAPL:1", kind, NOW + timedelta(minutes=minute),
        {"action_id": action}, "RR5"
    )


def test_restart_fails_closed_for_unresolved_broker_mutation():
    state = evaluate_restart_safety([event("e1", "ORDER_SUBMISSION_STARTED", 0, "a1")])
    assert not state.safe
    assert state.unresolved_action_ids == ("a1",)


def test_fill_resolves_open_submission():
    state = evaluate_restart_safety([
        event("e1", "ORDER_SUBMISSION_STARTED", 0, "a1"),
        event("e2", "ENTRY_FILLED", 1, "a1"),
    ])
    assert state.safe
