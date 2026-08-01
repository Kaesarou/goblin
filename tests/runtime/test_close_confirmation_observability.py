from datetime import datetime, timezone

from app.execution.position_close_reason import PositionCloseReason
from app.execution.position_models import PositionCloseSignal
from app.runtime.async_broker_operations import AsyncBrokerOperationsCoordinator
from app.runtime.pending_close import CloseState, PendingClose
from app.runtime.resilient_broker_operations import (
    ResilientBrokerOperationsCoordinator,
)


NOW = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)


def pending_close(*, confirmation_checks: int = 0) -> PendingClose:
    return PendingClose(
        position_id='position-1',
        symbol='AIR.PA',
        signal=PositionCloseSignal(
            position_id='position-1',
            symbol='AIR.PA',
            side='BUY',
            reason=PositionCloseReason.INITIAL_STOP,
            detected_at=NOW,
            last_execution_price=99.0,
            executable_estimate=98.9,
            bid_at_detection=98.9,
            ask_at_detection=99.1,
            observed_spread_percent=0.2,
        ),
        source='websocket_position_guard',
        state=CloseState.PENDING_CONFIRMATION,
        requested_at=NOW,
        submitted_at=NOW,
        accepted_at=NOW,
        confirmation_checks=confirmation_checks,
    )


def test_confirming_absence_is_counted_as_a_confirmation_check(monkeypatch):
    captured = {}

    def capture_confirmation(
        self,
        pending,
        *,
        closed_at,
        source,
        broker_execution=None,
    ):
        captured['pending'] = pending
        captured['closed_at'] = closed_at
        captured['source'] = source
        captured['broker_execution'] = broker_execution

    monkeypatch.setattr(
        AsyncBrokerOperationsCoordinator,
        '_confirm_pending_close',
        capture_confirmation,
    )
    coordinator = object.__new__(ResilientBrokerOperationsCoordinator)

    coordinator._confirm_pending_close(
        pending_close(confirmation_checks=2),
        closed_at=NOW,
        source='runtime_portfolio_reconciliation',
    )

    confirmed = captured['pending']
    assert confirmed.confirmation_checks == 3
    assert confirmed.last_confirmation_at == NOW
    assert captured['closed_at'] == NOW
    assert captured['source'] == 'runtime_portfolio_reconciliation'
