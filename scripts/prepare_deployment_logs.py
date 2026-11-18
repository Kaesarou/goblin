"""Deployment-only log reset. No broker access or trading-state mutation."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
from pathlib import Path

from app.config.settings import Settings

DATA_ROOT = Path("/app/data")
LOG_FIELDS = (
    "app_log_path", "journal_path", "market_log_path", "candle_journal_path",
    "errors_journal_path", "debug_decisions_journal_path", "daily_summary_path",
    "partial_daily_summary_path", "run_manifest_path",
)


def _inside(path: Path, directory: Path) -> bool:
    # Check both the configured pathname and its resolved target.
    return (Path(os.path.abspath(path)).is_relative_to(directory)
            or path.resolve().is_relative_to(directory))


def validate_log_directory(settings: Settings, data_root: Path = DATA_ROOT) -> Path:
    root = data_root.absolute()
    logs = root / "logs"
    if root.resolve() != root or not root.is_dir():
        raise ValueError("Expected a dedicated mounted data directory")
    if logs.is_symlink() or logs.resolve() != logs or logs.is_mount():
        raise ValueError("Refusing a symlinked or separately mounted logs directory")
    if logs.exists() and not logs.is_dir():
        raise ValueError("Logs path is not a directory")

    database = Path(settings.position_store_path)
    if database.absolute().parent != root or database.resolve().parent != root:
        raise ValueError("Expected SQLite in the dedicated data directory")
    # The storage marker is durable authority for the broker and database.
    marker = root / "runtime_storage.json"
    if marker.is_symlink():
        raise ValueError("Refusing symlinked runtime identity")
    if marker.exists():
        identity = json.loads(marker.read_text(encoding="utf-8"))
        if identity != {"version": 1, "broker": settings.broker, "database": database.name}:
            raise ValueError("Runtime storage identity does not match deployment broker/database")

    order_db = Path(f"{database}.{settings.broker}.orders.sqlite")
    persistent_paths = (
        database,
        order_db,
        *[Path(f"{db}{suffix}") for db in (database, order_db)
          for suffix in ("-wal", "-shm", "-journal")],
        Path(settings.instrument_id_cache_path),
        Path(settings.alpaca_instrument_id_cache_path),
        Path(os.environ.get("GOBLIN_RESTART_GUARD_PATH", "data/runtime_restart_guard.json")),
    )
    if any(_inside(path, logs) for path in persistent_paths):
        raise ValueError("Persistent runtime state overlaps the logs directory")

    for field in LOG_FIELDS:
        path = Path(getattr(settings, field))
        if not (Path(os.path.abspath(path)).is_relative_to(logs)
                and path.resolve().is_relative_to(logs)
                and path.resolve() != logs):
            raise ValueError("Configured log output is outside the dedicated logs directory")

    # Never cross an additional filesystem mounted beneath logs.
    if logs.exists():
        for directory, subdirectories, _ in os.walk(logs, followlinks=False):
            for name in subdirectories:
                child = Path(directory) / name
                if not child.is_symlink() and child.is_mount():
                    raise ValueError("Refusing a nested mount inside logs")
    return logs


def reset_deployment_logs(settings: Settings, data_root: Path = DATA_ROOT) -> Path:
    logs = validate_log_directory(settings, data_root)
    # Same lease as RuntimeStorageScope. The old Compose service must already
    # have stopped; never erase evidence while a second runtime holds this lease.
    lock_path = data_root / ".goblin-runtime.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        logs = validate_log_directory(settings, data_root)
        logs.mkdir(exist_ok=True)
        for item in logs.iterdir():
            if item.is_symlink() or not item.is_dir():
                item.unlink()
            else:
                shutil.rmtree(item)
        if any(logs.iterdir()):
            raise RuntimeError("Logs directory is not empty after reset")
        return logs
    finally:
        os.close(lock_fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("validate", "reset"))
    parser.add_argument("--broker", required=True, choices=("etoro_demo", "alpaca_demo"))
    args = parser.parse_args()
    try:
        settings = Settings(_env_file=None)
        if settings.broker != args.broker:
            raise ValueError("Deployment broker does not match runtime configuration")
        if args.mode == "validate":
            validate_log_directory(settings)
            print("GOBLIN_DEPLOYMENT_LOG_PATHS_VALID", flush=True)
        else:
            logs = reset_deployment_logs(settings)
            print(f"GOBLIN_DEPLOYMENT_LOGS_EMPTY broker={settings.broker} path={logs} entries=0",
                  flush=True)
    except Exception:
        # Configuration validation errors can contain credentials.
        print("Goblin deployment log preparation failed; startup refused", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
