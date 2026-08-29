#!/usr/bin/env bash
# Engine stage: apply the reviewed Docker daemon network policy transactionally.
#
# Validates, drains, applies daemon.json atomically (preserving unrelated keys),
# restarts Docker ONLY via injected command boundary, runs capacity probes +
# health checks, and rolls back the exact prior config on any failure.
#
# Environment variables (all injected, never host-defaulted):
#   CI_FLEET_DOCKER_DAEMON_CONFIG   absolute path to daemon.json
#   CI_FLEET_DOCKER_DRAIN_COMMAND   path to a host drain script (runs before mutation)
#   CI_FLEET_DOCKER_RESTART_COMMAND path to a Docker restart script
#   CI_FLEET_DOCKER_NETWORK_PROBE   path to a capacity probe script
#   CI_FLEET_HEALTH_CHECK_COMMAND   path to a health-check script
#   CI_FLEET_COMMAND_TIMEOUT_SECONDS command timeout in seconds (default 300)
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
removing=false
if [[ -z "$count" || "$count" == "0" ]]; then
  state_file=${checkpoint_dir:+$checkpoint_dir/docker-network-policy.json}
  if [[ -z "$state_file" || ! -e "$state_file" ]]; then
    printf 'NETWORK_POLICY_NOOP\n'
    exit 0
  fi
  removing=true
else
  [[ -n "$checkpoint_dir" ]] || die '--checkpoint is required when a network policy is configured'
fi

# --- Resolve required injected commands ---
daemon_config=${CI_FLEET_DOCKER_DAEMON_CONFIG:-}
drain_command=${CI_FLEET_DOCKER_DRAIN_COMMAND:-}
restart_command=${CI_FLEET_DOCKER_RESTART_COMMAND:-}
probe_command=${CI_FLEET_DOCKER_NETWORK_PROBE:-}
health_command=${CI_FLEET_HEALTH_CHECK_COMMAND:-}
command_timeout=${CI_FLEET_COMMAND_TIMEOUT_SECONDS:-300}

[[ "$command_timeout" =~ ^[1-9][0-9]*$ ]] || die 'CI_FLEET_COMMAND_TIMEOUT_SECONDS must be a positive integer'
[[ -n "$daemon_config" ]] || die 'CI_FLEET_DOCKER_DAEMON_CONFIG is required when a network policy is configured'
[[ -n "$drain_command" ]] || die 'CI_FLEET_DOCKER_DRAIN_COMMAND is required when a network policy is configured'
[[ -n "$restart_command" ]] || die 'CI_FLEET_DOCKER_RESTART_COMMAND is required when network policy is managed'
[[ -n "$health_command" ]] || die 'CI_FLEET_HEALTH_CHECK_COMMAND is required when network policy is managed'
[[ -x "$restart_command" ]] || die "restart command is not executable: $restart_command"
[[ -x "$drain_command" ]] || die "drain command is not executable: $drain_command"
[[ -x "$health_command" ]] || die "health-check command is not executable: $health_command"
if [[ "$removing" == false ]]; then
  [[ -n "$probe_command" ]] || die 'CI_FLEET_DOCKER_NETWORK_PROBE is required when a network policy is configured'
  [[ -x "$probe_command" ]] || die "network probe is not executable: $probe_command"
fi

# Serialize with installer mutations using the installer's host-local lock.
lock_file=${CI_FLEET_INSTALLER_LOCK:-${CI_FLEET_ROOT_PREFIX:-}/run/ci-fleet-installer.lock}
if [[ -n ${CI_FLEET_INSTALLER_LOCK_FD:-} ]]; then
  [[ "$CI_FLEET_INSTALLER_LOCK_FD" == 9 ]] || die 'inherited installer lock must use file descriptor 9'
  [[ $(readlink -f /proc/self/fd/9 2>/dev/null || true) == $(readlink -m "$lock_file") ]] || die 'inherited installer lock does not match the configured lock file'
  flock -n 9 || die 'inherited installer lock is unavailable'
