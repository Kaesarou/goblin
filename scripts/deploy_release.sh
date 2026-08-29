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

mkdir -p "$app_dir/data"

compose_file="$app_dir/docker-compose.production.yml"
image_env="$app_dir/.deployment.env"
next_compose="$app_dir/.docker-compose.production.yml.next"
next_image_env="$app_dir/.deployment.env.next"
previous_compose="$app_dir/.docker-compose.production.yml.previous"
previous_image_env="$app_dir/.deployment.env.previous"

deployment_diagnostics="$release_dir/deployment-failure.log"

cp "$incoming_compose" "$next_compose"
printf 'GOBLIN_IMAGE=%s\n' "$image" > "$next_image_env"

docker compose \
  --project-directory "$app_dir" \
  --env-file "$next_image_env" \
  -f "$next_compose" \
  config --quiet
docker pull "$image"

had_previous=false
if [[ -f "$compose_file" && -f "$image_env" ]]; then
  cp "$compose_file" "$previous_compose"
  cp "$image_env" "$previous_image_env"
  had_previous=true
fi

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
    printf '\n=== docker inspect goblin-bot ===\n'
    docker inspect goblin-bot || true
    printf '\n=== docker logs --tail 500 goblin-bot ===\n'
    docker logs --timestamps --tail 500 goblin-bot || true
  } 2>&1 | tee "$deployment_diagnostics" >&2 || true
}

if ! start_release; then
  printf 'Deployment failed for %s\n' "$image" >&2
  capture_failed_release_diagnostics
  if [[ "$had_previous" == true ]]; then
    printf 'Restoring the previous Goblin image\n' >&2
    mv "$previous_compose" "$compose_file"
    mv "$previous_image_env" "$image_env"
    start_release
  fi
  exit 1
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
  exit 1
fi

deployed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '{"git_commit":"%s","image":"%s","deployed_at":"%s"}\n' \
  "$git_sha" "$image" "$deployed_at" > "$app_dir/deployment.json"
printf 'Goblin is healthy on %s (%s)\n' "$image" "$container_id"
