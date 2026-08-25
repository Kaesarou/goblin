from __future__ import annotations
import math
from dataclasses import dataclass,replace
import numpy as np,pandas as pd
from app.v3.models import InventoryState,MarketState,PortfolioState,IntentPurpose

MIN_NOTIONAL=10.0

@dataclass(frozen=True)
class ReplayMetrics:
    starting_balance:float; ending_equity:float; return_pct:float; max_drawdown_pct:float; max_gross_exposure_pct:float; max_single_exposure_pct:float; entry_fills:int; exit_fills:int; cycles:int; forced_closes:int; positive_days:int; total_days:int; gross_pnl_before_costs:float; fees_paid:float
@dataclass(frozen=True)
class ReplayFill:
    timestamp:pd.Timestamp; symbol:str; purpose:str; side:str; units:float; price:float; fee:float; realized_pnl:float; entry_fill_count:int
@dataclass
class _Pending: intent:object; created_bar_time:pd.Timestamp

class M1FeatureFrameBuilder:
    """Build the exact causal feature representation used by Point-M screening."""
    def __init__(self):self.ema_spans=(790.0,math.sqrt(790*1080),1080.0)
    def build(self,candles):
        d=candles.copy();d.symbol=d.symbol.astype(str).str.upper();d.opened_at=pd.to_datetime(d.opened_at,utc=True);d=d.sort_values(['symbol','opened_at']).drop_duplicates(['symbol','opened_at'],keep='last').reset_index(drop=True)
        parts=[]
        for sym,g in d.groupby('symbol',sort=False):
            g=g.copy();es=[]
            for j,s in enumerate(self.ema_spans):g[f'_e{j}']=g.close.ewm(span=s,adjust=False,min_periods=1).mean();es.append(f'_e{j}')
            g['ema_lower']=g[es].min(axis=1);g['ema_upper']=g[es].max(axis=1);g['dist_lower']=g.close/g.ema_lower-1;g['ema_ratio']=g.ema_upper/g.ema_lower-1;g['ema_readiness']=g.close/(g.ema_lower*(1-.0028))-1
            lr=np.log(g.high.clip(lower=1e-12)/g.low.clip(lower=1e-12)).replace([np.inf,-np.inf],np.nan).fillna(0.0);g['vol1m_604']=lr.ewm(span=604,adjust=True,min_periods=1).mean();g['forager_vol_2274']=lr.ewm(span=2274,adjust=True,min_periods=1).mean();g['activity_310']=g.sample_count.astype(float).ewm(span=310,adjust=True,min_periods=1).mean()
            for h in (5,15,30,60):g[f'ret{h}']=g.close/g.close.shift(h)-1
            for h in (15,60):g[f'range{h}']=g.high.rolling(h).max()/g.low.rolling(h).min()-1
            mins=g.opened_at.dt.hour*60+g.opened_at.dt.minute;start=420 if '.' in sym else 810;dur=510 if '.' in sym else 390;g['session_frac']=((mins-start)/dur).clip(0,1)
            g['_hour']=g.opened_at.dt.floor('h');hourly=g.groupby('_hour').agg(high=('high','max'),low=('low','min'));hv=np.log(hourly.high.clip(lower=1e-12)/hourly.low.clip(lower=1e-12)).ewm(span=683,adjust=True,min_periods=1).mean();g['vol1h_683']=g._hour.map(hv.shift(1)).fillna(0.0);g=g.drop(columns=es+['_hour']);parts.append(g)
        return pd.concat(parts).sort_values(['opened_at','symbol']).reset_index(drop=True)

