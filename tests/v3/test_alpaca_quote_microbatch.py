"""FIFO microbatching must preserve all crossings and bound maintenance delay."""
from collections import deque
from datetime import UTC, datetime, timedelta
from math import ceil

from app.market.models import MarketSnapshot
from app.v3.intents import RestingIntentBook
from app.v3.models import ExecutionStyle, IntentPurpose, OrderIntent
from app.v3.runtime import GoblinV3Runtime


class Feed:
    runtime_drain_max_events = 128
    runtime_drain_max_seconds = 0.025

    def __init__(self, events):
        self.events = deque(events)
        self.timeouts = []

    def next_event(self, timeout):
        self.timeouts.append(timeout)
        return self.events.popleft() if self.events else None


def fake_runtime(events):
    runtime = object.__new__(GoblinV3Runtime)
    runtime.live_market_data = Feed(events)
    runtime._stop_requested = False
    runtime.metrics = {
        "market_data_batch_cycles": 0,
        "market_data_batch_events": 0,
        "market_data_batch_max": 0,
        "market_data_batch_time_limited": 0,
    }
    return runtime


def test_quote_microbatches_preserve_crossing_sequence_without_starving_clock():
    now = datetime.now(UTC)
    count = 12000
    prices = [(100.0, 101.0)] * count
    prices[53] = (98.0, 99.0)  # one isolated BUY crossing
    prices[54] = (103.0, 104.0)  # one isolated SELL crossing
    market = [
        MarketSnapshot("AAPL", bid, ask, (bid + ask) / 2,
                       now + timedelta(microseconds=i),
                       received_at=now + timedelta(microseconds=i))
        for i, (bid, ask) in enumerate(prices)
    ]
    runtime = fake_runtime(market)
    intents = RestingIntentBook()
    for name, side, limit, reduce_only, purpose in (
        ("buy", "BUY", 99.0, False, IntentPurpose.INITIAL_ENTRY),
        ("sell", "SELL", 103.0, True, IntentPurpose.PROFIT_EXIT),
    ):
        intents.replace_symbol("AAPL", (
            *intents.snapshot(),
            OrderIntent(
                intent_id=name, purpose=purpose, symbol="AAPL",
                side=side, notional=100, created_at=now,
                execution_style=ExecutionStyle.LIMIT,
                limit_price=limit, reduce_only=reduce_only,
            ),
        ))
    seen = []
    crossings = []

    def consume(event, _):
        seen.append(event.timestamp)
        for intent in intents.triggered(event):
            if intent.intent_id not in crossings:
                crossings.append(intent.intent_id)
                intents.mark_dispatched(intent.intent_id)

    runtime._handle_event = consume
    while runtime.live_market_data.events:
        head = runtime.live_market_data.next_event(0)
        runtime._consume_market_data_cycle(head, now=now)

    assert seen == [e.timestamp for e in market]
    assert crossings == ["buy", "sell"]
    assert runtime.metrics["market_data_batch_events"] == count
    assert runtime.metrics["market_data_batch_cycles"] <= ceil(count / 128) + 1
    assert 1 < runtime.metrics["market_data_batch_max"] <= 128
    assert all(t == 0 for t in runtime.live_market_data.timeouts)


def test_microbatch_respects_stop_and_preserves_eToro_single_event_semantics():
    now = datetime.now(UTC)
    runtime = fake_runtime(range(500))
    handled = []

    def consume(event, _):
        handled.append(event)
        if len(handled) == 3:
            runtime._stop_requested = True

    runtime._handle_event = consume
    runtime._consume_market_data_cycle(runtime.live_market_data.next_event(0), now=now)
    assert handled == [0, 1, 2]
    assert len(runtime.live_market_data.events) == 497

    runtime = fake_runtime(range(10))
    runtime.live_market_data.runtime_drain_max_events = 1
    runtime._handle_event = lambda event, _: handled.append(event)
    runtime._consume_market_data_cycle(runtime.live_market_data.next_event(0), now=now)
    assert len(runtime.live_market_data.events) == 9
    assert runtime.metrics["market_data_batch_cycles"] == 0
