from datetime import datetime, timedelta, timezone

import pytest

from app.persistence.closed_trade_memory_store import ClosedTradeMemoryStore
from app.execution.position_close_reason import PositionCloseReason
from app.risk.trade_cooldown import (
    ClosedTradeMemoryEntry,
    TradeCooldownConfig,
)
from app.strategies.guards.fixed_trade_cooldown_guard import FixedTradeCooldownGuard


CLOSED_AT = datetime(2026, 7, 10, 15, 0, tzinfo=timezone.utc)
CONFIG = TradeCooldownConfig(
    after_take_profit_minutes=30,
    after_initial_stop_minutes=45,
    initial_stop_symbol_lock_minutes=15,
)


def _entry(
    *,
    symbol: str = 'MU',
    side: str = 'SELL',
    close_reason: PositionCloseReason = PositionCloseReason.INITIAL_STOP,
) -> ClosedTradeMemoryEntry:
    cooldown_minutes = (
        45 if close_reason == PositionCloseReason.INITIAL_STOP else 30
    )
    return ClosedTradeMemoryEntry(
        symbol=symbol,
        side=side,
        close_reason=close_reason,
        opened_at=CLOSED_AT - timedelta(minutes=5),
        closed_at=CLOSED_AT,
        cooldown_expires_at=CLOSED_AT + timedelta(minutes=cooldown_minutes),
        position_id=f'{symbol}-{side}',
        created_at=CLOSED_AT,
        session_key='US-2026-07-10',
    )


def _guard(tmp_path, entry: ClosedTradeMemoryEntry) -> FixedTradeCooldownGuard:
    store = ClosedTradeMemoryStore(str(tmp_path / 'goblin.sqlite'))
    store.save_or_replace(entry)
    return FixedTradeCooldownGuard(store)


@pytest.mark.parametrize(
    ('source_side', 'candidate_side'),
    [
        ('SELL', 'SELL'),
        ('SELL', 'BUY'),
        ('BUY', 'BUY'),
        ('BUY', 'SELL'),
    ],
)
def test_stop_loss_locks_both_sides(
    tmp_path,
    source_side: str,
    candidate_side: str,
):
    guard = _guard(tmp_path, _entry(side=source_side))

    decision = guard.check(
        symbol='MU',
        side=candidate_side,
        config=CONFIG,
        now=CLOSED_AT + timedelta(minutes=5),
    )

    assert not decision.allowed
    assert decision.reason == 'cooldown_after_initial_stop_symbol_lock'
    assert decision.lock_scope == 'symbol_both_sides'
    assert decision.blocked_sides == ('BUY', 'SELL')
    assert decision.remaining_seconds == 10 * 60


def test_take_profit_does_not_lock_opposite_side(tmp_path):
    guard = _guard(
        tmp_path,
        _entry(close_reason=PositionCloseReason.TAKE_PROFIT),
    )

    decision = guard.check(
        symbol='MU',
        side='BUY',
        config=CONFIG,
        now=CLOSED_AT + timedelta(minutes=5),
    )

    assert decision.allowed


def test_symbol_lock_expires_before_same_side_stop_loss_cooldown(tmp_path):
    guard = _guard(tmp_path, _entry(side='SELL'))
    now = CLOSED_AT + timedelta(minutes=16)

    opposite_side = guard.check(
        symbol='MU',
        side='BUY',
        config=CONFIG,
        now=now,
    )
    same_side = guard.check(
        symbol='MU',
        side='SELL',
        config=CONFIG,
        now=now,
    )

    assert opposite_side.allowed
    assert not same_side.allowed
    assert same_side.reason == 'cooldown_after_initial_stop'
    assert same_side.lock_scope == 'symbol_side'
    assert same_side.remaining_seconds == 29 * 60


def test_symbol_lock_does_not_affect_other_symbols(tmp_path):
    guard = _guard(tmp_path, _entry(symbol='MU'))

    decision = guard.check(
        symbol='NVDA',
        side='BUY',
        config=CONFIG,
        now=CLOSED_AT + timedelta(minutes=5),
    )

    assert decision.allowed


def test_persisted_stop_loss_lock_survives_restart(tmp_path):
    path = str(tmp_path / 'goblin.sqlite')
    first_store = ClosedTradeMemoryStore(path)
    first_store.save_or_replace(_entry(side='SELL'))

    restarted_guard = FixedTradeCooldownGuard(ClosedTradeMemoryStore(path))
    decision = restarted_guard.check(
        symbol='MU',
        side='BUY',
        config=CONFIG,
        now=CLOSED_AT + timedelta(minutes=5),
    )

    assert not decision.allowed
    assert decision.active_cooldown is not None
    assert decision.active_cooldown.symbol == 'MU'
    assert decision.active_cooldown.side == 'SELL'