class InventoryReplayEngine:
    """Single-venue M1 replay of the shared V3 planner.

    Point-M mode intentionally mirrors the frozen screening execution contract:
    resting intents live for one observed candle of that symbol, close intents
    fill before entries, fills reset trailing extrema, and final open inventory
    is force-realized at the final observed close.
    """
    def __init__(self,*,planner,config,starting_balance=100000.,fee_pct_per_fill=.00225,forager_mode='no_volume',research_proxy_mode=True,ending_force_close=True):
        self.planner=planner;self.config=config;self.starting_balance=starting_balance;self.fee_pct_per_fill=fee_pct_per_fill;self.forager_mode=forager_mode;self.research_proxy_mode=research_proxy_mode;self.ending_force_close=ending_force_close
    def run(self,frame):
        df=frame.sort_values(['opened_at','symbol']).reset_index(drop=True)
        syms=np.array(sorted(df.symbol.astype(str).unique()));sid={s:i for i,s in enumerate(syms)}
        sym_ids=df.symbol.map(sid).to_numpy(np.int16);ts_ns=pd.to_datetime(df.opened_at,utc=True).astype('int64').to_numpy();_,starts=np.unique(ts_ns,return_index=True);ends=np.r_[starts[1:],len(df)]
        close=df.close.to_numpy(float);high=df.high.to_numpy(float);low=df.low.to_numpy(float);ema_lo=df.ema_lower.to_numpy(float);ema_hi=df.ema_upper.to_numpy(float);v1m=df.vol1m_604.to_numpy(float);v1h=df.vol1h_683.to_numpy(float);fvol=df.forager_vol_2274.to_numpy(float);activity=df.activity_310.to_numpy(float);read=df.ema_readiness.to_numpy(float)
        feat_names=('ema_readiness','forager_vol_2274','activity_310','vol1m_604','vol1h_683','ret5','ret15','ret30','ret60','range15','range60','ema_ratio','dist_lower','session_frac');feat_arrays={n:df[n].to_numpy(float) for n in feat_names};quality=df.quality_degraded.to_numpy(bool) if 'quality_degraded' in df else np.zeros(len(df),dtype=bool);entry_ok=df.entry_allowed.to_numpy(bool) if 'entry_allowed' in df else np.ones(len(df),dtype=bool)
        balance=self.starting_balance;pos={};pentry={};pclose={};latest=np.zeros(len(syms));fills=[];eqrows=[];cycle_seq={};gross=fees=0.;peak=balance;maxdd=0.;maxexp=maxsingle=0.;ds={};de={};forced=0
        for a,b in zip(starts,ends):
            ts=pd.Timestamp(int(ts_ns[a]),tz='UTC');ids=sym_ids[a:b].astype(int);filled=set()
            # Fill and consume resting closes then entries for current symbols.
            for idx in range(a,b):
                i=int(sym_ids[idx]);pn=pclose.pop(i,None)
                if pn is not None and high[idx]>float(pn.intent.limit_price):
                    balance,new,fill,gdelta,fee=self._apply_close(balance,pos.get(i),pn.intent,float(pn.intent.limit_price),ts);gross+=gdelta;fees+=fee;fills.append(fill);filled.add(i)
                    if new is None:pos.pop(i,None)
                    else:pos[i]=new
                pn=pentry.pop(i,None)
                if pn is not None and low[idx]<float(pn.intent.limit_price):
                    balance,new,fill,fee,_=self._apply_entry(balance,pos.get(i),pn.intent,float(pn.intent.limit_price),ts,pos,cycle_seq);fees+=fee;fills.append(fill);filled.add(i);pos[i]=new
                latest[i]=close[idx]
            # Ordered trailing bundle update only when no fill reset state.
            for idx in range(a,b):
                i=int(sym_ids[idx])
                if i in pos and i not in filled:pos[i]=_ext_values(pos[i],high[idx],low[idx],close[idx])
            portfolio=_portfolio(balance,{syms[i]:inv for i,inv in pos.items()})
            # Fresh open-position intents for each active symbol.
            for idx in range(a,b):
                i=int(sym_ids[idx])
                if i not in pos:continue
                pentry.pop(i,None);pclose.pop(i,None);m=_market_arrays(syms[i],ts,idx,close,ema_lo,ema_hi,v1m,v1h,feat_arrays,quality,entry_ok,False,self.research_proxy_mode);batch=self.planner.plan_symbol(market=m,portfolio=portfolio)
                for intent in batch.intents:
                    if intent.side=='BUY':pentry[i]=_Pending(intent,ts)
                    else:pclose[i]=_Pending(intent,ts)
            # Cancel all flat pending entries globally and refresh Forager.
            for i in list(pentry):
                if i not in pos:pentry.pop(i,None)
            free=max(0,self.config.risk.max_inventories-len(pos))
            if free>0:
                cand=[]
                for idx in range(a,b):
                    i=int(sym_ids[idx])
                    if i in pos:continue
                    if not (np.isfinite(read[idx]) and np.isfinite(fvol[idx])):continue
                    cand.append(idx)
                if cand:
                    cr=np.asarray(cand,dtype=int)
                    if self.forager_mode=='sample_count':
                        vv0=activity[cr];nkeep=max(free,min(len(cr),round(len(cr)*.96)));keep=np.argsort(-vv0,kind='stable')[:nkeep];cr=cr[keep];vv=vv0[keep];weights=(.18,.61,.21)
                    elif self.forager_mode=='no_volume':vv=np.zeros(len(cr));weights=(0,.61/.82,.21/.82)
                    else:raise ValueError(self.forager_mode)
                    sc=weights[0]*_minmax_high(vv)+weights[1]*_minmax_high(fvol[cr])+weights[2]*_minmax_low(read[cr]);top=np.argsort(-sc,kind='stable')[:free];portfolio=_portfolio(balance,{syms[i]:inv for i,inv in pos.items()})
                    for idx in cr[top]:
                        i=int(sym_ids[idx]);m=_market_arrays(syms[i],ts,idx,close,ema_lo,ema_hi,v1m,v1h,feat_arrays,quality,entry_ok,True,self.research_proxy_mode);batch=self.planner.plan_symbol(market=m,portfolio=portfolio)
                        for intent in batch.intents:
                            if intent.side=='BUY':pentry[i]=_Pending(intent,ts)
            exps=[inv.total_notional/max(balance,1e-12) for inv in pos.values()];exposure=sum(exps);maxexp=max(maxexp,exposure);maxsingle=max(maxsingle,max(exps,default=0));upnl=sum(inv.total_units*((latest[i] if latest[i]>0 else inv.last_entry_price)-inv.average_entry_price) for i,inv in pos.items());equity=balance+upnl;peak=max(peak,equity);dd=equity/peak-1;maxdd=min(maxdd,dd);day=ts.date();ds.setdefault(day,equity);de[day]=equity;eqrows.append({'ts':ts,'equity':equity,'balance':balance,'open_inventories':len(pos),'gross_exposure_pct':exposure,'drawdown_pct':dd})
        if self.ending_force_close and pos:
            last_ts=pd.Timestamp(int(ts_ns[-1]),tz='UTC')
            for i in list(pos):
                inv=pos[i];px=latest[i] if latest[i]>0 else inv.last_entry_price;intent=_force_intent(inv,last_ts,px);balance,new,fill,gdelta,fee=self._apply_close(balance,inv,intent,px,last_ts);gross+=gdelta;fees+=fee;fills.append(fill);forced+=1;pos.pop(i,None)
        ending=balance+sum(inv.total_units*((latest[i] if latest[i]>0 else inv.last_entry_price)-inv.average_entry_price) for i,inv in pos.items());fdf=pd.DataFrame([x.__dict__ for x in fills]);edf=pd.DataFrame(eqrows)
        return ReplayMetrics(self.starting_balance,ending,ending/self.starting_balance-1,abs(maxdd),maxexp,maxsingle,sum(x.side=='BUY' for x in fills),sum(x.side=='SELL' for x in fills),sum(cycle_seq.values()),forced,sum(de[d]>ds[d] for d in de),len(de),gross,fees),fdf,edf

    def _apply_entry(self,balance,inv,intent,price,ts,pos,cycle_seq):
        requested_units=float(intent.metadata.get('requested_units',intent.notional/price)) if hasattr(intent,'metadata') else intent.notional/price
        q=max(0.0,requested_units)
        # Crop at fill to global and per-symbol exposure budgets as in reference.
        curr=sum(x.total_notional for x in pos.values())/max(balance,1e-12);room=max(0.0,self.config.risk.max_portfolio_exposure_pct-curr)*balance;q=min(q,room/price)
        old_notional=inv.total_notional if inv else 0.0;indiv_room=max(0.0,self.config.risk.max_symbol_exposure_pct*balance-old_notional);q=min(q,indiv_room/price)
        if q*price<MIN_NOTIONAL:raise RuntimeError(f'Point-M planned sub-minimum entry {intent.symbol} {q*price}')
        notional=q*price;fee=notional*self.fee_pct_per_fill;balance-=fee
        if inv is None:
            cycle_seq[intent.symbol]=cycle_seq.get(intent.symbol,0)+1;iid=f'{intent.symbol}:{cycle_seq[intent.symbol]}';new=InventoryState(iid,intent.symbol,ts.to_pydatetime(),q,price,1,ts.to_pydatetime(),price,notional,notional/max(balance,1e-12),initial_entry_units=q,fees_paid=fee,last_fill_at=ts.to_pydatetime(),trailing_min_since_open=None,trailing_max_since_min=None,trailing_max_since_open=None,trailing_min_since_max=None,min_price_since_last_entry=price,max_price_since_last_entry=price,min_price_since_open=price,max_price_since_open=price)
        else:
            oldcost=inv.total_units*inv.average_entry_price;tu=inv.total_units+q;avg=(oldcost+notional)/tu;new=replace(inv,total_units=tu,average_entry_price=avg,entry_fill_count=inv.entry_fill_count+1,last_entry_at=ts.to_pydatetime(),last_entry_price=price,last_fill_at=ts.to_pydatetime(),total_notional=tu*avg,wallet_exposure_pct=tu*avg/max(balance,1e-12),fees_paid=inv.fees_paid+fee,trailing_min_since_open=None,trailing_max_since_min=None,trailing_max_since_open=None,trailing_min_since_max=None,min_price_since_last_entry=price,max_price_since_last_entry=price)
        fill=ReplayFill(ts,intent.symbol,intent.purpose.value,'BUY',q,price,fee,0.,new.entry_fill_count)
        return balance,new,fill,fee,inv is None
    def _apply_close(self,balance,inv,intent,price,ts):
        if inv is None:raise RuntimeError(f'reduce without inventory {intent.symbol}')
        frac=float(intent.metadata.get('close_fraction_of_units',0)) if hasattr(intent,'metadata') else 0
        units=min(inv.total_units,inv.total_units*frac if frac>0 else intent.notional/price)
        if (inv.total_units-units)*price<MIN_NOTIONAL:units=inv.total_units
        notional=units*price;fee=notional*self.fee_pct_per_fill;gross=units*(price-inv.average_entry_price);balance+=gross-fee;rem=inv.total_units-units
        if rem<=1e-12:new=None;ec=inv.entry_fill_count
        else:new=replace(inv,total_units=rem,total_notional=rem*inv.average_entry_price,wallet_exposure_pct=rem*inv.average_entry_price/max(balance,1e-12),realized_pnl=inv.realized_pnl+gross,fees_paid=inv.fees_paid+fee,profit_exit_fill_count=inv.profit_exit_fill_count+(1 if intent.purpose==IntentPurpose.PROFIT_EXIT else 0),last_fill_at=ts.to_pydatetime(),trailing_min_since_open=None,trailing_max_since_min=None,trailing_max_since_open=None,trailing_min_since_max=None,min_price_since_last_entry=price,max_price_since_last_entry=price);ec=new.entry_fill_count
        fill=ReplayFill(ts,intent.symbol,intent.purpose.value,'SELL',units,price,fee,gross,ec)
        return balance,new,fill,gross,fee

