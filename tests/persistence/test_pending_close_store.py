from datetime import UTC, datetime

from app.execution.position_close_reason import PositionCloseReason
from app.execution.position_models import (
    EntryPriceSource,
    PositionCloseSignal,
    TrackedPosition,
)
from app.persistence.pending_close_store import PendingCloseStore
from app.persistence.position_store import PositionStore
from app.runtime.pending_close import CloseState, PendingClose


NOW = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)


def tracked_position(position_id: str = 'position-1') -> TrackedPosition:
    return TrackedPosition(
        position_id=position_id,
        symbol='BTC',
        side='BUY',
        amount=500.0,
        signal_price=100.0,
        executable_entry_estimate=100.1,
        broker_entry_fill_price=None,
        pnl_entry_price=100.1,
        entry_price_source=EntryPriceSource.EXECUTABLE_ESTIMATE,
        stop_loss=99.0,
        take_profit=102.0,
        opened_at=NOW,
    )


def pending_close(position_id: str = 'position-1') -> PendingClose:
    return PendingClose(
        position_id=position_id,
        symbol='BTC',
        signal=PositionCloseSignal(
            position_id=position_id,
            symbol='BTC',
            side='BUY',
            reason=PositionCloseReason.PROTECTED_BREAKEVEN,
            detected_at=NOW,
            last_execution_price=99.0,
            executable_estimate=98.9,
            bid_at_detection=98.9,
            ask_at_detection=99.1,
            observed_spread_percent=0.202,
            metadata={'trigger': 'managed_stop'},
        ),
        source='websocket_position_guard',
        state=CloseState.SUBMISSION_UNKNOWN,
        requested_at=NOW,
        submitted_at=NOW,
        close_order_id='close-123',
        reference_id='ref-123',
        confirmation_checks=2,
        last_confirmation_at=NOW,
        last_error='network timeout',
        metadata={'session_decision_reason': 'open'},
    )


def test_pending_close_store_round_trips_quotes_taxonomy_and_provenance(tmp_path):
    store = PendingCloseStore(str(tmp_path / 'goblin.sqlite'))
    store.save(pending_close())

    restored = store.load_all()[0]

    assert restored.signal.reason is PositionCloseReason.PROTECTED_BREAKEVEN
    assert restored.signal.last_execution_price == 99.0
    assert restored.signal.executable_estimate == 98.9
    assert restored.signal.bid_at_detection == 98.9
    assert restored.signal.ask_at_detection == 99.1
    assert restored.signal.metadata == {'trigger': 'managed_stop'}
    assert restored.close_order_id == 'close-123'
    assert restored.confirmation_checks == 2
    assert restored.metadata == {'session_decision_reason': 'open'}


def test_confirmed_close_deletes_pending_and_open_position_atomically(tmp_path):
    path = str(tmp_path / 'goblin.sqlite')
    position_store = PositionStore(path)
    pending_store = PendingCloseStore(path)
    position_store.save_open_position(tracked_position())
    pending_store.save(pending_close())

    pending_store.delete_with_open_position('position-1')

    assert pending_store.load_all() == []
    assert position_store.load_open_positions() == []
