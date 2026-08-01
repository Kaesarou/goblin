from dataclasses import replace
from datetime import UTC, datetime, timedelta

from app.backtesting.position_replay import replay_position
from app.execution.position_close_reason import PositionCloseReason
from app.execution.position_tracker import PositionTracker
from app.market.models import MarketSnapshot
from app.risk.models import TradePlan

NOW = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)


def snapshot(at: datetime, bid: float, ask: float) -> MarketSnapshot:
    return MarketSnapshot("AMD", bid, ask, bid, at)


def opened_position():
    tracker = PositionTracker()
    return tracker.record_open_position(
        position_id="position-1",
        trade_plan=TradePlan(
            approved=True,
            reason="test",
            symbol="AMD",
            side="SELL",
            amount=1_000.0,
            stop_loss=101.0,
            take_profit=98.0,
            estimated_explicit_cost=2.0,
            estimated_explicit_cost_percent=0.2,
            breakeven_stop_enabled=True,
            breakeven_trigger_percent=0.6,
            breakeven_buffer_percent=0.05,
            trailing_stop_enabled=True,
            trailing_stop_trigger_percent=1.0,
            trailing_stop_distance_percent=0.45,
            trailing_stop_net_buffer_percent=0.1,
        ),
        signal_price=100.0,
        executable_entry_estimate=99.9,
        broker_entry_fill_price=None,
        opened_at=NOW,
    )


def test_replay_label_is_identical_to_live_lifecycle_and_economics():
    prices = [
        snapshot(NOW + timedelta(minutes=1), 99.0, 99.2),
        snapshot(NOW + timedelta(minutes=2), 99.5, 99.7),
    ]
    position = opened_position()

    live = PositionTracker()
    live.restore_open_position(replace(position))
    live_signal = None
    for price in prices:
        signals = live.evaluate_snapshot(price)
        if signals:
            live_signal = signals[0]
            break
    assert live_signal is not None
    live_closed = live.record_closed_position(live_signal)

    replay = replay_position(position=position, snapshots=prices)

    assert replay.close_signal == live_signal
    assert replay.closed_position == live_closed
    assert replay.closed_position is not None
    assert replay.closed_position.close_reason is (PositionCloseReason.PROTECTED_BREAKEVEN)


def test_replay_keeps_incomplete_position_open_at_last_tick():
    position = opened_position()

    result = replay_position(
        position=position,
        snapshots=[snapshot(NOW + timedelta(minutes=1), 99.7, 99.9)],
    )

    assert result.closed_position is None
    assert result.close_signal is None
    assert result.final_position is not None
