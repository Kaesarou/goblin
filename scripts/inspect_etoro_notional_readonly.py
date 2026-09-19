"""Read-only DEMO diagnostic for the order investedAmountCurrency=1.0 anomaly.

Run from the repository with an authorized .env:
    python scripts/inspect_etoro_notional_readonly.py --order-id ORDER_ID

Only GET order details + P&L. Prints a minimal numeric projection rather than
raw account data or API headers; never sends an opening or closing order.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from app.brokers.etoro.pnl_position_amount import position_amount_usd
from app.brokers.etoro.resilient_client import ResilientEtoroClient
from app.config.settings import Settings


def inspect(client: ResilientEtoroClient, order_id: str) -> dict:
    if client.env != "demo":
        raise ValueError("This diagnostic is restricted to eToro DEMO")
    order = client.get_order_details(order_id)
    executions = order.get("positionExecutions")
    if not isinstance(executions, list) or not all(isinstance(x, dict) for x in executions):
        raise ValueError("Missing/invalid positionExecutions in order lookup")
    from app.brokers.etoro.order_response_parser import extract_position_id
    projection = []
    for execution in executions:
        pid = extract_position_id(execution)
        if pid is None:
            raise ValueError("Order execution without broker position ID")
        opening = execution.get("openingData")
        if not isinstance(opening, dict):
            opening = {}
        projection.append({
            "position_id": pid,
            "order_invested_amount_currency": execution.get("investedAmountCurrency"),
            "initial_exposure_account_currency": execution.get("initialExposureAccountCurrency"),
            "initial_exposure_asset_currency": execution.get("initialExposureAssetCurrency"),
            "margin_account_currency": execution.get("marginAccountCurrency"),
            "opening_avg_price": opening.get("avgPrice"),
            "opening_units": opening.get("units"),
        })
    # One account snapshot shared across all execution legs. The P&L positions
    # list may no longer contain a position that the operator closed manually.
    pnl = client._get("/api/v1/trading/info/demo/pnl")
    for item in projection:
        item["pnl_position_amount_usd"] = position_amount_usd(pnl, item["position_id"])
    return {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "order_id": str(order_id),
        "position_executions": projection,
        "account_currency": "USD",
        "broker_mutations": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--order-id", required=True)
    args = parser.parse_args()
    settings = Settings()
    if settings.broker != "etoro_demo" or settings.base_currency.upper() != "USD":
        raise SystemExit("Refusing non-DEMO/non-USD account diagnostic")
    client = ResilientEtoroClient(settings=settings)
    print(json.dumps(inspect(client, args.order_id), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
