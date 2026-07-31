from dataclasses import replace
from datetime import datetime, timezone

from app.execution.candidate_economics import (
    CandidateEconomics,
    EvaluatedTradeCandidate,
)
from app.execution.scoring.managed_outcome import (
    CandidateManagedOutcomeEvaluator,
    FrozenManagedOutcomeModel,
)
from app.execution.trade_candidate import TradeCandidate
from app.market.models import Candle, MarketSnapshot
from app.strategies.signals import Signal


def _evaluated(*, progress: float = 0.2) -> EvaluatedTradeCandidate:
    timestamp = datetime(2026, 7, 31, 14, 0, tzinfo=timezone.utc)
    candidate = TradeCandidate(
        symbol='AMD',
        snapshot=MarketSnapshot(
            symbol='AMD',
            bid=99.95,
            ask=100.05,
            last=100.0,
            timestamp=timestamp,
        ),
        candle=Candle(
            symbol='AMD',
            timeframe_seconds=60,
            open=100.3,
            high=100.4,
            low=99.8,
            close=100.0,
            volume=None,
            opened_at=timestamp,
            closed_at=timestamp,
        ),
        signal=Signal(
            action='SELL',
            setup_quality=0.8,
            reason='test',
            metadata={
                'session_move_percent': -0.6,
                'snapshot_momentum_percent': -0.3,
                'trend_strength_percent': -0.2,
                'breakdown_percent': 0.15,
                'close_position_percent': 10.0,
                'atr_percent': 0.2,
                'regime_noise_ratio': 0.2,
            },
        ),
        rank_reason='test',
        base_score=120.0,
        directional_score=125.0,
        tp_feasibility_score=60.0,
        touch_probability=0.6,
        direction_probability=0.65,
        tp_probability=0.39,
        sl_probability=0.21,
        neither_probability=0.4,
        direction_break_even_probability=0.57,
        direction_edge=0.08,
        outcome_probability_model_version='outcome_probability_v2',
        outcome_probability_metadata={
            'direction_model_segment': 'EQUITY_US_SELL',
            'activity_features': {
                'asset_class': 'EQUITY_US',
                'session_progress': progress,
                'tp_to_atr_ratio': 6.0,
                'tp_to_momentum_ratio': 4.0,
                'cost_to_tp_ratio': 0.3,
                'movement_consumed_to_tp_ratio': 0.5,
                'tp_feasibility_score': 60.0,
                'entry_freshness_score': 90.0,
            },
            'direction_features': {
                'session_progress': progress,
                'aligned_session_move': 0.6,
                'aligned_snapshot_momentum': 0.3,
                'aligned_symbol_relative_strength': 0.2,
                'aligned_benchmark_momentum': 0.1,
                'breakout_percent': 0.15,
                'close_quality': 90.0,
                'atr_percent': 0.2,
                'regime_noise_ratio': 0.2,
            },
        },
    )
    return EvaluatedTradeCandidate(
        candidate=candidate,
        economics=CandidateEconomics(
            position_value=100.0,
            expected_gross_profit=1.2,
            expected_net_profit=0.85,
            expected_net_profit_percent=0.85,
            estimated_total_cost=0.35,
            estimated_total_cost_percent=0.35,
            min_expected_net_profit_percent=0.10,
            required_min_expected_net_profit_amount=0.10,
            effective_stop_loss_percent=0.70,
            effective_take_profit_percent=1.20,
            reward_to_risk_ratio=1.7142857142857142,
            net_reward_to_risk_ratio=0.8095238095238095,
        ),
    )


def test_frozen_model_contract_and_explicit_crypto_transfer():
    model = FrozenManagedOutcomeModel.load()

    assert model.version == 'managed_outcome_v1'
    assert model.feature_contract_version == 'managed_outcome_features_v1'
    assert model.segments['CRYPTO_SELL'].training_status == 'transferred'
    assert model.segments['CRYPTO_SELL'].source_segment == 'EQUITY_US_SELL'


def test_evaluator_attaches_managed_probabilities_edge_and_time_contribution():
    result = CandidateManagedOutcomeEvaluator().evaluate(
        evaluated_candidate=_evaluated(),
    )
    candidate = result.candidate

    assert 0.0 <= candidate.managed_protection_probability <= 1.0
    assert 0.0 <= candidate.managed_positive_probability <= 1.0
    assert candidate.managed_expected_net_return_percent is not None
    assert candidate.managed_edge is not None
    assert candidate.managed_outcome_model_version == 'managed_outcome_v1'
    assert candidate.managed_outcome_metadata['segment'] == 'EQUITY_US_SELL'
    assert candidate.managed_outcome_metadata['time_adjustment'][
        'hard_time_route'
    ] is False


def test_session_phase_changes_score_continuously_without_route_change():
    evaluator = CandidateManagedOutcomeEvaluator()
    early = evaluator.evaluate(evaluated_candidate=_evaluated(progress=0.15))
    later = evaluator.evaluate(evaluated_candidate=_evaluated(progress=0.55))

    assert early.candidate.managed_outcome_metadata['segment'] == (
        later.candidate.managed_outcome_metadata['segment']
    )
    assert early.candidate.managed_protection_probability != (
        later.candidate.managed_protection_probability
    )


def test_expected_return_is_bounded_by_hard_trade_economics():
    item = _evaluated()
    item = replace(
        item,
        economics=replace(
            item.economics,
            expected_net_profit_percent=0.02,
        ),
    )
    result = CandidateManagedOutcomeEvaluator().evaluate(
        evaluated_candidate=item,
    )

    assert result.candidate.managed_expected_net_return_percent <= 0.02
