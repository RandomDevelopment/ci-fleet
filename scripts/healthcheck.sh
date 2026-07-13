#!/usr/bin/env bash
set -Eeuo pipefail

warn_percent=${CI_FLEET_DISK_WARN_PERCENT:-80}
critical_percent=${CI_FLEET_DISK_CRITICAL_PERCENT:-90}
controller_name=${CI_FLEET_CONTROLLER_CONTAINER:-ci-fleet-controller-1}
status=0

emit() { printf '%s\n' "$*"; }
if ! command -v docker >/dev/null || ! docker info >/dev/null 2>&1; then
  emit "CRITICAL docker_unavailable"
  exit 2
fi
emit "OK docker_available"

used=$(df -P /var/lib/docker 2>/dev/null | awk 'NR==2 {gsub(/%/, "", $5); print $5}')
if [[ -z "$used" ]]; then used=$(df -P / | awk 'NR==2 {gsub(/%/, "", $5); print $5}'); fi
if ((used >= critical_percent)); then emit "CRITICAL disk_used_percent=$used"; status=2
elif ((used >= warn_percent)); then emit "WARN disk_used_percent=$used"; ((status < 1)) && status=1
else emit "OK disk_used_percent=$used"; fi

controller_state=$(docker inspect --format '{{.State.Status}}' "$controller_name" 2>/dev/null || true)
if [[ "$controller_state" == running ]]; then emit "OK controller_state=running"
else emit "CRITICAL controller_state=${controller_state:-missing}"; status=2; fi

stale=$(docker ps -aq --filter label=io.randomdevelopment.ci-fleet.managed=true --filter status=exited | wc -l | tr -d ' ')
if ((stale > 0)); then emit "WARN inactive_managed_containers=$stale"; ((status < 1)) && status=1
else emit "OK inactive_managed_containers=0"; fi
exit "$status"
