"""BUY recovery through real Alpaca/V3 journals, with mocked broker I/O only."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import requests

from app.brokers.cached_broker import CachedBrokerClient
from app.v3.book import InventoryBook
from app.v3.models import CostEstimate
from app.v3.persistence import InventoryEventStore
from app.v3.recovery import evaluate_restart_safety
from app.v3.state_store import CloseRetryState
from tests.brokers.alpaca.test_execution import broker
from tests.v3.test_executor_pending_open_reservation import DeferredRunner
from tests.v3.test_live_execution import _open_intent
from tests.v3.test_partial_close_execution import _close_intent, _executor, _snapshot
from tests.v3.test_runtime_equity_authority import runtime_for_test

NOW = datetime(2026, 9, 28, 14, tzinfo=UTC)


def make_executor(tmp_path, api=None):
    client, api = broker(tmp_path, api)
    events = InventoryEventStore(tmp_path / "v3.sqlite").events()
    executor = _executor(tmp_path, CachedBrokerClient(CachedBrokerClient(client)),
                         InventoryBook.from_events(events))
    executor.restore_pending_close_confirmations(events)
    return executor, api, client


def submit_buy(executor, action="buy", **overrides):
    intent = replace(_open_intent(), **{"intent_id": action, "notional": 300, **overrides})
    assert executor.schedule(intent, snapshot=_snapshot())
    event = executor.event_store.events()[-1]
    assert event.event_type == "ORDER_SUBMISSION_STARTED"
    return event.payload["client_order_id"]


def confirm_buy(executor):
    pending = next(iter(executor._pending_open_confirmations.values()))
    assert executor.schedule_close_confirmation_checks(
        monotonic_now=pending.next_attempt_monotonic + 1,
    ) == 1
    return executor.drain()


@pytest.mark.parametrize("crash_before_completion", [False, True])
@pytest.mark.parametrize("lost_response", [False, True])
def test_uncertain_buy_recovers_by_identity_without_another_post(
    tmp_path, crash_before_completion, lost_response,
):
    executor, api, _ = make_executor(tmp_path)
    api.next_status, api.timeout_after_post = "new", lost_response
    order_id = submit_buy(executor, cost_estimate=CostEstimate(spread_cost=0.1, commission=0.2))
    assert api.submissions[0]["client_order_id"] == order_id
    if not crash_before_completion:
        assert executor.drain() == ()
    executor, _, _ = make_executor(tmp_path, api)
    assert evaluate_restart_safety(executor.event_store.events()).safe
    assert executor.verify_known_broker_legs() == ()
    assert not executor.new_risk_allowed
    assert confirm_buy(executor) == ()
    assert not executor.schedule(replace(_open_intent(), intent_id="duplicate"), snapshot=_snapshot())
    api.fill(order_id, 3)
    assert confirm_buy(executor) == ("buy",)
    assert executor.new_risk_allowed
    assert not executor.runtime_state_store.load_open_retries()
    executor, _, _ = make_executor(tmp_path, api)
    assert executor.verify_known_broker_legs() == ()
    assert executor.new_risk_allowed
    assert not executor._pending_open_confirmations
    inventory = executor.book.active_for_symbol("AAPL")
    assert inventory.entry_fill_count == 1
    assert inventory.total_units == 3
    assert inventory.total_notional == 300
    fills = [event for event in executor.event_store.events() if event.event_type == "ENTRY_FILLED"]
    assert len(fills) == 1
    assert fills[0].payload["estimated_cost"] == pytest.approx(0.3)
    assert len(api.submissions) == 1


@pytest.mark.parametrize("status", ["canceled", "expired", "rejected"])
@pytest.mark.parametrize("recover", [False, True])
def test_partial_buy_books_actual_exposure_only_on_terminal_confirmation(tmp_path, status, recover):
    executor, api, _ = make_executor(tmp_path)
    api.next_status, api.next_qty = ("partially_filled" if recover else status), Decimal("0.5")
    order_id = submit_buy(executor)
    executor.drain()
    if recover:
        for qty in ("0.5", "1.2"):
            api.fill(order_id, qty, "partially_filled")
            executor, _, _ = make_executor(tmp_path, api)
            assert executor.verify_known_broker_legs() == ()
            assert confirm_buy(executor) == ()
            assert executor.book.active_for_symbol("AAPL") is None
            assert not executor.new_risk_allowed
        api.fill(order_id, "1.2", status)
        assert confirm_buy(executor) == ("buy",)
    expected = 1.2 if recover else 0.5
    executor, _, _ = make_executor(tmp_path, api)
    assert executor.verify_known_broker_legs() == ()
    assert executor.new_risk_allowed
    inventory = executor.book.active_for_symbol("AAPL")
    assert inventory.total_units == expected
    assert inventory.total_notional == 100 * expected
    assert inventory.entry_fill_count == 1
    assert not executor._notional_anomaly_action_ids
    assert len(api.submissions) == 1


@pytest.mark.parametrize("status", ["canceled", "expired", "rejected"])
def test_terminal_unfilled_buy_releases_reservation_and_allows_a_new_action(tmp_path, status):
    executor, api, _ = make_executor(tmp_path)
    api.next_status = "new"
    order_id = submit_buy(executor)
    executor.drain()
    api.fill(order_id, 0, status)
    assert confirm_buy(executor) == ()
    assert executor.new_risk_allowed
    assert executor.book.active_for_symbol("AAPL") is None
    executor, _, _ = make_executor(tmp_path, api)
    assert executor.verify_known_broker_legs() == ()
    assert executor.new_risk_allowed
    assert evaluate_restart_safety(executor.event_store.events()).safe
    api.next_status = "filled"
    assert not executor.schedule(replace(_open_intent(), intent_id="buy"), snapshot=_snapshot())
    submit_buy(executor, "another")
    assert executor.drain() == ("another",)
    assert len(api.submissions) == 2


@pytest.mark.parametrize("reserved", [False, True])
def test_missing_buy_evidence_after_crash_never_reposts_or_releases(tmp_path, reserved, monkeypatch):
    executor, api, _ = make_executor(tmp_path)
    if reserved:
        original = api.request

        def lost_post(method, path, **kwargs):
            if method == "POST":
                api.submissions.append(kwargs["json"])
                raise requests.Timeout("No response")
            return original(method, path, **kwargs)

        monkeypatch.setattr(api, "request", lost_post)
    else:
        executor.task_runner = DeferredRunner()
    order_id = submit_buy(executor)
    for _ in range(2):
        executor, _, _ = make_executor(tmp_path, api)
        assert executor.verify_known_broker_legs() == ()
        assert confirm_buy(executor) == ()
        assert not executor.new_risk_allowed
        assert executor.book.active_for_symbol("AAPL") is None
        assert next(iter(executor._pending_open_confirmations.values())).context.client_order_id == order_id
    assert len(api.submissions) == int(reserved)


def test_crash_after_broker_fill_before_completion_restores_only_the_owned_buy(tmp_path):
    executor, api, _ = make_executor(tmp_path)
    order_id = submit_buy(executor)
    executor, _, _ = make_executor(tmp_path, api)
    assert executor.verify_known_broker_legs() == ()
    assert confirm_buy(executor) == ("buy",)
    assert executor.book.active_for_symbol("AAPL").broker_legs[0].position_id == order_id
    assert len(api.submissions) == 1


def test_crash_after_entry_event_replays_without_applying_or_buying_twice(tmp_path, monkeypatch):
    executor, api, _ = make_executor(tmp_path)
    submit_buy(executor)

    def crash(**kwargs):
        raise RuntimeError("Crash after durable entry event")

    monkeypatch.setattr(executor.book, "apply_entry_fill", crash)
    with pytest.raises(RuntimeError, match="durable entry"):
        executor.drain()
    executor, _, _ = make_executor(tmp_path, api)
    assert executor.verify_known_broker_legs() == ()
    assert executor.new_risk_allowed
    assert not executor._pending_open_confirmations
    assert executor.book.active_for_symbol("AAPL").total_units == 3
    assert len(api.submissions) == 1


def test_late_duplicate_open_completion_does_not_apply_a_fill_twice(tmp_path):
    executor, api, _ = make_executor(tmp_path)
    submit_buy(executor)
    completion = executor.task_runner.items[0]
    assert executor.drain() == ("buy",)
    assert executor._handle_open_completion(completion) == []
    assert executor.book.active_for_symbol("AAPL").entry_fill_count == 1
    assert len(api.submissions) == 1


def test_order_owned_by_another_request_or_external_position_still_blocks_startup(tmp_path):
    executor, api, _ = make_executor(tmp_path)
    api.next_status, api.next_qty = "partially_filled", Decimal("0.5")
    submit_buy(executor)
    executor.drain()
    api.external = {"MSFT": Decimal(2)}
    executor, _, _ = make_executor(tmp_path, api)
    assert any("MSFT" in issue for issue in executor.verify_known_broker_legs())
    assert not executor.new_risk_allowed


def test_partial_buy_can_be_reduced_by_the_unchanged_close_allocator(tmp_path):
    executor, api, _ = make_executor(tmp_path)
    api.next_status, api.next_qty = "canceled", Decimal("1.2")
    submit_buy(executor)
    executor.drain()
    inventory = executor.book.active_for_symbol("AAPL")
    api.next_status, api.next_qty = "filled", None
    assert executor.schedule(_close_intent(inventory, 0.84), snapshot=_snapshot())
    executor.drain()
    pending = next(iter(executor._pending_close_confirmations.values()))
    assert executor.schedule_close_confirmation_checks(monotonic_now=pending.next_attempt_monotonic + 1)
    executor.drain()
    assert executor.book.active_for_symbol("AAPL").total_units == pytest.approx(0.192)
    assert len(api.submissions) == 2


@pytest.mark.parametrize("failure,minimum_delay", [
    ("timeout", 15), ("429", 60), ("503", 15),
])
def test_lookup_backoff_survives_restart_and_does_not_share_close_retry_state(
    tmp_path, monkeypatch, failure, minimum_delay,
):
    clock = {"utc": NOW, "mono": 100.0}
    monkeypatch.setattr("app.v3.live_execution._utc_now", lambda: clock["utc"])
    monkeypatch.setattr("app.v3.live_execution.time.monotonic", lambda: clock["mono"])
    executor, api, _ = make_executor(tmp_path)
    api.next_status = "new"
    submit_buy(executor)
    executor.drain()
    original = api.request
    lookups = []

    def unavailable(method, path, **kwargs):
        if path == "/v2/orders:by_client_order_id":
            lookups.append(path)
            if failure == "timeout":
                raise requests.Timeout("lookup timeout")
            response = requests.Response()
            response.status_code = int(failure)
            raise requests.HTTPError(response=response)
        return original(method, path, **kwargs)

    monkeypatch.setattr(api, "request", unavailable)
    assert confirm_buy(executor) == ()
    saved = executor.runtime_state_store.load_open_retries()["buy"]
    assert saved.attempt_count == 1
    assert (saved.next_attempt_at - NOW).total_seconds() >= minimum_delay
    executor.runtime_state_store.save_close_retry(CloseRetryState("buy", 9, NOW + timedelta(hours=1)))
    assert executor.runtime_state_store.load_close_retries()["buy"].attempt_count == 9
    assert executor.runtime_state_store.load_open_retries()["buy"] == saved
    clock.update(utc=NOW + timedelta(seconds=5), mono=500.0)
    executor, _, _ = make_executor(tmp_path, api)
    pending = executor._pending_open_confirmations["buy"]
    remaining = (saved.next_attempt_at - clock["utc"]).total_seconds()
    assert pending.next_attempt_monotonic == 500 + remaining
    assert executor.schedule_close_confirmation_checks(monotonic_now=500, utc_now=clock["utc"]) == 0
    assert len(lookups) == 1
    assert len(api.submissions) == 1
    assert executor.confirmation_metrics()["open_confirmations"][0]["last_result_state"] == "error"


def test_retry_deadline_is_persisted_before_dispatching_lookup(tmp_path, monkeypatch):
    monkeypatch.setattr("app.v3.live_execution._utc_now", lambda: NOW)
    monkeypatch.setattr("app.v3.live_execution.time.monotonic", lambda: 100.0)
    executor, api, _ = make_executor(tmp_path)
    api.next_status = "new"
    submit_buy(executor)
    executor.drain()
    executor.task_runner = DeferredRunner()
    assert executor.schedule_close_confirmation_checks(monotonic_now=111, utc_now=NOW) == 1
    saved = executor.runtime_state_store.load_open_retries()["buy"]
    assert saved.last_result_state == "lookup_in_flight"
    assert saved.attempt_count == 1
    assert executor.schedule_close_confirmation_checks(monotonic_now=1000, utc_now=NOW) == 0
    assert len(executor.task_runner.queued) == 1
    executor, _, _ = make_executor(tmp_path, api)
    assert executor._pending_open_confirmations["buy"].next_attempt_monotonic == 115
    assert len(api.submissions) == 1


@pytest.mark.parametrize("field,value", [
    ("order_id", "other-order"), ("position_id", "other-leg"), ("symbol", "MSFT"),
    ("requested_notional", 10.0), ("requested_notional", True),
    ("status", "partially_filled"),
])
def test_invalid_order_evidence_cannot_book_or_release_the_buy(tmp_path, field, value):
    executor, api, _ = make_executor(tmp_path)
    submit_buy(executor)
    completion = executor.task_runner.items[0]
    result = replace(completion.value, order=replace(completion.value.order, **{field: value}))
    executor.task_runner.items[0] = replace(completion, value=result)
    assert executor.drain() == ()
    assert executor.book.active_for_symbol("AAPL") is None
    assert not executor.new_risk_allowed
    assert confirm_buy(executor) == ("buy",)
    assert executor.new_risk_allowed
    assert len(api.submissions) == 1


@pytest.mark.parametrize("patch", [
    {"executed_units": True}, {"executed_units": float("nan")},
    {"executed_entry_price": float("inf")}, {"executed_notional": 1},
    {"executed_notional": None}, {"order": None},
])
def test_inconsistent_execution_economics_never_replace_exact_order_evidence(tmp_path, patch):
    executor, api, _ = make_executor(tmp_path)
    submit_buy(executor)
    completion = executor.task_runner.items[0]
    executor.task_runner.items[0] = replace(completion, value=replace(completion.value, **patch))
    assert executor.drain() == ()
    assert not executor.new_risk_allowed
    assert executor.book.active_for_symbol("AAPL") is None
    assert len(api.submissions) == 1


@pytest.mark.parametrize("status,qty", [("filled", "0.01"), ("canceled", "3.5")])
def test_partial_fill_exception_does_not_disable_notional_anomaly_guards(tmp_path, status, qty):
    executor, api, _ = make_executor(tmp_path)
    api.next_status, api.next_qty = status, Decimal(qty)
    submit_buy(executor)
    assert executor.drain() == ("buy",)
    assert not executor.new_risk_allowed
    assert executor.book.active_for_symbol("AAPL").total_notional == max(300, float(qty) * 100)
    executor, _, _ = make_executor(tmp_path, api)
    assert executor.halted_reason == "open_account_notional_mismatch_at_restart"


def test_unknown_buy_resolution_preserves_independent_halt(tmp_path):
    executor, api, _ = make_executor(tmp_path)
    api.next_status = "new"
    order_id = submit_buy(executor)
    executor.halted_reason = "external_broker_activity_ack_required"
    executor.drain()
    api.fill(order_id, 3)
    assert confirm_buy(executor) == ("buy",)
    assert executor.halted_reason == "external_broker_activity_ack_required"


def test_reconciliation_recovery_keeps_an_independent_unknown_buy_halted(tmp_path):
    executor, api, _ = make_executor(tmp_path)
    submit_buy(executor, "initial")
    executor.drain()
    api.next_status = "new"
    order_id = submit_buy(executor, "pending", symbol="MSFT")
    executor.halted_reason = "broker_reconciliation_unavailable"
    executor.drain()
    executor._last_broker_reconciliation_monotonic = 0
    assert executor._schedule_broker_reconciliation(1000) == 1
    executor.drain()
    assert not executor.new_risk_allowed
    assert executor.halted_reason == "open_order_confirmation_unknown"
    api.fill(order_id, 3)
    assert confirm_buy(executor) == ("pending",)
    assert executor.new_risk_allowed
    assert len(api.submissions) == 2


def test_known_close_remains_reduce_only_while_a_buy_is_unresolved(tmp_path):
    executor, api, _ = make_executor(tmp_path)
    submit_buy(executor, "initial")
    executor.drain()
    previous_id = executor.book.active_for_symbol("AAPL").inventory_id
    api.next_status = "new"
    late_id = submit_buy(executor, "late")
    executor.drain()
    assert not executor.new_risk_allowed
    inventory = executor.book.active_for_symbol("AAPL")
    api.next_status = "filled"
    assert executor.schedule(_close_intent(inventory, 1.0), snapshot=_snapshot())
    executor.drain()
    pending = next(iter(executor._pending_close_confirmations.values()))
    assert executor.schedule_close_confirmation_checks(monotonic_now=pending.next_attempt_monotonic + 1)
    assert executor.drain() == ("close",)
    assert executor.book.active_for_symbol("AAPL") is None
    assert not executor.new_risk_allowed
    api.fill(late_id, 3)
    assert confirm_buy(executor) == ("late",)
    inventory = executor.book.active_for_symbol("AAPL")
    assert inventory.inventory_id != previous_id
    assert inventory.entry_fill_count == 1
    executor, _, _ = make_executor(tmp_path, api)
    assert executor.verify_known_broker_legs() == ()
    assert executor.book.active_for_symbol("AAPL").inventory_id == inventory.inventory_id
    assert executor.new_risk_allowed
    assert len(api.submissions) == 3


def test_two_unknown_buys_must_both_resolve_before_new_risk_returns(tmp_path):
    executor, api, _ = make_executor(tmp_path)
    api.timeout_after_post = True
    submit_buy(executor, "first")
    submit_buy(executor, "second", symbol="MSFT")
    executor.drain()
    assert len(executor._pending_open_confirmations) == 2
    assert confirm_buy(executor) == ("first",)
    assert not executor.new_risk_allowed
    assert confirm_buy(executor) == ("second",)
    assert executor.new_risk_allowed
    assert len(api.submissions) == 2


@pytest.mark.parametrize("with_checkpoint", [False, True])
@pytest.mark.parametrize("status,qty", [("new", 0), ("partially_filled", 0.5), ("filled", 3)])
def test_startup_checks_causal_state_before_recovering_an_unbooked_buy(
    tmp_path, monkeypatch, with_checkpoint, status, qty,
):
    executor, api, _ = make_executor(tmp_path)
    api.next_status = "new"
    order_id = submit_buy(executor)
    api.fill(order_id, qty, status)
    # Crash before V3 receives the broker completion, then restore both journals.
    executor, _, client = make_executor(tmp_path, api)
    runtime = runtime_for_test(tmp_path)
    runtime.executor = executor
    runtime.execution_broker = client
    runtime.event_store = executor.event_store
    runtime.runtime_state_store = executor.runtime_state_store
    if with_checkpoint:
        runtime.feature_engine.states["AAPL"].last_opened_at = NOW
        runtime.runtime_state_store.save_feature_engine(runtime.feature_engine)
    monkeypatch.setattr(runtime.coordinator, "initialize_symbols", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "_maybe_schedule_equity_refresh", lambda *args, **kwargs: None)
    started = []
    runtime.live_market_data.start = lambda symbols: started.append(tuple(symbols))
    try:
        runtime.startup(now=NOW)
        assert started == [tuple(runtime.monitored_symbols)]
        assert executor.book is runtime.book
        assert runtime.book.active_for_symbol("AAPL") is None
        assert not executor.new_risk_allowed
        expected_halt = "missing_causal_feature_state_for_open_inventory:AAPL"
        if not with_checkpoint:
            assert executor.halted_reason == expected_halt
        api.fill(order_id, 3)
        assert confirm_buy(executor) == ("buy",)
        assert runtime.book.active_for_symbol("AAPL").total_units == 3
        assert executor.new_risk_allowed is with_checkpoint
        if not with_checkpoint:
            assert executor.halted_reason == expected_halt
        assert len(api.submissions) == 1
    finally:
        runtime.mutation_runner.close(wait=True)
        runtime.maintenance_runner.close(wait=True)


@pytest.mark.parametrize("changes", [
    {"order_id": "another"}, {"symbol": "MSFT"}, {"requested_notional": 100},
])
def test_startup_does_not_adopt_buy_evidence_for_another_request(tmp_path, monkeypatch, changes):
    executor, api, _ = make_executor(tmp_path)
    api.next_status, api.next_qty = "partially_filled", Decimal("0.5")
    order_id = submit_buy(executor)
    executor, _, client = make_executor(tmp_path, api)
    preflight = client.get_account_preflight()
    evidence = replace(preflight.open_orders[order_id], **changes)
    monkeypatch.setattr(client, "get_account_preflight", lambda: replace(
        preflight, open_orders={order_id: evidence},
    ))
    issues = executor.verify_known_broker_legs()
    assert any(issue.startswith("untracked_broker_position:") for issue in issues)
    assert any(issue.startswith("pending_broker_open:") for issue in issues)
    assert not executor.new_risk_allowed
    assert len(api.submissions) == 1
