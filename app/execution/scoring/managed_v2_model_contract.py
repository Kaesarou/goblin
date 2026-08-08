from __future__ import annotations

from app.execution.scoring.managed_outcome_model_contract import (
    MANAGED_SELECTION_POLICY_VERSION,
)
from app.execution.strategy_segment import StrategySegment

MANAGED_V2_MODEL_VERSION = 'managed_v2_20260808'
MANAGED_V2_ARTIFACT_SCHEMA_VERSION = 1
MANAGED_V2_OPPORTUNITY_MODEL_VERSION = 'managed_v2_opportunity_20260808'
MANAGED_V2_PATH_MODEL_VERSION = 'managed_v2_path_20260808'
MANAGED_V2_ECONOMICS_MODEL_VERSION = 'managed_v2_economics_20260808'
MANAGED_V2_FEATURE_CONTRACT_VERSION = 'managed_v2_features_v1'
MANAGED_V2_LABEL_CONTRACT_VERSION = 'managed_v2_labels_v1'
MANAGED_V2_SELECTION_POLICY_VERSION = 'managed_v2_segment_first_v1'
MANAGED_V2_FLOOR_POLICY_VERSION = 'managed_v2_training_prior_floors_v1'
MANAGED_V2_DEPLOYMENT_STATUS = 'shadow_not_approved'
ACTIVE_EQUITY_SELECTION_POLICY_VERSION = MANAGED_SELECTION_POLICY_VERSION

MANAGED_V2_TRAINING_ASSET_CLASSES = ('EQUITY_EU', 'EQUITY_US')
MANAGED_V2_SUPPORTED_SEGMENTS = (
    StrategySegment.EQUITY_EU_BUY,
    StrategySegment.EQUITY_EU_SELL,
    StrategySegment.EQUITY_US_BUY,
    StrategySegment.EQUITY_US_SELL,
)

_COMMON_ECONOMICS_FEATURES = (
    'opportunity_probability',
    'path_probability',
    'estimated_total_cost_percent',
    'expected_net_profit_percent',
    'effective_take_profit_percent',
    'effective_stop_loss_percent',
    'spread_percent',
    'relative_spread_ratio',
    'entry_freshness_score',
    'movement_consumed_to_tp_ratio',
)

MANAGED_V2_FEATURES = {
    StrategySegment.EQUITY_EU_BUY: {
        'opportunity': (
            'touch_probability',
            'entry_freshness_score',
            'movement_consumed_to_tp_ratio',
            'spread_percent',
            'relative_spread_ratio',
            'estimated_total_cost_percent',
            'aligned_session_move',
            'aligned_symbol_relative_strength',
            'aligned_benchmark_momentum',
            'm30_return_sample_aligned',
            'm30_close_vs_fast_ema_aligned',
            'h1_return_sample_aligned',
            'h1_fast_vs_slow_ema_aligned',
        ),
        'path': (
            'touch_probability',
            'entry_freshness_score',
            'movement_consumed_to_tp_ratio',
            'spread_percent',
            'relative_spread_ratio',
            'relative_spread_recent_change',
            'aligned_symbol_relative_strength',
            'aligned_benchmark_momentum',
            'atr_percent',
            'm30_return_sample_aligned',
            'h1_return_sample_aligned',
        ),
        'economics': _COMMON_ECONOMICS_FEATURES,
    },
    StrategySegment.EQUITY_EU_SELL: {
        'opportunity': (
            'touch_probability',
            'entry_freshness_score',
            'movement_consumed_to_tp_ratio',
            'spread_percent',
            'relative_spread_ratio',
            'estimated_total_cost_percent',
            'aligned_session_move',
            'aligned_symbol_relative_strength',
        ),
        'path': (
            'touch_probability',
            'entry_freshness_score',
            'spread_percent',
            'relative_spread_ratio',
            'aligned_session_move',
            'atr_percent',
        ),
        'economics': _COMMON_ECONOMICS_FEATURES,
    },
    StrategySegment.EQUITY_US_BUY: {
        'opportunity': (
            'touch_probability',
            'entry_freshness_score',
            'movement_consumed_to_tp_ratio',
            'spread_percent',
            'relative_spread_ratio',
            'estimated_total_cost_percent',
            'aligned_symbol_relative_strength',
            'aligned_benchmark_momentum',
            'atr_percent',
            'session_progress',
        ),
        'path': (
            'touch_probability',
            'entry_freshness_score',
            'movement_consumed_to_tp_ratio',
            'spread_percent',
            'relative_spread_ratio',
            'relative_spread_percentile',
            'relative_spread_recent_change',
            'aligned_symbol_relative_strength',
            'aligned_benchmark_momentum',
            'aligned_snapshot_momentum',
            'atr_percent',
            'regime_noise_ratio',
        ),
        'economics': _COMMON_ECONOMICS_FEATURES,
    },
    StrategySegment.EQUITY_US_SELL: {
        'opportunity': (
            'touch_probability',
            'entry_freshness_score',
            'movement_consumed_to_tp_ratio',
            'spread_percent',
            'relative_spread_ratio',
            'estimated_total_cost_percent',
            'aligned_symbol_relative_strength',
            'm15_return_sample_aligned',
            'm15_acceleration_aligned',
            'm30_return_sample_aligned',
        ),
        'path': (
            'touch_probability',
            'entry_freshness_score',
            'spread_percent',
            'relative_spread_ratio',
            'aligned_symbol_relative_strength',
            'm15_return_sample_aligned',
            'm15_velocity_aligned',
            'm15_acceleration_aligned',
            'm30_return_sample_aligned',
            'm30_velocity_aligned',
        ),
        'economics': _COMMON_ECONOMICS_FEATURES,
    },
}


def feature_names_for(
    segment: StrategySegment,
    component: str,
) -> tuple[str, ...]:
    try:
        return MANAGED_V2_FEATURES[segment][component]
    except KeyError as exc:
        raise ValueError(
            f'No MANAGED V2 {component!r} feature contract for {segment.value}.'
        ) from exc
