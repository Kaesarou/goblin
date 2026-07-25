from datetime import datetime, timezone

import pytest

from app.execution.candidate_ranking import (
    build_trade_candidate,
    rank_trade_candidates,
)
from app.execution.scoring.buy_signal_scorer import BuySignalScorer
from app.execution.scoring.sell_signal_scorer import SellSignalScorer
from app.execution.scoring.signal_scorer import directional_score_breakdown
from app.market.models import Candle, MarketSnapshot
from app.strategies.signals import Signal
from app.utils.commons import spread_percent


def snapshot(symbol: str, bid: float, ask: float, last: float) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        bid=bid,
        ask=ask,
        last=last,
        timestamp=datetime(2026, 6, 26, 15, 30, tzinfo=timezone.utc),
    )


def candle(
    symbol: str,
    open: float,
    high: float,
    low: float,
    close: float,
) -> Candle:
    opened_at = datetime(2026, 6, 26, 15, 29, tzinfo=timezone.utc)
    closed_at = datetime(2026, 6, 26, 15, 30, tzinfo=timezone.utc)
    return Candle(
        symbol=symbol,
        timeframe_seconds=60,
        open=open,
        high=high,
        low=low,
        close=close,
        volume=None,
        opened_at=opened_at,
        closed_at=closed_at,
    )


def buy_signal(
    session_move_percent: float,
    trend_strength_percent: float,
    breakout_percent: float,
    close_position_percent: float = 90.0,
) -> Signal:
    return Signal(
        action='BUY',
        setup_quality=0.8,
        reason='intraday_bullish_breakout',
        metadata={
            'session_move_percent': session_move_percent,
            'trend_strength_percent': trend_strength_percent,
            'breakout_percent': breakout_percent,
            'breakdown_percent': 0.0,
            'candle_range_percent': 0.4,
            'close_position_percent': close_position_percent,
        },
    )


def sell_signal(
    session_move_percent: float,
    trend_strength_percent: float,
    breakdown_percent: float,
    close_position_percent: float = 10.0,
) -> Signal:
    return Signal(
        action='SELL',
        setup_quality=0.8,
        reason='intraday_bearish_breakdown',
        metadata={
            'session_move_percent': session_move_percent,
            'trend_strength_percent': trend_strength_percent,
            'breakout_percent': 0.0,
            'breakdown_percent': breakdown_percent,
            'candle_range_percent': 0.4,
            'close_position_percent': close_position_percent,
            'atr_percent': 0.5,
            'snapshot_momentum_percent': -0.40,
            'snapshot_momentum_required_percent': 0.25,
            'snapshot_breakdown_percent': abs(breakdown_percent),
            'snapshot_close_position_percent': close_position_percent,
            'regime_noise_ratio': 0.8,
        },
    )


def base_score_without_exhaustion(
    market_snapshot: MarketSnapshot,
    signal: Signal,
) -> float:
    metadata = signal.metadata or {}
    session_move_percent = abs(
        float(metadata.get('session_move_percent', 0.0) or 0.0)
    )
    trend_strength_percent = abs(
        float(metadata.get('trend_strength_percent', 0.0) or 0.0)
    )
    breakout_percent = abs(
        float(metadata.get('breakout_percent', 0.0) or 0.0)
    )
    breakdown_percent = abs(
        float(metadata.get('breakdown_percent', 0.0) or 0.0)
    )
    impulse_percent = max(breakout_percent, breakdown_percent)
    candle_range_percent = float(
        metadata.get('candle_range_percent', 0.0) or 0.0
    )
    close_position_percent = float(
        metadata.get('close_position_percent', 0.0) or 0.0
    )
    close_quality = (
        100 - close_position_percent
        if signal.action == 'SELL'
        else close_position_percent
    )
    score = 0.0
    score += signal.setup_quality * 100
    score += min(session_move_percent * 15, 30)
    score += min(trend_strength_percent * 80, 25)
    score += min(impulse_percent * 40, 20)
    score += min(candle_range_percent * 20, 10)
    score += close_quality * 0.15
    score -= spread_percent(market_snapshot) * 120
    return score


