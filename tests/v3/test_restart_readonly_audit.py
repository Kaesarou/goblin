"""The offline audit must not mutate the week's research ledger."""

import importlib.util
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.v3.persistence import InventoryEvent, InventoryEventStore

NOW = datetime(2026, 9, 15, 13, 40, tzinfo=timezone.utc)
SCRIPT = Path(__file__).resolve().parents[2] / "scripts/audit_v3_restart_readonly.py"


def _audit_module():
    spec = importlib.util.spec_from_file_location("v3_restart_audit", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _fill(name, position, seconds, units):
    return InventoryEvent(
        event_id=f"{name}:fill", inventory_id=f"INTC:{name}",
        event_type="ENTRY_FILLED", occurred_at=NOW + timedelta(seconds=seconds),
        payload={"symbol": "INTC", "position_id": position,
                 "units": units, "price": 99.27,
                 "notional": units * 99.27, "fee": 0.0,
                 "action_id": name},
        strategy_version="INVENTORY_RR5_ETORO5_V1",
    )


def test_offline_audit_preserves_source_bytes_and_both_intc_legs(tmp_path):
    db = tmp_path / "copy.sqlite"
    store = InventoryEventStore(db)
    for e in (
        _fill("first", "3599774868", 4, 3.292115),
        _fill("second", "3599774883", 7, 3.291612),
    ):
        assert store.append(e)
    module = _audit_module()
    before = module._fingerprints(db)
    result = module.audit(db)
    assert module._fingerprints(db) == before
    assert result["source_sha256"] == before
    assert result["broker_reconciliation_performed"] is False
    assert result["historical_aliases"] == {"INTC:second": "INTC:first"}
    assert len(result["active_inventories"]) == 1
    intc = result["active_inventories"][0]
    assert intc["total_units"] == pytest.approx(6.583727)
    assert intc["units_by_position"] == {
        "3599774868": 3.292115, "3599774883": 3.291612,
    }
    assert result["restart_safety"]["safe_from_event_history_only"] is True
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM inventory_events").fetchone()[0] == 2


def test_offline_audit_exposes_unresolved_open_as_unsafe(tmp_path):
    db = tmp_path / "copy.sqlite"
    store = InventoryEventStore(db)
    store.append(InventoryEvent(
        event_id="pending", inventory_id="INTC:pending",
        event_type="ORDER_SUBMISSION_STARTED", occurred_at=NOW,
        payload={"action_id": "pending", "symbol": "INTC"},
        strategy_version="INVENTORY_RR5_ETORO5_V1",
    ))
    result = _audit_module().audit(db)
    assert result["restart_safety"]["safe_from_event_history_only"] is False
    assert result["restart_safety"]["unresolved_action_ids"] == ["pending"]
