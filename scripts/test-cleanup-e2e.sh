#!/usr/bin/env bash
# Use only invocation-owned fixtures on an inert validation daemon.
set -Eeuo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
temporary=$(mktemp -d)
instance="cleanup-test-$(basename "$temporary" | tr '[:upper:]' '[:lower:]')"
prefix=io.randomdevelopment.ci-fleet.
uncertain_volumes=()
cleanup_fixtures() {
  docker ps -aq --filter "label=${prefix}instance=$instance" | xargs -r docker rm -f -v >/dev/null
  docker volume ls -q --filter "label=${prefix}instance=$instance" | xargs -r docker volume rm >/dev/null
  docker network ls -q --filter "label=${prefix}instance=$instance" | xargs -r docker network rm >/dev/null
  if ((${#uncertain_volumes[@]})); then docker volume rm "${uncertain_volumes[@]}" >/dev/null 2>&1 || true; fi
  rm -rf "$temporary"
}
trap cleanup_fixtures EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -p "$temporary/proc/sys/kernel/random" "$temporary/var/run"
cat /proc/sys/kernel/random/boot_id >"$temporary/proc/sys/kernel/random/boot_id"
ln -s /var/run/docker.sock "$temporary/var/run/docker.sock"
labels=(--label "${prefix}managed=true" --label "${prefix}instance=$instance" --label "${prefix}expires-at=1")
image=${CI_FLEET_RUNNER_IMAGE:-ci-fleet-runner:dev}
for outcome in success failure cancellation timeout; do
  network=$(docker network create "${labels[@]}" "$instance-$outcome")
  volume=$(docker volume create "${labels[@]}" "$instance-$outcome")
  # The client can disappear without teardown. The task has its own finite lifetime.
  duration=0
  command='exit 0'
  if [[ "$outcome" == failure ]]; then command='exit 1'; fi
  if [[ "$outcome" == cancellation || "$outcome" == timeout ]]; then
    duration=2
    command='sleep 2'
  fi
  container=$(docker create "${labels[@]}" --network "$network" --mount "type=volume,src=$volume,dst=/fixture" --volume /uncertain \
    --entrypoint /bin/bash "$image" -c "$command")
  uncertain=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/uncertain"}}{{.Name}}{{end}}{{end}}' "$container")
  uncertain_volumes+=("$uncertain")
  if [[ "$duration" == 0 ]]; then
    docker start "$container" >/dev/null
    docker wait "$container" >/dev/null
  else
    docker start --attach "$container" >"$temporary/client.log" 2>&1 &
    client=$!
    for _ in {1..100}; do
      if [[ $(docker inspect --format '{{.State.Running}}' "$container") == true ]]; then break; fi
      sleep 0.01
    done
    if [[ "$outcome" == cancellation ]]; then kill -KILL "$client"; else kill -TERM "$client"; fi
    wait "$client" 2>/dev/null || true
    docker wait "$container" >/dev/null
  fi
  CI_FLEET_ROOT_PREFIX="$temporary" "$repo_root/scripts/cleanup.sh" --instance "$instance" >"$temporary/dry-run.log"
  docker inspect "$container" >/dev/null
  CI_FLEET_ROOT_PREFIX="$temporary" "$repo_root/scripts/cleanup.sh" --apply --instance "$instance" >"$temporary/apply.log"
  if docker inspect "$container" >/dev/null 2>&1 || docker volume inspect "$volume" >/dev/null 2>&1 || docker network inspect "$network" >/dev/null 2>&1; then
    cat "$temporary/apply.log"
    echo "ERROR host cleanup left $outcome fixture resources" >&2
    exit 1
  fi
  docker volume inspect "$uncertain" >/dev/null
  echo "OK host cleanup after $outcome without project teardown"
done
