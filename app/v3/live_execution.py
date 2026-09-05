from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

import requests

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
from app.v3.state_store import CloseRetryState, V3RuntimeStateStore


POINT_M_DUST_NOTIONAL_USD = 10.0
CONFIRMATION_INITIAL_DELAY_SECONDS = 10.0
CONFIRMATION_BACKOFF_BASE_SECONDS = 15.0
CONFIRMATION_BACKOFF_MAX_SECONDS = 300.0
# Internal load shedding, not a claim about eToro close-order retention.
ECONOMICS_CONFIRMATION_BACKOFF_MAX_SECONDS = 3600.0
CONFIRMATION_429_MIN_SECONDS = 60.0
CONFIRMATION_STALE_HALT_SECONDS = 15.0 * 60.0
BROKER_RECONCILIATION_INTERVAL_SECONDS = 60.0
BROKER_UNIT_ABS_TOLERANCE = 1e-8
BROKER_UNIT_REL_TOLERANCE = 1e-6
BROKER_RECONCILIATION_ATTRIBUTION_REL_TOLERANCE = 0.01
BROKER_RECONCILIATION_ATTRIBUTION_ABS_TOLERANCE = 1e-4
_RECONCILIATION_RECOVERABLE_HALT_REASONS = frozenset(
    {
        "broker_leg_reconciliation_failed",
        "broker_reconciliation_unavailable",
    }
)
_RECONCILIATION_FAILURE_OVERRIDABLE_HALT_REASONS = (
    _RECONCILIATION_RECOVERABLE_HALT_REASONS
    | frozenset(
        {
            "broker_quantity_reduction_pending_economic_fill",
            "stale_close_confirmation",
        }
    )
)
_STALE_CONFIRMATION_OVERRIDABLE_HALT_REASONS = frozenset(
    {"broker_quantity_reduction_pending_economic_fill"}
)


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
    pre_close_units: float | None = None  # None identifies legacy action attribution.


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
class _BrokerLegExpectation:
    position_id: str
    symbol: str
    book_units: float
    inventory_id: str
    pending_requested_units: float
    active_action_id: str | None = None


@dataclass(frozen=True)
class _BrokerReconciliationContext:
    expectations: tuple[_BrokerLegExpectation, ...]


