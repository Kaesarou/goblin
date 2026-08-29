import logging
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from uuid import uuid4

import requests

from app.brokers.base import (
    BrokerClient,
    BrokerCloseExecution,
    ClosePositionRejectedError,
    ClosePositionSubmission,
    ClosePositionSubmissionUnknownError,
    OpenPositionResult,
)
from app.brokers.etoro.account_equity_mapper import extract_account_equity
from app.brokers.etoro.attempt_delay import delay_seconds_for_attempt
from app.brokers.etoro.broker_environment import broker_environment_from_name
from app.brokers.etoro.close_order_details_parser import extract_close_execution
from app.brokers.etoro.close_order_payload_builder import build_close_order_payload
from app.brokers.etoro.endpoint_paths import (
    close_order_lookup_path,
    close_position_path,
    demo_portfolio_path,
    instrument_search_path,
    open_order_path,
    order_lookup_path,
    pnl_path,
    real_portfolio_path,
)
from app.brokers.etoro.get_rate_governor import (
    ETORO_GET_429_FALLBACK_SECONDS,
    EtoroGetRateGovernor,
)
from app.brokers.etoro.http_failure import raise_for_failed_response
from app.brokers.etoro.http_headers_builder import build_headers
from app.brokers.etoro.http_response_payload import response_payload
from app.brokers.etoro.http_retry_policy import (
    default_get_max_attempts,
    is_retryable_http_status,
)
from app.brokers.etoro.http_url_builder import build_http_url
from app.brokers.etoro.instrument_cache import (
    cached_instrument_id,
    remember_instrument_id,
)
from app.brokers.etoro.instrument_search_parser import resolve_exact_instrument_id
from app.brokers.etoro.order_payload_builder import build_open_order_payload
from app.brokers.etoro.order_response_parser import (
    ExecutedPositionDetails,
    extract_executed_position_details_list,
    extract_order_error_code,
    extract_order_error_message,
    extract_order_id,
    extract_reference_id,
    has_executed_position_details,
    is_close_response_accepted,
    is_order_executed,
    is_order_rejected,
)
from app.brokers.etoro.portfolio_position_parser import (
    contains_open_position,
    extract_open_position_units,
)
from app.brokers.etoro.position_instrument_cache import (
    forget_position_instrument_id,
    remember_position_instrument_id,
    require_position_instrument_id,
)
from app.brokers.etoro.request_settings import default_request_timeout_seconds
from app.brokers.etoro.trade_side import ensure_side_is_allowed, normalize_side
from app.config.settings import Settings

logger = logging.getLogger(__name__)


