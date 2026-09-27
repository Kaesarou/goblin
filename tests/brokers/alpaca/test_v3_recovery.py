"""Exercise the real Alpaca journal through V3, including crash boundaries."""

from dataclasses import replace
from decimal import Decimal

import pytest

from app.brokers.cached_broker import CachedBrokerClient
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
