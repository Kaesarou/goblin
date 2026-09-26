"""Durable operator acknowledgment gate for external DEMO broker activity.

A queued manual close can leave an open position visible, and the documented P&L
open-order arrays cannot prove that *close* requests have settled. The gate
therefore persists outside the replaceable SQLite and is never auto-cleared.

The deployment can additionally force observation independently of broker state:
GOBLIN_OBSERVATION_ONLY=1 cannot be acknowledged by deleting the marker.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timezone
from pathlib import Path

VERSION = 1
GATE_FILENAME = "v3_external_broker_activity.json"


def gate_path(sqlite_path: str | Path) -> Path:
    return Path(sqlite_path).parent / GATE_FILENAME


def gate_active(sqlite_path: str | Path) -> bool:
    path = gate_path(sqlite_path)
    forced_observation = os.environ.get("GOBLIN_OBSERVATION_ONLY") == "1"
    if not path.exists():
        return forced_observation
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(payload, dict) or payload.get("version") != VERSION
            or payload.get("reason") != "external_broker_activity"
            or not isinstance(payload.get("observed_issues"), list)):
        raise ValueError(f"Invalid external broker recovery gate: {path}")
    return True


def record_external_broker_activity(
    sqlite_path: str | Path, *, issues: tuple[str, ...],
) -> Path:
    if not issues:
        raise ValueError("External broker gate needs confirmed observation issues")
    path = gate_path(sqlite_path)
    if path.exists():
        gate_active(sqlite_path)  # Validate existing marker before reusing it.
        return path  # Never overwrite the first incident/acknowledgment marker.
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = {
        "version": VERSION,
        "reason": "external_broker_activity",
        "observed_at_utc": datetime.now(UTC).isoformat(),
        "observed_issues": list(issues),
        "operator_acknowledgment_required": True,
    }
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path
