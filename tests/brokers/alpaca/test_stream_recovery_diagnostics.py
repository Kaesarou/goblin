import json
import threading
import time

import pytest

from app.brokers.alpaca.market_data import AlpacaMarketDataFeed
from app.brokers.alpaca.stream import AlpacaStream
from tests.brokers.alpaca.test_transport_and_data import QUOTE


class ScriptedSocket:
    def __init__(self, clock, actions):
        self.clock = clock
        self.actions = iter(actions)
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def recv(self, **_):
        advance, value = next(self.actions)
        self.clock[0] += advance
        if isinstance(value, Exception):
            raise value
        return json.dumps(value)


def acknowledged():
    return [(0, [{"T": "success", "msg": "authenticated"}]),
            (0, [{"T": "subscription", "quotes": ["AAPL"]}])]


@pytest.mark.parametrize("mode,expected_last_delay", [
    ("stable_quotes", 1), ("brief_quotes", 30), ("invalid_quotes", 30),
    ("ack_only", 30),
    ("valid_then_invalid", 30),
])
def test_backoff_resets_only_after_stable_usable_quotes(monkeypatch, caplog, mode, expected_last_delay):
    clock = [100.0]
    monkeypatch.setattr("app.brokers.alpaca.stream.time.monotonic", lambda: clock[0])
    accepted_count = 0

    def accept(_):
        nonlocal accepted_count
        accepted_count += 1
        return mode != "invalid_quotes" and (mode != "valid_then_invalid" or accepted_count == 1)

    stream = AlpacaStream(api_key="secret-key", secret_key="secret-value",
                          on_message=accept, feed="iex")
    attempts = 0
    sockets = []

    def connect(_):
        nonlocal attempts
        attempts += 1
        if attempts <= 6:
            raise ConnectionError("secret-value must never enter diagnostics")
        actions = acknowledged()
        if mode != "ack_only":
            actions += [(5, [QUOTE])] * (14 if mode != "brief_quotes" else 2)
        actions += [(0, ConnectionError("secret-key"))]
        socket = ScriptedSocket(clock, actions)
        sockets.append(socket)
        return socket

    waits = []

    def wait(delay):
        assert not stream.healthy()
        assert stream.subscribed_symbols() == ()
        waits.append(delay)
        clock[0] += delay
        if len(waits) == 7:
            stream._stop.set()

    stream._connector = connect
    monkeypatch.setattr(stream._stop, "wait", wait)
    stream.update_symbols(["AAPL"])
    stream._run()
    assert waits == [1, 2, 4, 8, 16, 30, expected_last_delay]
    assert [m["action"] for m in sockets[0].sent] == ["auth", "subscribe"]
    diagnostic = stream.diagnostics()
    assert diagnostic["disconnect_reasons"] == {"network_error": 7}
    assert diagnostic["last_disconnect"]["retry_delay_seconds"] == expected_last_delay
    assert "secret-key" not in caplog.text
    assert "secret-value" not in json.dumps(diagnostic) + caplog.text


@pytest.mark.parametrize("ack,reason", [
    (False, "authentication_subscription_timeout"), (True, "quotes_silent"),
])
def test_silence_is_distinguished_from_auth_timeout(monkeypatch, ack, reason):
    clock = [100.0]
    monkeypatch.setattr("app.brokers.alpaca.stream.time.monotonic", lambda: clock[0])
    stream = AlpacaStream(api_key="key", secret_key="secret", on_message=lambda _: None, feed="iex")
    socket = ScriptedSocket(clock, (acknowledged() if ack else []) + [(16, TimeoutError())])
    stream._connector = lambda _: socket
    monkeypatch.setattr(stream._stop, "wait", lambda _: stream._stop.set())
    stream.update_symbols(["AAPL"])
    stream._run()
    assert not stream.healthy()
    assert stream.diagnostics()["last_disconnect"]["reason"] == reason
    assert stream.diagnostics()["last_disconnect"]["retry_delay_seconds"] == 1


def test_market_silence_is_normal_when_executable_data_is_not_expected(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("app.brokers.alpaca.stream.time.monotonic", lambda: clock[0])
    stream = AlpacaStream(
        api_key="key", secret_key="secret", on_message=lambda _: None, feed="iex"
    )
    stream.set_data_expected(False)
    socket = ScriptedSocket(
        clock,
        acknowledged() + [(16, TimeoutError()), (0, ConnectionError("end"))],
    )
    stream._connector = lambda _: socket

    with pytest.raises(ConnectionError, match="end"):
        stream._connection(("AAPL",))

    assert stream.healthy()
    assert stream.diagnostics()["data_expected"] is False


def test_slow_consumer_backpressure_is_bounded_and_preserves_connection():
    feed = AlpacaMarketDataFeed(
        api_key="key",
        secret_key="secret",
        queue_capacity=1,
    )
    stream = feed._stream
    stream._healthy = True
    stream._applied = ("AAPL",)
    stream.connections = 1
    feed._on_quote({**QUOTE, "t": "2026-09-25T14:00:00Z"})

    completed = threading.Event()

    def produce_second():
        try:
            assert feed._on_quote(
                {**QUOTE, "t": "2026-09-25T14:00:01Z"}
            )
        finally:
            completed.set()

    producer = threading.Thread(target=produce_second)
    producer.start()
    deadline = time.monotonic() + 1
    while feed.queue_backpressure_events == 0 and time.monotonic() < deadline:
        time.sleep(0.001)

    diagnostic = feed.diagnostics()
    assert diagnostic["queue_size"] == 1
    assert diagnostic["queue_high_watermark"] == 1
    assert diagnostic["queue_backpressure_events"] == 1
    assert diagnostic["queue_overflows"] == 0
    assert diagnostic["discarded_quotes"] == 0
    assert diagnostic["last_disconnect"] is None
    assert not completed.is_set()

    first = feed.next_event(0)
    assert first.snapshot.timestamp.second == 0
    assert completed.wait(1)
    producer.join(timeout=1)
    second = feed.next_event(0)
    assert second.snapshot.timestamp.second == 1
    assert feed.diagnostics()["discarded_quotes"] == 0


