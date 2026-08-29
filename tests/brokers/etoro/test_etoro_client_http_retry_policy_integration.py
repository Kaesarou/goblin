from types import SimpleNamespace

import requests

from app.brokers.etoro.attempt_delay import delay_seconds_for_attempt
from app.brokers.etoro.etoro_client import EtoroClient
from app.brokers.etoro.get_rate_governor import (
    ETORO_GET_429_FALLBACK_SECONDS,
    EtoroGetRateGovernor,
)
from app.brokers.etoro.http_retry_policy import is_retryable_http_status


class FakeClock:
    def __init__(self):
        self.value = 0.0
        self.governor_sleeps: list[float] = []

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.governor_sleeps.append(seconds)
        self.value += seconds


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: dict | None = None,
        headers: dict | None = None,
    ):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = b'{}'
        self.text = str(self._payload)
        self.headers = headers or {}

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        error = requests.HTTPError(f'status={self.status_code}')
        error.response = self
        raise error


def build_uninitialized_client() -> tuple[EtoroClient, FakeClock]:
    client = object.__new__(EtoroClient)
    client.etoro_api_base_url = 'https://example.test'
    client.settings = SimpleNamespace(etoro_api_key='api-key', etoro_user_key='user-key')
    clock = FakeClock()
    client._get_rate_governor = EtoroGetRateGovernor(
        max_requests=45,
        window_seconds=60.0,
        clock=clock.now,
        sleeper=clock.sleep,
    )
    return client, clock


def test_etoro_client_get_retries_retryable_status_before_success(monkeypatch):
    client, clock = build_uninitialized_client()
    calls = []
    sleeps = []
    responses = [
        FakeResponse(429),
        FakeResponse(200, {'ok': True}),
    ]

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append((url, clock.now()))
        return responses.pop(0)

    monkeypatch.setattr(requests, 'get', fake_get)
    monkeypatch.setattr('time.sleep', lambda seconds: sleeps.append(seconds))

    assert is_retryable_http_status(429) is True
    assert client._get('/path') == {'ok': True}
    assert len(calls) == 2
    assert sleeps == [delay_seconds_for_attempt(1)]
    assert clock.governor_sleeps == [ETORO_GET_429_FALLBACK_SECONDS]
    assert calls[1][1] >= ETORO_GET_429_FALLBACK_SECONDS


def test_etoro_client_get_honors_retry_after_globally(monkeypatch):
    client, clock = build_uninitialized_client()
    sleeps = []
    calls = []
    responses = [
        FakeResponse(429, headers={'Retry-After': '7'}),
        FakeResponse(200, {'ok': True}),
    ]

    def fake_get(*args, **kwargs):
        calls.append(clock.now())
        return responses.pop(0)

    monkeypatch.setattr(requests, 'get', fake_get)
    monkeypatch.setattr('time.sleep', lambda seconds: sleeps.append(seconds))

    assert client._get('/path') == {'ok': True}
    assert sleeps == [7.0]
    assert clock.governor_sleeps == [7.0]
    assert calls == [0.0, 7.0]


def test_one_429_blocks_other_gets_during_shared_retry_after(monkeypatch):
    client, clock = build_uninitialized_client()
    calls = []
    responses = [
        FakeResponse(429, headers={'Retry-After': '12'}),
        FakeResponse(200, {'ok': True}),
    ]

    def fake_get(*args, **kwargs):
        calls.append(clock.now())
        return responses.pop(0)

    monkeypatch.setattr(requests, 'get', fake_get)

    try:
        client._get_once('/close-confirmation')
    except requests.HTTPError:
        pass

    assert len(calls) == 1
    assert client._get_once('/pnl') == {'ok': True}
    assert calls == [0.0, 12.0]
    assert clock.governor_sleeps == [12.0]


def test_etoro_client_get_does_not_retry_non_retryable_status(monkeypatch):
    client, _ = build_uninitialized_client()
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(url)
        return FakeResponse(400)

    monkeypatch.setattr(requests, 'get', fake_get)

    assert is_retryable_http_status(400) is False

    try:
        client._get('/path')
    except requests.HTTPError:
        pass

    assert len(calls) == 1
