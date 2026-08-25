from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from app.brokers.base import (
    BrokerCloseExecution,
    ClosePositionSubmissionUnknownError,
    OpenPositionResult,
)
from app.brokers.etoro.order_confirmation_error import EtoroOrderConfirmationUnknownError
from app.market.models import MarketSnapshot
from app.runtime.broker_task_runner import BrokerTaskCompletion, BrokerTaskLane
from app.v3.book import InventoryBook
from app.v3.execution import ProRataPartialCloseAllocator
from app.v3.models import IntentPurpose, OrderIntent
from app.v3.persistence import InventoryEvent, InventoryEventStore


@dataclass(frozen=True)
class _OpenContext:
    action_id: str
    intent: OrderIntent
    inventory_id: str
    trigger_price: float


@dataclass(frozen=True)
class _CloseContext:
    action_id: str
    intent: OrderIntent
    inventory_id: str
    position_id: str
    trigger_price: float
    requested_units: float
    full_close: bool


@dataclass(frozen=True)
class _PendingCloseConfirmation:
    context: _CloseContext
    close_order_id: str


class V3BrokerExecutor:
    """Translate V3 intents into restart-safe broker mutations.

    Profit exits are translated pro-rata across all broker legs. eToro's native
    ``UnitsToDeduct`` is used for partial legs so the remaining aggregate weighted
    entry price follows the Passivbot/Point-M geometry instead of choosing whole
    broker positions arbitrarily.
    """

    def __init__(
        self,
        *,
        broker,
        task_runner,
        event_store: InventoryEventStore,
        book: InventoryBook,
        strategy_version: str,
        model_version: str | None,
    ) -> None:
        self.broker = broker
        self.task_runner = task_runner
        self.event_store = event_store
        self.book = book
        self.strategy_version = strategy_version
        self.model_version = model_version
        self.allocator = ProRataPartialCloseAllocator()
        self._pending_actions: set[str] = set()
        self._pending_close_confirmations: dict[str, _PendingCloseConfirmation] = {}
        self._confirmation_tasks: set[str] = set()
        self._pending_close_position_ids: set[str] = set()
        self.halted_reason: str | None = None

    @property
    def new_risk_allowed(self) -> bool:
        return self.halted_reason is None

    def schedule(self, intent: OrderIntent, *, snapshot: MarketSnapshot) -> bool:
        if intent.intent_id in self._pending_actions:
            return False
        if intent.purpose in {
            IntentPurpose.HEDGE_OPEN,
            IntentPurpose.HEDGE_ADJUST,
            IntentPurpose.HEDGE_CLOSE,
        }:
            raise RuntimeError(
                "Portfolio hedge execution is not enabled until hedge beta/cost validation is wired"
            )
        if intent.side.upper() == "BUY":
            if not self.new_risk_allowed:
                return False
            return self._schedule_open(intent, snapshot=snapshot)
        if intent.side.upper() == "SELL" and intent.reduce_only:
            return self._schedule_inventory_close(intent, snapshot=snapshot)
        raise RuntimeError(
            f"Unsupported V3 broker intent: {intent.purpose}/{intent.side}"
        )

    def _schedule_open(self, intent: OrderIntent, *, snapshot: MarketSnapshot) -> bool:
        inventory = self.book.active_for_symbol(intent.symbol)
        inventory_id = (
            inventory.inventory_id
            if inventory is not None
            else f"{intent.symbol}:{intent.intent_id}"
        )
        action_id = intent.intent_id
        trigger_price = float(snapshot.ask)
        self._append(
            event_type="ORDER_SUBMISSION_STARTED",
            inventory_id=inventory_id,
            event_id=f"{action_id}:open-start",
            payload={
                "action_id": action_id,
                "intent_id": intent.intent_id,
                "symbol": intent.symbol,
                "purpose": intent.purpose.value,
                "notional": intent.notional,
                "trigger_price": trigger_price,
            },
        )
        context = _OpenContext(action_id, intent, inventory_id, trigger_price)
        stop_loss = max(1e-8, trigger_price * 0.5)
        take_profit = trigger_price * 10.0
        self.task_runner.submit(
            kind="v3_open_position",
            task_id=f"v3-open:{action_id}",
            context=context,
            operation=lambda: self.broker.open_position(
                intent.symbol,
                "BUY",
                float(intent.notional),
                stop_loss,
                take_profit,
            ),
            lane=BrokerTaskLane.STANDARD,
        )
        self._pending_actions.add(action_id)
        return True

    def _schedule_inventory_close(
        self,
        intent: OrderIntent,
        *,
        snapshot: MarketSnapshot,
    ) -> bool:
        inventory = self.book.active_for_symbol(intent.symbol)
        if inventory is None:
            return False
        fraction = float(intent.metadata.get("close_fraction_of_units", 0.0))
        target_units = (
            inventory.total_units * fraction
            if fraction > 0
            else intent.notional / max(float(snapshot.bid), 1e-12)
        )
        plan = self.allocator.plan(inventory, target_units)
        if not plan.requests:
            return False

        scheduled = False
        for request in plan.requests:
            if request.position_id in self._pending_close_position_ids:
                continue
            action_id = f"{intent.intent_id}:{request.position_id}"
            if action_id in self._pending_actions:
                continue
            self._append(
                event_type="CLOSE_SUBMISSION_STARTED",
                inventory_id=inventory.inventory_id,
                event_id=f"{action_id}:close-start",
                payload={
                    "action_id": action_id,
                    "intent_id": intent.intent_id,
                    "position_id": request.position_id,
                    "symbol": intent.symbol,
                    "purpose": intent.purpose.value,
                    "target_units": plan.target_units,
                    "planned_units": plan.planned_units,
                    "allocation_error_units": plan.absolute_error_units,
                    "requested_units": request.units,
                    "full_close": request.full_close,
                    "trigger_price": float(snapshot.bid),
                },
            )
            context = _CloseContext(
                action_id=action_id,
                intent=intent,
                inventory_id=inventory.inventory_id,
                position_id=request.position_id,
                trigger_price=float(snapshot.bid),
                requested_units=request.units,
                full_close=request.full_close,
            )
            self.task_runner.submit(
                kind="close_position",
                task_id=f"v3-close:{action_id}",
                context=context,
                operation=lambda current=context: self.broker.close_position(
                    current.position_id,
                    units_to_deduct=(
                        None if current.full_close else current.requested_units
                    ),
                ),
                lane=BrokerTaskLane.CLOSE,
            )
            self._pending_actions.add(action_id)
            self._pending_close_position_ids.add(request.position_id)
            scheduled = True
        return scheduled

    def drain(self) -> tuple[str, ...]:
        applied: list[str] = []
        for completion in self.task_runner.drain():
            if completion.kind == "v3_open_position":
                applied.extend(self._handle_open_completion(completion))
            elif completion.kind == "close_position" and isinstance(
                completion.context,
                _CloseContext,
            ):
                applied.extend(self._handle_close_submission(completion))
            elif completion.kind == "v3_close_execution_lookup":
                applied.extend(self._handle_close_lookup(completion))
        return tuple(applied)

    def schedule_close_confirmation_checks(self) -> int:
        scheduled = 0
        for action_id, pending in tuple(self._pending_close_confirmations.items()):
            if action_id in self._confirmation_tasks:
                continue
            self._confirmation_tasks.add(action_id)
            self.task_runner.submit(
                kind="v3_close_execution_lookup",
                task_id=f"v3-close-confirm:{action_id}",
                context=pending,
                operation=lambda current=pending: self.broker.get_close_execution(
                    current.close_order_id,
                    current.context.position_id,
                ),
                lane=BrokerTaskLane.CLOSE,
            )
            scheduled += 1
        return scheduled

    def restore_pending_close_confirmations(
        self,
        events: Iterable[InventoryEvent],
    ) -> None:
        accepted: dict[str, _PendingCloseConfirmation] = {}
        resolved: set[str] = set()
        for event in events:
            action_id = str(event.payload.get("action_id", ""))
            if event.event_type == "CLOSE_SUBMISSION_ACCEPTED" and action_id:
                payload = event.payload
                context = _CloseContext(
                    action_id=action_id,
                    intent=_restored_close_intent(payload, event.occurred_at),
                    inventory_id=event.inventory_id,
                    position_id=str(payload["position_id"]),
                    trigger_price=float(payload["trigger_price"]),
                    requested_units=float(payload.get("requested_units", 0.0)),
                    full_close=bool(payload.get("full_close", True)),
                )
                accepted[action_id] = _PendingCloseConfirmation(
                    context=context,
                    close_order_id=str(payload["close_order_id"]),
                )
            elif event.event_type in {
                "EXIT_FILLED",
                "CLOSE_SUBMISSION_FAILED",
            } and action_id:
                resolved.add(action_id)
        for action_id in resolved:
            accepted.pop(action_id, None)
        self._pending_close_confirmations.update(accepted)
        self._pending_actions.update(accepted)
        self._pending_close_position_ids.update(
            pending.context.position_id for pending in accepted.values()
        )

    def verify_known_broker_legs(self) -> tuple[str, ...]:
        missing: list[str] = []
        for inventory in self.book.inventories:
            for leg in inventory.broker_legs:
                self.broker.remember_position_instrument(
                    leg.position_id,
                    inventory.symbol,
                )
                if not self.broker.is_position_open(leg.position_id):
                    missing.append(leg.position_id)
        if missing:
            self.halted_reason = "known_broker_leg_missing"
        return tuple(sorted(missing))

    def _handle_open_completion(self, completion: BrokerTaskCompletion) -> list[str]:
        context = completion.context
        if not isinstance(context, _OpenContext):
            return []
        self._pending_actions.discard(context.action_id)
        if completion.error is not None:
            unknown = isinstance(completion.error, EtoroOrderConfirmationUnknownError)
            self._append(
                event_type=("ORDER_SUBMISSION_UNKNOWN" if unknown else "ORDER_SUBMISSION_FAILED"),
                inventory_id=context.inventory_id,
                event_id=f"{context.action_id}:open-error",
                payload={
                    "action_id": context.action_id,
                    "symbol": context.intent.symbol,
                    "error": str(completion.error),
                    "error_type": type(completion.error).__name__,
                },
            )
            if unknown:
                self.halted_reason = "open_order_confirmation_unknown"
            return []
        result = completion.value
        if not isinstance(result, OpenPositionResult):
            self.halted_reason = "invalid_open_completion"
            return []
        price = float(result.executed_entry_price or context.trigger_price)
        units = context.intent.notional / price
        payload = {
            "action_id": context.action_id,
            "intent_id": context.intent.intent_id,
            "symbol": context.intent.symbol,
            "position_id": result.position_id,
            "units": units,
            "price": price,
            "notional": context.intent.notional,
            "fee": 0.0,
            "estimated_cost": (
                None
                if context.intent.cost_estimate is None
                else context.intent.cost_estimate.total
            ),
            "purpose": context.intent.purpose.value,
        }
        self._append(
            event_type="ENTRY_FILLED",
            inventory_id=context.inventory_id,
            event_id=f"{context.action_id}:entry-fill:{result.position_id}",
            payload=payload,
        )
        self.book.apply_entry_fill(
            inventory_id=context.inventory_id,
            symbol=context.intent.symbol,
            position_id=result.position_id,
            units=units,
            price=price,
            fee=0.0,
            filled_at=_utc_now(),
        )
        return [context.intent.intent_id]

    def _handle_close_submission(self, completion: BrokerTaskCompletion) -> list[str]:
        context = completion.context
        assert isinstance(context, _CloseContext)
        if completion.error is not None:
            self._pending_actions.discard(context.action_id)
            self._pending_close_position_ids.discard(context.position_id)
            unknown = isinstance(completion.error, ClosePositionSubmissionUnknownError)
            self._append(
                event_type=("CLOSE_SUBMISSION_UNKNOWN" if unknown else "CLOSE_SUBMISSION_FAILED"),
                inventory_id=context.inventory_id,
                event_id=f"{context.action_id}:close-error",
                payload={
                    "action_id": context.action_id,
                    "position_id": context.position_id,
                    "requested_units": context.requested_units,
                    "full_close": context.full_close,
                    "error": str(completion.error),
                    "error_type": type(completion.error).__name__,
                },
            )
            if unknown:
                self.halted_reason = "close_submission_outcome_unknown"
            return []

        submission = completion.value
        broker_payload = getattr(submission, "broker_response", {}) or {}
        if broker_payload.get("mode") == "paper":
            self._confirm_close(
                context=context,
                exit_price=context.trigger_price,
                filled_at=_utc_now(),
                close_order_id=str(getattr(submission, "close_order_id", "paper")),
                executed_units=context.requested_units,
            )
            return [context.intent.intent_id]

        close_order_id = getattr(submission, "close_order_id", None)
        if not close_order_id:
            self.halted_reason = "close_accepted_without_order_id"
            self._append(
                event_type="CLOSE_SUBMISSION_UNKNOWN",
                inventory_id=context.inventory_id,
                event_id=f"{context.action_id}:close-unknown",
                payload={
                    "action_id": context.action_id,
                    "position_id": context.position_id,
                    "requested_units": context.requested_units,
                    "full_close": context.full_close,
                },
            )
            return []

        pending = _PendingCloseConfirmation(context, str(close_order_id))
        self._pending_close_confirmations[context.action_id] = pending
        self._append(
            event_type="CLOSE_SUBMISSION_ACCEPTED",
            inventory_id=context.inventory_id,
            event_id=f"{context.action_id}:close-accepted",
            payload={
                "action_id": context.action_id,
                "intent_id": context.intent.intent_id,
                "position_id": context.position_id,
                "symbol": context.intent.symbol,
                "purpose": context.intent.purpose.value,
                "trigger_price": context.trigger_price,
                "requested_units": context.requested_units,
                "full_close": context.full_close,
                "close_order_id": str(close_order_id),
            },
        )
        return []

    def _handle_close_lookup(self, completion: BrokerTaskCompletion) -> list[str]:
        pending = completion.context
        if not isinstance(pending, _PendingCloseConfirmation):
            return []
        action_id = pending.context.action_id
        self._confirmation_tasks.discard(action_id)
        if completion.error is not None:
            self._append(
                event_type="CLOSE_CONFIRMATION_ERROR",
                inventory_id=pending.context.inventory_id,
                event_id=f"{action_id}:confirm-error:{_utc_now().isoformat()}",
                payload={"action_id": action_id, "error": str(completion.error)},
            )
            return []
        execution = completion.value
        if execution is None:
            return []
        if not isinstance(execution, BrokerCloseExecution):
            self.halted_reason = "invalid_close_execution"
            return []
        if not pending.context.full_close and execution.units is None:
            self.halted_reason = "partial_close_execution_units_missing"
            self._append(
                event_type="CLOSE_EXECUTION_INVALID",
                inventory_id=pending.context.inventory_id,
                event_id=f"{action_id}:missing-units",
                payload={
                    "action_id": action_id,
                    "position_id": pending.context.position_id,
                    "requested_units": pending.context.requested_units,
                },
            )
            return []
        self._confirm_close(
            context=pending.context,
            exit_price=float(execution.executed_exit_price),
            filled_at=execution.executed_at or _utc_now(),
            close_order_id=pending.close_order_id,
            executed_units=(
                pending.context.requested_units
                if execution.units is None
                else float(execution.units)
            ),
        )
        return [pending.context.intent.intent_id]

    def _confirm_close(
        self,
        *,
        context: _CloseContext,
        exit_price: float,
        filled_at: datetime,
        close_order_id: str,
        executed_units: float,
    ) -> None:
        inventory = self.book.active_for_symbol(context.intent.symbol)
        if inventory is None:
            self.halted_reason = "close_fill_without_inventory"
            return
        leg = next(
            (item for item in inventory.broker_legs if item.position_id == context.position_id),
            None,
        )
        if leg is None:
            self.halted_reason = "close_fill_without_broker_leg"
            return
        if executed_units <= 0 or executed_units > leg.units + 1e-9:
            self.halted_reason = "close_execution_units_invalid"
            self._append(
                event_type="CLOSE_EXECUTION_INVALID",
                inventory_id=context.inventory_id,
                event_id=f"{context.action_id}:invalid-units",
                payload={
                    "action_id": context.action_id,
                    "position_id": context.position_id,
                    "requested_units": context.requested_units,
                    "executed_units": executed_units,
                    "leg_units": leg.units,
                },
            )
            return

        self._append(
            event_type="EXIT_FILLED",
            inventory_id=context.inventory_id,
            event_id=f"{context.action_id}:exit-fill:{close_order_id}",
            payload={
                "action_id": context.action_id,
                "intent_id": context.intent.intent_id,
                "symbol": context.intent.symbol,
                "position_id": context.position_id,
                "units": executed_units,
                "requested_units": context.requested_units,
                "price": exit_price,
                "fee": 0.0,
                "close_order_id": close_order_id,
                "purpose": context.intent.purpose.value,
            },
            occurred_at=filled_at,
        )
        updated = self.book.apply_exit_fill(
            position_id=context.position_id,
            exit_price=exit_price,
            units=executed_units,
            fee=0.0,
            filled_at=filled_at,
        )
        if not any(
            item.position_id == context.position_id for item in updated.broker_legs
        ):
            self.broker.forget_position_instrument(context.position_id)
        self._pending_close_confirmations.pop(context.action_id, None)
        self._pending_actions.discard(context.action_id)
        self._pending_close_position_ids.discard(context.position_id)

    def _append(
        self,
        *,
        event_type: str,
        inventory_id: str,
        event_id: str,
        payload: dict,
        occurred_at: datetime | None = None,
    ) -> None:
        self.event_store.append(
            InventoryEvent(
                event_id=event_id,
                inventory_id=inventory_id,
                event_type=event_type,
                occurred_at=occurred_at or _utc_now(),
                payload=payload,
                strategy_version=self.strategy_version,
                model_version=self.model_version,
            )
        )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _restored_close_intent(payload: dict, occurred_at: datetime) -> OrderIntent:
    from app.v3.models import ExecutionStyle

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
