from datetime import datetime, timezone

import pytest

from app.execution.candidate_ranking import build_trade_candidate
from app.execution.scoring.move_exhaustion import MoveExhaustionAnalyzer
from app.execution.scoring.signal_scorer import directional_score_breakdown
from app.market.models import Candle, MarketSnapshot
from app.strategies.signals import Signal

BASE_TIME = datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)


def candle(
    *,
    open_price: float = 100.0,
    high: float = 102.0,
    low: float = 99.5,
    close: float = 101.9,
) -> Candle:
    return Candle(
        symbol='BTC',
        timeframe_seconds=60,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=None,
        opened_at=BASE_TIME,
        closed_at=BASE_TIME,
    )


def snapshot(last: float = 101.9) -> MarketSnapshot:
    return MarketSnapshot(
        symbol='BTC',
        bid=last,
        ask=last,
        last=last,
        timestamp=BASE_TIME,
    )


def signal(action: str = 'BUY', **metadata) -> Signal:
    base_metadata = {
        'session_move_percent': 1.0,
        'trend_strength_percent': 0.2,
        'breakout_percent': 0.1,
        'breakdown_percent': 0.0,
        'candle_range_percent': 0.4,
        'close_position_percent': 95.0,
        'atr_percent': 0.5,
        'snapshot_momentum_percent': 0.25,
    }
    base_metadata.update(metadata)
    return Signal(
        action=action,
        setup_quality=0.8,
        reason='test_signal',
        metadata=base_metadata,
    )


def test_move_exhaustion_keeps_healthy_accelerating_buy_penalty_low():
    analysis = MoveExhaustionAnalyzer().analyze(
        candle=candle(high=103.0, close=102.5),
        signal=signal(
            session_move_percent=1.4,
            snapshot_momentum_percent=0.35,
            close_position_percent=90.0,
        ),
        close_quality=90.0,
    )

    assert analysis.late_entry_risk == 0.0
    assert analysis.exhaustion_penalty == 0.0
    assert analysis.remaining_move_quality == 'GOOD'
    assert analysis.late_entry_severity == 'LOW'


def test_move_exhaustion_penalizes_extended_buy_with_deceleration_near_high():
    analysis = MoveExhaustionAnalyzer().analyze(
        candle=candle(high=103.01, close=103.0, low=101.0),
        signal=signal(
            session_move_percent=3.0,
            snapshot_momentum_percent=0.02,
            close_position_percent=99.5,
            atr_percent=0.4,
        ),
        close_quality=99.5,
    )

    assert analysis.late_entry_risk > 40
    assert analysis.exhaustion_penalty > 7
    assert analysis.late_entry_severity == 'HIGH'
    assert analysis.momentum_deceleration_detected is True
    assert 'extended_move' in analysis.reason_exhaustion_components
    assert 'near_trade_extreme' in analysis.reason_exhaustion_components
    assert 'momentum_deceleration' in analysis.reason_exhaustion_components


def test_move_exhaustion_penalizes_extended_sell_near_low():
    analysis = MoveExhaustionAnalyzer().analyze(
        candle=candle(
            open_price=103.0,
            high=103.5,
            low=100.0,
            close=100.01,
        ),
        signal=signal(
            action='SELL',
            session_move_percent=-3.0,
            snapshot_momentum_percent=-0.02,
            close_position_percent=0.5,
            atr_percent=0.4,
        ),
        close_quality=99.5,
    )

    assert analysis.late_entry_risk > 40
    assert analysis.exhaustion_penalty > 7
    assert analysis.distance_to_recent_low_percent < 0.02
    assert analysis.late_entry_severity == 'HIGH'
    assert analysis.momentum_deceleration_detected is True


def test_extreme_decelerating_late_entry_is_penalized_not_rejected():
    analysis = MoveExhaustionAnalyzer().analyze(
        candle=candle(high=106.001, close=106.0, low=104.0),
        signal=signal(
            session_move_percent=5.5,
            snapshot_momentum_percent=0.01,
            close_position_percent=99.8,
            atr_percent=0.8,
        ),
        close_quality=10.0,
    )

    assert analysis.late_entry_risk >= 85.0
    assert analysis.late_entry_severity == 'SEVERE'
    assert analysis.exhaustion_penalty <= 18.0


def test_directional_score_subtracts_exhaustion_without_cap():
    breakdown = directional_score_breakdown(
        snapshot=snapshot(),
        candle=candle(high=103.01, low=101.0, close=103.0),
        signal=signal(
            session_move_percent=3.0,
            snapshot_momentum_percent=0.02,
            close_position_percent=99.5,
            atr_percent=0.4,
        ),
        close_quality=99.5,
    )

    assert breakdown.exhaustion.exhaustion_penalty > 0
    assert breakdown.final_score == pytest.approx(
        breakdown.base_score - breakdown.exhaustion.exhaustion_penalty
    )


def test_trade_candidate_exposes_penalty_metadata_without_cap_fields():
    candidate = build_trade_candidate(
        symbol='BTC',
        snapshot=snapshot(),
        candle=candle(high=103.01, low=101.0, close=103.0),
        signal=signal(
            session_move_percent=3.0,
            snapshot_momentum_percent=0.02,
            close_position_percent=99.5,
            atr_percent=0.4,
        ),
    )

    assert candidate.directional_score == pytest.approx(
        candidate.base_score - candidate.exhaustion_penalty
    )
    assert candidate.late_entry_severity == 'HIGH'
    assert candidate.late_entry_risk > 0
    assert candidate.entry_quality_metadata['move_extension_percent'] == 3.0
    assert (
        candidate.entry_quality_metadata[
            'momentum_deceleration_detected'
        ]
        is True
    )
    assert 'late_entry_risk=' in candidate.rank_reason
    assert 'late_entry_score_cap=' not in candidate.rank_reason
