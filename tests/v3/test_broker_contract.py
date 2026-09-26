"""V3 consumes broker contracts, never an eToro implementation or payload."""

import ast
from pathlib import Path

import pytest

from app.brokers.base import BrokerAccountPreflight, OpenPositionRejectedError
from app.brokers.cached_broker import CachedBrokerClient
from app.brokers.etoro.account_equity_mapper import ACCOUNT_EQUITY_SOURCE
from app.brokers.etoro.resilient_client import ResilientEtoroClient
from app.brokers.paper.paper_broker import PaperBrokerClient
from app.config.settings import Settings
from app.v3.external_account_gate import gate_path
from tests.v3.test_executor_pending_open_reservation import _executor, _intent, _snapshot


class ExternalBroker(PaperBrokerClient):
    account_equity_source = "external_account_value"
    requires_external_activity_ack = True

    def get_account_preflight(self):
        self.preflight_calls = getattr(self, "preflight_calls", 0) + 1
        return BrokerAccountPreflight({"external-position": 2.0}, ("pending-order",))


def test_nested_cache_preserves_account_contract_and_does_not_cache_preflight():
    delegate = ExternalBroker()
    broker = CachedBrokerClient(CachedBrokerClient(delegate))
    assert broker.account_equity_source == "external_account_value"
    assert broker.requires_external_activity_ack
    first = broker.get_account_preflight()
    assert first == BrokerAccountPreflight({"external-position": 2.0}, ("pending-order",))
    assert broker.get_account_preflight() == first
    assert delegate.preflight_calls == 2


def test_external_account_gate_works_without_etoro_type_or_payload(tmp_path):
    broker = CachedBrokerClient(CachedBrokerClient(ExternalBroker()))
    executor, _, _, store = _executor(tmp_path, broker=broker)
    assert executor.verify_known_broker_legs() == ()
    assert executor.halted_reason == "external_broker_activity_observation_only"
    assert executor._last_broker_reconciliation_issues == (
        "untracked_broker_position:external-position:units=2",
        "pending_broker_open:pending-order",
    )
    assert gate_path(store.path).is_file()
    assert not executor.schedule(_intent("blocked"), snapshot=_snapshot())
    restarted, _, _, _ = _executor(tmp_path, broker=broker)
    assert restarted.halted_reason == "external_broker_activity_ack_required"


def test_generic_preflight_failure_still_fails_closed(tmp_path, monkeypatch):
    broker = ExternalBroker()
    def unavailable():
        raise ValueError("missing authoritative account data")
    monkeypatch.setattr(broker, "get_account_preflight", unavailable)
    executor, _, _, _ = _executor(tmp_path, broker=CachedBrokerClient(broker))
    with pytest.raises(ValueError, match="authoritative"):
        executor.verify_known_broker_legs()
    assert executor.halted_reason == "broker_account_preflight_unavailable"
    assert not executor.new_risk_allowed


def test_generic_structured_rejection_releases_buy_reservation(tmp_path):
    executor, broker, _, store = _executor(tmp_path)
    broker.outcomes = [OpenPositionRejectedError("terminal no-fill response")]
    assert executor.schedule(_intent("rejected"), snapshot=_snapshot())
    assert executor.drain() == ()
    assert executor.new_risk_allowed
    assert executor.confirmation_metrics()["pending_open_symbols"] == {}
    assert store.events()[-1].event_type == "ORDER_SUBMISSION_FAILED"
    assert executor.schedule(_intent("retry"), snapshot=_snapshot())


@pytest.mark.parametrize("broker_name,source,requires_ack", [
    ("paper", "paper_broker", False),
    ("etoro_demo", ACCOUNT_EQUITY_SOURCE, True),
    ("etoro_live", ACCOUNT_EQUITY_SOURCE, False),
])
def test_existing_broker_metadata_is_unchanged(broker_name, source, requires_ack):
    broker = (PaperBrokerClient() if broker_name == "paper" else ResilientEtoroClient(
        Settings.model_construct(broker=broker_name, base_currency="USD")
    ))
    cached = CachedBrokerClient(broker)
    assert cached.account_equity_source == source
    assert cached.requires_external_activity_ack is requires_ack


def test_v3_core_does_not_import_concrete_broker_adapters():
    root = Path(__file__).resolve().parents[2] / "app" / "v3"
    for path in root.glob("*.py"):
        # The run manifest describes the current eToro deployment, not broker
        # decisions. Keep its existing telemetry/schema stable in this refactor.
        if path.name == "manifest.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            modules = ([node.module] if isinstance(node, ast.ImportFrom)
                       else [alias.name for alias in node.names]
                       if isinstance(node, ast.Import) else [])
            assert not any(module and module.startswith((
                "app.brokers.etoro", "app.brokers.paper"
            )) for module in modules), path.name
