from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.execution.position_close_reason import PositionCloseReason
from app.execution.position_models import ExitPriceSource
from app.utils.commons import normalize_symbol

TRADE_COOLDOWN_CONTRACT_VERSION = 'trade_cooldown_v2'


@dataclass(frozen=True)
class TradeCooldownConfig:
    enabled: bool = True
    after_take_profit_minutes: int = 30
    after_initial_stop_minutes: int = 45
    after_protected_breakeven_minutes: int = 15
    after_protected_trailing_minutes: int = 15
    after_stale_exit_minutes: int = 15
    after_session_force_close_minutes: int = 15
    after_manual_or_broker_close_minutes: int = 15
    after_unknown_confirmed_close_minutes: int = 15
    initial_stop_symbol_lock_minutes: int = 15

    def duration_minutes_for(
        self,
        close_reason: PositionCloseReason,
    ) -> int:
        return {
            PositionCloseReason.TAKE_PROFIT: self.after_take_profit_minutes,
            PositionCloseReason.INITIAL_STOP: self.after_initial_stop_minutes,
            PositionCloseReason.PROTECTED_BREAKEVEN: (
                self.after_protected_breakeven_minutes
            ),
            PositionCloseReason.PROTECTED_TRAILING: (
                self.after_protected_trailing_minutes
            ),
            PositionCloseReason.STALE_EXIT: self.after_stale_exit_minutes,
            PositionCloseReason.SESSION_FORCE_CLOSE: (
                self.after_session_force_close_minutes
            ),
            PositionCloseReason.MANUAL_OR_BROKER_CLOSE: (
                self.after_manual_or_broker_close_minutes
            ),
            PositionCloseReason.UNKNOWN_CONFIRMED_CLOSE: (
                self.after_unknown_confirmed_close_minutes
            ),
        }[close_reason]

    def duration_for(
        self,
        close_reason: PositionCloseReason,
    ) -> timedelta:
        return timedelta(minutes=self.duration_minutes_for(close_reason))

    def initial_stop_symbol_lock_duration(self) -> timedelta:
        return timedelta(minutes=max(0, self.initial_stop_symbol_lock_minutes))


@dataclass(frozen=True)
class ClosedTradeMemoryEntry:
    symbol: str
    side: str
    close_reason: PositionCloseReason
    opened_at: datetime | None
    closed_at: datetime
    cooldown_expires_at: datetime
    position_id: str | None = None
    signal_price: float | None = None
    executable_entry_estimate: float | None = None
    broker_entry_fill_price: float | None = None
    pnl_entry_price: float | None = None
    pnl_exit_price: float | None = None
    exit_price_source: ExitPriceSource | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    highest_executable_price: float | None = None
    lowest_executable_price: float | None = None
    gross_pnl: float | None = None
    gross_pnl_percent: float | None = None
    explicit_costs_deducted: float | None = None
    net_pnl: float | None = None
    net_pnl_percent: float | None = None
    created_at: datetime | None = None
    session_key: str | None = None

    @property
    def expires_at(self) -> datetime:
        return self.cooldown_expires_at

    @property
    def registered_at(self) -> datetime:
        return self.created_at or self.closed_at

    @property
    def lock_scope(self) -> str:
        if self.close_reason == PositionCloseReason.INITIAL_STOP:
            return 'symbol_both_sides'
        return 'symbol_side'

    @property
    def blocked_sides(self) -> tuple[str, ...]:
        if self.close_reason == PositionCloseReason.INITIAL_STOP:
            return ('BUY', 'SELL')
        return (self.side,)

    def remaining_seconds(self, now: datetime) -> int:
        return max(0, int((self.cooldown_expires_at - now).total_seconds()))

    def symbol_lock_expires_at(
        self,
        config: TradeCooldownConfig,
    ) -> datetime:
        return self.closed_at + config.initial_stop_symbol_lock_duration()

    def symbol_lock_remaining_seconds(
        self,
        *,
        config: TradeCooldownConfig,
        now: datetime,
    ) -> int:
        return max(
            0,
            int((self.symbol_lock_expires_at(config) - now).total_seconds()),
        )


def build_closed_trade_memory_entry(
    *,
    symbol: str,
    side: str,
    config: TradeCooldownConfig,
    close_reason: PositionCloseReason,
    closed_at: datetime,
    position_id: str | None = None,
    opened_at: datetime | None = None,
    signal_price: float | None = None,
    executable_entry_estimate: float | None = None,
    broker_entry_fill_price: float | None = None,
    pnl_entry_price: float | None = None,
    pnl_exit_price: float | None = None,
    exit_price_source: ExitPriceSource | None = None,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    highest_executable_price: float | None = None,
    lowest_executable_price: float | None = None,
    gross_pnl: float | None = None,
    gross_pnl_percent: float | None = None,
    explicit_costs_deducted: float | None = None,
    net_pnl: float | None = None,
    net_pnl_percent: float | None = None,
    created_at: datetime | None = None,
    session_key: str | None = None,
) -> ClosedTradeMemoryEntry:
    return ClosedTradeMemoryEntry(
        symbol=normalize_symbol(symbol),
        side=side.strip().upper(),
        close_reason=close_reason,
        opened_at=opened_at,
        closed_at=closed_at,
        cooldown_expires_at=closed_at + config.duration_for(close_reason),
        position_id=position_id,
        signal_price=signal_price,
        executable_entry_estimate=executable_entry_estimate,
        broker_entry_fill_price=broker_entry_fill_price,
        pnl_entry_price=pnl_entry_price,
        pnl_exit_price=pnl_exit_price,
        exit_price_source=exit_price_source,
        stop_loss=stop_loss,
        take_profit=take_profit,
        highest_executable_price=highest_executable_price,
        lowest_executable_price=lowest_executable_price,
        gross_pnl=gross_pnl,
        gross_pnl_percent=gross_pnl_percent,
        explicit_costs_deducted=explicit_costs_deducted,
        net_pnl=net_pnl,
        net_pnl_percent=net_pnl_percent,
        created_at=created_at or datetime.now(timezone.utc),
        session_key=session_key,
    )
