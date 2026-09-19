"""A Saturday payload probe must not rearm trading even on a flat DEMO account."""

import importlib.util
from pathlib import Path
from datetime import datetime, timezone

from app.brokers.cached_broker import CachedBrokerClient
from app.brokers.etoro.resilient_client import ResilientEtoroClient
from app.config.settings import Settings
from app.market.models import MarketSnapshot
from app.v3.book import InventoryBook
from app.v3.external_account_gate import gate_active
from app.v3.live_execution import V3BrokerExecutor
from app.v3.models import ExecutionStyle, IntentPurpose, OrderIntent
from app.v3.persistence import InventoryEventStore

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 19, 13, 0, tzinfo=timezone.utc)


def test_observation_mode_stops_buy_with_empty_sqlite_and_flat_broker(tmp_path, monkeypatch):
    monkeypatch.setenv("GOBLIN_OBSERVATION_ONLY", "1")
    client = ResilientEtoroClient(settings=Settings.model_construct(
        broker="etoro_demo", base_currency="USD",
        etoro_api_key="fake", etoro_user_key="fake",
    ))
    monkeypatch.setattr(client, "get_portfolio", lambda: {"clientPortfolio": {"positions": []}})
    monkeypatch.setattr(client, "_get", lambda path: {"ordersForOpen": [], "orders": []})
    sqlite = tmp_path / "goblin.sqlite"
    store = InventoryEventStore(sqlite)
    executor = V3BrokerExecutor(
        broker=CachedBrokerClient(client), task_runner=object(), event_store=store,
        book=InventoryBook(), strategy_version="INVENTORY_RR5_ETORO5_V1",
        model_version=None,
    )
    assert gate_active(sqlite)
    assert not (tmp_path / "v3_external_broker_activity.json").exists()
    assert executor.verify_known_broker_legs() == ()
    assert executor.halted_reason == "external_broker_activity_ack_required"
    assert not executor.new_risk_allowed
    intent = OrderIntent(
        intent_id="probe-open", purpose=IntentPurpose.INITIAL_ENTRY,
        symbol="INTC", side="BUY", notional=300,
        created_at=NOW, execution_style=ExecutionStyle.MARKET,
    )
    snapshot = MarketSnapshot(
        symbol="INTC", bid=99, ask=100, last=100, timestamp=NOW, received_at=NOW,
    )
    assert not executor.schedule(intent, snapshot=snapshot)
    assert store.events() == []


def test_observation_gate_cannot_be_removed_by_deleting_latch(tmp_path, monkeypatch):
    monkeypatch.setenv("GOBLIN_OBSERVATION_ONLY", "1")
    assert gate_active(tmp_path / "goblin.sqlite")
    monkeypatch.delenv("GOBLIN_OBSERVATION_ONLY")
    assert not gate_active(tmp_path / "goblin.sqlite")


def test_production_release_is_pinned_read_only_and_never_rolls_back_to_old_image():
    compose = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    script = (ROOT / "scripts/deploy_release.sh").read_text(encoding="utf-8")
    assert 'GOBLIN_OBSERVATION_ONLY: "1"' in compose
    assert 'Refusing recovery release without pinned observation-only mode' in script
    assert 'docker update --restart=no goblin-bot' in script
    assert 'docker stop --time 30 goblin-bot' in script
    assert 'Restoring the previous Goblin image' not in script


def test_diagnostic_is_invoked_as_importable_module_from_image_workdir():
    script = (ROOT / "scripts/deploy_release.sh").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert 'WORKDIR /app' in dockerfile
    assert 'COPY app ./app' in dockerfile and 'COPY scripts ./scripts' in dockerfile
    assert 'docker exec "$container_id" python -m scripts.inspect_etoro_payload_schema_readonly' in script
    assert 'python scripts/inspect_etoro_payload_schema_readonly.py' not in script
    assert importlib.util.find_spec("scripts.inspect_etoro_payload_schema_readonly") is not None
