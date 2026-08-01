import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.execution.position_close_reason import PositionCloseReason
from app.execution.position_models import ExitPriceSource
from app.risk.trade_cooldown import (
    TRADE_COOLDOWN_CONTRACT_VERSION,
    ClosedTradeMemoryEntry,
)
from app.utils.commons import normalize_symbol


class ClosedTradeMemoryStore:
    def __init__(self, path: str, retention_minutes: int = 240):
        self.path = Path(path)
        self.retention_minutes = retention_minutes
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def save_or_replace(
        self,
        entry: ClosedTradeMemoryEntry,
    ) -> ClosedTradeMemoryEntry:
        existing = self.find_latest(symbol=entry.symbol, side=entry.side)
        if existing is not None and existing.closed_at > entry.closed_at:
            return existing
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO closed_trade_memory (
                    cooldown_contract_version, symbol, side, close_reason,
                    opened_at, closed_at, cooldown_expires_at, position_id,
                    signal_price, executable_entry_estimate,
                    broker_entry_fill_price, pnl_entry_price,
                    pnl_exit_price, exit_price_source,
                    stop_loss, take_profit, highest_executable_price,
                    lowest_executable_price, gross_pnl, gross_pnl_percent,
                    explicit_costs_deducted, net_pnl, net_pnl_percent,
                    created_at, session_key
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?
                )
                """,
                self._entry_values(entry),
            )
        return entry

    def find_latest(
        self,
        symbol: str,
        side: str,
    ) -> ClosedTradeMemoryEntry | None:
        with self._connect() as connection:
            row = connection.execute(
                f"""
                {self._select_sql()}
                WHERE symbol = ? AND side = ?
                LIMIT 1
                """,
                (normalize_symbol(symbol), side.strip().upper()),
            ).fetchone()
        return None if row is None else self._to_entry(row)

    def find_latest_initial_stop(
        self,
        *,
        symbol: str,
    ) -> ClosedTradeMemoryEntry | None:
        with self._connect() as connection:
            row = connection.execute(
                f"""
                {self._select_sql()}
                WHERE symbol = ? AND close_reason = ?
                ORDER BY closed_at DESC
                LIMIT 1
                """,
                (
                    normalize_symbol(symbol),
                    PositionCloseReason.INITIAL_STOP.value,
                ),
            ).fetchone()
        return None if row is None else self._to_entry(row)

    def find_active_cooldown(
        self,
        *,
        symbol: str,
        side: str,
        now: datetime,
    ) -> ClosedTradeMemoryEntry | None:
        entry = self.find_latest(symbol=symbol, side=side)
        if entry is None or entry.cooldown_expires_at <= now:
            return None
        return entry

    def find_recent_take_profit(
        self,
        *,
        symbol: str,
        side: str,
        now: datetime,
        lookback_minutes: int,
    ) -> ClosedTradeMemoryEntry | None:
        entry = self.find_latest(symbol=symbol, side=side)
        if (
            entry is None
            or entry.close_reason != PositionCloseReason.TAKE_PROFIT
            or lookback_minutes <= 0
            or entry.closed_at < now - timedelta(minutes=lookback_minutes)
        ):
            return None
        return entry

    def delete_expired(self, now: datetime) -> None:
        retention_cutoff = now - timedelta(minutes=self.retention_minutes)
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM closed_trade_memory
                WHERE cooldown_expires_at <= ? AND closed_at <= ?
                """,
                (now.isoformat(), retention_cutoff.isoformat()),
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _initialize_schema(self) -> None:
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name = 'closed_trade_memory'
                """
            ).fetchone()
            if existing is not None:
                columns = {
                    str(row[1])
                    for row in connection.execute(
                        'PRAGMA table_info(closed_trade_memory)'
                    ).fetchall()
                }
                if 'cooldown_contract_version' not in columns:
                    row_count = int(
                        connection.execute(
                            'SELECT COUNT(*) FROM closed_trade_memory'
                        ).fetchone()[0]
                    )
                    if row_count:
                        raise RuntimeError(
                            'Closed-trade memory uses the obsolete cooldown '
                            'taxonomy. Rows were preserved and no legacy '
                            'close reason was inferred.'
                        )
                    connection.execute('DROP TABLE closed_trade_memory')
            self._create_table(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_closed_trade_memory_cooldown_expires_at
                ON closed_trade_memory(cooldown_expires_at)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_closed_trade_memory_closed_at
                ON closed_trade_memory(closed_at)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_closed_trade_memory_symbol_close_reason
                ON closed_trade_memory(symbol, close_reason, closed_at)
                """
            )

    def _create_table(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS closed_trade_memory (
                cooldown_contract_version TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                close_reason TEXT NOT NULL,
                opened_at TEXT,
                closed_at TEXT NOT NULL,
                cooldown_expires_at TEXT NOT NULL,
                position_id TEXT,
                signal_price REAL,
                executable_entry_estimate REAL,
                broker_entry_fill_price REAL,
                pnl_entry_price REAL,
                pnl_exit_price REAL,
                exit_price_source TEXT,
                stop_loss REAL,
                take_profit REAL,
                highest_executable_price REAL,
                lowest_executable_price REAL,
                gross_pnl REAL,
                gross_pnl_percent REAL,
                explicit_costs_deducted REAL,
                net_pnl REAL,
                net_pnl_percent REAL,
                created_at TEXT,
                session_key TEXT,
                PRIMARY KEY (symbol, side)
            )
            """
        )

    @staticmethod
    def _entry_values(entry: ClosedTradeMemoryEntry) -> tuple[Any, ...]:
        return (
            TRADE_COOLDOWN_CONTRACT_VERSION,
            normalize_symbol(entry.symbol),
            entry.side.strip().upper(),
            entry.close_reason.value,
            entry.opened_at.isoformat() if entry.opened_at else None,
            entry.closed_at.isoformat(),
            entry.cooldown_expires_at.isoformat(),
            entry.position_id,
            entry.signal_price,
            entry.executable_entry_estimate,
            entry.broker_entry_fill_price,
            entry.pnl_entry_price,
            entry.pnl_exit_price,
            entry.exit_price_source.value if entry.exit_price_source else None,
            entry.stop_loss,
            entry.take_profit,
            entry.highest_executable_price,
            entry.lowest_executable_price,
            entry.gross_pnl,
            entry.gross_pnl_percent,
            entry.explicit_costs_deducted,
            entry.net_pnl,
            entry.net_pnl_percent,
            entry.created_at.isoformat() if entry.created_at else None,
            entry.session_key,
        )

    @staticmethod
    def _select_sql() -> str:
        return """
            SELECT
                cooldown_contract_version, symbol, side, close_reason,
                opened_at, closed_at, cooldown_expires_at, position_id,
                signal_price, executable_entry_estimate,
                broker_entry_fill_price, pnl_entry_price,
                pnl_exit_price, exit_price_source,
                stop_loss, take_profit, highest_executable_price,
                lowest_executable_price, gross_pnl, gross_pnl_percent,
                explicit_costs_deducted, net_pnl, net_pnl_percent,
                created_at, session_key
            FROM closed_trade_memory
        """

    def _to_entry(self, row: tuple[Any, ...]) -> ClosedTradeMemoryEntry:
        (
            contract_version, symbol, side, close_reason, opened_at,
            closed_at, cooldown_expires_at, position_id, signal_price,
            executable_entry_estimate, broker_entry_fill_price,
            pnl_entry_price, pnl_exit_price, exit_price_source,
            stop_loss, take_profit,
            highest_executable_price, lowest_executable_price, gross_pnl,
            gross_pnl_percent, explicit_costs_deducted, net_pnl,
            net_pnl_percent, created_at, session_key,
        ) = row
        if str(contract_version) != TRADE_COOLDOWN_CONTRACT_VERSION:
            raise RuntimeError(
                f'Unsupported cooldown contract: {contract_version}'
            )
        return ClosedTradeMemoryEntry(
            symbol=str(symbol),
            side=str(side),
            close_reason=PositionCloseReason(str(close_reason)),
            opened_at=self._optional_datetime(opened_at),
            closed_at=datetime.fromisoformat(str(closed_at)),
            cooldown_expires_at=datetime.fromisoformat(
                str(cooldown_expires_at)
            ),
            position_id=self._optional_string(position_id),
            signal_price=self._optional_float(signal_price),
            executable_entry_estimate=self._optional_float(
                executable_entry_estimate
            ),
            broker_entry_fill_price=self._optional_float(
                broker_entry_fill_price
            ),
            pnl_entry_price=self._optional_float(pnl_entry_price),
            pnl_exit_price=self._optional_float(pnl_exit_price),
            exit_price_source=(
                ExitPriceSource(str(exit_price_source))
                if exit_price_source is not None
                else None
            ),
            stop_loss=self._optional_float(stop_loss),
            take_profit=self._optional_float(take_profit),
            highest_executable_price=self._optional_float(
                highest_executable_price
            ),
            lowest_executable_price=self._optional_float(
                lowest_executable_price
            ),
            gross_pnl=self._optional_float(gross_pnl),
            gross_pnl_percent=self._optional_float(gross_pnl_percent),
            explicit_costs_deducted=self._optional_float(
                explicit_costs_deducted
            ),
            net_pnl=self._optional_float(net_pnl),
            net_pnl_percent=self._optional_float(net_pnl_percent),
            created_at=self._optional_datetime(created_at),
            session_key=self._optional_string(session_key),
        )

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        return None if value is None else float(value)

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        return None if value is None else str(value)

    @staticmethod
    def _optional_datetime(value: Any) -> datetime | None:
        return None if value is None else datetime.fromisoformat(str(value))
