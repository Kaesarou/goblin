from datetime import datetime, timezone

import pytest

from app.execution.sl_tp_profile import EffectiveSlTpResolver
from app.execution.trade_candidate import TradeCandidate
from app.instruments.models import AssetClass, RiskProfile
from app.market.models import Candle, MarketSnapshot
from app.strategies.signals import Signal


def risk_profile() -> RiskProfile:
    return RiskProfile(
        asset_class=AssetClass.EQUITY_US,
        profile_key='us_test_fixed_v1',
        max_position_size_percent=1.0,
        stop_loss_percent=0.7,
        take_profit_percent=1.2,
        force_close_enabled=False,
        force_close_hour=21,
        force_close_minute=55,
        max_spread_percent=1.0,
        min_move_spread_ratio=0.0,
    )


def candidate(
    *,
    action: str = 'BUY',
    atr_percent: float | None = 0.8,
) -> TradeCandidate:
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    snapshot = MarketSnapshot(
        symbol='AAPL',
        bid=99.95,
        ask=100.05,
        last=100.0,
        timestamp=now,
    )
    candle = Candle(
        symbol='AAPL',
        timeframe_seconds=60,
        open=99.5,
        high=100.5,
        low=99.0,
        close=100.0,
        volume=None,
        opened_at=now,
        closed_at=now,
    )
    metadata = {} if atr_percent is None else {'atr_percent': atr_percent}
    return TradeCandidate(
        symbol='AAPL',
        snapshot=snapshot,
        candle=candle,
        signal=Signal(
            action=action,
            setup_quality=0.8,
            reason='test',
            metadata=metadata,
        ),
        rank_reason='test',
    )


@pytest.mark.parametrize('action', ['BUY', 'SELL'])
@pytest.mark.parametrize('atr_percent', [None, 0.1, 0.8, 3.0])
def test_resolver_always_uses_named_fixed_profile(
    action: str,
    atr_percent: float | None,
):
    effective = EffectiveSlTpResolver().resolve(
        candidate=candidate(action=action, atr_percent=atr_percent),
        risk_profile=risk_profile(),
    )

    assert effective.mode == 'fixed'
    assert effective.source == 'us_test_fixed_v1'
    assert effective.stop_loss_percent == pytest.approx(0.7)
    assert effective.take_profit_percent == pytest.approx(1.2)
    assert effective.atr_percent == atr_percent
    assert effective.metadata['profile_key'] == 'us_test_fixed_v1'
    assert effective.metadata['side'] == action
