"""Exercise the real Alpaca journal through V3, including crash boundaries."""

from dataclasses import replace
from decimal import Decimal

import pytest

from app.brokers.base import BrokerPositionReconciliation
from app.brokers.cached_broker import CachedBrokerClient
from app.runtime.broker_task_runner import BrokerTaskCompletion, BrokerTaskLane
from app.v3.book import InventoryBook
from app.v3.recovery import evaluate_restart_safety
from tests.brokers.alpaca.test_execution import broker
from tests.v3.test_live_execution import _open_intent
from tests.v3.test_partial_close_execution import _close_intent, _executor, _snapshot


def opened_executor(tmp_path):
    client, api = broker(tmp_path)
    executor = _executor(tmp_path, CachedBrokerClient(CachedBrokerClient(client)), InventoryBook())
    assert executor.schedule(replace(_open_intent(), notional=300), snapshot=_snapshot())
    assert executor.drain() == ("i1",)
    return executor, api


def restart(tmp_path, api):
    client, _ = broker(tmp_path, api)
    executor = _executor(tmp_path, CachedBrokerClient(client), InventoryBook())
    events = executor.event_store.events()
    executor.book = InventoryBook.from_events(events)
    executor.restore_pending_close_confirmations(events)
    return executor


def submit_close(executor):
    inventory = executor.book.active_for_symbol("AAPL")
    assert executor.schedule(_close_intent(inventory, 0.84), snapshot=_snapshot(110))
    started = executor.event_store.events()[-1]
    assert started.event_type == "CLOSE_SUBMISSION_STARTED"
    return started.payload["client_order_id"]


def confirm(executor):
    pending = next(iter(executor._pending_close_confirmations.values()))
    assert executor.schedule_close_confirmation_checks(
        monotonic_now=pending.next_attempt_monotonic + 1,
    ) == 1
    return executor.drain()


def test_alpaca_cost_evidence_is_separate_from_unproven_ledger_fees(tmp_path):
    executor, api = opened_executor(tmp_path)
    entry = next(
        event for event in executor.event_store.events()
        if event.event_type == "ENTRY_FILLED"
    )
    assert entry.payload["broker_response"]["asset_class"] == "us_equity"
    assert entry.payload["broker_cost_evidence"]["status"] == "unavailable"
    assert entry.payload["broker_cost_evidence"]["amount"] is None
    assert entry.payload["fee"] == 0.0

    close_id = submit_close(executor)
    assert executor.drain() == ()
    # Alpaca Broker API may surface an optional commission; record its
    # provenance without assuming it is the entire economic cost of the trade.
    api.orders[close_id]["commission"] = "0.25"
    assert confirm(executor) == ("close",)
    exit_fill = next(
        event for event in executor.event_store.events()
        if event.event_type == "EXIT_FILLED"
    )
    assert exit_fill.payload["broker_cost_evidence"]["amount"] == 0.25
    assert exit_fill.payload["broker_cost_evidence"]["currency"] == "USD"
    assert exit_fill.payload["broker_cost_evidence"]["ledger_applied"] is False
    assert exit_fill.payload["fee"] == 0.0
    assert executor.book.active_for_symbol("AAPL").fees_paid == 0.0


def test_alpaca_profit_exit_dust_collapses_to_full_close(tmp_path):
    client, api = broker(tmp_path)
    executor = _executor(
        tmp_path,
        CachedBrokerClient(client),
        InventoryBook(),
    )
    assert executor.schedule(
        replace(_open_intent(), notional=50),
        snapshot=_snapshot(),
    )
    assert executor.drain() == ("i1",)
    inventory = executor.book.active_for_symbol("AAPL")
    assert inventory is not None
    assert inventory.total_units == pytest.approx(0.5)

    assert executor.schedule(
        _close_intent(inventory, 0.84, "dust-close"),
        snapshot=_snapshot(110),
    )
    assert executor.drain() == ()
    sell = api.submissions[-1]
    assert sell["side"] == "sell"
    assert sell["position_intent"] == "sell_to_close"
    assert Decimal(sell["qty"]) == Decimal("0.500000000")

    started = [
        event
        for event in executor.event_store.events()
        if event.event_type == "CLOSE_SUBMISSION_STARTED"
    ][-1]
    assert started.payload["full_close"] is True
    assert started.payload["dust_collapse"] is True

    assert confirm(executor) == ("dust-close",)
    assert executor.book.active_for_symbol("AAPL") is None
    assert executor.new_risk_allowed


