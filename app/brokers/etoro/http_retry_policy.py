import logging
import time
from collections.abc import Callable

import requests

from app.brokers.etoro.attempt_delay import delay_seconds_for_attempt
from app.brokers.etoro.get_rate_governor import (
    ETORO_GET_429_FALLBACK_SECONDS,
    EtoroGetRateGovernor,
)
from app.brokers.etoro.request_settings import default_request_timeout_seconds

DEFAULT_GET_MAX_ATTEMPTS = 3
RETRYABLE_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}


def default_get_max_attempts() -> int:
    return DEFAULT_GET_MAX_ATTEMPTS


def retryable_http_status_codes() -> set[int]:
    return set(RETRYABLE_HTTP_STATUS_CODES)


def is_retryable_http_status(status_code: int) -> bool:
    return status_code in RETRYABLE_HTTP_STATUS_CODES


def retry_after_seconds(response) -> float | None:
    value = response.headers.get('Retry-After')
    if value in (None, ''):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, seconds)


def apply_429_cooldown(response, governor: EtoroGetRateGovernor) -> None:
    if getattr(response, 'status_code', None) != 429:
        return
    retry_after = retry_after_seconds(response)
    governor.defer(
        retry_after if retry_after is not None else ETORO_GET_429_FALLBACK_SECONDS
    )


def get_with_retries(
    *,
    url: str,
    params: dict | None,
    headers: Callable[[], dict[str, str]],
    governor: EtoroGetRateGovernor,
    max_attempts: int,
    logger: logging.Logger,
    operation: str,
) -> requests.Response:
    """Perform a governed GET without changing endpoint-specific error handling."""
    for attempt in range(1, max_attempts + 1):
        governor.acquire()
        try:
            response = requests.get(
                url,
                headers=headers(),
                params=params,
                timeout=default_request_timeout_seconds(),
            )
        except requests.RequestException as exc:
            logger.warning(
                '%s GET failed | attempt=%s/%s | url=%s | params=%s | error=%s',
                operation,
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

        apply_429_cooldown(response, governor)
        if is_retryable_http_status(response.status_code) and attempt < max_attempts:
            logger.warning(
                '%s GET retryable error | attempt=%s/%s | status=%s | url=%s | params=%s',
                operation,
                attempt,
                max_attempts,
                response.status_code,
                url,
                params,
            )
            retry_after = retry_after_seconds(response)
            time.sleep(
                retry_after if retry_after is not None else delay_seconds_for_attempt(attempt)
            )
            continue
        return response
    raise RuntimeError(f'{operation} GET failed after retries | url={url}')
