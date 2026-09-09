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

    valid: list[dict[str, Any]] = []
    exact: list[dict[str, Any]] = []
    for item in positions:
        if not isinstance(item, dict):
            continue
        rate = _optional_float(item.get("rate"))
        if rate is None or rate <= 0:
            continue
        valid.append(item)
        if str(item.get("positionID")) == str(position_id):
            exact.append(item)

    # Historical full-close payloads may echo the original open position id. For
    # eToro native partial closes observed prospectively, however, the close-order
    # lookup can return a new execution/fill position id. In that case the order id
    # is the action identity and a single valid fill is unambiguous. Multiple
    # unmatched fills remain fail-closed rather than guessing attribution.
    if len(exact) == 1:
        item = exact[0]
    elif not exact and len(valid) == 1:
        item = valid[0]
    else:
        return None

    rate = _optional_float(item.get("rate"))
    assert rate is not None and rate > 0
    broker_execution_position_id = item.get("positionID")
    return BrokerCloseExecution(
        position_id=str(position_id),
        close_order_id=str(close_order_id),
        executed_exit_price=rate,
        executed_at=_optional_datetime(item.get("occurred")),
        units=_optional_float(item.get("units")),
        conversion_rate=_optional_float(item.get("conversionRate")),
        amount=_optional_float(item.get("amount")),
        broker_response=payload,
        broker_execution_position_id=(
            None
            if broker_execution_position_id is None
            else str(broker_execution_position_id)
        ),
    )


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
