from app.brokers.etoro.get_rate_governor import (
    ETORO_GET_429_FALLBACK_SECONDS,
    EtoroGetRateGovernor,
)

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
