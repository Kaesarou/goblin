from __future__ import annotations

from dataclasses import dataclass

from app.v3.models import IntentPurpose, InventoryState, OrderIntent


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


@dataclass(frozen=True)
class InventoryCloseAssessment:
    plan: PartialLegClosePlan
    strategy_fraction: float
    execution_fraction: float
    projected_remaining_notional: float
    projected_leg_remaining: dict[str, float]
    broker_leg_dust_positions: list[str]
    dust_collapse: bool
    dust_collapse_reason: str | None


def assess_inventory_close(
    *,
    inventory: InventoryState,
    intent: OrderIntent,
    bid: float,
    allocator: ProRataPartialCloseAllocator,
    dust_threshold_usd: float,
    unit_tolerance: float,
) -> InventoryCloseAssessment | None:
    """Keep the Point-M close geometry separate from broker mutation scheduling."""
    fraction = float(intent.metadata.get("close_fraction_of_units", 0.0))
    strategy_target_units = (
        inventory.total_units * fraction
        if fraction > 0
        else intent.notional / max(float(bid), 1e-12)
    )
    strategy_target_units = min(inventory.total_units, strategy_target_units)
    strategy_plan = allocator.plan(inventory, strategy_target_units)
    if not strategy_plan.requests:
        return None

    leg_by_position = {
        leg.position_id: leg
        for leg in inventory.broker_legs
        if leg.units > 0
    }
    projected_leg_remaining: dict[str, float] = {}
    for request in strategy_plan.requests:
        leg = leg_by_position[request.position_id]
        remaining_units = max(0.0, float(leg.units) - float(request.units))
        if remaining_units <= unit_tolerance:
            projected_leg_remaining[request.position_id] = 0.0
            continue
        entry_account_per_unit = (
            float(leg.account_notional) / max(float(leg.units), 1e-12)
            if leg.account_notional is not None
            else float(leg.entry_price)
        )
        current_account_per_unit = entry_account_per_unit
        if float(leg.entry_price) > 0:
            current_account_per_unit *= (
                float(bid) / float(leg.entry_price)
            )
        projected_leg_remaining[request.position_id] = max(
            0.0,
            remaining_units * current_account_per_unit,
        )

    projected_remaining_notional = sum(projected_leg_remaining.values())
    aggregate_dust = bool(
        intent.purpose == IntentPurpose.PROFIT_EXIT
        and strategy_target_units < inventory.total_units
        and 0.0 < projected_remaining_notional < dust_threshold_usd
    )
    broker_leg_dust_positions = sorted(
        request.position_id
        for request in strategy_plan.requests
        if not request.full_close
        and 0.0 < projected_leg_remaining[request.position_id]
        < dust_threshold_usd
    )
    broker_leg_dust = bool(
        intent.purpose == IntentPurpose.PROFIT_EXIT
        and strategy_target_units < inventory.total_units
        and broker_leg_dust_positions
    )
    dust_collapse = aggregate_dust or broker_leg_dust
    if aggregate_dust and broker_leg_dust:
        dust_collapse_reason = "inventory_and_broker_leg_below_minimum"
    elif broker_leg_dust:
        dust_collapse_reason = "broker_leg_below_minimum"
    elif aggregate_dust:
        dust_collapse_reason = "inventory_below_minimum"
    else:
        dust_collapse_reason = None

    # A physical broker leg below the minimum cannot retain a pro-rata residual.
    # Close the full inventory rather than change Point-M's weighted geometry.
    plan = (
        allocator.plan(inventory, inventory.total_units)
        if dust_collapse
        else strategy_plan
    )
    if not plan.requests:
        return None

    return InventoryCloseAssessment(
        plan=plan,
        strategy_fraction=fraction,
        execution_fraction=plan.target_units / max(inventory.total_units, 1e-12),
        projected_remaining_notional=projected_remaining_notional,
        projected_leg_remaining=projected_leg_remaining,
        broker_leg_dust_positions=broker_leg_dust_positions,
        dust_collapse=dust_collapse,
        dust_collapse_reason=dust_collapse_reason,
    )
