from __future__ import annotations

import queue
import time
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
    return MarketSnapshot(
        symbol,
        bid,
        ask,
        (bid + ask) / 2,
        timestamp(quote.get("t")),
        received_at=received_at,
        price_source=PriceSource.BID_ASK_MIDPOINT,
    )


class AlpacaRestMarketDataClient:
    def __init__(self, http, *, feed: str = "iex") -> None:
        if feed not in {"iex", "sip"}:
            raise ValueError("Only real-time IEX and SIP feeds are supported")
        self.http, self.feed = http, feed

    def _latest_quotes(self, symbols: list[str]) -> dict[str, dict]:
        if not symbols:
            return {}
        payload = self.http.request(
            "GET",
            "/v2/stocks/quotes/latest",
            params={
                "symbols": ",".join(sorted(set(symbols))),
                "feed": self.feed,
                "currency": "USD",
            },
        )
        quotes = payload.get("quotes") if isinstance(payload, dict) else None
        if not isinstance(quotes, dict):
            raise ValueError("Missing Alpaca quotes collection")
        missing = [symbol for symbol in symbols if symbol not in quotes]
        if missing:
            raise KeyError(missing[0])
        invalid = [symbol for symbol in symbols if not isinstance(quotes[symbol], dict)]
        if invalid:
            raise ValueError(f"Invalid Alpaca quote for {invalid[0]}")
        return quotes

    def validate_feed_access(self, symbols: list[str]) -> None:
        """Validate feed access without requiring executable prices.

        Alpaca legitimately returns zero bid/ask values when a symbol has no
        current quote (for example outside the US session). Startup must still
        be able to validate credentials, entitlement and symbol completeness at
        those times. The execution paths continue to use quote_snapshot,
        which rejects non-positive prices.
        """
        quotes = self._latest_quotes(symbols)
        for symbol in symbols:
            quote = quotes[symbol]
            number(quote.get("bp"))
            number(quote.get("ap"))
            timestamp(quote.get("t"))
            bid = float(quote["bp"])
            ask = float(quote["ap"])
            if bid > 0 and ask > 0 and ask < bid:
                raise ValueError(f"Crossed Alpaca quote for {symbol}")

    def get_market_snapshots(self, symbols: list[str]) -> dict[str, MarketSnapshot]:
        quotes = self._latest_quotes(symbols)
        now = datetime.now(UTC)
        snapshots: dict[str, MarketSnapshot] = {}
        for symbol in symbols:
            quote = quotes[symbol]
            # A zero side means Alpaca currently has no executable quote, which
            # is expected outside the stock session. It is absence of fallback
            # evidence, not a transport/runtime error.
            bid = number(quote.get("bp"))
            ask = number(quote.get("ap"))
            timestamp(quote.get("t"))
            if bid == 0 or ask == 0:
                continue
            snapshots[symbol] = quote_snapshot(
                symbol,
                quote,
                received_at=now,
            )
        return snapshots