@pytest.mark.parametrize("crash_before_completion", [False, True])
@pytest.mark.parametrize("lost_response", [False, True])
def test_close_identity_survives_lost_response_and_v3_completion_crash(
    tmp_path, crash_before_completion, lost_response,
):
    executor, api = opened_executor(tmp_path)
    api.next_status = "new"
    api.timeout_after_post = lost_response
    close_id = submit_close(executor)
    assert close_id == api.submissions[-1]["client_order_id"]
    if not crash_before_completion:
        assert executor.drain() == ()
    recovered = restart(tmp_path, api)
    assert evaluate_restart_safety(recovered.event_store.events()).safe
    assert recovered.verify_known_broker_legs() == ()
    pending = next(iter(recovered._pending_close_confirmations.values()))
    assert pending.close_order_id == close_id
    assert confirm(recovered) == ()
    assert len(api.submissions) == 2
    inventory = recovered.book.active_for_symbol("AAPL")
    assert not recovered.schedule(_close_intent(inventory, 0.84, "duplicate"), snapshot=_snapshot())
    api.fill(close_id, "2.52", price="110")
    assert confirm(recovered) == ("close",)
    assert recovered.new_risk_allowed
    assert recovered.book.active_for_symbol("AAPL").total_units == pytest.approx(0.48)
    assert len(api.submissions) == 2
    again = restart(tmp_path, api)
    assert again.verify_known_broker_legs() == ()
    assert again.new_risk_allowed
    assert not again._pending_close_confirmations
    assert again.book.active_for_symbol("AAPL").total_units == pytest.approx(0.48)


@pytest.mark.parametrize("status", ["rejected", "canceled", "expired"])
def test_unknown_close_is_resolved_only_by_terminal_no_fill_evidence(tmp_path, status):
    executor, api = opened_executor(tmp_path)
    api.next_status, api.timeout_after_post = "new", True
    close_id = submit_close(executor)
    executor.drain()
    assert not executor.new_risk_allowed
    assert confirm(executor) == ()
    assert not executor.new_risk_allowed
    api.fill(close_id, 0, status)
    assert confirm(executor) == ("close",)
    assert executor.new_risk_allowed
    assert executor.book.active_for_symbol("AAPL").total_units == 3
    assert evaluate_restart_safety(executor.event_store.events()).safe
    recovered = restart(tmp_path, api)
    assert recovered.new_risk_allowed
    assert not recovered._pending_close_confirmations
    assert len(api.submissions) == 2


def test_unknown_close_resolution_preserves_an_independent_halt(tmp_path):
    executor, api = opened_executor(tmp_path)
    api.next_status, api.timeout_after_post = "new", True
    close_id = submit_close(executor)
    executor.halted_reason = "unresolved_open_submission_at_restart"
    executor.drain()
    api.fill(close_id, Decimal("2.52"))
    assert confirm(executor) == ("close",)
    assert executor.halted_reason == "unresolved_open_submission_at_restart"


def test_dispatch_exception_keeps_leg_locked_and_client_identity_pollable(tmp_path):
    executor, api = opened_executor(tmp_path)
    api.next_status = "new"
    runner = executor.task_runner

    class LostDispatch:
        def submit(self, **kwargs):
            kwargs["operation"]()
            raise RuntimeError("dispatch result lost")

    executor.task_runner = LostDispatch()
    inventory = executor.book.active_for_symbol("AAPL")
    assert executor.schedule(_close_intent(inventory, 0.84), snapshot=_snapshot())
    assert not executor.schedule(_close_intent(inventory, 0.84, "second"), snapshot=_snapshot())
    assert not executor.new_risk_allowed
    executor.task_runner = runner
    close_id = api.submissions[-1]["client_order_id"]
    api.fill(close_id, "2.52")
    assert confirm(executor) == ("close",)
    assert executor.new_risk_allowed
    assert len(api.submissions) == 2


