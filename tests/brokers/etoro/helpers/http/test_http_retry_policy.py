from types import SimpleNamespace

import pytest

from app.brokers.etoro.get_rate_governor import EtoroGetRateGovernor
from app.brokers.etoro.http_retry_policy import (
    apply_429_cooldown,
    default_get_max_attempts,
    is_retryable_http_status,
    retry_after_seconds,
    retryable_http_status_codes,
)


def test_default_get_max_attempts():
    assert default_get_max_attempts() == 3


def test_retryable_http_status_codes_returns_copy():
    statuses = retryable_http_status_codes()
    statuses.add(418)

    assert retryable_http_status_codes() == {429, 500, 502, 503, 504}


def test_is_retryable_http_status():
    assert is_retryable_http_status(429) is True
    assert is_retryable_http_status(500) is True
    assert is_retryable_http_status(504) is True
    assert is_retryable_http_status(400) is False
    assert is_retryable_http_status(418) is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), ("", None), ("invalid", None), ("7.5", 7.5), ("-1", 0.0)],
)
def test_retry_after_seconds_preserves_numeric_hint_policy(raw, expected):
    assert retry_after_seconds(SimpleNamespace(headers={"Retry-After": raw})) == expected


def test_429_cooldown_is_bucket_local_with_fallback():
    account = EtoroGetRateGovernor(clock=lambda: 0.0)
    lookup = EtoroGetRateGovernor(clock=lambda: 0.0)
    apply_429_cooldown(SimpleNamespace(status_code=429, headers={}), account)
    apply_429_cooldown(
        SimpleNamespace(status_code=500, headers={"Retry-After": "120"}),
        lookup,
    )
    assert account.snapshot()["cooldown_remaining_seconds"] == 60.0
    assert lookup.snapshot()["cooldown_remaining_seconds"] == 0.0
