import math
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.execution.candidate_economics import (
    CandidateEconomics,
    EvaluatedTradeCandidate,
)
from app.execution.candidate_readiness import CandidateReadiness
from app.execution.entry_decision import EntryAction, EntryDecision
from app.execution.scoring.frozen_logistic import (
    FrozenDirectionSegmentModel,
    FrozenLogisticModel,
    FrozenOutcomeProbabilityModel,
)
from app.execution.scoring.outcome_probability import (
    CandidateOutcomeProbabilityEvaluator,
    OutcomeProbabilityEstimator,
)
from app.execution.scoring.outcome_probability_model_contract import (
    OUTCOME_PROBABILITY_FEATURE_CONTRACT_VERSION,
    OUTCOME_PROBABILITY_MODEL_VERSION,
    SUPPORTED_DIRECTION_SEGMENTS,
    TRAINING_ASSET_CLASSES,
)
from app.execution.scoring.tp_feasibility import TpFeasibilityAnalysis
from app.execution.trade_candidate import TradeCandidate
from app.instruments.models import AssetClass, RiskProfile
from app.market.market_context import ContextAlignment, MarketRegime
from app.market.models import Candle, MarketSnapshot
from app.market.timeframes import TimeframeDirection, TimeframeMaturity
from app.strategies.signals import Signal

NOW = datetime(2026, 7, 28, 15, 0, tzinfo=UTC)
SESSION_KEY = (
    'us:2026-07-28T13:30:00+00:00:'
    '2026-07-28T20:00:00+00:00'
)


def _candidate(*, side='BUY', asset_class=AssetClass.EQUITY_US):
    context = SimpleNamespace(
        regime=MarketRegime.RISK_ON,
        alignment=ContextAlignment.ALIGNED,
        benchmark=SimpleNamespace(momentum_percent=0.04),
        symbol_relative_strength_percent=0.18,
    )
    direction = (
        TimeframeDirection.UP if side == 'BUY' else TimeframeDirection.DOWN
    )
    signed = 1.0 if side == 'BUY' else -1.0
    mtf = SimpleNamespace(
        maturity_by_timeframe={
            'm5': TimeframeMaturity.READY,
            'm15': TimeframeMaturity.READY,
            'm30': TimeframeMaturity.READY,
            'h1': TimeframeMaturity.PROVISIONAL,
        },
        features_by_timeframe={
            name: SimpleNamespace(
                direction=direction,
                return_sample_percent=0.25 * signed,
                velocity_percent_per_bar=0.05 * signed,
                acceleration_percent_per_bar=0.01 * signed,
            )
            for name in ('m5', 'm15', 'm30', 'h1')
        },
    )
    return TradeCandidate(
        symbol='AMD',
        snapshot=MarketSnapshot('AMD', 100.0, 100.05, 100.02, NOW),
        candle=Candle(
            'AMD', 60, 99.5, 100.2, 99.4, 100.0, None, NOW, NOW
        ),
        signal=Signal(
            side,
            0.8,
            'test',
            metadata={
                'session_move_percent': 0.40 * signed,
                'snapshot_momentum_percent': 0.30 * signed,
                'trend_strength_percent': 0.25 * signed,
                'breakout_percent': 0.20 if side == 'BUY' else None,
                'breakdown_percent': 0.20 if side == 'SELL' else None,
                'close_position_percent': 88.0 if side == 'BUY' else 12.0,
                'atr_percent': 0.40,
                'regime_noise_ratio': 0.50,
            },
        ),
        rank_reason='test',
        session_key=(
            SESSION_KEY
            if asset_class != AssetClass.CRYPTO
            else 'crypto:2026-07-28T00:00:00+00:00:'
            '2026-07-29T00:00:00+00:00'
        ),
        base_score=126.0,
        directional_score=130.0,
        market_context_components={
            'relative_strength_raw': 0.18,
            'sector': 0.25,
            'benchmark_momentum': 0.04,
        },
        market_context=context,
        multi_timeframe_context=mtf,
    )


def _economics():
    return CandidateEconomics(
        position_value=500.0,
        expected_gross_profit=3.0,
        expected_net_profit=2.2,
        expected_net_profit_percent=0.44,
        estimated_total_cost=0.8,
        estimated_total_cost_percent=0.16,
        min_expected_net_profit_percent=0.10,
        required_min_expected_net_profit_amount=0.5,
        effective_take_profit_percent=0.60,
        effective_stop_loss_percent=0.40,
        cost_to_tp_ratio=0.20,
        reward_to_risk_ratio=1.50,
        net_reward_to_risk_ratio=0.80,
    )


