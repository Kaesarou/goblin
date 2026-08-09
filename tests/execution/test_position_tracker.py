from datetime import UTC, datetime, timedelta

import pytest

from app.brokers.base import BrokerCloseExecution
from app.execution.managed_stop import ManagedProtectionType
from app.execution.position_close_reason import PositionCloseReason
from app.execution.position_models import (
    EntryPriceSource,
    ExitPriceSource,
    PositionCloseSignal,
    TrackedPosition,
)
from app.execution.position_tracker import PositionTracker
from app.market.models import MarketSnapshot
from app.risk.models import TradePlan


OPENED_AT = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)


def snapshot(
    *,
    bid: float,
    ask: float,
    last: float | None = None,
    at: datetime = OPENED_AT,
) -> MarketSnapshot:
    return MarketSnapshot(
        symbol='AMD',
        bid=bid,
        ask=ask,
        last=bid if last is None else last,
        timestamp=at,
    )


def plan(
    side: str,
    *,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    explicit_cost: float = 0.0,
    explicit_cost_percent: float = 0.0,
    breakeven_trigger: float = 0.0,
    trailing_trigger: float = 0.0,
    stale: bool = False,
) -> TradePlan:
    return TradePlan(
        approved=True,
        reason='test',
        symbol='AMD',
        side=side,
        amount=1_000.0,
        stop_loss=(
            stop_loss
            if stop_loss is not None
            else (99.0 if side == 'BUY' else 101.0)
        ),
        take_profit=(
            take_profit
            if take_profit is not None
            else (102.0 if side == 'BUY' else 98.0)
        ),
        estimated_explicit_cost=explicit_cost,
        estimated_explicit_cost_percent=explicit_cost_percent,
        estimated_spread_cost=2.0,
        estimated_total_cost=2.0 + explicit_cost,
        estimated_total_cost_percent=0.2 + explicit_cost_percent,
        breakeven_stop_enabled=breakeven_trigger > 0,
        breakeven_trigger_percent=breakeven_trigger,
        breakeven_buffer_percent=0.05,
        trailing_stop_enabled=trailing_trigger > 0,
        trailing_stop_trigger_percent=trailing_trigger,
        trailing_stop_distance_percent=0.4,
        trailing_stop_net_buffer_percent=0.05,
        stale_position_enabled=stale,
        stale_position_max_age_minutes=60,
        stale_position_min_favorable_move_percent=0.5,
        stale_position_buffer_percent=0.1,
    )


def open_position(
    tracker: PositionTracker,
    side: str,
    *,
    position_id: str = 'position-1',
    broker_fill: float | None = 100.0,
    executable_estimate: float | None = None,
    trade_plan: TradePlan | None = None,
) -> TrackedPosition:
    estimate = (
        executable_estimate
        if executable_estimate is not None
        else (100.1 if side == 'BUY' else 99.9)
    )
    return tracker.record_open_position(
        position_id=position_id,
        trade_plan=trade_plan or plan(side),
        signal_price=100.0,
        executable_entry_estimate=estimate,
        broker_entry_fill_price=broker_fill,
        opened_at=OPENED_AT,
    )


@pytest.mark.parametrize(
    ('side', 'before', 'hit', 'reason'),
    [
        ('BUY', (101.99, 102.20), (102.0, 102.20), PositionCloseReason.TAKE_PROFIT),
        ('SELL', (97.80, 98.01), (97.80, 98.0), PositionCloseReason.TAKE_PROFIT),
        ('BUY', (99.01, 99.20), (99.0, 99.20), PositionCloseReason.INITIAL_STOP),
        ('SELL', (100.80, 100.99), (100.80, 101.0), PositionCloseReason.INITIAL_STOP),
    ],
)
def test_tp_and_initial_stop_use_executable_side(
    side: str,
    before: tuple[float, float],
    hit: tuple[float, float],
    reason: PositionCloseReason,
):
    tracker = PositionTracker()
    open_position(tracker, side)

    assert tracker.evaluate_snapshot(snapshot(bid=before[0], ask=before[1])) == []
    signal = tracker.evaluate_snapshot(snapshot(bid=hit[0], ask=hit[1]))[0]

    assert signal.reason is reason
    assert signal.executable_estimate == (hit[0] if side == 'BUY' else hit[1])


