from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.brokers.base import (
    BrokerClient,
    BrokerCloseExecution,
    OpenPositionResult,
)
from app.brokers.etoro.order_confirmation_error import (
    EtoroOrderConfirmationUnknownError,
)
from app.brokers.etoro.order_response_parser import (
    extract_executed_position_details_list,
    is_order_rejected,
)
from app.brokers.etoro.portfolio_position_parser import (
    contains_open_position,
    extract_open_positions,
    extract_position_id,
)


@dataclass(frozen=True)
class UnknownOrderLookup:
    order_id: str | None
    reference_id: str | None
    symbol: str
    side: str
    amount: float
    submitted_at: datetime
    known_position_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class UnknownOrderResolution:
    status: str
    result: OpenPositionResult | None = None
    matched_by: str | None = None
    details: dict[str, Any] | None = None


@dataclass(frozen=True)
class PositionReconciliationResult:
    open_states: dict[str, bool]
    close_executions: dict[str, BrokerCloseExecution]
    close_execution_unavailable: dict[str, str]


def unwrap_broker(broker: BrokerClient) -> BrokerClient:
    current = broker
    seen: set[int] = set()
    while hasattr(current, 'delegate') and id(current) not in seen:
        seen.add(id(current))
        delegate = getattr(current, 'delegate')
        if not isinstance(delegate, BrokerClient):
            break
        current = delegate
    return current


def get_fresh_position_open_states(
    broker: BrokerClient,
    position_ids: list[str],
) -> dict[str, bool]:
    """Resolve all requested positions from one fresh portfolio when possible."""
    if not position_ids:
        return {}
    raw = unwrap_broker(broker)
    get_portfolio = getattr(raw, 'get_portfolio', None)
    if callable(get_portfolio):
        portfolio = get_portfolio()
        return {
            position_id: contains_open_position(portfolio, position_id)
            for position_id in position_ids
        }
    batch = getattr(raw, 'get_position_open_states', None)
    if callable(batch):
        states = batch(position_ids)
        return {str(key): bool(value) for key, value in states.items()}
    return {
        position_id: broker.is_position_open(position_id)
        for position_id in position_ids
    }


def reconcile_positions_and_close_executions(
    broker: BrokerClient,
    position_ids: list[str],
    pending_close_order_ids: dict[str, str],
) -> PositionReconciliationResult:
    open_states = get_fresh_position_open_states(broker, position_ids)
    raw = unwrap_broker(broker)
    close_executions: dict[str, BrokerCloseExecution] = {}
    unavailable: dict[str, str] = {}
    for position_id, close_order_id in pending_close_order_ids.items():
        if open_states.get(position_id) is not False:
            continue
        try:
            execution = raw.get_close_execution(
                close_order_id,
                position_id,
            )
        except Exception as exc:  # portfolio absence still confirms the close
            unavailable[position_id] = f'lookup_failed:{exc}'
            continue
        if execution is not None:
            close_executions[position_id] = execution
        else:
            unavailable[position_id] = 'broker_fill_not_returned'
    return PositionReconciliationResult(
        open_states=open_states,
        close_executions=close_executions,
        close_execution_unavailable=unavailable,
    )


def resolve_unknown_open_order(
    broker: BrokerClient,
    lookup: UnknownOrderLookup,
) -> UnknownOrderResolution:
    raw = unwrap_broker(broker)
    if lookup.order_id:
        get_order_details = getattr(raw, 'get_order_details', None)
        if callable(get_order_details):
            try:
                details = get_order_details(lookup.order_id)
            except Exception:  # portfolio reconciliation remains the safe fallback
                details = None
            if isinstance(details, dict):
                if is_order_rejected(details):
                    return UnknownOrderResolution(
                        status='rejected',
                        matched_by='order_lookup',
                        details=details,
                    )
                executed = extract_executed_position_details_list(details)
                if len(executed) == 1:
                    item = executed[0]
                    return UnknownOrderResolution(
                        status='confirmed',
                        result=OpenPositionResult(
                            position_id=item.position_id,
                            executed_entry_price=item.executed_entry_price,
                        ),
                        matched_by='order_lookup',
                        details=details,
                    )

    get_portfolio = getattr(raw, 'get_portfolio', None)
    if not callable(get_portfolio):
        return UnknownOrderResolution(status='unresolved')
    portfolio = get_portfolio()
    matches = _matching_portfolio_positions(raw, portfolio, lookup)
    if len(matches) != 1:
        return UnknownOrderResolution(
            status='unresolved',
            matched_by='portfolio',
            details={'match_count': len(matches)},
        )
    position = matches[0]
    position_id = extract_position_id(position)
    if position_id is None:
        return UnknownOrderResolution(status='unresolved')
    return UnknownOrderResolution(
        status='confirmed',
        result=OpenPositionResult(
            position_id=str(position_id),
            executed_entry_price=_optional_float(
                position,
                ('openRate', 'OpenRate', 'entryRate', 'entryPrice', 'rate'),
            ),
        ),
        matched_by='portfolio',
        details=position,
    )


