from datetime import datetime, timezone

from app.market.models import MarketSnapshot
from app.v3.intents import RestingIntentBook
from app.v3.models import ExecutionStyle, IntentPurpose, OrderIntent

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


def intent(intent_id, side, limit):
    return OrderIntent(
        intent_id=intent_id,
        purpose=(IntentPurpose.REENTRY if side == "BUY" else IntentPurpose.PROFIT_EXIT),
        symbol="AAPL",
        side=side,
        notional=100,
        created_at=NOW,
        execution_style=ExecutionStyle.PASSIVE_LIMIT,
        limit_price=limit,
        inventory_id="AAPL:1",
        reduce_only=side == "SELL",
    )


def snapshot(*, bid, ask):
    return MarketSnapshot("AAPL", bid=bid, ask=ask, last=(bid + ask) / 2, timestamp=NOW)


def test_resting_buy_triggers_on_executable_ask_not_last_price():
    book = RestingIntentBook()
    buy = intent("buy1", "BUY", 100.0)
    book.replace_symbol("AAPL", (buy,))
    assert book.triggered(snapshot(bid=99.9, ask=100.1)) == ()
    assert book.triggered(snapshot(bid=99.8, ask=99.95)) == (buy,)


def test_resting_sell_triggers_on_executable_bid():
    book = RestingIntentBook()
    sell = intent("sell1", "SELL", 101.0)
    book.replace_symbol("AAPL", (sell,))
    assert book.triggered(snapshot(bid=100.9, ask=101.1)) == ()
    assert book.triggered(snapshot(bid=101.01, ask=101.1)) == (sell,)


def test_dispatched_intent_cannot_double_submit_and_next_candle_replaces_it():
    book = RestingIntentBook()
    buy = intent("buy1", "BUY", 100.0)
    book.replace_symbol("AAPL", (buy,))
    touch = snapshot(bid=99.8, ask=99.9)
    assert book.triggered(touch) == (buy,)
    book.mark_dispatched("buy1")
    assert book.triggered(touch) == ()
    replacement = intent("buy2", "BUY", 99.5)
    book.replace_symbol("AAPL", (replacement,))
    assert book.snapshot() == (replacement,)
