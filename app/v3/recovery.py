from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.v3.persistence import InventoryEvent


@dataclass(frozen=True)
class RestartSafety:
    safe: bool
    unresolved_action_ids: tuple[str, ...]


def evaluate_restart_safety(events: Iterable[InventoryEvent]) -> RestartSafety:
    """Fail closed if a broker mutation may have escaped local accounting.

    Submission-start events are written before dispatching blocking broker work.
    A later fill/confirmation/failure event resolves the action. If the process
    dies in-between, V3 must not open additional risk after restart until the
    unresolved mutation is reconciled explicitly.
    """

    unresolved: set[str] = set()
    for event in events:
        action_id = str(event.payload.get("action_id", "")).strip()
        if not action_id:
            continue
        if event.event_type in {"ORDER_SUBMISSION_STARTED", "CLOSE_SUBMISSION_STARTED"}:
            unresolved.add(action_id)
        elif event.event_type in {
            "ENTRY_FILLED",
            "ORDER_SUBMISSION_FAILED",
            "EXIT_FILLED",
            "CLOSE_SUBMISSION_FAILED",
        }:
            unresolved.discard(action_id)
    ordered = tuple(sorted(unresolved))
    return RestartSafety(safe=not ordered, unresolved_action_ids=ordered)
