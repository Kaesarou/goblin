from __future__ import annotations

import math
from typing import Any

ACCOUNT_EQUITY_SOURCE = "aggregate-portfolio.accountTotals.accountTotalValue"


def extract_account_equity(payload: dict[str, Any]) -> float:
    """Read the documented total; liquidity and component sums are not equity proof."""
    current = payload
    for _ in range(4):
        if not isinstance(current, dict):
            break
        if "accountTotals" in current:
            totals = current["accountTotals"]
            value = totals.get("accountTotalValue") if isinstance(totals, dict) else None
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("Invalid numeric accountTotals.accountTotalValue")
            equity = float(value)
            if not math.isfinite(equity) or equity <= 0:
                raise ValueError("accountTotals.accountTotalValue must be finite and positive")
            return equity
        current = current.get("data")
    raise ValueError("Missing accountTotals.accountTotalValue")


def extract_optional_account_equity(payload: dict[str, Any]) -> float | None:
    try:
        return extract_account_equity(payload)
    except (KeyError, TypeError, ValueError):
        return None
