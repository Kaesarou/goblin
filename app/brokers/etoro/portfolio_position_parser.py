from __future__ import annotations

import math
from collections.abc import Iterable

from app.brokers.etoro.payload_collections import keep_dict_items


def extract_open_positions(payload: dict) -> list[dict]:
    client_portfolio = payload.get('clientPortfolio')
    if isinstance(client_portfolio, dict):
        positions = client_portfolio.get('positions')
        if isinstance(positions, list):
            return keep_dict_items(positions)

    return []


def extract_position_id(payload: dict) -> str | None:
    position_id = payload.get('positionID')
    return None if position_id is None else str(position_id)


def extract_position_units(payload: dict) -> float | None:
    value = payload.get('units')
    if isinstance(value, bool):
        raise ValueError('Invalid boolean broker units')
    if value is None:
        return None
    units = float(value)
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
        return is_open is not False

    return False


def _quantitative_positions(payload: dict) -> list[dict]:
    client_portfolio = payload.get('clientPortfolio')
    if not isinstance(client_portfolio, dict):
        raise ValueError('Missing authoritative broker positions collection')
    positions = client_portfolio.get('positions')
    if not isinstance(positions, list) or not all(isinstance(p, dict) for p in positions):
        raise ValueError('Invalid broker positions collection')
    return positions
