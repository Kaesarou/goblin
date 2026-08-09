from dataclasses import replace
from datetime import datetime, timezone

from app.config.settings import Settings
from app.execution.candidate_economics import (
    CandidateEconomics,
    EvaluatedTradeCandidate,
)
from app.execution.candidate_selector import CandidateSelectionConfig
from app.execution.trade_candidate import TradeCandidate
from app.instruments.instrument_registry import InstrumentRegistry
from app.instruments.models import AssetClass
from app.market.models import Candle, MarketSnapshot
from app.risk.position_sizing import FixedPercentPositionSizing
from app.risk.risk_manager import RiskManager
from app.runtime.candidate_flow import (
    select_evaluated_trade_candidates_with_strategy_profile,
    select_trade_candidates_with_strategy_profile,
)
from app.strategies.balanced_strategy_config import BalancedStrategyConfig
from app.strategies.signals import Signal


def make_candidate(symbol: str, score: float) -> TradeCandidate:
    now = datetime(2026, 7, 4, 18, 0, tzinfo=timezone.utc)
    return TradeCandidate(
        symbol=symbol,
        snapshot=MarketSnapshot(symbol, 99.95, 100.05, 100.0, now),
        candle=Candle(
            symbol,
            60,
            99.5,
            100.2,
            99.4,
            100.0,
            None,
            now,
            now,
        ),
        signal=Signal(
            action='BUY',
            setup_quality=0.8,
            reason='test_signal',
            metadata={
                'session_move_percent': 1.0,
                'trend_strength_percent': 0.2,
                'breakout_percent': 0.1,
                'candle_range_percent': 0.8,
                'close_position_percent': 85.0,
            },
        ),
        rank_reason=f'test_score={score}',
        directional_score=score,
    )


def make_evaluated_candidate(symbol: str, score: float) -> EvaluatedTradeCandidate:
    return EvaluatedTradeCandidate(
        candidate=replace(
            make_candidate(symbol=symbol, score=score),
            probability_score=40.0,
            touch_probability=0.40,
            direction_probability=0.50,
            tp_probability=0.20,
            sl_probability=0.20,
            neither_probability=0.60,
            direction_break_even_probability=0.40,
            direction_edge=score / 1000,
            outcome_probability_model_version='outcome_probability_v2',
            managed_protection_probability=0.60,
            managed_positive_probability=0.50,
            managed_expected_net_return_percent=score / 1000 + 0.05,
            managed_edge=score / 1000,
            managed_outcome_model_version='managed_outcome_v1',
            managed_outcome_metadata={
                'minimum_protection_probability': 0.40,
                'minimum_positive_probability': 0.30,
                'minimum_expected_net_return_percent': 0.05,
            },
        ),
        economics=CandidateEconomics(
            position_value=100.0,
            expected_gross_profit=2.0,
            expected_net_profit=1.0,
            expected_net_profit_percent=0.5,
            estimated_total_cost=1.0,
            estimated_total_cost_percent=1.0,
            min_expected_net_profit_percent=0.1,
            required_min_expected_net_profit_amount=0.1,
        ),
    )


def build_risk_manager() -> RiskManager:
    settings = Settings(
        EQUITY_US_SYMBOLS='AAPL,MSFT,NVDA',
        EQUITY_EU_SYMBOLS='SAP.DE,ASML.NV',
    )
    return RiskManager(
        settings=settings,
        position_sizing_strategy=FixedPercentPositionSizing(),
        instrument_registry=InstrumentRegistry(settings),
    )


def build_strategy_profile() -> BalancedStrategyConfig:
    profile = BalancedStrategyConfig()
    return replace(
        profile,
        candidate_selection_configs={
            AssetClass.CRYPTO: CandidateSelectionConfig(top_n=2),
            AssetClass.EQUITY_US: CandidateSelectionConfig(top_n=2),
            AssetClass.EQUITY_EU: CandidateSelectionConfig(top_n=1),
        },
    )


def test_profile_candidate_selection_rejects_raw_overflow_with_top_n_reason():
    result = select_trade_candidates_with_strategy_profile(
        candidates=[
            make_candidate('NVDA', score=110.0),
            make_candidate('AAPL', score=130.0),
            make_candidate('MSFT', score=120.0),
        ],
        risk_manager=build_risk_manager(),
        strategy_profile=build_strategy_profile(),
    )

    assert [candidate.symbol for candidate in result.selected_candidates] == [
        'AAPL',
        'MSFT',
    ]
    assert len(result.rejected_candidates) == 1
    assert result.rejected_candidates[0].candidate.symbol == 'NVDA'
    assert result.rejected_candidates[0].reason == (
        'candidate_selection_outside_top_n'
    )


def test_profile_candidate_selection_rejects_evaluated_overflow_with_top_n_reason():
    result = select_evaluated_trade_candidates_with_strategy_profile(
        evaluated_candidates=[
            make_evaluated_candidate('NVDA', score=110.0),
            make_evaluated_candidate('AAPL', score=130.0),
            make_evaluated_candidate('MSFT', score=120.0),
        ],
        risk_manager=build_risk_manager(),
        strategy_profile=build_strategy_profile(),
    )

    assert [
        item.candidate.symbol for item in result.selected_candidates
    ] == ['AAPL', 'MSFT']
    assert len(result.rejected_candidates) == 1
    assert (
        result.rejected_candidates[0].evaluated_candidate.candidate.symbol
        == 'NVDA'
    )
    assert result.rejected_candidates[0].reason == (
        'candidate_selection_outside_top_n'
    )


def test_eu_top_one_does_not_reduce_us_top_two():
    result = select_trade_candidates_with_strategy_profile(
        candidates=[
            make_candidate('AAPL', score=130.0),
            make_candidate('MSFT', score=120.0),
            make_candidate('SAP.DE', score=125.0),
            make_candidate('ASML.NV', score=115.0),
        ],
        risk_manager=build_risk_manager(),
        strategy_profile=build_strategy_profile(),
    )

    assert {item.symbol for item in result.selected_candidates} == {
        'AAPL',
        'MSFT',
        'SAP.DE',
    }
    rejected = {
        item.candidate.symbol: item.reason
        for item in result.rejected_candidates
    }
    assert rejected == {'ASML.NV': 'candidate_selection_outside_top_n'}
