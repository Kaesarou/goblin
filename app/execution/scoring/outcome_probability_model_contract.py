OUTCOME_PROBABILITY_MODEL_VERSION = 'outcome_probability_v2'
OUTCOME_PROBABILITY_FEATURE_CONTRACT_VERSION = (
    'outcome_probability_features_v2'
)
PROBABILITY_SCORE_SCALE = 200.0
MINIMUM_DIRECTION_EDGE = 0.05

TRAINING_ASSET_CLASSES = (
    'EQUITY_EU',
    'EQUITY_US',
)

SUPPORTED_DIRECTION_SEGMENTS = (
    'EQUITY_EU_BUY',
    'EQUITY_EU_SELL',
    'EQUITY_US_BUY',
    'EQUITY_US_SELL',
    'CRYPTO_BUY',
    'CRYPTO_SELL',
)

ACTIVITY_NUMERIC_FEATURES = (
    'effective_take_profit_percent',
    'effective_stop_loss_percent',
    'estimated_total_cost_percent',
    'expected_net_profit_percent',
    'reward_to_risk_ratio',
    'net_reward_to_risk_ratio',
    'horizon_minutes',
    'session_minutes',
    'session_progress',
    'base_score',
    'directional_score',
    'tp_feasibility_score',
    'tp_feasibility_contribution',
    'tp_component_atr',
    'tp_component_momentum',
    'tp_component_cost',
    'tp_to_atr_ratio',
    'tp_to_momentum_ratio',
    'cost_to_tp_ratio',
    'movement_consumed_to_tp_ratio',
    'entry_freshness_score',
    'extension_to_tp_ratio',
)

ACTIVITY_CATEGORICAL_FEATURES = (
    'asset_class',
    'side',
    'profile_key',
    'm5_maturity',
    'm5_alignment',
    'm15_maturity',
    'm15_alignment',
    'm30_maturity',
    'm30_alignment',
    'h1_maturity',
)

CORE_DIRECTION_NUMERIC_FEATURES = (
    'session_progress',
    'aligned_session_move',
    'aligned_snapshot_momentum',
    'aligned_symbol_relative_strength',
    'aligned_benchmark_momentum',
    'breakout_percent',
    'trend_strength_percent',
    'close_quality',
    'atr_percent',
    'regime_noise_ratio',
    'touch_probability',
    'direction_break_even_probability',
)

MTF_DIRECTION_NUMERIC_FEATURES = (
    'session_progress',
    'aligned_session_move',
    'aligned_snapshot_momentum',
    'aligned_symbol_relative_strength',
    'aligned_m5_return',
    'aligned_m15_return',
    'aligned_m5_velocity',
    'aligned_m15_velocity',
    'aligned_m5_acceleration',
    'aligned_m15_acceleration',
    'breakout_percent',
    'close_quality',
    'atr_percent',
    'regime_noise_ratio',
    'touch_probability',
    'direction_break_even_probability',
)

GEOMETRY_DIRECTION_NUMERIC_FEATURES = (
    'session_progress',
    'aligned_session_move',
    'aligned_symbol_relative_strength',
    'aligned_m15_return',
    'breakout_percent',
    'atr_percent',
    'tp_to_atr_ratio',
    'tp_to_momentum_ratio',
    'cost_to_tp_ratio',
    'tp_feasibility_score',
    'movement_consumed_to_tp_ratio',
    'touch_probability',
    'direction_break_even_probability',
)

FULL_DIRECTION_CATEGORICAL_FEATURES = (
    'entry_route_action',
    'm5_maturity',
    'm5_alignment',
    'm15_maturity',
    'm15_alignment',
    'm30_maturity',
    'm30_alignment',
    'h1_maturity',
    'market_regime_context',
    'context_alignment',
    'm5_direction',
    'm15_direction',
    'm30_direction',
    'h1_direction',
)

US_BUY_DIRECTION_CATEGORICAL_FEATURES = (
    'entry_route_action',
    'm5_maturity',
    'm5_alignment',
    'm15_maturity',
    'm15_alignment',
    'm30_maturity',
    'm30_alignment',
    'market_regime_context',
    'context_alignment',
    'm5_direction',
    'm15_direction',
    'm30_direction',
)

US_SELL_DIRECTION_CATEGORICAL_FEATURES = (
    'entry_route_action',
    'm5_maturity',
    'm5_alignment',
    'm15_maturity',
    'm15_alignment',
    'm30_maturity',
    'market_regime_context',
    'context_alignment',
    'm5_direction',
    'm15_direction',
    'm30_direction',
)

DIRECTION_FEATURES_BY_SEGMENT = {
    'EQUITY_EU_BUY': (
        CORE_DIRECTION_NUMERIC_FEATURES,
        FULL_DIRECTION_CATEGORICAL_FEATURES,
    ),
    'EQUITY_EU_SELL': (
        MTF_DIRECTION_NUMERIC_FEATURES,
        FULL_DIRECTION_CATEGORICAL_FEATURES,
    ),
    'EQUITY_US_BUY': (
        GEOMETRY_DIRECTION_NUMERIC_FEATURES,
        US_BUY_DIRECTION_CATEGORICAL_FEATURES,
    ),
    'EQUITY_US_SELL': (
        MTF_DIRECTION_NUMERIC_FEATURES,
        US_SELL_DIRECTION_CATEGORICAL_FEATURES,
    ),
    'CRYPTO_BUY': (
        GEOMETRY_DIRECTION_NUMERIC_FEATURES,
        US_BUY_DIRECTION_CATEGORICAL_FEATURES,
    ),
    'CRYPTO_SELL': (
        MTF_DIRECTION_NUMERIC_FEATURES,
        US_SELL_DIRECTION_CATEGORICAL_FEATURES,
    ),
}
