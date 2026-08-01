from datetime import UTC, datetime
from types import SimpleNamespace

from app.brokers.base import OpenPositionResult
from app.execution.position_tracker import PositionTracker
from app.execution.trade_candidate import TradeCandidate
from app.market.models import Candle, MarketSnapshot
from app.persistence.position_store import PositionStore
from app.risk.models import TradePlan
from app.runtime.async_candidate_execution import (
    AsyncCandidateExecutionCoordinator,
)
from app.runtime.broker_task_runner import BrokerTaskCompletion, BrokerTaskLane
from app.strategies.balanced_strategy_config import BalancedStrategyConfig
from app.strategies.signals import Signal

NOW = datetime(2026, 7, 31, 16, 0, tzinfo=UTC)


class Runner:
    def __init__(self) -> None:
        self.tasks: list[dict] = []

    def submit(self, **task):
        self.tasks.append(task)
        return task.get('task_id') or task['kind']


class Journal:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def write(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))


class Broker:
    def __init__(self) -> None:
        self.equity_calls = 0

    def get_account_equity(self) -> float:
        self.equity_calls += 1
        return 100_000.0


class Executor:
    def __init__(self) -> None:
        self.executed: list[str] = []

    def execute(self, plan: TradePlan) -> OpenPositionResult:
        assert plan.symbol is not None
        if plan.symbol == 'FAIL':
            raise RuntimeError('simulated order failure')
        self.executed.append(plan.symbol)
        return OpenPositionResult(
            position_id=f'position-{plan.symbol}',
            executed_entry_price=None,
        )


class RiskManager:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(
            max_open_positions=5,
            max_open_positions_per_symbol=1,
            max_trades_per_session=4,
        )
        self.open_positions = 0
        self.open_positions_by_symbol: dict[str, int] = {}
        self.opened_symbols: list[str] = []

    def instrument_profile_for(self, symbol: str):
        return SimpleNamespace(asset_class='CRYPTO')

    def risk_profile_for(self, symbol: str):
        return SimpleNamespace()

    def trades_for_session(self, session_key: str) -> int:
        return 0

    def evaluate(
        self,
        signal,
        snapshot,
        account_equity,
        session_key,
        effective_sl_tp=None,
    ) -> TradePlan:
        sell = signal.action == 'SELL'
        return TradePlan(
            approved=True,
            reason='test',
            symbol=snapshot.symbol,
            side=signal.action,
            amount=1_000.0,
            stop_loss=101.0 if sell else 99.0,
            take_profit=98.0 if sell else 102.0,
            estimated_explicit_cost=1.0,
            estimated_explicit_cost_percent=0.1,
        )

    def adjust_trade_plan_to_entry_price(
        self,
        *,
        trade_plan: TradePlan,
        entry_price: float,
    ) -> TradePlan:
        return trade_plan

    def record_open_position(self, symbol: str, session_key: str) -> None:
        self.open_positions += 1
        self.open_positions_by_symbol[symbol] = 1
        self.opened_symbols.append(symbol)


def candidate(symbol: str, side: str, price: float, score: float) -> TradeCandidate:
    snapshot = MarketSnapshot(
        symbol=symbol,
        bid=price - 0.1,
        ask=price + 0.1,
        last=price,
        timestamp=NOW,
    )
    candle = Candle(
        symbol=symbol,
        timeframe_seconds=60,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=None,
        opened_at=NOW,
        closed_at=NOW,
    )
    return TradeCandidate(
        symbol=symbol,
        snapshot=snapshot,
        candle=candle,
        signal=Signal(action=side, setup_quality=0.8, reason='test'),
        rank_reason='test',
        session_key='CRYPTO:24_7',
        probability_score=score,
        candidate_id=f'{symbol}-{side}',
        origin_candidate_id=f'{symbol}-{side}',
    )


def completion(task: dict, *, value=None, error=None) -> BrokerTaskCompletion:
    return BrokerTaskCompletion(
        task_id=task.get('task_id') or task['kind'],
        kind=task['kind'],
        lane=BrokerTaskLane.STANDARD,
        context=task['context'],
        value=value,
        error=error,
    )


def test_broker_entry_work_is_async_and_one_failure_does_not_block_another(
    tmp_path,
):
    runner = Runner()
    journal = Journal()
    broker = Broker()
    executor = Executor()
    risk = RiskManager()
    tracker = PositionTracker()
    coordinator = AsyncCandidateExecutionCoordinator(
        runner=runner,
        execution_broker=broker,
        executor=executor,
        risk_manager=risk,
        position_tracker=tracker,
        position_store=PositionStore(str(tmp_path / 'goblin.sqlite')),
        trade_journal=journal,
        strategy_profile=BalancedStrategyConfig(),
        cooldown_guard=None,
        candidate_economics_estimator=None,
        pending_entry_manager=None,
    )

    coordinator.submit_candidates(
        [
            candidate('FAIL', 'BUY', 100.0, 2.0),
            candidate('ETH', 'SELL', 200.0, 1.0),
        ],
        now=NOW,
    )

    assert broker.equity_calls == 0
    equity_task = runner.tasks.pop()
    coordinator.handle_completion(
        completion(equity_task, value=equity_task['operation']()),
        now=NOW,
    )
    assert broker.equity_calls == 1
    assert [task['kind'] for task in runner.tasks] == [
        'open_order',
        'open_order',
    ]

    failed_task, successful_task = runner.tasks
    try:
        failed_task['operation']()
    except RuntimeError as exc:
        coordinator.handle_completion(
            completion(failed_task, error=exc),
            now=NOW,
        )
    coordinator.handle_completion(
        completion(
            successful_task,
            value=successful_task['operation'](),
        ),
        now=NOW,
    )

    positions = tracker.open_positions_snapshot()
    assert [position.symbol for position in positions] == ['ETH']
    assert positions[0].pnl_entry_price == 199.9
    assert positions[0].executable_entry_estimate == 199.9
    assert executor.executed == ['ETH']
    assert risk.opened_symbols == ['ETH']
    assert [event for event, _ in journal.events].count(
        'candidate_execution_error'
    ) == 1
    assert [event for event, _ in journal.events].count('position_opened') == 1
