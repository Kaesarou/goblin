import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/deploy_release.sh"


def _inspect_formats(source):
    """Read literal inspect invocations, including substitutions and continuations."""
    source = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    source = source.replace("\\\n", " ")
    formats = []
    for match in re.finditer(r"\bdocker\s+(?:container\s+)?inspect\b", source):
        lexer = shlex.shlex(source[match.end():], posix=True, punctuation_chars=";&|)\n")
        lexer.whitespace = " \t\r"
        args = []
        for token in lexer:
            if re.fullmatch(r"[;&|)\n]+", token):
                break
            args.append(token)
        selected = None
        for index, arg in enumerate(args):
            if arg in {"--format", "-f"} and index + 1 < len(args):
                selected = args[index + 1]
            elif arg.startswith("--format="):
                selected = arg.partition("=")[2]
        assert selected, "Unformatted docker inspect can expose container secrets"
        formats.append(selected)
    assert formats, "No inspect invocation found"
    return formats


def _assert_operational_format(format_string):
    selectors = re.findall(r"\.(?:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)?", format_string)
    assert selectors and all(field == ".State" or field.startswith(".State.") for field in selectors)
    assert not re.search(r"\b(?:Config|Env)\b", format_string, re.I)


def _diagnostics_function(source):
    return re.search(r"^capture_failed_release_diagnostics\(\)\s*\{.*?^\}",
                     source, re.M | re.S).group(0)


def test_all_deploy_inspects_are_formatted_and_diagnostics_only_select_state():
    source = SCRIPT.read_text()
    _inspect_formats(source)  # Includes the successful-path immutable image check.
    for format_string in _inspect_formats(_diagnostics_function(source)):
        _assert_operational_format(format_string)


@pytest.mark.parametrize("source", [
    "docker inspect goblin-bot || true",
    'docker inspect "$CONTAINER"',
    'docker container inspect \\\n  "$CONTAINER"',
    'state="$(docker inspect "$CONTAINER")"',
])
def test_security_guard_rejects_unformatted_inspect_variants(source):
    with pytest.raises(AssertionError, match="Unformatted"):
        _inspect_formats(source)


@pytest.mark.parametrize("format_string", [
    "{{json .}}", "{{json .Config}}", "{{json .Config.Env}}", "{{json .Env}}",
    "{{json .State}} {{json .Config.Env}}",
])
def test_diagnostic_guard_rejects_whole_container_and_sensitive_selectors(format_string):
    with pytest.raises(AssertionError):
        _assert_operational_format(format_string)


@pytest.mark.parametrize("source", [
    "docker inspect --format '{{json .State}}' goblin-bot",
    'docker inspect "$CONTAINER" --format="{{.State.Status}} {{.State.ExitCode}}"',
    'docker container inspect \\\n  -f "{{json .State.Health}}" "$CONTAINER"',
])
def test_security_guard_accepts_explicit_operational_fields(source):
    for format_string in _inspect_formats(source):
        _assert_operational_format(format_string)


@pytest.mark.parametrize("docker_exit", [0, 1])
def test_failure_diagnostics_keep_ps_logs_and_never_persist_container_env(tmp_path, docker_exit):
    # Execute only the diagnostic function, with a fake docker. No deploy,
    # rollback, production filesystem or actual container is touched.
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ["GOBLIN_TEST_DOCKER_CALLS"]).open("a") as handle:
    handle.write(json.dumps(args) + "\\n")
if args[0] == "inspect":
    if "--format" in args:
        print(json.dumps({"Status": "exited", "ExitCode": 1, "OOMKilled": False}))
    else:
        print(json.dumps({"Config": {"Env": ["ETORO_API_KEY=synthetic-secret-sentinel"]}}))
else:
    print("synthetic operational diagnostic")
sys.exit(int(os.environ["GOBLIN_TEST_DOCKER_EXIT"]))
''')
    fake_docker.chmod(0o755)
    calls_path = tmp_path / "calls.jsonl"
    diagnostics_path = tmp_path / "deployment-failure.log"
    variables = {
        "app_dir": str(tmp_path), "image_env": str(tmp_path / "image.env"),
        "compose_file": str(tmp_path / "compose.yml"), "git_sha": "a" * 40,
        "image": "test-image", "deployment_diagnostics": str(diagnostics_path),
    }
    harness = "set -Eeuo pipefail\n" + "\n".join(
        f"{key}={shlex.quote(value)}" for key, value in variables.items()
    ) + "\n" + _diagnostics_function(SCRIPT.read_text()) + "\ncapture_failed_release_diagnostics\n"
    result = subprocess.run(["bash", "-c", harness], text=True, capture_output=True, check=True,
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
             "GOBLIN_TEST_DOCKER_CALLS": str(calls_path), "GOBLIN_TEST_DOCKER_EXIT": str(docker_exit)})
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    assert len(calls) == 3
    assert calls[0][0] == "compose" and calls[0][-2:] == ["ps", "-a"]
    assert calls[1][0] == "inspect"
    _assert_operational_format(calls[1][calls[1].index("--format") + 1])
    assert calls[2] == ["logs", "--timestamps", "--tail", "500", "goblin-bot"]
    diagnostic = diagnostics_path.read_text()
    assert '"Status": "exited"' in diagnostic and '"ExitCode": 1' in diagnostic
    assert "synthetic operational diagnostic" in diagnostic
    for output in (diagnostic, result.stdout, result.stderr):
        assert "synthetic-secret-sentinel" not in output
        assert "ETORO_API_KEY" not in output


def test_deploy_release_bash_syntax():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True, capture_output=True)
