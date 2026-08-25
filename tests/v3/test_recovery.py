from datetime import datetime, timezone

from app.v3.persistence import InventoryEvent
from app.v3.recovery import evaluate_restart_safety

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


def _event(event_type: str, action_id: str) -> InventoryEvent:
    return InventoryEvent(
        event_id=f"{event_type}:{action_id}",
        inventory_id="inv-1",
        event_type=event_type,
        occurred_at=NOW,
        payload={"action_id": action_id},
        strategy_version="RR5",
    )


def test_restart_fails_closed_for_interrupted_or_unknown_open():
    interrupted = evaluate_restart_safety([_event("ORDER_SUBMISSION_STARTED", "o1")])
    assert not interrupted.safe
    assert interrupted.reason == "open_submission_interrupted_before_resolution"
    unknown = evaluate_restart_safety(
        [
            _event("ORDER_SUBMISSION_STARTED", "o1"),
            _event("ORDER_SUBMISSION_UNKNOWN", "o1"),
        ]
    )
    assert not unknown.safe
    assert unknown.reason == "broker_submission_outcome_unknown"


def test_accepted_close_is_restart_safe_because_confirmation_is_restorable():
    result = evaluate_restart_safety(
        [
            _event("CLOSE_SUBMISSION_STARTED", "c1"),
            _event("CLOSE_SUBMISSION_ACCEPTED", "c1"),
        ]
    )
    assert result.safe
    assert result.unresolved_action_ids == ()