def _feasibility():
    return TpFeasibilityAnalysis(
        effective_take_profit_percent=0.60,
        effective_stop_loss_percent=0.40,
        atr_percent=0.40,
        snapshot_momentum_percent=0.30,
        directional_snapshot_momentum_percent=0.30,
        session_move_percent=0.40,
        directional_session_move_percent=0.40,
        tp_to_atr_ratio=1.5,
        tp_to_snapshot_momentum_ratio=3.0,
        required_net_move_percent=0.36,
        cost_to_tp_ratio=0.20,
        reward_to_risk_ratio=1.50,
        net_reward_to_risk_ratio=0.80,
        sl_tp_mode='fixed',
        sl_tp_source='us_intraday_fixed_v1',
        distance_to_trade_extreme_percent=0.20,
        movement_consumed_percent=0.40,
        movement_consumed_to_tp_ratio=0.67,
        entry_freshness_score=88.0,
        feasibility_score=80.0,
        component_scores={
            'tp_vs_atr': 80.0,
            'tp_vs_momentum': 80.0,
            'cost_vs_tp': 80.0,
            'entry_freshness': 88.0,
        },
        score_contribution=0.0,
        tp_feasibility_hard_rejection_reason=None,
        readiness=CandidateReadiness.TRADABLE_NOW,
        readiness_reason='entry_decision_required',
        hard_rejection_components=(),
        reason_components=('continuous_score',),
    )


def _evaluated(*, side='BUY', asset_class=AssetClass.EQUITY_US):
    return EvaluatedTradeCandidate(
        candidate=_candidate(side=side, asset_class=asset_class),
        economics=_economics(),
        tp_feasibility=_feasibility(),
        entry_decision=EntryDecision(
            action=EntryAction.READY_FOR_SELECTION,
            reason='entry_conditions_satisfied',
            diagnostics={'extension_to_tp_ratio': 0.30},
        ),
    )


def _risk_profile(asset_class=AssetClass.EQUITY_US):
    return RiskProfile(
        asset_class=asset_class,
        profile_key='us_intraday_fixed_v1',
        max_position_size_percent=1.0,
        stop_loss_percent=0.40,
        take_profit_percent=0.60,
        force_close_enabled=False,
        force_close_hour=21,
        force_close_minute=55,
        max_spread_percent=1.0,
        min_move_spread_ratio=0.0,
    )


def _constant_logistic(probability):
    return FrozenLogisticModel(
        intercept=math.log(probability / (1.0 - probability)),
        numeric_terms=(),
        missing_indicator_terms=(),
        categorical_terms=(),
    )


def _constant_model(*, touch=0.40, raw_by_segment=None, prior=0.20):
    raw_by_segment = raw_by_segment or {
        segment: 0.60 for segment in SUPPORTED_DIRECTION_SEGMENTS
    }
    segments = {
        segment: FrozenDirectionSegmentModel(
            segment=segment,
            feature_family='test',
            training_status=(
                'provisional_transfer'
                if segment.startswith('CRYPTO_')
                else 'trained'
            ),
            source_segment=(
                f'EQUITY_US_{segment.rsplit("_", 1)[1]}'
                if segment.startswith('CRYPTO_')
                else None
            ),
            training_rows=0 if segment.startswith('CRYPTO_') else 10,
            segment_prior=prior,
            model_weight=0.5,
            segment_prior_weight=0.5,
            model=_constant_logistic(raw_by_segment[segment]),
        )
        for segment in SUPPORTED_DIRECTION_SEGMENTS
    }
    return FrozenOutcomeProbabilityModel(
        version=OUTCOME_PROBABILITY_MODEL_VERSION,
        feature_contract_version=OUTCOME_PROBABILITY_FEATURE_CONTRACT_VERSION,
        training_asset_classes=TRAINING_ASSET_CLASSES,
        supported_segments=SUPPORTED_DIRECTION_SEGMENTS,
        activity=_constant_logistic(touch),
        direction_segments=segments,
        provenance={},
    )


