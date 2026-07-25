from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.config.settings import Settings
from app.execution.candidate_economics import CandidateEconomicsEstimator
from app.execution.scoring.tp_feasibility import CandidateTpFeasibilityEvaluator
from app.execution.trade_candidate import TradeCandidate
from app.instruments.base_configs import EQUITY_US_CONFIG
from app.instruments.instrument_registry import InstrumentRegistry
from app.instruments.models import AssetClass, RiskProfile
from app.market.models import Candle, MarketSnapshot
from app.risk.position_sizing import FixedPercentPositionSizing
from app.risk.risk_manager import RiskManager
from app.strategies.signals import Signal


SESSION_KEY = 'EQUITY_US:test-session'


def make_candidate(action: str, metadata: dict | None = None) -> TradeCandidate:
    now = datetime(2026, 7, 4, 18, 0, tzinfo=timezone.utc)
    snapshot = MarketSnapshot('AAPL', 99.95, 100.05, 100.0, now)
    candle = Candle(
        'AAPL', 60, 99.0, 101.0, 98.5, 100.0,
        None, now, now,
    )
    return TradeCandidate(
        symbol='AAPL',
        snapshot=snapshot,
        candle=candle,
        signal=Signal(action, 0.8, 'test_signal', metadata=metadata),
        rank_reason='test',
        session_key=SESSION_KEY,
        directional_score=120.0,
    )


def build_instrument_registry(
    risk_profile: RiskProfile | None = None,
) -> InstrumentRegistry:
    settings = Settings(EQUITY_US_SYMBOLS='AAPL')
    actual = risk_profile or replace(
        EQUITY_US_CONFIG.risk,
        force_close_enabled=False,
    )
    return InstrumentRegistry(
        settings,
        risk_profiles={AssetClass.EQUITY_US: actual},
    )


@pytest.mark.parametrize('action', ['BUY', 'SELL'])
def test_candidate_economics_matches_risk_manager_trade_costs(action: str):
    settings = Settings(EQUITY_US_SYMBOLS='AAPL')
    registry = build_instrument_registry()
    sizing = FixedPercentPositionSizing()
    estimator = CandidateEconomicsEstimator(sizing, registry)
    risk_manager = RiskManager(settings, sizing, registry)
    candidate = make_candidate(action)

    evaluated = estimator.evaluate(candidate, 100000.0)
    plan = risk_manager.evaluate(
        signal=candidate.signal,
        snapshot=candidate.snapshot,
        account_equity=100000.0,
        session_key=candidate.session_key,
    )

    assert plan.approved
    assert plan.side == action
    assert plan.profile_key == 'us_intraday_fixed_v1'
    assert plan.amount == pytest.approx(evaluated.economics.position_value)
    assert plan.expected_gross_profit == pytest.approx(evaluated.economics.expected_gross_profit)
    assert plan.expected_net_profit == pytest.approx(evaluated.economics.expected_net_profit)
    assert plan.expected_net_profit_percent == pytest.approx(evaluated.economics.expected_net_profit_percent)
    assert plan.estimated_total_cost_percent == pytest.approx(evaluated.economics.estimated_total_cost_percent)


def test_effective_fixed_sl_tp_is_shared_across_all_stages():
    settings = Settings(EQUITY_US_SYMBOLS='AAPL')
    risk_profile = replace(EQUITY_US_CONFIG.risk, force_close_enabled=False)
    registry = build_instrument_registry(risk_profile)
    sizing = FixedPercentPositionSizing()
    estimator = CandidateEconomicsEstimator(sizing, registry)
    risk_manager = RiskManager(settings, sizing, registry)
    candidate = make_candidate('BUY', metadata={
        'atr_percent': 0.8,
        'snapshot_momentum_percent': 0.5,
        'session_move_percent': 0.3,
        'trend_strength_percent': 0.2,
        'close_position_percent': 90.0,
    })

    evaluated = estimator.evaluate(candidate, 100000.0)
    scored = CandidateTpFeasibilityEvaluator().evaluate(
        evaluated_candidate=evaluated,
        risk_profile=risk_profile,
    )
    plan = risk_manager.evaluate(
        signal=scored.candidate.signal,
        snapshot=scored.candidate.snapshot,
        account_equity=100000.0,
        session_key=scored.candidate.session_key,
        effective_sl_tp=scored.effective_sl_tp,
    )

    assert scored.effective_sl_tp is evaluated.effective_sl_tp
    assert scored.effective_sl_tp.source == 'us_intraday_fixed_v1'
    assert scored.effective_sl_tp.stop_loss_percent == 0.7
    assert scored.effective_sl_tp.take_profit_percent == 1.2
    assert plan.approved
    assert plan.profile_key == 'us_intraday_fixed_v1'
    assert plan.effective_stop_loss_percent == 0.7
    assert plan.effective_take_profit_percent == 1.2
    assert scored.tp_feasibility.effective_stop_loss_percent == 0.7
    assert scored.tp_feasibility.effective_take_profit_percent == 1.2


def test_risk_manager_builds_sell_levels_from_fixed_profile():
    settings = Settings(EQUITY_US_SYMBOLS='AAPL')
    registry = build_instrument_registry()
    manager = RiskManager(
        settings,
        FixedPercentPositionSizing(),
        registry,
    )
    candidate = make_candidate('SELL')
    plan = manager.evaluate(
        signal=candidate.signal,
        snapshot=candidate.snapshot,
        account_equity=100000.0,
        session_key=candidate.session_key,
    )

    assert plan.approved
    assert plan.side == 'SELL'
    assert plan.stop_loss == 100.7
    assert plan.take_profit == 98.8
    assert plan.stop_loss > candidate.snapshot.last
    assert plan.take_profit < candidate.snapshot.last
