from datetime import datetime, timezone

from app.execution.candidate_economics import (
    CandidateEconomics,
    EvaluatedTradeCandidate,
)
from app.execution.scoring.tp_feasibility import (
    CandidateTpFeasibilityEvaluator,
    TpFeasibilityAnalyzer,
)
from app.execution.trade_candidate import TradeCandidate
from app.instruments.models import AssetClass, RiskProfile, TpFeasibilityConfig
from app.market.models import Candle, MarketSnapshot
from app.strategies.signals import Signal


TIMESTAMP = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)


def candidate(*, side='BUY', score=150.0, metadata=None) -> TradeCandidate:
    return TradeCandidate(
        symbol='TEST',
        snapshot=MarketSnapshot('TEST', 99.9, 100.1, 100.0, TIMESTAMP),
        candle=Candle(
            'TEST', 60, 99.0, 101.0, 98.5, 100.0,
            None, TIMESTAMP, TIMESTAMP,
        ),
        signal=Signal(side, 0.8, 'test', metadata=metadata or {}),
        rank_reason='test',
        session_key='session',
        base_score=score,
        directional_score=score,
        entry_quality_metadata={
            'distance_to_recent_high_percent': 1.0,
            'distance_to_recent_low_percent': 1.0,
        },
    )


def economics(cost_percent=0.15) -> CandidateEconomics:
    return CandidateEconomics(
        position_value=1000.0,
        expected_gross_profit=10.0,
        expected_net_profit=10.0 - cost_percent * 10,
        expected_net_profit_percent=1.0 - cost_percent,
        estimated_total_cost=cost_percent * 10,
        estimated_total_cost_percent=cost_percent,
        min_expected_net_profit_percent=0.10,
        required_min_expected_net_profit_amount=1.0,
    )


def evaluated(*, side='BUY', score=150.0, metadata=None, cost_percent=0.15):
    return EvaluatedTradeCandidate(
        candidate=candidate(side=side, score=score, metadata=metadata),
        economics=economics(cost_percent),
    )


def risk_profile(tp=1.0, sl=0.8) -> RiskProfile:
    return RiskProfile(
        asset_class=AssetClass.EQUITY_US,
        profile_key='us_test_fixed_v1',
        max_position_size_percent=1.0,
        stop_loss_percent=sl,
        take_profit_percent=tp,
        force_close_enabled=False,
        force_close_hour=21,
        force_close_minute=55,
        max_spread_percent=1.0,
        min_move_spread_ratio=0.0,
        tp_feasibility=TpFeasibilityConfig(),
    )


def test_easy_tp_has_high_feasibility_and_positive_contribution():
    analysis = TpFeasibilityAnalyzer().analyze(
        evaluated_candidate=evaluated(metadata={
            'atr_percent': 0.8,
            'snapshot_momentum_percent': 0.4,
            'session_move_percent': 0.3,
        }),
        risk_profile=risk_profile(tp=1.0),
    )
    assert analysis.feasibility_score >= 75.0
    assert analysis.score_contribution > 0
    assert analysis.movement_consumed_to_tp_ratio == 0.3
    assert analysis.entry_freshness_score == 100.0


def test_far_tp_reduces_score_without_hidden_cap_or_veto():
    analysis = TpFeasibilityAnalyzer().analyze(
        evaluated_candidate=evaluated(metadata={
            'atr_percent': 0.25,
            'snapshot_momentum_percent': 0.15,
            'session_move_percent': 3.2,
        }),
        risk_profile=risk_profile(tp=1.6),
    )
    assert analysis.component_scores['tp_vs_atr'] == 0.0
    assert analysis.component_scores['entry_freshness'] == 0.0
    assert analysis.feasibility_score < 50.0
    assert -15.0 <= analysis.score_contribution < 0
    assert analysis.tp_feasibility_hard_rejection_reason is None


def test_opposite_snapshot_momentum_is_a_continuous_weak_component():
    analysis = TpFeasibilityAnalyzer().analyze(
        evaluated_candidate=evaluated(metadata={
            'atr_percent': 0.8,
            'snapshot_momentum_percent': -0.2,
            'session_move_percent': 0.3,
        }),
        risk_profile=risk_profile(),
    )
    assert analysis.component_scores['tp_vs_momentum'] == 0.0
    assert analysis.tp_feasibility_hard_rejection_reason is None


def test_cost_equal_to_tp_is_the_only_feasibility_hard_rejection():
    metadata = {
        'atr_percent': 0.8,
        'snapshot_momentum_percent': 0.4,
        'session_move_percent': 0.3,
    }
    soft = TpFeasibilityAnalyzer().analyze(
        evaluated_candidate=evaluated(metadata=metadata, cost_percent=0.9),
        risk_profile=risk_profile(tp=1.0),
    )
    hard = TpFeasibilityAnalyzer().analyze(
        evaluated_candidate=evaluated(metadata=metadata, cost_percent=1.0),
        risk_profile=risk_profile(tp=1.0),
    )
    assert soft.tp_feasibility_hard_rejection_reason is None
    assert soft.component_scores['cost_vs_tp'] < 25.0
    assert hard.tp_feasibility_hard_rejection_reason == (
        'candidate_selection_tp_feasibility_cost_to_tp_absurd'
    )


def test_missing_data_uses_explicit_prudent_score():
    analysis = TpFeasibilityAnalyzer().analyze(
        evaluated_candidate=evaluated(metadata={}),
        risk_profile=risk_profile(),
    )
    assert analysis.component_scores['tp_vs_atr'] == 45.0
    assert analysis.component_scores['tp_vs_momentum'] == 45.0
    assert analysis.component_scores['entry_freshness'] == 45.0
    assert 'missing_atr' in analysis.reason_components


def test_sell_uses_directional_momentum_and_freshness():
    good = TpFeasibilityAnalyzer().analyze(
        evaluated_candidate=evaluated(side='SELL', metadata={
            'atr_percent': 0.8,
            'snapshot_momentum_percent': -0.4,
            'session_move_percent': -0.3,
        }),
        risk_profile=risk_profile(),
    )
    bad = TpFeasibilityAnalyzer().analyze(
        evaluated_candidate=evaluated(side='SELL', metadata={
            'atr_percent': 0.8,
            'snapshot_momentum_percent': 0.4,
            'session_move_percent': 0.3,
        }),
        risk_profile=risk_profile(),
    )
    assert good.component_scores['tp_vs_momentum'] > bad.component_scores['tp_vs_momentum']
    assert good.feasibility_score > bad.feasibility_score


def test_evaluator_persists_freshness_context_and_feasibility_evidence():
    result = CandidateTpFeasibilityEvaluator().evaluate(
        evaluated_candidate=evaluated(metadata={
            'atr_percent': 0.8,
            'snapshot_momentum_percent': 0.4,
            'session_move_percent': 0.3,
            'trend_strength_percent': 0.2,
            'close_position_percent': 90.0,
        }),
        risk_profile=risk_profile(),
    )
    assert result.candidate.tp_feasibility_score == result.tp_feasibility.feasibility_score
    assert result.candidate.tp_feasibility_contribution == result.tp_feasibility.score_contribution
    assert result.tp_feasibility.entry_freshness_score == 100.0
    assert result.candidate.tp_probability is None
    assert 'entry_freshness_score=' in result.candidate.rank_reason
