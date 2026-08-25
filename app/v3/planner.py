from __future__ import annotations
import hashlib
from datetime import timedelta
from app.v3.models import *

def _id(*p):return hashlib.sha256('|'.join(map(str,p)).encode()).hexdigest()[:24]
def _mult(m,ratio,w1,wH,wE):return max(1.0,1+m.volatility_1m*w1+m.volatility_1h*wH+max(0,ratio)*wE)

class InventoryPlanner:
    """Pure V3 decision planner shared by live and replay.

    It deliberately returns intentions only. Broker submission/fill semantics are
    environment concerns. The trailing-martingale math mirrors the frozen Point-M
    reference policy so research/live drift is measurable rather than implicit.
    """
    def __init__(self,*,config,recoverability_scorer,risk_policy,economics_model):
        self.config=config; self.recoverability_scorer=recoverability_scorer; self.risk_policy=risk_policy; self.economics_model=economics_model
    def _decision(self,m,r,detail=None):return DecisionBatch(decisions=(DecisionRecord(m.symbol,r,m.asof,detail or {}),))
    def plan_symbol(self,*,market,portfolio):
        if not market.quality_ok:return self._decision(market,DecisionReason.MARKET_DATA_INVALID)
        inv=portfolio.inventory_for(market.symbol)
        if inv is None:return self._initial(market,portfolio)
        # Normal close and re-entry may coexist as resting intents in the reference
        # strategy. They point in opposite economic directions and are reconciled
        # independently by the execution environment.
        close=self._exit(market,portfolio,inv)
        reentry=self._reentry(market,portfolio,inv)
        return close.extend(reentry)
    def _exp_ratio(self,i,p):
        # Point-M/Passivbot dynamic spacing uses its effective wallet-exposure
        # limit. Goblin hard safety caps are deliberately independent so tightening
        # risk cannot silently rewrite strategy geometry.
        cap=self.config.strategy.effective_wallet_exposure_limit_pct
        exposure=i.total_notional/max(p.equity,1e-12)
        return exposure/cap if cap else 0.0
    def _close_threshold(self,m,ratio):
        c=self.config.strategy
        # Close threshold is additive by Passivbot contract. It may be <= 0.
        return c.close_threshold_base_pct+max(0,ratio)*c.close_threshold_exposure_weight+m.volatility_1m*c.close_threshold_volatility_1m_weight+m.volatility_1h*c.close_threshold_volatility_1h_weight
    def _initial(self,m,p):
        if not m.entry_allowed:return self._decision(m,DecisionReason.NO_OPPORTUNITY)
        c=self.config.strategy; limit=min(m.bid,m.ema_lower*(1-c.initial_ema_dist_pct)); exp=c.initial_exposure_pct
        r=self.risk_policy.allow_initial_entry(p,requested_exposure_pct=exp)
        if not r.allowed:return self._decision(m,r.reason or DecisionReason.PORTFOLIO_EXPOSURE_CAP)
        exp=r.approved_exposure_pct
        n=p.equity*exp; expected=max(0.0,self._close_threshold(m,exp/max(c.effective_wallet_exposure_limit_pct,1e-12)))
        e=self.economics_model.estimate(market=m,notional=n,expected_gross_capture_pct=expected,side='BUY')
        if not e.allowed:return self._decision(m,DecisionReason.ECONOMICS_REJECTED)
        it=OrderIntent(_id(m.symbol,'entry',m.asof,limit),IntentPurpose.INITIAL_ENTRY,m.symbol,'BUY',n,m.asof,ExecutionStyle.PASSIVE_LIMIT,limit_price=limit,expected_gross_capture=n*expected,cost_estimate=e.costs,metadata={'requested_exposure_pct':exp})
        return DecisionBatch((it,),(DecisionRecord(m.symbol,DecisionReason.OPPORTUNITY,m.asof,{}),))
    def _reentry(self,m,p,i):
        c=self.config.strategy; rc=self.config.risk
        if i.entry_fill_count>=rc.max_entry_fills:return self._decision(m,DecisionReason.MAX_ENTRY_FILLS)
        last_fill=i.last_fill_at or i.last_entry_at
        if m.asof-last_fill < timedelta(minutes=c.entry_cooldown_minutes):return DecisionBatch()
        ratio=self._exp_ratio(i,p)
        tm=_mult(m,ratio,c.reentry_threshold_volatility_1m_weight,c.reentry_threshold_volatility_1h_weight,c.reentry_threshold_exposure_weight)
        th_pct=c.reentry_threshold_base_pct*tm
        threshold=i.average_entry_price*(1-th_pct)
        trough=i.trailing_min_since_open
        rebound=i.trailing_max_since_min
        if trough is None or rebound is None or trough>=threshold:return DecisionBatch()
        rm=_mult(m,ratio,c.reentry_retracement_volatility_1m_weight,c.reentry_retracement_volatility_1h_weight,c.reentry_retracement_exposure_weight)
        rt_pct=c.reentry_retracement_base_pct*rm
        if rebound<=trough*(1+rt_pct):return DecisionBatch()
        # Reference proxy rests at the tighter of current touch, dynamic rebound
        # level and EMA gate.
        rp=min(m.bid,i.average_entry_price*(1-th_pct+rt_pct),m.ema_lower*(1-c.initial_ema_dist_pct))
        if rp<=0:return DecisionBatch()
        rec=self.recoverability_scorer.score(m.features,asof=m.asof)
        next_fill_count=i.entry_fill_count+1
        gate_active=(
            self.config.recoverability.enabled
            and next_fill_count>=self.config.recoverability.gate_from_entry_fill_count
        )
        if gate_active and (not rec.valid or rec.rank_quantile<self.config.recoverability.min_rank_quantile):
            return self._decision(m,DecisionReason.RECOVERABILITY_REJECTED,{'rank':rec.rank_quantile,'valid':rec.valid,'next_fill_count':next_fill_count})
        init_units=p.equity*c.initial_exposure_pct/rp
        requested_units=max(i.total_units*c.reentry_double_down_factor,init_units)
        n=requested_units*rp; req=n/max(p.equity,1e-12)
        r=self.risk_policy.allow_reentry(p,i,requested_exposure_pct=req)
        if not r.allowed:return self._decision(m,r.reason or DecisionReason.PORTFOLIO_EXPOSURE_CAP)
        if r.approved_exposure_pct < req:
            n=p.equity*r.approved_exposure_pct
            requested_units=n/rp
            req=r.approved_exposure_pct
        expected=max(0.0,self._close_threshold(m,(i.total_notional+n)/max(p.equity,1e-12)/max(c.effective_wallet_exposure_limit_pct,1e-12)))
        e=self.economics_model.estimate(market=m,notional=n,expected_gross_capture_pct=expected,side='BUY')
        if not e.allowed:return self._decision(m,DecisionReason.ECONOMICS_REJECTED)
        it=OrderIntent(_id(i.inventory_id,'reentry',i.entry_fill_count+1,m.asof,rp),IntentPurpose.REENTRY,m.symbol,'BUY',n,m.asof,ExecutionStyle.PASSIVE_LIMIT,limit_price=rp,inventory_id=i.inventory_id,cost_estimate=e.costs,metadata={'requested_exposure_pct':req,'requested_units':requested_units,'recoverability_rank':rec.rank_quantile,'reentry_threshold':threshold,'retracement_pct':rt_pct})
        return DecisionBatch((it,),(DecisionRecord(m.symbol,DecisionReason.RECOVERABILITY_ACCEPTED,m.asof,{'rank':rec.rank_quantile}),))
    def _exit(self,m,p,i):
        c=self.config.strategy; ratio=self._exp_ratio(i,p); cap=self._close_threshold(m,ratio)
        rm=_mult(m,ratio,c.close_retracement_volatility_1m_weight,c.close_retracement_volatility_1h_weight,0); rt_pct=c.close_retracement_base_pct*rm
        peak=i.trailing_max_since_open; retrough=i.trailing_min_since_max
        cp=0.0
        if cap<=0:
            if peak is not None and retrough is not None and retrough<peak*(1-rt_pct):cp=m.bid
        else:
            if peak is not None and retrough is not None and peak>i.average_entry_price*(1+cap) and retrough<peak*(1-rt_pct):
                cp=max(m.bid,i.average_entry_price*(1+cap-rt_pct))
        if cp<=0:return DecisionBatch()
        # Reference proxy closes 84% of current units; dust below minimum notional
        # is collapsed by the fill environment, not by speculative planner rules.
        fraction=c.close_qty_pct
        n=i.total_units*fraction*cp
        it=OrderIntent(_id(i.inventory_id,'close',m.asof,cp),IntentPurpose.PROFIT_EXIT,m.symbol,'SELL',n,m.asof,ExecutionStyle.LIMIT,limit_price=cp,inventory_id=i.inventory_id,reduce_only=True,metadata={'close_fraction_of_units':fraction,'close_threshold_pct':cap,'retracement_pct':rt_pct})
        return DecisionBatch((it,),(DecisionRecord(m.symbol,DecisionReason.TRAILING_EXIT,m.asof,{}),))
