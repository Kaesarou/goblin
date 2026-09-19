#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 3 ]]; then
  printf 'Usage: %s <git-sha> <image> <app-directory>\n' "$0" >&2
  exit 64
fi

git_sha="$1"
image="$2"
app_dir="$3"
release_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! "$git_sha" =~ ^[0-9a-f]{40}$ ]]; then
  printf 'Refusing invalid Git commit: %s\n' "$git_sha" >&2
  exit 65
fi

expected_image="ghcr.io/kaesarou/goblin:${git_sha}"
if [[ "$image" != "$expected_image" ]]; then
  printf 'Refusing mutable or unexpected image: %s\n' "$image" >&2
  exit 65
fi

if [[ "$app_dir" != /opt/goblin ]]; then
  printf 'Refusing unexpected deployment directory: %s\n' "$app_dir" >&2
  exit 65
fi

if [[ ! -f "$app_dir/.env" ]]; then
  printf 'Missing runtime configuration: %s/.env\n' "$app_dir" >&2
  exit 66
fi

incoming_compose="$release_dir/docker-compose.production.yml"
if [[ ! -f "$incoming_compose" ]]; then
  printf 'Missing production Compose file in release directory\n' >&2
  exit 66
fi

# The recovery release must never trade by accident, even if a manual close
# completes before startup. Refuse deployment if the safety override is absent.
if ! grep -Eq '^[[:space:]]+GOBLIN_OBSERVATION_ONLY:[[:space:]]*"1"[[:space:]]*$' "$incoming_compose"; then
  printf 'Refusing recovery release without pinned observation-only mode\n' >&2
  exit 67
fi

mkdir -p "$app_dir/data"

compose_file="$app_dir/docker-compose.production.yml"
image_env="$app_dir/.deployment.env"
next_compose="$app_dir/.docker-compose.production.yml.next"
next_image_env="$app_dir/.deployment.env.next"

deployment_diagnostics="$release_dir/deployment-failure.log"

cp "$incoming_compose" "$next_compose"
printf 'GOBLIN_IMAGE=%s\n' "$image" > "$next_image_env"

docker compose \
  --project-directory "$app_dir" \
  --env-file "$next_image_env" \
  -f "$next_compose" \
  config --quiet
docker pull "$image"

mv "$next_compose" "$compose_file"
mv "$next_image_env" "$image_env"

start_release() {
  docker compose \
    --project-directory "$app_dir" \
    --env-file "$image_env" \
    -f "$compose_file" \
    up --detach --remove-orphans --wait --wait-timeout 90
}

capture_failed_release_diagnostics() {
  {
    printf '=== Goblin failed release diagnostics ===\n'
    printf 'git_sha=%s\nimage=%s\ncaptured_at=%s\n' \
      "$git_sha" "$image" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '\n=== docker compose ps -a ===\n'
    docker compose \
      --project-directory "$app_dir" \
      --env-file "$image_env" \
      -f "$compose_file" \
      ps -a || true
    printf '\n=== container operational state ===\n'
    docker inspect --format '{{json .State}}' goblin-bot || true
    printf '\n=== docker logs --tail 500 goblin-bot ===\n'
    docker logs --timestamps --tail 500 goblin-bot || true
  } 2>&1 | tee "$deployment_diagnostics" >&2 || true
}

fail_closed() {
  printf 'Recovery deployment not validated; stopping the container without rolling back to the old trading image\n' >&2
  docker update --restart=no goblin-bot || true
  docker stop --time 30 goblin-bot || true
  exit 1
}

if ! start_release; then
  printf 'Deployment failed for %s\n' "$image" >&2
  capture_failed_release_diagnostics
  fail_closed
fi

container_id="$(docker compose \
  --project-directory "$app_dir" \
  --env-file "$image_env" \
  -f "$compose_file" \
  ps --quiet goblin)"
running_image="$(docker inspect --format '{{.Config.Image}}' "$container_id")"

if [[ "$running_image" != "$image" ]]; then
  printf 'Running image mismatch: expected %s, got %s\n' "$image" "$running_image" >&2
  capture_failed_release_diagnostics
  fail_closed
fi

# A mere PID-1 healthcheck does not prove that V3 startup/preflight succeeded.
# Observe the process for a short interval and issue only two redacted DEMO
# GETs for actual portfolio/P&L payload-shape diagnostics. Never send a POST.
printf 'Observing the read-only DEMO release before payload validation\n'
sleep 120
if ! docker inspect --format '{{.State.Running}}' "$container_id" | grep -qx true; then
  printf 'Goblin exited during observation window\n' >&2
  capture_failed_release_diagnostics
  fail_closed
fi
# A file-path invocation makes sys.path[0] /app/scripts, hiding the sibling
# /app/app package. -m runs from the image WORKDIR /app and resolves both.
if ! docker exec "$container_id" python -m scripts.inspect_etoro_payload_schema_readonly; then
  printf 'Read-only DEMO schema probe failed; refusing to certify the release\n' >&2
  capture_failed_release_diagnostics
  fail_closed
fi
printf '=== Runtime startup/health summary (no raw broker payloads) ===\n'
docker logs --timestamps --tail 180 "$container_id" 2>&1 | \
  grep -E 'v3_runtime_started|external_broker_activity|observation|CRITICAL|ERROR|Traceback|429|Timeout' | \
  tail -n 60 || true

# Do not mistake PID 1 being alive for an authorized trading state.
deployed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '{"git_commit":"%s","image":"%s","deployed_at":"%s","observation_only":true}\n' \
  "$git_sha" "$image" "$deployed_at" > "$app_dir/deployment.json"
printf 'Goblin observation-only release verified on %s (%s)\n' "$image" "$container_id"
