from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from app.execution.position_close_reason import PositionCloseReason
from app.execution.position_models import (
    POSITION_ECONOMICS_CONTRACT_VERSION,
    PositionCloseSignal,
)
from app.runtime.pending_close import CloseState, PendingClose


class PendingCloseStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def save(self, pending: PendingClose) -> None:
        signal = pending.signal
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO pending_closes (
                    position_contract_version, position_id, symbol, side,
                    state, close_reason, source, last_execution_price,
                    executable_estimate, bid_at_detection, ask_at_detection,
                    observed_spread_percent, signal_detected_at, requested_at,
                    submitted_at, accepted_at, close_order_id, reference_id,
                    confirmation_checks, last_confirmation_at, last_error,
                    metadata_json, delayed_reported_at,
                    manual_intervention_reported_at
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?
                )
                """,
                (
                    POSITION_ECONOMICS_CONTRACT_VERSION,
                    pending.position_id,
                    pending.symbol,
                    signal.side,
                    pending.state.value,
                    signal.reason.value,
                    pending.source,
                    signal.last_execution_price,
                    signal.executable_estimate,
                    signal.bid_at_detection,
                    signal.ask_at_detection,
                    signal.observed_spread_percent,
                    signal.detected_at.isoformat(),
                    pending.requested_at.isoformat(),
                    self._datetime_text(pending.submitted_at),
                    self._datetime_text(pending.accepted_at),
                    pending.close_order_id,
                    pending.reference_id,
                    pending.confirmation_checks,
                    self._datetime_text(pending.last_confirmation_at),
                    pending.last_error,
                    json.dumps(
                        {
                            'signal_metadata': signal.metadata,
                            'runtime_metadata': pending.metadata,
                        },
                        ensure_ascii=False,
                        separators=(',', ':'),
                        sort_keys=True,
                    ),
                    self._datetime_text(pending.delayed_reported_at),
                    self._datetime_text(
                        pending.manual_intervention_reported_at
                    ),
                ),
            )

    def delete(self, position_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                'DELETE FROM pending_closes WHERE position_id = ?',
                (position_id,),
            )

    def delete_with_open_position(self, position_id: str) -> None:
        """Clear confirmed close state atomically on the main loop."""
        with self._connect() as connection:
            connection.execute(
                'DELETE FROM pending_closes WHERE position_id = ?',
                (position_id,),
            )
            connection.execute(
                'DELETE FROM open_positions WHERE position_id = ?',
                (position_id,),
            )

    def load_all(self) -> list[PendingClose]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    position_contract_version, position_id, symbol, side,
                    state, close_reason, source, last_execution_price,
                    executable_estimate, bid_at_detection, ask_at_detection,
                    observed_spread_percent, signal_detected_at, requested_at,
                    submitted_at, accepted_at, close_order_id, reference_id,
                    confirmation_checks, last_confirmation_at, last_error,
                    metadata_json, delayed_reported_at,
                    manual_intervention_reported_at
                FROM pending_closes
                ORDER BY requested_at ASC
                """
            ).fetchall()
        return [self._to_pending(row) for row in rows]

    def _to_pending(self, row: tuple[Any, ...]) -> PendingClose:
        (
            contract_version, position_id, symbol, side, state, close_reason,
            source, last_execution_price, executable_estimate,
            bid_at_detection, ask_at_detection, observed_spread_percent,
            signal_detected_at, requested_at, submitted_at, accepted_at,
            close_order_id, reference_id, confirmation_checks,
            last_confirmation_at, last_error, metadata_json,
            delayed_reported_at, manual_intervention_reported_at,
        ) = row
        if str(contract_version) != POSITION_ECONOMICS_CONTRACT_VERSION:
            raise RuntimeError(
                f'Unsupported pending close contract: {contract_version}'
            )
        metadata_payload = self._load_metadata(metadata_json)
        signal_metadata = metadata_payload.get('signal_metadata')
        runtime_metadata = metadata_payload.get('runtime_metadata')
        return PendingClose(
            position_id=str(position_id),
            symbol=str(symbol),
            signal=PositionCloseSignal(
                position_id=str(position_id),
                symbol=str(symbol),
                side=str(side),
                reason=PositionCloseReason(str(close_reason)),
                detected_at=datetime.fromisoformat(str(signal_detected_at)),
                last_execution_price=float(last_execution_price),
                executable_estimate=float(executable_estimate),
                bid_at_detection=float(bid_at_detection),
                ask_at_detection=float(ask_at_detection),
                observed_spread_percent=float(observed_spread_percent),
                metadata=(
                    dict(signal_metadata)
                    if isinstance(signal_metadata, dict)
                    else None
                ),
            ),
            source=str(source),
            state=CloseState(str(state)),
            requested_at=datetime.fromisoformat(str(requested_at)),
            submitted_at=self._optional_datetime(submitted_at),
            accepted_at=self._optional_datetime(accepted_at),
            close_order_id=(
                str(close_order_id) if close_order_id is not None else None
            ),
            reference_id=(
                str(reference_id) if reference_id is not None else None
            ),
            confirmation_checks=int(confirmation_checks),
            last_confirmation_at=self._optional_datetime(last_confirmation_at),
            last_error=str(last_error) if last_error is not None else None,
            metadata=(
                dict(runtime_metadata)
                if isinstance(runtime_metadata, dict)
                else None
            ),
            delayed_reported_at=self._optional_datetime(delayed_reported_at),
            manual_intervention_reported_at=self._optional_datetime(
                manual_intervention_reported_at
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _initialize_schema(self) -> None:
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name = 'pending_closes'
                """
            ).fetchone()
            if existing is not None:
                columns = {
                    str(row[1])
                    for row in connection.execute(
                        'PRAGMA table_info(pending_closes)'
                    ).fetchall()
                }
                if 'position_contract_version' not in columns:
                    row_count = int(
                        connection.execute(
                            'SELECT COUNT(*) FROM pending_closes'
                        ).fetchone()[0]
                    )
                    if row_count:
                        raise RuntimeError(
                            'Pending closes use the obsolete price contract. '
                            'Rows and reserved risk were preserved; no exit '
                            'price was inferred.'
                        )
                    connection.execute('DROP TABLE pending_closes')
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_closes (
                    position_contract_version TEXT NOT NULL,
                    position_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    state TEXT NOT NULL,
                    close_reason TEXT NOT NULL,
                    source TEXT NOT NULL,
                    last_execution_price REAL NOT NULL,
                    executable_estimate REAL NOT NULL,
                    bid_at_detection REAL NOT NULL,
                    ask_at_detection REAL NOT NULL,
                    observed_spread_percent REAL NOT NULL,
                    signal_detected_at TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    submitted_at TEXT,
                    accepted_at TEXT,
                    close_order_id TEXT,
                    reference_id TEXT,
                    confirmation_checks INTEGER NOT NULL DEFAULT 0,
                    last_confirmation_at TEXT,
                    last_error TEXT,
                    metadata_json TEXT NOT NULL,
                    delayed_reported_at TEXT,
                    manual_intervention_reported_at TEXT
                )
                """
            )

    @staticmethod
    def _datetime_text(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    @staticmethod
    def _optional_datetime(value: Any) -> datetime | None:
        return datetime.fromisoformat(str(value)) if value is not None else None

    @staticmethod
    def _load_metadata(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        try:
            decoded = json.loads(str(value))
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