@dataclass(frozen=True)
class _ReconciledCloseQuantity:
    action_id: str
    inventory_id: str
    position_id: str
    reconciled_book_units: float
    broker_units: float
    entry_price_basis: float
    attribution_confident: bool


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
        runtime_state_store: V3RuntimeStateStore | None = None,
    ) -> None:
        self.broker = broker
        self.task_runner = task_runner
        self.event_store = event_store
        self.book = book
        self.strategy_version = strategy_version
        self.model_version = model_version
        self.runtime_state_store = runtime_state_store or V3RuntimeStateStore(event_store.path)
        self.allocator = ProRataPartialCloseAllocator()
        self._pending_actions: set[str] = set()
        self._pending_close_confirmations: dict[str, _PendingCloseConfirmation] = {}
        self._confirmation_tasks: set[str] = set()
        self._active_close_mutations_by_position: dict[str, str] = {}
        self._active_close_context_by_action: dict[str, _CloseContext] = {}
        self._resolved_close_action_ids: set[str] = set()
        self._pending_economic_fill_action_ids: set[str] = set()
        self._reconciled_close_quantities: dict[str, _ReconciledCloseQuantity] = {}
        self._unattributed_reconciled_position_ids: set[str] = set()
        self.halted_reason: str | None = None
        self._confirmation_attempts = 0
        self._mutation_confirmation_attempts = 0
        self._economics_confirmation_attempts = 0
        self._confirmation_errors = 0
        self._confirmation_429 = 0
        self._confirmation_timeouts = 0
        self._confirmation_5xx = 0
        self._confirmation_recovered = 0
        self._stale_confirmation_actions_logged: set[str] = set()
        self._broker_unit_reductions_observed = 0
        self._broker_units_unavailable = 0
        self._broker_reconciliation_in_flight = False
        self._last_broker_reconciliation_monotonic: float | None = None
        self._broker_reconciliation_attempts = 0
        self._broker_reconciliation_errors = 0
        self._broker_reconciliation_mismatches = 0
        self._broker_reconciliation_stale_results = 0
        self._broker_reconciliation_recovered = 0
        self._last_broker_reconciliation_status = "never"
        self._last_broker_reconciliation_issues: tuple[str, ...] = ()

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
        strategy_target_units = (
            inventory.total_units * fraction
            if fraction > 0
            else intent.notional / max(float(snapshot.bid), 1e-12)
        )
        strategy_target_units = min(inventory.total_units, strategy_target_units)
        remaining_fraction = max(
            0.0,
            (inventory.total_units - strategy_target_units)
            / max(inventory.total_units, 1e-12),
        )
        projected_remaining_notional = max(
            0.0,
            inventory.total_notional * remaining_fraction,
        )
        dust_collapse = bool(
            intent.purpose == IntentPurpose.PROFIT_EXIT
            and strategy_target_units < inventory.total_units
            and 0.0 < projected_remaining_notional < POINT_M_DUST_NOTIONAL_USD
        )
        target_units = inventory.total_units if dust_collapse else strategy_target_units
        plan = self.allocator.plan(inventory, target_units)
        if not plan.requests:
            return False

        execution_fraction = plan.target_units / max(inventory.total_units, 1e-12)
        # A plan is pro-rata across its legs. Wait for an active mutation instead
        # of silently submitting only the currently unlocked subset of the plan.
        if any(request.position_id in self._active_close_mutations_by_position
               for request in plan.requests):
            return False
        scheduled = False
        for request in plan.requests:
            action_id = f"{intent.intent_id}:{request.position_id}"
            if (action_id in self._pending_actions
                    or action_id in self._pending_close_confirmations
                    or action_id in self._resolved_close_action_ids):
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
                    "pre_close_units": self._current_leg_units(request.position_id),
                    "full_close": request.full_close,
                    "trigger_price": float(snapshot.bid),
                    "strategy_close_fraction": fraction,
                    "execution_close_fraction": execution_fraction,
                    "projected_remaining_notional_usd": projected_remaining_notional,
                    "dust_threshold_usd": POINT_M_DUST_NOTIONAL_USD,
                    "dust_collapse": dust_collapse,
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
                pre_close_units=self._current_leg_units(request.position_id),
            )
            self.task_runner.submit(
                kind="close_position",
                task_id=f"v3-close:{action_id}",
                context=context,
                operation=lambda current=context: self._submit_broker_close(current),
                lane=BrokerTaskLane.CLOSE,
            )
            self._pending_actions.add(action_id)
            self._active_close_mutations_by_position[request.position_id] = action_id
            self._active_close_context_by_action[action_id] = context
            scheduled = True
        return scheduled

    def _submit_broker_close(self, context: _CloseContext):
        if context.full_close:
            return self.broker.close_position(context.position_id)
        return self.broker.close_position(
            context.position_id,
            units_to_deduct=context.requested_units,
        )

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
            elif completion.kind == "v3_broker_reconciliation":
                self._handle_broker_reconciliation(completion)
        return tuple(applied)

    def schedule_close_confirmation_checks(
        self,
        *,
        monotonic_now: float | None = None,
        utc_now: datetime | None = None,
    ) -> int:
        now = time.monotonic() if monotonic_now is None else float(monotonic_now)
        self._refresh_stale_confirmation_halt(utc_now or _utc_now())
        if self._confirmation_tasks or self._broker_reconciliation_in_flight:
            return 0
        due = [
            (pending.next_attempt_monotonic, pending.accepted_at, action_id, pending)
            for action_id, pending in self._pending_close_confirmations.items()
            if pending.next_attempt_monotonic <= now
        ]
        if due:
            _, _, action_id, pending = min(due, key=lambda item: item[:3])
            pending.attempt_count += 1
            self._confirmation_attempts += 1
            if pending.mutation_active:
                self._mutation_confirmation_attempts += 1
            else:
                self._economics_confirmation_attempts += 1
            # Reserve a UTC retry deadline before dispatch, including crash during GET.
            self._defer_confirmation(pending, monotonic_now=now, utc_now=utc_now,
                                     result_state="lookup_in_flight")
            self._confirmation_tasks.add(action_id)
            self.task_runner.submit(
                kind="v3_close_execution_lookup",
                task_id=f"v3-close-confirm:{action_id}",
                context=pending,
                operation=lambda current=pending: self.broker.get_close_execution(
                    current.close_order_id,
                    current.context.position_id,
                ),
                lane=BrokerTaskLane.QUERY,
            )
            return 1
        return self._schedule_broker_reconciliation(now)

    def _schedule_broker_reconciliation(self, monotonic_now: float) -> int:
        previous = self._last_broker_reconciliation_monotonic
        if previous is None:
            self._last_broker_reconciliation_monotonic = monotonic_now
            return 0
        if monotonic_now - previous < BROKER_RECONCILIATION_INTERVAL_SECONDS:
            return 0
        context = self._broker_reconciliation_context()
        self._last_broker_reconciliation_monotonic = monotonic_now
        if not context.expectations:
            return 0
        position_ids = tuple(item.position_id for item in context.expectations)
        self._broker_reconciliation_in_flight = True
        self._broker_reconciliation_attempts += 1
        self.task_runner.submit(
            kind="v3_broker_reconciliation",
            task_id="v3-broker-reconciliation",
            context=context,
            operation=lambda ids=position_ids: self.broker.get_open_position_units(ids),
            lane=BrokerTaskLane.QUERY,
        )
        return 1

    def restore_pending_close_confirmations(
        self, events: Iterable[InventoryEvent], *,
        utc_now: datetime | None = None, monotonic_now: float | None = None,
    ) -> None:
        contexts: dict[str, _CloseContext] = {}
        accepted: dict[str, _PendingCloseConfirmation] = {}
        resolved: set[str] = set()
        quantities: dict[str, _ReconciledCloseQuantity] = {}
        for event in events:
            payload = event.payload
            action_id = str(payload.get("action_id", ""))
            if event.event_type in {"CLOSE_SUBMISSION_STARTED", "CLOSE_SUBMISSION_ACCEPTED"} and action_id:
                position_id = str(payload["position_id"])
                full_close = bool(payload.get("full_close", True))
                requested = float(payload.get("requested_units", 0.0))
                if full_close and requested <= 0:
                    requested = self._current_leg_units(position_id)
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
                )
                contexts[action_id] = context
                if event.event_type == "CLOSE_SUBMISSION_ACCEPTED":
                    accepted[action_id] = _PendingCloseConfirmation(
                        context, str(payload["close_order_id"]), event.occurred_at,
                    )
            elif event.event_type == "BROKER_QUANTITY_RECONCILED":
                position_id = str(payload["position_id"])
                confident = bool(payload.get("attribution_confident", False))
                ids = [str(value) for value in payload.get("action_ids", [])]
                if not confident:
                    self._unattributed_reconciled_position_ids.add(position_id)
                if len(ids) == 1:
                    quantities.setdefault(ids[0], _ReconciledCloseQuantity(
                        ids[0], event.inventory_id, position_id,
                        float(payload["reconciled_book_units"]), float(payload["broker_units"]),
                        float(payload["entry_price_basis"]), confident,
                    ))
            elif event.event_type == "BROKER_RECONCILIATION_ACKNOWLEDGED":
                self._unattributed_reconciled_position_ids.discard(str(payload["position_id"]))
                resolved.update(str(value) for value in payload["abandoned_action_ids"])
            elif event.event_type in {"EXIT_FILLED", "EXIT_ECONOMICS_CONFIRMED", "CLOSE_SUBMISSION_FAILED"} and action_id:
                resolved.add(action_id)
                if event.event_type == "EXIT_ECONOMICS_CONFIRMED" and not payload.get("attribution_confident", True):
                    self._unattributed_reconciled_position_ids.add(str(payload["position_id"]))

        self._resolved_close_action_ids.update(resolved)
        saved_retries = self.runtime_state_store.load_close_retries()
        now = _as_utc(utc_now or _utc_now())
        mono = time.monotonic() if monotonic_now is None else monotonic_now
        for action_id, context in contexts.items():
            if action_id in resolved:
                continue
            pending = accepted.get(action_id)
            quantity = quantities.get(action_id)
            if pending is not None:
                if quantity is not None:
                    self._reconciled_close_quantities[action_id] = quantity
                    self._pending_economic_fill_action_ids.add(action_id)
                    pending.quantity_resolved = True
                    pending.attribution_confident = quantity.attribution_confident
                    pending.mutation_active = not quantity.attribution_confident
                retry = saved_retries.get(action_id)
                if retry is not None:
                    pending.attempt_count = retry.attempt_count
                    pending.next_attempt_at = retry.next_attempt_at
                    pending.next_attempt_monotonic = mono + max(0.0, (retry.next_attempt_at - now).total_seconds())
                    pending.last_error_type = retry.last_error_type
                    pending.last_http_status = retry.last_http_status
                    pending.last_result_state = retry.last_result_state
                    pending.error_active = retry.last_result_state == "error"
                self._pending_close_confirmations[action_id] = pending
            if pending is None or pending.mutation_active:
                old = self._active_close_mutations_by_position.get(context.position_id)
                if old is not None and old != action_id:
                    raise RuntimeError("Multiple unresolved close mutations on one broker leg")
                self._active_close_mutations_by_position[context.position_id] = action_id
                self._active_close_context_by_action[action_id] = context
                self._pending_actions.add(action_id)
        for action_id in saved_retries.keys() - self._pending_close_confirmations.keys():
            self.runtime_state_store.delete_close_retry(action_id)
        if self._unattributed_reconciled_position_ids:
            self.halted_reason = "broker_quantity_reduction_unattributed"

    def _current_leg_units(self, position_id: str) -> float:
        for inventory in self.book.inventories:
            for leg in inventory.broker_legs:
                if leg.position_id == position_id:
                    return float(leg.units)
        return 0.0

    def _broker_reconciliation_context(
        self,
        *,
        remember_positions: bool = False,
    ) -> _BrokerReconciliationContext:
        expectations: list[_BrokerLegExpectation] = []
        for inventory in self.book.inventories:
            for leg in inventory.broker_legs:
                if leg.units <= 0:
                    continue
                if remember_positions:
                    self.broker.remember_position_instrument(
                        leg.position_id,
                        inventory.symbol,
                    )
                active_id = self._active_close_mutations_by_position.get(leg.position_id)
                active = self._active_close_context_by_action.get(active_id)
                pending_units = active.requested_units if active else 0.0
                expectations.append(
                    _BrokerLegExpectation(
                        position_id=leg.position_id,
                        symbol=inventory.symbol,
                        book_units=float(leg.units),
                        inventory_id=inventory.inventory_id,
                        pending_requested_units=float(pending_units),
                        active_action_id=active_id,
                    )
                )
        return _BrokerReconciliationContext(
            expectations=tuple(sorted(expectations, key=lambda item: item.position_id))
        )

    def verify_known_broker_legs(self) -> tuple[str, ...]:
        context = self._broker_reconciliation_context(remember_positions=True)
        if not context.expectations:
            self._last_broker_reconciliation_monotonic = time.monotonic()
            self._last_broker_reconciliation_status = "ok"
            return ()

        self._broker_reconciliation_attempts += 1
        try:
            units_by_position = self.broker.get_open_position_units(
                item.position_id for item in context.expectations
            )
        except Exception:
            self._broker_reconciliation_errors += 1
            self._last_broker_reconciliation_status = "unavailable"
            raise
        finally:
            self._last_broker_reconciliation_monotonic = time.monotonic()

        issues, reductions_observed = self._compare_broker_units(context, units_by_position)
        if issues:
            self._broker_reconciliation_mismatches += 1
            self._last_broker_reconciliation_status = "mismatch"
            self._last_broker_reconciliation_issues = issues
            self.halted_reason = "broker_leg_reconciliation_failed"
        elif reductions_observed:
            self._last_broker_reconciliation_status = "pending_economic_fill"
            self._last_broker_reconciliation_issues = ()
            if self._unattributed_reconciled_position_ids:
                self.halted_reason = "broker_quantity_reduction_unattributed"

        else:
            self._last_broker_reconciliation_status = "ok"
            self._last_broker_reconciliation_issues = ()
        self._refresh_stale_confirmation_halt(_utc_now())
        return issues

    def _handle_broker_reconciliation(
        self,
        completion: BrokerTaskCompletion,
    ) -> None:
        self._broker_reconciliation_in_flight = False
        context = completion.context
        if not isinstance(context, _BrokerReconciliationContext):
            self._broker_reconciliation_errors += 1
            self._last_broker_reconciliation_status = "invalid_context"
            if (
                self.halted_reason is None
                or self.halted_reason
                in _RECONCILIATION_FAILURE_OVERRIDABLE_HALT_REASONS
            ):
                self.halted_reason = "broker_reconciliation_unavailable"
            return

        current_context = self._broker_reconciliation_context()
        if current_context != context:
            self._broker_reconciliation_stale_results += 1
            self._last_broker_reconciliation_status = "stale"
            self._last_broker_reconciliation_issues = ()
            self._last_broker_reconciliation_monotonic = (
                time.monotonic() - BROKER_RECONCILIATION_INTERVAL_SECONDS
            )
            self._refresh_stale_confirmation_halt(_utc_now())
            return

        previous_status = self._last_broker_reconciliation_status
        if completion.error is not None:
            self._broker_reconciliation_errors += 1
            self._last_broker_reconciliation_status = "unavailable"
            self._last_broker_reconciliation_issues = (
                f"{type(completion.error).__name__}:{completion.error}",
            )
            if (
                self.halted_reason is None
                or self.halted_reason
                in _RECONCILIATION_FAILURE_OVERRIDABLE_HALT_REASONS
            ):
                self.halted_reason = "broker_reconciliation_unavailable"
            return

        units_by_position = completion.value
        if not isinstance(units_by_position, dict):
            self._broker_reconciliation_errors += 1
            self._last_broker_reconciliation_status = "invalid_response"
            self._last_broker_reconciliation_issues = (
                "broker_units_response_not_mapping",
            )
            if (
                self.halted_reason is None
                or self.halted_reason
                in _RECONCILIATION_FAILURE_OVERRIDABLE_HALT_REASONS
            ):
                self.halted_reason = "broker_reconciliation_unavailable"
            return

        issues, reductions_observed = self._compare_broker_units(context, units_by_position)
        if issues:
            self._broker_reconciliation_mismatches += 1
            self._last_broker_reconciliation_status = "mismatch"
            self._last_broker_reconciliation_issues = issues
            if (
                self.halted_reason is None
                or self.halted_reason
                in _RECONCILIATION_FAILURE_OVERRIDABLE_HALT_REASONS
            ):
                self.halted_reason = "broker_leg_reconciliation_failed"
            return

        self._last_broker_reconciliation_issues = ()
        if reductions_observed:
            self._last_broker_reconciliation_status = "pending_economic_fill"
            if self._unattributed_reconciled_position_ids:
                self.halted_reason = "broker_quantity_reduction_unattributed"
            elif self.halted_reason in _RECONCILIATION_RECOVERABLE_HALT_REASONS:
                self.halted_reason = None
            self._refresh_stale_confirmation_halt(_utc_now())
            return

        self._last_broker_reconciliation_status = "ok"
        if previous_status in {"mismatch", "unavailable", "invalid_response"}:
            self._broker_reconciliation_recovered += 1
        if self.halted_reason in _RECONCILIATION_RECOVERABLE_HALT_REASONS:
            self.halted_reason = None
        self._refresh_stale_confirmation_halt(_utc_now())

    def _compare_broker_units(
        self, context: _BrokerReconciliationContext,
        units_by_position: dict[str, float | None],
    ) -> tuple[tuple[str, ...], bool]:
        issues: list[str] = []
        reductions_observed = False
        for expectation in context.expectations:
            value = units_by_position.get(expectation.position_id)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                self._broker_units_unavailable += 1
                issues.append(f"{expectation.position_id}:broker_units_unavailable_or_invalid")
                continue
            broker_units = float(value)
            if _units_close(broker_units, expectation.book_units):
                continue
            if broker_units < expectation.book_units:
                reductions_observed = True
                self._reconcile_broker_quantity_reduction(
                    expectation=expectation, broker_units=broker_units,
                    allow_action_attribution=True,
                )
                continue
            issues.append(f"{expectation.position_id}:book={expectation.book_units:.12g}:broker={broker_units:.12g}")
        return tuple(sorted(issues)), reductions_observed

    def _reconcile_broker_quantity_reduction(
        self,
        *,
        expectation: _BrokerLegExpectation,
        broker_units: float,
        allow_action_attribution: bool,
    ) -> None:
        inventory = next(
            item
            for item in self.book.inventories
            if item.inventory_id == expectation.inventory_id
        )
        leg = next(
            item
            for item in inventory.broker_legs
            if item.position_id == expectation.position_id
        )
        previous_units = float(leg.units)
        actual_broker_units = max(0.0, float(broker_units))
        reconciled_book_units = previous_units - actual_broker_units
        if reconciled_book_units <= BROKER_UNIT_ABS_TOLERANCE:
            return

        active_id = self._active_close_mutations_by_position.get(expectation.position_id)
        matching_action_ids = [active_id] if active_id else []
        pending = self._pending_close_confirmations.get(active_id)
        attribution_confident = False
        if allow_action_attribution and pending is not None and pending.mutation_active:
            context = pending.context
            baseline_matches = (context.pre_close_units is None or
                                _units_close(previous_units, context.pre_close_units))
            compare = _migration_units_close if context.pre_close_units is None else _units_close
            attribution_confident = baseline_matches and compare(reconciled_book_units, context.requested_units)

        previous_account_notional = (
            float(leg.account_notional)
            if leg.account_notional is not None
            else previous_units * float(leg.entry_price)
        )
        remaining_account_notional = (
            0.0
            if previous_units <= 0
            else previous_account_notional * actual_broker_units / previous_units
        )
        observed_at = _utc_now()
        inserted = self._append(
            event_type="BROKER_QUANTITY_RECONCILED",
            inventory_id=expectation.inventory_id,
            event_id=(
                f"{expectation.position_id}:broker-quantity-reconciled:"
                f"{actual_broker_units:.12g}"
            ),
            payload={
                "position_id": expectation.position_id,
                "symbol": expectation.symbol,
                "previous_book_units": previous_units,
                "broker_units": actual_broker_units,
                "reconciled_book_units": reconciled_book_units,
                "entry_price_basis": float(leg.entry_price),
                "previous_account_notional": previous_account_notional,
                "remaining_account_notional": remaining_account_notional,
                "pending_requested_units": expectation.pending_requested_units,
                "economic_fill_pending": True,
                "action_ids": matching_action_ids,
                "attribution_confident": attribution_confident,
                "source": "broker_portfolio",
            },
            occurred_at=observed_at,
        )
        if inserted or not _units_close(
            self._current_leg_units(expectation.position_id),
            actual_broker_units,
        ):
            self.book.reconcile_broker_leg_units(
                position_id=expectation.position_id,
                broker_units=actual_broker_units,
                observed_at=observed_at,
            )

        if inserted:
            self._broker_unit_reductions_observed += 1

        if allow_action_attribution and len(matching_action_ids) == 1:
            action_id = matching_action_ids[0]
            self._pending_economic_fill_action_ids.add(action_id)
            self._reconciled_close_quantities.setdefault(action_id, _ReconciledCloseQuantity(
                action_id=action_id,
                inventory_id=expectation.inventory_id,
                position_id=expectation.position_id,
                reconciled_book_units=reconciled_book_units,
                broker_units=actual_broker_units,
                entry_price_basis=float(leg.entry_price),
                attribution_confident=attribution_confident,
            ))
            if pending is not None:
                pending.quantity_resolved = True
                pending.attribution_confident = attribution_confident
                if attribution_confident:
                    pending.mutation_active = False
                    self._release_close_mutation(pending.context)
                    self._defer_confirmation(pending, result_state="economics_pending")
            if not attribution_confident:
                self._unattributed_reconciled_position_ids.add(expectation.position_id)
        else:
            self._unattributed_reconciled_position_ids.add(expectation.position_id)

        if self._unattributed_reconciled_position_ids:
            self.halted_reason = "broker_quantity_reduction_unattributed"
        elif self.halted_reason in {"broker_quantity_reduction_pending_economic_fill", "stale_close_confirmation"}:
            self.halted_reason = None

    def _refresh_stale_confirmation_halt(self, now: datetime) -> None:
        actual_now = _as_utc(now)
        stale: list[tuple[str, _PendingCloseConfirmation, float]] = []
        for action_id, pending in self._pending_close_confirmations.items():
            age = max(
                0.0,
                (actual_now - _as_utc(pending.accepted_at)).total_seconds(),
            )
            if pending.mutation_active and age >= CONFIRMATION_STALE_HALT_SECONDS:
                stale.append((action_id, pending, age))

        if stale:
            if self._unattributed_reconciled_position_ids:
                self.halted_reason = "broker_quantity_reduction_unattributed"
            elif (
                self.halted_reason is None
                or self.halted_reason in _STALE_CONFIRMATION_OVERRIDABLE_HALT_REASONS
            ):
                self.halted_reason = "stale_close_confirmation"
            for action_id, pending, age in stale:
                if action_id in self._stale_confirmation_actions_logged:
                    continue
                self._stale_confirmation_actions_logged.add(action_id)
                self._append(
                    event_type="CLOSE_CONFIRMATION_STALE",
                    inventory_id=pending.context.inventory_id,
                    event_id=f"{action_id}:close-confirmation-stale",
                    payload={
                        "action_id": action_id,
                        "position_id": pending.context.position_id,
                        "close_order_id": pending.close_order_id,
                        "accepted_at": pending.accepted_at,
                        "age_seconds": age,
                        "stale_halt_seconds": CONFIRMATION_STALE_HALT_SECONDS,
                    },
                )
            return

        if self.halted_reason == "stale_close_confirmation":
            if self._unattributed_reconciled_position_ids:
                self.halted_reason = "broker_quantity_reduction_unattributed"

            else:
                self.halted_reason = None
        elif self.halted_reason is None:
            if self._unattributed_reconciled_position_ids:
                self.halted_reason = "broker_quantity_reduction_unattributed"


    def confirmation_metrics(self) -> dict[str, object]:
        now = _utc_now()
        oldest_seconds = 0.0
        stale_count = 0
        if self._pending_close_confirmations:
            oldest = min(
                pending.accepted_at
                for pending in self._pending_close_confirmations.values()
            )
            oldest_seconds = max(0.0, (now - _as_utc(oldest)).total_seconds())
            stale_count = sum(
                1
                for pending in self._pending_close_confirmations.values()
                if (now - _as_utc(pending.accepted_at)).total_seconds()
                >= CONFIRMATION_STALE_HALT_SECONDS
            )
        broker_rate_limit = {}
        getter = getattr(self.broker, "get_rate_limit_metrics", None)
        if callable(getter):
            try:
                broker_rate_limit = dict(getter())
            except Exception:
                broker_rate_limit = {"status": "unavailable"}
        return {
            "pending": len(self._pending_close_confirmations),
            "in_flight": len(self._confirmation_tasks),
            "attempts": self._confirmation_attempts,
            "mutation_confirmation_attempts": self._mutation_confirmation_attempts,
            "economics_only_confirmation_attempts": self._economics_confirmation_attempts,
            "active_close_mutation_count": len(self._active_close_mutations_by_position),
            "active_close_mutations": [
                {"action_id": c.action_id, "position_id": c.position_id,
                 "requested_units": c.requested_units, "pre_close_units": c.pre_close_units,
                 "confirmation_order_known": c.action_id in self._pending_close_confirmations}
                for c in self._active_close_context_by_action.values()
            ],
            "pending_economic_confirmation_count": sum(not p.mutation_active for p in self._pending_close_confirmations.values()),
            "unattributed_reconciliation_count": len(self._unattributed_reconciled_position_ids),
            "stale_mutation_count": sum(p.mutation_active and (now - _as_utc(p.accepted_at)).total_seconds() >= CONFIRMATION_STALE_HALT_SECONDS for p in self._pending_close_confirmations.values()),
            "stale_economics_only_count": sum(not p.mutation_active and (now - _as_utc(p.accepted_at)).total_seconds() >= CONFIRMATION_STALE_HALT_SECONDS for p in self._pending_close_confirmations.values()),
            "errors": self._confirmation_errors,
            "http_429": self._confirmation_429,
            "timeouts": self._confirmation_timeouts,
            "http_5xx": self._confirmation_5xx,
            "recovered": self._confirmation_recovered,
            "oldest_pending_seconds": oldest_seconds,
            "stale_halt_seconds": CONFIRMATION_STALE_HALT_SECONDS,
            "stale_pending_count": stale_count,
            "pending_economic_fill_count": len(self._pending_economic_fill_action_ids),
            "pending_economic_fill_action_ids": sorted(
                self._pending_economic_fill_action_ids
            ),
            "unattributed_reconciled_position_ids": sorted(
                self._unattributed_reconciled_position_ids
            ),
            "broker_quantity_reductions_observed": self._broker_unit_reductions_observed,
            "broker_units_unavailable": self._broker_units_unavailable,
            "broker_get_rate_limit": broker_rate_limit,
            "broker_reconciliation": {
                "interval_seconds": BROKER_RECONCILIATION_INTERVAL_SECONDS,
                "in_flight": self._broker_reconciliation_in_flight,
                "attempts": self._broker_reconciliation_attempts,
                "errors": self._broker_reconciliation_errors,
                "mismatches": self._broker_reconciliation_mismatches,
                "stale_results": self._broker_reconciliation_stale_results,
                "recovered": self._broker_reconciliation_recovered,
                "last_status": self._last_broker_reconciliation_status,
                "last_issues": list(self._last_broker_reconciliation_issues),
            },
        }

    def pending_close_confirmation_snapshot(self) -> list[dict[str, object]]:
        return [
            {
                "action_id": action_id,
                "intent_id": pending.context.intent.intent_id,
                "inventory_id": pending.context.inventory_id,
                "symbol": pending.context.intent.symbol,
                "position_id": pending.context.position_id,
                "requested_units": pending.context.requested_units,
                "full_close": pending.context.full_close,
                "trigger_price": pending.context.trigger_price,
                "close_order_id": pending.close_order_id,
                "accepted_at": pending.accepted_at,
                "attempt_count": pending.attempt_count,
                "next_attempt_at": pending.next_attempt_at,
                "mutation_active": pending.mutation_active,
                "quantity_resolved": pending.quantity_resolved,
                "attribution_confident": pending.attribution_confident,
                "economics_pending": pending.economics_pending,
                "pre_close_units": pending.context.pre_close_units,
                "last_result_state": pending.last_result_state,
                "last_error_type": pending.last_error_type,
                "last_http_status": pending.last_http_status,
                "broker_quantity_reduction_observed": (
                    action_id in self._pending_economic_fill_action_ids
                ),
                "reconciled_book_units": (
                    None
                    if action_id not in self._reconciled_close_quantities
                    else self._reconciled_close_quantities[action_id].reconciled_book_units
                ),
            }
            for action_id, pending in sorted(self._pending_close_confirmations.items())
        ]

    def _handle_open_completion(self, completion: BrokerTaskCompletion) -> list[str]:
        context = completion.context
        if not isinstance(context, _OpenContext):
            return []
        self._pending_actions.discard(context.action_id)
        if completion.error is not None:
            unknown = isinstance(completion.error, EtoroOrderConfirmationUnknownError)
            self._append(
                event_type=(
                    "ORDER_SUBMISSION_UNKNOWN"
                    if unknown
                    else "ORDER_SUBMISSION_FAILED"
                ),
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
        if not math.isfinite(price) or price <= 0:
            self.halted_reason = "invalid_open_execution_price"
            return []

        if result.executed_units is not None:
            units = float(result.executed_units)
            units_source = "broker_confirmed"
        elif result.executed_entry_price is None:
            units = context.intent.notional / price
            units_source = "paper_derived"
        else:
            self.halted_reason = "open_execution_units_missing"
            self._append(
                event_type="ORDER_SUBMISSION_UNKNOWN",
                inventory_id=context.inventory_id,
                event_id=f"{context.action_id}:open-units-unknown",
                payload={
                    "action_id": context.action_id,
                    "intent_id": context.intent.intent_id,
                    "symbol": context.intent.symbol,
                    "position_id": result.position_id,
                    "executed_entry_price": result.executed_entry_price,
                    "reason": "confirmed_position_units_missing",
                },
            )
            return []

        if not math.isfinite(units) or units <= 0:
            self.halted_reason = "invalid_open_execution_units"
            self._append(
                event_type="ORDER_SUBMISSION_UNKNOWN",
                inventory_id=context.inventory_id,
                event_id=f"{context.action_id}:open-units-invalid",
                payload={
                    "action_id": context.action_id,
                    "intent_id": context.intent.intent_id,
                    "symbol": context.intent.symbol,
                    "position_id": result.position_id,
                    "executed_units": result.executed_units,
                    "reason": "invalid_confirmed_position_units",
                },
            )
            return []

        if result.executed_notional is not None:
            account_notional = float(result.executed_notional)
            notional_source = "broker_confirmed_account_currency"
        else:
            account_notional = float(context.intent.notional)
            notional_source = "requested_account_currency"
        if not math.isfinite(account_notional) or account_notional <= 0:
            self.halted_reason = "invalid_open_account_notional"
            return []

        payload = {
            "action_id": context.action_id,
            "intent_id": context.intent.intent_id,
            "symbol": context.intent.symbol,
            "position_id": result.position_id,
            "units": units,
            "units_source": units_source,
            "price": price,
            "requested_notional": context.intent.notional,
            "notional": account_notional,
            "notional_source": notional_source,
            "asset_notional_at_entry": units * price,
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
            account_notional=account_notional,
            fee=0.0,
            filled_at=_utc_now(),
        )
        return [context.intent.intent_id]

    def _handle_close_submission(self, completion: BrokerTaskCompletion) -> list[str]:
        context = completion.context
        assert isinstance(context, _CloseContext)
        if completion.error is not None:
            unknown = isinstance(completion.error, ClosePositionSubmissionUnknownError)
            if not unknown:
                self._release_close_mutation(context)
            self._append(
                event_type=(
                    "CLOSE_SUBMISSION_UNKNOWN"
                    if unknown
                    else "CLOSE_SUBMISSION_FAILED"
                ),
                inventory_id=context.inventory_id,
                event_id=f"{context.action_id}:close-error",
                payload={
                    "action_id": context.action_id,
                    "position_id": context.position_id,
                    "requested_units": context.requested_units,
                    "pre_close_units": context.pre_close_units,
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
                    "pre_close_units": context.pre_close_units,
                    "full_close": context.full_close,
                },
            )
            return []

        accepted_at = getattr(submission, "accepted_at", None) or _utc_now()
        pending = _PendingCloseConfirmation(
            context=context,
            close_order_id=str(close_order_id),
            accepted_at=accepted_at,
            next_attempt_monotonic=time.monotonic() + CONFIRMATION_INITIAL_DELAY_SECONDS,
        )
        self._pending_close_confirmations[context.action_id] = pending
        self._defer_confirmation(pending, delay=CONFIRMATION_INITIAL_DELAY_SECONDS,
                                 result_state="submitted")
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
                "pre_close_units": context.pre_close_units,
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
        if action_id in self._resolved_close_action_ids:
            return []
        if completion.error is not None:
            self._confirmation_errors += 1
            status = _http_status(completion.error)
            pending.last_error_type = type(completion.error).__name__
            pending.last_http_status = status
            if status == 429:
                self._confirmation_429 += 1
            if isinstance(completion.error, requests.Timeout):
                self._confirmation_timeouts += 1
            if status is not None and 500 <= status < 600:
                self._confirmation_5xx += 1
            self._defer_confirmation(pending, status=status, error=completion.error,
                                     result_state="error")
            if not pending.error_active:
                pending.error_active = True
                self._append(
                    event_type="CLOSE_CONFIRMATION_ERROR",
                    inventory_id=pending.context.inventory_id,
                    event_id=f"{action_id}:close-confirmation-error",
                    payload={
                        "action_id": action_id,
                        "position_id": pending.context.position_id,
                        "close_order_id": pending.close_order_id,
                        "error": str(completion.error),
                        "error_type": pending.last_error_type,
                        "http_status": status,
                        "attempt_count": pending.attempt_count,
                    },
                )
            self._refresh_stale_confirmation_halt(_utc_now())
            return []
        execution = completion.value
        if execution is None:
            pending.last_error_type = None
            pending.last_http_status = None
            self._defer_confirmation(pending, result_state="execution_unavailable")
            self._refresh_stale_confirmation_halt(_utc_now())
            return []
        if not isinstance(execution, BrokerCloseExecution):
            self._defer_confirmation(pending, result_state="invalid_close_execution")
            return []
        if execution.units is None:
            self._defer_confirmation(pending, result_state="close_execution_units_missing")
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
        executed_units = float(execution.units)
        if str(execution.position_id) != pending.context.position_id or str(execution.close_order_id) != pending.close_order_id:
            self._defer_confirmation(pending, result_state="close_execution_identity_mismatch")
            return []
        if pending.error_active:
            self._confirmation_recovered += 1
            self._append(
                event_type="CLOSE_CONFIRMATION_RECOVERED",
                inventory_id=pending.context.inventory_id,
                event_id=f"{action_id}:close-confirmation-recovered",
                payload={
                    "action_id": action_id,
                    "position_id": pending.context.position_id,
                    "close_order_id": pending.close_order_id,
                    "attempt_count": pending.attempt_count,
                },
            )
        confirmed = self._confirm_close(
            context=pending.context,
            exit_price=float(execution.executed_exit_price),
            filled_at=execution.executed_at or _utc_now(),
            close_order_id=pending.close_order_id,
            executed_units=executed_units,
        )
        if not confirmed:
            self._defer_confirmation(pending, result_state="invalid_execution")
        return [pending.context.intent.intent_id] if confirmed else []

    def _confirm_close(
        self,
        *,
        context: _CloseContext,
        exit_price: float,
        filled_at: datetime,
        close_order_id: str,
        executed_units: float,
    ) -> bool:
        if context.action_id in self._resolved_close_action_ids:
            return False
        if not math.isfinite(exit_price) or exit_price <= 0 or not math.isfinite(executed_units) or executed_units <= 0:
            self.halted_reason = "close_execution_invalid"
            return False
        reconciled = self._reconciled_close_quantities.get(context.action_id)
        if reconciled is not None:
            compare = _migration_units_close if context.pre_close_units is None else _units_close
            attribution_confident = reconciled.attribution_confident and compare(
                executed_units, reconciled.reconciled_book_units,
            )
            if not attribution_confident:
                self._unattributed_reconciled_position_ids.add(context.position_id)
                if self.halted_reason in {
                    None,
                    "broker_quantity_reduction_pending_economic_fill",
                    "stale_close_confirmation",
                }:
                    self.halted_reason = "broker_quantity_reduction_unattributed"
            confirmed_at = _utc_now()
            inserted = self._append(
                event_type="EXIT_ECONOMICS_CONFIRMED",
                inventory_id=reconciled.inventory_id,
                event_id=f"{context.action_id}:exit-economics:{close_order_id}",
                payload={
                    "action_id": context.action_id,
                    "intent_id": context.intent.intent_id,
                    "symbol": context.intent.symbol,
                    "position_id": context.position_id,
                    "units": executed_units,
                    "requested_units": context.requested_units,
                    "pre_close_units": context.pre_close_units,
                    "reconciled_book_units": reconciled.reconciled_book_units,
                    "migration_unit_delta": (
                        executed_units - reconciled.reconciled_book_units
                    ),
                    "broker_remaining_units": reconciled.broker_units,
                    "entry_price_basis": reconciled.entry_price_basis,
                    "price": exit_price,
                    "fee": 0.0,
                    "close_order_id": close_order_id,
                    "purpose": context.intent.purpose.value,
                    "executed_at": filled_at,
                    "quantity_already_reconciled": True,
                    "attribution_confident": attribution_confident,
                },
                occurred_at=confirmed_at,
            )
            if not inserted:
                return False
            self.book.apply_exit_economics(
                inventory_id=reconciled.inventory_id,
                position_id=context.position_id,
                exit_price=exit_price,
                units=executed_units,
                entry_price_basis=reconciled.entry_price_basis,
                fee=0.0,
            )
            if reconciled.broker_units <= BROKER_UNIT_ABS_TOLERANCE:
                self.broker.forget_position_instrument(context.position_id)
            self._finalize_confirmed_close_action(context)
            return True

        inventory = self.book.active_for_symbol(context.intent.symbol)
        if inventory is None:
            self.halted_reason = "close_fill_without_inventory"
            return False
        leg = next(
            (
                item
                for item in inventory.broker_legs
                if item.position_id == context.position_id
            ),
            None,
        )
        if leg is None:
            self.halted_reason = "close_fill_without_broker_leg"
            return False
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
                    "pre_close_units": context.pre_close_units,
                    "executed_units": executed_units,
                    "leg_units": leg.units,
                },
            )
            return False

        confirmed_at = _utc_now()
        inserted = self._append(
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
                "pre_close_units": context.pre_close_units,
                "price": exit_price,
                "executed_at": filled_at,
                "fee": 0.0,
                "close_order_id": close_order_id,
                "purpose": context.intent.purpose.value,
            },
            occurred_at=confirmed_at,
        )
        if not inserted:
            return False
        updated = self.book.apply_exit_fill(
            position_id=context.position_id,
            exit_price=exit_price,
            units=executed_units,
            fee=0.0,
            filled_at=confirmed_at,
        )
        if not any(
            item.position_id == context.position_id for item in updated.broker_legs
        ):
            self.broker.forget_position_instrument(context.position_id)
        self._finalize_confirmed_close_action(context)
        return True

    def _release_close_mutation(self, context: _CloseContext) -> None:
        if self._active_close_mutations_by_position.get(context.position_id) == context.action_id:
            self._active_close_mutations_by_position.pop(context.position_id)
        self._active_close_context_by_action.pop(context.action_id, None)
        self._pending_actions.discard(context.action_id)

    def _defer_confirmation(
        self, pending: _PendingCloseConfirmation, *, delay: float | None = None,
        status: int | None = None, error: Exception | None = None,
        result_state: str = "pending", monotonic_now: float | None = None,
        utc_now: datetime | None = None,
    ) -> None:
        if delay is None:
            delay = _confirmation_backoff_seconds(
                pending.attempt_count, status=status, error=error,
                economics_only=not pending.mutation_active,
            )
        mono = time.monotonic() if monotonic_now is None else monotonic_now
        pending.next_attempt_monotonic = mono + delay
        pending.next_attempt_at = _as_utc(utc_now or _utc_now()) + timedelta(seconds=delay)
        pending.last_result_state = result_state
        self.runtime_state_store.save_close_retry(CloseRetryState(
            action_id=pending.context.action_id, attempt_count=pending.attempt_count,
            next_attempt_at=pending.next_attempt_at, last_error_type=pending.last_error_type,
            last_http_status=pending.last_http_status, last_result_state=result_state,
        ))

    def _finalize_confirmed_close_action(self, context: _CloseContext) -> None:
        self._pending_close_confirmations.pop(context.action_id, None)
        self._release_close_mutation(context)
        self._resolved_close_action_ids.add(context.action_id)
        self.runtime_state_store.delete_close_retry(context.action_id)
        self._pending_economic_fill_action_ids.discard(context.action_id)
        self._reconciled_close_quantities.pop(context.action_id, None)
        if self._unattributed_reconciled_position_ids:
            if self.halted_reason in {
                None,
                "broker_quantity_reduction_pending_economic_fill",
                "stale_close_confirmation",
            }:
                self.halted_reason = "broker_quantity_reduction_unattributed"
        elif (
            self.halted_reason == "broker_quantity_reduction_pending_economic_fill"
            and not self._pending_economic_fill_action_ids
        ):
            self.halted_reason = None
        self._refresh_stale_confirmation_halt(_utc_now())

    def _append(
        self,
        *,
        event_type: str,
        inventory_id: str,
        event_id: str,
        payload: dict,
        occurred_at: datetime | None = None,
    ) -> bool:
        return self.event_store.append(
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


def _units_close(left: float, right: float) -> bool:
    return math.isclose(
        float(left),
        float(right),
        rel_tol=BROKER_UNIT_REL_TOLERANCE,
        abs_tol=BROKER_UNIT_ABS_TOLERANCE,
    )


def _migration_units_close(left: float, right: float) -> bool:
    return math.isclose(
        float(left),
        float(right),
        rel_tol=BROKER_RECONCILIATION_ATTRIBUTION_REL_TOLERANCE,
        abs_tol=BROKER_RECONCILIATION_ATTRIBUTION_ABS_TOLERANCE,
    )


def _http_status(error: Exception) -> int | None:
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    return None if status is None else int(status)


def _confirmation_backoff_seconds(
    attempt_count: int,
    *,
    status: int | None = None,
    error: Exception | None = None,
    economics_only: bool = False,
) -> float:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", {}) or {}
    retry_after = headers.get("Retry-After") if hasattr(headers, "get") else None
    try:
        retry_after_seconds = float(retry_after) if retry_after not in (None, "") else 0.0
    except (TypeError, ValueError):
        retry_after_seconds = 0.0
    exponent = max(0, min(int(attempt_count) - 1, 12))
    calculated = min(
        ECONOMICS_CONFIRMATION_BACKOFF_MAX_SECONDS if economics_only else CONFIRMATION_BACKOFF_MAX_SECONDS,
        CONFIRMATION_BACKOFF_BASE_SECONDS * (2**exponent),
    )
    if status == 429:
        calculated = max(calculated, CONFIRMATION_429_MIN_SECONDS)
    return max(calculated, retry_after_seconds if math.isfinite(retry_after_seconds) else 0.0)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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
