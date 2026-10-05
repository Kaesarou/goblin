from dataclasses import dataclass

from app.instruments.models import (
    AssetClass,
    InstrumentConfig,
    RiskProfile,
    TpFeasibilityConfig,
    TrendStrategyConfig,
)
from app.risk.stale_position_guard import StalePositionConfig
from app.risk.trade_cost_model import TradeCostConfig

US_INTRADAY_FIXED_PROFILE = 'us_intraday_fixed_v1'


@dataclass(frozen=True)
class EquityUsConfig(InstrumentConfig):
    trend: TrendStrategyConfig = TrendStrategyConfig(
        lookback=3,
        fast_lookback=5,
        slow_lookback=15,
        session_lookback=30,
        min_session_move_percent=0.20,
        min_breakout_percent=0.04,
        min_candle_range_percent=0.03,
        min_close_position_percent=72.0,
        atr_lookback=14,
        market_regime_filter_enabled=True,
        market_regime_min_trend_strength_percent=0.05,
        market_regime_min_atr_percent=0.01,
        market_regime_max_atr_percent=0.50,
        market_regime_max_noise_ratio=2.0,
        snapshot_momentum_window_seconds=180,
        min_snapshot_momentum_percent=0.20,
    )
    risk: RiskProfile = RiskProfile(
        asset_class=AssetClass.EQUITY_US,
        profile_key=US_INTRADAY_FIXED_PROFILE,
        max_position_size_percent=0.75,
        stop_loss_percent=0.70,
        take_profit_percent=1.20,
        force_close_enabled=False,
        force_close_hour=21,
        force_close_minute=55,
        max_spread_percent=0.10,
        min_move_spread_ratio=3.0,
        breakeven_stop_enabled=True,
        breakeven_trigger_percent=0.60,
        breakeven_buffer_percent=0.05,
        trailing_stop_enabled=True,
        trailing_stop_trigger_percent=1.00,
        trailing_stop_distance_percent=0.45,
        trailing_stop_net_buffer_percent=0.10,
        stale_position=StalePositionConfig(
            enabled=True,
            max_age_minutes=60,
            min_favorable_move_percent=0.35,
            buffer_percent=0.10,
        ),
        trade_cost=TradeCostConfig(
            open_fee_percent=0.15,
            close_fee_percent=0.15,
            fixed_open_fee=0.0,
            fixed_close_fee=0.0,
            include_spread_cost=True,
            min_expected_net_profit_percent=0.10,
        ),
        tp_feasibility=TpFeasibilityConfig(
            feasibility_buffer_percent=0.10,
        ),
    )
