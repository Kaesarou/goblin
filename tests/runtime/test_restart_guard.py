"""The production restart guard is durable across separate processes."""

import json
from pathlib import Path

import pytest

from app.runtime import restart_guard


def test_production_compose_runs_guard_with_persistent_data_volume():
    """A unit-tested guard is useless if production bypasses the wrapper."""
    compose = (Path(__file__).resolve().parents[2] /
               "docker-compose.production.yml").read_text(encoding="utf-8")
    assert 'command: ["python", "-m", "app.runtime.restart_guard"]' in compose
    assert './data:/app/data' in compose
    assert 'restart: "on-failure:5"' in compose


def test_sixth_start_within_half_hour_is_blocked_even_if_each_run_survives(tmp_path):
    path = tmp_path / "data" / "runtime_restart_guard.json"
    for attempt in range(5):
        count, exhausted = restart_guard.record_start(path, now=1000.0 + attempt * 60)
        assert count == attempt + 1
        assert not exhausted
    count, exhausted = restart_guard.record_start(path, now=1300.0)
    assert (count, exhausted) == (6, True)
    assert len(json.loads(path.read_text())["starts"]) == 6


def test_healthy_period_expires_prior_failed_start_budget(tmp_path):
    path = tmp_path / "guard.json"
    for attempt in range(6):
        restart_guard.record_start(path, now=1000.0 + attempt)
    assert restart_guard.record_start(path, now=3000.0) == (1, False)


def test_corrupt_persistent_guard_does_not_silently_reset(tmp_path):
    path = tmp_path / "guard.json"
    path.write_text('{"starts": ["invalid"]}', encoding="utf-8")
    with pytest.raises(ValueError, match="timestamps"):
        restart_guard.record_start(path, now=1000.0)
    assert path.read_text(encoding="utf-8") == '{"starts": ["invalid"]}'


def test_exhausted_main_never_launches_trading_process(tmp_path, monkeypatch):
    path = tmp_path / "guard.json"
    for attempt in range(5):
        restart_guard.record_start(path, now=1000.0 + attempt)
    monkeypatch.setenv("GOBLIN_RESTART_GUARD_PATH", str(path))
    monkeypatch.setattr(restart_guard.time, "time", lambda: 1006.0)
    monkeypatch.setattr(
        restart_guard.subprocess, "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("app.main must not start after breaker trips")
        ),
    )
    assert restart_guard.main() == 0


def test_guard_io_failure_stops_instead_of_retrying(tmp_path, monkeypatch):
    path = tmp_path / "guard.json"
    path.write_text("not json", encoding="utf-8")
    monkeypatch.setenv("GOBLIN_RESTART_GUARD_PATH", str(path))
    monkeypatch.setattr(
        restart_guard.subprocess, "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("app.main must not start without a verified guard")
        ),
    )
    assert restart_guard.main() == 0
