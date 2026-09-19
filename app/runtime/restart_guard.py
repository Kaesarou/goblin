"""Bound repeated production starts without touching trading state or research logs.

Docker's on-failure retry counter can reset when a container runs for 10 seconds,
so restart: on-failure:5 alone cannot stop a crash loop whose startup lasts longer.
This wrapper records each attempted app start in the persistent data volume and
exits successfully WITHOUT starting the app when the budget is exhausted.
A deliberate operator restart after the rolling window can run normally.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

RESTART_WINDOW_SECONDS = 30 * 60
MAX_STARTS_PER_WINDOW = 5
DEFAULT_GUARD_PATH = Path("data/runtime_restart_guard.json")


def record_start(
    state_path: Path,
    *,
    now: float,
    window_seconds: float = RESTART_WINDOW_SECONDS,
    max_starts: int = MAX_STARTS_PER_WINDOW,
) -> tuple[int, bool]:
    """Atomically persist this attempt and return (count, budget_exhausted).

    This function never deletes or edits the SQLite database or any run log.
    An invalid state file raises rather than silently resetting the guard.
    """
    state_path = Path(state_path)
    if state_path.exists():
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("starts"), list):
            raise ValueError("Invalid persistent restart guard state")
        starts = payload["starts"]
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in starts
        ):
            raise ValueError("Invalid persistent restart guard timestamps")
    else:
        starts = []
    recent = [
        float(value)
        for value in starts
        if 0 <= now - float(value) <= window_seconds
    ]
    recent.append(float(now))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_name(f".{state_path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump({"starts": recent}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, state_path)
    finally:
        temporary.unlink(missing_ok=True)
    return len(recent), len(recent) > max_starts


def main() -> int:
    path = Path(os.environ.get("GOBLIN_RESTART_GUARD_PATH", str(DEFAULT_GUARD_PATH)))
    try:
        count, exhausted = record_start(path, now=time.time())
    except Exception as exc:
        print(
            f"CRITICAL: restart guard cannot be verified ({type(exc).__name__}: {exc}); "
            "refusing to start Goblin; operator review required",
            file=sys.stderr, flush=True,
        )
        # on-failure must not endlessly restart a process whose safety guard is
        # itself unavailable. Exit 0 is a STOP signal, not a healthy run.
        return 0
    if exhausted:
        print(
            f"CRITICAL: {count} Goblin starts in the last "
            f"{RESTART_WINDOW_SECONDS // 60} minutes; startup circuit breaker "
            f"open; no broker action will be submitted; state={path}",
            file=sys.stderr, flush=True,
        )
        # on-failure does not restart clean exits. This is deliberately a
        # stopped container, not a simulated healthy/trading process.
        return 0

    # The conditional DEMO watcher is the guard's child, not a second wrapper:
    # it execs app.main in-place after broker-flat confirmation, preserving PID
    # and the SIGTERM/SIGINT forwarding contract for Docker stop and redeploy.
    demo_close_watcher = (
        os.environ.get("GOBLIN_DEMO_AUTO_REARM_AFTER_MANUAL_CLOSE") == "1"
        and os.environ.get("GOBLIN_OBSERVATION_ONLY") == "0"
    )
    child_module = (
        "scripts.demo_rearm_after_manual_closes" if demo_close_watcher
        else "app.main"
    )
    child = subprocess.Popen([sys.executable, "-m", child_module])
    stopping = False

    def forward_stop(signum, _frame):
        nonlocal stopping
        stopping = True
        if child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward_stop)
    signal.signal(signal.SIGINT, forward_stop)
    return_code = child.wait()
    if stopping:
        return 0
    return return_code if return_code > 0 else (1 if return_code < 0 else 0)


if __name__ == "__main__":
    raise SystemExit(main())
