import pytest
from datetime import datetime, timezone

from app.config.settings import Settings
from app.execution.candidate_economics import CandidateEconomicsEstimator
from app.execution.trade_candidate import TradeCandidate
from app.instruments.instrument_registry import InstrumentRegistry
from app.market.models import Candle, MarketSnapshot
from app.risk.position_sizing import FixedPercentPositionSizing
from app.strategies.signals import Signal


def make_candidate(symbol: str) -> TradeCandidate:
    now = datetime(2026, 6, 26, 15, 30, tzinfo=timezone.utc)
    snapshot = MarketSnapshot(
        symbol=symbol,
        bid=99.95,
        ask=100.05,
        last=100.0,
        timestamp=now,
    )
    candle = Candle(
        symbol=symbol,
        timeframe_seconds=60,
        open=99.0,
        high=101.0,
        low=98.5,
        close=100.0,
        volume=None,
        opened_at=now,
        closed_at=now,
    )
    signal = Signal(
        action='BUY',
        setup_quality=0.8,
        reason='test_signal',
    )
    return TradeCandidate(
        symbol=symbol,
        snapshot=snapshot,
        candle=candle,
        signal=signal,
        rank_reason='test',
    )


def test_candidate_economics_uses_canonical_fixed_us_profile_and_costs():
    settings = Settings(EQUITY_US_SYMBOLS='AAPL')
    estimator = CandidateEconomicsEstimator(
        position_sizing_strategy=FixedPercentPositionSizing(),
        instrument_registry=InstrumentRegistry(settings),
    )

    evaluated = estimator.evaluate(
        candidate=make_candidate('AAPL'),
        account_equity=100000.0,
    )

    assert evaluated.economics.position_value == 750.0
    assert evaluated.economics.effective_take_profit_percent == 1.2
    assert evaluated.economics.effective_stop_loss_percent == 0.7
    assert evaluated.economics.expected_gross_profit == pytest.approx(9.0)
    assert evaluated.economics.estimated_total_cost == pytest.approx(3.0)
    assert evaluated.economics.expected_net_profit == pytest.approx(6.0)
    assert evaluated.economics.expected_net_profit_percent == pytest.approx(0.8)
    assert evaluated.economics.required_min_expected_net_profit_amount == pytest.approx(0.75)
