from pathlib import Path
import fcntl

import pytest

from app.config.settings import Settings
from scripts.prepare_deployment_logs import reset_deployment_logs, validate_log_directory


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GOBLIN_RESTART_GUARD_PATH", raising=False)
    root = tmp_path / "data"
    root.mkdir()
    logs = root / "logs"
    logs.mkdir()
    (logs / ".hidden").write_text("old")
    (logs / "runs").mkdir()
    (logs / "runs" / "manifest.json").write_text("old")
    (root / "goblin.sqlite").write_bytes(b"persistent state")
    (root / "goblin.sqlite.alpaca_demo.orders.sqlite").write_bytes(b"orders")
    (root / "runtime_restart_guard.json").write_text("guard")
    (root / "runtime_storage.json").write_text("identity")
    (root / "alpaca_instrument_ids.json").write_text("cache")
    return root, logs


@pytest.mark.parametrize("broker", ["etoro_demo", "alpaca_demo"])
def test_reset_only_logs_and_check_emptiness(storage, broker):
    root, logs = storage
    settings = Settings(_env_file=None, BROKER=broker)
    persistent = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    assert validate_log_directory(settings, root) == logs
    assert (logs / ".hidden").exists()
    assert reset_deployment_logs(settings, root) == logs
    assert list(logs.iterdir()) == []
    assert all((root / name).read_bytes() == content for name, content in persistent.items())
    assert reset_deployment_logs(settings, root) == logs


@pytest.mark.parametrize("override", [
    {"POSITION_STORE_PATH": "data/logs/goblin.sqlite"},
    {"ALPACA_INSTRUMENT_ID_CACHE_PATH": "data/logs/cache.json"},
    {"ETORO_INSTRUMENT_ID_CACHE_PATH": "data/logs/cache.json"},
    {"JOURNAL_PATH": "data/trades.jsonl"},
    {"RUN_MANIFEST_PATH": "data/logs"},
])
def test_invalid_paths_leave_old_logs_in_place(storage, override):
    root, logs = storage
    with pytest.raises(ValueError):
        reset_deployment_logs(Settings(_env_file=None, **override), root)
    assert (logs / ".hidden").exists()


def test_symlinked_logs_root_is_refused(storage, tmp_path):
    root, logs = storage
    outside = tmp_path / "other-broker"
    logs.rename(outside)
    logs.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        reset_deployment_logs(Settings(_env_file=None), root)
    assert (outside / ".hidden").exists()


def test_symlinked_log_entry_does_not_delete_persistent_target(storage):
    root, logs = storage
    (logs / "old-link").symlink_to(root / "goblin.sqlite")
    reset_deployment_logs(Settings(_env_file=None), root)
    assert (root / "goblin.sqlite").read_bytes() == b"persistent state"
    assert list(logs.iterdir()) == []


def test_active_runtime_lease_prevents_log_reset(storage):
    root, logs = storage
    with (root / ".goblin-runtime.lock").open("a+") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            reset_deployment_logs(Settings(_env_file=None), root)
    assert (logs / ".hidden").exists()


def test_restart_guard_cannot_be_in_logs(storage, monkeypatch):
    root, logs = storage
    monkeypatch.setenv("GOBLIN_RESTART_GUARD_PATH", "data/logs/guard.json")
    with pytest.raises(ValueError):
        reset_deployment_logs(Settings(_env_file=None), root)
    assert (logs / ".hidden").exists()
