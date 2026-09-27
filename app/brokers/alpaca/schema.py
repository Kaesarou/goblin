from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from math import isfinite

TERMINAL_STATUSES = frozenset({"filled", "canceled", "expired", "rejected"})
OPEN_STATUSES = frozenset(
    {
        "new",
        "accepted",
        "pending_new",
        "partially_filled",
        "pending_cancel",
        "pending_replace",
        "accepted_for_bidding",
        "stopped",
        "suspended",
        "done_for_day",
        "calculated",
    }
)


def number(value, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("Missing or invalid Alpaca numeric field")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Invalid Alpaca numeric field") from exc
    if (
        not result.is_finite()
        or not isfinite(float(result))
        or result < 0
        or (positive and result == 0)
    ):
        raise ValueError("Invalid Alpaca numeric range")
    return result


def timestamp(value) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Missing Alpaca timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Alpaca timestamp must include a timezone")
    return result.astimezone(UTC)


def text(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Missing Alpaca identity field")
    return value


def order(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Invalid Alpaca order")
    for key in ("id", "client_order_id", "symbol"):
        text(payload.get(key))
    if payload.get("side") not in {"buy", "sell"}:
        raise ValueError("Invalid Alpaca order side")
    if payload.get("status") not in TERMINAL_STATUSES | OPEN_STATUSES:
        raise ValueError("Unsupported Alpaca order status")
    qty = number(payload.get("filled_qty"))
    timestamp(payload.get("updated_at"))
    if qty:
        number(payload.get("filled_avg_price"), positive=True)
    if payload["status"] == "filled" and not qty:
        raise ValueError("Filled Alpaca order has no execution quantity")
    return payload
