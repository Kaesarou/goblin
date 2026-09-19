"""A fresh SQLite cannot establish that the eToro DEMO account is empty."""

from datetime import datetime, timezone

import pytest

from app.brokers.cached_broker import CachedBrokerClient
from app.brokers.etoro.resilient_client import ResilientEtoroClient
from app.config.settings import Settings
from app.market.models import MarketSnapshot
from app.v3.book import InventoryBook
from app.v3.external_account_gate import gate_path
from app.v3.live_execution import V3BrokerExecutor
from app.v3.models import ExecutionStyle, IntentPurpose, OrderIntent
from app.v3.persistence import InventoryEvent, InventoryEventStore

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def _executor(tmp_path, monkeypatch, payload, *, pnl=None):
    client = ResilientEtoroClient(settings=Settings.model_construct(
        broker="etoro_demo", base_currency="USD",
        etoro_api_key="api", etoro_user_key="user",
    ))
    monkeypatch.setattr(client, "get_portfolio", lambda: payload)
    if pnl is None:
        pnl = {"ordersForOpen": [], "orders": []}
    def get_pnl(path):
        assert path == "/api/v1/trading/info/demo/pnl"
        return pnl
    monkeypatch.setattr(client, "_get", get_pnl)
    store = InventoryEventStore(tmp_path / "fresh.sqlite")
    executor = V3BrokerExecutor(
        broker=CachedBrokerClient(client), task_runner=object(),
        event_store=store, book=InventoryBook.from_events(store.events()),
        strategy_version="INVENTORY_RR5_ETORO5_V1", model_version=None,
    )
    return executor


def _snapshot():
    return MarketSnapshot(symbol="INTC", bid=100.0, ask=100.1,
                          last=100.0, timestamp=NOW, received_at=NOW)


def _intent(side):
    return OrderIntent(
        intent_id=f"manual-close-{side}", purpose=(
            IntentPurpose.INITIAL_ENTRY if side == "BUY"
            else IntentPurpose.RISK_REDUCTION
        ), symbol="INTC", side=side, notional=300.0,
        created_at=NOW, execution_style=ExecutionStyle.MARKET,
        reduce_only=(side == "SELL"),
    )


def test_empty_account_allows_a_fresh_ledger_to_start(tmp_path, monkeypatch):
    executor = _executor(tmp_path, monkeypatch, {"clientPortfolio": {"positions": []}})
    assert executor.verify_known_broker_legs() == ()
    assert executor.new_risk_allowed
    assert not executor.confirmation_metrics()["account_observation_only"]


def test_pending_manual_close_starts_observing_without_buy_or_duplicate_close(tmp_path, monkeypatch):
    # A market-closed manual close is not an open order. Until execution the
    # position is still in the portfolio, despite the fresh SQLite being empty.
    portfolio = {"clientPortfolio": {"positions": [
        {"positionID": "3599774868", "units": 3.292115, "isOpen": True},
    ]}}
    executor = _executor(tmp_path, monkeypatch, portfolio)
    assert executor.event_store.events() == []
    assert executor.verify_known_broker_legs() == ()  # startup may continue
    assert executor.halted_reason == "external_broker_activity_observation_only"
    assert executor.confirmation_metrics()["account_observation_only"] is True
    assert not executor.new_risk_allowed
    assert "3599774868" in str(executor._last_broker_reconciliation_issues)
    assert executor.schedule(_intent("BUY"), snapshot=_snapshot()) is False
    assert executor.schedule(_intent("SELL"), snapshot=_snapshot()) is False
    assert executor.event_store.events() == []  # no phantom fills / close starts
    marker = gate_path(tmp_path / "fresh.sqlite")
    assert marker.is_file()

    # Even if the next portfolio snapshot becomes flat, there is no API proof
    # that every manually queued close has settled. No automatic BUY re-arm.
    flat = _executor(tmp_path, monkeypatch, {"clientPortfolio": {"positions": []}})
    assert flat.verify_known_broker_legs() == ()
    assert not flat.new_risk_allowed
    assert flat.halted_reason == "external_broker_activity_ack_required"
    assert flat.confirmation_metrics()["account_observation_only"]
    assert flat.schedule(_intent("BUY"), snapshot=_snapshot()) is False

    # An explicit operator action is required AFTER checking the broker; tests
    # model acknowledgment by deleting ONLY this gate, never the SQLite/logs.
    marker.unlink()
    armed = _executor(tmp_path, monkeypatch, {"clientPortfolio": {"positions": []}})
    assert armed.verify_known_broker_legs() == ()
    assert armed.new_risk_allowed