@pytest.mark.parametrize("terminal_status,terminal_qty", [
    ("canceled", "1.2"), ("expired", "1.2"), ("filled", "2.52"),
])
@pytest.mark.parametrize("lost_response", [False, True])
def test_partial_fill_reconciliation_waits_for_terminal_economics_across_restarts(
    tmp_path, terminal_status, terminal_qty, lost_response,
):
    executor, api = opened_executor(tmp_path)
    api.next_status, api.next_qty = "partially_filled", Decimal("0.5")
    api.timeout_after_post = lost_response
    close_id = submit_close(executor)
    executor.drain()
    for qty in ("0.5", "1.2"):
        api.fill(close_id, qty, "partially_filled", price="110")
        assert executor.verify_known_broker_legs() == ()
        assert confirm(executor) == ()
        executor = restart(tmp_path, api)
        assert executor.verify_known_broker_legs() == ()
        inventory = executor.book.active_for_symbol("AAPL")
        assert inventory.total_units == 3
        assert inventory.realized_pnl == 0
        assert executor._active_close_mutations_by_position
        assert not executor._reconciled_close_quantities
        assert not executor._unattributed_reconciled_position_ids
        assert not executor.schedule(_close_intent(inventory, 0.84, "duplicate"), snapshot=_snapshot())

    api.fill(close_id, terminal_qty, terminal_status, price="110")
    assert executor.verify_known_broker_legs() == ()
    assert executor.book.active_for_symbol("AAPL").total_units == 3
    assert confirm(executor) == ("close",)
    executor = restart(tmp_path, api)
    assert executor.verify_known_broker_legs() == ()
    inventory = executor.book.active_for_symbol("AAPL")
    assert inventory.total_units == pytest.approx(3 - float(terminal_qty))
    assert inventory.realized_pnl == pytest.approx(10 * float(terminal_qty))
    assert executor.new_risk_allowed
    assert not executor._pending_close_confirmations
    events = executor.event_store.events()
    assert sum(event.event_type == "EXIT_FILLED" for event in events) == 1
    assert not any(event.event_type == "BROKER_QUANTITY_RECONCILED" for event in events)
    assert len(api.submissions) == 2


def test_multiple_legs_and_sequential_closes_preserve_residual_units(tmp_path):
    executor, api = opened_executor(tmp_path)
    assert executor.schedule(
        replace(_open_intent(), intent_id="second-buy", notional=200), snapshot=_snapshot(),
    )
    executor.drain()
    api.next_status = "new"
    submit_close(executor)
    executor.drain()
    closes = [order for order in api.submissions if order["side"] == "sell"]
    assert sorted(Decimal(order["qty"]) for order in closes) == [Decimal("1.68"), Decimal("2.52")]
    for order in closes:
        api.fill(order["client_order_id"], Decimal(order["qty"]) / 2, "canceled", price="110")
    assert executor.verify_known_broker_legs() == ()
    assert len(executor._active_close_mutations_by_position) == 2
    for _ in closes:
        assert confirm(executor) == ("close",)
    executor = restart(tmp_path, api)
    assert executor.verify_known_broker_legs() == ()
    inventory = executor.book.active_for_symbol("AAPL")
    assert sorted(leg.units for leg in inventory.broker_legs) == pytest.approx([1.16, 1.74])
    assert inventory.realized_pnl == pytest.approx(21)
    api.next_status = "filled"
    assert executor.schedule(_close_intent(inventory, 0.84, "next-close"), snapshot=_snapshot())
    executor.drain()
    assert executor.verify_known_broker_legs() == ()
    for _ in closes:
        assert confirm(executor) == ("next-close",)
    assert executor.verify_known_broker_legs() == ()
    residual = executor.book.active_for_symbol("AAPL")
    assert sorted(leg.units for leg in residual.broker_legs) == pytest.approx([0.1856, 0.2784])
    assert len(api.submissions) == 6


def test_missing_order_404_survives_restart_without_releasing_or_reposting(tmp_path):
    executor, api = opened_executor(tmp_path)
    api.next_status, api.timeout_after_post = "new", True
    close_id = submit_close(executor)
    saved = api.orders.pop(close_id)
    executor.drain()
    for _ in range(2):
        executor = restart(tmp_path, api)
        assert executor.verify_known_broker_legs() == ()
        assert confirm(executor) == ()
        assert not executor.new_risk_allowed
        inventory = executor.book.active_for_symbol("AAPL")
        assert inventory.total_units == 3
        assert not executor.schedule(_close_intent(inventory, 0.84, "retry"), snapshot=_snapshot())
    api.orders[close_id] = saved
    api.fill(close_id, "2.52")
    assert executor.verify_known_broker_legs() == ()
    assert confirm(executor) == ("close",)
    assert executor.new_risk_allowed
    assert len(api.submissions) == 2


