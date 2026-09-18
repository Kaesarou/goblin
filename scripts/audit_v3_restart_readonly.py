"""Audit an OFFLINE COPY of the V3 SQLite ledger without modifying it.

Example: python scripts/audit_v3_restart_readonly.py /path/to/copied/goblin.sqlite
This tool never contacts eToro, schedules orders, or attempts remediation.
Its results are NOT a broker reconciliation or permission to restart production.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from app.v3.book import InventoryBook
from app.v3.persistence import InventoryEvent
from app.v3.recovery import evaluate_restart_safety


def _fingerprints(db: Path) -> dict[str, str]:
    result = {}
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{db}{suffix}")
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        result[path.name] = digest.hexdigest()
    return result


def _event_rows_on_disposable_copy(db: Path) -> list[tuple]:
    # SQLite may checkpoint or remove its WAL/SHM files even on connection close.
    # Never let SQLite open the SOURCE, including an offline operator backup.
    # Copy the complete stopped database and its WAL/SHM siblings first.
    with tempfile.TemporaryDirectory(prefix="goblin-v3-audit-") as scratch:
        shadow = Path(scratch) / db.name
        for suffix in ("", "-wal", "-shm"):
            source = Path(f"{db}{suffix}")
            if source.is_file():
                shutil.copy2(source, Path(f"{shadow}{suffix}"))
        uri = f"file:{quote(str(shadow.resolve()), safe='/')}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            conn.execute("PRAGMA query_only = ON")
            rows = conn.execute(
                "SELECT event_id, inventory_id, event_type, occurred_at, payload_json, "
                "strategy_version, model_version FROM inventory_events "
                "ORDER BY occurred_at, rowid"
            ).fetchall()
        return rows


def audit(db: Path) -> dict:
    if not db.is_file():
        raise FileNotFoundError(db)
    before = _fingerprints(db)
    rows = _event_rows_on_disposable_copy(db)

    events = [
        InventoryEvent(
            event_id=row[0], inventory_id=row[1], event_type=row[2],
            occurred_at=datetime.fromisoformat(row[3]), payload=json.loads(row[4]),
            strategy_version=row[5], model_version=row[6],
        )
        for row in rows
    ]
    book = InventoryBook.from_events(events)
    safety = evaluate_restart_safety(events)
    active = [
        {
            "symbol": inventory.symbol,
            "inventory_id": inventory.inventory_id,
            "position_ids": [leg.position_id for leg in inventory.broker_legs],
            "units_by_position": {
                leg.position_id: leg.units for leg in inventory.broker_legs
            },
            "total_units": inventory.total_units,
            "entry_fill_count": inventory.entry_fill_count,
        }
        for inventory in book.inventories if inventory.total_units > 0
    ]
    result = {
        "database": str(db),
        "events": len(events),
        "event_types": dict(sorted(Counter(event.event_type for event in events).items())),
        "first_event_utc": events[0].occurred_at.isoformat() if events else None,
        "last_event_utc": events[-1].occurred_at.isoformat() if events else None,
        "active_inventories": sorted(active, key=lambda row: (row["symbol"], row["inventory_id"])),
        "historical_aliases": dict(book.legacy_inventory_aliases),
        "restart_safety": {
            "safe_from_event_history_only": safety.safe,
            "reason": safety.reason,
            "unresolved_action_ids": list(safety.unresolved_action_ids),
        },
        "broker_reconciliation_performed": False,
        "source_sha256": before,
    }
    after = _fingerprints(db)
    if before != after:
        raise RuntimeError("SQLite source fingerprint changed during read-only audit")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sqlite_copy", type=Path, help="Path to an offline COPY, not live VPS database")
    args = parser.parse_args()
    report = audit(args.sqlite_copy)
    print(json.dumps(report, sort_keys=True, indent=2))
    if not report["restart_safety"]["safe_from_event_history_only"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
