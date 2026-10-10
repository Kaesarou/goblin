from datetime import datetime, timedelta, timezone

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



def test_resting_intent_retirement_proves_best_non_crossing_quote():
    book = RestingIntentBook()
    sell = intent("sell-proof", "SELL", 101.0)
    change = book.replace_symbol("AAPL", (sell,))
    assert change.added == (sell,)
    assert change.removed == ()

    book.triggered(snapshot(bid=100.40, ask=100.50))
    book.triggered(snapshot(bid=100.95, ask=101.05))
    replacement = intent("sell-next", "SELL", 101.20)
    change = book.replace_symbol("AAPL", (replacement,))

    assert len(change.removed) == 1
    evidence = change.removed[0]
    assert evidence.intent == sell
    assert evidence.quote_count == 2
    assert evidence.max_bid == 100.95
    assert evidence.min_ask == 100.50
    assert evidence.first_crossed_at is None
    assert evidence.first_crossing_price is None
    assert evidence.closest_distance_bp < 0


def test_first_crossing_is_reported_once_and_retained_until_replacement():
    book = RestingIntentBook()
    sell = intent("sell-cross", "SELL", 101.0)
    book.replace_symbol("AAPL", (sell,))

    first = MarketSnapshot(
        "AAPL",
        bid=101.02,
        ask=101.03,
        last=101.025,
        timestamp=NOW,
    )
    later = MarketSnapshot(
        "AAPL",
        bid=101.10,
        ask=101.11,
        last=101.105,
        timestamp=NOW + timedelta(seconds=1),
    )
    assert book.triggered(first) == (sell,)
    crossing = book.take_first_crossing("sell-cross")
    assert crossing is not None
    assert crossing.first_crossed_at == NOW
    assert crossing.first_crossing_price == 101.02
    assert crossing.closest_distance_bp > 0
    assert book.take_first_crossing("sell-cross") is None

    assert book.triggered(later) == (sell,)
    retired = book.replace_symbol("AAPL", ()).removed[0]
    assert retired.quote_count == 2
    assert retired.max_bid == 101.10
    assert retired.first_crossed_at == NOW
    assert retired.first_crossing_price == 101.02
