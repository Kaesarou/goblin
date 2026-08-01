import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from app.execution.managed_stop import ManagedProtectionType
from app.execution.position_models import (
    POSITION_ECONOMICS_CONTRACT_VERSION,
    EntryPriceSource,
    ExplicitCostSource,
    TrackedPosition,
)


class PositionStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def save_open_position(self, position: TrackedPosition) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO open_positions (
                    position_contract_version, position_id, symbol, side,
                    amount, signal_price, executable_entry_estimate,
                    broker_entry_fill_price, pnl_entry_price,
                    entry_price_source, stop_loss, take_profit, opened_at,
                    initial_stop_loss,
                    highest_executable_price, lowest_executable_price,
                    highest_last_execution_price, lowest_last_execution_price,
                    breakeven_stop_enabled, breakeven_trigger_percent,
                    breakeven_buffer_percent, trailing_stop_enabled,
                    trailing_stop_trigger_percent, trailing_stop_distance_percent,
                    trailing_stop_net_buffer_percent, managed_stop_protection_type,
                    estimated_open_fee, estimated_close_fee,
                    estimated_fixed_fees, estimated_explicit_cost,
                    estimated_explicit_cost_percent,
                    pretrade_estimated_spread_cost,
                    pretrade_observed_spread_percent,
                    pretrade_estimated_total_cost,
                    pretrade_estimated_total_cost_percent,
                    explicit_cost_source, stale_position_enabled,
                    stale_position_max_age_minutes,
                    stale_position_min_favorable_move_percent,
                    stale_position_buffer_percent
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?
                )
                """,
                (
                    POSITION_ECONOMICS_CONTRACT_VERSION,
                    position.position_id,
                    position.symbol,
                    position.side,
                    position.amount,
                    position.signal_price,
                    position.executable_entry_estimate,
                    position.broker_entry_fill_price,
                    position.pnl_entry_price,
                    position.entry_price_source.value,
                    position.stop_loss,
                    position.take_profit,
                    position.opened_at.isoformat(),
                    position.initial_stop_loss,
                    position.highest_executable_price,
                    position.lowest_executable_price,
                    position.highest_last_execution_price,
                    position.lowest_last_execution_price,
                    int(position.breakeven_stop_enabled),
                    position.breakeven_trigger_percent,
                    position.breakeven_buffer_percent,
                    int(position.trailing_stop_enabled),
                    position.trailing_stop_trigger_percent,
                    position.trailing_stop_distance_percent,
                    position.trailing_stop_net_buffer_percent,
                    (
                        position.managed_stop_protection_type.value
                        if position.managed_stop_protection_type is not None
                        else None
                    ),
                    position.estimated_open_fee,
                    position.estimated_close_fee,
                    position.estimated_fixed_fees,
                    position.estimated_explicit_cost,
                    position.estimated_explicit_cost_percent,
                    position.pretrade_estimated_spread_cost,
                    position.pretrade_observed_spread_percent,
                    position.pretrade_estimated_total_cost,
                    position.pretrade_estimated_total_cost_percent,
                    position.explicit_cost_source.value,
                    int(position.stale_position_enabled),
                    position.stale_position_max_age_minutes,
                    position.stale_position_min_favorable_move_percent,
                    position.stale_position_buffer_percent,
                ),
            )

    def delete_open_position(self, position_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                'DELETE FROM open_positions WHERE position_id = ?',
                (position_id,),
            )

    def load_open_positions(self) -> list[TrackedPosition]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    position_contract_version, position_id, symbol, side,
                    amount, signal_price, executable_entry_estimate,
                    broker_entry_fill_price, pnl_entry_price,
                    entry_price_source, stop_loss, take_profit, opened_at,
                    initial_stop_loss,
                    highest_executable_price, lowest_executable_price,
                    highest_last_execution_price, lowest_last_execution_price,
                    breakeven_stop_enabled, breakeven_trigger_percent,
                    breakeven_buffer_percent, trailing_stop_enabled,
                    trailing_stop_trigger_percent, trailing_stop_distance_percent,
                    trailing_stop_net_buffer_percent, managed_stop_protection_type,
                    estimated_open_fee, estimated_close_fee,
                    estimated_fixed_fees, estimated_explicit_cost,
                    estimated_explicit_cost_percent,
                    pretrade_estimated_spread_cost,
                    pretrade_observed_spread_percent,
                    pretrade_estimated_total_cost,
                    pretrade_estimated_total_cost_percent,
                    explicit_cost_source, stale_position_enabled,
                    stale_position_max_age_minutes,
                    stale_position_min_favorable_move_percent,
                    stale_position_buffer_percent
                FROM open_positions
                ORDER BY opened_at ASC
                """
            ).fetchall()
        return [self._to_position(row) for row in rows]

    def _to_position(self, row: tuple[Any, ...]) -> TrackedPosition:
        (
            contract_version, position_id, symbol, side, amount,
            signal_price, executable_entry_estimate, broker_entry_fill_price,
            pnl_entry_price, entry_price_source, stop_loss, take_profit,
            opened_at, initial_stop_loss, highest_executable_price,
            lowest_executable_price, highest_last_execution_price,
            lowest_last_execution_price, breakeven_stop_enabled,
            breakeven_trigger_percent, breakeven_buffer_percent,
            trailing_stop_enabled, trailing_stop_trigger_percent,
            trailing_stop_distance_percent, trailing_stop_net_buffer_percent,
            managed_stop_protection_type, estimated_open_fee,
            estimated_close_fee, estimated_fixed_fees, estimated_explicit_cost,
            estimated_explicit_cost_percent, pretrade_estimated_spread_cost,
            pretrade_observed_spread_percent, pretrade_estimated_total_cost,
            pretrade_estimated_total_cost_percent, explicit_cost_source,
            stale_position_enabled, stale_position_max_age_minutes,
            stale_position_min_favorable_move_percent,
            stale_position_buffer_percent,
        ) = row
        if str(contract_version) != POSITION_ECONOMICS_CONTRACT_VERSION:
            raise RuntimeError(
                'Unsupported persisted position contract: '
                f'{contract_version}'
            )
        return TrackedPosition(
            position_id=str(position_id),
            symbol=str(symbol),
            side=str(side),
            amount=float(amount),
            signal_price=float(signal_price),
            executable_entry_estimate=float(executable_entry_estimate),
            broker_entry_fill_price=self._optional_float(
                broker_entry_fill_price
            ),
            pnl_entry_price=float(pnl_entry_price),
            entry_price_source=EntryPriceSource(str(entry_price_source)),
            stop_loss=float(stop_loss),
            take_profit=float(take_profit),
            opened_at=datetime.fromisoformat(str(opened_at)),
            initial_stop_loss=self._optional_float(initial_stop_loss),
            highest_executable_price=self._optional_float(
                highest_executable_price
            ),
            lowest_executable_price=self._optional_float(
                lowest_executable_price
            ),
            highest_last_execution_price=self._optional_float(
                highest_last_execution_price
            ),
            lowest_last_execution_price=self._optional_float(
                lowest_last_execution_price
            ),
            breakeven_stop_enabled=bool(breakeven_stop_enabled),
            breakeven_trigger_percent=float(breakeven_trigger_percent),
            breakeven_buffer_percent=float(breakeven_buffer_percent),
            trailing_stop_enabled=bool(trailing_stop_enabled),
            trailing_stop_trigger_percent=float(
                trailing_stop_trigger_percent
            ),
            trailing_stop_distance_percent=float(
                trailing_stop_distance_percent
            ),
            trailing_stop_net_buffer_percent=float(
                trailing_stop_net_buffer_percent
            ),
            managed_stop_protection_type=(
                ManagedProtectionType(str(managed_stop_protection_type))
                if managed_stop_protection_type is not None
                else None
            ),
            estimated_open_fee=float(estimated_open_fee),
            estimated_close_fee=float(estimated_close_fee),
            estimated_fixed_fees=float(estimated_fixed_fees),
            estimated_explicit_cost=float(estimated_explicit_cost),
            estimated_explicit_cost_percent=float(
                estimated_explicit_cost_percent
            ),
            pretrade_estimated_spread_cost=float(
                pretrade_estimated_spread_cost
            ),
            pretrade_observed_spread_percent=float(
                pretrade_observed_spread_percent
            ),
            pretrade_estimated_total_cost=float(
                pretrade_estimated_total_cost
            ),
            pretrade_estimated_total_cost_percent=float(
                pretrade_estimated_total_cost_percent
            ),
            explicit_cost_source=ExplicitCostSource(str(explicit_cost_source)),
            stale_position_enabled=bool(stale_position_enabled),
            stale_position_max_age_minutes=int(
                stale_position_max_age_minutes
            ),
            stale_position_min_favorable_move_percent=float(
                stale_position_min_favorable_move_percent
            ),
            stale_position_buffer_percent=float(stale_position_buffer_percent),
        )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _initialize_schema(self) -> None:
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name = 'open_positions'
                """
            ).fetchone()
            if existing is not None:
                columns = {
                    str(row[1])
                    for row in connection.execute(
                        'PRAGMA table_info(open_positions)'
                    ).fetchall()
                }
                if 'position_contract_version' not in columns:
                    row_count = int(
                        connection.execute(
                            'SELECT COUNT(*) FROM open_positions'
                        ).fetchone()[0]
                    )
                    if row_count:
                        raise RuntimeError(
                            'Open positions use the obsolete economics contract. '
                            'Deployment requires a flat portfolio; rows were '
                            'preserved and no values were inferred.'
                        )
                    connection.execute('DROP TABLE open_positions')
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS open_positions (
                    position_contract_version TEXT NOT NULL,
                    position_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    amount REAL NOT NULL,
                    signal_price REAL NOT NULL,
                    executable_entry_estimate REAL NOT NULL,
                    broker_entry_fill_price REAL,
                    pnl_entry_price REAL NOT NULL,
                    entry_price_source TEXT NOT NULL,
                    stop_loss REAL NOT NULL,
                    take_profit REAL NOT NULL,
                    opened_at TEXT NOT NULL,
                    initial_stop_loss REAL,
                    highest_executable_price REAL,
                    lowest_executable_price REAL,
                    highest_last_execution_price REAL,
                    lowest_last_execution_price REAL,
                    breakeven_stop_enabled INTEGER NOT NULL,
                    breakeven_trigger_percent REAL NOT NULL,
                    breakeven_buffer_percent REAL NOT NULL,
                    trailing_stop_enabled INTEGER NOT NULL,
                    trailing_stop_trigger_percent REAL NOT NULL,
                    trailing_stop_distance_percent REAL NOT NULL,
                    trailing_stop_net_buffer_percent REAL NOT NULL,
                    managed_stop_protection_type TEXT,
                    estimated_open_fee REAL NOT NULL,
                    estimated_close_fee REAL NOT NULL,
                    estimated_fixed_fees REAL NOT NULL,
                    estimated_explicit_cost REAL NOT NULL,
                    estimated_explicit_cost_percent REAL NOT NULL,
                    pretrade_estimated_spread_cost REAL NOT NULL,
                    pretrade_observed_spread_percent REAL NOT NULL,
                    pretrade_estimated_total_cost REAL NOT NULL,
                    pretrade_estimated_total_cost_percent REAL NOT NULL,
                    explicit_cost_source TEXT NOT NULL,
                    stale_position_enabled INTEGER NOT NULL,
                    stale_position_max_age_minutes INTEGER NOT NULL,
                    stale_position_min_favorable_move_percent REAL NOT NULL,
                    stale_position_buffer_percent REAL NOT NULL
                )
                """
            )

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        return None if value is None else float(value)
