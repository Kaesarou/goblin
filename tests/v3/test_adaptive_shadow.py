from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.market.models import MarketSnapshot
from app.v3.adaptive_policy import AdaptiveConfig, Regime
from app.v3.adaptive_shadow import ShadowExperiment, _ShadowPortfolio
from app.v3.config import etoro5_research_config
from app.v3.economics import BrokerCostSchedule
from app.v3.models import DecisionBatch, ExecutionStyle, IntentPurpose, MarketState, OrderIntent
from tests.v3.test_planner import _planner

NOW = datetime(2026, 11, 19, 14, 30, tzinfo=timezone.utc)
FEATURE = SimpleNamespace(forager_volatility=.02, ema_readiness=-.02)


class Journal:
    suppressed_count = failed_count = 0

    def __init__(self):
        self.events = []

    def write(self, event, payload):
        self.events.append((event, payload))


def market(at, price=90., symbol="AAPL", allowed=True):
    return MarketState(symbol, at, price, price+.1, price+.05, 100., 101.,
                       .001, .01, entry_allowed=allowed)


def arm(policy=None, planner=None, fee=1.):
    config = etoro5_research_config()
    journal = Journal()
    portfolio = _ShadowPortfolio(name="test", planner=planner or _planner(config),
                                 config=config,
                                 cost_schedule=BrokerCostSchedule(fixed_fee_per_fill=fee),
                                 emit=journal.write, policy=policy)
    return portfolio, journal


def step(p, at, *, price=90., session="day1", regime=Regime.NORMAL,
         allowed=True, symbols=("AAPL",)):
    p.step(asof=at, session=session, markets={s: market(at, price, s, allowed) for s in symbols},
           features={s: FEATURE for s in symbols}, regime=regime)


def test_baseline_fills_only_on_next_quote_and_includes_spread_and_fees():
    p, journal = arm()
    step(p, NOW)
    assert p.book.active_for_symbol("AAPL") is None
    assert len(p.pending) == 1
    step(p, NOW + timedelta(minutes=1), price=89.)
    inv = p.book.active_for_symbol("AAPL")
    assert inv is not None
    assert inv.average_entry_price == pytest.approx(89.1)
    assert p.fees == 1.
    assert p.equity() == pytest.approx(100_000 - 1. - inv.total_units * .1)
    filled = [x for event, x in journal.events if event == "adaptive_shadow_fill"]
    assert len(filled) == 1
    assert filled[0]["planned_at"] == NOW
    assert filled[0]["asof"] == NOW + timedelta(minutes=1)


def test_missing_minute_and_session_boundary_expire_pending_intents():
    p, _ = arm()
    step(p, NOW)
    step(p, NOW + timedelta(minutes=2), price=89.)
    assert p.book.active_for_symbol("AAPL") is None
    step(p, NOW + timedelta(minutes=3), price=88., session="day2")
    assert p.book.active_for_symbol("AAPL") is None
    step(p, NOW + timedelta(minutes=4), price=87., session="day2")
    assert p.book.active_for_symbol("AAPL") is not None


def test_adaptive_gates_regime_and_sector_at_plan_and_fill():
    p, _ = arm(AdaptiveConfig())
    symbols = ("AAPL", "MSFT", "NVDA")
    step(p, NOW, regime=Regime.UNKNOWN, symbols=symbols)
    assert not p.pending
    step(p, NOW+timedelta(minutes=1), symbols=symbols)
    assert len(p.pending) == 2
    step(p, NOW+timedelta(minutes=2), price=89., regime=Regime.SYSTEMIC_STRESS, symbols=symbols)
    assert not p.portfolio().inventories
    assert p.counts["fill_gate_rejections"] == 2
    assert not p.pending


def test_buy_rechecks_fill_time_caps_after_mark_and_commission():
    p, _ = arm()
    m = market(NOW)
    p.marks[m.symbol], p.mark_times[m.symbol] = m.bid, m.asof
    intent = OrderIntent("1", IntentPurpose.INITIAL_ENTRY, m.symbol, "BUY",
                         20_000., m.asof, ExecutionStyle.PASSIVE_LIMIT)
    assert p._buy(intent, m)
    inv = p.book.active_for_symbol(m.symbol)
    assert inv.total_notional < 4_000
    assert p.portfolio().inventory_for(m.symbol).wallet_exposure_pct <= .04 + 1e-12
    # Missing fresh mark prevents subsequent simulated risk.
    other = market(NOW+timedelta(minutes=1), symbol="MSFT")
    second = replace(intent, intent_id="2", symbol=other.symbol)
    assert not p._buy(second, other)


