from __future__ import annotations

import json
import threading
import time

from app.brokers.alpaca.environment import AlpacaEnvironment


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
        }

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
                self._last_error = str(exc)
                return
            except Exception as exc:
                # Never log frames: the authentication frame contains secrets.
                self._last_error = type(exc).__name__
                # Invalidate transport authority before the reconnect backoff.
                self._healthy = False
                self._applied = ()
                self._stop.wait(delay)
                delay = min(30.0, delay * 2)
            finally:
                self._healthy = False
                self._applied = ()

    def _connection(self, symbols: tuple[str, ...]) -> None:
        self.connections += 1
        deadline = time.monotonic() + 10
        authenticated = False
        self._last_data = time.monotonic()
        with self._connector(self.url) as websocket:
            websocket.send(json.dumps(self._auth))
            while not self._stop.is_set() and not self._changed.is_set():
                if not self._healthy and time.monotonic() > deadline:
                    raise TimeoutError("Alpaca authentication/subscription timeout")
                if self._market and self._healthy and not self.healthy():
                    raise TimeoutError("Alpaca quotes silent")
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
                        raise RuntimeError("Alpaca stream error")
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
                    elif self._healthy and kind == ("q" if self._market else "trade_updates"):
                        self._last_data = time.monotonic()
                        self._callback(message if self._market else data)
