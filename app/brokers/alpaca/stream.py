from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from datetime import UTC, datetime

from app.brokers.alpaca.environment import AlpacaEnvironment

logger = logging.getLogger(__name__)


class StreamFailure(RuntimeError):
    """A fixed diagnostic category, never a broker frame or exception message."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class QuoteQueueOverflow(StreamFailure):
    def __init__(self):
        super().__init__("quote_queue_overflow")


def _connect(url):
    from websockets.sync.client import connect

    return connect(
        url, open_timeout=10, close_timeout=2, ping_interval=10, ping_timeout=10, max_queue=1024
    )


class AlpacaStream:
    """Reconnectable JSON stream; trading also uses binary JSON frames."""

    def __init__(
        self,
        *,
        api_key: str,
        secret_key: str,
        on_message,
        feed: str | None = None,
        connector=None,
        silence_seconds=15.0,
        stable_reset_seconds=60.0,
        diagnostics_context=None,
        environment: AlpacaEnvironment = AlpacaEnvironment.DEMO,
    ) -> None:
        if feed not in {None, "iex", "sip"}:
            raise ValueError("Only real-time IEX and SIP feeds are supported")
        self.url = (
            f"wss://stream.data.alpaca.markets/v2/{feed}"
            if feed
            else AlpacaEnvironment(environment).stream_url
        )
        self._auth = {"action": "auth", "key": api_key, "secret": secret_key}
        self._market = feed is not None
        self._callback = on_message
        self._connector = connector or _connect
        self._silence_seconds = silence_seconds
        self._stable_reset_seconds = stable_reset_seconds
        self._diagnostics_context = diagnostics_context or (lambda: {})
        self._stable_since: float | None = None
        self._last_accepted_data: float | None = None
        self._backoff_reset_ready = False
        self._connected_at: float | None = None
        self._last_disconnect: dict | None = None
        self._disconnect_reasons: Counter[str] = Counter()
        self._reconnect_delay = 0.0
        self._messages_received = 0
        self._stop = threading.Event()
        self._changed = threading.Event()
        self._thread: threading.Thread | None = None
        self._desired: tuple[str, ...] = ()
        self._applied: tuple[str, ...] = ()
        self._healthy = False
        self._fatal: Exception | None = None
        self._last_error: str | None = None
        self._last_data = 0.0
        self.connections = 0

    def start(self, symbols=()) -> None:
        self.update_symbols(symbols)
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="alpaca-stream", daemon=True)
        self._thread.start()

    def update_symbols(self, symbols) -> None:
        desired = tuple(sorted(set(symbols)))
        if desired != self._desired:
            self._desired = desired
            self._healthy = False
            self._changed.set()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=12)

    def check_error(self) -> None:
        if self._fatal is not None:
            raise RuntimeError(
                "Alpaca stream failed; check credentials/feed entitlement"
            ) from self._fatal

    def healthy(self) -> bool:
        return self._healthy and (
            not self._market or time.monotonic() - self._last_data < self._silence_seconds
        )

    def subscribed_symbols(self) -> tuple[str, ...]:
        return self._applied if self._healthy else ()

    def diagnostics(self) -> dict:
        return {
            "healthy": self.healthy(),
            "connections": self.connections,
            "last_error": self._last_error,
            "fatal": self._fatal is not None,
            "messages_received": self._messages_received,
            "last_data_age_seconds": (
                max(0.0, time.monotonic() - self._last_data) if self._messages_received else None
            ),
            "disconnect_reasons": dict(self._disconnect_reasons),
            "last_disconnect": self._last_disconnect,
            "reconnect_delay_seconds": self._reconnect_delay,
        }

    def _record_disconnect(self, exc: Exception, delay: float) -> None:
        now = time.monotonic()
        if isinstance(exc, StreamFailure):
            reason = exc.reason
        elif isinstance(exc, PermissionError):
            reason = "authorization_or_subscription_denied"
        elif type(exc).__name__.startswith("ConnectionClosed"):
            reason = "remote_close"
        elif isinstance(exc, TimeoutError):
            reason = "transport_timeout"
        elif isinstance(exc, (ConnectionError, OSError)):
            reason = "network_error"
        elif isinstance(exc, (ValueError, TypeError)):
            reason = "invalid_frame"
        else:
            reason = "stream_error"
        self._last_error = type(exc).__name__
        close_code = getattr(getattr(exc, "rcvd", None), "code", None)
        self._disconnect_reasons[reason] += 1
        self._reconnect_delay = delay
        # Exception text, remote close reasons and frames may contain secrets.
        self._last_disconnect = {
            "at": datetime.now(UTC).isoformat(),
            "stream": "market" if self._market else "trade_updates",
            "connection_id": self.connections,
            "reason": reason,
            "error_type": self._last_error,
            "close_code": close_code if isinstance(close_code, int) else None,
            "connection_duration_seconds": (
                max(0.0, now - self._connected_at) if self._connected_at is not None else 0.0
            ),
            "last_data_age_seconds": max(0.0, now - self._last_data),
            "retry_delay_seconds": delay,
            **self._diagnostics_context(),
        }
        logger.warning("alpaca_stream_disconnected %s", json.dumps(self._last_disconnect))

    def _run(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            self._changed.clear()
            symbols = self._desired
            if self._market and not symbols:
                self._stop.wait(0.25)
                continue
            try:
                self._connection(symbols)
                delay = 1.0
            except PermissionError as exc:
                self._fatal = exc
                self._healthy = False
                self._applied = ()
                self._record_disconnect(exc, 0.0)
                return
            except Exception as exc:
                # Invalidate transport authority before the reconnect backoff.
                self._healthy = False
                self._applied = ()
                if self._stop.is_set():
                    break
                if self._backoff_reset_ready:
                    delay = 1.0
                self._record_disconnect(exc, delay)
                self._stop.wait(delay)
                delay = min(30.0, delay * 2)
            finally:
                self._healthy = False
                self._applied = ()

    def _connection(self, symbols: tuple[str, ...]) -> None:
        self.connections += 1
        self._connected_at = time.monotonic()
        self._stable_since = None
        self._last_accepted_data = None
        self._backoff_reset_ready = False
        self._reconnect_delay = 0.0
        deadline = time.monotonic() + 10
        authenticated = False
        self._last_data = time.monotonic()
        with self._connector(self.url) as websocket:
            websocket.send(json.dumps(self._auth))
            while not self._stop.is_set() and not self._changed.is_set():
                if not self._healthy and time.monotonic() > deadline:
                    raise StreamFailure("authentication_subscription_timeout")
                if self._market and self._healthy and not self.healthy():
                    raise StreamFailure("quotes_silent")
                if (self.healthy() and self._stable_since is not None
                        and (not self._market or (self._last_accepted_data is not None
                             and time.monotonic() - self._last_accepted_data < self._silence_seconds))
                        and time.monotonic() - self._stable_since >= self._stable_reset_seconds):
                    self._backoff_reset_ready = True
                try:
                    frame = json.loads(websocket.recv(timeout=0.5))
                except TimeoutError:
                    continue
                messages = frame if isinstance(frame, list) else [frame]
                for message in messages:
                    if not isinstance(message, dict):
                        raise ValueError("Invalid Alpaca stream frame")
                    kind = message.get("T") if self._market else message.get("stream")
                    data = message.get("data", {})
                    if kind == "error" or message.get("action") == "error":
                        if message.get("code") in {400, 401, 402, 403, 405, 409}:
                            raise PermissionError("Alpaca denied authentication or subscription")
                        raise StreamFailure("broker_stream_error")
                    auth_ok = (
                        kind == "success" and message.get("msg") == "authenticated"
                        if self._market
                        else kind == "authorization" and data.get("status") == "authorized"
                    )
                    if kind == "authorization" and data.get("status") != "authorized":
                        raise PermissionError("Alpaca trading stream authorization failed")
                    if auth_ok and not authenticated:
                        authenticated = True
                        subscription = (
                            {"action": "subscribe", "quotes": list(symbols)}
                            if self._market
                            else {"action": "listen", "data": {"streams": ["trade_updates"]}}
                        )
                        websocket.send(json.dumps(subscription))
                    elif kind in {"subscription", "listening"}:
                        expected = set(symbols) if self._market else {"trade_updates"}
                        actual = set(
                            message.get("quotes", []) if self._market else data.get("streams", [])
                        )
                        if not authenticated or actual != expected:
                            raise PermissionError("Alpaca subscription incomplete")
                        self._healthy = True
                        self._applied = symbols
                        if not self._market:
                            # An idle order-update stream is normal; quotes are
                            # required only for a market stream's stable period.
                            self._stable_since = time.monotonic()
                    elif self._healthy and kind == ("q" if self._market else "trade_updates"):
                        self._last_data = time.monotonic()
                        self._messages_received += 1
                        accepted = self._callback(message if self._market else data)
                        if accepted is not False:
                            accepted_at = time.monotonic()
                            if self._stable_since is None or (self._market
                                    and self._last_accepted_data is not None
                                    and accepted_at - self._last_accepted_data >= self._silence_seconds):
                                self._stable_since = accepted_at
                            self._last_accepted_data = accepted_at
