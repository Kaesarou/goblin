"""Lossless regression tests for the two broker-confirmed INTC fills on 2026-09-15."""

from datetime import datetime, timedelta, timezone

import pytest

from app.v3.book import InventoryBook
from app.v3.persistence import InventoryEvent


NOW = datetime(2026, 9, 15, 13, 40, tzinfo=timezone.utc)
FIRST_ID = "INTC:55fcaea1e52b4ecb6f8935e4"
SECOND_ID = "INTC:97c14413004a4203fb61ea12"
FIRST_POSITION = "3599774868"
SECOND_POSITION = "3599774883"


def _event(event_id, inventory_id, event_type, ts, payload):
    return InventoryEvent(
        event_id=event_id,
        inventory_id=inventory_id,
        event_type=event_type,
        occurred_at=ts,
        payload=payload,
        strategy_version="INVENTORY_RR5_ETORO5_V1",
    )


def _historical_intc_fills():
    return [
        _event(
            "intc-open-1", FIRST_ID, "ENTRY_FILLED",
            NOW + timedelta(seconds=4, microseconds=542386),
            {"symbol": "INTC", "position_id": FIRST_POSITION,
             "units": 3.292115, "price": 99.27, "fee": 0.0,
             "notional": 3.292115 * 99.27},
        ),
        _event(
            "intc-open-2", SECOND_ID, "ENTRY_FILLED",
            NOW + timedelta(seconds=7, microseconds=172395),
            {"symbol": "INTC", "position_id": SECOND_POSITION,
             "units": 3.291612, "price": 99.27, "fee": 0.0,
             "notional": 3.291612 * 99.27},
        ),
    ]


def test_restart_preserves_both_real_broker_legs_without_ledger_rewrite():
    original = _historical_intc_fills()
    before = [(e.event_id, e.inventory_id, dict(e.payload)) for e in original]
    book = InventoryBook.from_events(original)
    inv = book.active_for_symbol("INTC")
    assert inv is not None
    assert inv.inventory_id == FIRST_ID
    assert book.legacy_inventory_aliases == {SECOND_ID: FIRST_ID}
    assert len([x for x in book.inventories if x.symbol == "INTC" and x.total_units > 0]) == 1
    assert {leg.position_id for leg in inv.broker_legs} == {FIRST_POSITION, SECOND_POSITION}
    assert inv.entry_fill_count == 2
    assert inv.total_units == pytest.approx(6.583727)
    assert inv.total_notional == pytest.approx(6.583727 * 99.27)
    assert set(book.active_broker_position_ids()) == {FIRST_POSITION, SECOND_POSITION}
    assert book.portfolio(equity=100_000).active_inventory_count == 1
    assert [(e.event_id, e.inventory_id, dict(e.payload)) for e in original] == before
    rebuilt = InventoryBook.from_events(original)
    assert rebuilt.legacy_inventory_aliases == book.legacy_inventory_aliases
    assert rebuilt.active_for_symbol("INTC") == inv


def test_legacy_second_inventory_economics_can_be_replayed():
    events = _historical_intc_fills()
    events.append(_event(
        "intc-economic-close-2", SECOND_ID, "EXIT_ECONOMICS_CONFIRMED",
        NOW + timedelta(minutes=1),
        {"position_id": SECOND_POSITION, "price": 101.0,
         "units": 3.291612, "entry_price_basis": 99.27, "fee": 0.21},
    ))
    book = InventoryBook.from_events(events)
    inv = book.active_for_symbol("INTC")
    assert inv is not None
    assert inv.realized_pnl == pytest.approx(3.291612 * (101.0 - 99.27))
    assert inv.fees_paid == pytest.approx(0.21)
    assert set(book.active_broker_position_ids()) == {FIRST_POSITION, SECOND_POSITION}


def test_legacy_partial_and_full_close_preserve_remaining_leg_and_economics():
    events = _historical_intc_fills()
    events.append(_event(
        "intc-partial-2", SECOND_ID, "EXIT_FILLED",
        NOW + timedelta(minutes=1),
        {"position_id": SECOND_POSITION, "price": 103.0,
         "units": 1.291612, "fee": 0.10},
    ))
    events.append(_event(
        "intc-full-1", FIRST_ID, "EXIT_FILLED",
        NOW + timedelta(minutes=2),
        {"position_id": FIRST_POSITION, "price": 102.0,
         "units": 3.292115, "fee": 0.20},
    ))
    book = InventoryBook.from_events(events)
    inv = book.active_for_symbol("INTC")
    assert inv is not None
    assert inv.total_units == pytest.approx(2.0)
    assert len(inv.broker_legs) == 1
    assert inv.broker_legs[0].position_id == SECOND_POSITION
    assert inv.realized_pnl == pytest.approx(
        1.291612 * (103.0 - 99.27) + 3.292115 * (102.0 - 99.27)
    )
    assert inv.fees_paid == pytest.approx(0.30)
    assert book.active_broker_position_ids() == (SECOND_POSITION,)
    assert InventoryBook.from_events(events).active_for_symbol("INTC") == inv


def test_new_live_duplicate_inventory_remains_forbidden():
    book = InventoryBook()
    book.apply_entry_fill(
        inventory_id=FIRST_ID, symbol="INTC", position_id=FIRST_POSITION,
        units=3.292115, price=99.27, fee=0.0, filled_at=NOW,
    )
    with pytest.raises(ValueError, match="Multiple active inventories for INTC"):
        book.apply_entry_fill(
            inventory_id=SECOND_ID, symbol="INTC", position_id=SECOND_POSITION,
            units=3.291612, price=99.27, fee=0.0,
            filled_at=NOW + timedelta(seconds=3),
        )


def test_two_separate_intc_roundtrips_do_not_get_aliased():
    first = _historical_intc_fills()[0]
    full_close = _event(
        "intc-first-close", FIRST_ID, "EXIT_FILLED", NOW + timedelta(minutes=1),
        {"position_id": FIRST_POSITION, "price": 100.0,
         "units": 3.292115, "fee": 0.0},
    )
    second = _historical_intc_fills()[1]
    book = InventoryBook.from_events([first, full_close, second])
    assert book.legacy_inventory_aliases == {}
    assert len(book.inventories) == 2
    assert book.active_for_symbol("INTC").inventory_id == SECOND_ID
    assert book.active_broker_position_ids() == (SECOND_POSITION,)
