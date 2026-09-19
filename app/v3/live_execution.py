"""V3 broker execution with durable same-symbol open authority.

The unchanged close/reconciliation code lives in ``_live_execution_impl``.
Expose that module directly to existing callers so monkeypatches of its clocks
and helpers still affect the functions in the original implementation.
"""

from __future__ import annotations

import math
import sys
from dataclasses import replace

from app.brokers.etoro.order_confirmation_error import EtoroOrderRejectedError
from app.brokers.etoro.pending_orders_preflight import pending_open_order_descriptions
from app.brokers.etoro.portfolio_position_parser import extract_open_position_units
from app.brokers.etoro.resilient_client import ResilientEtoroClient
from . import _live_execution_impl as _impl


class V3BrokerExecutor(_impl.V3BrokerExecutor):
    """Reserve BUY symbols before submission and fail closed on uncertain opens."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._open_by_symbol: dict[str, str] = {}
        self._open_action_ids: set[str] = set()
        self._unresolved_open_actions: set[str] = set()
        self._notional_anomaly_action_ids: set[str] = set()
        started: dict[str, str] = {}
        resolved: set[str] = set()
        unknown: set[str] = set()
        for event in self.event_store.events():
            action = str(event.payload.get("action_id", "")).strip()
            if not action:
                continue
            if event.event_type == "ORDER_SUBMISSION_STARTED":
                symbol = str(event.payload.get("symbol", "")).strip().upper()
                if not symbol:
                    symbol = event.inventory_id.split(":", 1)[0].strip().upper()
                started[action] = symbol
                self._open_action_ids.add(action)
            elif event.event_type in {"ENTRY_FILLED", "ORDER_SUBMISSION_FAILED"}:
                resolved.add(action)
            elif event.event_type == "ORDER_SUBMISSION_UNKNOWN":
                unknown.add(action)
            elif event.event_type == "OPEN_ACCOUNT_NOTIONAL_MISMATCH":
                self._notional_anomaly_action_ids.add(action)
        self._unresolved_open_actions = (set(started) - resolved) | unknown
        for action in sorted(self._unresolved_open_actions):
            symbol = started.get(action)
            if symbol:
                previous = self._open_by_symbol.setdefault(symbol, action)
                if previous != action:
                    self.halted_reason = "multiple_unresolved_open_submissions"
        if self._unresolved_open_actions and self.halted_reason is None:
            self.halted_reason = "unresolved_open_submission_at_restart"
        if self._notional_anomaly_action_ids and self.halted_reason is None:
            self.halted_reason = "open_account_notional_mismatch_at_restart"

    def verify_known_broker_legs(self) -> tuple[str, ...]:
        # Fresh SQLite has no known legs: the base verifier would return OK
        # without contacting eToro. Query the whole account, including pending
        # opens, and refuse any incomplete response rather than assume zero risk.
        broker = getattr(self.broker, "delegate", self.broker)
        if isinstance(broker, ResilientEtoroClient):
            known_ids = {
                str(leg.position_id)
                for inventory in self.book.inventories
                for leg in inventory.broker_legs
                if leg.units > 0
            }
            try:
                all_units = extract_open_position_units(broker.get_portfolio())
                pending_opens = pending_open_order_descriptions(broker)
            except Exception:
                self.halted_reason = "broker_account_preflight_unavailable"
                raise
            unexpected = tuple(
                f"untracked_broker_position:{position_id}:units={units:.12g}"
                for position_id, units in sorted(all_units.items())
                if units is not None and units > 0 and position_id not in known_ids
            )
            pending_issues = tuple(
                f"pending_broker_open:{description}" for description in pending_opens
            )
            issues = unexpected + pending_issues
            if issues:
                self.halted_reason = (
                    "pending_broker_open_orders" if pending_issues
                    else "untracked_broker_positions"
                )
                self._last_broker_reconciliation_issues = issues
                return issues
        return super().verify_known_broker_legs()

    def schedule(self, intent, *, snapshot):
        if intent.side.upper() != "BUY":
            # SELL reduce-only stays independent of the pending BUY lock.
            return super().schedule(intent, snapshot=snapshot)
        symbol = intent.symbol.strip().upper()
        action = str(intent.intent_id)
        if (not self.new_risk_allowed or symbol in self._open_by_symbol
                or action in self._open_action_ids):
            return False
        # Reserve before the durable START and before task_runner.submit() can
        # dispatch a broker request. No second intent can use the same symbol.
        self._open_by_symbol[symbol] = action
        self._unresolved_open_actions.add(action)
        self._open_action_ids.add(action)
        try:
            scheduled = super().schedule(intent, snapshot=snapshot)
        except Exception:
            # submit() may have reached eToro even when it raises locally.
            self.halted_reason = "open_submission_dispatch_unknown"
            raise
        if not scheduled:
            self._release_open(symbol, action)
            self._open_action_ids.discard(action)
        return scheduled

    def _release_open(self, symbol: str, action: str) -> None:
        if self._open_by_symbol.get(symbol) == action:
            self._open_by_symbol.pop(symbol, None)
        self._unresolved_open_actions.discard(action)

    def _handle_open_completion(self, completion):
        context = completion.context
        if not isinstance(context, _impl._OpenContext):
            return super()._handle_open_completion(completion)
        action = context.action_id
        symbol = context.intent.symbol.strip().upper()
        error = completion.error
        # Only a structured broker terminal status creates EtoroOrderRejectedError.
        # Error text, even a literal "eToro order rejected:", is never proof.
        if error is not None and not isinstance(error, EtoroOrderRejectedError):
            self._pending_actions.discard(action)
            self._append(
                event_type="ORDER_SUBMISSION_UNKNOWN",
                inventory_id=context.inventory_id,
                event_id=f"{action}:open-error-uncertain",
                payload={
                    "action_id": action,
                    "intent_id": context.intent.intent_id,
                    "symbol": symbol,
                    "reason": "open_broker_outcome_not_proven_rejected",
                    "error_type": type(error).__name__,
                    "error": str(error),
                },
            )
            self.halted_reason = "open_order_confirmation_unknown"
            return []

        result = completion.value
        if error is None and isinstance(result, _impl.OpenPositionResult):
            requested = float(context.intent.notional)
            reported_raw = result.executed_notional
            if reported_raw is not None and math.isfinite(requested) and requested > 0:
                try:
                    reported = float(reported_raw)
                except (TypeError, ValueError):
                    reported = math.nan
                inconsistent = (
                    isinstance(reported_raw, bool)
                    or not math.isfinite(reported)
                    or reported <= 0
                    or reported < 0.8 * requested
                    or reported > 1.2 * requested
                )
                if inconsistent:
                    # Preserve the broker's raw $1 amount for investigation.
                    # Requested account currency is provisional, not certified.
                    use_requested = not math.isfinite(reported) or reported < requested
                    booked = requested if use_requested else reported
                    self._append(
                        event_type="OPEN_ACCOUNT_NOTIONAL_MISMATCH",
                        inventory_id=context.inventory_id,
                        event_id=f"{action}:account-notional-mismatch",
                        payload={
                            "action_id": action,
                            "intent_id": context.intent.intent_id,
                            "symbol": symbol,
                            "position_id": result.position_id,
                            "requested_account_notional": requested,
                            "reported_account_notional": (
                                reported if math.isfinite(reported) else None
                            ),
                            "reported_raw": repr(reported_raw),
                            "booked_account_notional": booked,
                            "reason": "broker_account_notional_inconsistent_with_request",
                        },
                    )
                    self._notional_anomaly_action_ids.add(action)
                    self.halted_reason = "open_account_notional_mismatch"
                    if use_requested:
                        completion = replace(
                            completion, value=replace(result, executed_notional=None)
                        )
        try:
            applied = super()._handle_open_completion(completion)
        except Exception:
            # The confirmed fill may already be durable in the append-only
            # ledger. Never submit another BUY; replay and reconcile on restart.
            self.halted_reason = "open_ledger_projection_failed"
            raise
        if applied:
            self._release_open(symbol, action)
        elif isinstance(error, EtoroOrderRejectedError):
            self._release_open(symbol, action)
        else:
            if self.halted_reason is None:
                self.halted_reason = "invalid_open_completion"
            self._append(
                event_type="ORDER_SUBMISSION_UNKNOWN",
                inventory_id=context.inventory_id,
                event_id=f"{action}:open-incomplete-unknown",
                payload={
                    "action_id": action,
                    "intent_id": context.intent.intent_id,
                    "symbol": symbol,
                    "position_id": getattr(completion.value, "position_id", None),
                    "reason": "open_completion_not_projected",
                },
            )
        return applied

    def confirmation_metrics(self):
        result = super().confirmation_metrics()
        result["pending_open_symbols"] = dict(sorted(self._open_by_symbol.items()))
        result["unresolved_open_action_ids"] = sorted(self._unresolved_open_actions)
        result["account_notional_anomaly_action_ids"] = sorted(
            self._notional_anomaly_action_ids
        )
        return result


# Preserve the original module's globals for existing imports and test clock
# monkeypatches; the only replaced exported object is the guarded executor.
_impl.V3BrokerExecutor = V3BrokerExecutor
sys.modules[__name__] = _impl
