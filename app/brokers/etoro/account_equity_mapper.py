from __future__ import annotations

import math
from typing import Any

ACCOUNT_EQUITY_SOURCE = "aggregate-portfolio.accountTotals.accountTotalValue"


def extract_account_equity(payload: dict[str, Any]) -> float:
    """Read the documented total; liquidity and component sums are not equity proof."""
    if not isinstance(payload, dict):
        raise ValueError("Missing accountTotals.accountTotalValue")
    totals = payload.get("accountTotals")
    value = totals.get("accountTotalValue") if isinstance(totals, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        if totals is None:
            raise ValueError("Missing accountTotals.accountTotalValue")
        raise ValueError("Invalid numeric accountTotals.accountTotalValue")
    equity = float(value)
    if not math.isfinite(equity) or equity <= 0:
        raise ValueError("accountTotals.accountTotalValue must be finite and positive")
    return equity
