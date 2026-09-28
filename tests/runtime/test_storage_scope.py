import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from app.config.settings import Settings
from app.runtime.storage_scope import RuntimeStorageScope
from app.v3.persistence import InventoryEventStore
from tests.v3.test_live_execution import _open_intent, _snapshot
from tests.v3.test_partial_close_execution import _executor


def storage_settings(root, broker="alpaca_demo", **overrides):
    paths = {field.alias: str(root / Path(field.default).relative_to("data"))
             for field in Settings.model_fields.values()
             if isinstance(field.default, str) and field.default.startswith("data/")}
    return Settings(_env_file=None, **{
        **paths, "BROKER": broker, "ALPACA_API_KEY": "key", "ALPACA_SECRET_KEY": "secret",
        "WATCHLIST": "AAPL", "EQUITY_US_SYMBOLS": "AAPL", "EQUITY_EU_SYMBOLS": "",
        "CRYPTO_SYMBOLS": "", "MARKET_BENCHMARK_EQUITY_US": "SPY", **overrides,
    })


def identity(settings):
    with sqlite3.connect(settings.position_store_path) as db:
        return db.execute("SELECT broker, account_id FROM runtime_identity").fetchone()


def test_separate_scopes_run_concurrently_and_retain_independent_accounts(tmp_path):
    etoro = storage_settings(tmp_path / "etoro", "etoro_demo")
    alpaca = storage_settings(tmp_path / "alpaca")
    with RuntimeStorageScope(etoro), RuntimeStorageScope(alpaca) as scope:
        scope.bind_account("account-a")
        assert identity(etoro) == ("etoro_demo", None)
        assert identity(alpaca) == ("alpaca_demo", "account-a")
    with RuntimeStorageScope(alpaca) as scope:
        scope.bind_account("account-a")
    assert "secret" not in (tmp_path / "alpaca" / "runtime_storage.json").read_text()


def test_second_runtime_cannot_share_a_directory_even_with_another_sqlite_name(tmp_path):
    config = storage_settings(tmp_path)
    with RuntimeStorageScope(config):
        for other in (config, config.model_copy(update={"position_store_path": str(tmp_path / "other.sqlite")})):
            with pytest.raises(RuntimeError, match="already in use"):
                with RuntimeStorageScope(other):
                    pytest.fail("Second process acquired runtime data")
    with RuntimeStorageScope(config):
        pass


@pytest.mark.parametrize("mode", ["etoro_demo", "etoro_live", "paper", "alpaca_live"])
def test_reusing_alpaca_directory_for_another_broker_is_rejected(tmp_path, mode):
    config = storage_settings(tmp_path)
    with RuntimeStorageScope(config) as scope:
        scope.bind_account("account-a")
    before = Path(config.position_store_path).read_bytes()
    with pytest.raises(RuntimeError, match="identity mismatch"):
        with RuntimeStorageScope(storage_settings(tmp_path, mode)):
            pytest.fail("Wrong broker was accepted")
    assert Path(config.position_store_path).read_bytes() == before


def test_alpaca_refuses_existing_unlabelled_state_without_changing_it(tmp_path):
    config = storage_settings(tmp_path)
    path = Path(config.position_store_path)
    InventoryEventStore(path)
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="empty dedicated"):
        with RuntimeStorageScope(config):
            pytest.fail("Legacy data adopted")
    assert path.read_bytes() == before
    assert not (tmp_path / "runtime_storage.json").exists()


def test_existing_etoro_data_is_preserved_and_labelled_without_replaying(tmp_path):
    from app.brokers.paper.paper_broker import PaperBrokerClient
    from app.v3.book import InventoryBook

    executor = _executor(tmp_path, PaperBrokerClient(), InventoryBook())
    executor.schedule(replace(_open_intent(), intent_id="legacy"), snapshot=_snapshot())
    executor.drain()
    config = storage_settings(tmp_path, "etoro_demo", POSITION_STORE_PATH=str(executor.event_store.path))
    events = executor.event_store.events()
    with RuntimeStorageScope(config):
        assert InventoryEventStore(config.position_store_path).events() == events
    assert identity(config) == ("etoro_demo", None)
    with pytest.raises(RuntimeError, match="identity mismatch"):
        with RuntimeStorageScope(storage_settings(tmp_path, POSITION_STORE_PATH=str(executor.event_store.path))):
            pytest.fail("eToro data adopted")