def test_crash_before_adapter_reservation_never_resubmits_started_close(tmp_path):
    executor, api = opened_executor(tmp_path)

    class UndispatchedRunner:
        def submit(self, **kwargs):
            return kwargs["task_id"]

    executor.task_runner = UndispatchedRunner()
    close_id = submit_close(executor)
    executor = restart(tmp_path, api)
    assert executor.verify_known_broker_legs() == ()
    assert confirm(executor) == ()
    assert not executor.new_risk_allowed
    pending = next(iter(executor._pending_close_confirmations.values()))
    assert pending.close_order_id == close_id
    assert pending.error_active  # No adapter ownership proof; operator reconciliation required.
    assert executor._active_close_mutations_by_position
    assert len(api.submissions) == 1


def test_untracked_pending_close_is_not_hidden_by_the_adapter_journal(tmp_path):
    executor, api = opened_executor(tmp_path)
    position_id = executor.book.active_for_symbol("AAPL").broker_legs[0].position_id
    api.next_status = "new"
    executor.broker.close_position(position_id, 1)
    executor = restart(tmp_path, api)
    issues = executor.verify_known_broker_legs()
    assert len(issues) == 1 and issues[0].startswith("untracked_broker_close:")
    assert executor.halted_reason == "untracked_broker_close_orders"


@pytest.mark.parametrize("evidence", [0.1, -1, float("nan"), float("inf"), True, "0.5", 3])
def test_inconsistent_cumulative_evidence_does_not_authorize_a_quantity_change(
    tmp_path, monkeypatch, evidence,
):
    executor, api = opened_executor(tmp_path)
    api.next_status, api.next_qty = "partially_filled", Decimal("0.5")
    close_id = submit_close(executor)
    executor.drain()
    position_id = executor.book.active_for_symbol("AAPL").broker_legs[0].position_id
    monkeypatch.setattr(executor.broker, "get_position_reconciliation", lambda *args, **kwargs:
                        BrokerPositionReconciliation({position_id: 2.5}, {close_id: evidence}))
    assert executor.verify_known_broker_legs() == (f"{position_id}:close_fill_quantity_mismatch",)
    assert not executor.new_risk_allowed
    assert executor.book.active_for_symbol("AAPL").total_units == 3
    assert not executor._reconciled_close_quantities


def test_external_reduction_cannot_be_hidden_as_a_partial_close(tmp_path):
    executor, api = opened_executor(tmp_path)
    api.next_status, api.next_qty = "partially_filled", Decimal("0.5")
    submit_close(executor)
    executor.drain()
    api.external = {"AAPL": Decimal("-0.2")}
    executor._last_broker_reconciliation_monotonic = 0
    assert executor._schedule_broker_reconciliation(100) == 1
    executor.drain()
    assert executor.halted_reason == "broker_reconciliation_unavailable"
    assert executor.book.active_for_symbol("AAPL").total_units == 3
    assert executor._active_close_mutations_by_position


def test_late_partial_snapshot_cannot_reapply_a_terminal_fill(tmp_path):
    executor, api = opened_executor(tmp_path)
    api.next_status, api.next_qty = "partially_filled", Decimal("0.5")
    close_id = submit_close(executor)
    executor.drain()
    context = executor._broker_reconciliation_context()
    observation = executor._read_broker_reconciliation(context)
    api.fill(close_id, "2.52", price="110")
    assert confirm(executor) == ("close",)
    executor._handle_broker_reconciliation(BrokerTaskCompletion(
        task_id="late", kind="v3_broker_reconciliation", lane=BrokerTaskLane.QUERY,
        context=context, value=observation,
    ))
    assert executor._last_broker_reconciliation_status == "stale"
    inventory = executor.book.active_for_symbol("AAPL")
    assert inventory.total_units == pytest.approx(0.48)
    assert inventory.realized_pnl == pytest.approx(25.2)


def test_pre_submit_rejection_cannot_reuse_an_action_with_a_new_unjournaled_identity(tmp_path):
    executor, api = opened_executor(tmp_path)
    api.open = False
    submit_close(executor)
    executor.drain()
    assert executor.event_store.events()[-1].event_type == "CLOSE_SUBMISSION_FAILED"
    assert executor.new_risk_allowed
    assert not executor._active_close_mutations_by_position
    api.open = True
    inventory = executor.book.active_for_symbol("AAPL")
    assert not executor.schedule(_close_intent(inventory, 0.84), snapshot=_snapshot())
    assert len(api.submissions) == 1
    assert executor.schedule(_close_intent(inventory, 0.84, "new-action"), snapshot=_snapshot())
    executor.drain()
    executor = restart(tmp_path, api)
    assert executor.verify_known_broker_legs() == ()
    assert confirm(executor) == ("new-action",)
    assert len(api.submissions) == 2


