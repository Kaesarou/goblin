"""Read-only DEMO schema check; never print full portfolio, orders, credentials or headers.

Run inside the observation-only container after startup:
    python scripts/inspect_etoro_payload_schema_readonly.py
This performs one portfolio GET and one P&L GET, no broker mutations.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.brokers.etoro.resilient_client import ResilientEtoroClient
from app.config.settings import Settings


def describe(payload: object) -> dict:
    result: dict[str, object] = {"root_type": type(payload).__name__}
    if not isinstance(payload, dict):
        return result
    result["root_keys"] = sorted(map(str, payload))[:80]
    for parent in ("", "data", "clientPortfolio"):
        node = payload if not parent else payload.get(parent)
        if not isinstance(node, dict):
            continue
        label = parent or "root"
        result[f"{label}_keys"] = sorted(map(str, node))[:80]
        for field in ("positions", "ordersForOpen", "orders", "ordersForClose"):
            value = node.get(field)
            if value is None:
                result[f"{label}_{field}_type"] = "absent_or_null"
            elif isinstance(value, list):
                result[f"{label}_{field}_count"] = len(value)
                if value and isinstance(value[0], dict):
                    result[f"{label}_{field}_sample_keys"] = sorted(map(str, value[0]))[:80]
            else:
                result[f"{label}_{field}_type"] = type(value).__name__
    return result


def inspect(client: ResilientEtoroClient) -> dict:
    if client.env != "demo":
        raise ValueError("Refusing to inspect non-DEMO account")
    portfolio = client.get_portfolio()
    pnl = client._get("/api/v1/trading/info/demo/pnl")
    return {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "broker_mutations": 0,
        "portfolio_schema": describe(portfolio),
        "pnl_schema": describe(pnl),
    }


def main() -> None:
    settings = Settings()
    if settings.broker != "etoro_demo":
        raise SystemExit("Read-only schema diagnostic requires BROKER=etoro_demo")
    client = ResilientEtoroClient(settings=settings)
    try:
        result = inspect(client)
    except Exception as exc:
        # Do not print response bodies or auth-bearing exception messages.
        print(json.dumps({"diagnostic": "failed", "error_type": type(exc).__name__,
                          "broker_mutations": 0}, sort_keys=True))
        raise SystemExit(2) from None
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
