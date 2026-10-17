"""Alpaca outage recovery: subscribed does not mean executable data."""

import time

import pytest

from app.brokers.alpaca.market_data import AlpacaMarketDataFeed
from app.brokers.alpaca.stream import AlpacaStream, StreamFailure
from tests.brokers.alpaca.test_stream_recovery_diagnostics import (
    ScriptedSocket, acknowledged,
)
from tests.brokers.alpaca.test_transport_and_data import QUOTE


def test_subscription_without_fresh_quotes_is_not_entry_ready(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("app.brokers.alpaca.stream.time.monotonic", lambda: clock[0])
    stream = AlpacaStream(api_key="key", secret_key="secret",
                          on_message=lambda _: True, feed="iex")
    stream._healthy = True
    stream._applied = ("AAPL",)
    stream._last_data = clock[0]
    assert stream.healthy()  # transport/subscription health remains backward-compatible
    assert not stream.fresh_quotes_healthy()
    assert stream.diagnostics()["market_data_phase"] == "recovering"

    stream._last_accepted_data = clock[0]
    assert stream.fresh_quotes_healthy()
    clock[0] += 16
    assert not stream.fresh_quotes_healthy()
    assert stream.diagnostics()["market_data_phase"] == "recovering"


def test_outage_with_ack_but_no_quotes_triggers_silence(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("app.brokers.alpaca.stream.time.monotonic", lambda: clock[0])
    stream = AlpacaStream(api_key="key", secret_key="secret",
                          on_message=lambda _: True, feed="iex")
    socket = ScriptedSocket(
        clock, acknowledged() + [(16, TimeoutError())],
    )
    stream._connector = lambda _: socket
    with pytest.raises(StreamFailure) as err:
        stream._connection(("AAPL",))
    assert err.value.reason == "quotes_silent"
    assert not stream.fresh_quotes_healthy()
    assert [item["action"] for item in socket.sent] == ["auth", "subscribe"]


def test_quote_rejection_reasons_preserve_strict_ordering():
    feed = AlpacaMarketDataFeed(api_key="key", secret_key="secret")
    feed._stream._healthy = True
    feed._stream._applied = ("AAPL",)
    feed._stream.connections = 1
    assert feed._on_quote(QUOTE)
    assert not feed._on_quote({**QUOTE, "bp": 100.01})
    assert not feed._on_quote({**QUOTE, "t": "2026-09-25T14:00:00Z"})
    diagnostic = feed.diagnostics()
    assert diagnostic["ordering_equal_timestamps"] == 1
    assert diagnostic["ordering_older_timestamps"] == 1
    assert diagnostic["ordering_by_symbol"] == {"AAPL": 2}

    feed._stream.connections = 2
    assert feed.next_event(0) is None
    assert feed.diagnostics()["ordering_old_epoch"] == 1
    assert feed.diagnostics()["ordering_drops"] == 3