def test_late_submission_completion_cannot_reopen_a_resolved_close(tmp_path):
    executor, api = opened_executor(tmp_path)
    submit_close(executor)
    completion = executor.task_runner.items[0]
    executor.drain()
    assert confirm(executor) == ("close",)
    event_count = len(executor.event_store.events())
    assert executor._handle_close_submission(completion) == []
    assert not executor._pending_close_confirmations
    assert len(executor.event_store.events()) == event_count
    assert len(api.submissions) == 2


def test_cumulative_fill_must_match_even_an_unchanged_position(tmp_path, monkeypatch):
    executor, api = opened_executor(tmp_path)
    api.next_status = "new"
    close_id = submit_close(executor)
    executor.drain()
    position_id = executor.book.active_for_symbol("AAPL").broker_legs[0].position_id
    monkeypatch.setattr(executor.broker, "get_position_reconciliation", lambda *args, **kwargs:
                        BrokerPositionReconciliation({position_id: 3.0}, {close_id: 0.5}))
    assert executor.verify_known_broker_legs() == (f"{position_id}:close_fill_quantity_mismatch",)
    assert not executor.new_risk_allowed


def test_order_fill_evidence_cannot_be_attributed_to_a_different_leg(tmp_path):
    executor, api = opened_executor(tmp_path)
    api.next_status = "new"
    close_id = submit_close(executor)
    executor.drain()
    position_id = executor.book.active_for_symbol("AAPL").broker_legs[0].position_id
    with pytest.raises(ValueError, match="does not own"):
        executor.broker.get_position_reconciliation(
            [position_id], close_order_ids={close_id: "another-leg"},
        )


def test_unknown_error_identity_is_retained_without_preassigned_id(tmp_path, monkeypatch):
    executor, api = opened_executor(tmp_path)
    monkeypatch.setattr(executor.broker, "prepare_close_order_id", lambda _: None)
    api.next_status, api.timeout_after_post = "new", True
    assert submit_close(executor) is None
    close_id = api.submissions[-1]["client_order_id"]
    executor.drain()
    assert executor.event_store.events()[-1].payload["close_order_id"] == close_id
    executor = restart(tmp_path, api)
    assert evaluate_restart_safety(executor.event_store.events()).safe
    assert executor.verify_known_broker_legs() == ()
    api.fill(close_id, "2.52")
    assert confirm(executor) == ("close",)
    assert executor.new_risk_allowed
    assert len(api.submissions) == 2


def test_stream_duplicates_and_old_partial_updates_do_not_double_book_v3(tmp_path):
    executor, api = opened_executor(tmp_path)
    client = executor.broker.delegate.delegate
    api.next_status = "new"
    close_id = submit_close(executor)
    executor.drain()
    api.fill(close_id, "0.5", "partially_filled", price="110")
    old = {"event": "partial_fill", "order": dict(api.orders[close_id])}
    client._on_trade_update(old)
    api.fill(close_id, "1.2", "partially_filled", price="110")
    current = {"event": "partial_fill", "order": dict(api.orders[close_id])}
    for update in (current, old, current):
        client._on_trade_update(update)
    assert executor.verify_known_broker_legs() == ()
    assert confirm(executor) == ()
    assert executor.book.active_for_symbol("AAPL").total_units == 3
    api.fill(close_id, "1.2", "canceled", price="110")
    terminal = {"event": "canceled", "order": dict(api.orders[close_id])}
    client._on_trade_update(terminal)
    assert confirm(executor) == ("close",)
    for update in (terminal, old, current):
        client._on_trade_update(update)
    executor = restart(tmp_path, api)
    assert executor.verify_known_broker_legs() == ()
    inventory = executor.book.active_for_symbol("AAPL")
    assert inventory.total_units == pytest.approx(1.8)
    assert inventory.realized_pnl == pytest.approx(12)


def test_accepted_pending_sell_does_not_add_an_account_wide_buy_gate(tmp_path):
    executor, api = opened_executor(tmp_path)
    api.next_status = "new"
    submit_close(executor)
    executor.drain()
    api.next_status = "filled"
    assert executor.schedule(
        replace(_open_intent(), intent_id="another-buy", notional=200), snapshot=_snapshot(),
    )
    assert executor.drain() == ("another-buy",)
    assert executor.book.active_for_symbol("AAPL").total_units == 5
    assert executor.verify_known_broker_legs() == ()
