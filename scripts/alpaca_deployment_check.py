"""Offline deployment checks: never instantiate a broker or write runtime state."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import time
from datetime import datetime
from pathlib import Path

from app.config.settings import Settings
from app.runtime.storage_scope import RuntimeStorageScope

STORAGE_ROOT = Path("/app/data")


def validate_configuration(settings: Settings) -> None:
    if settings.broker != "alpaca_demo":
        raise ValueError("This deployment requires BROKER=alpaca_demo")
    for value in (settings.alpaca_api_key, settings.alpaca_secret_key):
        if not value.strip() or value.strip().lower() in {"replace_me", "####"}:
            raise ValueError("Set both Alpaca paper-account credentials in the VPS .env")
    scope = RuntimeStorageScope(settings)
    if scope.root != STORAGE_ROOT:
        raise ValueError("POSITION_STORE_PATH must place SQLite directly inside /app/data")
    # Only validate paths; entering the scope would acquire the trading lease
    # and initialize SQLite while the preceding release may still be running.
    scope._validate_alpaca_paths()


def container_started_at() -> float:
    # /proc/1/stat's starttime is field 22. The command in parentheses may
    # contain spaces; fields after its final ')' start at field 3.
    fields = Path("/proc/1/stat").read_text().rsplit(")", 1)[1].split()
    uptime = float(Path("/proc/uptime").read_text().split()[0])
    return time.time() - uptime + int(fields[19]) / os.sysconf("SC_CLK_TCK")


def validate_startup(settings: Settings, *, started_at: float, git_commit: str) -> None:
    manifest = json.loads(Path(settings.run_manifest_path).read_text())
    if (manifest["status"] != "running"
            or manifest["code"]["git_commit"] != git_commit
            or datetime.fromisoformat(manifest["started_at"]).timestamp() < started_at):
        raise ValueError("Waiting for this container's running manifest")
    broker = manifest["broker"]
    if (broker["mode"] != "alpaca_demo" or broker["universe_preflight"] != "passed"
            or not broker["account_id"]):
        raise ValueError("Waiting for Alpaca account, universe and data-feed validation")
    checkpoint_path = Path(manifest["files"]["state_start"]).resolve()
    if not checkpoint_path.is_relative_to(STORAGE_ROOT):
        raise ValueError("Startup checkpoint must be inside /app/data")
    with gzip.open(checkpoint_path, "rt") as handle:
        checkpoint = json.load(handle)
    if checkpoint["run_id"] != manifest["run_id"] or checkpoint["phase"] != "start":
        raise ValueError("Waiting for this run's startup reconciliation checkpoint")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--health", action="store_true")
    args = parser.parse_args()
    try:
        settings = Settings(_env_file=None)
        validate_configuration(settings)
        if args.health:
            validate_startup(settings, started_at=container_started_at(),
                             git_commit=os.environ["GIT_COMMIT"])
    except Exception:
        # Validation errors may include settings values. Neither Docker health
        # output nor deployment logs should contain credentials or raw settings.
        print("Alpaca deployment check failed: verify .env and runtime startup logs")
        return 1
    print("Alpaca startup validated" if args.health else "Alpaca deployment configuration validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
