from __future__ import annotations

from dataclasses import dataclass

from app.v3.models import InventoryState


@dataclass(frozen=True)
class LegClosePlan:
    position_ids: tuple[str, ...]
    planned_units: float
    target_units: float
    absolute_error_units: float


@dataclass(frozen=True)
class PartialLegCloseRequest:
    position_id: str
    units: float
    full_close: bool


@dataclass(frozen=True)
class PartialLegClosePlan:
    requests: tuple[PartialLegCloseRequest, ...]
    planned_units: float
    target_units: float
    absolute_error_units: float


class WholeLegCloseAllocator:
    """Legacy whole-leg translation retained for historical comparisons."""

    def plan(self, inventory: InventoryState, target_units: float) -> LegClosePlan:
        legs = tuple(leg for leg in inventory.broker_legs if leg.units > 0)
        if target_units <= 0 or not legs:
            return LegClosePlan((), 0.0, max(0.0, target_units), max(0.0, target_units))

        target = min(float(target_units), sum(leg.units for leg in legs))
        best_ids: tuple[str, ...] = ()
        best_units = 0.0
        best_key = (abs(target), 0, ())
        count = len(legs)
        for mask in range(1, 1 << count):
            selected = tuple(legs[index] for index in range(count) if mask & (1 << index))
            units = sum(leg.units for leg in selected)
            error = abs(units - target)
            ids = tuple(sorted(leg.position_id for leg in selected))
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


class ProRataPartialCloseAllocator:
    """Preserve aggregate Passivbot inventory geometry using broker partial closes.

    Every live broker leg is reduced by the same fraction. This preserves the
    weighted average entry price of the remaining aggregate inventory, unlike
    choosing complete legs by position id. Requests become full closes only when
    the requested aggregate fraction is effectively 100%.
    """

    def plan(self, inventory: InventoryState, target_units: float) -> PartialLegClosePlan:
        legs = tuple(leg for leg in inventory.broker_legs if leg.units > 0)
        total_units = sum(leg.units for leg in legs)
        if target_units <= 0 or total_units <= 0:
            target = max(0.0, float(target_units))
            return PartialLegClosePlan((), 0.0, target, target)

        target = min(float(target_units), total_units)
        fraction = min(1.0, target / total_units)
        requests = tuple(
            PartialLegCloseRequest(
                position_id=leg.position_id,
                units=leg.units * fraction,
                full_close=(fraction >= 1.0 - 1e-12),
            )
            for leg in sorted(legs, key=lambda item: item.position_id)
        )
        planned = sum(item.units for item in requests)
        return PartialLegClosePlan(
            requests=requests,
            planned_units=planned,
            target_units=target,
            absolute_error_units=abs(planned - target),
        )
