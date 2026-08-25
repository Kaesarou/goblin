import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from app.brokers.base import (
    BrokerClient,
    BrokerCloseExecution,
    ClosePositionRejectedError,
    ClosePositionSubmission,
    OpenPositionResult,
)

logger = logging.getLogger(__name__)


@dataclass
class PaperBrokerClient(BrokerClient):
    """Simulate execution while the runtime consumes real eToro market data."""

    equity: float = 50.0
    positions: dict[str, dict[str, object]] = field(default_factory=dict)

    def get_account_equity(self) -> float:
        return self.equity

    def open_position(
        self,
        symbol: str,
        side: str,
        amount: float,
        stop_loss: float,
        take_profit: float,
    ) -> OpenPositionResult:
        position_id = f'paper-{uuid4()}'
        opened_at = datetime.now(timezone.utc)
        self.positions[position_id] = {
            'position_id': position_id,
            'symbol': symbol.strip().upper(),
            'side': side.strip().upper(),
            'amount': float(amount),
            'stop_loss': float(stop_loss),
            'take_profit': float(take_profit),
            'opened_at': opened_at,
        }
        logger.info(
            'Paper order recorded | position_id=%s | symbol=%s | side=%s | '
            'amount=%s | stop_loss=%s | take_profit=%s',
            position_id,
            symbol,
            side,
            amount,
            stop_loss,
            take_profit,
        )
        return OpenPositionResult(
            position_id=position_id,
            executed_entry_price=None,
        )

    def close_position(
        self,
        position_id: str,
        units_to_deduct: float | None = None,
    ) -> ClosePositionSubmission:
        submitted_at = datetime.now(timezone.utc)
        position = self.positions.get(position_id)
        if position is None:
            raise ClosePositionRejectedError(
                position_id=position_id,
                message=f'Unknown paper position: {position_id}',
            )
        if units_to_deduct is not None and units_to_deduct <= 0:
            raise ClosePositionRejectedError(
                position_id=position_id,
                message='Paper partial close requires positive units_to_deduct',
            )
        partial = units_to_deduct is not None
        if not partial:
            self.positions.pop(position_id, None)
        accepted_at = datetime.now(timezone.utc)
        close_order_id = f'paper-close-{uuid4()}'
        logger.info(
            'Paper close recorded | position_id=%s | close_order_id=%s | '
            'units_to_deduct=%s',
            position_id,
            close_order_id,
            units_to_deduct,
        )
        return ClosePositionSubmission(
            position_id=position_id,
            close_order_id=close_order_id,
            reference_id=None,
            submitted_at=submitted_at,
            accepted_at=accepted_at,
            broker_response={
                'accepted': True,
                'mode': 'paper',
                'position_id': position_id,
                'close_order_id': close_order_id,
                'partial': partial,
                'units_to_deduct': units_to_deduct,
            },
        )

    def is_position_open(self, position_id: str) -> bool:
        return position_id in self.positions

    def get_close_execution(
        self,
        close_order_id: str,
        position_id: str,
    ) -> BrokerCloseExecution | None:
        # Paper execution has no independent broker fill source.
        return None

    def get_position_open_states(
        self,
        position_ids: list[str],
    ) -> dict[str, bool]:
        return {
            position_id: position_id in self.positions
            for position_id in position_ids
        }

    def remember_position_instrument(self, position_id: str, symbol: str) -> None:
        self.positions.setdefault(
            position_id,
            {
                'position_id': position_id,
                'symbol': symbol.strip().upper(),
                'restored': True,
            },
        )

    def forget_position_instrument(self, position_id: str) -> None:
        self.positions.pop(position_id, None)