def order_id_from_confirmation_error(exc: Exception) -> str | None:
    if isinstance(exc, EtoroOrderConfirmationUnknownError):
        return exc.order_id
    value = getattr(exc, 'order_id', None)
    if value:
        return str(value)
    message = str(exc)
    marker = 'order_id='
    if marker not in message:
        return None
    candidate = message.split(marker, maxsplit=1)[1]
    return candidate.split(',', maxsplit=1)[0].split(' ', maxsplit=1)[0].strip()


def reference_id_from_confirmation_error(exc: Exception) -> str | None:
    if isinstance(exc, EtoroOrderConfirmationUnknownError):
        return exc.reference_id
    value = getattr(exc, 'reference_id', None)
    return str(value) if value else None


def submitted_at_from_confirmation_error(
    exc: Exception,
    fallback: datetime,
) -> datetime:
    value = getattr(exc, 'submitted_at', None)
    return _as_utc(value) if isinstance(value, datetime) else _as_utc(fallback)


def is_confirmation_unknown_error(exc: Exception) -> bool:
    if isinstance(exc, EtoroOrderConfirmationUnknownError):
        return True
    message = str(exc).lower()
    return (
        'order was not executed with required details after polling' in message
        or 'order category not found' in message
        or 'order confirmation unknown' in message
    ) and order_id_from_confirmation_error(exc) is not None


def _matching_portfolio_positions(
    raw_broker: BrokerClient,
    portfolio: dict,
    lookup: UnknownOrderLookup,
) -> list[dict]:
    known_ids = set(lookup.known_position_ids)
    positions = [
        item
        for item in extract_open_positions(portfolio)
        if str(extract_position_id(item)) not in known_ids
    ]
    instrument_id = None
    resolver = getattr(raw_broker, '_find_instrument_id', None)
    if callable(resolver):
        try:
            instrument_id = int(resolver(lookup.symbol))
        except Exception:
            instrument_id = None

    earliest_plausible_open = _as_utc(lookup.submitted_at) - timedelta(minutes=2)
    result: list[dict] = []
    for position in positions:
        if instrument_id is not None:
            candidate_instrument = _optional_int(
                position,
                ('instrumentID', 'instrumentId', 'InstrumentID', 'InstrumentId'),
            )
            if candidate_instrument is not None and candidate_instrument != instrument_id:
                continue
        candidate_side = _position_side(position)
        if candidate_side is not None and candidate_side != lookup.side.upper():
            continue
        candidate_amount = _optional_float(
            position,
            ('amount', 'Amount', 'invested', 'Invested', 'investment'),
        )
        if (
            candidate_amount is not None
            and lookup.amount > 0
            and abs(candidate_amount - lookup.amount) / lookup.amount > 0.10
        ):
            continue
        opened_at = _optional_datetime(
            position,
            ('openDateTime', 'openedAt', 'openTime', 'createdAt'),
        )
        if opened_at is not None and opened_at < earliest_plausible_open:
            continue
        result.append(position)
    return result


def _position_side(payload: dict) -> str | None:
    is_buy = payload.get('isBuy')
    if isinstance(is_buy, bool):
        return 'BUY' if is_buy else 'SELL'
    for key in ('side', 'Side', 'direction', 'transaction'):
        value = payload.get(key)
        if value is None:
            continue
        normalized = str(value).strip().upper()
        if normalized in {'BUY', 'SELL'}:
            return normalized
        if normalized in {'OPENBUY', 'BUYOPEN'}:
            return 'BUY'
        if normalized in {'OPENSELL', 'SELLSHORT'}:
            return 'SELL'
    return None


def _optional_float(payload: dict, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _optional_int(payload: dict, keys: tuple[str, ...]) -> int | None:
    value = _optional_float(payload, keys)
    return int(value) if value is not None else None


def _optional_datetime(payload: dict, keys: tuple[str, ...]) -> datetime | None:
    for key in keys:
        value = payload.get(key)
        if not value:
            continue
        if isinstance(value, datetime):
            return _as_utc(value)
        try:
            return _as_utc(
                datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            )
        except ValueError:
            continue
    return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
