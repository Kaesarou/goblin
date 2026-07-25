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
    FrozenLogisticModel,
    FrozenOutcomeProbabilityModel,
)
from app.execution.scoring.outcome_probability import (
    CandidateOutcomeProbabilityEvaluator,
    OutcomeProbabilityEstimator,
)
from app.execution.scoring.pr5e_model_contract import (
    OUTCOME_PROBABILITY_FEATURE_CONTRACT_VERSION,
    OUTCOME_PROBABILITY_MODEL_VERSION,
)
from app.execution.scoring.tp_feasibility import TpFeasibilityAnalysis
from app.execution.trade_candidate import TradeCandidate
from app.instruments.equity_us_config import EquityUsConfig
from app.instruments.models import AssetClass, RiskProfile
from app.market.models import Candle, MarketSnapshot
from app.market.timeframes import TimeframeDirection, TimeframeMaturity
from app.strategies.signals import Signal

NOW = datetime(2026, 7, 24, 15, 0, tzinfo=UTC)
SESSION_KEY = (
    'us:2026-07-24T13:30:00+00:00:'
    '2026-07-24T20:00:00+00:00'
)


def _candidate(*, side='BUY') -> TradeCandidate:
    return TradeCandidate(
        symbol='AMD',
        snapshot=MarketSnapshot('AMD', 100.0, 100.05, 100.02, NOW),
        candle=Candle('AMD', 60, 99.5, 100.2, 99.4, 100.0, None, NOW, NOW),
        signal=Signal(
            side,
            0.8,
            'test',
            metadata={
                'market_regime': 'TRENDING',
                'trend_strength_percent': 0.25,
                'close_position_percent': 88.0,
            },
        ),
        rank_reason='test',
        session_key=SESSION_KEY,
        base_score=126.0,
        directional_score=130.0,
        market_context_components={
            'relative_strength_raw': 0.18,
            'sector': 0.25,
            'benchmark_momentum': 0.04,
        },
    )


def _economics() -> CandidateEconomics:
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


def _feasibility() -> TpFeasibilityAnalysis:
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


def _evaluated(*, side='BUY') -> EvaluatedTradeCandidate:
    return EvaluatedTradeCandidate(
        candidate=_candidate(side=side),
        economics=_economics(),
        tp_feasibility=_feasibility(),
        entry_decision=EntryDecision(
            action=EntryAction.READY_FOR_SELECTION,
            reason='entry_conditions_satisfied',
            diagnostics={'extension_to_tp_ratio': 0.30},
        ),
    )


def _risk_profile(asset_class=AssetClass.EQUITY_US) -> RiskProfile:
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


def _constant_model(
    *,
    touch_probability: float,
    direction_probability: float,
) -> FrozenOutcomeProbabilityModel:
    empty = {
        'numeric_terms': (),
        'missing_indicator_terms': (),
        'categorical_terms': (),
    }
    return FrozenOutcomeProbabilityModel(
        version=OUTCOME_PROBABILITY_MODEL_VERSION,
        feature_contract_version=(
            OUTCOME_PROBABILITY_FEATURE_CONTRACT_VERSION
        ),
        training_asset_classes=(
            AssetClass.EQUITY_EU.value,
            AssetClass.EQUITY_US.value,
        ),
        activity=FrozenLogisticModel(
            intercept=math.log(
                touch_probability / (1.0 - touch_probability)
            ),
            **empty,
        ),
        direction=FrozenLogisticModel(
            intercept=math.log(
                direction_probability / (1.0 - direction_probability)
            ),
            **empty,
        ),
        provenance={},
    )


def test_two_stage_probability_contract_and_monotone_score():
    estimate = OutcomeProbabilityEstimator(
        _constant_model(
            touch_probability=0.40,
            direction_probability=0.35,
        )
    ).estimate(
        evaluated_candidate=_evaluated(),
        risk_profile=_risk_profile(),
    )

    assert estimate.touch_probability == pytest.approx(0.40)
    assert estimate.direction_probability == pytest.approx(0.35)
    assert estimate.tp_probability == pytest.approx(0.14)
    assert estimate.sl_probability == pytest.approx(0.26)
    assert estimate.neither_probability == pytest.approx(0.60)
    assert (
        estimate.tp_probability
        + estimate.sl_probability
        + estimate.neither_probability
        == pytest.approx(1.0)
    )
    assert estimate.probability_score == 28.0


def test_direction_edge_uses_conditional_break_even_probability():
    estimate = OutcomeProbabilityEstimator(
        _constant_model(
            touch_probability=0.40,
            direction_probability=0.35,
        )
    ).estimate(
        evaluated_candidate=_evaluated(),
        risk_profile=_risk_profile(),
    )

    expected_break_even = (0.40 + 0.16) / (0.44 + 0.40 + 0.16)
    assert estimate.direction_break_even_probability == pytest.approx(
        expected_break_even
    )
    assert estimate.direction_edge == pytest.approx(
        0.35 - expected_break_even
    )