else
  install -d -m 0755 "$(dirname "$lock_file")"
  exec 9>"$lock_file"
  flock -n 9 || die 'another ci-fleet installer or drift check is already running'
fi

[[ ! -L "$daemon_config" ]] || die "daemon.json must not be a symlink: $daemon_config"

# --- Ownership guard (relaxed in testing) ---
daemon_mode=644
if [[ -f "$daemon_config" ]]; then
  daemon_mode=$(stat -c %a "$daemon_config")
fi
if [[ "$testing" != 1 ]]; then
  [[ -w "$(dirname "$daemon_config")" ]] || die "daemon config directory is not writable: $(dirname "$daemon_config")"
  if [[ -f "$daemon_config" ]]; then
    file_owner=$(stat -c %u "$daemon_config")
    [[ "$file_owner" == "0" ]] || die "daemon.json must be owned by root: $daemon_config"
  fi
else
  : # testing mode — skip root checks
fi

if [[ "$removing" == true ]]; then
  [[ -f "$state_file" && ! -L "$state_file" ]] || die 'network-policy checkpoint state is invalid'
  mapfile -t managed_state < <(python3 - "$state_file" <<'PY'
import json, sys
state = json.load(open(sys.argv[1], encoding="utf-8"))
if set(state) != {"managed", "prior_mode", "prior_present"} or state["managed"] is not True:
    raise SystemExit(1)
if not isinstance(state["prior_present"], bool):
    raise SystemExit(1)
mode = state["prior_mode"]
if state["prior_present"]:
    if not isinstance(mode, str) or not mode.isdigit():
        raise SystemExit(1)
elif mode is not None:
    raise SystemExit(1)
print("true" if state["prior_present"] else "false")
print(mode or "")
PY
  ) || die 'network-policy checkpoint state is invalid'
  [[ ${#managed_state[@]} == 2 ]] || die 'network-policy checkpoint state is invalid'
  prior_present=${managed_state[0]}
  prior_mode=${managed_state[1]}
  if [[ "$prior_present" == true ]]; then
    [[ "$prior_mode" =~ ^[0-7]{3,4}$ ]] || die 'network-policy checkpoint state is invalid'
    [[ -f "$checkpoint_dir/daemon.json" && ! -L "$checkpoint_dir/daemon.json" ]] || die 'network-policy checkpoint backup is invalid'
  fi
  [[ -f "$daemon_config" ]] || die 'managed daemon.json is missing before policy removal'

  work_dir=$(mktemp -d "${CI_FLEET_TEMP_DIR:-/tmp}/.ci-fleet-remove.XXXXXX")
  managed_daemon=$work_dir/daemon.json.managed
  cp -p "$daemon_config" "$managed_daemon"
  managed_mode=$(stat -c %a "$daemon_config")
  daemon_dir=$(dirname "$daemon_config")

  if ! timeout "$command_timeout" "$drain_command" >/dev/null 2>&1; then
    rm -rf "$work_dir"
    die 'drain command failed before network-policy removal'
  fi

  removal_failure='network-policy removal interrupted'
  # shellcheck disable=SC2317 # invoked indirectly by the EXIT trap below
  rollback_removal() {
    local failed=0
    cp -p "$managed_daemon" "$daemon_config" || failed=1
    chmod "$managed_mode" "$daemon_config" || failed=1
    cmp -s "$managed_daemon" "$daemon_config" || failed=1
    timeout "$command_timeout" "$restart_command" "$daemon_dir" >/dev/null 2>&1 || failed=1
    return "$failed"
  }
  # shellcheck disable=SC2317 # invoked indirectly by the EXIT trap below
  removal_on_exit() {
    local status=$?
    trap - EXIT INT TERM
    ((status != 0)) || status=1
    if rollback_removal; then
      rm -rf "$work_dir"
      printf 'ERROR: %s; managed daemon.json restored\n' "$removal_failure" >&2
    else
      printf 'ERROR: %s; rollback verification failed; recovery data retained at %s\n' "$removal_failure" "$work_dir" >&2
    fi
    exit "$status"
  }
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap removal_on_exit EXIT

  if [[ "$prior_present" == true ]]; then
    cp -p "$checkpoint_dir/daemon.json" "$daemon_config" || { removal_failure='failed to restore prior daemon.json'; exit 2; }
    chmod "$prior_mode" "$daemon_config" || { removal_failure='failed to restore prior daemon.json mode'; exit 2; }
    cmp -s "$checkpoint_dir/daemon.json" "$daemon_config" || { removal_failure='failed to verify prior daemon.json'; exit 2; }
  else
    rm -f "$daemon_config" || { removal_failure='failed to restore absent daemon.json'; exit 2; }
    [[ ! -e "$daemon_config" ]] || { removal_failure='failed to verify absent daemon.json'; exit 2; }
  fi
  timeout "$command_timeout" "$restart_command" "$daemon_dir" >/dev/null 2>&1 || { removal_failure='Docker restart command failed during network-policy removal'; exit 2; }
  timeout "$command_timeout" "$health_command" --env "$env_file" >/dev/null 2>&1 || { removal_failure='health check failed after network-policy removal'; exit 2; }

  # Marker deletion commits removal. Ignore catchable signals across the atomic
  # unlink so failure still rolls back and success cannot leave a stale marker.
  trap '' INT TERM
  if ! rm -f "$state_file"; then
    removal_failure='failed to clear network-policy managed marker'
    exit 2
  fi
  trap - EXIT INT TERM
  rm -rf "$work_dir"
  printf 'NETWORK_POLICY_REMOVED\n'
  exit 0
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

state_file=$checkpoint_dir/docker-network-policy.json
managed_before=false
if [[ -e "$state_file" ]]; then
  [[ -f "$state_file" && ! -L "$state_file" ]] || { rm -rf "$work_dir"; die 'network-policy checkpoint state is invalid'; }
  python3 - "$state_file" "$checkpoint_dir/daemon.json" <<'PY' || { rm -rf "$work_dir"; die 'network-policy checkpoint backup is invalid'; }
import json, os, re, sys
state = json.load(open(sys.argv[1], encoding="utf-8"))
if set(state) != {"managed", "prior_mode", "prior_present"} or state["managed"] is not True:
    raise SystemExit(1)
if not isinstance(state["prior_present"], bool):
    raise SystemExit(1)
if state["prior_present"]:
    if not isinstance(state["prior_mode"], str) or not re.fullmatch(r"[0-7]{3,4}", state["prior_mode"]):
        raise SystemExit(1)
    if not os.path.isfile(sys.argv[2]) or os.path.islink(sys.argv[2]):
        raise SystemExit(1)
elif state["prior_mode"] is not None or os.path.exists(sys.argv[2]):
    raise SystemExit(1)
PY
  managed_before=true
fi

if [[ -f "$daemon_config" ]] && python3 - "$daemon_config" "$staging_daemon" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as current, open(sys.argv[2], encoding="utf-8") as staged:
    raise SystemExit(json.load(current) != json.load(staged))
PY
then
  rm -rf "$work_dir"
  printf 'NETWORK_POLICY_NO_CHANGE\n'
  exit 0
fi

# --- Back up exact current daemon.json for transactional rollback ---
prior_daemon="$work_dir/prior"
mkdir -p "$prior_daemon"
backup_dir=$prior_daemon
backup_name=daemon.json.before
had_prior=false
if [[ -f "$daemon_config" ]]; then
  had_prior=true
  cp -p "$daemon_config" "$backup_dir/$backup_name"
fi

daemon_dir=$(dirname "$daemon_config")

# --- Drain after local validation/checkpointing, before mutation or restart ---
if ! timeout "$command_timeout" "$drain_command" >/dev/null 2>&1; then
  rm -rf "$work_dir"
  die "drain command failed before network-policy apply"
fi

# Record the original host state before the first managed mutation. Re-applying
# policy keeps this baseline and uses the temp copy above for transaction rollback.
if [[ "$managed_before" == false ]]; then
  install -d -m 0700 "$checkpoint_dir"
  if [[ "$had_prior" == true ]]; then
    cp -p "$backup_dir/$backup_name" "$checkpoint_dir/daemon.json"
  fi
  python3 - "$state_file" "$had_prior" "$daemon_mode" <<'PY' || { rm -rf "$work_dir"; die 'failed to record network-policy checkpoint state'; }
import json, os, sys, tempfile
path = sys.argv[1]
state = {
    "managed": True,
    "prior_mode": sys.argv[3] if sys.argv[2] == "true" else None,
    "prior_present": sys.argv[2] == "true",
}
fd, tmp = tempfile.mkstemp(prefix=".docker-network-policy.", dir=os.path.dirname(path), text=True)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
finally:
    if os.path.exists(tmp):
        os.unlink(tmp)
PY
fi

restore_daemon() {
  if [[ "$had_prior" == true ]]; then
    cp -p "$backup_dir/$backup_name" "$daemon_config"
  else
    rm -f "$daemon_config"
  fi
}

# Rollback: restore prior config, restart through the boundary, run health check.
# Failure evidence is surfaced through exit code only — no CIDRs or secrets leaked.
rollback_daemon() {
  local failed=0
  restore_daemon || failed=1
  timeout "$command_timeout" "$restart_command" "$daemon_dir" >/dev/null 2>&1 || failed=1
  timeout "$command_timeout" "$health_command" --env "$env_file" >/dev/null 2>&1 || failed=1
  return "$failed"
}

transaction_failure='network-policy apply interrupted'
rollback_on_exit() {
  local status=$?
  trap - EXIT INT TERM
  ((status != 0)) || status=1
  if rollback_daemon >/dev/null 2>&1; then
    if [[ "$managed_before" == false ]]; then
      rm -f "$state_file" "$checkpoint_dir/daemon.json"
    fi
    rm -rf "$work_dir"
    printf 'ERROR: %s; prior daemon.json restored\n' "$transaction_failure" >&2
  else
    printf 'ERROR: %s; rollback verification failed; recovery data retained at %s\n' "$transaction_failure" "$work_dir" >&2
  fi
  exit "$status"
}
trap 'exit 130' INT
trap 'exit 143' TERM
trap rollback_on_exit EXIT

fail_after_apply() {
  transaction_failure=$1
  exit 2
}

# --- Transaction: apply → restart → probe → health, with rollback ---
# Apply daemon.json atomically (rename within same directory)
python3 - "$staging_daemon" "$daemon_dir" "$daemon_config" "$daemon_mode" <<'PY' || { transaction_failure='failed to apply daemon.json'; exit 2; }
import os, shutil, sys, tempfile
_, _, daemon_dir, target, daemon_mode = sys.argv
fd, tmp = tempfile.mkstemp(prefix=".daemon.json.", dir=daemon_dir)
try:
    with open(sys.argv[1], "rb") as source, os.fdopen(fd, "wb") as staged:
        shutil.copyfileobj(source, staged)
    os.chmod(tmp, int(daemon_mode, 8))
    os.replace(tmp, target)
finally:
    if os.path.exists(tmp):
        os.unlink(tmp)
PY

# Restart Docker through the injected command boundary (never host-direct).
if ! timeout "$command_timeout" "$restart_command" "$daemon_dir" >/dev/null 2>&1; then
  fail_after_apply "Docker restart command failed"
fi

# Bounded capacity probe
if ! timeout "$command_timeout" "$probe_command" >/dev/null 2>&1; then
  fail_after_apply "capacity probe failed after network-policy restart"
fi

# Health verification
if ! timeout "$command_timeout" "$health_command" --env "$env_file" >/dev/null 2>&1; then
  fail_after_apply "health check failed after network-policy restart"
fi

# --- Success ---
trap - EXIT INT TERM
rm -rf "$work_dir"
printf 'NETWORK_POLICY_APPLIED daemon_config=%s\n' "$daemon_config"
