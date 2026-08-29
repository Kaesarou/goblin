from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Mapping

class InventoryStatus(StrEnum): ACTIVE='active'; EXITING='exiting'; CLOSED='closed'
class IntentPurpose(StrEnum):
    INITIAL_ENTRY='initial_entry'; REENTRY='reentry'; PROFIT_EXIT='profit_exit'; UNSTUCK='unstuck'; RISK_REDUCTION='risk_reduction'; HEDGE_OPEN='hedge_open'; HEDGE_ADJUST='hedge_adjust'; HEDGE_CLOSE='hedge_close'
class ExecutionStyle(StrEnum): MARKET='market'; LIMIT='limit'; PASSIVE_LIMIT='passive_limit'
class DecisionReason(StrEnum):
    OPPORTUNITY='opportunity'; NO_OPPORTUNITY='no_opportunity'; RECOVERABILITY_ACCEPTED='recoverability_accepted'; RECOVERABILITY_REJECTED='recoverability_rejected'; MAX_ENTRY_FILLS='max_entry_fills'; SYMBOL_EXPOSURE_CAP='symbol_exposure_cap'; PORTFOLIO_EXPOSURE_CAP='portfolio_exposure_cap'; MARKET_DATA_INVALID='market_data_invalid'; ECONOMICS_REJECTED='economics_rejected'; TRAILING_EXIT='trailing_exit'; UNSTUCK='unstuck'; HEDGE_REQUIRED='hedge_required'; HEDGE_NOT_REQUIRED='hedge_not_required'; NO_ACTION='no_action'

def _as_utc(v):
    if v.tzinfo is None:return v.replace(tzinfo=timezone.utc)
    return v.astimezone(timezone.utc)

@dataclass(frozen=True)
class MarketState:
    symbol:str; asof:datetime; bid:float; ask:float; last:float; ema_lower:float; ema_upper:float; volatility_1m:float; volatility_1h:float; features:Mapping[str,float]=field(default_factory=dict); quality_ok:bool=True; entry_allowed:bool=True
    def __post_init__(self):
        object.__setattr__(self,'symbol',self.symbol.upper()); object.__setattr__(self,'asof',_as_utc(self.asof))
    @property
    def midpoint(self): return (self.bid+self.ask)/2
    @property
    def spread_pct(self): return 0.0 if self.midpoint<=0 else (self.ask-self.bid)/self.midpoint

@dataclass(frozen=True)
class RecoverabilityAssessment:
    model_version:str; feature_manifest_version:str; raw_score:float; rank_quantile:float; target_recovery_pct:float; adverse_barrier_pct:float; horizon_minutes:int; asof:datetime; valid:bool=True; invalid_reason:str|None=None

@dataclass(frozen=True)
class BrokerLeg:
    position_id:str; units:float; entry_price:float; opened_at:datetime; side:str='BUY'; account_notional:float|None=None

@dataclass(frozen=True)
class InventoryState:
    inventory_id:str; symbol:str; opened_at:datetime; total_units:float; average_entry_price:float; entry_fill_count:int; last_entry_at:datetime; last_entry_price:float; total_notional:float; wallet_exposure_pct:float
    initial_entry_units:float|None=None; profit_exit_fill_count:int=0; realized_pnl:float=0.0; fees_paid:float=0.0; carry_paid:float=0.0
    last_fill_at:datetime|None=None
    # Passivbot-compatible trailing bundle. These four values preserve path ordering,
    # which min/max alone cannot represent. They reset after every fill.
    trailing_min_since_open:float|None=None; trailing_max_since_min:float|None=None; trailing_max_since_open:float|None=None; trailing_min_since_max:float|None=None
    min_price_since_last_entry:float|None=None; max_price_since_last_entry:float|None=None; min_price_since_open:float|None=None; max_price_since_open:float|None=None; mfe_pct:float=0.0; mae_pct:float=0.0; recoverability:RecoverabilityAssessment|None=None; broker_legs:tuple[BrokerLeg,...]=(); status:InventoryStatus=InventoryStatus.ACTIVE

@dataclass(frozen=True)
class HedgeState:
    symbol:str; notional:float; beta_notional_offset:float; opened_at:datetime|None=None; broker_position_ids:tuple[str,...]=()

@dataclass(frozen=True)
class PortfolioState:
    equity:float; inventories:tuple[InventoryState,...]=(); hedge:HedgeState|None=None; symbol_betas:Mapping[str,float]=field(default_factory=dict)
    @property
    def gross_long_exposure_pct(self): return sum(i.wallet_exposure_pct for i in self.inventories if i.status!=InventoryStatus.CLOSED)
    @property
    def active_inventory_count(self): return sum(1 for i in self.inventories if i.status!=InventoryStatus.CLOSED)
    def inventory_for(self,symbol):
        for i in self.inventories:
            if i.symbol==symbol.upper() and i.status!=InventoryStatus.CLOSED:return i
        return None
    @property
    def beta_notional(self):
        x=sum(i.total_notional*float(self.symbol_betas.get(i.symbol,1.0)) for i in self.inventories if i.status!=InventoryStatus.CLOSED)
        return x-(self.hedge.beta_notional_offset if self.hedge else 0)

@dataclass(frozen=True)
class CostEstimate:
    spread_cost:float=0; commission:float=0; fixed_fee:float=0; expected_slippage:float=0; expected_overnight:float=0; expected_borrow:float=0
    @property
    def total(self):return self.spread_cost+self.commission+self.fixed_fee+self.expected_slippage+self.expected_overnight+self.expected_borrow

@dataclass(frozen=True)
class OrderIntent:
    intent_id:str; purpose:IntentPurpose; symbol:str; side:str; notional:float; created_at:datetime; execution_style:ExecutionStyle; limit_price:float|None=None; inventory_id:str|None=None; reduce_only:bool=False; expected_gross_capture:float|None=None; cost_estimate:CostEstimate|None=None; metadata:Mapping[str,object]=field(default_factory=dict)

@dataclass(frozen=True)
class DecisionRecord:
    symbol:str; reason:DecisionReason; asof:datetime; detail:Mapping[str,object]=field(default_factory=dict)
@dataclass(frozen=True)
class DecisionBatch:
    intents:tuple[OrderIntent,...]=(); decisions:tuple[DecisionRecord,...]=()
    def extend(self,o):return DecisionBatch(self.intents+o.intents,self.decisions+o.decisions)
