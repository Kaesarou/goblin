from datetime import datetime, timezone
from typing import cast

from app.brokers.base import BrokerClient, OpenPositionResult
from app.execution.position_tracker import PositionTracker
from app.execution.trade_candidate import TradeCandidate
from app.execution.trade_executor import TradeExecutor
from app.journal.jsonl_journal import JsonlJournal
from app.market.models import Candle, MarketSnapshot
from app.risk.models import TradePlan
from app.risk.risk_manager import RiskManager
from app.runtime.candidate_flow import execute_ranked_candidates
from app.strategies.signals import Signal

TEST_SESSION_KEY = 'test-session'


def snapshot(symbol: str) -> MarketSnapshot:
    return MarketSnapshot(symbol=symbol, bid=99.9, ask=100.1, last=100.0, timestamp=datetime(2026, 6, 26, 16, 0, tzinfo=timezone.utc))


def candle(symbol: str) -> Candle:
    timestamp = datetime(2026, 6, 26, 16, 0, tzinfo=timezone.utc)
    return Candle(symbol=symbol, timeframe_seconds=60, open=99.0, high=101.0, low=98.5, close=100.0, volume=None, opened_at=timestamp, closed_at=timestamp)


def candidate(symbol: str, score: float) -> TradeCandidate:
    return TradeCandidate(
        symbol=symbol,
        snapshot=snapshot(symbol),
        candle=candle(symbol),
        signal=Signal(action='BUY', setup_quality=0.8, reason='test_candidate', metadata={'session_move_percent': 1.0, 'trend_strength_percent': 0.2, 'breakout_percent': 0.2, 'candle_range_percent': 0.5, 'close_position_percent': 90.0}),
        rank_reason=f'score={score}',
        session_key=TEST_SESSION_KEY,
    )


class FakeExecutionBroker(BrokerClient):
    def get_market_snapshots(self, symbols: list[str]) -> dict[str, MarketSnapshot]:
        raise NotImplementedError

    def get_market_snapshot(self, symbol: str) -> MarketSnapshot:
        raise NotImplementedError

    def get_account_equity(self) -> float:
        return 100_000.0

    def open_position(self, symbol: str, side: str, amount: float, stop_loss: float, take_profit: float) -> OpenPositionResult:
        raise NotImplementedError

    def close_position(self, position_id: str) -> None:
        raise NotImplementedError

    def is_position_open(self, position_id: str) -> bool:
        raise NotImplementedError


class FakeRiskManager:
    def __init__(self):
        self.opened_symbols: list[str] = []
        self.session_keys: list[str] = []

    def evaluate(self, signal, snapshot, account_equity, session_key):
        self.session_keys.append(session_key)
        return TradePlan(approved=True, reason=signal.reason, symbol=snapshot.symbol, side=signal.action, amount=500.0, stop_loss=99.0, take_profit=102.0, effective_stop_loss_percent=1.0, effective_take_profit_percent=2.0)

    def adjust_trade_plan_to_entry_price(self, *, trade_plan: TradePlan, entry_price: float) -> TradePlan:
        return trade_plan

    def instrument_profile_for(self, symbol: str) -> dict[str, str]:
        return {'symbol': symbol, 'asset_class': 'TEST'}

    def risk_profile_for(self, symbol: str) -> dict[str, str]:
        return {'symbol': symbol, 'asset_class': 'TEST'}

    def record_open_position(self, symbol: str, session_key: str) -> None:
        self.opened_symbols.append(symbol)
        self.session_keys.append(session_key)


class FakeExecutor:
    def __init__(self):
        self.executed_symbols: list[str] = []

    def execute(self, plan: TradePlan) -> OpenPositionResult | None:
        symbol = plan.symbol
        assert symbol is not None
        if symbol == 'FAIL':
            raise RuntimeError('simulated failure')
        self.executed_symbols.append(symbol)
        return OpenPositionResult(position_id=f'position-{symbol}', executed_entry_price=None)


class FakePositionTracker:
    def __init__(self):
        self.tracked_symbols: list[str] = []

    def record_open_position(self, position_id: str, trade_plan: TradePlan, entry_price: float) -> dict[str, str]:
        symbol = trade_plan.symbol
        assert symbol is not None
        self.tracked_symbols.append(symbol)
        return {'position_id': position_id, 'symbol': symbol}


class FakeJournal:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def write(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))


def test_execute_ranked_candidates_continues_after_candidate_execution_error():
    execution_broker = FakeExecutionBroker()
    risk_manager = FakeRiskManager()
    executor = FakeExecutor()
    position_tracker = FakePositionTracker()
    journal = FakeJournal()

    execute_ranked_candidates(
        candidates=[candidate('FAIL', 200.0), candidate('MSFT', 150.0)],
        execution_broker=execution_broker,
        risk_manager=cast(RiskManager, risk_manager),
        executor=cast(TradeExecutor, executor),
        position_tracker=cast(PositionTracker, position_tracker),
        trade_journal=cast(JsonlJournal, journal),
    )

    assert executor.executed_symbols == ['MSFT']
    assert risk_manager.opened_symbols == ['MSFT']
    assert position_tracker.tracked_symbols == ['MSFT']
    assert TEST_SESSION_KEY in risk_manager.session_keys
    event_types = [event_type for event_type, _ in journal.events]
    assert 'candidate_ranking' in event_types
    assert 'candidate_execution_error' in event_types
    assert 'position_opened' in event_types
