from __future__ import annotations

from dataclasses import dataclass

from app.v3.models import BrokerLeg, InventoryState


@dataclass(frozen=True)
class LegClosePlan:
    position_ids: tuple[str, ...]
    planned_units: float
    target_units: float
    absolute_error_units: float


class WholeLegCloseAllocator:
    """Approximate an aggregate partial close when broker closes whole legs only.

    eToro's public trading surface closes by position id. The allocator therefore
    chooses a deterministic subset of broker legs whose units are closest to the
    aggregate target. It never closes more units than the inventory owns.
    """

    def plan(self, inventory: InventoryState, target_units: float) -> LegClosePlan:
        legs = tuple(leg for leg in inventory.broker_legs if leg.units > 0)
        if target_units <= 0 or not legs:
            return LegClosePlan((), 0.0, max(0.0, target_units), max(0.0, target_units))

        target = min(float(target_units), sum(leg.units for leg in legs))
        # Inventory depth is bounded (RR5), so exhaustive subset selection is
        # tiny and gives deterministic, auditable broker translation.
        best_ids: tuple[str, ...] = ()
        best_units = 0.0
        best_key = (abs(target), 0, ())
        count = len(legs)
        for mask in range(1, 1 << count):
            selected = tuple(legs[index] for index in range(count) if mask & (1 << index))
            units = sum(leg.units for leg in selected)
            error = abs(units - target)
            ids = tuple(sorted(leg.position_id for leg in selected))
            # Prefer lower error, then fewer broker operations, then stable ids.
            key = (error, len(selected), ids)
            if key < best_key:
                best_key = key
                best_ids = ids
                best_units = units
        return LegClosePlan(
            position_ids=best_ids,
            planned_units=best_units,
            target_units=target,
            absolute_error_units=abs(best_units - target),
        )
