from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InventoryEvent:
    event_id: str
    inventory_id: str
    event_type: str
    occurred_at: datetime
    payload: dict[str, Any]
    strategy_version: str
    model_version: str | None = None


@dataclass(frozen=True)
class InventorySnapshot:
    inventory_id: str
    asof: datetime
    state: dict[str, Any]


class InventoryEventStore:
    """Append-only inventory ledger with idempotent event application.

    SQLite is the economic restart truth. An optional event sink mirrors only
    newly inserted events into the compact run journal, so weekly log archives
    remain auditable without serializing causal state on every loop.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        event_sink: Callable[[InventoryEvent], None] | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._event_sink = event_sink
        self._initialize()

    def set_event_sink(
        self,
        event_sink: Callable[[InventoryEvent], None] | None,
    ) -> None:
        self._event_sink = event_sink

    def append(self, event: InventoryEvent, *, expected_event_ids: set[str] | None = None) -> bool:
        actual_event = _deduplicate_high_frequency_event(event)
        with self._connect() as connection:
            if expected_event_ids is not None:
                connection.execute("BEGIN IMMEDIATE")
                actual_ids = {row[0] for row in connection.execute("SELECT event_id FROM inventory_events")}
                if actual_ids != expected_event_ids:
                    raise RuntimeError("Inventory ledger changed during operator reconciliation")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO inventory_events (
                    event_id, inventory_id, event_type, occurred_at,
                    payload_json, strategy_version, model_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    actual_event.event_id,
                    actual_event.inventory_id,
                    actual_event.event_type,
                    actual_event.occurred_at.isoformat(),
                    json.dumps(
                        _jsonable(actual_event.payload),
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    actual_event.strategy_version,
                    actual_event.model_version,
                ),
            )
            inserted = cursor.rowcount == 1
        if inserted and self._event_sink is not None:
            try:
                self._event_sink(actual_event)
            except Exception:
                logger.exception(
                    "V3 inventory event journal mirror failed | event_type=%s | event_id=%s",
                    actual_event.event_type,
                    actual_event.event_id,
                )
        return inserted

    def events(self, inventory_id: str | None = None) -> list[InventoryEvent]:
        query = (
            "SELECT event_id, inventory_id, event_type, occurred_at, payload_json, "
            "strategy_version, model_version FROM inventory_events"
        )
        params: tuple[object, ...] = ()
        if inventory_id is not None:
            query += " WHERE inventory_id = ?"
            params = (inventory_id,)
        query += " ORDER BY occurred_at ASC, rowid ASC"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            InventoryEvent(
                event_id=str(row[0]),
                inventory_id=str(row[1]),
                event_type=str(row[2]),
                occurred_at=datetime.fromisoformat(str(row[3])),
                payload=json.loads(str(row[4])),
                strategy_version=str(row[5]),
                model_version=(None if row[6] is None else str(row[6])),
            )
            for row in rows
        ]

    def save_snapshot(self, snapshot: InventorySnapshot) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO inventory_snapshots (inventory_id, asof, state_json)
                VALUES (?, ?, ?)
                ON CONFLICT(inventory_id) DO UPDATE SET
                    asof = excluded.asof,
                    state_json = excluded.state_json
                """,
                (
                    snapshot.inventory_id,
                    snapshot.asof.isoformat(),
                    json.dumps(
                        _jsonable(snapshot.state),
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )

    def load_snapshot(self, inventory_id: str) -> InventorySnapshot | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT inventory_id, asof, state_json FROM inventory_snapshots "
                "WHERE inventory_id = ?",
                (inventory_id,),
            ).fetchone()
        if row is None:
            return None
        return InventorySnapshot(
            inventory_id=str(row[0]),
            asof=datetime.fromisoformat(str(row[1])),
            state=json.loads(str(row[2])),
        )

    def delete_snapshot(self, inventory_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM inventory_snapshots WHERE inventory_id = ?",
                (inventory_id,),
            )

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS inventory_events (
                    event_id TEXT PRIMARY KEY,
                    inventory_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    model_version TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_inventory_events_inventory_time
                ON inventory_events (inventory_id, occurred_at);

                CREATE TABLE IF NOT EXISTS inventory_snapshots (
                    inventory_id TEXT PRIMARY KEY,
                    asof TEXT NOT NULL,
                    state_json TEXT NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


def _deduplicate_high_frequency_event(event: InventoryEvent) -> InventoryEvent:
    if event.event_type != "CLOSE_CONFIRMATION_ERROR":
        return event
    action_id = str(event.payload.get("action_id", "")).strip()
    if not action_id:
        return event
    return replace(
        event,
        event_id=f"{action_id}:close-confirmation-error",
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(v) for v in value]
    return value
