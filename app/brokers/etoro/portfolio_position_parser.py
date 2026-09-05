from __future__ import annotations

from collections.abc import Iterable
import math

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
    if any(isinstance(payload.get(key), bool) for key in POSITION_UNITS_KEYS):
        raise ValueError('Invalid boolean broker units')
    units = extract_optional_float(payload, POSITION_UNITS_KEYS)
    if units is None:
        return None
    if not math.isfinite(units) or units < 0:
        raise ValueError('Invalid broker units')
    return float(units)


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
    for position in _quantitative_positions(payload):
        position_id = extract_position_id(position)
        if position_id is None:
            raise ValueError('Missing broker position identity')
        normalized = str(position_id)
        if normalized in result:
            raise ValueError('Duplicate broker position identity')
        if requested is not None and normalized not in requested:
            continue
        if position.get('isOpen') is False:
            result[normalized] = 0.0
            continue
        units = extract_position_units(position)
        if units is None:
            # This parser is the eToro quantitative reconciliation boundary. An
            # open position whose units cannot be proven must never be treated as
            # a successful equality check; let startup/periodic reconciliation
            # fail closed instead of silently degrading to existence-only mode.
            raise ValueError(
                'Unable to extract units for open eToro position '
                f'{normalized}'
            )
        result[normalized] = units

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


def _quantitative_positions(payload: dict) -> list[dict]:
    current = payload
    for _ in range(4):
        if not isinstance(current, dict):
            break
        root = current.get('clientPortfolio', current)
        if isinstance(root, dict) and 'positions' in root:
            positions = root['positions']
            if not isinstance(positions, list) or not all(isinstance(p, dict) for p in positions):
                raise ValueError('Invalid broker positions collection')
            return positions
        current = current.get('data')
    raise ValueError('Missing authoritative broker positions collection')
