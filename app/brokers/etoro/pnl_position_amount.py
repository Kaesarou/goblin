"""Read an exact broker position's invested principal from eToro P&L.

The documented P&L position ``amount`` is allocated USD capital. Do not use
units * instrument price as account-currency notional: European assets require
FX and the order lookup's investedAmountCurrency has returned 1.0 in DEMO.
"""

from __future__ import annotations

import math

from app.brokers.etoro.portfolio_position_parser import extract_position_id


def position_amount_usd(payload: dict, position_id: str) -> float | None:
    """Return one proven P&L position amount, None if the position is absent.

    Unknown schema, duplicate identities, missing or invalid amounts raise so
    callers cannot mistake a malformed broker response for zero exposure.
    ``isOpen=False`` still needs no amount, but cannot attest that a pending
    manual close has settled unless the whole portfolio is reconciled as well.
    """
    if not isinstance(payload, dict):
        raise ValueError("Invalid eToro P&L payload")
    client_portfolio = payload.get("clientPortfolio")
    if not isinstance(client_portfolio, dict):
        raise ValueError("Missing authoritative eToro P&L positions")
    positions = client_portfolio.get("positions")
    if not isinstance(positions, list) or not all(isinstance(p, dict) for p in positions):
        raise ValueError("Invalid eToro P&L positions collection")
    matches = [
        p for p in positions
        if extract_position_id(p) == str(position_id)
    ]
    if len(matches) > 1:
        raise ValueError(f"Duplicate eToro P&L position identity: {position_id}")
    if not matches:
        return None
    position = matches[0]
    if position.get("isOpen") is False:
        return None
    amount = position.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (float, int)):
        raise ValueError("Missing or nonnumeric eToro P&L position amount")
    amount = float(amount)
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError("Invalid eToro P&L position amount")
    return amount
