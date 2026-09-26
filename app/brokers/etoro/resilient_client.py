import logging
import math
import time

import requests

from app.brokers.base import BrokerAccountPreflight
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
from app.brokers.etoro.pnl_position_amount import position_amount_usd
from app.brokers.etoro.pending_orders_preflight import pending_open_order_descriptions
from app.brokers.etoro.portfolio_position_parser import extract_open_position_units

logger = logging.getLogger(__name__)


class ResilientEtoroClient(EtoroClient):
    """Preserve exposure while accepted open orders remain uncertain."""

    @property
    def requires_external_activity_ack(self) -> bool:
        return self.env == "demo"

    def get_account_preflight(self) -> BrokerAccountPreflight:
        return BrokerAccountPreflight(
            position_units=extract_open_position_units(self.get_portfolio()),
            pending_open_orders=pending_open_order_descriptions(self),
        )

    def _translate_open_confirmation_error(
        self,
        *,
        order_id,
        reference_id,
        symbol,
        side,
        amount,
        submitted_at,
        cause,
    ):
        if isinstance(cause, EtoroOrderRejectedError):
            return None
        return EtoroOrderConfirmationUnknownError(
            order_id=order_id,
            reference_id=reference_id,
            symbol=symbol.strip().upper(),
            side=side,
            amount=amount,
            submitted_at=submitted_at,
            cause=cause,
        )

    def _invalid_open_execution_error(
        self,
        *,
        order_id,
        reference_id,
        symbol,
        side,
        amount,
        submitted_at,
        details,
        execution_count,
    ):
        return EtoroOrderConfirmationUnknownError(
            order_id=order_id,
            reference_id=reference_id,
            symbol=symbol.strip().upper(),
            side=side,
            amount=amount,
            submitted_at=submitted_at,
            cause=RuntimeError(
                'unsupported executed position count: '
                f'{execution_count}'
            ),
        )

    def _resolve_open_notional(self, *, position_id, requested, reported):
        return self._resolve_suspicious_account_notional(
            position_id=position_id,
            requested=requested,
            reported=reported,
        )

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

    def _resolve_suspicious_account_notional(
        self, *, position_id: str, requested: float,
        reported: float | None,
    ) -> float | None:
        """Cross-check suspect order amounts using the exact P&L position in USD.

        Missing/stale/malformed P&L never hides the confirmed position. The V3
        executor retains its provisional exposure and blocks further BUYs.
        """
        if (reported is not None and math.isfinite(reported)
                and 0.8 * requested <= reported <= 1.2 * requested):
            return reported
        if self.settings.base_currency.strip().upper() != 'USD':
            logger.error('P&L notional cross-check requires USD account currency')
            return reported
        if self.env not in {'demo', 'real'}:
            return reported
        path = ('/api/v1/trading/info/demo/pnl' if self.env == 'demo'
                else '/api/v1/trading/info/real/pnl')
        try:
            pnl_amount = position_amount_usd(self._get(path), position_id)
        except Exception as exc:
            logger.warning(
                'eToro account-notional P&L cross-check unavailable | '
                'position_id=%s | error_type=%s',
                position_id, type(exc).__name__,
            )
            return reported
        if pnl_amount is None:
            logger.warning(
                'eToro P&L has not yet exposed confirmed position | position_id=%s',
                position_id,
            )
            return reported
        logger.warning(
            'eToro order notional cross-checked with exact P&L position | '
            'position_id=%s | order_invested_amount=%s | pnl_account_amount_usd=%s',
            position_id, reported, pnl_amount,
        )
        return pnl_amount