class AlpacaMarketDataFeed(LiveMarketDataFeed):
    def __init__(
        self,
        *,
        api_key: str,
        secret_key: str,
        feed: str = "iex",
        trade_stream=None,
        queue_capacity=4096,
        connector=None,
        global_silence_seconds=15.0,
    ) -> None:
        self._queue: queue.Queue[MarketDataEvent] = queue.Queue(maxsize=queue_capacity)
        self._last_timestamp: dict[str, datetime] = {}
        self._trade_stream = trade_stream
        self._stream = AlpacaStream(
            api_key=api_key,
            secret_key=secret_key,
            feed=feed,
            on_message=self._on_quote,
            connector=connector,
            silence_seconds=global_silence_seconds,
            diagnostics_context=self._queue_diagnostics,
        )
        self.invalid_quotes = self.ordering_drops = self.queue_overflows = 0
        self.queue_high_watermark = self.discarded_quotes = 0
        self.queue_backpressure_events = 0
        self.queue_backpressure_wait_seconds = 0.0

    def _queue_diagnostics(self) -> dict:
        return {
            "queue_capacity": self._queue.maxsize,
            "queue_size": self._queue.qsize(),
            "queue_high_watermark": self.queue_high_watermark,
            "queue_overflows": self.queue_overflows,
            "queue_backpressure_events": self.queue_backpressure_events,
            "queue_backpressure_wait_seconds": round(
                self.queue_backpressure_wait_seconds,
                6,
            ),
            "discarded_quotes": self.discarded_quotes,
        }

    @property
    def requires_websocket_health(self) -> bool:
        return True

    def start(self, symbols: list[str]) -> None:
        if self._trade_stream:
            self._trade_stream.start()
        self._stream.start(symbols)

    def update_symbols(self, symbols: list[str]) -> None:
        self._stream.update_symbols(symbols)

    def set_data_expected(self, expected: bool) -> None:
        self._stream.set_data_expected(expected)

    def executable_data_expected(self) -> bool:
        return self._stream.data_expected()

    def subscribed_symbols(self) -> tuple[str, ...]:
        return self._stream.subscribed_symbols()

    def next_event(self, timeout_seconds: float) -> MarketDataEvent | None:
        self._stream.check_error()
        if self._trade_stream:
            self._trade_stream.check_error()
        try:
            event = self._queue.get(timeout=timeout_seconds)
        except queue.Empty:
            return None
        if event.connection_id != str(self._stream.connections):
            # An old queued quote must not regain entry authority merely
            # because a replacement socket has authenticated successfully.
            self.ordering_drops += 1
            self.discarded_quotes += 1
            return None
        return event

    def stop(self) -> None:
        self._stream.stop()
        if self._trade_stream:
            self._trade_stream.stop()

    def connection_healthy(self) -> bool:
        return self._stream.healthy()

    def diagnostics(self) -> dict:
        return {
            **self._stream.diagnostics(),
            "mode": "alpaca_websocket",
            "invalid_quotes": self.invalid_quotes,
            "ordering_drops": self.ordering_drops,
            **self._queue_diagnostics(),
            "trade_updates": self._trade_stream.diagnostics() if self._trade_stream else None,
        }

    def _on_quote(self, message: dict) -> bool:
        symbol = message.get("S")
        if symbol not in self._stream.subscribed_symbols():
            return False
        now = datetime.now(UTC)
        try:
            snapshot = quote_snapshot(symbol, message, received_at=now)
        except (ValueError, TypeError):
            self.invalid_quotes += 1
            return False
        previous = self._last_timestamp.get(symbol)
        if previous is not None and snapshot.timestamp <= previous:
            self.ordering_drops += 1
            return False
        self._last_timestamp[symbol] = snapshot.timestamp
        event = MarketDataEvent(
            symbol,
            MarketDataSource.WEBSOCKET,
            now,
            snapshot,
            connection_id=str(self._stream.connections),
        )
        try:
            self._queue.put_nowait(event)
            self.queue_high_watermark = max(
                self.queue_high_watermark,
                self._queue.qsize(),
            )
            return True
        except queue.Full:
            # Local overload must never erase an executable crossing. Apply
            # backpressure to the WebSocket consumer instead of purging FIFO
            # history. The queue remains memory-bounded and preserves causal
            # order; shutdown can still interrupt the wait promptly.
            self.queue_backpressure_events += 1
            self.queue_high_watermark = self._queue.maxsize
            started = time.monotonic()
            while not self._stream.stopping():
                try:
                    self._queue.put(event, timeout=0.1)
                    self.queue_backpressure_wait_seconds += max(
                        0.0,
                        time.monotonic() - started,
                    )
                    return True
                except queue.Full:
                    continue
            self.queue_backpressure_wait_seconds += max(
                0.0,
                time.monotonic() - started,
            )
            self.discarded_quotes += 1
            return False
