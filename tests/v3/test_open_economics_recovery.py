"""S2 regressions: causal fill quarantine, exact proof, restart and risk gates."""

from dataclasses import replace
from datetime import timedelta

import pytest

from app.brokers.base import BrokerPositionEconomics, OpenPositionResult
from app.brokers.cached_broker import CachedBrokerClient
from app.market.models import MarketSnapshot
from app.v3.book import InventoryBook
from app.v3.live_execution import V3BrokerExecutor
from app.v3.models import ExecutionStyle, IntentPurpose, OrderIntent
from app.v3.persistence import InventoryEvent
from tests.v3.test_open_fill_units import NOW, ImmediateRunner


@pytest.fixture(autouse=True)
def causal_clock(monkeypatch):
    monkeypatch.setattr("app.v3.live_execution._utc_now", lambda: NOW)


class Broker:
    open_price_sanity_required = True
    requires_external_activity_ack = False

    def __init__(self, price, notional):
        self.result = OpenPositionResult("p1", price, 3.0, notional,
                                         broker_response={"orderId": "lookup-order", "raw": price,
                                             "positionExecutions": [{"positionId": "p1",
                                                 "openingData": {"orderId": "broker-open"}}]})
        self.evidence = {}
        self.closed_units = {"p1": 0.0}
        self.opens = 0
        self.reads = 0

    def open_position(self, *_args):
        self.opens += 1
        return self.result

    def prepare_open_order_id(self, _action_id):
        return None

    def remember_position_instrument(self, *_args):
        pass

    def get_open_position_economics(self, _ids):
        self.reads += 1
        if isinstance(self.evidence, Exception):
            raise self.evidence
        return self.evidence

    def get_open_position_units(self, _ids):
        return self.closed_units


def setup_executor(tmp_path, price=53.0, ask=56.02, notional=300.0, *, cached=False):
    from app.v3.persistence import InventoryEventStore
    broker = Broker(price, notional)
    store = InventoryEventStore(tmp_path / "economics.sqlite")
    executor = V3BrokerExecutor(broker=CachedBrokerClient(broker) if cached else broker,
                                task_runner=ImmediateRunner(), event_store=store,
                                book=InventoryBook(), strategy_version="ETORO5", model_version=None)
    intent = OrderIntent("open-1", IntentPurpose.INITIAL_ENTRY, "IFX.DE", "BUY", 300.0,
                         NOW, ExecutionStyle.MARKET)
    quote = MarketSnapshot("IFX.DE", ask - .01, ask, ask, NOW)
    assert executor.schedule(intent, snapshot=quote)
    assert executor.drain() == (intent.intent_id,)
    return executor, broker, store, intent, quote


def evidence(price=56.02, amount=300.0, units=3.0, position_id="p1", order_id="broker-open"):
    return BrokerPositionEconomics(position_id, units, price, amount,
                                   "etoro_exact_pnl_position_usd", {"orderId": order_id})


def restart(executor, broker, store):
    return V3BrokerExecutor(broker=broker, task_runner=ImmediateRunner(), event_store=store,
                            book=InventoryBook.from_events(store.events()),
                            strategy_version="ETORO5", model_version=None)


@pytest.mark.parametrize("price,ask", [(53.0,56.02),(10.9,11.3),(128.0,131.0),(63.0,67.0),(110.0,100.0)])
def test_s2_prices_are_quarantined_and_position_survives_restart(tmp_path, price, ask):
    executor, broker, store, intent, quote = setup_executor(tmp_path, price, ask)
    fill = next(e for e in store.events() if e.event_type == "ENTRY_FILLED")
    assert fill.payload["economics_status"] == "ECONOMICS_UNRESOLVED"
    assert fill.payload["broker_raw_price"] == price
    assert fill.payload["broker_response"]["raw"] == price
    assert fill.payload["causal_quote"]["ask"] == ask
    assert fill.payload["price_source"] == "causal_quote_provisional"
    inv = executor.book.active_for_symbol("IFX.DE")
    assert not inv.economics_resolved
    assert inv.total_units == 3.0 and inv.total_notional == 300.0
    executor.book.observe_candle(symbol="IFX.DE", high=ask+2, low=ask-1, close=ask)
    assert executor.book.active_for_symbol("IFX.DE").trailing_max_since_open is None
    assert not executor.new_risk_allowed
    assert not executor.schedule(replace(intent, intent_id="new"), snapshot=quote)
    close = replace(intent, intent_id="profit", purpose=IntentPurpose.PROFIT_EXIT, side="SELL",
                    inventory_id=inv.inventory_id, reduce_only=True)
    assert not executor.schedule(close, snapshot=quote)
    restored = restart(executor, broker, store)
    assert not restored.new_risk_allowed
    assert not restored.book.active_for_symbol("IFX.DE").economics_resolved
    assert broker.opens == 1


