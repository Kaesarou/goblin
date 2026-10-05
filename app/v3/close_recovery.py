"""Rebuild unresolved broker-close state from the append-only ledger."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime

from app.v3.models import ExecutionStyle, IntentPurpose, OrderIntent
from app.v3.persistence import InventoryEvent


@dataclass(frozen=True)
class _CloseContext:
    action_id: str
    intent: OrderIntent
    inventory_id: str
    position_id: str
    trigger_price: float
    requested_units: float
    full_close: bool
    pre_close_units: float | None = None  # None identifies legacy action attribution.
    client_order_id: str | None = None


@dataclass
class _PendingCloseConfirmation:
    context: _CloseContext
    close_order_id: str
    accepted_at: datetime
    attempt_count: int = 0
    next_attempt_monotonic: float = 0.0
    error_active: bool = False
    last_error_type: str | None = None
    last_http_status: int | None = None
    next_attempt_at: datetime | None = None
    mutation_active: bool = True
    quantity_resolved: bool = False
    attribution_confident: bool = False
    economics_pending: bool = True
    last_result_state: str = "pending"


@dataclass(frozen=True)
class _ReconciledCloseQuantity:
    action_id: str
    inventory_id: str
    position_id: str
    reconciled_book_units: float
    broker_units: float
    entry_price_basis: float
    attribution_confident: bool
    entry_economics_resolved: bool = True


@dataclass(frozen=True)
class CloseEventReplay:
    contexts: dict[str, _CloseContext]
    accepted: dict[str, _PendingCloseConfirmation]
    resolved: set[str]
    quantities: dict[str, _ReconciledCloseQuantity]
    unattributed_position_ids: set[str]
    unknown_action_ids: set[str]


def reconciled_reduction_attributable(
    context: _CloseContext,
    *,
    previous_units: float,
    reconciled_book_units: float,
    units_close: Callable[[float, float], bool],
    migration_units_close: Callable[[float, float], bool],
) -> bool:
    """Compare a pending close with an observed broker quantity reduction."""
    baseline_matches = (
        context.pre_close_units is None
        or units_close(previous_units, context.pre_close_units)
    )
    compare = migration_units_close if context.pre_close_units is None else units_close
    return baseline_matches and compare(reconciled_book_units, context.requested_units)


def confirmed_economics_attributable(
    context: _CloseContext,
    reconciled: _ReconciledCloseQuantity,
    *,
    executed_units: float,
    units_close: Callable[[float, float], bool],
    migration_units_close: Callable[[float, float], bool],
) -> bool:
    """Modern broker evidence can correct old attribution; legacy evidence cannot."""
    if context.pre_close_units is None:
        return reconciled.attribution_confident and migration_units_close(
            executed_units, reconciled.reconciled_book_units,
        )

    baseline_matches = units_close(
        context.pre_close_units,
        reconciled.reconciled_book_units + reconciled.broker_units,
    )
    requested_matches = units_close(
        reconciled.reconciled_book_units, context.requested_units,
    )
    execution_matches = units_close(
        executed_units, reconciled.reconciled_book_units,
    )
    return baseline_matches and requested_matches and execution_matches


def replay_close_events(
    events: Iterable[InventoryEvent],
    *,
    current_leg_units: Callable[[str], float],
    units_close: Callable[[float, float], bool],
    unattributed_position_ids: Iterable[str] = (),
) -> CloseEventReplay:
    """Read close ledger events without mutating the executor or retry store."""
    contexts: dict[str, _CloseContext] = {}
    accepted: dict[str, _PendingCloseConfirmation] = {}
    resolved: set[str] = set()
    quantities: dict[str, _ReconciledCloseQuantity] = {}
    unattributed = set(unattributed_position_ids)
    unknown: set[str] = set()
    for event in events:
        payload = event.payload
        action_id = str(payload.get("action_id", ""))
        if event.event_type in {"CLOSE_SUBMISSION_STARTED", "CLOSE_SUBMISSION_ACCEPTED"} and action_id:
            position_id = str(payload["position_id"])
            full_close = bool(payload.get("full_close", True))
            requested = float(payload.get("requested_units", 0.0))
            if full_close and requested <= 0:
                requested = current_leg_units(position_id)
            previous = contexts.get(action_id)
            context = _CloseContext(
                action_id=action_id,
                intent=_restored_close_intent(payload, event.occurred_at),
                inventory_id=event.inventory_id, position_id=position_id,
                trigger_price=float(payload["trigger_price"]),
                requested_units=requested, full_close=full_close,
                pre_close_units=(float(payload["pre_close_units"])
                                 if payload.get("pre_close_units") is not None
                                 else previous.pre_close_units if previous else None),
                client_order_id=(payload.get("client_order_id")
                                 or (previous.client_order_id if previous else None)),
            )
            contexts[action_id] = context
            if event.event_type == "CLOSE_SUBMISSION_ACCEPTED":
                accepted[action_id] = _PendingCloseConfirmation(
                    context, str(payload["close_order_id"]), event.occurred_at,
                )
                unknown.discard(action_id)
            elif context.client_order_id:
                # A crash can precede both the POST response and the V3 completion.
                # Resume reads only; the start event never authorizes another POST.
                accepted[action_id] = _PendingCloseConfirmation(
                    context, context.client_order_id, event.occurred_at,
                )
                unknown.add(action_id)
        elif event.event_type == "CLOSE_SUBMISSION_UNKNOWN" and action_id:
            unknown.add(action_id)
            context = contexts.get(action_id)
            if context is not None:
                close_id = payload.get("close_order_id") or context.client_order_id
                if close_id:
                    accepted[action_id] = _PendingCloseConfirmation(
                        context, str(close_id), event.occurred_at,
                    )
        elif event.event_type == "BROKER_QUANTITY_RECONCILED":
            position_id = str(payload["position_id"])
            confident = bool(payload.get("attribution_confident", False))
            ids = [str(value) for value in payload.get("action_ids", [])]
            if not confident:
                unattributed.add(position_id)
            if len(ids) == 1:
                quantities.setdefault(ids[0], _ReconciledCloseQuantity(
                    ids[0], event.inventory_id, position_id,
                    float(payload["reconciled_book_units"]), float(payload["broker_units"]),
                    float(payload["entry_price_basis"]), confident,
                    bool(payload.get("entry_economics_resolved", True)),
                ))
        elif event.event_type == "BROKER_RECONCILIATION_ACKNOWLEDGED":
            unattributed.discard(str(payload["position_id"]))
            resolved.update(str(value) for value in payload["abandoned_action_ids"])
        elif event.event_type in {
            "EXIT_FILLED",
            "EXIT_ECONOMICS_CONFIRMED",
            "CLOSE_SUBMISSION_FAILED",
            "CLOSE_EXECUTION_REJECTED",
        } and action_id:
            resolved.add(action_id)
            if event.event_type == "EXIT_ECONOMICS_CONFIRMED":
                position_id = str(payload["position_id"])
                if payload.get("attribution_confident", True):
                    quantity = quantities.get(action_id)
                    if (
                        quantity is not None
                        and not quantity.attribution_confident
                        and units_close(
                            current_leg_units(position_id),
                            quantity.broker_units,
                        )
                    ):
                        unattributed.discard(position_id)
                else:
                    unattributed.add(position_id)

    return CloseEventReplay(contexts, accepted, resolved, quantities, unattributed, unknown - resolved)


def _restored_close_intent(payload: dict, occurred_at: datetime) -> OrderIntent:
    purpose_value = str(payload.get("purpose", IntentPurpose.PROFIT_EXIT.value))
    try:
        purpose = IntentPurpose(purpose_value)
    except ValueError:
        purpose = IntentPurpose.PROFIT_EXIT
    return OrderIntent(
        intent_id=str(payload["intent_id"]),
        purpose=purpose,
        symbol=str(payload["symbol"]),
        side="SELL",
        notional=0.0,
        created_at=occurred_at,
        execution_style=ExecutionStyle.MARKET,
        inventory_id=None,
        reduce_only=True,
        metadata={"restored": True},
    )
