"""Audit an unattributed broker reduction; --apply explicitly abandons its economics.

Stop the runtime before applying. This command only reads the broker portfolio.
"""
from __future__ import annotations
import argparse
import json
from app.brokers.etoro.etoro_client import EtoroClient
from app.config.settings import Settings
from app.v3.operator_reconciliation import acknowledge_broker_reconciliation
from app.v3.persistence import InventoryEventStore
from app.v3.state_store import V3RuntimeStateStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--position-id", required=True)
    parser.add_argument("--broker-units", required=True, type=float)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--state-path")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    if settings.broker not in {"etoro_demo", "etoro_live"}:
        parser.error("An eToro demo/live broker configuration is required")
    path = args.state_path or settings.position_store_path
    result = acknowledge_broker_reconciliation(
        event_store=InventoryEventStore(path), state_store=V3RuntimeStateStore(path),
        broker=EtoroClient(settings), position_id=args.position_id,
        expected_broker_units=args.broker_units, reason=args.reason, apply=args.apply,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
