from dataclasses import dataclass


@dataclass(frozen=True)
class TradePlan:
    approved: bool
    reason: str
    symbol: str | None = None
    side: str | None = None
    amount: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    expected_gross_profit: float | None = None
    expected_net_profit: float | None = None
    expected_net_profit_percent: float | None = None
    required_min_expected_net_profit_amount: float | None = None
    min_expected_net_profit_percent: float | None = None
    estimated_open_fee: float | None = None
    estimated_close_fee: float | None = None
    estimated_fixed_fees: float | None = None
    estimated_explicit_cost: float | None = None
    estimated_explicit_cost_percent: float | None = None
    estimated_spread_cost: float | None = None
    estimated_total_cost: float | None = None
    estimated_total_cost_percent: float | None = None
    spread_percent: float | None = None
    max_spread_percent: float | None = None
    expected_move_percent: float | None = None
    min_required_move_percent: float | None = None
    min_move_spread_ratio: float | None = None
    atr_percent: float | None = None
    profile_key: str | None = None
    sl_tp_mode: str | None = None
    sl_tp_source: str | None = None
    effective_stop_loss_percent: float | None = None
    effective_take_profit_percent: float | None = None
    breakeven_stop_enabled: bool = False
    configured_breakeven_trigger_percent: float = 0.0
    configured_breakeven_buffer_percent: float = 0.0
    breakeven_trigger_percent: float = 0.0
    breakeven_buffer_percent: float = 0.0
    trailing_stop_enabled: bool = False
    trailing_stop_trigger_percent: float = 0.0
    trailing_stop_distance_percent: float = 0.0
    trailing_stop_net_buffer_percent: float = 0.0
    stale_position_enabled: bool = False
    stale_position_max_age_minutes: int = 0
    stale_position_min_favorable_move_percent: float = 0.0
    stale_position_buffer_percent: float = 0.0
