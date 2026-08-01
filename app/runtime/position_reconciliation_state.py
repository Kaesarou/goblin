from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.execution.position_models import TrackedPosition


@dataclass(frozen=True)
class PositionReconciliationEvidence:
    position_id: str
    first_missing_at: datetime
    last_missing_at: datetime
    consecutive_misses: int


@dataclass(frozen=True)
class PositionReconciliationOutcome:
    confirmed_closed: tuple[TrackedPosition, ...] = ()
    newly_suspect: tuple[TrackedPosition, ...] = ()
    still_suspect: tuple[TrackedPosition, ...] = ()
    recovered: tuple[TrackedPosition, ...] = ()
    grace_ignored: tuple[TrackedPosition, ...] = ()
    missing_states: tuple[TrackedPosition, ...] = ()


class PositionReconciliationTracker:
    """Require repeated fresh portfolio evidence before removing a position."""

    def __init__(
        self,
        *,
        grace_seconds: float = 30.0,
        required_misses: int = 3,
        minimum_miss_interval_seconds: float = 10.0,
    ) -> None:
        self.grace = timedelta(seconds=max(0.0, grace_seconds))
        self.required_misses = max(2, required_misses)
        self.minimum_miss_interval = timedelta(
            seconds=max(0.0, minimum_miss_interval_seconds)
        )
        self._evidence: dict[str, PositionReconciliationEvidence] = {}

    def observe(
        self,
        *,
        positions: list[TrackedPosition],
        open_states: dict[str, bool],
        observed_at: datetime,
    ) -> PositionReconciliationOutcome:
        now = _as_utc(observed_at)
        tracked_ids = {position.position_id for position in positions}
        for position_id in list(self._evidence):
            if position_id not in tracked_ids:
                self._evidence.pop(position_id, None)

        confirmed_closed: list[TrackedPosition] = []
        newly_suspect: list[TrackedPosition] = []
        still_suspect: list[TrackedPosition] = []
        recovered: list[TrackedPosition] = []
        grace_ignored: list[TrackedPosition] = []
        missing_states: list[TrackedPosition] = []

        for position in positions:
            position_id = position.position_id
            if position_id not in open_states:
                missing_states.append(position)
                continue

            if open_states[position_id]:
                if self._evidence.pop(position_id, None) is not None:
                    recovered.append(position)
                continue

            opened_at = _as_utc(position.opened_at)
            if now - opened_at < self.grace:
                grace_ignored.append(position)
                continue

            previous = self._evidence.get(position_id)
            if previous is None:
                evidence = PositionReconciliationEvidence(
                    position_id=position_id,
                    first_missing_at=now,
                    last_missing_at=now,
                    consecutive_misses=1,
                )
                self._evidence[position_id] = evidence
                newly_suspect.append(position)
                continue

            if now - previous.last_missing_at < self.minimum_miss_interval:
                still_suspect.append(position)
                continue

            evidence = PositionReconciliationEvidence(
                position_id=position_id,
                first_missing_at=previous.first_missing_at,
                last_missing_at=now,
                consecutive_misses=previous.consecutive_misses + 1,
            )
            self._evidence[position_id] = evidence
            if evidence.consecutive_misses >= self.required_misses:
                confirmed_closed.append(position)
                self._evidence.pop(position_id, None)
            else:
                still_suspect.append(position)

        return PositionReconciliationOutcome(
            confirmed_closed=tuple(confirmed_closed),
            newly_suspect=tuple(newly_suspect),
            still_suspect=tuple(still_suspect),
            recovered=tuple(recovered),
            grace_ignored=tuple(grace_ignored),
            missing_states=tuple(missing_states),
        )

    def evidence_for(
        self,
        position_id: str,
    ) -> PositionReconciliationEvidence | None:
        return self._evidence.get(position_id)

    def clear(self, position_id: str) -> None:
        self._evidence.pop(position_id, None)

    def snapshot(self) -> dict[str, PositionReconciliationEvidence]:
        return dict(self._evidence)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