def test_buy_ignores_last_and_ask_until_bid_reaches_tp():
    tracker = PositionTracker()
    open_position(tracker, 'BUY')

    assert tracker.evaluate_snapshot(
        snapshot(bid=101.8, ask=102.2, last=102.1)
    ) == []


def test_sell_ignores_last_and_bid_until_ask_reaches_tp():
    tracker = PositionTracker()
    open_position(tracker, 'SELL')

    assert tracker.evaluate_snapshot(
        snapshot(bid=97.7, ask=98.2, last=97.7)
    ) == []


def test_replay_can_limit_lifecycle_evaluation_to_due_positions():
    tracker = PositionTracker()
    open_position(tracker, 'BUY', position_id='not-due')
    open_position(tracker, 'BUY', position_id='due')

    signals = tracker.evaluate_snapshot(
        snapshot(bid=102.0, ask=102.2),
        position_ids={'due'},
    )

    assert [signal.position_id for signal in signals] == ['due']
    untouched = next(
        item
        for item in tracker.open_positions_snapshot()
        if item.position_id == 'not-due'
    )
    assert untouched.highest_executable_price == untouched.pnl_entry_price


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
def test_breakeven_activation_and_trigger_are_side_aware(side: str):
    tracker = PositionTracker()
    open_position(
        tracker,
        side,
        trade_plan=plan(
            side,
            explicit_cost_percent=0.10,
            breakeven_trigger=0.55,
        ),
    )
    activating = (
        snapshot(bid=100.56, ask=100.76)
        if side == 'BUY'
        else snapshot(bid=99.24, ask=99.44)
    )

    assert tracker.evaluate_snapshot(activating) == []
    position = tracker.open_positions_snapshot()[0]
    assert position.managed_stop_protection_type is ManagedProtectionType.BREAKEVEN
    assert position.stop_loss == pytest.approx(100.15 if side == 'BUY' else 99.85)

    trigger = (
        snapshot(bid=100.15, ask=100.35)
        if side == 'BUY'
        else snapshot(bid=99.65, ask=99.85)
    )
    signal = tracker.evaluate_snapshot(trigger)[0]
    assert signal.reason is PositionCloseReason.PROTECTED_BREAKEVEN


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
def test_trailing_activation_move_and_trigger_are_side_aware(side: str):
    tracker = PositionTracker()
    open_position(
        tracker,
        side,
        trade_plan=plan(side, trailing_trigger=1.0),
    )
    first = (
        snapshot(bid=101.2, ask=101.4)
        if side == 'BUY'
        else snapshot(bid=98.6, ask=98.8)
    )
    tracker.evaluate_snapshot(first)
    first_stop = tracker.open_positions_snapshot()[0].stop_loss
    assert first_stop == pytest.approx(100.7952 if side == 'BUY' else 99.1952)

    farther = (
        snapshot(bid=101.5, ask=101.7)
        if side == 'BUY'
        else snapshot(bid=98.3, ask=98.5)
    )
    tracker.evaluate_snapshot(farther)
    position = tracker.open_positions_snapshot()[0]
    assert position.managed_stop_protection_type is ManagedProtectionType.TRAILING
    assert position.stop_loss != first_stop

    trigger = (
        snapshot(bid=position.stop_loss, ask=position.stop_loss + 0.2)
        if side == 'BUY'
        else snapshot(bid=position.stop_loss - 0.2, ask=position.stop_loss)
    )
    assert tracker.evaluate_snapshot(trigger)[0].reason is (
        PositionCloseReason.PROTECTED_TRAILING
    )


def test_stale_and_force_close_use_executable_price_and_canonical_taxonomy():
    stale_tracker = PositionTracker()
    open_position(stale_tracker, 'SELL', trade_plan=plan('SELL', stale=True))
    stale = stale_tracker.evaluate_snapshot(
        snapshot(
            bid=99.2,
            ask=99.8,
            at=OPENED_AT + timedelta(minutes=61),
        )
    )[0]
    assert stale.reason is PositionCloseReason.STALE_EXIT
    assert stale.executable_estimate == 99.8

    force_tracker = PositionTracker()
    open_position(force_tracker, 'SELL')
    forced = force_tracker.evaluate_snapshot(
        snapshot(bid=99.0, ask=99.4),
        force_close=True,
    )[0]
    assert forced.reason is PositionCloseReason.SESSION_FORCE_CLOSE
    assert forced.executable_estimate == 99.4


