import gzip
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config.settings import Settings
from scripts import alpaca_deployment_check as check

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/deploy_alpaca_release.sh"
SHA = "a" * 40
IMAGE = f"ghcr.io/kaesarou/goblin:{SHA}"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(check, "STORAGE_ROOT", tmp_path / "data")
    monkeypatch.delenv("GOBLIN_RESTART_GUARD_PATH", raising=False)
    return Settings(_env_file=None, BROKER="alpaca_demo",
                    ALPACA_API_KEY="synthetic-api", ALPACA_SECRET_KEY="synthetic-secret")


@pytest.mark.parametrize("broker", ["paper", "etoro_demo", "etoro_live", "alpaca_live"])
def test_configuration_rejects_other_accounts_without_touching_state(settings, broker):
    settings.broker = broker
    with pytest.raises(ValueError, match="BROKER=alpaca_demo"):
        check.validate_configuration(settings)
    assert not Path("data").exists()


@pytest.mark.parametrize("key", ["", "replace_me", "####"])
def test_configuration_rejects_missing_keys(settings, key):
    settings.alpaca_secret_key = key
    with pytest.raises(ValueError, match="credentials"):
        check.validate_configuration(settings)


def test_configuration_is_read_only_and_requires_persistent_isolated_paths(settings):
    check.validate_configuration(settings)
    assert not Path("data").exists()
    settings.position_store_path = "elsewhere/goblin.sqlite"
    with pytest.raises(ValueError, match="/app/data"):
        check.validate_configuration(settings)
    settings.position_store_path = "data/goblin.sqlite"
    settings.app_log_path = "outside/goblin.log"
    with pytest.raises(ValueError, match="inside the dedicated"):
        check.validate_configuration(settings)


@pytest.fixture
def startup(settings):
    checkpoint_path = Path("data/logs/runs/current/state_start.json.gz").resolve()
    checkpoint_path.parent.mkdir(parents=True)
    manifest = {
        "status": "running", "started_at": datetime.fromtimestamp(100, UTC).isoformat(),
        "run_id": "current", "code": {"git_commit": SHA},
        "broker": {"mode": "alpaca_demo", "universe_preflight": "passed", "account_id": "paper"},
        "files": {"state_start": str(checkpoint_path)},
    }
    with gzip.open(checkpoint_path, "wt") as handle:
        json.dump({"run_id": "current", "phase": "start"}, handle)
    Path(settings.run_manifest_path).write_text(json.dumps(manifest))
    return manifest


def test_health_accepts_completed_startup_without_broker_access(settings, startup):
    check.validate_startup(settings, started_at=99, git_commit=SHA)


@pytest.mark.parametrize("failure", ["stale", "wrong_image", "failed", "pending", "checkpoint"])
def test_health_rejects_stale_or_incomplete_startup(settings, startup, failure):
    started_at = 101 if failure == "stale" else 99
    git_commit = "b" * 40 if failure == "wrong_image" else SHA
    if failure == "failed":
        startup["status"] = "failed"
    elif failure == "pending":
        startup["broker"]["universe_preflight"] = "pending"
    elif failure == "checkpoint":
        with gzip.open(startup["files"]["state_start"], "wt") as handle:
            json.dump({"run_id": "previous", "phase": "start"}, handle)
    Path(settings.run_manifest_path).write_text(json.dumps(startup))
    with pytest.raises(ValueError):
        check.validate_startup(settings, started_at=started_at, git_commit=git_commit)


def test_cli_never_prints_settings_validation_errors(monkeypatch, capsys):
    def invalid_settings(**_):
        raise ValueError("ALPACA_SECRET_KEY=synthetic-secret-sentinel")
    monkeypatch.setattr(check, "Settings", invalid_settings)
    monkeypatch.setattr(sys, "argv", ["alpaca_deployment_check"])
    assert check.main() == 1
    captured = capsys.readouterr()
    assert "synthetic-secret-sentinel" not in captured.out + captured.err


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    """Run the real Bash control flow against fake Docker, never /opt or a broker."""
    app_dir = tmp_path / "alpaca"
    app_dir.mkdir()
    (app_dir / ".env").write_text("BROKER=alpaca_demo\n")
    release = app_dir / "releases" / SHA
    release.mkdir(parents=True)
    # Substitute only the hardcoded root for this isolated harness.
    script = release / SCRIPT.name
    script.write_text(SCRIPT.read_text().replace("/opt/goblin-alpaca", str(app_dir)))
    (release / "docker-compose.alpaca.yml").write_text(
        (ROOT / "docker-compose.alpaca.yml").read_text())
    calls = tmp_path / "docker-calls.jsonl"
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ["DEPLOY_TEST_CALLS"]).open("a") as handle:
    handle.write(json.dumps(args) + "\\n")
