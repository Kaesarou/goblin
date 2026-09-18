"""Regression reproducer for the 2026-09-15 INTC double-open incident.

These tests describe the required *non-destructive* recovery behavior. They are
marked xfail while the fix is under development: removing xfail is a merge gate.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.v3.book import InventoryBook
from app.v3.persistence import InventoryEvent


NOW = datetime(2026, 9, 15, 13, 40, tzinfo=timezone.utc)


def _entry(event_id, inventory_id, position_id, ts, units):
    return InventoryEvent(
        event_id=event_id,
        inventory_id=inventory_id,
        event_type="ENTRY_FILLED",
        occurred_at=ts,
        payload={
            "symbol": "INTC",
            "position_id": position_id,
            "units": units,
            "price": 99.27,
            "fee": 0.0,
            "notional": units * 99.27,
        },
        strategy_version="INVENTORY_RR5_ETORO5_V1",
    )


def _historical_intc_fills():
    return [
        _entry(
            "intc-open-1", "INTC:55fcaea1e52b4ecb6f8935e4",
            "3599774868", NOW + timedelta(seconds=4, microseconds=542386),
            3.292115,
        ),
        _entry(
            "intc-open-2", "INTC:97c14413004a4203fb61ea12",
            "3599774883", NOW + timedelta(seconds=7, microseconds=172395),
            3.291612,
        ),
    ]


@pytest.mark.xfail(strict=True, reason="INTC double-open replay currently crashes at startup")
def test_restart_preserves_both_real_broker_legs_without_ledger_rewrite():
    original = _historical_intc_fills()
    book = InventoryBook.from_events(original)
    inv = book.active_for_symbol("INTC")
    assert inv is not None
    assert len([x for x in book.inventories if x.symbol == "INTC" and x.total_units > 0]) == 1
    assert {leg.position_id for leg in inv.broker_legs} == {"3599774868", "3599774883"}
    assert inv.total_units == pytest.approx(6.583727)
    assert inv.total_notional == pytest.approx(6.583727 * 99.27)
    # A second reconstruction of the *same* append-only source must be deterministic.
    rebuilt = InventoryBook.from_events(original)
    assert rebuilt.active_for_symbol("INTC").total_units == pytest.approx(inv.total_units)


@pytest.mark.xfail(strict=True, reason="INTC duplicate inventory alias economics not yet handled")
def test_legacy_second_inventory_economics_can_be_replayed():
    events = _historical_intc_fills()
    events.append(InventoryEvent(
        event_id="intc-economic-close-2", inventory_id=events[1].inventory_id,
        event_type="EXIT_ECONOMICS_CONFIRMED",
        occurred_at=NOW + timedelta(minutes=1),
        payload={
            "position_id": "3599774883", "price": 101.0,
            "units": 3.291612, "entry_price_basis": 99.27, "fee": 0.0,
        },
        strategy_version="INVENTORY_RR5_ETORO5_V1",
    ))
    # Real historical inventory IDs remain in the ledger for audit; projection
    # must map economic events onto the original broker leg, not lose them.
    book = InventoryBook.from_events(events)
    inv = book.active_for_symbol("INTC")
    assert inv is not None
    assert inv.realized_pnl == pytest.approx(3.291612 * (101.0 - 99.27))
