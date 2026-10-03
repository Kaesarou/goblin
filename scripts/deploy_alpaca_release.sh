#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

if [[ $# -ne 3 ]]; then
  printf 'Usage: %s <git-sha> <image> <app-directory>\n' "$0" >&2
  exit 64
fi

git_sha="$1"
image="$2"
app_dir="$3"
release_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project=goblin-alpaca
container=goblin-alpaca

if [[ ! "$git_sha" =~ ^[0-9a-f]{40}$ ]] || \
   [[ "$image" != "ghcr.io/kaesarou/goblin:${git_sha}" ]]; then
  printf 'Refusing invalid commit or mutable/unexpected image\n' >&2
  exit 65
fi
if [[ "$app_dir" != /opt/goblin-alpaca ]] || \
   [[ "$(realpath -m -- "$app_dir")" != "$app_dir" ]]; then
  printf 'Refusing deployment outside the dedicated /opt/goblin-alpaca directory\n' >&2
  exit 65
fi
if [[ ! -f "$app_dir/.env" || -L "$app_dir/.env" ]]; then
  printf 'Install the Alpaca runtime configuration at %s/.env first\n' "$app_dir" >&2
  exit 66
fi
if [[ "$(realpath -m -- "$app_dir/data")" != "$app_dir/data" ]]; then
  printf 'Refusing a shared or symlinked Alpaca data directory\n' >&2
  exit 65
fi
incoming_compose="$release_dir/docker-compose.alpaca.yml"
if [[ ! -f "$incoming_compose" ]]; then
  printf 'Missing Alpaca Compose file in release directory\n' >&2
  exit 66
fi

# Serialize manual invocations too, independently from the main deployment.
exec 9>"$app_dir/.deployment.lock"
flock -n 9 || { printf 'Another Alpaca deployment is running\n' >&2; exit 75; }

assert_container_owner() {
  local owner
  owner="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}/{{index .Config.Labels "com.docker.compose.service"}}' "$container" 2>/dev/null)" || return 0
  if [[ "$owner" != "$project/goblin" ]]; then
    printf 'Refusing to replace a container not owned by the Alpaca Compose project\n' >&2
    return 1
  fi
}
assert_container_owner

mkdir -p "$app_dir/data"
compose_file="$app_dir/docker-compose.alpaca.yml"
image_env="$app_dir/.deployment.env"
next_compose="$app_dir/.docker-compose.alpaca.yml.next"
next_image_env="$app_dir/.deployment.env.next"
cp "$incoming_compose" "$next_compose"
printf 'GOBLIN_IMAGE=%s\n' "$image" > "$next_image_env"

compose() {
  docker compose --project-name "$project" --project-directory "$app_dir" \
    --env-file "$image_env" -f "$compose_file" "$@"
}
docker compose --project-name "$project" --project-directory "$app_dir" \
  --env-file "$next_image_env" -f "$next_compose" config --quiet
docker pull "$image"
# Uses the same Compose env_file parsing as the service, without a broker,
# network request, SQLite write or competing runtime process.
docker compose --project-name "$project" --project-directory "$app_dir" \
  --env-file "$next_image_env" -f "$next_compose" \
  run --rm --no-deps --entrypoint python goblin -m scripts.alpaca_deployment_check

mv "$next_compose" "$compose_file"
mv "$next_image_env" "$image_env"

fail_closed() {
  printf 'Alpaca startup failed; stopping only its container, without automatic rollback\n' >&2
  # Inspect only operational state, never .Config.Env or a full container dump.
  docker inspect --format '{{json .State}}' "$container" >&2 || true
  if assert_container_owner; then
    docker update --restart=no "$container" || true
    docker stop --time 120 "$container" || true
  fi
  exit 1
}

if ! compose up --detach --wait --wait-timeout 180 goblin; then
  fail_closed
fi
container_id="$(compose ps --quiet goblin)"
running_image="$(docker inspect --format '{{.Config.Image}}' "$container_id")"
if [[ "$running_image" != "$image" ]]; then
  fail_closed
fi

printf '{"git_commit":"%s","image":"%s","deployed_at":"%s","project":"%s","container":"%s"}\n' \
  "$git_sha" "$image" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$project" "$container" \
  > "$app_dir/deployment.json"
printf 'Alpaca release deployed: %s (%s)\n' "$image" "$container_id"
