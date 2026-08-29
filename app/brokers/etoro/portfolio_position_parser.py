from __future__ import annotations

from collections.abc import Iterable

from app.brokers.etoro.payload_collections import keep_dict_items
from app.brokers.etoro.scalar_extractors import extract_optional_float
from app.brokers.etoro.string_extractors import extract_optional_string


POSITION_ID_KEYS = ('positionID', 'positionId', 'PositionID', 'PositionId')
POSITION_UNITS_KEYS = (
    'units',
    'Units',
    'netUnits',
    'NetUnits',
    'unitAmount',
    'UnitAmount',
)


def extract_open_positions(payload: dict) -> list[dict]:
    client_portfolio = payload.get('clientPortfolio')
    if isinstance(client_portfolio, dict):
        positions = client_portfolio.get('positions')
        if isinstance(positions, list):
            return keep_dict_items(positions)

    positions = payload.get('positions')
    if isinstance(positions, list):
        return keep_dict_items(positions)

    data = payload.get('data')
    if isinstance(data, dict):
        return extract_open_positions(data)

    return []


def extract_position_id(payload: dict) -> str | None:
    return extract_optional_string(payload, POSITION_ID_KEYS)


def extract_position_units(payload: dict) -> float | None:
    units = extract_optional_float(payload, POSITION_UNITS_KEYS)
    if units is None:
        return None
    return max(0.0, float(units))


def extract_open_position_units(
    payload: dict,
    position_ids: Iterable[str] | None = None,
) -> dict[str, float | None]:
    requested = (
        None
        if position_ids is None
        else {str(position_id) for position_id in position_ids}
    )
    result: dict[str, float | None] = {}
    for position in extract_open_positions(payload):
        position_id = extract_position_id(position)
        if position_id is None:
            continue
        normalized = str(position_id)
        if requested is not None and normalized not in requested:
            continue
        if position.get('isOpen') is False:
            result[normalized] = 0.0
            continue
        result[normalized] = extract_position_units(position)

    if requested is not None:
        for position_id in requested:
            result.setdefault(position_id, 0.0)
    return result


def contains_open_position(payload: dict, position_id: str) -> bool:
    open_positions = extract_open_positions(payload)

    for position in open_positions:
        candidate_position_id = extract_position_id(position)

        if str(candidate_position_id) != str(position_id):
            continue

        is_open = position.get('isOpen')
        if is_open is False:
            return False

        return True

    return False