class QuietPlanner:
    def plan_symbol(self, **_):
        return DecisionBatch()

    def plan_existing_inventory(self, **_):
        return DecisionBatch()


def test_age_uses_session_transitions_and_exits_at_next_quote():
    p, journal = arm(AdaptiveConfig(stale_after_sessions=1, exit_after_sessions=2),
                     planner=QuietPlanner())
    step(p, NOW)
    m = market(NOW, price=100.)
    p.marks[m.symbol], p.mark_times[m.symbol] = m.bid, m.asof
    intent = OrderIntent("initial", IntentPurpose.INITIAL_ENTRY, m.symbol, "BUY",
                         1000., m.asof, ExecutionStyle.PASSIVE_LIMIT)
    assert p._buy(intent, m)
    assert p._age(p.book.active_for_symbol(m.symbol)) == 0
    step(p, NOW+timedelta(days=1), price=80., session="day2")
    assert p._age(p.book.active_for_symbol(m.symbol)) == 1
    assert not p.pending
    due = NOW+timedelta(days=2)
    step(p, due, price=75., session="day3")
    assert p.pending[0].reduce_only
    assert p.pending[0].metadata["adaptive_reason"] == "time_exit"
    assert p.book.active_for_symbol(m.symbol) is not None
    step(p, due+timedelta(minutes=1), price=74., session="day3")
    assert p.book.active_for_symbol(m.symbol) is None
    assert p.counts["time_exit"] == 1
    assert not p._initial_allowed(m.symbol, Regime.NORMAL, ())
    assert p.equity() < 100_000
    assert any(event == "adaptive_shadow_lifecycle" for event, _ in journal.events)


def test_stress_reduction_is_once_per_inventory():
    p, _ = arm(AdaptiveConfig(stale_after_sessions=1, exit_after_sessions=3),
                planner=QuietPlanner())
    step(p, NOW)
    m = market(NOW, price=100.)
    p.marks[m.symbol], p.mark_times[m.symbol] = m.bid, m.asof
    assert p._buy(OrderIntent("entry", IntentPurpose.INITIAL_ENTRY, m.symbol, "BUY",
                               1000., m.asof, ExecutionStyle.PASSIVE_LIMIT), m)
    original = p.book.active_for_symbol(m.symbol).total_units
    day2 = NOW+timedelta(days=1)
    step(p, day2, session="day2", regime=Regime.SYSTEMIC_STRESS, price=80.)
    step(p, day2+timedelta(minutes=1), session="day2", regime=Regime.SYSTEMIC_STRESS, price=79.)
    assert p.book.active_for_symbol(m.symbol).total_units == pytest.approx(original/2)
    assert p.counts["stress_reduction"] == 1
    assert not p.pending


def test_benchmark_high_frequency_sampling_retains_causal_momentum():
    config = etoro5_research_config()
    x = ShadowExperiment(planner=_planner(config), config=config,
                         cost_schedule=BrokerCostSchedule(), journal=Journal(),
                         benchmark_symbols=("SPY",))
    for minute in range(17):
        for tick in range(250):
            at = NOW+timedelta(minutes=minute, milliseconds=tick*200)
            price = 100+minute
            x.observe_benchmark(MarketSnapshot("SPY", price, price, price, at))
    assert len(x.benchmarks["SPY"]) == 17
    # The minute-16 quote exists in memory but can't enter the minute-16 decision.
    ret, momentum, symbol = x._benchmark_context(NOW+timedelta(minutes=16), NOW)
    assert symbol == "SPY"
    assert ret == pytest.approx(.15)
    assert momentum == pytest.approx(.15)


def test_independent_arms_do_not_share_inventories():
    config = etoro5_research_config()
    x = ShadowExperiment(planner=_planner(config), config=config,
                         cost_schedule=BrokerCostSchedule(), journal=Journal(),
                         benchmark_symbols=("SPY",))
    for minute, price in [(0, 90.), (1, 89.)]:
        at = NOW+timedelta(minutes=minute+1)
        x.step(asof=at, session="day1", session_start=NOW,
               expected={"AAPL"}, markets={"AAPL": market(at, price)},
               features={"AAPL": FEATURE})
    assert x.arms[0].book.active_for_symbol("AAPL") is not None
    assert x.arms[1].book.active_for_symbol("AAPL") is None
    assert x.metrics()["context"].regime == Regime.UNKNOWN
