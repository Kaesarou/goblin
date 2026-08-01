from datetime import UTC, datetime, timedelta

import pytest

from app.execution.position_close_reason import PositionCloseReason
from app.risk.trade_cooldown import (
    TradeCooldownConfig,
    build_closed_trade_memory_entry,
)


CLOSED_AT = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ('reason', 'minutes'),
    [
        (PositionCloseReason.TAKE_PROFIT, 30),
        (PositionCloseReason.INITIAL_STOP, 45),
        (PositionCloseReason.PROTECTED_BREAKEVEN, 15),
        (PositionCloseReason.PROTECTED_TRAILING, 15),
        (PositionCloseReason.STALE_EXIT, 15),
        (PositionCloseReason.SESSION_FORCE_CLOSE, 15),
        (PositionCloseReason.MANUAL_OR_BROKER_CLOSE, 15),
        (PositionCloseReason.UNKNOWN_CONFIRMED_CLOSE, 15),
    ],
)
def test_canonical_close_reason_selects_exact_cooldown_policy(
    reason: PositionCloseReason,
    minutes: int,
):
    entry = build_closed_trade_memory_entry(
        symbol=' amd ',
        side=' sell ',
        config=TradeCooldownConfig(),
        close_reason=reason,
        closed_at=CLOSED_AT,
        gross_pnl=10.0,
        net_pnl=9.0,
    )

    assert entry.symbol == 'AMD'
    assert entry.side == 'SELL'
    assert entry.close_reason is reason
    assert entry.expires_at == CLOSED_AT + timedelta(minutes=minutes)


def test_positive_protected_breakeven_never_uses_take_profit_duration():
    entry = build_closed_trade_memory_entry(
        symbol='MU',
        side='SELL',
        config=TradeCooldownConfig(
            after_take_profit_minutes=90,
            after_protected_breakeven_minutes=12,
        ),
        close_reason=PositionCloseReason.PROTECTED_BREAKEVEN,
        closed_at=CLOSED_AT,
        gross_pnl=25.0,
        net_pnl=20.0,
    )

    assert entry.cooldown_expires_at == CLOSED_AT + timedelta(minutes=12)