def test_alpaca_account_is_pinned_in_sqlite_independently_of_adapter_journal(tmp_path):
    config = storage_settings(tmp_path)
    with RuntimeStorageScope(config) as scope:
        scope.bind_account("account-a")
    with RuntimeStorageScope(config) as scope:
        with pytest.raises(RuntimeError, match="account identity mismatch"):
            scope.bind_account("account-b")
    assert identity(config) == ("alpaca_demo", "account-a")


def test_copied_alpaca_sqlite_cannot_be_opened_as_etoro_without_directory_marker(tmp_path):
    config = storage_settings(tmp_path / "alpaca")
    with RuntimeStorageScope(config) as scope:
        scope.bind_account("account-a")
    other = storage_settings(tmp_path / "etoro", "etoro_demo")
    path = Path(other.position_store_path)
    path.parent.mkdir()
    path.write_bytes(Path(config.position_store_path).read_bytes())
    with pytest.raises(RuntimeError, match="SQLite broker"):
        with RuntimeStorageScope(other):
            pytest.fail("Copied database adopted")


@pytest.mark.parametrize("field,value", [
    ("APP_LOG_PATH", "../shared.log"), ("JOURNAL_PATH", "../shared/trades.jsonl"),
    ("ALPACA_INSTRUMENT_ID_CACHE_PATH", "../etoro.json"),
    ("RUN_MANIFEST_PATH", "runtime_storage.json"), ("ERRORS_JOURNAL_PATH", "goblin.sqlite"),
    ("ALPACA_INSTRUMENT_ID_CACHE_PATH", "goblin.sqlite.alpaca_demo.orders.sqlite"),
    ("CANDLE_JOURNAL_PATH", "logs/goblin.log"),
])
def test_shared_or_aliased_output_paths_are_rejected_before_creating_state(tmp_path, field, value):
    root = tmp_path / "alpaca"
    config = storage_settings(root, **{field: str(root / value)})
    with pytest.raises(ValueError, match="paths must not alias|separate file"):
        with RuntimeStorageScope(config):
            pytest.fail("Shared path was accepted")
    assert not root.exists()


def test_symlink_outside_scope_is_rejected(tmp_path):
    root = tmp_path / "alpaca"
    root.mkdir()
    shared = tmp_path / "etoro"
    shared.mkdir()
    (root / "logs").symlink_to(shared, target_is_directory=True)
    with pytest.raises(ValueError, match="separate file"):
        with RuntimeStorageScope(storage_settings(root)):
            pytest.fail("Symlink escaped scope")


def test_restart_wrapper_state_does_not_prevent_first_alpaca_start(tmp_path):
    (tmp_path / "runtime_restart_guard.json").write_text(json.dumps({"starts": [100]}))
    with RuntimeStorageScope(storage_settings(tmp_path)):
        pass


def test_invalid_marker_fails_closed_and_releases_the_process_lock(tmp_path):
    (tmp_path / "runtime_storage.json").write_text("invalid")
    for _ in range(2):
        with pytest.raises(ValueError):
            with RuntimeStorageScope(storage_settings(tmp_path)):
                pytest.fail("Invalid marker accepted")


def test_main_acquires_scope_before_journals_or_clients(tmp_path, monkeypatch):
    from app import main

    config = storage_settings(tmp_path)
    monkeypatch.setattr(main, "get_settings", lambda: config)
    # Exercise the future bootstrap wiring, without granting demo authority.
    monkeypatch.setattr(main, "_assert_v3_execution_mode", lambda broker: None)
    entered = []
    def run(settings, scope):
        assert identity(settings) == ("alpaca_demo", None)
        with pytest.raises(RuntimeError, match="already in use"):
            with RuntimeStorageScope(settings):
                pytest.fail("Scope not held through runtime")
        entered.append(True)
    monkeypatch.setattr(main, "_run_main", run)
    main.main()
    assert entered == [True]
