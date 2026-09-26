"""A queued manual CLOSE is never proof that an old DEMO position filled."""

from types import SimpleNamespace

import pytest

from app.v3.external_account_gate import gate_path, record_external_broker_activity
from scripts import demo_rearm_after_manual_closes as rearm


class Broker:
    env = "demo"

    def __init__(self, positions, pending=None):
        self.positions = iter(positions)
        self.pending = iter(pending or [])
        self.current = []
        self.pending_current = []
        self.portfolio_reads = 0

    def get_portfolio(self):
        self.current = next(self.positions)
        self.portfolio_reads += 1
        return {"clientPortfolio": {"positions": self.current}}

    def _get(self, path):
        assert path == "/api/v1/trading/info/demo/pnl"
        self.pending_current = next(self.pending, [])
        return {"ordersForOpen": self.pending_current, "orders": []}


def _open_position():
    return {"positionID": "3599774868", "units": 3.292115, "isOpen": True}


def test_broker_flat_requires_no_open_position_and_no_pending_open():
    assert not rearm.broker_account_flat(Broker([[_open_position()]]))
    assert not rearm.broker_account_flat(Broker([[]], [[{"orderId": "pending"}]]))
    assert rearm.broker_account_flat(Broker([[]]))
    with pytest.raises(ValueError, match="Missing authoritative broker positions"):
        rearm.broker_account_flat(SimpleNamespace(
            env="demo", get_portfolio=lambda: {"unexpected": []}))


def _setup(monkeypatch, tmp_path, broker, *, events=(), mode="etoro_demo", marker=True):
    sqlite = tmp_path / "goblin.sqlite"
    sqlite.touch()
    if marker:
        record_external_broker_activity(sqlite, issues=("untracked_broker_position:3599774868",))
    monkeypatch.setenv("GOBLIN_OBSERVATION_ONLY", "0")
    monkeypatch.setenv("GOBLIN_DEMO_AUTO_REARM_AFTER_MANUAL_CLOSE", "1")
    monkeypatch.setattr(rearm, "Settings", lambda: SimpleNamespace(
        broker=mode, base_currency="USD", position_store_path=str(sqlite)))
    monkeypatch.setattr(rearm, "InventoryEventStore", lambda _: SimpleNamespace(
        events=lambda: list(events)))
    monkeypatch.setattr(rearm, "ResilientEtoroClient", lambda settings: broker)
    return sqlite


def test_queued_closes_wait_then_two_flat_reads_archive_marker(monkeypatch, tmp_path):
    broker = Broker([[ _open_position() ], [], []])
    sqlite = _setup(monkeypatch, tmp_path, broker)
    sleeps = []
    monkeypatch.setattr(rearm.time, "sleep", lambda seconds: sleeps.append(seconds))
    starts = []
    def start():
        starts.append(True)
        assert not gate_path(sqlite).exists()
        return 0
    monkeypatch.setattr(rearm, "start_normal_runtime", start)
    assert rearm.main() == 0
    assert broker.portfolio_reads == 3
    assert sleeps == [rearm.CHECK_INTERVAL_SECONDS] * 2
    assert starts == [True]
    assert len(list(tmp_path.glob("v3_external_broker_activity.acknowledged.*.json"))) == 1
    assert sqlite.exists()  # Existing event ledger is never deleted or reset.


def test_pending_open_resets_flat_confirmations(monkeypatch, tmp_path):
    broker = Broker([[], [], [], []], [[], [{"orderId": 1}], [], []])
    sqlite = _setup(monkeypatch, tmp_path, broker)
    monkeypatch.setattr(rearm.time, "sleep", lambda _: None)
    monkeypatch.setattr(rearm, "start_normal_runtime", lambda: 0)
    assert rearm.main() == 0
    assert broker.portfolio_reads == 4
    assert not gate_path(sqlite).exists()


def test_unknown_broker_response_stays_waiting(monkeypatch, tmp_path):
    broker = Broker([[_open_position()], [{"positionID": "bad"}]])
    sqlite = _setup(monkeypatch, tmp_path, broker)
    calls = []
    def stop_after_one_sleep(_):
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt
    monkeypatch.setattr(rearm.time, "sleep", stop_after_one_sleep)
    monkeypatch.setattr(rearm, "start_normal_runtime", lambda: pytest.fail("unexpected BUY rearm"))
    with pytest.raises(KeyboardInterrupt):
        rearm.main()
    assert gate_path(sqlite).exists()


def test_existing_ledger_with_external_gate_cannot_be_acknowledged(monkeypatch, tmp_path):
    broker = Broker([[]])
    sqlite = _setup(monkeypatch, tmp_path, broker, events=(object(),))
    monkeypatch.setattr(rearm, "start_normal_runtime", lambda: pytest.fail("unsafe start"))
    assert rearm.main() == 0
    assert gate_path(sqlite).exists()
    assert broker.portfolio_reads == 0


def test_existing_ledger_without_marker_uses_normal_reconciliation(monkeypatch, tmp_path):
    broker = Broker([[]])
    _setup(monkeypatch, tmp_path, broker, events=(object(),), marker=False)
    monkeypatch.setattr(rearm, "start_normal_runtime", lambda: 7)
    assert rearm.main() == 7
    assert broker.portfolio_reads == 0


def test_real_account_and_forced_observation_never_rearm(monkeypatch, tmp_path):
    broker = Broker([[]])
    sqlite = _setup(monkeypatch, tmp_path, broker, mode="etoro_live")
    monkeypatch.setattr(rearm, "start_normal_runtime", lambda: pytest.fail("unsafe start"))
    assert rearm.main() == 0
    assert gate_path(sqlite).exists()
    monkeypatch.setenv("GOBLIN_OBSERVATION_ONLY", "1")
    assert rearm.main() == 0
