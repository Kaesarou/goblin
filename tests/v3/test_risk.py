from datetime import datetime, timezone

from app.v3.config import InventoryRiskConfig
from app.v3.models import InventoryState, PortfolioState
from app.v3.risk import InventoryRiskPolicy

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


def inventory(*, fills=1, exposure=0.02):
    return InventoryState(
        inventory_id="AAPL:1",
        symbol="AAPL",
        opened_at=NOW,
        total_units=1.0,
        average_entry_price=100.0,
        entry_fill_count=fills,
        last_entry_at=NOW,
        last_entry_price=100.0,
        total_notional=exposure * 100_000,
        wallet_exposure_pct=exposure,
    )


def test_reentry_cannot_override_max_entry_fill_cap():
    cfg = InventoryRiskConfig(max_entry_fills=5)
    inv = inventory(fills=5)
    decision = InventoryRiskPolicy(cfg).allow_reentry(
        PortfolioState(100_000, (inv,)), inv, requested_exposure_pct=0.001
    )
    assert not decision.allowed
    assert decision.reason.value == "max_entry_fills"


def test_reentry_is_cropped_to_remaining_symbol_budget():
    cfg = InventoryRiskConfig(max_symbol_exposure_pct=0.04)
    inv = inventory(exposure=0.039)
    decision = InventoryRiskPolicy(cfg).allow_reentry(
        PortfolioState(100_000, (inv,)), inv, requested_exposure_pct=0.002
    )
    assert decision.allowed
    assert abs(decision.approved_exposure_pct - 0.001) < 1e-12
    assert abs(decision.projected_symbol_exposure_pct - 0.04) < 1e-12


def test_reentry_is_rejected_when_symbol_budget_is_exhausted():
    cfg = InventoryRiskConfig(max_symbol_exposure_pct=0.04)
    inv = inventory(exposure=0.04)
    decision = InventoryRiskPolicy(cfg).allow_reentry(
        PortfolioState(100_000, (inv,)), inv, requested_exposure_pct=0.002
    )
    assert not decision.allowed
    assert decision.reason.value == "symbol_exposure_cap"


def test_closed_inventories_do_not_consume_inventory_slots():
    from app.v3.models import InventoryStatus

    cfg = InventoryRiskConfig(max_inventories=1)
    closed = inventory(exposure=0.0)
    closed = closed.__class__(**{**closed.__dict__, "status": InventoryStatus.CLOSED})
    decision = InventoryRiskPolicy(cfg).allow_initial_entry(
        PortfolioState(100_000, (closed,)), requested_exposure_pct=0.002
    )
    assert decision.allowed
