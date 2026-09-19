"""A fresh SQLite cannot establish that the eToro DEMO account is empty."""

import pytest

from app.brokers.cached_broker import CachedBrokerClient
from app.brokers.etoro.resilient_client import ResilientEtoroClient
from app.config.settings import Settings
from app.v3.book import InventoryBook
from app.v3.live_execution import V3BrokerExecutor
from app.v3.persistence import InventoryEventStore


def _executor(tmp_path, monkeypatch, payload, *, pnl=None):
    client = ResilientEtoroClient(settings=Settings.model_construct(
        broker="etoro_demo", base_currency="USD",
        etoro_api_key="api", etoro_user_key="user",
    ))
    monkeypatch.setattr(client, "get_portfolio", lambda: payload)
    # P&L exposes pending market and MIT orders that the position-only endpoint
    # cannot see. Never contact the real broker from a synthetic test.
    if pnl is None:
        pnl = {"ordersForOpen": [], "orders": []}
    def get_pnl(path):
        assert path == "/api/v1/trading/info/demo/pnl"
        return pnl
    monkeypatch.setattr(client, "_get", get_pnl)
    executor = V3BrokerExecutor(
        broker=CachedBrokerClient(client), task_runner=object(),
        event_store=InventoryEventStore(tmp_path / "fresh.sqlite"),
        book=InventoryBook(),
        strategy_version="INVENTORY_RR5_ETORO5_V1", model_version=None,
    )
    return executor


def test_empty_account_allows_a_fresh_ledger_to_start(tmp_path, monkeypatch):
    executor = _executor(tmp_path, monkeypatch, {"clientPortfolio": {"positions": []}})
    assert executor.verify_known_broker_legs() == ()
    assert executor.new_risk_allowed


def test_untracked_intc_blocks_startup_even_with_zero_sqlite_events(tmp_path, monkeypatch):
    executor = _executor(tmp_path, monkeypatch, {
        "clientPortfolio": {"positions": [
            {"positionID": "3599774868", "units": 3.292115, "isOpen": True},
        ]},
    })
    assert executor.event_store.events() == []
    issues = executor.verify_known_broker_legs()
    assert any("3599774868" in issue for issue in issues)
    assert executor.halted_reason == "untracked_broker_positions"
    assert not executor.new_risk_allowed


def test_unparseable_portfolio_is_not_mistaken_for_empty(tmp_path, monkeypatch):
    executor = _executor(tmp_path, monkeypatch, {"other": "not a portfolio"})
    with pytest.raises(ValueError, match="Missing authoritative broker positions collection"):
        executor.verify_known_broker_legs()
    assert executor.halted_reason == "broker_account_preflight_unavailable"
    assert not executor.new_risk_allowed


def test_closed_historical_broker_row_is_not_open_exposure(tmp_path, monkeypatch):
    executor = _executor(tmp_path, monkeypatch, {
        "clientPortfolio": {"positions": [
            {"positionID": "old-closed", "isOpen": False},
        ]},
    })
    assert executor.verify_known_broker_legs() == ()
    assert executor.new_risk_allowed


@pytest.mark.parametrize("field", ("ordersForOpen", "orders"))
def test_pending_order_blocks_fresh_ledger_even_without_positions(tmp_path, monkeypatch, field):
    pnl = {"ordersForOpen": [], "orders": []}
    pnl[field] = [{"orderId": "pending-2808", "amount": 327.0}]
    executor = _executor(tmp_path, monkeypatch,
                         {"clientPortfolio": {"positions": []}}, pnl=pnl)
    issues = executor.verify_known_broker_legs()
    assert issues == (f"pending_broker_open:{field}:pending-2808",)
    assert executor.halted_reason == "pending_broker_open_orders"
    assert not executor.new_risk_allowed


@pytest.mark.parametrize("pnl", (
    {"credit": 100_000},
    {"ordersForOpen": []},
    {"ordersForOpen": [], "orders": None},
    {"ordersForOpen": [None], "orders": []},
))
def test_incomplete_pending_order_data_fails_closed(tmp_path, monkeypatch, pnl):
    executor = _executor(tmp_path, monkeypatch,
                         {"clientPortfolio": {"positions": []}}, pnl=pnl)
    with pytest.raises(ValueError, match="pending-order|P&L"):
        executor.verify_known_broker_legs()
    assert executor.halted_reason == "broker_account_preflight_unavailable"
    assert not executor.new_risk_allowed
