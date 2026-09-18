"""The local intent book must not dispatch two BUYs before the first resolves."""

from datetime import datetime, timezone

import pytest

from app.market.models import MarketSnapshot
from app.v3.intents import RestingIntentBook
from app.v3.models import ExecutionStyle, IntentPurpose, OrderIntent


NOW = datetime(2026, 9, 15, 13, 40, tzinfo=timezone.utc)


def _intent(intent_id, *, side="BUY", limit=100.0, symbol="INTC"):
    return OrderIntent(
        intent_id=intent_id,
        purpose=IntentPurpose.REENTRY if side == "BUY" else IntentPurpose.PROFIT_EXIT,
        symbol=symbol,
        side=side,
        notional=100.0,
        created_at=NOW,
        execution_style=ExecutionStyle.PASSIVE_LIMIT,
        limit_price=limit,
        inventory_id=f"{symbol}:1",
        reduce_only=side == "SELL",
    )


def _touch(symbol="INTC"):
    return MarketSnapshot(symbol, bid=100.0, ask=100.0, last=100.0, timestamp=NOW)


def test_one_buy_per_symbol_even_when_multiple_intents_cross_on_same_quote():
    book = RestingIntentBook()
    first, second = _intent("first"), _intent("second")
    book.replace_symbol("INTC", (first, second))
    assert book.triggered(_touch()) == (first,)
    book.mark_dispatched(first.intent_id)
    # The next candle replaces an intent, not an in-flight broker operation.
    book.replace_symbol("INTC", (second,))
    assert book.triggered(_touch()) == ()
    book.cancel_symbol("INTC")
    book.replace_symbol("INTC", (second,))
    assert book.triggered(_touch()) == ()
    book.resolve(first.intent_id)
    assert book.triggered(_touch()) == (second,)


def test_an_unresolved_buy_does_not_block_reduce_only_sell_or_other_symbol():
    book = RestingIntentBook()
    buy = _intent("buy")
    sell = _intent("sell", side="SELL")
    book.replace_symbol("INTC", (buy,))
    book.mark_dispatched(buy.intent_id)
    book.replace_symbol("INTC", (sell, _intent("another_buy")))
    assert book.triggered(_touch()) == (sell,)
    other = _intent("other", symbol="AMD")
    book.replace_symbol("AMD", (other,))
    assert book.triggered(_touch("AMD")) == (other,)


def test_a_different_buy_cannot_be_marked_dispatched_while_symbol_reserved():
    book = RestingIntentBook()
    buy1, buy2 = _intent("buy1"), _intent("buy2")
    book.replace_symbol("INTC", (buy1, buy2))
    book.mark_dispatched("buy1")
    with pytest.raises(RuntimeError, match="Concurrent BUY dispatch"):
        book.mark_dispatched("buy2")
    book.resolve("buy1")
    book.mark_dispatched("buy2")
