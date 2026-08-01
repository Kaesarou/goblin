from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from app.brokers.base import ClosePositionSubmission
from app.execution.position_models import PositionCloseSignal


class CloseState(StrEnum):
    SUBMITTING = 'close_submitting'
    PENDING_CONFIRMATION = 'close_pending_confirmation'
    SUBMISSION_UNKNOWN = 'close_submission_unknown'
    REJECTED = 'close_rejected'


@dataclass(frozen=True)
class PendingClose:
    position_id: str
    symbol: str
    signal: PositionCloseSignal
    source: str
    state: CloseState
    requested_at: datetime
    submitted_at: datetime | None = None
    accepted_at: datetime | None = None
    close_order_id: str | None = None
    reference_id: str | None = None
    confirmation_checks: int = 0
    last_confirmation_at: datetime | None = None
    last_error: str | None = None
    metadata: dict[str, Any] | None = None
    delayed_reported_at: datetime | None = None
    manual_intervention_reported_at: datetime | None = None

    def mark_submitted(
        self,
        submission: ClosePositionSubmission,
    ) -> 'PendingClose':
        if submission.position_id != self.position_id:
            raise ValueError(
                'Close submission position mismatch: '
                f'pending={self.position_id}, submitted={submission.position_id}'
            )
        return replace(
            self,
            state=CloseState.PENDING_CONFIRMATION,
            submitted_at=_as_utc(submission.submitted_at),
            accepted_at=_as_utc(submission.accepted_at),
            close_order_id=submission.close_order_id,
            reference_id=submission.reference_id,
            last_error=None,
        )

    def mark_submission_unknown(
        self,
        *,
        error: Exception,
        submitted_at: datetime | None = None,
        close_order_id: str | None = None,
        reference_id: str | None = None,
    ) -> 'PendingClose':
        error_close_order_id = getattr(error, 'close_order_id', None)
        error_reference_id = getattr(error, 'reference_id', None)
        return replace(
            self,
            state=CloseState.SUBMISSION_UNKNOWN,
            submitted_at=(
                _as_utc(submitted_at)
                if submitted_at is not None
                else self.submitted_at
            ),
            close_order_id=(
                close_order_id
                or error_close_order_id
                or self.close_order_id
            ),
            reference_id=(
                reference_id
                or error_reference_id
                or self.reference_id
            ),
            last_error=str(error),
        )

    def mark_rejected(self, *, error: Exception) -> 'PendingClose':
        return replace(
            self,
            state=CloseState.REJECTED,
            last_error=str(error),
        )

    def record_confirmation_check(
        self,
        *,
        observed_at: datetime,
    ) -> 'PendingClose':
        return replace(
            self,
            confirmation_checks=self.confirmation_checks + 1,
            last_confirmation_at=_as_utc(observed_at),
        )

    def observe_still_open(self, *, observed_at: datetime) -> 'PendingClose':
        return self.record_confirmation_check(observed_at=observed_at)

    def mark_delayed_reported(self, *, reported_at: datetime) -> 'PendingClose':
        return replace(self, delayed_reported_at=_as_utc(reported_at))

    def mark_manual_intervention_reported(
        self,
        *,
        reported_at: datetime,
    ) -> 'PendingClose':
        return replace(
            self,
            manual_intervention_reported_at=_as_utc(reported_at),
        )

    def confirmation_age_seconds(self, *, now: datetime) -> float:
        origin = self.accepted_at or self.submitted_at or self.requested_at
        return max(0.0, (_as_utc(now) - _as_utc(origin)).total_seconds())


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