def close_quality(signal: Signal) -> float:
    metadata = signal.metadata or {}
    close_position_percent = float(
        metadata.get('close_position_percent', 0.0) or 0.0
    )
    if signal.action == 'SELL':
        return 100 - close_position_percent
    return close_position_percent


def assert_penalized_score(
    *,
    market_snapshot: MarketSnapshot,
    closed_candle: Candle,
    signal: Signal,
    score: float,
):
    breakdown = directional_score_breakdown(
        snapshot=market_snapshot,
        candle=closed_candle,
        signal=signal,
        close_quality=close_quality(signal),
    )
    assert breakdown.base_score == pytest.approx(
        base_score_without_exhaustion(market_snapshot, signal)
    )
    assert score == pytest.approx(breakdown.final_score)
    assert score == pytest.approx(
        breakdown.base_score - breakdown.exhaustion.exhaustion_penalty
    )


def test_buy_signal_scorer_applies_move_exhaustion_penalty():
    market_snapshot = snapshot('MSFT', bid=365.9, ask=366.0, last=365.95)
    closed_candle = candle(
        'MSFT',
        open=362.0,
        high=366.0,
        low=361.9,
        close=365.95,
    )
    signal = buy_signal(
        session_move_percent=1.8,
        trend_strength_percent=0.35,
        breakout_percent=0.25,
    )
    score = BuySignalScorer().score(
        snapshot=market_snapshot,
        candle=closed_candle,
        signal=signal,
    )
    assert_penalized_score(
        market_snapshot=market_snapshot,
        closed_candle=closed_candle,
        signal=signal,
        score=score,
    )


def test_sell_signal_scorer_keeps_clean_sell_on_directional_path():
    market_snapshot = snapshot('AIR.PA', bid=190.9, ask=191.1, last=191.0)
    closed_candle = candle(
        'AIR.PA',
        open=191.5,
        high=191.6,
        low=190.4,
        close=190.5,
    )
    signal = sell_signal(
        session_move_percent=-1.4,
        trend_strength_percent=-0.25,
        breakdown_percent=-0.2,
        close_position_percent=12.0,
    )
    score = SellSignalScorer().score(
        snapshot=market_snapshot,
        candle=closed_candle,
        signal=signal,
    )
    assert_penalized_score(
        market_snapshot=market_snapshot,
        closed_candle=closed_candle,
        signal=signal,
        score=score,
    )


def test_build_trade_candidate_scores_buy_with_penalized_score():
    candidate = build_trade_candidate(
        symbol='MSFT',
        snapshot=snapshot('MSFT', bid=365.9, ask=366.0, last=365.95),
        candle=candle(
            'MSFT',
            open=362.0,
            high=366.0,
            low=361.9,
            close=365.95,
        ),
        signal=buy_signal(
            session_move_percent=1.8,
            trend_strength_percent=0.35,
            breakout_percent=0.25,
            close_position_percent=90.0,
        ),
    )
    assert candidate.base_score == round(
        base_score_without_exhaustion(candidate.snapshot, candidate.signal),
        4,
    )
    assert candidate.directional_score == round(
        candidate.base_score - candidate.exhaustion_penalty,
        4,
    )
    assert candidate.exhaustion_penalty >= 0
    assert candidate.late_entry_risk >= 0
    assert candidate.sell_score_metadata == {}
    assert 'base_score=' in candidate.rank_reason
    assert 'exhaustion_penalty=' in candidate.rank_reason
    assert 'late_entry_risk=' in candidate.rank_reason


