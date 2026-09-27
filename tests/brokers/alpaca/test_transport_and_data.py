import json
from datetime import UTC, datetime

import pytest
import requests

from app.brokers.alpaca.http_client import AlpacaHttpClient
from app.brokers.alpaca.market_data import (
    AlpacaMarketDataFeed,
    AlpacaRestMarketDataClient,
    quote_snapshot,
)
from app.brokers.alpaca.stream import AlpacaStream
from app.market.models import PriceSource

NOW = datetime(2026, 9, 25, 14, 0, tzinfo=UTC)
QUOTE = {"T": "q", "S": "AAPL", "bp": 100, "ap": 102,
         "t": "2026-09-25T14:00:00.123456789Z"}


def response(status, payload=None, headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode()
    result.headers.update(headers or {})
    return result


@pytest.mark.parametrize("failure", [requests.Timeout(), response(503), response(429)])
def test_mutation_is_never_retried(failure):
    calls = []

    def transport(*args, **kwargs):
        calls.append((args, kwargs))
        if isinstance(failure, Exception):
            raise failure
        return failure

    client = AlpacaHttpClient("key", "secret", transport=transport)
    with pytest.raises(requests.RequestException):
        client.request("POST", "/v2/orders", json={"client_order_id": "persisted"})
    assert len(calls) == 1
    assert calls[0][0][1] == "https://paper-api.alpaca.markets/v2/orders"
    assert calls[0][1]["allow_redirects"] is False


def test_read_retries_respect_shared_rate_limit_cooldown():
    now = [0.0]
    call_times = []
    responses = iter([response(429, headers={"Retry-After": "4"}), response(200, {"ok": True})])

    def transport(*args, **kwargs):
        call_times.append(now[0])
        return next(responses)

    client = AlpacaHttpClient("key", "secret", transport=transport, clock=lambda: now[0],
                             sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
    assert client.request("GET", "/v2/account") == {"ok": True}
    assert call_times == [0, 4]
    assert client.rate_limits == 1


def test_quotes_preserve_broker_time_and_explicit_midpoint_provenance():
    snapshot = quote_snapshot("AAPL", QUOTE, received_at=NOW)
    assert (snapshot.bid, snapshot.ask, snapshot.last) == (100, 102, 101)
    assert snapshot.timestamp.microsecond == 123456
    assert snapshot.received_at == NOW
    assert snapshot.price_source == PriceSource.BID_ASK_MIDPOINT


@pytest.mark.parametrize("patch", [{"bp": 0}, {"ap": "NaN"}, {"ap": 99},
                                    {"bp": True}, {"t": "2026-09-25T14:00:00"}])
def test_invalid_quotes_are_not_synthesized(patch):
    with pytest.raises(ValueError):
        quote_snapshot("AAPL", {**QUOTE, **patch}, received_at=NOW)


def test_rest_fallback_pins_same_feed_and_rejects_missing_symbol():
    calls = []
    http = type("Http", (), {"request": lambda _, *args, **kwargs:
                            calls.append((args, kwargs)) or {"quotes": {"AAPL": QUOTE}}})()
    client = AlpacaRestMarketDataClient(http, feed="sip")
    assert client.get_market_snapshots(["AAPL"])["AAPL"].last == 101
    assert calls[0][1]["params"]["feed"] == "sip"
    with pytest.raises(KeyError):
        client.get_market_snapshots(["MSFT"])


class Socket:
    def __init__(self, frames, stop):
        self.frames = iter(frames)
        self.sent = []
        self.stop = stop

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def send(self, frame):
        self.sent.append(json.loads(frame))

    def recv(self, **kwargs):
        try:
            frame = next(self.frames)
        except StopIteration:
            self.stop.set()
            raise TimeoutError from None
        return json.dumps(frame).encode()  # paper trade_updates can be binary JSON


@pytest.mark.parametrize("market", [False, True])
def test_authenticated_subscription_and_binary_frames(market):
    received = []
    stream = AlpacaStream(api_key="key", secret_key="secret", on_message=received.append,
                          feed="iex" if market else None)
    frames = ([
        [{"T": "success", "msg": "connected"}],
        [{"T": "success", "msg": "authenticated"}],
        [{"T": "subscription", "quotes": ["AAPL"]}], [QUOTE],
    ] if market else [
        {"stream": "authorization", "data": {"status": "authorized"}},
        {"stream": "listening", "data": {"streams": ["trade_updates"]}},
        {"stream": "trade_updates", "data": {"event": "partial_fill", "order": {"id": "o"}}},
    ])
    socket = Socket(frames, stream._stop)
    stream._connector = lambda url: socket
    stream._connection(("AAPL",))
    assert len(received) == 1
    assert socket.sent[0]["action"] == "auth"
    assert socket.sent[1]["action"] == ("subscribe" if market else "listen")


@pytest.mark.parametrize("frames", [
    [[{"T": "error", "code": 409, "msg": "insufficient subscription"}]],
    [[{"T": "success", "msg": "authenticated"}], [{"T": "subscription", "quotes": []}]],
])
def test_stream_denied_or_incomplete_subscription_fails_closed(frames):
    stream = AlpacaStream(api_key="key", secret_key="secret", on_message=lambda _: None,
                          feed="iex")
    stream._connector = lambda url: Socket(frames, stream._stop)
    with pytest.raises(PermissionError):
        stream._connection(("AAPL",))
    assert not stream.healthy()


def test_feed_drops_out_of_order_quotes_and_fails_on_queue_overflow():
    feed = AlpacaMarketDataFeed(api_key="key", secret_key="secret", queue_capacity=1)
    feed._stream._healthy = True
    feed._stream._applied = ("AAPL",)
    feed._on_quote(QUOTE)
    feed._on_quote(QUOTE)
    assert feed.ordering_drops == 1
    with pytest.raises(RuntimeError, match="overflow"):
        feed._on_quote({**QUOTE, "t": "2026-09-25T14:00:01Z"})
    assert feed.next_event(0) is None
    assert feed.queue_overflows == 1
