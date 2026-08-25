from datetime import datetime, timedelta, timezone

from app.v3.book import InventoryBook
from app.v3.persistence import InventoryEvent

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


def test_inventory_book_aggregates_broker_legs_and_recomputes_after_close():
    book = InventoryBook()
    first = book.apply_entry_fill(
        inventory_id="AAPL:1", symbol="AAPL", position_id="p1",
        units=1.0, price=100.0, fee=1.0, filled_at=NOW,
    )
    second = book.apply_entry_fill(
        inventory_id="AAPL:1", symbol="AAPL", position_id="p2",
        units=2.0, price=90.0, fee=1.0, filled_at=NOW + timedelta(minutes=1),
    )
    assert second.entry_fill_count == 2
    assert abs(second.average_entry_price - (280 / 3)) < 1e-12
    remaining = book.apply_exit_fill(
        position_id="p1", exit_price=105.0, fee=1.0,
        filled_at=NOW + timedelta(hours=1),
    )
    assert remaining.total_units == 2.0
    assert remaining.average_entry_price == 90.0
    assert remaining.realized_pnl == 5.0
    assert remaining.broker_legs[0].position_id == "p2"


def test_inventory_book_rebuilds_from_fill_events():
    events = [
        InventoryEvent(
            "e1", "AAPL:1", "ENTRY_FILLED", NOW,
            {"symbol": "AAPL", "position_id": "p1", "units": 1, "price": 100, "fee": 0},
            "RR5",
        ),
        InventoryEvent(
            "e2", "AAPL:1", "ENTRY_FILLED", NOW + timedelta(minutes=1),
            {"symbol": "AAPL", "position_id": "p2", "units": 1, "price": 90, "fee": 0},
            "RR5",
        ),
    ]
    book = InventoryBook.from_events(events)
    inventory = book.active_for_symbol("AAPL")
    assert inventory is not None
    assert inventory.entry_fill_count == 2
    assert set(book.active_broker_position_ids()) == {"p1", "p2"}


def test_observe_candle_updates_ordered_trailing_bundle():
    book = InventoryBook()
    book.apply_entry_fill(
        inventory_id="AAPL:1", symbol="AAPL", position_id="p1",
        units=1.0, price=100.0, fee=0.0, filled_at=NOW,
    )
    book.observe_candle(symbol="AAPL", high=101, low=99, close=100)
    first = book.active_for_symbol("AAPL")
    assert first is not None
    assert first.trailing_min_since_open == 99
    assert first.trailing_max_since_open == 101
    book.observe_candle(symbol="AAPL", high=100.5, low=98, close=99)
    second = book.active_for_symbol("AAPL")
    assert second is not None
    assert second.trailing_min_since_open == 98
    assert second.trailing_max_since_min == 99
