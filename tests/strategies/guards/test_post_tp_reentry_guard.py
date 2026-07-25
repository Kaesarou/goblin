from datetime import datetime, timedelta, timezone

from app.execution.trade_candidate import TradeCandidate
from app.market.models import Candle, MarketSnapshot
from app.persistence.closed_trade_memory_store import ClosedTradeMemoryStore
from app.risk.trade_cooldown import CloseReason, ClosedTradeMemoryEntry
from app.strategies.guards.post_tp_reentry_guard import (
    CONSUMED_MOVE_AFTER_TP_REASON,
    POST_TP_RESET_CONFIRMED_REASON,
    PostTpReentryConfig,
    PostTpReentryGuard,
)
from app.strategies.signals import Signal

NOW = datetime(2026, 7, 9, 18, 0, tzinfo=timezone.utc)


def save_tp(store: ClosedTradeMemoryStore):
    store.save_or_replace(
        ClosedTradeMemoryEntry(
            symbol='META',
            side='BUY',
            close_reason=CloseReason.TAKE_PROFIT,
            raw_close_reason='take_profit_hit',
            opened_at=NOW - timedelta(minutes=20),
            closed_at=NOW - timedelta(minutes=15),
            cooldown_expires_at=NOW - timedelta(minutes=5),
            position_id='meta-buy-1',
            entry_price=100.0,
            exit_price=101.0,
            take_profit=101.0,
            highest_price=101.2,
        )
    )


def candidate(side: str, price: float) -> TradeCandidate:
    candle = Candle('META', 60, price, price + 0.1, price - 0.1, price, None, NOW, NOW)
    snapshot = MarketSnapshot('META', price - 0.01, price + 0.01, price, NOW)
    return TradeCandidate(
        symbol='META',
        snapshot=snapshot,
        candle=candle,
        signal=Signal(action=side, setup_quality=0.8, reason='test', metadata={}),
        rank_reason='test',
    )


def test_blocks_same_side_after_tp_without_reset(tmp_path):
    store = ClosedTradeMemoryStore(str(tmp_path / 'goblin.sqlite'))
    save_tp(store)

    decision = PostTpReentryGuard(store).check(
        candidate=candidate('BUY', 101.15),
        config=PostTpReentryConfig(),
        now=NOW,
    )

    assert not decision.allowed
    assert decision.reason == CONSUMED_MOVE_AFTER_TP_REASON


def test_allows_same_side_after_pullback_reset(tmp_path):
    store = ClosedTradeMemoryStore(str(tmp_path / 'goblin.sqlite'))
    save_tp(store)

    decision = PostTpReentryGuard(store).check(
        candidate=candidate('BUY', 100.70),
        config=PostTpReentryConfig(),
        now=NOW,
    )

    assert decision.allowed
    assert decision.reason == POST_TP_RESET_CONFIRMED_REASON


def test_does_not_block_opposite_side(tmp_path):
    store = ClosedTradeMemoryStore(str(tmp_path / 'goblin.sqlite'))
    save_tp(store)

    decision = PostTpReentryGuard(store).check(
        candidate=candidate('SELL', 100.70),
        config=PostTpReentryConfig(),
        now=NOW,
    )

    assert decision.allowed
