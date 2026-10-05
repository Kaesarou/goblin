"""Causal fill quarantine and durable, read-only economic revalidation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.brokers.base import BrokerPositionEconomics

# A deliberately conservative adapter alarm, not a strategy/slippage target.
# The S2 suspect prices deviated by 229-597 bp. Both directions are checked.
OPEN_PRICE_MAX_DEVIATION_BP = 100.0
OPEN_ECONOMICS_RETRY_SECONDS = 60.0
OPEN_ECONOMICS_RETRY_MAX_SECONDS = 300.0
OPEN_ECONOMICS_HALT_REASONS = frozenset({
    "open_account_notional_mismatch", "open_account_notional_mismatch_at_restart",
    "open_fill_economics_unresolved", "open_fill_economics_unresolved_at_restart",
})


def price_sanity(price: object, quote: dict) -> bool:
    ask = quote.get("ask")
    return (positive_number(price) and positive_number(ask)
            and abs(float(price) / float(ask) - 1) * 10_000
            <= OPEN_PRICE_MAX_DEVIATION_BP)


def positive_number(value: object) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (float, int))
            and math.isfinite(value) and value > 0)


@dataclass(frozen=True)
class OpenEconomicsCheck:
    action_id: str
    inventory_id: str
    position_id: str
    current_units: float
    fill: dict


class OpenEconomicsRecovery:
    def __init__(self, events):
        self.fills: dict[str, tuple[str, dict]] = {}
        self.price_anomalies: set[str] = set()
        self.retries: dict[str, tuple[int, datetime]] = {}
        self.in_flight = False
        self.attempts = 0
        self.failures = 0
        self.reconciled = 0
        for event in events:
            action = str(event.payload.get("action_id", ""))
            if event.event_type == "ENTRY_FILLED":
                self.fills[action] = (event.inventory_id, event.payload)
                if event.payload.get("economics_status") == "ECONOMICS_UNRESOLVED":
                    self.price_anomalies.add(action)
            elif event.event_type == "OPEN_FILL_ECONOMICS_RECONCILED":
                self.price_anomalies.discard(action)
            elif event.event_type == "OPEN_ECONOMICS_REVALIDATION_STARTED":
                self.retries[action] = (
                    int(event.payload["attempt_count"]),
                    datetime.fromisoformat(event.payload["next_attempt_at"]),
                )

    def due_checks(self, book, notional_anomalies, now):
        checks = []
        legs = {leg.position_id: leg for inv in book.inventories for leg in inv.broker_legs}
        for action in sorted(notional_anomalies | self.price_anomalies):
            retry = self.retries.get(action)
            if retry and now < retry[1]:
                continue
            fill = self.fills.get(action)
            if fill is None:
                continue  # Missing ledger attribution is never guessed.
            iid, payload = fill
            pid = str(payload["position_id"])
            checks.append(OpenEconomicsCheck(action, iid, pid,
                                            legs[pid].units if pid in legs else 0.0,
                                            payload))
        return tuple(checks)

    def reserve(self, check, now):
        count = self.retries.get(check.action_id, (0, now))[0] + 1
        delay = min(OPEN_ECONOMICS_RETRY_MAX_SECONDS,
                    OPEN_ECONOMICS_RETRY_SECONDS * 2 ** min(count - 1, 3))
        deadline = now + timedelta(seconds=delay)
        self.retries[check.action_id] = (count, deadline)
        return {"action_id": check.action_id, "position_id": check.position_id,
                "attempt_count": count, "next_attempt_at": deadline.isoformat()}

    def validate(self, check, evidence, *, units_close):
        if (not isinstance(evidence, BrokerPositionEconomics)
                or evidence.position_id != check.position_id
                or not evidence.source
                or not all(positive_number(v) for v in (
                    evidence.units, evidence.entry_price, evidence.account_notional))
                or not units_close(evidence.units, check.current_units)):
            return False
        expected = float(check.fill.get("requested_notional", check.fill["notional"]))
        expected *= check.current_units / float(check.fill["units"])
        if not 0.8 * expected <= evidence.account_notional <= 1.2 * expected:
            return False
        raw = check.fill.get("broker_response") or {}
        executions = raw.get("positionExecutions", [])
        matching = [execution for execution in executions
                    if isinstance(execution, dict)
                    and str(execution.get("positionId")) == check.position_id]
        # The lookup orderId and openingData.orderId are distinct documented
        # identities. P&L orderId belongs to the underlying opening position.
        opening = matching[0].get("openingData") if len(matching) == 1 else None
        order_id = opening.get("orderId") if isinstance(opening, dict) else None
        if order_id is not None and str(evidence.broker_response.get("orderId")) != str(order_id):
            return False
        if check.action_id in self.price_anomalies:
            return price_sanity(evidence.entry_price, check.fill.get("causal_quote") or {})
        return True

    def metrics(self):
        return {"open_economics_unresolved_action_ids": sorted(self.price_anomalies),
                "open_economics_revalidation_attempts": self.attempts,
                "open_economics_revalidation_failures": self.failures,
                "open_economics_reconciled": self.reconciled,
                "open_economics_revalidation_in_flight": self.in_flight}