def test_mfe_and_mae_are_calculated_from_executable_prices():
    tracker = PositionTracker()
    open_position(
        tracker,
        'SELL',
        trade_plan=plan('SELL', stop_loss=105.0, take_profit=95.0),
    )
    tracker.evaluate_snapshot(snapshot(bid=98.0, ask=98.5, last=98.0))
    tracker.evaluate_snapshot(snapshot(bid=100.5, ask=101.0, last=100.5))
    signal = PositionCloseSignal(
        position_id='position-1',
        symbol='AMD',
        side='SELL',
        reason=PositionCloseReason.MANUAL_OR_BROKER_CLOSE,
        detected_at=OPENED_AT + timedelta(minutes=2),
        last_execution_price=100.5,
        executable_estimate=101.0,
        bid_at_detection=100.5,
        ask_at_detection=101.0,
        observed_spread_percent=0.5,
    )

    closed = tracker.record_closed_position(signal)

    assert closed is not None
    assert closed.mfe_percent == pytest.approx(1.5)
    assert closed.mae_percent == pytest.approx(1.0)


def test_executable_prices_capture_spread_without_second_deduction():
    tracker = PositionTracker()
    open_position(
        tracker,
        'BUY',
        broker_fill=None,
        executable_estimate=100.1,
        trade_plan=plan(
            'BUY',
            explicit_cost=1.0,
            explicit_cost_percent=0.1,
        ),
    )
    signal = PositionCloseSignal(
        position_id='position-1',
        symbol='AMD',
        side='BUY',
        reason=PositionCloseReason.MANUAL_OR_BROKER_CLOSE,
        detected_at=OPENED_AT + timedelta(minutes=5),
        last_execution_price=100.9,
        executable_estimate=100.9,
        bid_at_detection=100.9,
        ask_at_detection=101.1,
        observed_spread_percent=0.198,
    )

    closed = tracker.record_closed_position(signal)

    assert closed is not None
    assert closed.entry_price_source is EntryPriceSource.EXECUTABLE_ESTIMATE
    assert closed.exit_price_source is ExitPriceSource.EXECUTABLE_ESTIMATE
    assert closed.pretrade_estimated_spread_cost == 2.0
    assert closed.explicit_costs_deducted == 1.0
    assert closed.net_pnl == pytest.approx(closed.gross_pnl - 1.0)


def test_broker_close_fill_has_priority_over_detection_estimate():
    tracker = PositionTracker()
    open_position(tracker, 'BUY', broker_fill=100.0)
    signal = PositionCloseSignal(
        position_id='position-1',
        symbol='AMD',
        side='BUY',
        reason=PositionCloseReason.TAKE_PROFIT,
        detected_at=OPENED_AT + timedelta(minutes=5),
        last_execution_price=102.0,
        executable_estimate=101.9,
        bid_at_detection=101.9,
        ask_at_detection=102.1,
        observed_spread_percent=0.196,
    )
    execution = BrokerCloseExecution(
        position_id='position-1',
        close_order_id='close-1',
        executed_exit_price=101.7,
        executed_at=OPENED_AT + timedelta(minutes=5, seconds=1),
        units=10.0,
        conversion_rate=1.0,
        amount=1_017.0,
        broker_response={'positions': []},
    )

    closed = tracker.record_closed_position(signal, broker_execution=execution)

    assert closed is not None
    assert closed.broker_exit_fill_price == 101.7
    assert closed.pnl_exit_price == 101.7
    assert closed.exit_price_source is ExitPriceSource.BROKER_FILL
    assert closed.executable_exit_estimate == 101.9


def test_restore_preserves_explicit_price_provenance():
    tracker = PositionTracker()
    position = TrackedPosition(
        position_id='position-1',
        symbol='AMD',
        side='BUY',
        amount=1_000.0,
        signal_price=100.0,
        executable_entry_estimate=100.1,
        broker_entry_fill_price=None,
        pnl_entry_price=100.1,
        entry_price_source=EntryPriceSource.EXECUTABLE_ESTIMATE,
        stop_loss=99.0,
        take_profit=102.0,
        opened_at=OPENED_AT,
    )

    tracker.restore_open_position(position)
    restored = tracker.open_positions_snapshot()[0]

    assert restored.highest_executable_price == 100.1
    assert restored.highest_last_execution_price == 100.0
    assert restored.broker_entry_fill_price is None
