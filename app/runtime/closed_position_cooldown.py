import logging
from datetime import datetime

from app.execution.position_close_reason import PositionCloseReason
from app.execution.position_models import ClosedPosition, TrackedPosition
from app.journal.jsonl_journal import JsonlJournal
from app.persistence.trade_cooldown_store import TradeCooldownStore
from app.risk.risk_manager import RiskManager
from app.risk.trade_cooldown import build_closed_trade_memory_entry

logger = logging.getLogger(__name__)


def _cooldown_payload(*, entry, cooldown_config) -> dict:
    symbol_lock_expires_at = None
    if entry.close_reason == PositionCloseReason.INITIAL_STOP:
        symbol_lock_expires_at = entry.symbol_lock_expires_at(cooldown_config)
    return {
        "entry": entry,
        "session_key": entry.session_key,
        "lock_scope": entry.lock_scope,
        "blocked_sides": list(entry.blocked_sides),
        "registered_at": entry.registered_at,
        "expires_at": entry.expires_at,
        "symbol_lock_expires_at": symbol_lock_expires_at,
    }


def register_trade_cooldown_for_closed_position(
    *,
    closed_position: ClosedPosition | None,
    risk_manager: RiskManager,
    cooldown_store: TradeCooldownStore,
    trade_journal: JsonlJournal,
    session_key: str | None = None,
) -> None:
    if closed_position is None:
        return
    cooldown_config = risk_manager.risk_profile_for(closed_position.symbol).trade_cooldown
    if not cooldown_config.enabled:
        return
    entry = build_closed_trade_memory_entry(
        symbol=closed_position.symbol,
        side=closed_position.side,
        config=cooldown_config,
        close_reason=closed_position.close_reason,
        opened_at=closed_position.opened_at,
        closed_at=closed_position.closed_at,
        position_id=closed_position.position_id,
        signal_price=closed_position.signal_price,
        executable_entry_estimate=closed_position.executable_entry_estimate,
        broker_entry_fill_price=closed_position.broker_entry_fill_price,
        pnl_entry_price=closed_position.pnl_entry_price,
        pnl_exit_price=closed_position.pnl_exit_price,
        exit_price_source=closed_position.exit_price_source,
        stop_loss=closed_position.stop_loss,
        take_profit=closed_position.take_profit,
        highest_executable_price=closed_position.highest_executable_price,
        lowest_executable_price=closed_position.lowest_executable_price,
        gross_pnl=closed_position.gross_pnl,
        gross_pnl_percent=closed_position.gross_pnl_percent,
        explicit_costs_deducted=closed_position.explicit_costs_deducted,
        net_pnl=closed_position.net_pnl,
        net_pnl_percent=closed_position.net_pnl_percent,
        session_key=session_key,
    )
    saved_entry = cooldown_store.save_or_extend(entry)
    trade_journal.write(
        "trade_cooldown_registered",
        {
            "source": "confirmed_position_close",
            **_cooldown_payload(
                entry=saved_entry,
                cooldown_config=cooldown_config,
            ),
            "closed_position": closed_position,
        },
    )
    logger.info(
        "Trade cooldown registered | symbol=%s | side=%s | reason=%s | "
        "lock_scope=%s | expires_at=%s",
        saved_entry.symbol,
        saved_entry.side,
        saved_entry.close_reason.value,
        saved_entry.lock_scope,
        saved_entry.expires_at.isoformat(),
    )


def register_trade_cooldown_for_unknown_confirmed_close(
    *,
    position: TrackedPosition,
    closed_at: datetime,
    risk_manager: RiskManager,
    cooldown_store: TradeCooldownStore,
    trade_journal: JsonlJournal,
    session_key: str | None = None,
) -> None:
    cooldown_config = risk_manager.risk_profile_for(position.symbol).trade_cooldown
    if not cooldown_config.enabled:
        return
    entry = build_closed_trade_memory_entry(
        symbol=position.symbol,
        side=position.side,
        config=cooldown_config,
        close_reason=PositionCloseReason.UNKNOWN_CONFIRMED_CLOSE,
        opened_at=position.opened_at,
        closed_at=closed_at,
        position_id=position.position_id,
        signal_price=position.signal_price,
        executable_entry_estimate=position.executable_entry_estimate,
        broker_entry_fill_price=position.broker_entry_fill_price,
        pnl_entry_price=position.pnl_entry_price,
        stop_loss=position.stop_loss,
        take_profit=position.take_profit,
        highest_executable_price=position.highest_executable_price,
        lowest_executable_price=position.lowest_executable_price,
        session_key=session_key,
    )
    saved_entry = cooldown_store.save_or_extend(entry)
    trade_journal.write(
        "trade_cooldown_registered",
        {
            "source": "unknown_confirmed_close_without_price",
            **_cooldown_payload(
                entry=saved_entry,
                cooldown_config=cooldown_config,
            ),
            "position": position,
        },
    )
