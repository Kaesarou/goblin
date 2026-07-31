MANAGED_OUTCOME_MODEL_VERSION = 'managed_outcome_v1'
MANAGED_OUTCOME_FEATURE_CONTRACT_VERSION = 'managed_outcome_features_v1'
MANAGED_SELECTION_POLICY_VERSION = 'managed_edge_v1'

TRAINING_ASSET_CLASSES = (
    'EQUITY_EU',
    'EQUITY_US',
)

SUPPORTED_MANAGED_SEGMENTS = (
    'EQUITY_EU_BUY',
    'EQUITY_EU_SELL',
    'EQUITY_US_BUY',
    'EQUITY_US_SELL',
    'CRYPTO_BUY',
    'CRYPTO_SELL',
)

MANAGED_NUMERIC_FEATURES = (
    'session_progress',
    'session_progress_sq',
    'aligned_session_move',
    'aligned_snapshot_momentum',
    'aligned_symbol_relative_strength',
    'aligned_benchmark_momentum',
    'breakout_percent',
    'aligned_trend_strength',
    'close_quality',
    'atr_percent',
    'regime_noise_ratio',
    'touch_probability',
    'direction_probability',
    'direction_edge',
    'tp_to_atr_ratio',
    'tp_to_momentum_ratio',
    'cost_to_tp_ratio',
    'movement_consumed_to_tp_ratio',
    'tp_feasibility_score',
    'entry_freshness_score',
    'estimated_total_cost_percent',
    'expected_net_profit_percent',
    'reward_to_risk_ratio',
    'net_reward_to_risk_ratio',
    'base_score',
    'directional_score',
)
