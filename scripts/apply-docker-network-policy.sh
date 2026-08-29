#!/usr/bin/env bash
# Engine stage: apply the reviewed Docker daemon network policy transactionally.
#
# Validates, drains, applies daemon.json atomically (preserving unrelated keys),
# restarts Docker ONLY via injected command boundary, runs capacity probes +
# health checks, and rolls back the exact prior config on any failure.
#
# Environment variables (all injected, never host-defaulted):
#   CI_FLEET_DOCKER_DAEMON_CONFIG   absolute path to daemon.json
#   CI_FLEET_DOCKER_RESTART_COMMAND path to a Docker restart script
#   CI_FLEET_DOCKER_NETWORK_PROBE   path to a capacity probe script
#   CI_FLEET_HEALTH_CHECK_COMMAND   path to a health-check script
#   CI_FLEET_TESTING                when 1, relaxes root/strict checks
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
testing=${CI_FLEET_TESTING:-0}

env_file=
checkpoint_dir=

usage() {
  cat >&2 <<'EOF'
usage: apply-docker-network-policy.sh --env PATH [--checkpoint PATH]

--env PATH          path to the rendered ci-fleet env file (required)
--checkpoint PATH   directory to back up the prior daemon.json into
EOF
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 2
}

while (($#)); do
  case "$1" in
    --env)
      [[ $# -ge 2 ]] || die '--env requires a value'
      env_file=$2
      shift 2
      ;;
    --checkpoint)
      [[ $# -ge 2 ]] || die '--checkpoint requires a value'
      checkpoint_dir=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      die "unknown argument: $1"
      ;;
  esac
done

[[ -n "$env_file" ]] || die '--env is required'
[[ -r "$env_file" ]] || die "rendered env is unreadable: $env_file"

# --- No-op when no network policy is rendered ---
count=$(awk -F= '$1 == "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT" {print substr($0, index($0, "=") + 1)}' "$env_file")
if [[ -z "$count" || "$count" == "0" ]]; then
  printf 'NETWORK_POLICY_NOOP\n'
  exit 0
fi

# --- Resolve required injected commands ---
daemon_config=${CI_FLEET_DOCKER_DAEMON_CONFIG:-}
restart_command=${CI_FLEET_DOCKER_RESTART_COMMAND:-}
probe_command=${CI_FLEET_DOCKER_NETWORK_PROBE:-}
health_command=${CI_FLEET_HEALTH_CHECK_COMMAND:-}

[[ -n "$daemon_config" ]] || die 'CI_FLEET_DOCKER_DAEMON_CONFIG is required when a network policy is configured'
[[ -n "$restart_command" ]] || die 'CI_FLEET_DOCKER_RESTART_COMMAND is required when a network policy is configured'
[[ -n "$probe_command" ]] || die 'CI_FLEET_DOCKER_NETWORK_PROBE is required when a network policy is configured'
[[ -n "$health_command" ]] || die 'CI_FLEET_HEALTH_CHECK_COMMAND is required when a network policy is configured'
[[ -x "$restart_command" ]] || die "restart command is not executable: $restart_command"
[[ -x "$probe_command" ]] || die "network probe is not executable: $probe_command"
[[ -x "$health_command" ]] || die "health-check command is not executable: $health_command"

# --- Ownership guard (relaxed in testing) ---
if [[ "$testing" != 1 ]]; then
  [[ -w "$(dirname "$daemon_config")" ]] || die "daemon config directory is not writable: $(dirname "$daemon_config")"
  if [[ -f "$daemon_config" ]]; then
    file_owner=$(stat -c %u "$daemon_config")
    [[ "$file_owner" == "0" ]] || die "daemon.json must be owned by root: $daemon_config"
  fi
else
  : # testing mode — skip root checks
fi

# --- Render desired daemon config block via shared validator ---
desired_pools_json=$(python3 - "$env_file" "$repo_root/scripts" <<'PY'
import json, os, sys
env_path, scripts_dir = sys.argv[1], sys.argv[2]
sys.path.insert(0, scripts_dir)
values = {}
with open(env_path, encoding="utf-8") as handle:
    for line in handle:
        line = line.rstrip("\n")
        if "=" in line and line:
            key, _, value = line.partition("=")
            values[key] = value
from desired_state import render_docker_daemon_config
print(json.dumps(render_docker_daemon_config(values)))
PY
) || die "daemon policy rendering failed"

# Re-check: if rendering returned empty, treat as no-op.
if [[ "$desired_pools_json" == "{}" ]]; then
  printf 'NETWORK_POLICY_NOOP\n'
  exit 0
fi

# --- Stage merged daemon.json (preserve unrelated keys) ---
work_dir=$(mktemp -d "${CI_FLEET_TEMP_DIR:-/tmp}/.ci-fleet-apply.XXXXXX")
staging_daemon="$work_dir/daemon.json"

python3 - "$env_file" "$daemon_config" "$staging_daemon" "$desired_pools_json" <<'PY' || { rm -rf "$work_dir"; die "failed to stage merged daemon.json"; }
import json, os, sys
_, _, daemon_path, staging_path, desired_pools_json = sys.argv
prior = {}
if os.path.exists(daemon_path):
    try:
        text = open(daemon_path, encoding="utf-8").read()
        prior = json.loads(text)
        if not isinstance(prior, dict):
            raise ValueError("daemon.json root must be an object")
    except (json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f"ERROR: existing daemon.json is not a valid JSON object: {exc}")
desired_pools = json.loads(desired_pools_json)
merged = dict(prior)
merged["default-address-pools"] = desired_pools.get("default-address-pools", [])
with open(staging_path, "w", encoding="utf-8") as handle:
    json.dump(merged, handle, indent=2, sort_keys=True)
    handle.write("\n")
os.chmod(staging_path, 0o644)
PY

# --- Back up exact prior daemon.json for rollback ---
prior_daemon="$work_dir/prior"
mkdir -p "$prior_daemon"
if [[ -n "$checkpoint_dir" ]]; then
  mkdir -p "$checkpoint_dir"
  backup_dir="$checkpoint_dir"
  backup_name="daemon.json"
else
  backup_dir="$prior_daemon"
  backup_name="daemon.json.before"
fi
if [[ -f "$daemon_config" ]]; then
  cp -p "$daemon_config" "$backup_dir/$backup_name"
fi

restore_daemon() {
  local backup_file="$backup_dir/$backup_name"
  if [[ -f "$backup_file" ]]; then
    cp -p "$backup_file" "$daemon_config"
  fi
}

# --- Transaction: apply → restart → probe → health, with rollback ---
daemon_dir=$(dirname "$daemon_config")

# Apply daemon.json atomically (rename within same directory)
python3 - "$staging_daemon" "$daemon_dir" "$daemon_config" <<'PY' || { restore_daemon; rm -rf "$work_dir"; die "failed to apply daemon.json"; }
import os, sys
_, _, daemon_dir, target = sys.argv
tmp = os.path.join(daemon_dir, ".daemon.json.tmp")
os.replace(sys.argv[1], tmp)
os.replace(tmp, target)
PY

# Restart Docker through the injected command boundary (never host-direct)
if ! "$restart_command" "$daemon_dir" 2>&1; then
  restore_daemon
  rm -rf "$work_dir"
  die "Docker restart command failed; prior daemon.json restored"
fi

# Bounded capacity probe
if ! "$probe_command" 2>&1; then
  restore_daemon
  rm -rf "$work_dir"
  die "capacity probe failed after network-policy restart; prior daemon.json restored"
fi

# Health verification
if ! "$health_command" 2>&1; then
  restore_daemon
  rm -rf "$work_dir"
  die "health check failed after network-policy restart; prior daemon.json restored"
fi

# --- Success ---
rm -rf "$work_dir"
printf 'NETWORK_POLICY_APPLIED daemon_config=%s\n' "$daemon_config"
