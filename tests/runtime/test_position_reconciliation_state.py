from datetime import datetime, timedelta, timezone

from app.execution.position_models import EntryPriceSource, TrackedPosition
from app.runtime.position_reconciliation_state import (
    PositionReconciliationTracker,
)


def position(opened_at: datetime) -> TrackedPosition:
    return TrackedPosition(
        position_id='p-1',
        symbol='BTC',
        side='BUY',
        amount=100.0,
        signal_price=100.0,
        executable_entry_estimate=100.1,
        broker_entry_fill_price=None,
        pnl_entry_price=100.1,
        entry_price_source=EntryPriceSource.EXECUTABLE_ESTIMATE,
        stop_loss=99.0,
        take_profit=102.0,
        opened_at=opened_at,
    )


def test_reconciliation_requires_grace_and_three_spaced_absences():
    opened_at = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    tracked = position(opened_at)
    tracker = PositionReconciliationTracker(
        grace_seconds=30,
        required_misses=3,
        minimum_miss_interval_seconds=10,
    )

    grace = tracker.observe(
        positions=[tracked],
        open_states={'p-1': False},
        observed_at=opened_at + timedelta(seconds=5),
    )
    assert grace.grace_ignored == (tracked,)
    assert not grace.confirmed_closed

    first = tracker.observe(
        positions=[tracked],
        open_states={'p-1': False},
        observed_at=opened_at + timedelta(seconds=31),
    )
    assert first.newly_suspect == (tracked,)

    too_soon = tracker.observe(
        positions=[tracked],
        open_states={'p-1': False},
        observed_at=opened_at + timedelta(seconds=35),
    )
    assert too_soon.still_suspect == (tracked,)
    assert tracker.evidence_for('p-1').consecutive_misses == 1

    second = tracker.observe(
        positions=[tracked],
        open_states={'p-1': False},
        observed_at=opened_at + timedelta(seconds=42),
    )
    assert second.still_suspect == (tracked,)
    assert tracker.evidence_for('p-1').consecutive_misses == 2

    third = tracker.observe(
        positions=[tracked],
        open_states={'p-1': False},
        observed_at=opened_at + timedelta(seconds=53),
    )
    assert third.confirmed_closed == (tracked,)
    assert tracker.evidence_for('p-1') is None


def test_reconciliation_recovery_clears_suspect_state():
    opened_at = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    tracked = position(opened_at)
    tracker = PositionReconciliationTracker(
        grace_seconds=0,
        required_misses=3,
        minimum_miss_interval_seconds=0,
    )

    tracker.observe(
        positions=[tracked],
        open_states={'p-1': False},
        observed_at=opened_at,
    )
    recovered = tracker.observe(
        positions=[tracked],
        open_states={'p-1': True},
        observed_at=opened_at + timedelta(seconds=1),
    )

    assert recovered.recovered == (tracked,)
    assert tracker.evidence_for('p-1') is None


def test_missing_broker_state_never_counts_as_closed():
    opened_at = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    tracked = position(opened_at)
    tracker = PositionReconciliationTracker(
        grace_seconds=0,
        required_misses=2,
        minimum_miss_interval_seconds=0,
    )

    for offset in range(3):
        outcome = tracker.observe(
            positions=[tracked],
            open_states={},
            observed_at=opened_at + timedelta(seconds=offset),
        )
        assert outcome.missing_states == (tracked,)
        assert not outcome.confirmed_closed
    assert tracker.evidence_for('p-1') is None
