from datetime import datetime, timedelta, timezone

from app.persistence.closed_trade_memory_store import ClosedTradeMemoryStore
from app.execution.position_close_reason import PositionCloseReason
from app.risk.trade_cooldown import ClosedTradeMemoryEntry, TradeCooldownConfig
from app.strategies.guards.fixed_trade_cooldown_guard import FixedTradeCooldownGuard


def save_tp(store: ClosedTradeMemoryStore) -> ClosedTradeMemoryEntry:
    closed_at = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    entry = ClosedTradeMemoryEntry(
        symbol='AMD',
        side='SELL',
        close_reason=PositionCloseReason.TAKE_PROFIT,
        opened_at=closed_at - timedelta(minutes=5),
        closed_at=closed_at,
        cooldown_expires_at=closed_at + timedelta(minutes=30),
        position_id='position-1',
        pnl_entry_price=100.0,
        pnl_exit_price=99.0,
        take_profit=99.0,
        created_at=closed_at,
    )
    return store.save_or_replace(entry)


def test_fixed_trade_cooldown_blocks_same_symbol_and_side(tmp_path):
    store = ClosedTradeMemoryStore(str(tmp_path / 'goblin.sqlite'))
    entry = save_tp(store)
    guard = FixedTradeCooldownGuard(store)

    decision = guard.check(
        symbol='amd',
        side='sell',
        config=TradeCooldownConfig(),
        now=entry.closed_at + timedelta(minutes=10),
    )

    assert not decision.allowed
    assert decision.reason == 'cooldown_after_take_profit'
    assert decision.remaining_seconds == 20 * 60


def test_fixed_trade_cooldown_allows_opposite_side(tmp_path):
    store = ClosedTradeMemoryStore(str(tmp_path / 'goblin.sqlite'))
    entry = save_tp(store)
    guard = FixedTradeCooldownGuard(store)

    decision = guard.check(
        symbol='AMD',
        side='BUY',
        config=TradeCooldownConfig(),
        now=entry.closed_at + timedelta(minutes=10),
    )

    assert decision.allowed
