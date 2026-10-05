import json

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


def test_slow_consumer_overflow_is_bounded_and_recovers_with_fresh_quotes(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("app.brokers.alpaca.stream.time.monotonic", lambda: clock[0])
    feed = AlpacaMarketDataFeed(api_key="key", secret_key="secret", queue_capacity=4)
    stream = feed._stream
    first = ScriptedSocket(clock, acknowledged() + [
        (0.1, [{**QUOTE, "t": f"2026-09-25T14:00:0{i}Z"}]) for i in range(5)
    ])
    second = ScriptedSocket(clock, acknowledged() + [
        (0.1, [{**QUOTE, "t": "2026-09-25T14:00:10Z"}]),
    ])
    sockets = iter([first, second])
    stream._connector = lambda _: next(sockets)
    received = []
    callback = stream._callback

    def consume_after_reconnect(message):
        accepted = callback(message)
        if stream.connections == 2:
            received.append(feed.next_event(0))
            assert feed.connection_healthy()
            stream._stop.set()
        return accepted

    stream._callback = consume_after_reconnect

    def backoff(_):
        assert not feed.connection_healthy()
        assert feed.next_event(0) is None
        d = feed.diagnostics()
        assert d["queue_size"] == 0
        assert d["discarded_quotes"] == 5
        assert d["last_disconnect"]["reason"] == "quote_queue_overflow"
        assert d["last_disconnect"]["queue_high_watermark"] == 4

    monkeypatch.setattr(stream._stop, "wait", backoff)
    stream.update_symbols(["AAPL"])
    stream._run()
    assert len(received) == 1
    assert received[0].connection_id == "2"
    assert received[0].snapshot.timestamp.second == 10
    assert feed.diagnostics()["queue_overflows"] == 1
    assert all([m["action"] for m in s.sent] == ["auth", "subscribe"] for s in [first, second])
