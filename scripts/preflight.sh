#!/usr/bin/env bash
set -Eeuo pipefail

failures=0
warnings=0

ok() { printf 'OK %s\n' "$*"; }
warn() { printf 'WARN %s\n' "$*"; warnings=$((warnings + 1)); }
fail() { printf 'FAIL %s\n' "$*" >&2; failures=$((failures + 1)); }

required=(
  CI_FLEET_GITHUB_URL
  CI_FLEET_SCALE_SET_NAME
  CI_FLEET_LABELS
  CI_FLEET_RUNNER_GROUP
  CI_FLEET_INSTANCE
  CI_FLEET_GITHUB_APP_CLIENT_ID
  CI_FLEET_GITHUB_APP_INSTALLATION_ID
  CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE
  CI_FLEET_DOCKER_GID
)

for name in "${required[@]}"; do
  if [[ -n "${!name:-}" ]]; then ok "$name is set"; else fail "$name is missing"; fi
done

if [[ "${CI_FLEET_MIN_RUNNERS:-0}" != 0 ]]; then fail "CI_FLEET_MIN_RUNNERS must be 0 for the pilot"; fi
if [[ "${CI_FLEET_MAX_RUNNERS:-1}" != 1 ]]; then fail "CI_FLEET_MAX_RUNNERS must be 1 for the pilot"; fi
if [[ "${CI_FLEET_SCALE_SET_NAME:-}" != *"${CI_FLEET_INSTANCE:-__missing__}"* ]]; then
  fail "scale-set name must include the unique fleet instance ID"
fi
if [[ ",${CI_FLEET_LABELS:-}," == *,self-hosted,* ]]; then fail "do not use self-hosted as a shared routing label"; fi

for command in docker stat df awk; do
  command -v "$command" >/dev/null || fail "$command is unavailable"
done
if command -v docker >/dev/null; then
  if docker info >/dev/null 2>&1; then ok "Docker daemon is reachable"; else fail "Docker daemon is unreachable"; fi
  if docker compose version >/dev/null 2>&1; then ok "Docker Compose plugin is available"; else fail "Docker Compose plugin is unavailable"; fi
fi

socket_gid=$(stat -c '%g' /var/run/docker.sock 2>/dev/null || true)
if [[ -n "$socket_gid" && "$socket_gid" == "${CI_FLEET_DOCKER_GID:-}" ]]; then
  ok "Docker socket group matches CI_FLEET_DOCKER_GID"
else
  fail "Docker socket group is ${socket_gid:-unknown}; configured value is ${CI_FLEET_DOCKER_GID:-missing}"
fi

secret=${CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE:-}
if [[ -f "$secret" ]]; then
  mode=$(stat -c '%a' "$secret")
  owner=$(stat -c '%u' "$secret")
  if [[ "$mode" == 600 && "$owner" == 0 ]]; then ok "GitHub App PEM is root-owned mode 0600"
  else fail "GitHub App PEM must be root-owned mode 0600 (found uid=$owner mode=$mode)"; fi
else
  fail "GitHub App PEM file is missing"
fi

disk_used=$(df -P /var/lib/docker 2>/dev/null | awk 'NR==2 {gsub(/%/, "", $5); print $5}')
if [[ -z "$disk_used" ]]; then disk_used=$(df -P / | awk 'NR==2 {gsub(/%/, "", $5); print $5}'); fi
if ((disk_used >= 80)); then fail "Docker filesystem is ${disk_used}% full"; else ok "Docker filesystem is ${disk_used}% full"; fi

if command -v docker >/dev/null; then
  active=$(docker ps -q \
    --filter label=io.randomdevelopment.ci-fleet.managed=true \
    --filter "label=io.randomdevelopment.ci-fleet.instance=${CI_FLEET_INSTANCE:-}" | wc -l | tr -d ' ')
  if ((active == 0)); then ok "No active managed containers for this instance"
  else fail "$active managed containers already active for this instance"; fi
fi

if ((failures > 0)); then
  printf 'PREFLIGHT_FAILED failures=%d warnings=%d\n' "$failures" "$warnings" >&2
  exit 2
fi
printf 'PREFLIGHT_OK warnings=%d\n' "$warnings"
