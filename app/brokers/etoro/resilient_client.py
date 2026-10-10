import logging
import math
import time

import requests

from app.brokers.base import BrokerAccountPreflight, BrokerPositionEconomics
from app.brokers.etoro.etoro_client import EtoroClient
from app.brokers.etoro.order_confirmation_error import (
    EtoroOrderConfirmationUnknownError,
    EtoroOrderRejectedError,
)
from app.brokers.etoro.order_response_parser import (
    convergent_account_exposure,
    extract_executed_position_details_list,
    extract_order_error_code,
    extract_order_error_message,
    has_executed_position_details,
    is_order_executed,
    is_order_rejected,
)
from app.brokers.etoro.pending_orders_preflight import pending_open_order_descriptions
from app.brokers.etoro.pnl_position_amount import position_amount_usd
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

    def get_open_position_economics(self, position_ids):
        if self.settings.base_currency.strip().upper() != "USD":
            raise ValueError("Exact eToro economics require USD account currency")
        path = ("/api/v1/trading/info/demo/pnl" if self.env == "demo"
                else "/api/v1/trading/info/real/pnl")
        payload = self._get(path)
        # Reuse the strict amount parser, including envelope/duplicate validation.
        result = {}
        for position_id in position_ids:
            amount = position_amount_usd(payload, position_id)
            if amount is None:
                continue
            position = next(p for p in payload["clientPortfolio"]["positions"]
                            if str(p.get("positionId")) == str(position_id))
            expected_instrument = self.position_instruments.get(str(position_id))
            if (expected_instrument is None
                    or position.get("instrumentId") != expected_instrument
                    or position.get("isBuy") is not True
                    or isinstance(position.get("leverage"), bool)
                    or position.get("leverage") != 1):
                raise ValueError("Exact eToro economics position identity mismatch")
            units, price = position.get("units"), position.get("openRate")
            for value in (units, price):
                if (isinstance(value, bool) or not isinstance(value, (float, int))
                        or not math.isfinite(value) or value <= 0):
                    raise ValueError("Invalid exact eToro economics price/units")
            result[str(position_id)] = BrokerPositionEconomics(
                position_id=str(position_id), units=float(units),
                entry_price=float(price), account_notional=amount,
                source="etoro_exact_pnl_position_usd", broker_response=position,
            )
        return result

    def recover_open_position_economics_from_fill(self, fill):
        """Re-evaluate durable order evidence after restart without another POST."""
        raw = fill.get("broker_response")
        if not isinstance(raw, dict):
            return None
        # The historical order is evidence only for the exact USD BUY we own;
        # it must not rearm a position using another currency or an ambiguous
        # multi-position execution.
        if self.settings.base_currency.strip().upper() != "USD":
            return None
        currency = raw.get("orderCurrency")
        if not isinstance(currency, str) or currency.strip().upper() != "USD":
            return None
        if raw.get("action") not in (None, "open"):
            return None
        if raw.get("transaction") not in (None, "buy"):
            return None
        position_id = str(fill.get("position_id", ""))
        raw_requested = fill.get("requested_notional")
        if raw_requested is None:
            raw_requested = fill.get("notional")
        try:
            requested = float(raw_requested)
        except (TypeError, ValueError):
            return None
        executions = extract_executed_position_details_list(raw)
        if len(executions) != 1 or executions[0].position_id != position_id:
            return None
        details = executions[0]
        exposure = convergent_account_exposure(
            requested=requested,
            initial_exposure_account_currency=(
                details.initial_exposure_account_currency
            ),
            margin_account_currency=details.margin_account_currency,
            leverage=details.leverage,
        )
        if exposure is None:
            return None
        return BrokerPositionEconomics(
            position_id=position_id,
            units=details.executed_units,
            entry_price=details.executed_entry_price,
            account_notional=exposure,
            source="etoro_order_convergent_account_exposure",
            broker_response={
                "orderId": details.opening_order_id,
                "initialExposureAccountCurrency": (
                    details.initial_exposure_account_currency
                ),
                "marginAccountCurrency": details.margin_account_currency,
                "leverage": details.leverage,
            },
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

    def _resolve_open_notional(
        self,
        *,
        position_id,
        requested,
        reported,
        initial_exposure_account_currency=None,
        margin_account_currency=None,
        leverage=None,
    ):
        return self._resolve_suspicious_account_notional(
            position_id=position_id,
            requested=requested,
            reported=reported,
            initial_exposure_account_currency=initial_exposure_account_currency,
            margin_account_currency=margin_account_currency,
            leverage=leverage,
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
        self,
        *,
        position_id: str,
        requested: float,
        reported: float | None,
        initial_exposure_account_currency: float | None = None,
        margin_account_currency: float | None = None,
        leverage: float | None = None,
    ) -> float | None:
        """Cross-check suspect order amounts using the exact P&L position in USD.

        Missing/stale/malformed P&L never hides the confirmed position. The V3
        executor retains its provisional exposure and blocks further BUYs.
        """
        if (reported is not None and math.isfinite(reported)
                and 0.8 * requested <= reported <= 1.2 * requested):
            return reported

        # eToro's order lookup documents initialExposureAccountCurrency as the
        # initial exposure in account currency. Prospectively, DEMO has returned
        # investedAmountCurrency=1.0 for ~USD 328 unleveraged positions while
        # both initial exposure and account margin independently agree near the
        # requested cash amount. Promote that evidence only when all invariants
        # agree; otherwise keep the existing exact-P&L fallback.
        exposure = convergent_account_exposure(
            requested=requested,
            initial_exposure_account_currency=initial_exposure_account_currency,
            margin_account_currency=margin_account_currency,
            leverage=leverage,
        )
        if exposure is not None:
            logger.warning(
                'eToro suspicious invested amount resolved from convergent '
                'account-exposure evidence | position_id=%s | '
                'order_invested_amount=%s | initial_exposure_account=%s | '
                'margin_account=%s | leverage=%s',
                position_id,
                reported,
                initial_exposure_account_currency,
                margin_account_currency,
                leverage,
            )
            return exposure
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
