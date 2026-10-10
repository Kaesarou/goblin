from datetime import datetime, timezone

from app.v3.config import EconomicsConfig, EconomicsPolicy
from app.v3.economics import (
    ALPACA_FIXED_FEE_ASSUMPTION_PER_REQUEST,
    ETORO_FIXED_FEE_ASSUMPTION_PER_REQUEST,
    BrokerCostSchedule,
    EconomicsModel,
    broker_cost_schedule,
)
from app.v3.models import MarketState

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


def market():
    return MarketState("AAPL", NOW, 99.9, 100.1, 100, 98, 102, 0.001, 0.01)


def test_research_gross_policy_can_measure_edge_even_when_broker_net_is_negative():
    model = EconomicsModel(
        EconomicsConfig(policy=EconomicsPolicy.RESEARCH_GROSS_EDGE),
        BrokerCostSchedule(commission_pct_per_fill=0.003),
    )
    decision = model.estimate(
        market=market(),
        notional=1_000,
        expected_gross_capture_pct=0.002,
        side="BUY",
    )
    assert decision.allowed
    assert decision.estimated_net_capture < 0


def test_require_net_positive_rejects_same_trade():
    model = EconomicsModel(
        EconomicsConfig(policy=EconomicsPolicy.REQUIRE_NET_POSITIVE),
        BrokerCostSchedule(commission_pct_per_fill=0.003),
    )
    decision = model.estimate(
        market=market(),
        notional=1_000,
        expected_gross_capture_pct=0.002,
        side="BUY",
    )
    assert not decision.allowed


def test_broker_cost_schedule_keeps_etoro_and_alpaca_assumptions_separate():
    etoro = broker_cost_schedule("etoro_demo")
    alpaca = broker_cost_schedule("alpaca_demo")

    assert etoro.fixed_fee_per_fill == ETORO_FIXED_FEE_ASSUMPTION_PER_REQUEST
    assert alpaca.fixed_fee_per_fill == ALPACA_FIXED_FEE_ASSUMPTION_PER_REQUEST
    assert etoro.fixed_fee_per_fill == 1.0
    assert alpaca.fixed_fee_per_fill == 0.0
