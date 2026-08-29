from __future__ import annotations

from typing import Any

from app.brokers.etoro.pnl_mapper import (
    extract_account_equity as _extract_account_equity_from_pnl,
)


def extract_account_equity(payload: dict[str, Any]) -> float:
    """Return account equity from an eToro P&L payload only.

    Historical Goblin code accepted fields such as ``credit``, ``cash`` and
    ``availableBalance`` as if they were equity. Those values are liquidity
    components, not total account value. The P&L contract is now the sole source
    of account equity and follows eToro's documented calculation formula.
    """

    return _extract_account_equity_from_pnl(payload)


def extract_optional_account_equity(payload: dict[str, Any]) -> float | None:
    try:
        return extract_account_equity(payload)
    except (KeyError, TypeError, ValueError):
        return None
