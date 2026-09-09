import json
from datetime import UTC, datetime, timedelta

import pytest

from app.brokers.etoro.websocket_feed import EtoroWebSocketMarketDataFeed
from app.brokers.etoro.websocket_protocol import parse_websocket_events
from app.market.models import MarketSnapshot, TimestampSource
from app.market_data.models import MarketDataEvent, MarketDataSource


class _RestClient:
    def resolve_instrument_ids(self, symbols):
        return {symbol: index + 1 for index, symbol in enumerate(symbols)}


def _frame(*, message_type, content):
    return json.dumps({
        "messages": [{
            "id": "msg",
            "topic": "instrument:1",
            "type": message_type,
            "content": content,
        }]
    })


def test_timestamp_less_patch_does_not_reuse_stale_snapshot_timestamp():
    state = {}
    initial_received = datetime(2026, 9, 9, 13, 30, tzinfo=UTC)
    initial = parse_websocket_events(
        _frame(
            message_type="snapshot",
            content={
                "Bid": 100.0,
                "Ask": 100.2,
                "Last": 100.1,
                "Date": "2026-09-09T13:30:00Z",
            },
        ),
        symbol_by_instrument_id={1: "TMUS"},
        received_at=initial_received,
        connection_id="c1",
        rate_state_by_instrument_id=state,
    )
    assert initial[0].snapshot.timestamp == initial_received
    assert initial[0].snapshot.timestamp_source is TimestampSource.BROKER

    patch_received = initial_received + timedelta(minutes=5)
    patched = parse_websocket_events(
        _frame(
            message_type="patch",
            content={"Bid": 101.0, "Ask": 101.2, "Last": 101.1},
        ),
        symbol_by_instrument_id={1: "TMUS"},
        received_at=patch_received,
        connection_id="c1",
        rate_state_by_instrument_id=state,
    )

    assert patched[0].snapshot.timestamp == patch_received
    assert patched[0].snapshot.timestamp_source is TimestampSource.LOCAL_RECEIVE_TIME
    assert patched[0].snapshot.bid == 101.0
    assert patched[0].state_reconstructed is True


def test_queue_overflow_is_reconnectable_not_feed_fatal():
    feed = EtoroWebSocketMarketDataFeed(
        api_key="key",
        user_key="user",
        rest_client=_RestClient(),
        queue_capacity=1,
        global_silence_seconds=15.0,
    )
    now = datetime(2026, 9, 9, 13, 30, tzinfo=UTC)
    event = MarketDataEvent(
        symbol="AAPL",
        source=MarketDataSource.WEBSOCKET,
        received_at=now,
        snapshot=MarketSnapshot("AAPL", 100.0, 100.1, 100.05, now),
    )

    feed._publish(event)
    with pytest.raises(RuntimeError, match="Market-data queue overflow"):
        feed._publish(event)

    diagnostics = feed.diagnostics()
    assert diagnostics["queue_overflows"] == 1
    assert diagnostics["fatal_error"] is None
    assert not feed._stop_event.is_set()