failure = os.environ.get("DEPLOY_TEST_FAILURE")
if args[0] == "inspect":
    fmt = args[args.index("--format") + 1]
    if "Labels" in fmt:
        print("main/goblin" if failure == "owner" else "goblin-alpaca/goblin")
    elif "Image" in fmt:
        print("wrong-image" if failure == "image" else os.environ["DEPLOY_TEST_IMAGE"])
    else:
        print('{"Status":"exited"}')
elif args[0] == "compose":
    if "run" in args and failure == "configuration":
        sys.exit(1)
    if "up" in args and failure == "startup":
        sys.exit(1)
    if "ps" in args:
        print("alpaca-container-id")
elif args[0] == "pull" and failure == "pull":
    sys.exit(1)
''')
    fake_docker.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("DEPLOY_TEST_CALLS", str(calls))
    monkeypatch.setenv("DEPLOY_TEST_IMAGE", IMAGE)

    def run(*, failure="", app_directory=None, image=IMAGE):
        monkeypatch.setenv("DEPLOY_TEST_FAILURE", failure)
        result = subprocess.run(["bash", str(script), SHA, image, str(app_directory or app_dir)],
                                capture_output=True, text=True, check=False)
        invocations = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
        return result, invocations

    return app_dir, run


def test_successful_release_only_controls_its_explicit_compose_project(deployment):
    app_dir, run = deployment
    result, calls = run()
    assert result.returncode == 0, result.stderr
    assert json.loads((app_dir / "deployment.json").read_text())["image"] == IMAGE
    for args in calls:
        assert "goblin-bot" not in args
        assert "--remove-orphans" not in args and "down" not in args
        if args[0] == "compose":
            assert args[args.index("--project-name") + 1] == "goblin-alpaca"
            assert args[args.index("--project-directory") + 1] == str(app_dir)
    assert not any(args[0] in {"stop", "update"} for args in calls)


@pytest.mark.parametrize("failure", ["configuration", "pull", "owner"])
def test_failed_preparation_leaves_existing_containers_and_release_untouched(deployment, failure):
    app_dir, run = deployment
    installed = app_dir / "docker-compose.alpaca.yml"
    installed.write_text("previous release")
    result, calls = run(failure=failure)
    assert result.returncode != 0
    assert installed.read_text() == "previous release"
    assert not any(args[0] in {"stop", "update"} or "up" in args for args in calls)


@pytest.mark.parametrize("failure", ["startup", "image"])
def test_failed_startup_stops_only_alpaca_with_graceful_shutdown(deployment, failure):
    app_dir, run = deployment
    result, calls = run(failure=failure)
    assert result.returncode != 0
    assert ["update", "--restart=no", "goblin-alpaca"] in calls
    assert ["stop", "--time", "120", "goblin-alpaca"] in calls
    assert not (app_dir / "deployment.json").exists()
    assert all("goblin-bot" not in args for args in calls)
    assert all("--format" in args for args in calls if args[0] == "inspect")


@pytest.mark.parametrize("invalid", ["main_directory", "missing_env", "shared_data", "mutable_image"])
def test_invalid_targets_are_rejected_before_any_docker_call(deployment, tmp_path, invalid):
    app_dir, run = deployment
    kwargs = {}
    if invalid == "main_directory":
        kwargs["app_directory"] = "/opt/goblin"
    elif invalid == "missing_env":
        (app_dir / ".env").unlink()
    elif invalid == "shared_data":
        (app_dir / "data").symlink_to(tmp_path / "main-data")
    elif invalid == "mutable_image":
        kwargs["image"] = "ghcr.io/kaesarou/goblin:alpaca-experimental"
    result, calls = run(**kwargs)
    assert result.returncode != 0
    assert calls == []


def test_alpaca_release_bash_syntax():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True, capture_output=True)