def test_pending_open_from_old_account_starts_readonly_not_as_new_risk(tmp_path, monkeypatch):
    executor = _executor(tmp_path, monkeypatch,
                         {"clientPortfolio": {"positions": []}}, pnl={
                             "ordersForOpen": [{"orderId": "pending-2808", "amount": 327.0}],
                             "orders": [],
                         })
    assert executor.verify_known_broker_legs() == ()
    assert not executor.new_risk_allowed
    assert executor.halted_reason == "external_broker_activity_observation_only"
    assert "pending_broker_open:ordersForOpen:pending-2808" in (
        executor._last_broker_reconciliation_issues
    )
    assert gate_path(tmp_path / "fresh.sqlite").is_file()


def test_untracked_position_in_nonempty_ledger_still_rejects_startup(tmp_path, monkeypatch):
    store = InventoryEventStore(tmp_path / "fresh.sqlite")
    store.append(InventoryEvent(
        event_id="historic-failed", inventory_id="AMD:historic",
        event_type="ORDER_SUBMISSION_FAILED", occurred_at=NOW,
        payload={"action_id": "historic", "symbol": "AMD"},
        strategy_version="INVENTORY_RR5_ETORO5_V1",
    ))
    executor = _executor(tmp_path, monkeypatch, {"clientPortfolio": {"positions": [
        {"positionID": "external", "units": 2.0, "isOpen": True},
    ]}})
    assert executor.verify_known_broker_legs() == ("untracked_broker_position:external:units=2",)
    assert not executor.new_risk_allowed
    assert not executor.confirmation_metrics()["account_observation_only"]
    assert not gate_path(tmp_path / "fresh.sqlite").exists()


def test_corrupt_recovery_gate_never_silently_rearms_buy(tmp_path, monkeypatch):
    gate_path(tmp_path / "fresh.sqlite").write_text('{"version": "unknown"}', encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid external broker recovery gate"):
        _executor(tmp_path, monkeypatch, {"clientPortfolio": {"positions": []}})


def test_unparseable_portfolio_is_not_mistaken_for_empty(tmp_path, monkeypatch):
    executor = _executor(tmp_path, monkeypatch, {"other": "not a portfolio"})
    with pytest.raises(ValueError, match="Missing authoritative broker positions collection"):
        executor.verify_known_broker_legs()
    assert executor.halted_reason == "broker_account_preflight_unavailable"
    assert not executor.new_risk_allowed


def test_closed_historical_broker_row_is_not_open_exposure(tmp_path, monkeypatch):
    executor = _executor(tmp_path, monkeypatch, {"clientPortfolio": {"positions": [
        {"positionID": "old-closed", "isOpen": False},
    ]}})
    assert executor.verify_known_broker_legs() == ()
    assert executor.new_risk_allowed


@pytest.mark.parametrize("field", ("ordersForOpen", "orders"))
def test_pending_open_is_not_interpreted_as_a_flat_account(tmp_path, monkeypatch, field):
    pnl = {"ordersForOpen": [], "orders": []}
    pnl[field] = [{"orderId": "pending-2808", "amount": 327.0}]
    executor = _executor(tmp_path, monkeypatch,
                         {"clientPortfolio": {"positions": []}}, pnl=pnl)
    assert executor.verify_known_broker_legs() == ()
    assert not executor.new_risk_allowed
    assert executor.confirmation_metrics()["account_observation_only"]
    assert executor.schedule(_intent("BUY"), snapshot=_snapshot()) is False


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