def _market(r,entry_allowed,research_proxy):
    names=('ema_readiness','vol1m_604','vol1h_683','ret5','ret15','ret30','ret60','range15','range60','ema_ratio','dist_lower','session_frac');f={n:float(getattr(r,n)) for n in names};q=math.isfinite(float(r.ema_lower)) and math.isfinite(float(r.ema_upper));
    if not research_proxy:q=q and not bool(getattr(r,'quality_degraded',False)) and bool(getattr(r,'entry_allowed',True))
    close=float(r.close);return MarketState(str(r.symbol),pd.Timestamp(r.opened_at).to_pydatetime(),close,close,close,float(r.ema_lower),float(r.ema_upper),float(r.vol1m_604) if math.isfinite(float(r.vol1m_604)) else 0,float(r.vol1h_683) if math.isfinite(float(r.vol1h_683)) else 0,f,q,entry_allowed)
def _entry_touched(i,b):return i.limit_price is not None and float(b.low)<float(i.limit_price)
def _close_touched(i,b):return i.limit_price is not None and float(b.high)>float(i.limit_price)
def _ext(i,b):
    lo=float(b.low);hi=float(b.high);c=float(b.close)
    tmin=i.trailing_min_since_open;tmaxmin=i.trailing_max_since_min;tmax=i.trailing_max_since_open;tminmax=i.trailing_min_since_max
    if tmin is None or lo<tmin:tmin=lo;tmaxmin=c
    else:tmaxmin=max(tmaxmin if tmaxmin is not None else c,hi)
    if tmax is None or hi>tmax:tmax=hi;tminmax=c
    else:tminmax=min(tminmax if tminmax is not None else c,lo)
    mil=min(x for x in (i.min_price_since_last_entry,lo) if x is not None);mal=max(x for x in (i.max_price_since_last_entry,hi) if x is not None);mio=min(x for x in (i.min_price_since_open,lo) if x is not None);mao=max(x for x in (i.max_price_since_open,hi) if x is not None)
    return replace(i,trailing_min_since_open=tmin,trailing_max_since_min=tmaxmin,trailing_max_since_open=tmax,trailing_min_since_max=tminmax,min_price_since_last_entry=mil,max_price_since_last_entry=mal,min_price_since_open=mio,max_price_since_open=mao,mfe_pct=max(i.mfe_pct,mao/i.average_entry_price-1),mae_pct=min(i.mae_pct,mio/i.average_entry_price-1))