def test_exact_pnl_price_and_notional_resolve_both_durable_halts(tmp_path):
    executor, broker, store, _, _ = setup_executor(tmp_path, notional=1.0)
    broker.evidence = {"p1": evidence()}
    assert executor._schedule_open_economics_revalidation(NOW)
    executor.drain()
    assert executor.new_risk_allowed
    inv = executor.book.active_for_symbol("IFX.DE")
    assert inv.economics_resolved
    assert inv.average_entry_price == 56.02
    assert inv.entry_fill_count == 1 and inv.total_notional == 300.0
    assert inv.trailing_max_since_open is None
    rebuilt = restart(executor, broker, store)
    assert rebuilt.new_risk_allowed
    assert rebuilt.book.active_for_symbol("IFX.DE").average_entry_price == 56.02
    assert {"OPEN_ACCOUNT_NOTIONAL_RECONCILED", "OPEN_FILL_ECONOMICS_RECONCILED"} <= {
        e.event_type for e in store.events()}
    assert broker.opens == 1


def test_credible_etoro_fill_keeps_broker_price_and_needs_no_extra_get(tmp_path):
    executor, broker, store, _, _ = setup_executor(tmp_path, price=56.03)
    assert executor.new_risk_allowed
    assert executor.book.active_for_symbol("IFX.DE").average_entry_price == 56.03
    assert not executor._schedule_open_economics_revalidation(NOW)
    assert broker.reads == 0


def test_production_cache_wrapper_keeps_quarantine_and_uncached_recovery_authority(tmp_path):
    executor, broker, store, _, _ = setup_executor(tmp_path, cached=True, notional=1.0)
    assert not executor.book.active_for_symbol("IFX.DE").economics_resolved
    assert not executor.new_risk_allowed
    broker.evidence = {"p1": evidence(price=53.0)}
    executor._schedule_open_economics_revalidation(NOW)
    executor.drain()
    assert not executor.new_risk_allowed
    # A later authoritative answer must not be hidden by a recovery TTL cache.
    broker.evidence = {"p1": evidence()}
    executor._schedule_open_economics_revalidation(NOW + timedelta(seconds=61))
    executor.drain()
    assert executor.new_risk_allowed and broker.reads == 2
    restored = restart(executor, executor.broker, store)
    assert restored.new_risk_allowed


def test_unresolved_close_does_not_invent_zero_resolved_pnl(tmp_path):
    executor, _, store, _, _ = setup_executor(tmp_path)
    fill = next(e for e in store.events() if e.event_type == "ENTRY_FILLED")
    event = InventoryEvent("emergency-close", fill.inventory_id, "EXIT_FILLED", NOW,
                           {"position_id": "p1", "units": 3.0, "price": 57.0,
                            "entry_economics_resolved": False}, "ETORO5")
    store.append(event)
    inventory = InventoryBook.from_events(store.events()).inventories[0]
    assert inventory.realized_pnl == 0.0
    assert inventory.unresolved_exit_units == 3.0  # zero is incomplete, not resolved P&L


