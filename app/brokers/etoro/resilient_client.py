import logging
import time
from datetime import datetime, timezone

import requests

from app.brokers.base import OpenPositionResult
from app.brokers.etoro.etoro_client import EtoroClient
from app.brokers.etoro.order_confirmation_error import (
    EtoroOrderConfirmationUnknownError,
    EtoroOrderRejectedError,
)
from app.brokers.etoro.order_response_parser import (
    extract_order_error_code,
    extract_order_error_message,
    has_executed_position_details,
    is_order_executed,
    is_order_rejected,
)
from app.brokers.etoro.position_instrument_cache import (
    remember_position_instrument_id,
)
from app.brokers.etoro.trade_side import ensure_side_is_allowed, normalize_side

logger = logging.getLogger(__name__)


class ResilientEtoroClient(EtoroClient):
    """Preserve exposure while accepted open orders remain uncertain."""

    def _wait_for_executed_order(
        self,
        order_id: str,
        attempts: int = 10,
        delay_seconds: float = 1.0,
        require_position_details: bool = True,
    ) -> dict:
        """Classify a rejection only from unambiguous terminal broker status.

        A response with both a terminal failure and execution evidence is NOT a
        proven zero-fill rejection. A later lookup or portfolio reconciliation
        must resolve it; do not free the per-symbol BUY reservation.
        """
        last_lookup_error: Exception | None = None
        for _attempt in range(1, attempts + 1):
            try:
                details = self.get_order_details(order_id)
            except (requests.RequestException, RuntimeError, ValueError) as exc:
                last_lookup_error = exc
                time.sleep(delay_seconds)
                continue
            if is_order_rejected(details):
                # Even malformed/noncanonical executions are evidence of a
                # potentially filled position. Never claim a definitive reject
                # when a broker response contains both contradictory signals.
                if details.get("positionExecutions") or has_executed_position_details(details):
                    raise RuntimeError(
                        "eToro order outcome conflicting: terminal rejection "
                        f"with position execution data; order_id={order_id}"
                    )
                raise EtoroOrderRejectedError(
                    order_id=order_id,
                    error_code=extract_order_error_code(details),
                    error_message=extract_order_error_message(details),
                    details=details,
                )
            executed = is_order_executed(details)
            position_details_ready = has_executed_position_details(details)
            if executed and (position_details_ready or not require_position_details):
                return details
            time.sleep(delay_seconds)
        raise RuntimeError(
            'eToro order was not executed with required details after polling: '
            f'order_id={order_id}, '
            f'require_position_details={require_position_details}, '
            f'last_lookup_error={last_lookup_error}'
        )

    def open_position(
        self,
        symbol: str,
        side: str,
        amount: float,
        stop_loss: float,
        take_profit: float,
    ) -> OpenPositionResult:
        normalized_side = normalize_side(side)
        ensure_side_is_allowed(normalized_side)
        instrument_id = self._find_instrument_id(symbol)
        payload = self._build_open_order_payload(
            instrument_id=instrument_id,
            side=normalized_side,
            amount=amount,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        logger.warning(
            'Sending eToro order | env=%s | symbol=%s | side=%s | '
            'transaction=%s | instrument_id=%s | amount=%s | '
            'bot_stop_loss=%s | take_profit=%s | StopLossRate=%s | '
            'TakeProfitRate=%s | leverage=%s | payload=%s',
            self.env,
            symbol,
            normalized_side,
            payload.get('transaction'),
            instrument_id,
            amount,
            stop_loss,
            take_profit,
            payload.get('StopLossRate'),
            payload.get('TakeProfitRate'),
            payload.get('leverage'),
            payload,
        )
        submitted_at = datetime.now(timezone.utc)
        order_response = self._post(self._open_order_path(), payload)
        order_id = self._extract_order_id(order_response)
        reference_id = self._extract_reference_id(order_response)
        logger.info(
            'eToro order submitted | order_id=%s | reference_id=%s',
            order_id,
            reference_id,
        )
        try:
            order_details = self._wait_for_executed_order(
                order_id,
                require_position_details=True,
            )
        except EtoroOrderRejectedError:
            # Only an unambiguous terminal broker status proves no fill.
            raise
        except Exception as exc:
            raise EtoroOrderConfirmationUnknownError(
                order_id=order_id,
                reference_id=reference_id,
                symbol=symbol.strip().upper(),
                side=normalized_side,
                amount=amount,
                submitted_at=submitted_at,
                cause=exc,
            ) from exc

        executed_positions = self._extract_executed_position_details_list(
            order_details
        )
        if len(executed_positions) != 1:
            raise EtoroOrderConfirmationUnknownError(
                order_id=order_id,
                reference_id=reference_id,
                symbol=symbol.strip().upper(),
                side=normalized_side,
                amount=amount,
                submitted_at=submitted_at,
                cause=RuntimeError(
                    'unsupported executed position count: '
                    f'{len(executed_positions)}'
                ),
            )
        executed_position = executed_positions[0]
        remember_position_instrument_id(
            position_instruments=self.position_instruments,
            position_id=executed_position.position_id,
            instrument_id=instrument_id,
        )
        logger.info(
            'eToro position confirmed | order_id=%s | position_id=%s | '
            'instrument_id=%s | side=%s | executed_entry_price=%s | '
            'executed_units=%s | executed_notional=%s',
            order_id,
            executed_position.position_id,
            instrument_id,
            normalized_side,
            executed_position.executed_entry_price,
            executed_position.executed_units,
            executed_position.executed_notional,
        )
        return OpenPositionResult(
            position_id=executed_position.position_id,
            executed_entry_price=executed_position.executed_entry_price,
            executed_units=executed_position.executed_units,
            executed_notional=executed_position.executed_notional,
        )
