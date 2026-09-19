from __future__ import annotations

from datetime import datetime


class EtoroOrderRejectedError(RuntimeError):
    """The order lookup explicitly reported a terminal rejected/failed state.

    This exception must only be raised after examining the broker's structured
    response. Neither HTTP failure nor an exception message proves rejection.
    """

    def __init__(
        self,
        *,
        order_id: str,
        error_code: int | None,
        error_message: str | None,
        details: dict,
    ) -> None:
        self.order_id = order_id
        self.error_code = error_code
        self.error_message = error_message
        self.details = details
        super().__init__(
            'eToro order rejected: '
            f'order_id={order_id}, error_code={error_code}, '
            f'error_message={error_message}, details={details}'
        )


class EtoroOrderConfirmationUnknownError(RuntimeError):
    def __init__(
        self,
        *,
        order_id: str,
        reference_id: str | None,
        symbol: str,
        side: str,
        amount: float,
        submitted_at: datetime,
        cause: Exception,
    ) -> None:
        self.order_id = order_id
        self.reference_id = reference_id
        self.symbol = symbol
        self.side = side
        self.amount = amount
        self.submitted_at = submitted_at
        self.cause = cause
        super().__init__(
            'eToro order confirmation unknown: '
            f'order_id={order_id}, reference_id={reference_id}, '
            f'symbol={symbol}, side={side}, amount={amount}, cause={cause}'
        )
