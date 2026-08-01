from datetime import datetime

from app.brokers.base import BrokerCloseExecution
from app.runtime.async_broker_operations import AsyncBrokerOperationsCoordinator
from app.runtime.pending_close import PendingClose


class ResilientBrokerOperationsCoordinator(
    AsyncBrokerOperationsCoordinator
):
    """Add truthful confirmation evidence to the durable close lifecycle."""

    def _confirm_pending_close(
        self,
        pending: PendingClose,
        *,
        closed_at: datetime,
        source: str,
        broker_execution: BrokerCloseExecution | None = None,
    ) -> None:
        confirmed = pending.record_confirmation_check(observed_at=closed_at)
        super()._confirm_pending_close(
            confirmed,
            closed_at=closed_at,
            source=source,
            broker_execution=broker_execution,
        )
