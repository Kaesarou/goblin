from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class OpenPositionResult:
    position_id: str
    executed_entry_price: float | None = None


@dataclass(frozen=True)
class ClosePositionSubmission:
    position_id: str
    close_order_id: str | None
    reference_id: str | None
    submitted_at: datetime
    accepted_at: datetime
    broker_response: dict[str, Any]


@dataclass(frozen=True)
class BrokerCloseExecution:
    position_id: str
    close_order_id: str
    executed_exit_price: float
    executed_at: datetime | None
    units: float | None
    conversion_rate: float | None
    amount: float | None
    broker_response: dict[str, Any]


class ClosePositionRejectedError(RuntimeError):
    def __init__(
        self,
        *,
        position_id: str,
        message: str,
        broker_response: dict[str, Any] | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.position_id = position_id
        self.broker_response = broker_response
        self.cause = cause
        self.response = getattr(cause, 'response', None)


class ClosePositionSubmissionUnknownError(RuntimeError):
    def __init__(
        self,
        *,
        position_id: str,
        submitted_at: datetime,
        cause: Exception,
        broker_response: dict[str, Any] | None = None,
        close_order_id: str | None = None,
        reference_id: str | None = None,
    ) -> None:
        super().__init__(
            'Close submission outcome is unknown: '
            f'position_id={position_id}, cause={cause}'
        )
        self.position_id = position_id
        self.submitted_at = submitted_at
        self.cause = cause
        self.broker_response = broker_response
        self.close_order_id = close_order_id
        self.reference_id = reference_id
        self.response = getattr(cause, 'response', None)


class BrokerClient(ABC):
    """Execution and account contract.

    Market-data access is deliberately excluded. Paper, demo and live execution
    all consume the same independent eToro market-data pipeline.
    """

    @abstractmethod
    def get_account_equity(self) -> float:
        raise NotImplementedError

    @abstractmethod
    def open_position(
        self,
        symbol: str,
        side: str,
        amount: float,
        stop_loss: float,
        take_profit: float,
    ) -> OpenPositionResult:
        raise NotImplementedError

    @abstractmethod
    def close_position(self, position_id: str) -> ClosePositionSubmission:
        """Submit one close request and return as soon as it is accepted.

        Portfolio disappearance is confirmed asynchronously by the runtime. This
        method must never poll the portfolio for closure confirmation.
        """
        raise NotImplementedError

    @abstractmethod
    def is_position_open(self, position_id: str) -> bool:
        raise NotImplementedError

    def get_close_execution(
        self,
        close_order_id: str,
        position_id: str,
    ) -> BrokerCloseExecution | None:
        """Return a confirmed broker close fill, or explicit absence.

        Implementations must not derive a fill from close-request acceptance.
        """
        raise NotImplementedError

    def remember_position_instrument(self, position_id: str, symbol: str) -> None:
        """Restore broker-specific metadata needed to manage a position."""
        return None

    def forget_position_instrument(self, position_id: str) -> None:
        """Discard broker-specific metadata after portfolio-confirmed closure."""
        return None