def _market_arrays(symbol,ts,idx,close,ema_lo,ema_hi,v1m,v1h,feat_arrays,quality,entry_ok,entry_allowed,research_proxy):
    f={n:float(a[idx]) for n,a in feat_arrays.items()};q=np.isfinite(ema_lo[idx]) and np.isfinite(ema_hi[idx])
    if not research_proxy:q=q and not bool(quality[idx]) and bool(entry_ok[idx])
    c=float(close[idx]);return MarketState(str(symbol),ts.to_pydatetime(),c,c,c,float(ema_lo[idx]),float(ema_hi[idx]),float(v1m[idx]) if np.isfinite(v1m[idx]) else 0.0,float(v1h[idx]) if np.isfinite(v1h[idx]) else 0.0,f,bool(q),bool(entry_allowed))
def _ext_values(i,hi,lo,c):
    lo=float(lo);hi=float(hi);c=float(c);tmin=i.trailing_min_since_open;tmaxmin=i.trailing_max_since_min;tmax=i.trailing_max_since_open;tminmax=i.trailing_min_since_max
    if tmin is None or lo<tmin:tmin=lo;tmaxmin=c
    else:tmaxmin=max(tmaxmin if tmaxmin is not None else c,hi)
    if tmax is None or hi>tmax:tmax=hi;tminmax=c
    else:tminmax=min(tminmax if tminmax is not None else c,lo)
    mil=min(x for x in (i.min_price_since_last_entry,lo) if x is not None);mal=max(x for x in (i.max_price_since_last_entry,hi) if x is not None);mio=min(x for x in (i.min_price_since_open,lo) if x is not None);mao=max(x for x in (i.max_price_since_open,hi) if x is not None)
    return replace(i,trailing_min_since_open=tmin,trailing_max_since_min=tmaxmin,trailing_max_since_open=tmax,trailing_min_since_max=tminmax,min_price_since_last_entry=mil,max_price_since_last_entry=mal,min_price_since_open=mio,max_price_since_open=mao,mfe_pct=max(i.mfe_pct,mao/i.average_entry_price-1),mae_pct=min(i.mae_pct,mio/i.average_entry_price-1))
