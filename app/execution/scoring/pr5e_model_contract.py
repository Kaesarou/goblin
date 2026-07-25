OUTCOME_PROBABILITY_MODEL_VERSION = 'outcome_probability_v1'
OUTCOME_PROBABILITY_FEATURE_CONTRACT_VERSION = 'pr5e_features_v1'
PROBABILITY_SCORE_SCALE = 200.0

TRAINING_ASSET_CLASSES = (
    'EQUITY_EU',
    'EQUITY_US',
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

DIRECTION_NUMERIC_FEATURES = (
    'session_progress',
    'context_relative_strength_raw',
    'context_sector',
    'm5_aligned_x_consumed',
    'context_benchmark_momentum',
)

DIRECTION_CATEGORICAL_FEATURES = (
    'asset_class',
    'side',
    'profile_key',
    'm5_maturity',
)
