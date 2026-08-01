from datetime import UTC, datetime
from types import SimpleNamespace

from app.execution.position_close_reason import PositionCloseReason
from app.execution.position_tracker import PositionTracker
from app.market.models import MarketSnapshot
from app.persistence.pending_close_store import PendingCloseStore
from app.persistence.position_store import PositionStore
from app.risk.models import TradePlan
from app.runtime.async_broker_operations import AsyncBrokerOperationsCoordinator


NOW = datetime(2026, 7, 31, 19, 50, tzinfo=UTC)


class Runner:
    def __init__(self):
        self.tasks = []

    def submit(self, **task):
        self.tasks.append(task)

    def has_pending_kind(self, kind):
        return False


class Journal:
    def __init__(self):
        self.events = []

    def write(self, event_type, payload):
        self.events.append((event_type, payload))


class Executor:
    def __init__(self):
        self.calls = []

    def close(self, position_id):
        self.calls.append(position_id)
        raise AssertionError('broker close must execute only on the task runner')


def test_force_close_is_side_aware_and_queued_off_websocket_consumer(tmp_path):
    path = str(tmp_path / 'goblin.sqlite')
    tracker = PositionTracker()
    position = tracker.record_open_position(
        position_id='p1',
        trade_plan=TradePlan(
            approved=True,
            reason='test',
            symbol='AAPL',
            side='SELL',
            amount=100.0,
            stop_loss=102.0,
            take_profit=98.0,
        ),
        signal_price=100.0,
        executable_entry_estimate=99.9,
        broker_entry_fill_price=None,
        opened_at=NOW,
    )
    position_store = PositionStore(path)
    position_store.save_open_position(position)
    runner = Runner()
    executor = Executor()
    journal = Journal()
    coordinator = AsyncBrokerOperationsCoordinator(
        runner=runner,
        execution_broker=SimpleNamespace(),
        rest_market_data=SimpleNamespace(),
        executor=executor,
        position_tracker=tracker,
        risk_manager=SimpleNamespace(),
        position_store=position_store,
        pending_close_store=PendingCloseStore(path),
        cooldown_store=SimpleNamespace(),
        trade_journal=journal,
        market_data_coordinator=SimpleNamespace(),
        is_broker_authorization_error=lambda exc: False,
    )

    coordinator.on_snapshot(
        snapshot=MarketSnapshot('AAPL', 99.0, 99.4, 99.0, NOW),
        session_decision=SimpleNamespace(
            force_close_required=True,
            reason='force_close_before_session_end',
            time_until_session_end_minutes=10.0,
        ),
    )

    assert executor.calls == []
    assert len(runner.tasks) == 1
    pending = coordinator._pending_closes['p1']
    assert pending.signal.reason is PositionCloseReason.SESSION_FORCE_CLOSE
    assert pending.signal.executable_estimate == 99.4
    assert tracker.has_open_positions()
