from datetime import datetime, timezone

from app.v3.execution import WholeLegCloseAllocator
from app.v3.models import BrokerLeg, InventoryState

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


def test_whole_leg_allocator_selects_closest_deterministic_subset():
    legs = (
        BrokerLeg("p1", 1.0, 100, NOW),
        BrokerLeg("p2", 2.0, 95, NOW),
        BrokerLeg("p3", 4.0, 90, NOW),
    )
    inv = InventoryState(
        "AAPL:1", "AAPL", NOW, 7.0, 93.0, 3, NOW, 90.0, 651.0, 0.00651,
        broker_legs=legs,
    )
    plan = WholeLegCloseAllocator().plan(inv, 5.0)
    assert plan.position_ids == ("p1", "p3")
    assert plan.planned_units == 5.0
    assert plan.absolute_error_units == 0.0
