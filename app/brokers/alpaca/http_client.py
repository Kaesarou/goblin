from __future__ import annotations

import threading
import time
from collections import deque

import requests

from app.brokers.alpaca.environment import AlpacaEnvironment

DATA_API_URL = "https://data.alpaca.markets"


class AlpacaHttpClient:
    """Bounded reads and single-shot mutations, with independent API budgets."""

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        *,
        data: bool = False,
        environment: AlpacaEnvironment = AlpacaEnvironment.DEMO,
        transport=None,
        clock=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        if not api_key.strip() or not secret_key.strip():
            raise ValueError("Alpaca API key and secret are required")
        self.environment = AlpacaEnvironment(environment)
        self.base_url = DATA_API_URL if data else self.environment.api_url
        self._headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key}
        self._transport = transport or requests.request
        self._clock, self._sleep = clock, sleep
        self._lock = threading.Lock()
        self._requests: deque[float] = deque()
        self._cooldown_until = 0.0
        self.calls = 0
        self.rate_limits = 0

    def request(self, method: str, path: str, *, params=None, json=None):
        if not path.startswith("/v2/"):
            raise ValueError("Alpaca API path must be relative to /v2/")
        # V3 owns the persisted close-lookup retry deadline. Inner retries would
        # multiply attempts and hide 429/timeouts from that scheduler.
        attempts = 3 if method == "GET" and path != "/v2/orders:by_client_order_id" else 1
        for attempt in range(attempts):
            self._acquire()
            try:
                response = self._transport(
                    method,
                    self.base_url + path,
                    headers=self._headers,
                    params=params,
                    json=json,
                    timeout=(5, 10),
                    allow_redirects=False,
                )
            except (requests.ConnectionError, requests.Timeout):
                if attempt + 1 == attempts:
                    raise
                self._sleep(2**attempt)
                continue
            if response.status_code == 429:
                self.rate_limits += 1
                try:
                    delay = float(response.headers.get("Retry-After", "60"))
                except (ValueError, TypeError):
                    delay = 60.0
                # Invalid hints cannot turn a rate limit into a busy retry loop.
                delay = delay if 1 <= delay <= 300 else 60.0
                with self._lock:
                    self._cooldown_until = max(self._cooldown_until, self._clock() + delay)
            if response.status_code in {429, 500, 502, 503, 504} and attempt + 1 < attempts:
                self._sleep(2**attempt)
                continue
            response.raise_for_status()
            if not 200 <= response.status_code < 300:
                raise RuntimeError("Unexpected Alpaca HTTP redirect or response")
            return None if response.status_code == 204 else response.json()
        raise AssertionError("Unreachable Alpaca request state")

    def _acquire(self) -> None:
        # Keep headroom below the standard 200 requests/minute API budget.
        while True:
            with self._lock:
                now = self._clock()
                while self._requests and self._requests[0] <= now - 60:
                    self._requests.popleft()
                delay = max(0.0, self._cooldown_until - now)
                if len(self._requests) >= 180:
                    delay = max(delay, self._requests[0] + 60 - now)
                if delay <= 0:
                    self._requests.append(now)
                    self.calls += 1
                    return
            self._sleep(delay)
