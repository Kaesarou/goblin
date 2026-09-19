"""Wait for manual DEMO closes to settle before handing off to normal V3.

This script is the child of app.runtime.restart_guard. The final os.execv
replaces this child with app.main, retaining its PID and Docker stop forwarding.
Only account GETs are issued while untracked positions are still present.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from app.brokers.etoro.pending_orders_preflight import pending_open_order_descriptions
from app.brokers.etoro.portfolio_position_parser import extract_open_position_units
from app.brokers.etoro.resilient_client import ResilientEtoroClient
from app.config.settings import Settings
from app.v3.external_account_gate import gate_active, gate_path
from app.v3.persistence import InventoryEventStore

CHECK_INTERVAL_SECONDS = 180
REQUIRED_CONSECUTIVE_FLAT_CHECKS = 2


def start_normal_runtime() -> int:
    """Replace the restart-guard child; do not create an untracked grandchild."""
    os.execv(sys.executable, [sys.executable, "-m", "app.main"])
    raise RuntimeError("execv unexpectedly returned")


def broker_account_flat(client: ResilientEtoroClient) -> bool:
    """Require authoritative zero open units AND zero pending opening orders."""
    positions = extract_open_position_units(client.get_portfolio())
    # A queued/accepted close is not proof of execution.
    pending_opens = pending_open_order_descriptions(client)
    return not any(units is None or units > 0 for units in positions.values()) and not pending_opens


def archive_external_gate(sqlite_path: str | Path) -> Path | None:
    """Archive the incident marker in the persistent volume, never SQLite/logs."""
    path = gate_path(sqlite_path)
    if not path.exists():
        return None
    gate_active(sqlite_path)  # A malformed marker must fail closed.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archive = path.with_name(f"{path.stem}.acknowledged.{stamp}.{os.getpid()}.json")
    if archive.exists():
        raise FileExistsError(f"Recovery acknowledgment archive already exists: {archive}")
    path.rename(archive)
    directory_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return archive


def main() -> int:
    if (os.environ.get("GOBLIN_OBSERVATION_ONLY") != "0"
            or os.environ.get("GOBLIN_DEMO_AUTO_REARM_AFTER_MANUAL_CLOSE") != "1"):
        print("CRITICAL: DEMO close-wait release configuration missing; staying stopped",
              file=sys.stderr, flush=True)
        return 0
    try:
        settings = Settings()
        if settings.broker != "etoro_demo" or settings.base_currency.upper() != "USD":
            raise ValueError("Only the USD eToro DEMO account is supported")
        sqlite_path = Path(settings.position_store_path)
        store = InventoryEventStore(sqlite_path)
        has_events = bool(store.events())
        marker_active = gate_active(sqlite_path)
        if has_events:
            if marker_active:
                raise ValueError("Nonempty V3 ledger with unresolved external broker gate")
            print("DEMO_REARM: existing V3 ledger; normal startup will reconcile it",
                  flush=True)
            return start_normal_runtime()
        client = ResilientEtoroClient(settings=settings)
    except Exception as exc:
        print(f"CRITICAL: DEMO close-wait preflight invalid: {type(exc).__name__}; "
              "operator review required", file=sys.stderr, flush=True)
        return 0

    confirmations = 0
    print("DEMO_REARM_WAIT: fresh ledger; awaiting two broker-flat GET preflights; "
          "no BUY or automatic CLOSE", flush=True)
    while True:
        try:
            flat = broker_account_flat(client)
        except Exception as exc:
            confirmations = 0
            print(f"DEMO_REARM_WAIT: broker preflight unavailable ({type(exc).__name__}); "
                  "BUY remains disabled", flush=True)
        else:
            confirmations = confirmations + 1 if flat else 0
            if not flat:
                print("DEMO_REARM_WAIT: open broker positions or pending OPEN orders; "
                      "BUY remains disabled", flush=True)
            elif confirmations < REQUIRED_CONSECUTIVE_FLAT_CHECKS:
                print("DEMO_REARM_WAIT: first flat snapshot; waiting for independent confirmation",
                      flush=True)
            else:
                # V3 re-checks the broker during its own startup and records a
                # new durable gate if old positions reappear.
                try:
                    archived = archive_external_gate(sqlite_path)
                except Exception as exc:
                    print(f"CRITICAL: DEMO close-wait acknowledgment failed: "
                          f"{type(exc).__name__}; BUY remains disabled",
                          file=sys.stderr, flush=True)
                    return 0
                print("DEMO_REARM_READY: two flat broker checks; "
                      f"incident marker archived={archived is not None}; "
                      "starting normal V3 with its broker preflight", flush=True)
                return start_normal_runtime()
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