@pytest.mark.parametrize("proof", [
    {}, {"p1": evidence(price=53.0)}, {"p1": evidence(amount=1.0)},
    {"p1": evidence(units=2.0)}, {"p1": evidence(position_id="other")},
    {"p1": evidence(order_id="other")}, {"p1": evidence(price=float("nan"))},
    RuntimeError("HTTP 429"),
])
def test_invalid_absent_stale_or_ambiguous_evidence_never_rearms(tmp_path, proof):
    executor, broker, store, _, _ = setup_executor(tmp_path, notional=1.0)
    broker.evidence = proof
    executor._schedule_open_economics_revalidation(NOW)
    executor.drain()
    assert not executor.new_risk_allowed
    assert not restart(executor, broker, store).new_risk_allowed
    assert not any(e.event_type.endswith("RECONCILED") for e in store.events())


def test_retry_deadline_survives_restart_and_does_not_repeat_open(tmp_path):
    executor, broker, store, _, _ = setup_executor(tmp_path)
    executor._schedule_open_economics_revalidation(NOW)
    executor.drain()
    restored = restart(executor, broker, store)
    assert not restored._schedule_open_economics_revalidation(NOW + timedelta(seconds=59))
    assert restored._schedule_open_economics_revalidation(NOW + timedelta(seconds=60))
    restored.drain()
    assert not restored._schedule_open_economics_revalidation(NOW + timedelta(seconds=179))
    assert broker.reads == 2 and broker.opens == 1


def test_resolution_preserves_independent_halt(tmp_path):
    executor, broker, store, _, _ = setup_executor(tmp_path)
    executor.halted_reason = "untracked_broker_positions"
    broker.evidence = {"p1": evidence()}
    executor._schedule_open_economics_revalidation(NOW)
    executor.drain()
    assert executor.book.active_for_symbol("IFX.DE").economics_resolved
    assert executor.halted_reason == "untracked_broker_positions"
    assert not executor.new_risk_allowed


def test_partial_close_scales_notional_proof_and_preserves_fill_count(tmp_path):
    executor, broker, store, _, _ = setup_executor(tmp_path, price=56.02, notional=1.0)
    executor.book.apply_exit_fill(position_id="p1", units=2.52, exit_price=57.0,
                                  fee=0, filled_at=NOW)
    broker.evidence = {"p1": evidence(units=.48, amount=48.0)}
    executor._schedule_open_economics_revalidation(NOW)
    executor.drain()
    inv = executor.book.active_for_symbol("IFX.DE")
    assert executor.new_risk_allowed
    assert inv.total_notional == 48.0 and inv.entry_fill_count == 1


def test_stale_query_after_quantity_change_cannot_resolve(tmp_path):
    executor, broker, store, _, _ = setup_executor(tmp_path)
    broker.evidence = {"p1": evidence()}
    executor._schedule_open_economics_revalidation(NOW)
    executor.book.reconcile_broker_leg_units(position_id="p1", broker_units=2.0, observed_at=NOW)
    executor.drain()
    assert not executor.new_risk_allowed


def test_closed_notional_risk_requires_exact_flat_portfolio_and_confirmed_ledger(tmp_path):
    executor, broker, store, _, _ = setup_executor(tmp_path, price=56.02, notional=1.0)
    executor.book.apply_exit_fill(position_id="p1", exit_price=57, fee=0, filled_at=NOW)
    assert executor._schedule_open_economics_revalidation(NOW)
    executor.drain()
    assert not executor.new_risk_allowed  # absence alone is not enough
    store.append(InventoryEvent("exit-1", "IFX.DE:open-1", "EXIT_FILLED", NOW,
                                {"position_id":"p1", "units":3.0, "price":57.0}, "ETORO5"))
    broker.closed_units = {"p1": 1.0}
    executor._schedule_open_economics_revalidation(NOW + timedelta(seconds=61))
    executor.drain()
    assert not executor.new_risk_allowed
    broker.closed_units = {"p1": 0.0}
    executor._schedule_open_economics_revalidation(NOW + timedelta(seconds=182))
    executor.drain()
    assert executor.new_risk_allowed
    resolution = next(e for e in store.events() if e.event_type == "OPEN_ACCOUNT_NOTIONAL_RECONCILED")
    assert resolution.payload["original_account_notional_resolved"] is False
    assert restart(executor, broker, store).new_risk_allowed
