from app.config.settings import Settings
from app.execution.sl_tp_profile import EffectiveSlTpResolver
from app.execution.trade_candidate import TradeCandidate
from app.instruments.instrument_registry import InstrumentRegistry
from app.instruments.models import AssetClass, RiskProfile
from app.market.models import Candle, MarketSnapshot
from app.risk.position_sizing import FixedPercentPositionSizing
from app.risk.risk_manager import RiskManager
from app.risk.trade_cost_model import TradeCostConfig
from app.strategies.signals import Signal


def risk_profile(
    *,
    asset_class: AssetClass,
    max_position_size_percent: float = 100.0,
    take_profit_percent: float = 1.6,
    trade_cost: TradeCostConfig,
    breakeven_stop_enabled: bool = False,
    breakeven_trigger_percent: float = 1.0,
    breakeven_buffer_percent: float = 0.0,
) -> RiskProfile:
    return RiskProfile(
        asset_class=asset_class,
        profile_key=f'{asset_class.value.lower()}_test_fixed_v1',
        max_position_size_percent=max_position_size_percent,
        stop_loss_percent=0.9,
        take_profit_percent=take_profit_percent,
        force_close_enabled=False,
        force_close_hour=23,
        force_close_minute=59,
        max_spread_percent=5.0,
        min_move_spread_ratio=0.0,
        breakeven_stop_enabled=breakeven_stop_enabled,
        breakeven_trigger_percent=breakeven_trigger_percent,
        breakeven_buffer_percent=breakeven_buffer_percent,
        trade_cost=trade_cost,
    )


def build_risk_manager(profile: RiskProfile) -> RiskManager:
    settings = Settings(EQUITY_US_SYMBOLS='AAPL', CRYPTO_SYMBOLS='BTC')
    return RiskManager(
        settings=settings,
        position_sizing_strategy=FixedPercentPositionSizing(),
        instrument_registry=InstrumentRegistry(
            settings,
            risk_profiles={profile.asset_class: profile},
        ),
    )


def snapshot(symbol='AAPL', bid=99.95, ask=100.05):
    return MarketSnapshot.now(symbol=symbol, bid=bid, ask=ask, last=100.0)


def buy_signal():
    return Signal(action='BUY', setup_quality=0.75, reason='test_buy')


def evaluate(manager, signal, market_snapshot, equity):
    candle = Candle(
        market_snapshot.symbol, 60,
        market_snapshot.last, market_snapshot.last,
        market_snapshot.last, market_snapshot.last,
        None, market_snapshot.timestamp, market_snapshot.timestamp,
    )
    candidate = TradeCandidate(
        symbol=market_snapshot.symbol,
        snapshot=market_snapshot,
        candle=candle,
        signal=signal,
        rank_reason='test',
    )
    effective = EffectiveSlTpResolver().resolve(
        candidate=candidate,
        risk_profile=manager.risk_profile_for(market_snapshot.symbol),
    )
    return manager.evaluate(
        signal=signal,
        snapshot=market_snapshot,
        account_equity=equity,
        session_key='test-session',
        effective_sl_tp=effective,
    )


def test_risk_manager_uses_fixed_equity_costs_and_preserves_net_buffer():
    profile = risk_profile(
        asset_class=AssetClass.EQUITY_US,
        trade_cost=TradeCostConfig(
            open_fee_percent=0.15,
            close_fee_percent=0.15,
            include_spread_cost=True,
            min_expected_net_profit_percent=0.10,
        ),
        breakeven_stop_enabled=True,
        breakeven_trigger_percent=1.0,
        breakeven_buffer_percent=0.0,
    )
    plan = evaluate(build_risk_manager(profile), buy_signal(), snapshot(), 1000.0)
    assert plan.approved
    assert plan.expected_gross_profit == 16.0
    assert plan.expected_net_profit == 12.0
    assert plan.estimated_total_cost_percent == 0.4
    assert plan.breakeven_trigger_percent == 1.0
    assert plan.breakeven_buffer_percent == 0.0


def test_risk_manager_rejects_when_post_cost_profit_is_below_minimum():
    profile = risk_profile(
        asset_class=AssetClass.EQUITY_US,
        take_profit_percent=3.0,
        trade_cost=TradeCostConfig(
            open_fee_percent=1.0,
            close_fee_percent=1.0,
            include_spread_cost=False,
            min_expected_net_profit_percent=1.20,
        ),
    )
    plan = evaluate(build_risk_manager(profile), buy_signal(), snapshot(bid=100.0, ask=100.0), 500.0)
    assert not plan.approved
    assert plan.reason == 'expected_profit_too_low_after_fees'
    assert plan.expected_net_profit_percent == 1.0
    assert plan.required_min_expected_net_profit_amount == 6.0


def test_risk_manager_accepts_crypto_when_fixed_target_pays_costs():
    profile = risk_profile(
        asset_class=AssetClass.CRYPTO,
        take_profit_percent=3.0,
        trade_cost=TradeCostConfig(
            open_fee_percent=1.0,
            close_fee_percent=1.0,
            include_spread_cost=False,
            min_expected_net_profit_percent=0.10,
        ),
    )
    plan = evaluate(build_risk_manager(profile), buy_signal(), snapshot('BTC', 100.0, 100.0), 747.0)
    assert plan.approved
    assert plan.expected_gross_profit == 22.41
    assert plan.estimated_total_cost == 14.94
    assert plan.expected_net_profit == 7.47
    assert plan.expected_net_profit_percent == 1.0


def test_configured_net_buffer_is_not_replaced_by_costs():
    profile = risk_profile(
        asset_class=AssetClass.EQUITY_US,
        trade_cost=TradeCostConfig(
            open_fee_percent=0.15,
            close_fee_percent=0.15,
            include_spread_cost=False,
            min_expected_net_profit_percent=0.0,
        ),
        breakeven_stop_enabled=True,
        breakeven_trigger_percent=1.0,
        breakeven_buffer_percent=1.0,
    )
    plan = evaluate(build_risk_manager(profile), buy_signal(), snapshot(bid=100.0, ask=100.0), 1000.0)
    assert plan.approved
    assert plan.estimated_total_cost_percent == 0.3
    assert plan.breakeven_trigger_percent == 1.3
    assert plan.breakeven_buffer_percent == 1.0
