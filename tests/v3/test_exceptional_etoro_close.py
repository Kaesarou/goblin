"""Exceptional eToro execution: corroborate, close exactly once, re-arm only flat."""

from datetime import timedelta

import pytest

from app.brokers.base import ClosePositionSubmission
from app.v3.book import InventoryBook
from app.v3.live_execution import V3BrokerExecutor
from tests.v3.test_open_economics_recovery import (
    NOW,
    evidence,
    restart,
    setup_executor,
)


class FavorablePriceBroker:
    """Attach a mock full-close endpoint to the canonical economic broker fake."""

    @staticmethod
    def attach(broker):
        broker.close_calls = []
        broker.forget_position_instrument = lambda position_id: None

        def close_position(position_id, units_to_deduct=None):
            broker.close_calls.append((position_id, units_to_deduct))
            return ClosePositionSubmission(
                position_id=position_id,
                close_order_id="close-exceptional-1",
                reference_id=None,
                submitted_at=NOW,
                accepted_at=NOW,
                broker_response={"mode": "paper"},
            )

        broker.close_position = close_position
        return broker


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr("app.v3.live_execution._utc_now", lambda: NOW)


def test_favorable_price_is_verified_then_fully_closed_and_rearmed(tmp_path):
    executor, broker, store, _, quote = setup_executor(
        tmp_path, price=53.0, ask=56.02, notional=300.0,
    )
    FavorablePriceBroker.attach(broker)
    broker.evidence = {"p1": evidence(price=53.0, amount=300.0)}
    assert executor._schedule_open_economics_revalidation(NOW)
    executor.drain()

    assert not executor.new_risk_allowed
    assert len(executor._open_economics.confirmed_outliers) == 1
    assert executor.book.active_for_symbol("IFX.DE").average_entry_price == 53.0
    assert len([event for event in store.events()
                if event.event_type == "OPEN_EXCEPTIONAL_PRICE_CONFIRMED"]) == 1

    assert executor.schedule_exceptional_close(snapshot=quote)
    executor.drain()
    assert broker.close_calls == [("p1", None)]
    assert executor.book.active_for_symbol("IFX.DE") is None
    assert not executor.new_risk_allowed
    assert not executor.schedule_exceptional_close(snapshot=quote)

    assert executor._schedule_open_economics_revalidation(NOW + timedelta(seconds=61))
    executor.drain()
    assert executor.new_risk_allowed
    assert restart(executor, broker, store).new_risk_allowed
    events = store.events()
    assert sum(event.event_type == "OPEN_FILL_ECONOMICS_RECONCILED"
               for event in events) == 1
    assert sum(event.event_type == "EXIT_FILLED" for event in events) == 1
    assert InventoryBook.from_events(events).active_for_symbol("IFX.DE") is None


def test_unproved_outlier_never_triggers_close(tmp_path):
    executor, broker, store, _, quote = setup_executor(tmp_path)
    FavorablePriceBroker.attach(broker)
    broker.evidence = {"p1": evidence(price=52.0)}
    assert executor._schedule_open_economics_revalidation(NOW)
    executor.drain()
    assert not executor.schedule_exceptional_close(snapshot=quote)
    assert broker.close_calls == []
    assert not executor.new_risk_allowed


def test_independent_unresolved_risk_blocks_exceptional_close(tmp_path):
    executor, broker, _, _, quote = setup_executor(tmp_path)
    FavorablePriceBroker.attach(broker)
    broker.evidence = {"p1": evidence(price=53.0)}
    assert executor._schedule_open_economics_revalidation(NOW)
    executor.drain()
    executor.halted_reason = "untracked_broker_positions"
    assert not executor.schedule_exceptional_close(snapshot=quote)
    assert not broker.close_calls