def _portfolio(balance,pos):
    b=max(balance,1e-12);return PortfolioState(b,tuple(replace(i,wallet_exposure_pct=i.total_notional/b) for i in pos.values()))
def _mark(balance,pos,latest):return balance+sum(i.total_units*(latest.get(s,i.last_entry_price)-i.average_entry_price) for s,i in pos.items())
def _minmax_high(x):
    if len(x)==0:return x
    mn=np.nanmin(x);mx=np.nanmax(x)
    if not np.isfinite(mn) or not np.isfinite(mx) or mx-mn<=1e-15:return np.ones_like(x,dtype=float)
    return (x-mn)/(mx-mn)
def _minmax_low(x):
    if len(x)==0:return x
    mn=np.nanmin(x);mx=np.nanmax(x)
    if not np.isfinite(mn) or not np.isfinite(mx) or mx-mn<=1e-15:return np.ones_like(x,dtype=float)
    return (mx-x)/(mx-mn)
def _force_intent(inv,ts,px):
    from app.v3.models import OrderIntent,ExecutionStyle
    return OrderIntent(f'force:{inv.inventory_id}:{ts}',IntentPurpose.RISK_REDUCTION,inv.symbol,'SELL',inv.total_units*px,ts.to_pydatetime(),ExecutionStyle.MARKET,inventory_id=inv.inventory_id,reduce_only=True,metadata={'close_fraction_of_units':1.0,'force_close':True})
