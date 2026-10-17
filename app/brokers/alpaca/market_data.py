from __future__ import annotations

import queue
from collections import Counter
from datetime import UTC, datetime

from app.brokers.alpaca.schema import number, timestamp
from app.brokers.alpaca.stream import AlpacaStream, QuoteQueueOverflow
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
    # Operational freshness bounds, not trading-strategy parameters.
    max_entry_quote_age_seconds = 5.0
    max_queue_delivery_age_seconds = 5.0
    # Drain several validated events before repeating expensive V3 clock,
    # inventory and maintenance bookkeeping. Each event remains in FIFO order
    # for exact crossing and candle semantics.
    runtime_drain_max_events = 128
    runtime_drain_max_seconds = 0.025
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
        self.ordering_equal_timestamps = 0
        self.ordering_older_timestamps = 0
        self.ordering_old_epoch = 0
        self.ordering_by_symbol: Counter[str] = Counter()
        self.queue_high_watermark = self.discarded_quotes = 0
        self.queue_backpressure_events = 0
        self.queue_backpressure_wait_seconds = 0.0
        self.stale_queue_events = 0
        self.max_queue_delivery_age_observed_seconds = 0.0
        self.epoch_queue_purges = 0
        self.epoch_queue_discarded = 0

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
            "stale_queue_events": self.stale_queue_events,
            "epoch_queue_purges": self.epoch_queue_purges,
            "epoch_queue_discarded": self.epoch_queue_discarded,
            "max_queue_delivery_age_observed_seconds": round(
                self.max_queue_delivery_age_observed_seconds, 6,
            ),
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

    def _purge_invalid_epoch(self) -> None:
        """Drop missing-continuity ticks; never replay them as later crossings.

        Queue operations remain bounded by its configured capacity, and the
        socket must already have had new-entry authority revoked by caller.
        """
        count = 0
        while True:
            try:
                self._queue.get_nowait()
                count += 1
            except queue.Empty:
                break
        self.epoch_queue_purges += 1
        self.epoch_queue_discarded += count
        self.discarded_quotes += count

    def next_event(self, timeout_seconds: float) -> MarketDataEvent | None:
        self._stream.check_error()
        if self._trade_stream:
            self._trade_stream.check_error()
        try:
            event = self._queue.get(timeout=timeout_seconds)
        except queue.Empty:
            return None
        # Epoch identity takes precedence over freshness. An obsolete quote
        # must never revoke an already established replacement connection.
        if (not self._stream.subscribed_symbols()
                or event.connection_id != str(self._stream.connections)):
            self.ordering_drops += 1
            self.ordering_old_epoch += 1
            self.discarded_quotes += 1
            return None
        age = max(0.0, (datetime.now(UTC) - event.received_at).total_seconds())
        self.max_queue_delivery_age_observed_seconds = max(
            self.max_queue_delivery_age_observed_seconds, age,
        )
        if age > self.max_queue_delivery_age_seconds:
            # This quote may have crossed an intent long ago, but cannot be
            # replayed as an executable *current* price. Revoke all entry
            # authority and start a fresh connection instead of running down
            # an arbitrarily deep old backlog.
            self.stale_queue_events += 1
            self.discarded_quotes += 1
            if self._stream.revoke_market_data(
                reconnect=True, reason="quote_queue_stale",
                expected_connection_id=event.connection_id,
            ):
                self._purge_invalid_epoch()
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
            "ordering_equal_timestamps": self.ordering_equal_timestamps,
            "ordering_older_timestamps": self.ordering_older_timestamps,
            "ordering_old_epoch": self.ordering_old_epoch,
            "ordering_by_symbol": dict(sorted(self.ordering_by_symbol.items())),
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
            self.ordering_by_symbol[symbol] += 1
            if snapshot.timestamp == previous:
                self.ordering_equal_timestamps += 1
            else:
                self.ordering_older_timestamps += 1
            return False
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
            self._last_timestamp[symbol] = snapshot.timestamp
            return True
        except queue.Full as exc:
            # Never park the reader thread on a full FIFO: doing so starves
            # WebSocket ping/pong and produces seconds-old executable quotes.
            # We cannot safely discard individual crossings and claim market
            # continuity. Invalidate this epoch, log the gap and reconnect;
            # no BUY is authorized until a fresh feed is healthy again.
            self.queue_backpressure_events += 1
            self.queue_overflows += 1
            self.queue_high_watermark = self._queue.maxsize
            self.discarded_quotes += 1
            if self._stream.revoke_market_data(
                expected_connection_id=event.connection_id,
            ):
                self._purge_invalid_epoch()
            raise QuoteQueueOverflow() from exc
