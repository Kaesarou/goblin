from __future__ import annotations
from dataclasses import dataclass, field
from enum import StrEnum

class EconomicsPolicy(StrEnum):
    REQUIRE_NET_POSITIVE='require_net_positive'
    RESEARCH_GROSS_EDGE='research_gross_edge'

@dataclass(frozen=True)
class RecoverabilityConfig:
    enabled: bool=False
    min_rank_quantile: float=.75
    target_recovery_pct: float=.005
    adverse_barrier_pct: float=.05
    horizon_minutes: int=2340
    gate_from_entry_fill_count: int=2

@dataclass(frozen=True)
class InventoryRiskConfig:
    max_entry_fills:int=5
    max_symbol_exposure_pct:float=.04
    max_portfolio_exposure_pct:float=.15
    max_inventories:int=7

@dataclass(frozen=True)
class InventoryStrategyConfig:
    name:str='INVENTORY_RR5_V1'
    ema_span_0:float=790.0
    ema_span_1:float=1080.0
    initial_ema_dist_pct:float=.0028
    # Frozen Passivbot effective per-slot wallet exposure used by dynamic
    # threshold/close math. This is strategy geometry, not a Goblin hard cap.
    effective_wallet_exposure_limit_pct:float=(1.5/7)*1.37
    initial_exposure_pct:float=.0023779285714285716
    reentry_double_down_factor:float=.94
    reentry_threshold_base_pct:float=.019
    reentry_threshold_volatility_1m_weight:float=17.68
    reentry_threshold_volatility_1h_weight:float=4.58
    reentry_threshold_exposure_weight:float=1.007
    reentry_retracement_base_pct:float=.0008
    reentry_retracement_volatility_1m_weight:float=50.42
    reentry_retracement_volatility_1h_weight:float=15.5
    reentry_retracement_exposure_weight:float=.334
    close_qty_pct:float=.84
    close_threshold_base_pct:float=-.0027
    close_threshold_volatility_1m_weight:float=3.22
    close_threshold_volatility_1h_weight:float=.48
    close_threshold_exposure_weight:float=-.0001
    close_retracement_base_pct:float=.0005
    close_retracement_volatility_1m_weight:float=52.34
    close_retracement_volatility_1h_weight:float=22.95
    unstuck_enabled:bool=True
    unstuck_threshold_ratio:float=.466
    unstuck_close_pct:float=.041
    unstuck_ema_dist_pct:float=-.0269
    unstuck_loss_allowance_pct:float=.0052
    entry_cooldown_minutes:float=24.1

@dataclass(frozen=True)
class HedgeConfig:
    enabled:bool=True
    hedge_symbol:str='SPY'
    target_beta_exposure_pct:float=.06
    open_above_beta_exposure_pct:float=.10
    close_below_beta_exposure_pct:float=.06
    rebalance_deadband_pct:float=.02
    max_hedge_notional_pct:float=.10
    min_adjustment_notional_pct:float=.005

@dataclass(frozen=True)
class EconomicsConfig:
    policy:EconomicsPolicy=EconomicsPolicy.RESEARCH_GROSS_EDGE
    expected_holding_days:float=3.0
    minimum_gross_capture_pct:float=0.0

@dataclass(frozen=True)
class GoblinV3Config:
    strategy:InventoryStrategyConfig=field(default_factory=InventoryStrategyConfig)
    recoverability:RecoverabilityConfig=field(default_factory=RecoverabilityConfig)
    risk:InventoryRiskConfig=field(default_factory=InventoryRiskConfig)
    hedge:HedgeConfig=field(default_factory=HedgeConfig)
    economics:EconomicsConfig=field(default_factory=EconomicsConfig)

def rr5_research_config()->GoblinV3Config:
    return GoblinV3Config()


def rr5_recoverability_experiment_config(
    *, min_rank_quantile: float = 0.75, gate_from_entry_fill_count: int = 2
) -> GoblinV3Config:
    base = rr5_research_config()
    return GoblinV3Config(
        strategy=base.strategy,
        recoverability=RecoverabilityConfig(
            enabled=True,
            min_rank_quantile=min_rank_quantile,
            target_recovery_pct=base.recoverability.target_recovery_pct,
            adverse_barrier_pct=base.recoverability.adverse_barrier_pct,
            horizon_minutes=base.recoverability.horizon_minutes,
            gate_from_entry_fill_count=gate_from_entry_fill_count,
        ),
        risk=base.risk,
        hedge=base.hedge,
        economics=base.economics,
    )
