from __future__ import annotations

import queue
from datetime import UTC, datetime

from app.brokers.alpaca.schema import number, timestamp
from app.brokers.alpaca.stream import AlpacaStream
from app.market.models import MarketSnapshot, PriceSource
from app.market_data.contracts import LiveMarketDataFeed
from app.market_data.models import MarketDataEvent, MarketDataSource


def quote_snapshot(symbol: str, quote: dict, *, received_at: datetime) -> MarketSnapshot:
    bid = float(number(quote.get("bp"), positive=True))
    ask = float(number(quote.get("ap"), positive=True))
    if ask < bid:
        raise ValueError("Crossed Alpaca quote")
    return MarketSnapshot(symbol, bid, ask, (bid + ask) / 2, timestamp(quote.get("t")),
                          received_at=received_at, price_source=PriceSource.BID_ASK_MIDPOINT)


class AlpacaRestMarketDataClient:
    def __init__(self, http, *, feed: str = "iex") -> None:
        if feed not in {"iex", "sip"}:
            raise ValueError("Only real-time IEX and SIP feeds are supported")
        self.http, self.feed = http, feed

    def get_market_snapshots(self, symbols: list[str]) -> dict[str, MarketSnapshot]:
        if not symbols:
            return {}
        payload = self.http.request("GET", "/v2/stocks/quotes/latest", params={
            "symbols": ",".join(sorted(set(symbols))), "feed": self.feed, "currency": "USD",
        })
        quotes = payload.get("quotes") if isinstance(payload, dict) else None
        if not isinstance(quotes, dict):
            raise ValueError("Missing Alpaca quotes collection")
        now = datetime.now(UTC)
        return {symbol: quote_snapshot(symbol, quotes[symbol], received_at=now)
                for symbol in symbols}


class AlpacaMarketDataFeed(LiveMarketDataFeed):
    def __init__(self, *, api_key: str, secret_key: str, feed: str = "iex",
                 trade_stream=None, queue_capacity=4096, connector=None,
                 global_silence_seconds=15.0) -> None:
        self._queue: queue.Queue[MarketDataEvent] = queue.Queue(maxsize=queue_capacity)
        self._last_timestamp: dict[str, datetime] = {}
        self._trade_stream = trade_stream
        self._stream = AlpacaStream(
            api_key=api_key, secret_key=secret_key, feed=feed, on_message=self._on_quote,
            connector=connector, silence_seconds=global_silence_seconds,
        )
        self.invalid_quotes = self.ordering_drops = self.queue_overflows = 0

    @property
    def requires_websocket_health(self) -> bool:
        return True

    def start(self, symbols: list[str]) -> None:
        if self._trade_stream:
            self._trade_stream.start()
        self._stream.start(symbols)

    def update_symbols(self, symbols: list[str]) -> None:
        self._stream.update_symbols(symbols)

    def subscribed_symbols(self) -> tuple[str, ...]:
        return self._stream.subscribed_symbols()

    def next_event(self, timeout_seconds: float) -> MarketDataEvent | None:
        self._stream.check_error()
        if self._trade_stream:
            self._trade_stream.check_error()
        try:
            return self._queue.get(timeout=timeout_seconds)
        except queue.Empty:
            return None

    def stop(self) -> None:
        self._stream.stop()
        if self._trade_stream:
            self._trade_stream.stop()

    def connection_healthy(self) -> bool:
        return self._stream.healthy()

    def diagnostics(self) -> dict:
        return {**self._stream.diagnostics(), "mode": "alpaca_websocket",
                "invalid_quotes": self.invalid_quotes, "ordering_drops": self.ordering_drops,
                "queue_overflows": self.queue_overflows, "queue_size": self._queue.qsize(),
                "trade_updates": self._trade_stream.diagnostics() if self._trade_stream else None}

    def _on_quote(self, message: dict) -> None:
        symbol = message.get("S")
        if symbol not in self._stream.subscribed_symbols():
            return
        now = datetime.now(UTC)
        try:
            snapshot = quote_snapshot(symbol, message, received_at=now)
        except (ValueError, TypeError):
            self.invalid_quotes += 1
            return
        previous = self._last_timestamp.get(symbol)
        if previous is not None and snapshot.timestamp <= previous:
            self.ordering_drops += 1
            return
        self._last_timestamp[symbol] = snapshot.timestamp
        event = MarketDataEvent(symbol, MarketDataSource.WEBSOCKET, now, snapshot,
                                connection_id=str(self._stream.connections))
        try:
            self._queue.put_nowait(event)
        except queue.Full as exc:
            self.queue_overflows += 1
            # Discard buffered stale quotes before reconnecting and rebuilding freshness.
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
            raise RuntimeError("Alpaca quote queue overflow") from exc
