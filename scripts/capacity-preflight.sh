#!/usr/bin/env bash
set -Eeuo pipefail

phase=
target_max=

usage() {
  printf 'usage: %s --phase pre-change|post-change --target-max N\n' "$0" >&2
}

die() {
  printf 'FAIL %s\n' "$1" >&2
  printf 'CAPACITY_PREFLIGHT_FAILED phase=%s\n' "${phase:-unknown}" >&2
  exit 2
}

while (($#)); do
  case "$1" in
    --phase)
      (($# >= 2)) || die '--phase requires a value'
      phase=$2
      shift 2
      ;;
    --target-max)
      (($# >= 2)) || die '--target-max requires a value'
      target_max=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      die 'unknown capacity-preflight argument'
      ;;
  esac
done

[[ "$phase" == pre-change || "$phase" == post-change ]] || die 'explicit --phase pre-change or post-change is required'
[[ -n "$target_max" ]] || die 'explicit --target-max is required'
[[ "$target_max" =~ ^[1-9][0-9]{0,3}$ ]] || die 'target MAX must be a positive integer'
target_max=$((10#$target_max))

required=(
  CI_FLEET_GITHUB_URL
  CI_FLEET_SCALE_SET_NAME
  CI_FLEET_LABELS
  CI_FLEET_RUNNER_GROUP
  CI_FLEET_INSTANCE
  CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE
  CI_FLEET_DOCKER_GID
  CI_FLEET_MIN_RUNNERS
  CI_FLEET_MAX_RUNNERS
  CI_FLEET_RUNNER_CPUS
  CI_FLEET_RUNNER_MEMORY_MIB
)
for name in "${required[@]}"; do
  [[ -n "${!name:-}" ]] || die "$name is required for capacity validation"
done

[[ "$CI_FLEET_GITHUB_URL" == https://github.com/* ]] || die 'CI_FLEET_GITHUB_URL must use https://github.com'
[[ "$CI_FLEET_SCALE_SET_NAME" == *"$CI_FLEET_INSTANCE"* ]] || die 'scale-set name must retain the stable fleet instance ID'
[[ ",$CI_FLEET_LABELS," != *,self-hosted,* ]] || die 'shared routing labels must not include self-hosted'
[[ "$CI_FLEET_RUNNER_GROUP" != Default ]] || die 'post-pilot capacity requires the existing non-default runner group'
[[ "$CI_FLEET_MIN_RUNNERS" == 0 ]] || die 'CI_FLEET_MIN_RUNNERS must be 0'
[[ "$CI_FLEET_MAX_RUNNERS" =~ ^[1-9][0-9]{0,3}$ ]] || die 'configured MAX must be a positive integer'
configured_max=$((10#$CI_FLEET_MAX_RUNNERS))
[[ "$CI_FLEET_RUNNER_CPUS" =~ ^[1-9][0-9]{0,5}$ ]] || die 'per-runner CPU limit must be a positive integer'
runner_cpus=$((10#$CI_FLEET_RUNNER_CPUS))
[[ "$CI_FLEET_RUNNER_MEMORY_MIB" =~ ^[1-9][0-9]{0,9}$ ]] || die 'per-runner memory limit must be a positive integer MiB value'
runner_memory_mib=$((10#$CI_FLEET_RUNNER_MEMORY_MIB))

if [[ "$phase" == pre-change ]]; then
  ((target_max > configured_max)) || die 'pre-change target MAX must be greater than configured MAX'
else
  ((configured_max == target_max)) || die 'post-change configured MAX does not match requested target'
fi

for command in docker stat df awk getconf free dmesg grep wc tr; do
  command -v "$command" >/dev/null || die "$command is unavailable"
done
docker info >/dev/null 2>&1 || die 'Docker daemon is unreachable'
docker compose version >/dev/null 2>&1 || die 'Docker Compose plugin is unavailable'

socket_gid=$(stat -c '%g' /var/run/docker.sock 2>/dev/null || true)
[[ -n "$socket_gid" && "$socket_gid" == "$CI_FLEET_DOCKER_GID" ]] || die 'Docker socket group does not match CI_FLEET_DOCKER_GID'

secret=$CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE
[[ -f "$secret" ]] || die 'GitHub App PEM file is missing'
secret_mode=$(stat -c '%a' "$secret" 2>/dev/null || true)
secret_owner=$(stat -c '%u' "$secret" 2>/dev/null || true)
[[ "$secret_mode" == 600 && "$secret_owner" == 0 ]] || die 'GitHub App PEM must be root-owned mode 0600'

warn_percent=${CI_FLEET_DISK_WARN_PERCENT:-80}
[[ "$warn_percent" =~ ^[1-9][0-9]?$|^100$ ]] || die 'CI_FLEET_DISK_WARN_PERCENT must be an integer from 1 through 100'
docker_root=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)
[[ "$docker_root" == /* ]] || die 'Docker root directory could not be determined'
disk_used=$(df -P "$docker_root" 2>/dev/null | awk 'NR==2 {gsub(/%/, "", $5); print $5}')
[[ "$disk_used" =~ ^[0-9]{1,3}$ ]] || die 'Docker filesystem utilization could not be determined'
((disk_used < warn_percent)) || die "Docker filesystem must remain below ${warn_percent}%"

controller=${CI_FLEET_CONTROLLER_CONTAINER:-ci-fleet-controller-1}
controller_state=$(docker inspect --format '{{.State.Status}}' "$controller" 2>/dev/null || true)
[[ "$controller_state" == running ]] || die 'controller must be running and healthy'
controller_oom=$(docker inspect --format '{{.State.OOMKilled}}' "$controller" 2>/dev/null || true)
[[ "$controller_oom" == false ]] || die 'controller OOM evidence is present'

controller_env=$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$controller" 2>/dev/null) || die 'effective controller configuration could not be inspected'
effective_value() {
  local wanted=$1 line key value='' count=0
  while IFS= read -r line; do
    key=${line%%=*}
    if [[ "$key" == "$wanted" ]]; then
      value=${line#*=}
      count=$((count + 1))
    fi
  done <<<"$controller_env"
  ((count == 1)) || die "effective controller $wanted must occur exactly once"
  printf '%s' "$value"
}

stable=(
  CI_FLEET_GITHUB_URL
  CI_FLEET_SCALE_SET_NAME
  CI_FLEET_LABELS
  CI_FLEET_RUNNER_GROUP
  CI_FLEET_INSTANCE
  CI_FLEET_MIN_RUNNERS
  CI_FLEET_RUNNER_CPUS
  CI_FLEET_RUNNER_MEMORY_MIB
  CI_FLEET_DOCKER_GID
)
for name in "${stable[@]}"; do
  effective=$(effective_value "$name")
  [[ "$effective" == "${!name}" ]] || die "effective controller $name does not match configured state"
done
effective_min=$(effective_value CI_FLEET_MIN_RUNNERS)
effective_max=$(effective_value CI_FLEET_MAX_RUNNERS)
[[ "$effective_min" == 0 ]] || die 'effective controller MIN must be 0'
[[ "$effective_max" =~ ^[1-9][0-9]{0,3}$ ]] || die 'effective controller MAX must be a positive integer'
effective_max=$((10#$effective_max))
if [[ "$phase" == pre-change ]]; then
  ((effective_max == configured_max)) || die 'effective controller MAX does not match configured pre-change MAX'
else
  ((effective_max == target_max)) || die 'effective controller MAX does not match requested target'
fi

managed_count=$(docker ps -aq \
  --filter label=io.randomdevelopment.ci-fleet.managed=true \
  --filter label=io.randomdevelopment.ci-fleet.kind=runner \
  --filter "label=io.randomdevelopment.ci-fleet.instance=$CI_FLEET_INSTANCE" | wc -l | tr -d ' ')
[[ "$managed_count" == 0 ]] || die 'active managed runner or runner residue exists'
job_container_count=$(docker ps -aq --filter label=ci-fleet.repository | wc -l | tr -d ' ')
[[ "$job_container_count" == 0 ]] || die 'active fleet job container or job residue exists'
job_volume_count=$(docker volume ls -q --filter label=ci-fleet.repository | wc -l | tr -d ' ')
[[ "$job_volume_count" == 0 ]] || die 'fleet job volume residue exists'
job_network_count=$(docker network ls -q --filter label=ci-fleet.repository | wc -l | tr -d ' ')
[[ "$job_network_count" == 0 ]] || die 'fleet job network residue exists'

while IFS= read -r running_name; do
  [[ -z "$running_name" || "$running_name" == "$controller" ]] || die 'unrelated running Docker workload exists'
done < <(docker ps --format '{{.Names}}')

if ! oom_evidence=$(dmesg --level=err,crit,alert,emerg 2>/dev/null); then
  die 'kernel OOM evidence could not be inspected'
fi
if grep -Eiq 'out of memory|oom-killer|killed process' <<<"$oom_evidence"; then
  die 'OOM evidence exists for the current boot'
fi

logical_cpus=$(getconf _NPROCESSORS_ONLN 2>/dev/null || true)
[[ "$logical_cpus" =~ ^[1-9][0-9]{0,5}$ ]] || die 'logical CPU count could not be determined'
logical_cpus=$((10#$logical_cpus))
read -r total_memory_mib available_memory_mib < <(free -m | awk '/^Mem:/ {print $2, $7}')
[[ "$total_memory_mib" =~ ^[1-9][0-9]{0,9}$ ]] || die 'total memory could not be determined'
[[ "$available_memory_mib" =~ ^[1-9][0-9]{0,9}$ ]] || die 'available memory could not be determined'
total_memory_mib=$((10#$total_memory_mib))
available_memory_mib=$((10#$available_memory_mib))

# Fixed reservations match deploy/compose.yaml and the documented capacity policy.
controller_cpu_millicpus=1000
controller_memory_mib=512
docker_cpu_reserve_millicpus=1000
docker_memory_reserve_mib=1024
total_cpu_millicpus=$((logical_cpus * 1000))
os_cpu_reserve_millicpus=$(((total_cpu_millicpus * 15 + 99) / 100))
((os_cpu_reserve_millicpus >= 1000)) || os_cpu_reserve_millicpus=1000
os_memory_reserve_mib=$(((total_memory_mib * 20 + 99) / 100))
((os_memory_reserve_mib >= 2048)) || os_memory_reserve_mib=2048
required_cpu_millicpus=$((target_max * runner_cpus * 1000 + controller_cpu_millicpus + docker_cpu_reserve_millicpus + os_cpu_reserve_millicpus))
required_memory_mib=$((target_max * runner_memory_mib + controller_memory_mib + docker_memory_reserve_mib + os_memory_reserve_mib))
required_available_memory_mib=$((target_max * runner_memory_mib + controller_memory_mib + docker_memory_reserve_mib))

((required_cpu_millicpus <= total_cpu_millicpus)) || die 'CPU capacity is insufficient for the requested target and reserves'
((required_memory_mib <= total_memory_mib)) || die 'memory capacity is insufficient for the requested target and reserves'
((required_available_memory_mib <= available_memory_mib)) || die 'available memory is insufficient for the requested target'

printf 'OK configured_max=%d requested_target_max=%d\n' "$configured_max" "$target_max"
printf 'OK effective_min=%d effective_max=%d\n' "$effective_min" "$effective_max"
printf 'OK cpu_budget_millicpus=%d/%d os_reserve_millicpus=%d docker_reserve_millicpus=%d\n' \
  "$required_cpu_millicpus" "$total_cpu_millicpus" "$os_cpu_reserve_millicpus" "$docker_cpu_reserve_millicpus"
printf 'OK memory_budget_mib=%d/%d available_required_mib=%d available_mib=%d os_reserve_mib=%d docker_reserve_mib=%d\n' \
  "$required_memory_mib" "$total_memory_mib" "$required_available_memory_mib" "$available_memory_mib" "$os_memory_reserve_mib" "$docker_memory_reserve_mib"
printf 'OK docker_filesystem_used_percent=%d warning_percent=%d\n' "$disk_used" "$warn_percent"
printf 'CAPACITY_PREFLIGHT_OK phase=%s target_max=%d configured_max=%d effective_max=%d\n' \
  "$phase" "$target_max" "$configured_max" "$effective_max"
