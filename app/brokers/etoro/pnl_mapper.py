from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


@dataclass(frozen=True)
class EtoroAccountMetrics:
    """USD account metrics derived from the eToro P&L payload.

    eToro documents equity as available cash + total invested + unrealized P&L.
    ``credit`` alone is *not* account equity and must never be used as a sizing
    denominator.
    """

    equity: float
    available_cash: float
    total_invested: float
    unrealized_pnl: float
    credit: float


def extract_account_metrics(payload: dict[str, Any]) -> EtoroAccountMetrics:
    root = _account_root(payload)
    credit = _required_float(root, "credit")

    positions = _dict_items(root.get("positions"))
    mirrors = _dict_items(root.get("mirrors"))
    orders_for_open = _dict_items(root.get("ordersForOpen"))
    orders = _dict_items(root.get("orders"))

    manual_orders_for_open = [
        order for order in orders_for_open if _is_manual_order(order)
    ]
    pending_open_amount = sum(_optional_float(order.get("amount")) for order in manual_orders_for_open)
    pending_order_amount = sum(_optional_float(order.get("amount")) for order in orders)

    available_cash = credit - pending_open_amount - pending_order_amount

    direct_invested = sum(_optional_float(position.get("amount")) for position in positions)
    mirror_position_invested = sum(
        _optional_float(position.get("amount"))
        for mirror in mirrors
        for position in _dict_items(mirror.get("positions"))
    )
    mirror_available = sum(
        _optional_float(mirror.get("availableAmount"))
        - _optional_float(mirror.get("closedPositionsNetProfit"))
        for mirror in mirrors
    )
    pending_external_costs = sum(
        _optional_float(order.get("totalExternalCosts"))
        for order in manual_orders_for_open
    )
    total_invested = (
        direct_invested
        + mirror_position_invested
        + mirror_available
        + pending_open_amount
        + pending_order_amount
        + pending_external_costs
    )

    direct_unrealized = sum(_position_unrealized_pnl(position) for position in positions)
    mirror_unrealized = sum(
        _position_unrealized_pnl(position)
        for mirror in mirrors
        for position in _dict_items(mirror.get("positions"))
    )
    mirror_closed_profit = sum(
        _optional_float(mirror.get("closedPositionsNetProfit")) for mirror in mirrors
    )
    unrealized_pnl = direct_unrealized + mirror_unrealized + mirror_closed_profit

    equity = available_cash + total_invested + unrealized_pnl
    values = (credit, available_cash, total_invested, unrealized_pnl, equity)
    if not all(math.isfinite(value) for value in values):
        raise ValueError(f"Invalid non-finite eToro account metrics: {values}")
    if equity <= 0:
        raise ValueError(f"Invalid eToro account equity={equity}")

    return EtoroAccountMetrics(
        equity=equity,
        available_cash=available_cash,
        total_invested=total_invested,
        unrealized_pnl=unrealized_pnl,
        credit=credit,
    )


def extract_account_equity(payload: dict[str, Any]) -> float:
    return extract_account_metrics(payload).equity


def _account_root(payload: dict[str, Any]) -> dict[str, Any]:
    current: dict[str, Any] = payload
    for _ in range(4):
        if "credit" in current:
            return current
        nested = current.get("data")
        if not isinstance(nested, dict):
            break
        current = nested
    raise ValueError("Unable to locate eToro P&L account payload with credit field")


def _position_unrealized_pnl(position: dict[str, Any]) -> float:
    value = position.get("unrealizedPnL")
    if not isinstance(value, dict):
        return 0.0
    for key in ("pnL", "pnl", "PnL", "PNL"):
        if key in value:
            return _optional_float(value.get(key))
    return 0.0


def _is_manual_order(order: dict[str, Any]) -> bool:
    mirror_id = order.get("mirrorID", order.get("mirrorId"))
    return mirror_id in (None, 0, "0", "")


def _dict_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _required_float(payload: dict[str, Any], key: str) -> float:
    if key not in payload:
        raise ValueError(f"Missing required eToro P&L field: {key}")
    return _coerce_float(payload[key], key=key)


def _optional_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    return _coerce_float(value, key="optional")


def _coerce_float(value: Any, *, key: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"Invalid boolean numeric value for {key}: {value}")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid numeric eToro field {key}={value!r}") from exc
    if not math.isfinite(result):
        raise ValueError(f"Non-finite numeric eToro field {key}={value!r}")
    return result
