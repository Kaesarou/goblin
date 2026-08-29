from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from threading import Lock


ETORO_GET_MAX_REQUESTS_PER_WINDOW = 45
ETORO_GET_RATE_WINDOW_SECONDS = 60.0
ETORO_GET_429_FALLBACK_SECONDS = 60.0


class EtoroGetRateGovernor:
    """Serialize the eToro user-key REST GET budget across client threads.

    eToro applies GET limits over a rolling one-minute window for a user key.
    Goblin has independent execution, maintenance and REST market-data callers,
    so endpoint-local retry/backoff is not sufficient: every eToro REST GET using
    that user key shares one budget and one global 429 cooldown.

    The default 45/minute budget intentionally leaves headroom below the broker's
    published 60/minute read ceiling for incidental calls and timing jitter. POST
    order/close mutations and the WebSocket market-data stream are deliberately
    outside this governor.
    """

    def __init__(
        self,
        *,
        max_requests: int = ETORO_GET_MAX_REQUESTS_PER_WINDOW,
        window_seconds: float = ETORO_GET_RATE_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_requests <= 0:
            raise ValueError('max_requests must be positive')
        if window_seconds <= 0:
            raise ValueError('window_seconds must be positive')
        self.max_requests = int(max_requests)
        self.window_seconds = float(window_seconds)
        self._clock = clock
        self._sleep = sleeper
        self._timestamps: deque[float] = deque()
        self._not_before = 0.0
        self._lock = Lock()
        self.wait_count = 0
        self.wait_seconds = 0.0
        self.cooldown_count = 0

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                self._prune(now)
                cooldown_wait = max(0.0, self._not_before - now)
                rate_wait = 0.0
                if len(self._timestamps) >= self.max_requests:
                    rate_wait = max(
                        0.0,
                        self._timestamps[0] + self.window_seconds - now,
                    )
                wait_seconds = max(cooldown_wait, rate_wait)
                if wait_seconds <= 0.0:
                    self._timestamps.append(now)
                    return
                self.wait_count += 1
                self.wait_seconds += wait_seconds
            self._sleep(wait_seconds)

    def defer(self, seconds: float) -> None:
        delay = max(0.0, float(seconds))
        if delay <= 0.0:
            return
        with self._lock:
            self._not_before = max(self._not_before, self._clock() + delay)
            self.cooldown_count += 1

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            now = self._clock()
            self._prune(now)
            return {
                'max_requests': self.max_requests,
                'window_seconds': self.window_seconds,
                'requests_in_window': len(self._timestamps),
                'cooldown_remaining_seconds': max(0.0, self._not_before - now),
                'wait_count': self.wait_count,
                'wait_seconds': self.wait_seconds,
                'cooldown_count': self.cooldown_count,
            }

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._timestamps and self._timestamps[0] <= cutoff:
            self._timestamps.popleft()
