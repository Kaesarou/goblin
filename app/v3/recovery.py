from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.v3.persistence import InventoryEvent


@dataclass(frozen=True)
class RestartSafety:
    safe: bool
    unresolved_action_ids: tuple[str, ...]
    reason: str | None = None


def evaluate_restart_safety(events: Iterable[InventoryEvent]) -> RestartSafety:
    """Prove that no broker mutation was interrupted at an unknowable boundary.

    Open submissions are resolved only by a confirmed fill or explicit failure.
    A close submission is safe to resume once the broker returned an accepted
    close-order id because V3 can restore that pending confirmation and continue
    polling. A directly confirmed EXIT_FILLED also resolves the close-start path
    (notably paper execution). An explicit UNKNOWN outcome always fails closed.
    """

    open_started: set[str] = set()
    open_resolved: set[str] = set()
    close_started: set[str] = set()
    close_submission_resolved: set[str] = set()
    explicit_unknown: set[str] = set()
    acknowledged_closes: set[str] = set()

    for event in events:
        if event.event_type == "BROKER_RECONCILIATION_ACKNOWLEDGED":
            acknowledged_closes.update(str(value) for value in event.payload["abandoned_action_ids"])
        action_id = str(event.payload.get("action_id", "")).strip()
        if not action_id:
            continue
        if event.event_type == "ORDER_SUBMISSION_STARTED":
            open_started.add(action_id)
        elif event.event_type in {"ENTRY_FILLED", "ORDER_SUBMISSION_FAILED"}:
            open_resolved.add(action_id)
        elif event.event_type == "ORDER_SUBMISSION_UNKNOWN":
            explicit_unknown.add(action_id)
        elif event.event_type == "CLOSE_SUBMISSION_STARTED":
            close_started.add(action_id)
        elif event.event_type in {
            "CLOSE_SUBMISSION_ACCEPTED",
            "CLOSE_SUBMISSION_FAILED",
            "EXIT_FILLED",
        }:
            close_submission_resolved.add(action_id)
        elif event.event_type == "CLOSE_SUBMISSION_UNKNOWN":
            explicit_unknown.add(action_id)

    # A close acknowledgment has no authority over an open submission.
    acknowledged_closes &= close_started
    explicit_unknown -= acknowledged_closes - open_started
    close_submission_resolved.update(acknowledged_closes)
    if explicit_unknown:
        unresolved = tuple(sorted(explicit_unknown))
        return RestartSafety(False, unresolved, "broker_submission_outcome_unknown")

    unresolved_open = open_started - open_resolved
    if unresolved_open:
        unresolved = tuple(sorted(unresolved_open))
        return RestartSafety(False, unresolved, "open_submission_interrupted_before_resolution")

    unresolved_close = close_started - close_submission_resolved
    if unresolved_close:
        unresolved = tuple(sorted(unresolved_close))
        return RestartSafety(False, unresolved, "close_submission_interrupted_before_resolution")

    return RestartSafety(True, (), None)