def test_default_frozen_model_exposes_reproducible_provenance():
    estimator = OutcomeProbabilityEstimator()
    estimate = estimator.estimate(
        evaluated_candidate=_evaluated(),
        risk_profile=_risk_profile(),
    )

    assert estimator.model.provenance['training_rows'] == 1958
    assert estimator.model.provenance['dataset_sha256'] == (
        '4608e799936ac007292dcb5cfc1894880893e3f9544d0b7a33096d23b5bb8290'
    )
    assert estimator.model.provenance['challenger_replay'][
        'selected'
    ] == 286
    assert estimator.model.provenance['challenger_replay'][
        'selection_order'
    ] == 'direction_edge_top_n_then_probability_gates_no_backfill'
    assert estimate.in_training_domain is True
    assert estimate.probability_score == pytest.approx(
        200.0 * estimate.tp_probability,
        abs=0.0002,
    )


def test_evaluator_persists_probabilities_without_changing_raw_evidence():
    original = _evaluated()
    updated = CandidateOutcomeProbabilityEvaluator().evaluate(
        evaluated_candidate=original,
        risk_profile=_risk_profile(),
    )

    assert updated.candidate.directional_score == (
        original.candidate.directional_score
    )
    assert updated.candidate.market_context_components == (
        original.candidate.market_context_components
    )
    assert updated.outcome_probability is not None
    assert updated.candidate.tp_probability is not None
    assert updated.candidate.outcome_probability_model_version == (
        OUTCOME_PROBABILITY_MODEL_VERSION
    )
    assert 'p_direction=' in updated.candidate.rank_reason


def test_asset_class_without_training_cohort_is_scored_without_rejection():
    estimate = OutcomeProbabilityEstimator().estimate(
        evaluated_candidate=_evaluated(),
        risk_profile=_risk_profile(AssetClass.CRYPTO),
    )

    assert estimate.in_training_domain is False
    assert estimate.tp_probability is not None
    assert estimate.probability_score == pytest.approx(
        200.0 * estimate.tp_probability,
        abs=0.0002,
    )


def test_model_cannot_change_the_training_domain_silently():
    model = replace(
        _constant_model(
            touch_probability=0.40,
            direction_probability=0.35,
        ),
        training_asset_classes=(
            AssetClass.EQUITY_EU.value,
            AssetClass.EQUITY_US.value,
            AssetClass.CRYPTO.value,
        ),
    )

    with pytest.raises(
        RuntimeError,
        match='training-domain contract does not match code',
    ):
        OutcomeProbabilityEstimator(model)


def test_runtime_feature_contract_reproduces_a_frozen_cohort_vector():
    candidate = replace(
        _candidate(),
        symbol='DE',
        snapshot=MarketSnapshot(
            'DE',
            100.0,
            100.05,
            100.02,
            datetime(2026, 7, 23, 15, 35, tzinfo=UTC),
        ),
        session_key=(
            'EQUITY_US:2026-07-23T15:30:00+02:00:'
            '2026-07-23T22:00:00+02:00'
        ),
        base_score=107.4146,
        directional_score=107.4146,
        market_context_components={
            'relative_strength_raw': 10.0495,
            'sector': 0.0,
            'benchmark_momentum': 0.3562,
        },
        multi_timeframe_context=SimpleNamespace(
            maturity_by_timeframe={
                'm5': TimeframeMaturity.READY,
            },
            features_by_timeframe={
                'm5': SimpleNamespace(
                    direction=TimeframeDirection.UP,
                ),
            },
        ),
    )
    feasibility = replace(
        _feasibility(),
        effective_take_profit_percent=1.2,
        effective_stop_loss_percent=0.7,
        tp_to_atr_ratio=12.5392,
        tp_to_snapshot_momentum_ratio=5.0804,
        cost_to_tp_ratio=0.3209,
        movement_consumed_to_tp_ratio=0.6223,
        entry_freshness_score=91.85,
        feasibility_score=49.4746,
        score_contribution=-0.1576,
        component_scores={
            'tp_vs_atr': 0.0,
            'tp_vs_momentum': 76.884,
            'cost_vs_tp': 75.4553,
            'entry_freshness': 91.85,
        },
    )
    economics = replace(
        _economics(),
        expected_net_profit_percent=0.814917289788423,
        estimated_total_cost_percent=0.385082710211577,
        effective_take_profit_percent=1.2,
        effective_stop_loss_percent=0.7,
        cost_to_tp_ratio=0.3209,
        reward_to_risk_ratio=1.7142857142857144,
        net_reward_to_risk_ratio=0.7510185925177305,
    )
    evaluated = EvaluatedTradeCandidate(
        candidate=candidate,
        economics=economics,
        tp_feasibility=feasibility,
        entry_decision=EntryDecision(
            action=EntryAction.READY_FOR_SELECTION,
            reason='entry_conditions_satisfied',
            diagnostics={'extension_to_tp_ratio': 0.0641},
        ),
    )

    estimate = OutcomeProbabilityEstimator().estimate(
        evaluated_candidate=evaluated,
        risk_profile=EquityUsConfig().risk,
    )

    assert estimate.touch_probability == pytest.approx(
        0.296985776,
        abs=1e-9,
    )
    assert estimate.direction_probability == pytest.approx(
        0.320672264,
        abs=1e-9,
    )
    assert estimate.tp_probability == pytest.approx(
        0.095235101,
        abs=1e-9,
    )
    assert estimate.probability_score == pytest.approx(19.047)
