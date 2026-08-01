from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.brokers.base import BrokerCloseExecution


def extract_close_execution(
    payload: dict[str, Any],
    *,
    close_order_id: str,
    position_id: str,
) -> BrokerCloseExecution | None:
    positions = payload.get("positions")
    if not isinstance(positions, list):
        return None
    for item in positions:
        if not isinstance(item, dict):
            continue
        if str(item.get("positionID")) != str(position_id):
            continue
        rate = _optional_float(item.get("rate"))
        if rate is None or rate <= 0:
            return None
        return BrokerCloseExecution(
            position_id=str(position_id),
            close_order_id=str(close_order_id),
            executed_exit_price=rate,
            executed_at=_optional_datetime(item.get("occurred")),
            units=_optional_float(item.get("units")),
            conversion_rate=_optional_float(item.get("conversionRate")),
            amount=_optional_float(item.get("amount")),
            broker_response=payload,
        )
    return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