class EtoroClient(BrokerClient):
    """eToro execution and account client.

    Market-data search and rates are owned by EtoroRestMarketDataClient. This
    class only handles account, order and portfolio operations.
    """

    etoro_api_base_url = 'https://public-api.etoro.com'

    def __init__(self, settings: Settings):
        self.settings = settings
        self.env = broker_environment_from_name(settings.broker)
        self.position_instruments: dict[str, int] = {}
        self.instrument_ids_by_symbol: dict[int | str, int] = {}
        self.symbol_by_instrument_id: dict[int, str] = {}
        self._get_rate_governor = EtoroGetRateGovernor()

    def get_account_equity(self) -> float:
        equity = extract_account_equity(self.get_pnl())
        logger.info('Account equity resolved | env=%s | equity=%s', self.env, equity)
        return equity

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
        order_response = self._post(self._open_order_path(), payload)
        order_id = extract_order_id(order_response)
        reference_id = extract_reference_id(order_response)
        logger.info('eToro order submitted | order_id=%s | reference_id=%s', order_id, reference_id)
        order_details = self._wait_for_executed_order(
            order_id,
            require_position_details=True,
        )
        executed_positions = extract_executed_position_details_list(order_details)
        if len(executed_positions) != 1:
            raise RuntimeError(
                'eToro order executed with unsupported position execution '
                f'count: order_id={order_id}, '
                f'count={len(executed_positions)}, details={order_details}'
            )
        executed_position = executed_positions[0]
        remember_position_instrument_id(
            position_instruments=self.position_instruments,
            position_id=executed_position.position_id,
            instrument_id=instrument_id,
        )
        return OpenPositionResult(
            position_id=executed_position.position_id,
            executed_entry_price=executed_position.executed_entry_price,
            executed_units=executed_position.executed_units,
        )

    def close_position(
        self,
        position_id: str,
        units_to_deduct: float | None = None,
    ) -> ClosePositionSubmission:
        try:
            instrument_id = require_position_instrument_id(
                position_instruments=self.position_instruments,
                position_id=position_id,
            )
        except Exception as exc:
            raise ClosePositionRejectedError(
                position_id=position_id,
                message=(
                    'Close submission was not sent because broker position '
                    f'metadata is unavailable: position_id={position_id}'
                ),
                cause=exc,
            ) from exc

        submitted_at = datetime.now(timezone.utc)
        try:
            response = self._post(
                self._close_position_path(position_id),
                build_close_order_payload(
                    instrument_id,
                    units_to_deduct=units_to_deduct,
                ),
            )
        except requests.HTTPError as exc:
            status = getattr(exc.response, 'status_code', None)
            if (
                status is not None
                and 400 <= status < 500
                and status not in {408, 409, 425, 429}
            ):
                raise ClosePositionRejectedError(
                    position_id=position_id,
                    message=(
                        'eToro explicitly rejected close submission: '
                        f'position_id={position_id}, status={status}'
                    ),
                    cause=exc,
                ) from exc
            raise ClosePositionSubmissionUnknownError(
                position_id=position_id,
                submitted_at=submitted_at,
                cause=exc,
            ) from exc
        except requests.RequestException as exc:
            raise ClosePositionSubmissionUnknownError(
                position_id=position_id,
                submitted_at=submitted_at,
                cause=exc,
            ) from exc

        close_order_id = _optional_order_id(response)
        reference_id = _optional_reference_id(response)
        if not is_close_response_accepted(response, position_id):
            if is_order_rejected(response):
                raise ClosePositionRejectedError(
                    position_id=position_id,
                    message=(
                        'eToro explicitly rejected close submission: '
                        f'position_id={position_id}, response={response}'
                    ),
                    broker_response=response,
                )
            raise ClosePositionSubmissionUnknownError(
                position_id=position_id,
                submitted_at=submitted_at,
                cause=RuntimeError('eToro close response did not prove acceptance'),
                broker_response=response,
                close_order_id=close_order_id,
                reference_id=reference_id,
            )

        accepted_at = datetime.now(timezone.utc)
        logger.info(
            'eToro close submitted | position_id=%s | close_order_id=%s | '
            'reference_id=%s | units_to_deduct=%s',
            position_id,
            close_order_id,
            reference_id,
            units_to_deduct,
        )
        return ClosePositionSubmission(
            position_id=position_id,
            close_order_id=close_order_id,
            reference_id=reference_id,
            submitted_at=submitted_at,
            accepted_at=accepted_at,
            broker_response=response,
        )

    def get_order_details(self, order_id: str) -> dict:
        return self._get(self._order_lookup_path(), params={'orderId': order_id})

    def get_close_execution(
        self,
        close_order_id: str,
        position_id: str,
    ) -> BrokerCloseExecution | None:
        # The V3 confirmation scheduler owns per-action retry/backoff. The eToro
        # client still applies one shared user-key GET budget before this request.
        payload = self._get_once(close_order_lookup_path(self.env, close_order_id))
        return extract_close_execution(
            payload,
            close_order_id=close_order_id,
            position_id=position_id,
        )

    def get_pnl(self) -> dict:
        return self._get(pnl_path(self.env))

    def get_portfolio(self) -> dict:
        path = demo_portfolio_path() if self.env == 'demo' else real_portfolio_path()
        return self._get(path)

    def get_open_position_units(
        self,
        position_ids: Iterable[str],
    ) -> dict[str, float | None]:
        requested = tuple(str(position_id) for position_id in position_ids)
        if not requested:
            return {}
        return extract_open_position_units(self.get_portfolio(), requested)

    def is_position_open(self, position_id: str) -> bool:
        return contains_open_position(self.get_portfolio(), position_id)

    def remember_position_instrument(self, position_id: str, symbol: str) -> None:
        instrument_id = self._find_instrument_id(symbol)
        remember_position_instrument_id(
            position_instruments=self.position_instruments,
            position_id=position_id,
            instrument_id=instrument_id,
        )
        logger.info(
            'eToro restored position instrument | position_id=%s | symbol=%s | instrument_id=%s',
            position_id,
            symbol,
            instrument_id,
        )

    def forget_position_instrument(self, position_id: str) -> None:
        forget_position_instrument_id(
            position_instruments=self.position_instruments,
            position_id=position_id,
        )

    def get_rate_limit_metrics(self) -> dict[str, object]:
        return self._get_governor().snapshot()

    @property
    def headers(self) -> dict[str, str]:
        return build_headers(
            request_id=str(uuid4()),
            api_key=self.settings.etoro_api_key,
            user_key=self.settings.etoro_user_key,
        )

    def _get_once(self, path: str, params: dict | None = None) -> dict:
        url = build_http_url(self.etoro_api_base_url, path)
        self._get_governor().acquire()
        response = requests.get(
            url,
            headers=self.headers,
            params=params,
            timeout=default_request_timeout_seconds(),
        )
        self._apply_global_429_cooldown(response)
        if not response.ok:
            raise_for_failed_response(response)
        return response_payload(response)

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = build_http_url(self.etoro_api_base_url, path)
        max_attempts = default_get_max_attempts()
        for attempt in range(1, max_attempts + 1):
            self._get_governor().acquire()
            try:
                response = requests.get(
                    url,
                    headers=self.headers,
                    params=params,
                    timeout=default_request_timeout_seconds(),
                )
            except requests.RequestException as exc:
                logger.warning(
                    'eToro GET failed | attempt=%s/%s | url=%s | params=%s | error=%s',
                    attempt,
                    max_attempts,
                    url,
                    params,
                    exc,
                )
                if attempt == max_attempts:
                    raise
                time.sleep(delay_seconds_for_attempt(attempt))
                continue

            self._apply_global_429_cooldown(response)
            if (
                is_retryable_http_status(response.status_code)
                and attempt < max_attempts
            ):
                logger.warning(
                    'eToro GET retryable error | attempt=%s/%s | status=%s | url=%s | params=%s',
                    attempt,
                    max_attempts,
                    response.status_code,
                    url,
                    params,
                )
                retry_after = _retry_after_seconds(response)
                time.sleep(
                    retry_after
                    if retry_after is not None
                    else delay_seconds_for_attempt(attempt)
                )
                continue
            if not response.ok:
                raise_for_failed_response(response)
            return response_payload(response)
        raise RuntimeError(f'eToro GET failed after retries | url={url}')

    def _get_governor(self) -> EtoroGetRateGovernor:
        governor = getattr(self, '_get_rate_governor', None)
        if governor is None:
            governor = EtoroGetRateGovernor()
            self._get_rate_governor = governor
        return governor

    def _apply_global_429_cooldown(self, response) -> None:
        if getattr(response, 'status_code', None) != 429:
            return
        retry_after = _retry_after_seconds(response)
        self._get_governor().defer(
            retry_after
            if retry_after is not None
            else ETORO_GET_429_FALLBACK_SECONDS
        )

    def _post(self, path: str, payload: dict) -> dict:
        url = build_http_url(self.etoro_api_base_url, path)
        try:
            response = requests.post(
                url,
                headers=self.headers,
                json=payload,
                timeout=default_request_timeout_seconds(),
            )
        except requests.RequestException:
            logger.exception('eToro POST failed | url=%s', url)
            raise
        if not response.ok:
            raise_for_failed_response(response)
        return response_payload(response)

    def _build_open_order_payload(
        self,
        instrument_id: int,
        side: str,
        amount: float,
        stop_loss: float,
        take_profit: float,
    ) -> dict:
        return build_open_order_payload(
            instrument_id=instrument_id,
            side=side,
            amount=amount,
            stop_loss=stop_loss,
            take_profit=take_profit,
            order_currency=self.settings.base_currency,
        )

    def _open_order_path(self) -> str:
        return open_order_path(self.env)

    def _close_position_path(self, position_id: str) -> str:
        return close_position_path(self.env, position_id)

    def _order_lookup_path(self) -> str:
        return order_lookup_path(self.env)

    def _find_instrument_id(self, symbol: str) -> int:
        instrument_id = cached_instrument_id(
            instrument_ids_by_symbol=self.instrument_ids_by_symbol,
            symbol=symbol,
        )
        if instrument_id is not None:
            return instrument_id
        payload = self._get(
            instrument_search_path(),
            params={'internalSymbolFull': symbol},
        )
        instrument_id = resolve_exact_instrument_id(symbol, payload)
        remember_instrument_id(
            instrument_ids_by_symbol=self.instrument_ids_by_symbol,
            symbol_by_instrument_id=self.symbol_by_instrument_id,
            symbol=symbol,
            instrument_id=instrument_id,
        )
        return instrument_id

    def _wait_for_executed_order(
        self,
        order_id: str,
        attempts: int = 10,
        delay_seconds: float = 1.0,
        require_position_details: bool = True,
    ) -> dict:
        last_lookup_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                details = self.get_order_details(order_id)
            except (requests.RequestException, RuntimeError, ValueError) as exc:
                last_lookup_error = exc
                time.sleep(delay_seconds)
                continue
            if is_order_rejected(details):
                raise RuntimeError(
                    'eToro order rejected: '
                    f'order_id={order_id}, '
                    f'error_code={extract_order_error_code(details)}, '
                    f'error_message={extract_order_error_message(details)}, '
                    f'details={details}'
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

    def _extract_executed_position_details_list(
        self,
        payload: dict,
    ) -> list[ExecutedPositionDetails]:
        return extract_executed_position_details_list(payload)

    def _extract_order_id(self, payload: dict) -> str:
        return extract_order_id(payload)

    def _extract_reference_id(self, payload: dict) -> str | None:
        return extract_reference_id(payload)

    def _is_close_response_accepted(self, payload: dict, position_id: str) -> bool:
        return is_close_response_accepted(payload, position_id)

    def _is_order_rejected(self, payload: dict) -> bool:
        return is_order_rejected(payload)


def _retry_after_seconds(response) -> float | None:
    value = response.headers.get('Retry-After')
    if value in (None, ''):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, seconds)


def _optional_order_id(payload: dict) -> str | None:
    try:
        return extract_order_id(payload)
    except (KeyError, TypeError, ValueError):
        return None


def _optional_reference_id(payload: dict) -> str | None:
    try:
        return extract_reference_id(payload)
    except (KeyError, TypeError, ValueError):
        return None