def test_build_trade_candidate_scores_clean_sell_with_penalty_metadata():
    candidate = build_trade_candidate(
        symbol='AIR.PA',
        snapshot=snapshot('AIR.PA', bid=190.9, ask=191.1, last=191.0),
        candle=candle(
            'AIR.PA',
            open=191.5,
            high=191.6,
            low=190.4,
            close=190.5,
        ),
        signal=sell_signal(
            session_move_percent=-1.4,
            trend_strength_percent=-0.25,
            breakdown_percent=-0.2,
            close_position_percent=10.0,
        ),
    )
    assert candidate.base_score == round(
        base_score_without_exhaustion(candidate.snapshot, candidate.signal),
        4,
    )
    assert candidate.directional_score == round(
        candidate.base_score - candidate.exhaustion_penalty,
        4,
    )
    assert candidate.sell_specific_penalty == 0.0
    assert candidate.sell_score_metadata['sell_specific_penalty'] == 0.0
    assert 'sell_specific_penalty=0.0' in candidate.rank_reason
    assert 'short_snapshot_momentum=' in candidate.rank_reason


def test_hold_or_unknown_action_uses_penalized_scoring_path():
    candidate = build_trade_candidate(
        symbol='UNKNOWN',
        snapshot=snapshot('UNKNOWN', bid=99.9, ask=100.1, last=100.0),
        candle=candle(
            'UNKNOWN',
            open=99.0,
            high=101.0,
            low=98.5,
            close=100.0,
        ),
        signal=Signal(
            action='HOLD',
            setup_quality=0.0,
            reason='test_hold',
            metadata={
                'session_move_percent': 1.0,
                'trend_strength_percent': 0.2,
                'breakout_percent': 0.1,
                'breakdown_percent': 0.0,
                'candle_range_percent': 0.4,
                'close_position_percent': 75.0,
            },
        ),
    )
    assert candidate.base_score == round(
        base_score_without_exhaustion(candidate.snapshot, candidate.signal),
        4,
    )
    assert candidate.directional_score == round(
        candidate.base_score - candidate.exhaustion_penalty,
        4,
    )


def test_candidate_ranking_puts_stronger_opportunity_first():
    weaker = build_trade_candidate(
        symbol='AIR.PA',
        snapshot=snapshot('AIR.PA', bid=190.9, ask=191.1, last=191.0),
        candle=candle(
            'AIR.PA',
            open=190.5,
            high=191.1,
            low=190.4,
            close=191.0,
        ),
        signal=buy_signal(
            session_move_percent=0.25,
            trend_strength_percent=0.05,
            breakout_percent=0.05,
        ),
    )
    stronger = build_trade_candidate(
        symbol='MSFT',
        snapshot=snapshot('MSFT', bid=365.9, ask=366.0, last=365.95),
        candle=candle(
            'MSFT',
            open=362.0,
            high=366.0,
            low=361.9,
            close=365.95,
        ),
        signal=buy_signal(
            session_move_percent=1.8,
            trend_strength_percent=0.35,
            breakout_percent=0.25,
        ),
    )
    ranked = rank_trade_candidates([weaker, stronger])
    assert ranked[0].symbol == 'MSFT'
    assert ranked[1].symbol == 'AIR.PA'
    assert ranked[0].directional_score > ranked[1].directional_score


def test_candidate_ranking_penalizes_wide_spread():
    tight_spread = build_trade_candidate(
        symbol='MSFT',
        snapshot=snapshot('MSFT', bid=365.9, ask=366.0, last=365.95),
        candle=candle(
            'MSFT',
            open=362.0,
            high=366.0,
            low=361.9,
            close=365.95,
        ),
        signal=buy_signal(
            session_move_percent=1.0,
            trend_strength_percent=0.2,
            breakout_percent=0.2,
        ),
    )
    wide_spread = build_trade_candidate(
        symbol='WIDE',
        snapshot=snapshot('WIDE', bid=360.0, ask=370.0, last=365.0),
        candle=candle(
            'WIDE',
            open=362.0,
            high=366.0,
            low=361.9,
            close=365.95,
        ),
        signal=buy_signal(
            session_move_percent=1.0,
            trend_strength_percent=0.2,
            breakout_percent=0.2,
        ),
    )
    assert (
        tight_spread.directional_score
        > wide_spread.directional_score
    )
