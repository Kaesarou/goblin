from datetime import datetime, timezone

from app.brokers.base import OpenPositionResult
from app.execution.trade_candidate import TradeCandidate
from app.runtime.candidate_flow import execute_ranked_candidates
from app.market.models import Candle, MarketSnapshot
from app.risk.models import TradePlan
from app.risk.risk_manager import RiskManager
from app.strategies.signals import Signal

SESSION_KEY = 'test-session'


def snapshot() -> MarketSnapshot:
    return MarketSnapshot(symbol='AAPL', bid=241.7, ask=241.9, last=241.8, timestamp=datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc))


def candle() -> Candle:
    timestamp = datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc)
    return Candle(symbol='AAPL', timeframe_seconds=60, open=240.0, high=242.0, low=239.0, close=241.8, volume=None, opened_at=timestamp, closed_at=timestamp)


def candidate() -> TradeCandidate:
    return TradeCandidate(symbol='AAPL', snapshot=snapshot(), candle=candle(), signal=Signal(action='BUY', setup_quality=0.8, reason='test'), rank_reason='test', session_key=SESSION_KEY)


class FakeBroker:
    def get_account_equity(self) -> float:
        return 100_000.0


class FakeRiskManager:
    def __init__(self):
        self.recorded = []

    def evaluate(self, signal, snapshot, account_equity, session_key):
        return TradePlan(approved=True, reason='test', symbol=snapshot.symbol, side=signal.action, amount=100.0, stop_loss=239.8656, take_profit=245.1852, effective_stop_loss_percent=0.8, effective_take_profit_percent=1.4)

    def adjust_trade_plan_to_entry_price(self, *, trade_plan: TradePlan, entry_price: float) -> TradePlan:
        return RiskManager.adjust_trade_plan_to_entry_price(None, trade_plan=trade_plan, entry_price=entry_price)

    def instrument_profile_for(self, symbol):
        return {'symbol': symbol}

    def risk_profile_for(self, symbol):
        return {'symbol': symbol}

    def record_open_position(self, symbol: str, session_key: str) -> None:
        self.recorded.append((symbol, session_key))


class FakeExecutor:
    def __init__(self, result: OpenPositionResult):
        self.result = result

    def execute(self, plan: TradePlan):
        return self.result


class FakePositionTracker:
    def __init__(self):
        self.calls = []

    def record_open_position(self, position_id: str, trade_plan: TradePlan, entry_price: float):
        self.calls.append((position_id, trade_plan, entry_price))
        return {'position_id': position_id, 'symbol': trade_plan.symbol, 'entry_price': entry_price, 'stop_loss': trade_plan.stop_loss, 'take_profit': trade_plan.take_profit}


class FakeJournal:
    def __init__(self):
        self.events = []

    def write(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))


def opened_event(journal: FakeJournal):
    return [payload for event_type, payload in journal.events if event_type == 'position_opened'][0]


def test_candidate_flow_tracks_position_from_broker_entry_price():
    tracker = FakePositionTracker()
    journal = FakeJournal()

    execute_ranked_candidates(candidates=[candidate()], execution_broker=FakeBroker(), risk_manager=FakeRiskManager(), executor=FakeExecutor(OpenPositionResult('p1', 238.0)), position_tracker=tracker, trade_journal=journal)

    position_id, adjusted_plan, entry_price = tracker.calls[0]
    event = opened_event(journal)

    assert position_id == 'p1'
    assert entry_price == 238.0
    assert adjusted_plan.stop_loss == 236.096
    assert adjusted_plan.take_profit == 241.332
    assert event['entry_price_source'] == 'broker_execution'
    assert event['planned_entry_price'] == 241.8
    assert event['executed_entry_price'] == 238.0
    assert event['effective_entry_price'] == 238.0
    assert event['execution_slippage_percent'] == -1.5715


def test_candidate_flow_falls_back_to_snapshot_entry_price_when_broker_price_is_missing():
    tracker = FakePositionTracker()
    journal = FakeJournal()

    execute_ranked_candidates(candidates=[candidate()], execution_broker=FakeBroker(), risk_manager=FakeRiskManager(), executor=FakeExecutor(OpenPositionResult('p1', None)), position_tracker=tracker, trade_journal=journal)

    _, adjusted_plan, entry_price = tracker.calls[0]
    event = opened_event(journal)

    assert entry_price == 241.8
    assert adjusted_plan.stop_loss == 239.8656
    assert adjusted_plan.take_profit == 245.1852
    assert event['entry_price_source'] == 'snapshot_fallback'
    assert event['executed_entry_price'] is None
    assert event['effective_entry_price'] == 241.8
