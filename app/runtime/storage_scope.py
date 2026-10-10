"""Exclusive, broker-scoped runtime storage for independent deployments.

The directory marker protects logs/caches as well as SQLite. The SQLite identity
travels with database backups and additionally pins authoritative account IDs.
Neither marker is a disposable cache. Existing unlabelled eToro directories keep
their legacy migration path; Alpaca must start with a new, dedicated directory.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from pathlib import Path

from app.config.settings import Settings

STORAGE_SCOPE_VERSION = 1
_MARKER = "runtime_storage.json"
_LOCK = ".goblin-runtime.lock"
_OUTPUT_PATHS = (
    "app_log_path", "journal_path", "market_log_path", "candle_journal_path",
    "errors_journal_path", "debug_decisions_journal_path", "daily_summary_path",
    "partial_daily_summary_path", "run_manifest_path",
)


class RuntimeStorageScope:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.database = Path(settings.position_store_path).resolve()
        self.root = self.database.parent
        self._handle = None

    def __enter__(self):
        if self.settings.broker.startswith("alpaca_"):
            self._validate_alpaca_paths()
        self.root.mkdir(parents=True, exist_ok=True)
        self._handle = (self.root / _LOCK).open("a+")
        try:
            try:
                fcntl.flock(self._handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Runtime data directory is already in use") from exc
            marker = self.root / _MARKER
            expected = {"version": STORAGE_SCOPE_VERSION, "broker": self.settings.broker,
                        "database": self.database.name}
            if marker.exists():
                if json.loads(marker.read_text(encoding="utf-8")) != expected:
                    raise RuntimeError("Runtime storage broker/database identity mismatch")
            elif self.settings.broker.startswith("alpaca_"):
                # The restart wrapper may have recorded its attempt before main.
                existing = {item.name for item in self.root.iterdir()} - {
                    _LOCK, "runtime_restart_guard.json",
                }
                if existing:
                    raise RuntimeError("Alpaca requires an empty dedicated data directory; "
                                       "unlabelled existing state cannot be adopted")
            self._bind_database()
            if not marker.exists():
                with marker.open("x", encoding="utf-8") as handle:
                    json.dump(expected, handle, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                directory = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def bind_account(self, account_id: str) -> None:
        if self._handle is None:
            raise RuntimeError("Runtime storage must be locked before binding an account")
        if not isinstance(account_id, str) or not account_id.strip() or account_id != account_id.strip():
            raise ValueError("Runtime account identity must be nonempty and canonical")
        self._bind_database(account_id)

    def _bind_database(self, account_id: str | None = None) -> None:
        with sqlite3.connect(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            existing_tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
            )}
            db.execute("CREATE TABLE IF NOT EXISTS runtime_identity ("
                       "id INTEGER PRIMARY KEY CHECK (id=1), version INTEGER NOT NULL, "
                       "broker TEXT NOT NULL, account_id TEXT)")
            row = db.execute("SELECT version, broker, account_id FROM runtime_identity WHERE id=1").fetchone()
            if row is not None:
                if row[:2] != (STORAGE_SCOPE_VERSION, self.settings.broker):
                    raise RuntimeError("SQLite broker identity mismatch")
                if account_id is not None and row[2] not in (None, account_id):
                    raise RuntimeError("SQLite account identity mismatch")
            else:
                if (self.settings.broker.startswith("alpaca_")
                        and existing_tables - {"runtime_identity"}):
                    raise RuntimeError("Unlabelled SQLite cannot be adopted by Alpaca")
                db.execute("INSERT INTO runtime_identity VALUES (1, ?, ?, NULL)",
                           (STORAGE_SCOPE_VERSION, self.settings.broker))
            if account_id is not None:
                db.execute("UPDATE runtime_identity SET account_id=? WHERE id=1", (account_id,))

    def _validate_alpaca_paths(self) -> None:
        paths = {field: Path(getattr(self.settings, field)).resolve() for field in _OUTPUT_PATHS}
        paths["alpaca_instrument_id_cache_path"] = Path(
            self.settings.alpaca_instrument_id_cache_path,
        ).resolve()
        guard = os.environ.get("GOBLIN_RESTART_GUARD_PATH")
        if guard:
            paths["GOBLIN_RESTART_GUARD_PATH"] = Path(guard).resolve()
        reserved = {self.database, self.root / _MARKER, self.root / _LOCK,
                    Path(f"{self.database}.{self.settings.broker}.orders.sqlite")}
        for field, path in paths.items():
            if not path.is_relative_to(self.root) or path == self.root or path in reserved:
                raise ValueError(f"{field} must be a separate file inside the dedicated data directory")
        if len(set(paths.values())) != len(paths):
            raise ValueError("Alpaca output/cache/guard paths must not alias each other")
