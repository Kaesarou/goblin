from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.v3.book import InventoryBook
from app.v3.features import OnlineFeatureEngine

V3_RUNTIME_STATE_VERSION = "v3_runtime_state_v1"
V3_BROKER_EQUITY_STATE_VERSION = "v3_broker_equity_reference_v1"
V3_CLOSE_RETRY_STATE_VERSION = "v3_close_retry_scheduler_v1"


@dataclass(frozen=True)
class BrokerEquityReference:
    value: float
    observed_at: datetime
    source: str
    version: str = V3_BROKER_EQUITY_STATE_VERSION


@dataclass(frozen=True)
class CloseRetryState:
    action_id: str
    attempt_count: int
    next_attempt_at: datetime
    last_error_type: str | None = None
    last_http_status: int | None = None
    last_result_state: str = "pending"
    version: str = V3_CLOSE_RETRY_STATE_VERSION


class V3RuntimeStateStore:
    """Replaceable restart state for causal features and trailing inventory state.

    The append-only inventory event ledger remains economic truth. These snapshots
    only preserve state that cannot be reconstructed from fills alone (EMA/EWM
    accumulators and trailing extrema). Version mismatch fails closed instead of
    silently reinterpreting an old state shape.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def save_broker_equity(self, *, value: float, observed_at: datetime,
                           source: str) -> BrokerEquityReference:
        if isinstance(value, bool) or not math.isfinite(value) or value <= 0 or not source:
            raise ValueError("Broker equity reference must be finite, positive and sourced")
        reference = BrokerEquityReference(float(value), _required_utc(observed_at), source)
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO v3_broker_equity VALUES (1, ?, ?, ?, ?)",
                (reference.value, reference.observed_at.isoformat(), source, reference.version),
            )
        return reference

    def load_broker_equity(self) -> BrokerEquityReference | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value, observed_at, source, version FROM v3_broker_equity WHERE id=1"
            ).fetchone()
        if row is None:
            return None
        value, observed_at, source, version = row
        if version != V3_BROKER_EQUITY_STATE_VERSION:
            raise RuntimeError(f"Unsupported broker equity reference: {version}")
        if not math.isfinite(value) or value <= 0 or not source:
            raise ValueError("Invalid persisted broker equity reference")
        return BrokerEquityReference(value, _required_utc(datetime.fromisoformat(observed_at)),
                                     source, version)

    def export_broker_equity(self) -> dict[str, Any] | None:
        reference = self.load_broker_equity()
        return None if reference is None else asdict(reference)

    def save_close_retry(self, state: CloseRetryState) -> None:
        if not state.action_id or state.attempt_count < 0:
            raise ValueError("Invalid close retry state")
        if state.version != V3_CLOSE_RETRY_STATE_VERSION:
            raise ValueError("Unsupported close retry state version")
        payload = asdict(state)
        payload["next_attempt_at"] = _required_utc(state.next_attempt_at).isoformat()
        with self._connect() as connection:
            connection.execute("INSERT OR REPLACE INTO v3_close_retry VALUES (?, ?)",
                               (state.action_id, json.dumps(payload, sort_keys=True)))

    def load_close_retries(self) -> dict[str, CloseRetryState]:
        with self._connect() as connection:
            rows = connection.execute("SELECT action_id, state_json FROM v3_close_retry").fetchall()
        result = {}
        for action_id, raw in rows:
            payload = json.loads(raw)
            if payload.get("version") != V3_CLOSE_RETRY_STATE_VERSION:
                raise RuntimeError("Unsupported close retry state version")
            payload["next_attempt_at"] = _required_utc(datetime.fromisoformat(payload["next_attempt_at"]))
            state = CloseRetryState(**payload)
            if state.action_id != action_id or state.attempt_count < 0:
                raise ValueError("Invalid persisted close retry state")
            result[action_id] = state
        return result

    def delete_close_retry(self, action_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM v3_close_retry WHERE action_id=?", (action_id,))

    def save_feature_engine(
        self,
        engine: OnlineFeatureEngine,
        *,
        symbols: Iterable[str] | None = None,
    ) -> None:
        selected = (
            None
            if symbols is None
            else {str(symbol).strip().upper() for symbol in symbols}
        )
        with self._connect() as connection:
            for symbol, state in engine.states.items():
                if selected is not None and symbol not in selected:
                    continue
                if state.last_opened_at is None:
                    continue
                connection.execute(
                    """
                    INSERT INTO v3_feature_state(symbol, state_version, asof, state_json)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        state_version=excluded.state_version,
                        asof=excluded.asof,
                        state_json=excluded.state_json
                    """,
                    (
                        symbol,
                        V3_RUNTIME_STATE_VERSION,
                        state.last_opened_at.isoformat(),
                        json.dumps(
                            _feature_state_payload(state),
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                    ),
                )

    def restore_feature_engine(
        self,
        engine: OnlineFeatureEngine,
    ) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT symbol, state_version, state_json FROM v3_feature_state"
            ).fetchall()
        restored: list[str] = []
        for symbol, version, payload_json in rows:
            normalized = str(symbol).strip().upper()
            if normalized not in engine.states:
                continue
            if str(version) != V3_RUNTIME_STATE_VERSION:
                raise RuntimeError(
                    f"Unsupported V3 feature state contract for {normalized}: {version}"
                )
            payload = json.loads(str(payload_json))
            _restore_feature_state(engine.states[normalized], payload)
            restored.append(normalized)
        return tuple(sorted(restored))

    def save_inventory_book(self, book: InventoryBook, *, asof: datetime) -> None:
        active_ids = {
            inventory.inventory_id
            for inventory in book.inventories
            if inventory.total_units > 0
        }
        with self._connect() as connection:
            for inventory in book.inventories:
                if inventory.inventory_id not in active_ids:
                    continue
                connection.execute(
                    """
                    INSERT INTO v3_inventory_runtime_state(
                        inventory_id, state_version, asof, state_json
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(inventory_id) DO UPDATE SET
                        state_version=excluded.state_version,
                        asof=excluded.asof,
                        state_json=excluded.state_json
                    """,
                    (
                        inventory.inventory_id,
                        V3_RUNTIME_STATE_VERSION,
                        asof.isoformat(),
                        json.dumps(
                            _inventory_runtime_payload(inventory),
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                    ),
                )
            if active_ids:
                placeholders = ",".join("?" for _ in active_ids)
                connection.execute(
                    f"DELETE FROM v3_inventory_runtime_state "
                    f"WHERE inventory_id NOT IN ({placeholders})",
                    tuple(sorted(active_ids)),
                )
            else:
                connection.execute("DELETE FROM v3_inventory_runtime_state")

    def restore_inventory_book(self, book: InventoryBook) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT inventory_id, state_version, asof, state_json "
                "FROM v3_inventory_runtime_state"
            ).fetchall()
        restored: list[str] = []
        by_id = {inventory.inventory_id: inventory for inventory in book.inventories}
        for inventory_id, version, asof_text, payload_json in rows:
            iid = str(inventory_id)
            inventory = by_id.get(iid)
            if inventory is None or inventory.total_units <= 0:
                continue
            if str(version) != V3_RUNTIME_STATE_VERSION:
                raise RuntimeError(
                    f"Unsupported V3 inventory runtime state: {version}"
                )
            snapshot_asof = datetime.fromisoformat(str(asof_text))
            last_fill = inventory.last_fill_at or inventory.last_entry_at
            if snapshot_asof < last_fill:
                continue
            payload = json.loads(str(payload_json))
            book._inventories[iid] = replace(
                inventory,
                trailing_min_since_open=_opt_float(
                    payload.get("trailing_min_since_open")
                ),
                trailing_max_since_min=_opt_float(
                    payload.get("trailing_max_since_min")
                ),
                trailing_max_since_open=_opt_float(
                    payload.get("trailing_max_since_open")
                ),
                trailing_min_since_max=_opt_float(
                    payload.get("trailing_min_since_max")
                ),
                min_price_since_last_entry=_opt_float(
                    payload.get("min_price_since_last_entry")
                ),
                max_price_since_last_entry=_opt_float(
                    payload.get("max_price_since_last_entry")
                ),
                min_price_since_open=_opt_float(
                    payload.get("min_price_since_open")
                ),
                max_price_since_open=_opt_float(
                    payload.get("max_price_since_open")
                ),
                mfe_pct=float(payload.get("mfe_pct", inventory.mfe_pct)),
                mae_pct=float(payload.get("mae_pct", inventory.mae_pct)),
            )
            restored.append(iid)
        return tuple(sorted(restored))

    def export_feature_state(self, engine: OnlineFeatureEngine) -> dict[str, Any]:
        """Return the causal state needed to replay a run without the VPS SQLite."""

        return {
            symbol: {
                "state_version": V3_RUNTIME_STATE_VERSION,
                "asof": _dt(state.last_opened_at),
                "state": _feature_state_payload(state),
            }
            for symbol, state in sorted(engine.states.items())
            if state.last_opened_at is not None
        }

    def export_inventory_runtime_state(self, book: InventoryBook) -> dict[str, Any]:
        return {
            inventory.inventory_id: {
                "state_version": V3_RUNTIME_STATE_VERSION,
                "state": _inventory_runtime_payload(inventory),
            }
            for inventory in sorted(book.inventories, key=lambda item: item.inventory_id)
            if inventory.total_units > 0
        }

    def feature_state_symbols(self) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT symbol FROM v3_feature_state ORDER BY symbol"
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS v3_broker_equity(
                    id INTEGER PRIMARY KEY CHECK(id=1), value REAL NOT NULL,
                    observed_at TEXT NOT NULL, source TEXT NOT NULL, version TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS v3_close_retry(
                    action_id TEXT PRIMARY KEY, state_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS v3_feature_state(
                    symbol TEXT PRIMARY KEY,
                    state_version TEXT NOT NULL,
                    asof TEXT NOT NULL,
                    state_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS v3_inventory_runtime_state(
                    inventory_id TEXT PRIMARY KEY,
                    state_version TEXT NOT NULL,
                    asof TEXT NOT NULL,
                    state_json TEXT NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)


def _required_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Persisted authority timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _feature_state_payload(state: Any) -> dict[str, Any]:
    return {
        "strategy_ema_values": [ema.value for ema in state.strategy_emas],
        "volatility_1m": _ewm_payload(state.volatility_1m),
        "forager_volatility": _ewm_payload(state.forager_volatility),
        "activity": _ewm_payload(state.activity),
        "hourly_volatility": _ewm_payload(state.hourly_volatility),
        "current_hour": _dt(state.current_hour),
        "current_hour_high": state.current_hour_high,
        "current_hour_low": state.current_hour_low,
        "completed_hour_volatility": state.completed_hour_volatility,
        "closes": list(state.closes),
        "highs": list(state.highs),
        "lows": list(state.lows),
        "last_opened_at": _dt(state.last_opened_at),
    }


def _restore_feature_state(state: Any, payload: dict[str, Any]) -> None:
    ema_values = payload.get("strategy_ema_values", [])
    if len(ema_values) != len(state.strategy_emas):
        raise RuntimeError(f"Invalid V3 EMA state for {state.symbol}")
    for ema, value in zip(state.strategy_emas, ema_values, strict=True):
        ema.value = _opt_float(value)
    _restore_ewm(state.volatility_1m, payload["volatility_1m"])
    _restore_ewm(state.forager_volatility, payload["forager_volatility"])
    _restore_ewm(state.activity, payload["activity"])
    _restore_ewm(state.hourly_volatility, payload["hourly_volatility"])
    state.current_hour = _parse_dt(payload.get("current_hour"))
    state.current_hour_high = _opt_float(payload.get("current_hour_high"))
    state.current_hour_low = _opt_float(payload.get("current_hour_low"))
    state.completed_hour_volatility = float(
        payload.get("completed_hour_volatility", 0.0)
    )
    state.closes.clear()
    state.closes.extend(float(value) for value in payload.get("closes", []))
    state.highs.clear()
    state.highs.extend(float(value) for value in payload.get("highs", []))
    state.lows.clear()
    state.lows.extend(float(value) for value in payload.get("lows", []))
    state.last_opened_at = _parse_dt(payload.get("last_opened_at"))


def _ewm_payload(ewm: Any) -> dict[str, Any]:
    return {
        "numerator": ewm.numerator,
        "denominator": ewm.denominator,
        "value": ewm.value,
    }


def _restore_ewm(ewm: Any, payload: dict[str, Any]) -> None:
    ewm.numerator = float(payload["numerator"])
    ewm.denominator = float(payload["denominator"])
    ewm.value = _opt_float(payload.get("value"))


def _inventory_runtime_payload(inventory: Any) -> dict[str, Any]:
    return {
        "trailing_min_since_open": inventory.trailing_min_since_open,
        "trailing_max_since_min": inventory.trailing_max_since_min,
        "trailing_max_since_open": inventory.trailing_max_since_open,
        "trailing_min_since_max": inventory.trailing_min_since_max,
        "min_price_since_last_entry": inventory.min_price_since_last_entry,
        "max_price_since_last_entry": inventory.max_price_since_last_entry,
        "min_price_since_open": inventory.min_price_since_open,
        "max_price_since_open": inventory.max_price_since_open,
        "mfe_pct": inventory.mfe_pct,
        "mae_pct": inventory.mae_pct,
    }


def _dt(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _parse_dt(value: Any) -> datetime | None:
    return None if value in (None, "") else datetime.fromisoformat(str(value))


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)
