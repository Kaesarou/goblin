"""Read-only broker verification and auditable abandonment of ambiguous close economics."""
from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone

from app.v3.book import InventoryBook
from app.v3.live_execution import _units_close
from app.v3.persistence import InventoryEvent


def unresolved_broker_reconciliations(events) -> dict[str, dict]:
    outstanding: dict[str, dict] = {}
    for event in events:
        payload = event.payload
        if event.event_type == "BROKER_RECONCILIATION_ACKNOWLEDGED":
            outstanding.pop(str(payload["position_id"]), None)
        elif event.event_type in {"BROKER_QUANTITY_RECONCILED", "EXIT_ECONOMICS_CONFIRMED"} and not payload.get("attribution_confident", False):
            position_id = str(payload["position_id"])
            units = float(payload.get("broker_units", payload.get("broker_remaining_units")))
            if not math.isfinite(units) or units < 0:
                raise ValueError("Invalid unattributed reconciliation quantity")
            record = outstanding.setdefault(position_id, {
                "inventory_id": event.inventory_id, "source_event_ids": [],
                "action_ids": set(), "strategy_version": event.strategy_version,
                "model_version": event.model_version,
            })
            record["broker_units"] = units
            record["source_event_ids"].append(event.event_id)
            record["action_ids"].update(str(value) for value in payload.get("action_ids", []))
            if payload.get("action_id"):
                record["action_ids"].add(str(payload["action_id"]))
    return outstanding


def acknowledge_broker_reconciliation(
    *, event_store, state_store, broker, position_id: str,
    expected_broker_units: float, reason: str, apply: bool = False,
    observed_at: datetime | None = None,
) -> dict:
    position_id, reason = position_id.strip(), reason.strip()
    if not position_id or not reason:
        raise ValueError("Explicit position ID and human reason are required")
    if isinstance(expected_broker_units, bool) or not math.isfinite(expected_broker_units) or expected_broker_units < 0:
        raise ValueError("Expected broker units must be finite and nonnegative")
    events = event_store.events()
    record = unresolved_broker_reconciliations(events).get(position_id)
    if record is None:
        raise ValueError("No unattributed reconciliation exists for this position")
    book = InventoryBook.from_events(events)
    units = sum(leg.units for inv in book.inventories for leg in inv.broker_legs if leg.position_id == position_id)
    if not _units_close(units, expected_broker_units) or not _units_close(record["broker_units"], expected_broker_units):
        raise ValueError("Expected quantity differs from reconciled inventory ledger")
    actual = broker.get_open_position_units((position_id,)).get(position_id)
    if isinstance(actual, bool) or not isinstance(actual, (int, float)) or not math.isfinite(actual) or actual < 0 or not _units_close(actual, expected_broker_units):
        raise ValueError("Current broker quantity does not strictly match expected units")
    now = observed_at or datetime.now(timezone.utc)
    if now.tzinfo is None or any(event.occurred_at > now for event in events):
        raise ValueError("Acknowledgment must follow the ledger with an aware timestamp")
    payload = {
        "position_id": position_id, "broker_units": float(actual),
        "reason": reason, "source_event_ids": record["source_event_ids"],
        "abandoned_action_ids": sorted(record["action_ids"]),
        "economics_unknown": True, "broker_operation": "read_only",
        "authority": "explicit_operator_acknowledgment_v1",
    }
    digest = hashlib.sha256("|".join(record["source_event_ids"]).encode()).hexdigest()[:24]
    event = InventoryEvent(
        event_id=f"broker-reconciliation-ack:{digest}", inventory_id=record["inventory_id"],
        event_type="BROKER_RECONCILIATION_ACKNOWLEDGED", occurred_at=now,
        payload=payload, strategy_version=record["strategy_version"], model_version=record["model_version"],
    )
    if apply:
        event_store.append(event, expected_event_ids={item.event_id for item in events})
        for action_id in payload["abandoned_action_ids"]:
            state_store.delete_close_retry(action_id)
    return {"applied": apply, "event_id": event.event_id, **payload}
