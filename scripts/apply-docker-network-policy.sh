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
#   CI_FLEET_CONTROLLER_RESUME_COMMAND path to a controller resume/start script
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
[[ -f "$env_file" && ! -L "$env_file" && -r "$env_file" ]] || die "rendered env must be a readable regular file: $env_file"
env_owner=$(stat -c %u "$env_file")
expected_env_owner=0
[[ "$testing" != 1 ]] || expected_env_owner=$(id -u)
[[ "$env_owner" == "$expected_env_owner" ]] || die "rendered env has an untrusted owner: $env_file"
env_mode=$(stat -c %a "$env_file")
(( (8#$env_mode & 8#022) == 0 )) || die "rendered env must not be group/world writable: $env_file"
work_dir=$(mktemp -d "${CI_FLEET_TEMP_DIR:-/tmp}/.ci-fleet-apply.XXXXXX")
chmod 0700 "$work_dir"
trap 'rm -rf "$work_dir"' EXIT
install -m 0600 -- "$env_file" "$work_dir/ci-fleet.env"
env_file=$work_dir/ci-fleet.env

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
resume_command=${CI_FLEET_CONTROLLER_RESUME_COMMAND:-}
probe_command=${CI_FLEET_DOCKER_NETWORK_PROBE:-}
health_command=${CI_FLEET_HEALTH_CHECK_COMMAND:-}
command_timeout=${CI_FLEET_COMMAND_TIMEOUT_SECONDS:-300}

validate_command() {
  local name=$1 path=$2 canonical
  [[ -n "$path" ]] || die "$name is required when network policy is managed"
  [[ "$path" == /* && -f "$path" && ! -L "$path" && -x "$path" ]] || die "$name must be an absolute canonical regular executable path"
  canonical=$(readlink -f -- "$path") || die "$name must be an absolute canonical regular executable path"
  [[ "$path" == "$canonical" ]] || die "$name must be an absolute canonical regular executable path"
}

run_command() {
  timeout --kill-after=5 "$command_timeout" "$@" >/dev/null 2>&1
}

run_health() {
  local status=0
  CI_FLEET_HEALTH_SUPPRESS_DELIVERY=1 run_command "$health_command" --env "$1" || status=$?
  ((status < 2))
}

[[ "$command_timeout" =~ ^[1-9][0-9]*$ ]] || die 'CI_FLEET_COMMAND_TIMEOUT_SECONDS must be a positive integer'
[[ -n "$daemon_config" ]] || die 'CI_FLEET_DOCKER_DAEMON_CONFIG is required when a network policy is configured'
validate_command CI_FLEET_DOCKER_DRAIN_COMMAND "$drain_command"
validate_command CI_FLEET_DOCKER_RESTART_COMMAND "$restart_command"
validate_command CI_FLEET_CONTROLLER_RESUME_COMMAND "$resume_command"
validate_command CI_FLEET_HEALTH_CHECK_COMMAND "$health_command"
if [[ "$removing" == false ]]; then
  validate_command CI_FLEET_DOCKER_NETWORK_PROBE "$probe_command"
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

# Snapshot the installer's authoritative pre-transaction environment while
# holding its lock. Rollback must not validate against the rejected candidate.
installed_env=${CI_FLEET_ROOT_PREFIX:-}/etc/ci-fleet/ci-fleet.env
[[ -f "$installed_env" && ! -L "$installed_env" && -r "$installed_env" ]] || die "installed rendered env must be a readable regular file: $installed_env"
[[ $(stat -c %u "$installed_env") == "$expected_env_owner" ]] || die "installed rendered env has an untrusted owner: $installed_env"
installed_env_mode=$(stat -c %a "$installed_env")
(( (8#$installed_env_mode & 8#022) == 0 )) || die "installed rendered env must not be group/world writable: $installed_env"
prior_env=$work_dir/prior-ci-fleet.env
install -m 0600 -- "$installed_env" "$prior_env"

checkpoint_owner=0
[[ "$testing" != 1 ]] || checkpoint_owner=$(id -u)
if [[ -e "$checkpoint_dir" || -L "$checkpoint_dir" ]]; then
  [[ -d "$checkpoint_dir" && ! -L "$checkpoint_dir" && $(stat -c %u "$checkpoint_dir") == "$checkpoint_owner" && $(stat -c %a "$checkpoint_dir") == 700 ]] ||
    die "checkpoint directory must be owned by root with mode 0700: $checkpoint_dir"
  checkpoint_file=$checkpoint_dir/docker-network-policy.json
  if [[ -e "$checkpoint_file" || -L "$checkpoint_file" ]]; then
    [[ -f "$checkpoint_file" && ! -L "$checkpoint_file" && $(stat -c %u "$checkpoint_file") == "$checkpoint_owner" && $(stat -c %a "$checkpoint_file") == 600 ]] ||
      die "checkpoint files must be owned by root with mode 0600: $checkpoint_file"
  fi
  [[ ! -e "$checkpoint_dir/daemon.json" && ! -L "$checkpoint_dir/daemon.json" ]] ||
    die 'network-policy checkpoint state is invalid'
fi

[[ ! -L "$daemon_config" ]] || die "daemon.json must not be a symlink: $daemon_config"

# --- Ownership guard (relaxed in testing) ---
daemon_mode=644
daemon_gid=0
[[ "$testing" != 1 ]] || daemon_gid=$(id -g)
if [[ -f "$daemon_config" ]]; then
  daemon_mode=$(stat -c %a "$daemon_config")
  daemon_gid=$(stat -c %g "$daemon_config")
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
daemon_dir=$(dirname "$daemon_config")

atomic_replace_daemon() {
  python3 - "$1" "$daemon_dir" "$daemon_config" "$2" "$3" <<'PY'
import os, shutil, sys, tempfile
_, source, daemon_dir, target, mode, gid = sys.argv
fd, tmp = tempfile.mkstemp(prefix=".daemon.json.", dir=daemon_dir)
try:
    with open(source, "rb") as source_handle, os.fdopen(fd, "wb") as staged:
        shutil.copyfileobj(source_handle, staged)
        os.fchmod(staged.fileno(), int(mode, 8))
        os.fchown(staged.fileno(), -1, int(gid))
        staged.flush()
        os.fsync(staged.fileno())
    os.replace(tmp, target)
    directory_fd = os.open(daemon_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    if os.path.exists(tmp):
        os.unlink(tmp)
PY
}

file_generation() {
  python3 - "$1" <<'PY'
import hashlib, sys
with open(sys.argv[1], "rb") as handle:
    print(hashlib.file_digest(handle, "sha256").hexdigest())
PY
}

set_verified_generation() {
  python3 - "$state_file" "${1:-}" <<'PY'
import json, os, sys, tempfile
path, generation = sys.argv[1:]
state = json.load(open(path, encoding="utf-8"))
state["verified_generation"] = generation or None
fd, tmp = tempfile.mkstemp(prefix=".docker-network-policy.", dir=os.path.dirname(path), text=True)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
        os.fchmod(handle.fileno(), 0o600)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    directory_fd = os.open(os.path.dirname(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    if os.path.exists(tmp):
        os.unlink(tmp)
PY
}

if [[ "$removing" == true ]]; then
  [[ -f "$state_file" && ! -L "$state_file" ]] || die 'network-policy checkpoint state is invalid'
  mapfile -t managed_state < <(python3 - "$state_file" <<'PY'
import json, re, sys
state = json.load(open(sys.argv[1], encoding="utf-8"))
required = {"managed", "prior_default_address_pools", "prior_default_address_pools_present", "prior_mode", "prior_present"}
if set(state) not in (required, required | {"verified_generation"}) or state["managed"] is not True:
    raise SystemExit(1)
generation = state.get("verified_generation")
if generation is not None and (not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{64}", generation)):
    raise SystemExit(1)
if not isinstance(state["prior_present"], bool):
    raise SystemExit(1)
if not isinstance(state["prior_default_address_pools_present"], bool):
    raise SystemExit(1)
if state["prior_default_address_pools_present"] and not state["prior_present"]:
    raise SystemExit(1)
if not state["prior_default_address_pools_present"] and state["prior_default_address_pools"] is not None:
    raise SystemExit(1)
mode = state["prior_mode"]
if state["prior_present"]:
    if not isinstance(mode, str) or not mode.isdigit():
        raise SystemExit(1)
elif mode is not None:
    raise SystemExit(1)
print("true" if state["prior_present"] else "false")
print(mode or "")
print(generation or "")
PY
  ) || die 'network-policy checkpoint state is invalid'
  [[ ${#managed_state[@]} == 3 ]] || die 'network-policy checkpoint state is invalid'
  prior_present=${managed_state[0]}
  prior_mode=${managed_state[1]}
  prior_verified_generation=${managed_state[2]}
  if [[ "$prior_present" == true ]]; then
    [[ "$prior_mode" =~ ^[0-7]{3,4}$ ]] || die 'network-policy checkpoint state is invalid'
  fi
  [[ -f "$daemon_config" ]] || die 'managed daemon.json is missing before policy removal'

  managed_daemon=$work_dir/daemon.json.managed
  cp -p "$daemon_config" "$managed_daemon"
  managed_mode=$(stat -c %a "$daemon_config")
  managed_gid=$(stat -c %g "$daemon_config")

  if ! run_command "$drain_command"; then
    rm -rf "$work_dir"
    die 'drain command failed before network-policy removal'
  fi

  removal_failure='network-policy removal interrupted'
  # shellcheck disable=SC2317 # invoked indirectly by the EXIT trap below
  rollback_removal() {
    local failed=0 rollback_daemon=$work_dir/daemon.json.rollback
    python3 - "$daemon_config" "$managed_daemon" "$rollback_daemon" <<'PY' || failed=1
import json, os, sys
current_path, managed_path, output_path = sys.argv[1:]
try:
    current = json.load(open(current_path, encoding="utf-8")) if os.path.exists(current_path) else {}
    managed = json.load(open(managed_path, encoding="utf-8"))
    if not isinstance(current, dict) or not isinstance(managed, dict) or "default-address-pools" not in managed:
        raise ValueError
except (OSError, json.JSONDecodeError, ValueError):
    raise SystemExit(1)
current["default-address-pools"] = managed["default-address-pools"]
with open(output_path, "w", encoding="utf-8") as handle:
    json.dump(current, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
    if ((failed == 0)); then
      atomic_replace_daemon "$rollback_daemon" "$managed_mode" "$managed_gid" || failed=1
      cmp -s "$rollback_daemon" "$daemon_config" || failed=1
    fi
    run_command "$restart_command" "$daemon_dir" || failed=1
    run_command "$resume_command" --env "$prior_env" || failed=1
    run_health "$prior_env" || failed=1
    set_verified_generation "$prior_verified_generation" || failed=1
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
  set_verified_generation "" || { removal_failure='failed to mark network-policy verification pending'; exit 2; }

  removal_daemon=$work_dir/daemon.json.removal
  python3 - "$daemon_config" "$state_file" "$removal_daemon" <<'PY' || { removal_failure='failed to stage network-policy removal'; exit 2; }
import json, sys
daemon_path, state_path, output_path = sys.argv[1:]
try:
    current = json.load(open(daemon_path, encoding="utf-8"))
    state = json.load(open(state_path, encoding="utf-8"))
    if not isinstance(current, dict):
        raise ValueError
except (OSError, json.JSONDecodeError, ValueError):
    raise SystemExit(1)
if state["prior_default_address_pools_present"]:
    current["default-address-pools"] = state["prior_default_address_pools"]
else:
    current.pop("default-address-pools", None)
with open(output_path, "w", encoding="utf-8") as handle:
    json.dump(current, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
  if [[ "$prior_present" == false ]] && python3 - "$removal_daemon" <<'PY'
import json, sys
raise SystemExit(bool(json.load(open(sys.argv[1], encoding="utf-8"))))
PY
  then
    python3 - "$daemon_config" "$daemon_dir" <<'PY' || { removal_failure='failed to remove empty daemon config'; exit 2; }
import os, sys
try:
    os.unlink(sys.argv[1])
except FileNotFoundError:
    pass
directory_fd = os.open(sys.argv[2], os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
  else
    atomic_replace_daemon "$removal_daemon" "$managed_mode" "$managed_gid" || { removal_failure='failed to install prior network-policy key state'; exit 2; }
    cmp -s "$removal_daemon" "$daemon_config" || { removal_failure='failed to verify prior network-policy key state'; exit 2; }
  fi
  run_command "$restart_command" "$daemon_dir" || { removal_failure='Docker restart command failed during network-policy removal'; exit 2; }
  run_command "$resume_command" --env "$env_file" || { removal_failure='controller resume command failed during network-policy removal'; exit 2; }
  run_health "$env_file" || { removal_failure='health check failed after network-policy removal'; exit 2; }

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
desired_pools_json=$(python3 - "$env_file" "$repo_root/scripts" 2>/dev/null <<'PY'
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
prior_verified_generation=
if [[ -e "$state_file" ]]; then
  [[ -f "$state_file" && ! -L "$state_file" ]] || { rm -rf "$work_dir"; die 'network-policy checkpoint state is invalid'; }
  prior_verified_generation=$(
    python3 - "$state_file" <<'PY'
import json, re, sys
state = json.load(open(sys.argv[1], encoding="utf-8"))
required = {"managed", "prior_default_address_pools", "prior_default_address_pools_present", "prior_mode", "prior_present"}
if set(state) not in (required, required | {"verified_generation"}) or state["managed"] is not True:
    raise SystemExit(1)
generation = state.get("verified_generation")
if generation is not None and (not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{64}", generation)):
    raise SystemExit(1)
if not isinstance(state["prior_present"], bool):
    raise SystemExit(1)
if not isinstance(state["prior_default_address_pools_present"], bool):
    raise SystemExit(1)
if state["prior_default_address_pools_present"] and not state["prior_present"]:
    raise SystemExit(1)
if not state["prior_default_address_pools_present"] and state["prior_default_address_pools"] is not None:
    raise SystemExit(1)
if state["prior_present"]:
    if not isinstance(state["prior_mode"], str) or not re.fullmatch(r"[0-7]{3,4}", state["prior_mode"]):
        raise SystemExit(1)
elif state["prior_mode"] is not None:
    raise SystemExit(1)
print(generation or "")
PY
  ) || { rm -rf "$work_dir"; die 'network-policy checkpoint state is invalid'; }
  managed_before=true
fi

daemon_matches=false
if [[ -f "$daemon_config" ]] && python3 - "$daemon_config" "$staging_daemon" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as current, open(sys.argv[2], encoding="utf-8") as staged:
    raise SystemExit(json.load(current) != json.load(staged))
PY
then
  daemon_matches=true
  current_generation=$(file_generation "$daemon_config") || { rm -rf "$work_dir"; die 'failed to identify daemon.json generation'; }
  if [[ -n "$prior_verified_generation" && "$current_generation" == "$prior_verified_generation" ]]; then
    rm -rf "$work_dir"
    printf 'NETWORK_POLICY_NO_CHANGE\n'
    exit 0
  fi
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

restore_daemon() {
  if [[ "$had_prior" == true ]]; then
    atomic_replace_daemon "$backup_dir/$backup_name" "$daemon_mode" "$daemon_gid"
  else
    rm -f "$daemon_config"
  fi
}

rollback_daemon() {
  local failed=0
  restore_daemon || failed=1
  run_command "$restart_command" "$daemon_dir" || failed=1
  run_command "$resume_command" --env "$prior_env" || failed=1
  run_health "$prior_env" || failed=1
  if [[ "$managed_before" == true ]]; then set_verified_generation "$prior_verified_generation" || failed=1; fi
  return "$failed"
}

transaction_failure='network-policy apply interrupted'
rollback_on_exit() {
  local status=$?
  trap - EXIT INT TERM
  ((status != 0)) || status=1
  if rollback_daemon >/dev/null 2>&1; then
    if [[ "$managed_before" == false ]]; then
      rm -f "$state_file"
    fi
    rm -rf "$work_dir"
    printf 'ERROR: %s; prior daemon.json restored\n' "$transaction_failure" >&2
  else
    printf 'ERROR: %s; rollback verification failed; recovery data retained at %s\n' "$transaction_failure" "$work_dir" >&2
  fi
  exit "$status"
}

# --- Drain after local validation/checkpointing, before mutation or restart ---
if ! run_command "$drain_command"; then
  rm -rf "$work_dir"
  die "drain command failed before network-policy apply"
fi
trap 'exit 130' INT
trap 'exit 143' TERM
trap rollback_on_exit EXIT

# Record the original host state before the first managed mutation. Re-applying
# policy keeps this baseline and uses the temp copy above for transaction rollback.
if [[ "$managed_before" == false ]]; then
  install -d -m 0700 "$checkpoint_dir"
  python3 - "$state_file" "$had_prior" "$daemon_mode" "$backup_dir/$backup_name" <<'PY' || { transaction_failure='failed to record network-policy checkpoint state'; exit 2; }
import json, os, sys, tempfile
path = sys.argv[1]
prior = json.load(open(sys.argv[4], encoding="utf-8")) if sys.argv[2] == "true" else {}
prior_key_present = "default-address-pools" in prior
state = {
    "managed": True,
    "prior_default_address_pools": prior.get("default-address-pools") if prior_key_present else None,
    "prior_default_address_pools_present": prior_key_present,
    "prior_mode": sys.argv[3] if sys.argv[2] == "true" else None,
    "prior_present": sys.argv[2] == "true",
    "verified_generation": None,
}
fd, tmp = tempfile.mkstemp(prefix=".docker-network-policy.", dir=os.path.dirname(path), text=True)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
        os.fchmod(handle.fileno(), 0o600)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    directory_fd = os.open(os.path.dirname(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    if os.path.exists(tmp):
        os.unlink(tmp)
PY
fi
if [[ "$managed_before" == true ]]; then
  set_verified_generation "" || { transaction_failure='failed to mark network-policy verification pending'; exit 2; }
fi

fail_after_apply() {
  transaction_failure=$1
  exit 2
}

# --- Transaction: apply → restart → probe → health, with rollback ---
# Apply daemon.json atomically (rename within same directory).
if [[ "$daemon_matches" == false ]]; then
  atomic_replace_daemon "$staging_daemon" "$daemon_mode" "$daemon_gid" || { transaction_failure='failed to apply daemon.json'; exit 2; }
fi

# Restart Docker through the injected command boundary (never host-direct).
if ! run_command "$restart_command" "$daemon_dir"; then
  fail_after_apply "Docker restart command failed"
fi

# Bounded capacity probe
if ! run_command "$probe_command"; then
  fail_after_apply "capacity probe failed after network-policy restart"
fi

# Resume the drained controller before health verification.
if ! run_command "$resume_command" --env "$env_file"; then
  fail_after_apply "controller resume command failed after network-policy restart"
fi

# Health verification
if ! run_health "$env_file"; then
  fail_after_apply "health check failed after network-policy restart"
fi

verified_generation=$(file_generation "$daemon_config") || fail_after_apply "failed to identify verified daemon.json generation"
set_verified_generation "$verified_generation" || fail_after_apply "failed to record verified daemon.json generation"

# --- Success ---
trap - EXIT INT TERM
rm -rf "$work_dir"
printf 'NETWORK_POLICY_APPLIED daemon_config=%s\n' "$daemon_config"
