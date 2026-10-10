"""Strict broker-reported execution-cost evidence for journal/audit use.

Order fields are not a verified cash ledger and must not silently change the
book's realized PnL. Absence of fees is not proof of zero commission.
"""
from __future__ import annotations

import math
from typing import Any


def _amount(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def broker_order_cost_evidence(
    response: dict[str, Any] | None,
) -> dict[str, object]:
    """Return field-level evidence, never a fabricated broker fee.

    eToro order-info `totalCosts` is the total costs associated with the
    order in `orderCurrency`; it is not necessarily pure commission.
    Alpaca's optional `commission` is an order commission field (including
    Broker API end-user charges), not proof of total execution friction.
    """
    missing = {
        "status": "unavailable",
        "amount": None,
        "currency": None,
        "source": None,
        "scope": None,
        "ledger_applied": False,
    }
    if not isinstance(response, dict):
        return missing

    if "totalCosts" in response and "orderCurrency" in response:
        amount = _amount(response.get("totalCosts"))
        currency = response.get("orderCurrency")
        if amount is not None and isinstance(currency, str) and currency.strip():
            return {
                "status": "broker_order_reported",
                "amount": amount,
                "currency": currency.strip().upper(),
                "source": "etoro.order_info.totalCosts",
                "scope": "total_order_costs_not_commission_only",
                "ledger_applied": False,
            }
    if "commission" in response and "asset_class" in response:
        amount = _amount(response.get("commission"))
        if amount is not None:
            return {
                "status": "broker_order_reported",
                "amount": amount,
                "currency": "USD" if response.get("asset_class") == "us_equity" else None,
                "source": "alpaca.order.commission",
                "scope": "optional_end_user_commission_not_total_friction",
                "ledger_applied": False,
            }
    return missing
