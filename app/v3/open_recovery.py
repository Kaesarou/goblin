"""Request-bound BUY recovery and explicit terminal partial-fill evidence."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from app.brokers.base import BrokerOpenOrder, OpenPositionResult
from app.v3.models import CostEstimate, ExecutionStyle, IntentPurpose, OrderIntent
from app.v3.persistence import InventoryEvent


@dataclass(frozen=True)
class _OpenContext:
    action_id: str
    intent: OrderIntent
    inventory_id: str
    trigger_price: float
    client_order_id: str | None = None
    causal_quote: dict | None = None


@dataclass
class _PendingOpenConfirmation:
    context: _OpenContext
    submitted_at: datetime
    attempt_count: int = 0
    next_attempt_monotonic: float = 0.0
    next_attempt_at: datetime | None = None
    last_error_type: str | None = None
    last_http_status: int | None = None
    last_result_state: str = "submission_unknown"


def restored_open_context(event: InventoryEvent) -> _OpenContext:
    payload = event.payload
    return _OpenContext(
        action_id=str(payload["action_id"]),
        intent=OrderIntent(
            intent_id=str(payload["intent_id"]), purpose=IntentPurpose(payload["purpose"]),
            symbol=str(payload["symbol"]), side="BUY", notional=float(payload["notional"]),
            created_at=event.occurred_at, execution_style=ExecutionStyle.MARKET,
            inventory_id=event.inventory_id,
            cost_estimate=(CostEstimate(**payload["cost_estimate"])
                           if payload.get("cost_estimate") is not None else None),
        ),
        inventory_id=event.inventory_id, trigger_price=float(payload["trigger_price"]),
        client_order_id=str(payload["client_order_id"]),
        causal_quote=payload.get("causal_quote"),
    )


def _positive(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def open_order_matches_request(order: BrokerOpenOrder, context: _OpenContext) -> bool:
    return (
        isinstance(order, BrokerOpenOrder)
        and bool(context.client_order_id) and order.order_id == context.client_order_id
        and isinstance(order.position_id, str) and bool(order.position_id.strip())
        and order.symbol == context.intent.symbol
        and _positive(order.requested_notional) and _positive(context.intent.notional)
        # Notional orders may round down to account-currency cents, never up.
        and -1e-9 <= context.intent.notional - order.requested_notional < 0.01 + 1e-9
    )


def terminal_open_evidence_valid(result: OpenPositionResult, context: _OpenContext) -> bool:
    return (
        isinstance(result, OpenPositionResult)
        and open_order_matches_request(result.order, context)
        and result.order.status in {"filled", "canceled", "expired", "rejected"}
        and result.position_id == result.order.position_id
        and _positive(result.executed_entry_price) and _positive(result.executed_units)
        and _positive(result.executed_notional)
        and math.isclose(result.executed_notional,
                         result.executed_entry_price * result.executed_units,
                         rel_tol=1e-9, abs_tol=1e-6)
    )


def terminal_partial_open_valid(result: OpenPositionResult, context: _OpenContext) -> bool:
    return (
        terminal_open_evidence_valid(result, context)
        and result.order.status in {"canceled", "expired", "rejected"}
        and result.executed_notional <= result.order.requested_notional + 1e-6
    )


def partial_open_event_valid(payload: dict) -> bool:
    """Revalidate durable partial-fill evidence; a boolean flag is not authority."""
    evidence = payload.get("open_order")
    if not isinstance(evidence, dict):
        return False
    try:
        context = _OpenContext(
            action_id=str(payload["action_id"]), inventory_id="",
            trigger_price=payload["price"], client_order_id=payload["client_order_id"],
            intent=OrderIntent(
                intent_id=str(payload["intent_id"]), purpose=IntentPurpose(payload["purpose"]),
                symbol=payload["symbol"], side="BUY", notional=payload["requested_notional"],
                created_at=datetime.min, execution_style=ExecutionStyle.MARKET,
            ),
        )
        result = OpenPositionResult(
            payload["position_id"], payload["price"], payload["units"], payload["notional"],
            BrokerOpenOrder(**evidence),
        )
        return terminal_partial_open_valid(result, context)
    except (KeyError, TypeError, ValueError):
        return False
