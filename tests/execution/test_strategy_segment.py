from datetime import UTC, datetime, timedelta

import pytest

from app.execution.candidate_ranking import build_trade_candidate
from app.execution.strategy_segment import StrategySegment
from app.instruments.models import AssetClass
from app.market.models import Candle, MarketSnapshot
from app.strategies.signals import Signal

NOW = datetime(2026, 8, 4, 15, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ('asset_class', 'side', 'expected'),
    [
        (AssetClass.EQUITY_EU, 'BUY', StrategySegment.EQUITY_EU_BUY),
        (AssetClass.EQUITY_EU, 'SELL', StrategySegment.EQUITY_EU_SELL),
        (AssetClass.EQUITY_US, 'BUY', StrategySegment.EQUITY_US_BUY),
        (AssetClass.EQUITY_US, 'SELL', StrategySegment.EQUITY_US_SELL),
        (AssetClass.CRYPTO, 'BUY', StrategySegment.CRYPTO_BUY),
        (AssetClass.CRYPTO, 'SELL', StrategySegment.CRYPTO_SELL),
    ],
)
def test_candidate_builder_assigns_first_class_segment(
    asset_class,
    side,
    expected,
):
    candidate = build_trade_candidate(
        symbol='AMD',
        snapshot=MarketSnapshot('AMD', 99.9, 100.1, 100.0, NOW),
        candle=Candle(
            'AMD',
            60,
            99.8,
            100.2,
            99.7,
            100.0,
            None,
            NOW - timedelta(minutes=1),
            NOW,
        ),
        signal=Signal(
            side,
            0.8,
            'test',
            metadata={
                'session_move_percent': 0.5,
                'trend_strength_percent': 0.2,
                'close_position_percent': 90.0,
                'atr_percent': 0.3,
            },
        ),
        asset_class=asset_class,
    )

    assert candidate.segment is expected
    assert candidate.segment.asset_class is asset_class
    assert candidate.segment.side == side


def test_unsupported_side_fails_closed():
    with pytest.raises(ValueError, match='Unsupported strategy side'):
        StrategySegment.from_asset_and_side(AssetClass.EQUITY_US, 'HOLD')
