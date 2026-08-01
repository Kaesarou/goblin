from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from app.execution.position_models import (
    ClosedPosition,
    ManagedStopUpdate,
    PositionCloseSignal,
    TrackedPosition,
)
from app.execution.position_tracker import PositionTracker
from app.market.models import MarketSnapshot

POSITION_REPLAY_CONTRACT_VERSION = "shared_position_replay_v1"


@dataclass(frozen=True)
class PositionReplayResult:
    initial_position: TrackedPosition
    final_position: TrackedPosition | None
    close_signal: PositionCloseSignal | None
    closed_position: ClosedPosition | None
    managed_stop_updates: tuple[ManagedStopUpdate, ...]
    snapshots_processed: int


ForceClosePredicate = Callable[[MarketSnapshot], bool]


def replay_position(
    *,
    position: TrackedPosition,
    snapshots: Iterable[MarketSnapshot],
    force_close_when: ForceClosePredicate | None = None,
) -> PositionReplayResult:
    """Replay one position through the exact live tracker and lifecycle engine."""
    tracker = PositionTracker()
    tracker.restore_open_position(position)
    updates: list[ManagedStopUpdate] = []
    snapshots_processed = 0
    for snapshot in snapshots:
        snapshots_processed += 1
        close_signals = tracker.evaluate_snapshot(
            snapshot,
            force_close=(force_close_when(snapshot) if force_close_when is not None else False),
        )
        updates.extend(tracker.consume_managed_stop_updates())
        if not close_signals:
            continue
        close_signal = close_signals[0]
        closed = tracker.record_closed_position(
            close_signal,
            confirmed_at=close_signal.detected_at,
        )
        return PositionReplayResult(
            initial_position=position,
            final_position=None,
            close_signal=close_signal,
            closed_position=closed,
            managed_stop_updates=tuple(updates),
            snapshots_processed=snapshots_processed,
        )

    open_positions = tracker.open_positions_snapshot()
    return PositionReplayResult(
        initial_position=position,
        final_position=open_positions[0] if open_positions else None,
        close_signal=None,
        closed_position=None,
        managed_stop_updates=tuple(updates),
        snapshots_processed=snapshots_processed,
    )
