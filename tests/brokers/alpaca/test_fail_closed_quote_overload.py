"""Local feed overload: never turn missing/stale crossings into fresh orders."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import time

import pytest

from app.brokers.alpaca.market_data import AlpacaMarketDataFeed
from app.brokers.alpaca.stream import QuoteQueueOverflow

QUOTE = {"T": "q", "S": "AAPL", "bp": 100, "ap": 101, "t": "2026-10-12T14:00:00Z"}


def feed_with_active_socket(capacity=2):
    feed = AlpacaMarketDataFeed(api_key="key", secret_key="secret", queue_capacity=capacity)
    feed._stream._healthy = True
    feed._stream._applied = ("AAPL",)
    feed._stream.connections = 1
    return feed


def test_stale_fifo_delivery_causes_reconnect_without_replaying_crossing():
    feed = feed_with_active_socket()
    assert feed._on_quote(QUOTE)
    event = feed._queue.get_nowait()
    feed._queue.put_nowait(replace(
        event, received_at=datetime.now(UTC) - timedelta(seconds=8),
    ))
    assert feed.next_event(0) is None
    assert not feed.connection_healthy()
    assert feed.diagnostics()["stale_queue_events"] == 1
    assert feed.diagnostics()["disconnect_reasons"]["quote_queue_stale"] == 1
    assert feed._stream._changed.is_set()


def test_burst_over_capacity_fails_in_bounded_time_and_drops_old_epoch():
    feed = feed_with_active_socket(capacity=128)
    base = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)
    for index in range(128):
        stamp = (base + timedelta(microseconds=index + 1)).isoformat()
        assert feed._on_quote({**QUOTE, "t": stamp})
    started = time.monotonic()
    with pytest.raises(QuoteQueueOverflow):
        feed._on_quote({**QUOTE, "t": (base + timedelta(seconds=1)).isoformat()})
    assert time.monotonic() - started < 0.5
    assert feed.queue_overflows == 1
    assert feed._queue.qsize() == 0
    assert feed.epoch_queue_discarded == 128
    assert not feed.connection_healthy()

    feed._stream.connections = 2
    feed._stream._healthy = True
    feed._stream._applied = ("AAPL",)
    assert feed.next_event(0) is None  # old epoch already purged
    assert feed._on_quote({**QUOTE, "t": (base + timedelta(seconds=2)).isoformat()})
    fresh = feed.next_event(0)
    assert fresh is not None and fresh.connection_id == "2"


def test_old_stale_epoch_must_not_disconnect_new_socket():
    feed = feed_with_active_socket()
    assert feed._on_quote(QUOTE)
    old = feed._queue.get_nowait()
    feed._queue.put_nowait(replace(
        old, received_at=datetime.now(UTC) - timedelta(seconds=25),
    ))
    feed._stream.connections = 2
    feed._stream._healthy = True
    feed._stream._applied = ("AAPL",)
    assert feed.next_event(0) is None
    assert feed.connection_healthy()
    assert feed.stale_queue_events == 0
    assert feed.diagnostics()["disconnect_reasons"] == {}
    assert feed._on_quote({**QUOTE, "t": "2026-10-12T14:00:01Z"})
    fresh = feed.next_event(0)
    assert fresh is not None and fresh.connection_id == "2"


def test_stale_current_epoch_is_purged_once_not_replayed_as_crossings():
    feed = feed_with_active_socket(capacity=10)
    assert feed._on_quote(QUOTE)
    old = feed._queue.get_nowait()
    feed._queue.put_nowait(replace(
        old, received_at=datetime.now(UTC) - timedelta(seconds=20),
    ))
    assert feed._on_quote({**QUOTE, "t": "2026-10-12T14:00:01Z"})
    # Last queued tick is stale too; it must not trigger another reconnect.
    assert feed.next_event(0) is None
    assert feed.epoch_queue_discarded == 1
    assert feed.next_event(0) is None
    assert feed.diagnostics()["disconnect_reasons"]["quote_queue_stale"] == 1