def test_two_stage_probability_uses_shrunk_segment_direction_probability():
    estimate = OutcomeProbabilityEstimator(
        _constant_model(touch=0.40, prior=0.20)
    ).estimate(
        evaluated_candidate=_evaluated(),
        risk_profile=_risk_profile(),
    )

    assert estimate.direction_probability_raw == pytest.approx(0.60)
    assert estimate.direction_segment_prior == pytest.approx(0.20)
    assert estimate.direction_probability == pytest.approx(0.40)
    assert estimate.touch_probability == pytest.approx(0.40)
    assert estimate.tp_probability == pytest.approx(0.16)
    assert estimate.sl_probability == pytest.approx(0.24)
    assert estimate.neither_probability == pytest.approx(0.60)
    assert estimate.probability_score == pytest.approx(32.0)


def test_exact_market_and_side_segment_is_used():
    raw = {segment: 0.51 for segment in SUPPORTED_DIRECTION_SEGMENTS}
    raw['EQUITY_EU_SELL'] = 0.81
    estimate = OutcomeProbabilityEstimator(
        _constant_model(raw_by_segment=raw, prior=0.21)
    ).estimate(
        evaluated_candidate=_evaluated(
            side='SELL',
            asset_class=AssetClass.EQUITY_EU,
        ),
        risk_profile=_risk_profile(AssetClass.EQUITY_EU),
    )

    assert estimate.direction_model_segment == 'EQUITY_EU_SELL'
    assert estimate.direction_probability_raw == pytest.approx(0.81)
    assert estimate.direction_probability == pytest.approx(0.51)
    assert estimate.in_training_domain is True


def test_sell_features_are_aligned_to_favorable_direction():
    estimate = OutcomeProbabilityEstimator(_constant_model()).estimate(
        evaluated_candidate=_evaluated(side='SELL'),
        risk_profile=_risk_profile(),
    )

    assert estimate.direction_features['aligned_session_move'] == pytest.approx(
        0.40
    )
    assert estimate.direction_features[
        'aligned_snapshot_momentum'
    ] == pytest.approx(0.30)
    assert estimate.direction_features['aligned_m5_return'] == pytest.approx(
        0.25
    )
    assert estimate.direction_features['close_quality'] == pytest.approx(88.0)


def test_crypto_uses_explicit_provisional_segment_not_runtime_fallback():
    estimate = OutcomeProbabilityEstimator(_constant_model()).estimate(
        evaluated_candidate=_evaluated(
            side='BUY',
            asset_class=AssetClass.CRYPTO,
        ),
        risk_profile=_risk_profile(AssetClass.CRYPTO),
    )

    assert estimate.direction_model_segment == 'CRYPTO_BUY'
    assert estimate.direction_training_status == 'provisional_transfer'
    assert estimate.direction_source_segment == 'EQUITY_US_BUY'
    assert estimate.in_training_domain is False
    assert estimate.tp_probability is not None


def test_default_artifact_has_frozen_all_candidate_provenance():
    estimator = OutcomeProbabilityEstimator()

    assert estimator.model.artifact_sha256 == (
        '57cf61302346288ffdb76bc66f134437bd75a9e4064f30569f2a54b47606b55e'
    )
    assert estimator.model.provenance['training_rows'] == 3527
    assert estimator.model.provenance['decisive_training_rows'] == 1547
    assert estimator.model.provenance['direction_margin'] == 0.05
    assert set(estimator.model.direction_segments) == set(
        SUPPORTED_DIRECTION_SEGMENTS
    )
    assert estimator.model.direction_segments[
        'CRYPTO_SELL'
    ].training_status == 'provisional_transfer'


def test_evaluator_persists_segment_metadata_without_changing_raw_evidence():
    original = _evaluated()
    updated = CandidateOutcomeProbabilityEvaluator().evaluate(
        evaluated_candidate=original,
        risk_profile=_risk_profile(),
    )

    assert updated.candidate.directional_score == (
        original.candidate.directional_score
    )
    assert updated.outcome_probability is not None
    assert updated.candidate.outcome_probability_metadata[
        'direction_model_segment'
    ] == 'EQUITY_US_BUY'
    assert 'p_direction_raw=' in updated.candidate.rank_reason


def test_model_cannot_change_supported_segments_silently():
    model = replace(
        _constant_model(),
        supported_segments=SUPPORTED_DIRECTION_SEGMENTS[:-1],
    )
    with pytest.raises(
        RuntimeError,
        match='segment contract does not match code',
    ):
        OutcomeProbabilityEstimator(model)
