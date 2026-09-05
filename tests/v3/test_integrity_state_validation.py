import json
import sqlite3
from datetime import timedelta, timezone

import pytest

from app.brokers.etoro.portfolio_position_parser import extract_open_position_units
from app.v3.operator_reconciliation import acknowledge_broker_reconciliation
from app.v3.persistence import InventoryEvent
from app.v3.state_store import CloseRetryState, V3RuntimeStateStore
from tests.v3.test_action_scoped_close_lifecycle import NOW, make_executor, submit_close


@pytest.mark.parametrize("value", [True, 0, -1, float("nan"), float("inf")])
def test_persisted_equity_rejects_invalid_authority(tmp_path, value):
    store = V3RuntimeStateStore(tmp_path / "state.sqlite")
    with pytest.raises(ValueError):
        store.save_broker_equity(value=value, observed_at=NOW, source="broker")
    assert store.load_broker_equity() is None


def test_authority_state_uses_utc_and_rejects_unknown_versions(tmp_path):
    path = tmp_path / "state.sqlite"
    store = V3RuntimeStateStore(path)
    local = NOW.astimezone(timezone(timedelta(hours=2)))
    store.save_broker_equity(value=100_000, observed_at=local, source="broker")
    assert store.load_broker_equity().observed_at == NOW
    retry = CloseRetryState("action", 3, local, "Timeout", 504, "error")
    store.save_close_retry(retry)
    restored = store.load_close_retries()["action"]
    assert restored.next_attempt_at.tzinfo == timezone.utc
    with sqlite3.connect(path) as connection:
        raw = connection.execute("SELECT state_json FROM v3_close_retry").fetchone()[0]
        assert "monotonic" not in raw
        assert json.loads(raw)["next_attempt_at"].endswith("+00:00")
        connection.execute("UPDATE v3_broker_equity SET version='future'")
        payload = json.loads(raw)
        payload["version"] = "future"
        connection.execute("UPDATE v3_close_retry SET state_json=?", (json.dumps(payload),))
    with pytest.raises(RuntimeError, match="Unsupported"):
        store.load_broker_equity()
    with pytest.raises(RuntimeError, match="Unsupported"):
        store.load_close_retries()
    with pytest.raises(ValueError, match="timezone-aware"):
        store.save_close_retry(CloseRetryState("action", 1, NOW.replace(tzinfo=None)))
    with pytest.raises(ValueError, match="timezone-aware"):
        store.save_broker_equity(value=100, observed_at=NOW.replace(tzinfo=None), source="broker")


@pytest.mark.parametrize("payload", [
    {}, {"positions": None}, {"positions": ["invalid"]}, {"positions": [{}]},
    {"positions": [{"positionID": "p1", "units": -1}]},
    {"positions": [{"positionID": "p1", "units": True}]},
    {"positions": [{"positionID": "p1", "units": float("nan")}]},
    {"positions": [{"positionID": "p1", "units": float("inf")}]},
    {"positions": [{"positionID": "p1", "units": 1}, {"positionID": "p1", "units": 1}]},
])
def test_quantitative_portfolio_parser_never_turns_malformed_state_into_zero(payload):
    with pytest.raises(ValueError):
        extract_open_position_units(payload, position_ids=("p1",))


def test_operator_acknowledgment_refuses_ledger_changed_during_broker_read(tmp_path):
    executor = make_executor(tmp_path)
    submit_close(executor, "A")
    executor.broker.units = 0.25
    executor.verify_known_broker_legs()

    def concurrent_read(ids):
        executor.event_store.append(InventoryEvent(
            "concurrent-event", "inventory", "OPERATOR_NOTE", NOW, {}, "test",
        ))
        return {position: 0.25 for position in ids}

    executor.broker.get_open_position_units = concurrent_read
    with pytest.raises(RuntimeError, match="ledger changed"):
        acknowledge_broker_reconciliation(
            event_store=executor.event_store, state_store=executor.runtime_state_store,
            broker=executor.broker, position_id="p1", expected_broker_units=0.25,
            reason="Operator abandons unprovable economics", apply=True,
        )
    assert not any(event.event_type == "BROKER_RECONCILIATION_ACKNOWLEDGED"
                   for event in executor.event_store.events())
    assert executor.runtime_state_store.load_close_retries()
