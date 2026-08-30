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
original_args=("$@")

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

validate_trusted_path() {
  local name=$1 path=$2 kind=$3 allow_absent=${4:-false} create=${5:-false}
  python3 - "$path" "$kind" "$allow_absent" "$create" "$testing" "${CI_FLEET_ROOT_PREFIX:-}" <<'PY' ||
import errno, os, stat, sys

path, kind, allow_absent, create, testing, root_prefix = sys.argv[1:]
expected_owner = os.getuid() if testing == "1" else 0
anchor = os.path.realpath(root_prefix) if testing == "1" and root_prefix and kind != "tempdir" else "/"
try:
    if not path.startswith("/") or os.path.normpath(path) != path or os.path.realpath(path) != path:
        raise ValueError
    if os.path.commonpath((anchor, path)) != anchor:
        raise ValueError
    metadata = os.lstat(path)
    exists = True
except FileNotFoundError:
    if allow_absent != "true":
        raise SystemExit(1)
    exists = False
except (OSError, ValueError):
    raise SystemExit(1)

if exists:
    if kind == "executable" and (not stat.S_ISREG(metadata.st_mode) or not os.access(path, os.X_OK)):
        raise SystemExit(1)
    if kind == "regular" and not stat.S_ISREG(metadata.st_mode):
        raise SystemExit(1)
    if kind == "checkpoint" and (not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700):
        raise SystemExit(1)
    if kind == "tempdir" and not stat.S_ISDIR(metadata.st_mode):
        raise SystemExit(1)
current = path if kind == "tempdir" else os.path.dirname(path)

while True:
    try:
        metadata = os.lstat(current)
    except OSError:
        raise SystemExit(1)
    sticky_temp = (
        kind == "tempdir"
        and metadata.st_uid in (0, expected_owner)
        and metadata.st_mode & stat.S_ISVTX
    )
    trusted_owner = metadata.st_uid == expected_owner or (kind == "tempdir" and metadata.st_uid == 0)
    if not stat.S_ISDIR(metadata.st_mode) or (
        (not trusted_owner or metadata.st_mode & 0o022) and not sticky_temp
    ):
        raise SystemExit(1)
    if current == anchor:
        break
    current = os.path.dirname(current)

if exists and kind != "tempdir":
    metadata = os.lstat(path)
    if metadata.st_uid != expected_owner or metadata.st_mode & 0o022:
        raise SystemExit(1)
elif create == "true":
    parent, leaf = os.path.split(path)
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.mkdir(leaf, 0o700, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
        fd = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            metadata = os.fstat(fd)
            if metadata.st_uid != expected_owner or stat.S_IMODE(metadata.st_mode) != 0o700:
                raise OSError
        finally:
            os.close(fd)
    except OSError:
        raise SystemExit(1)
    finally:
        os.close(parent_fd)
PY
    die "$name must be a trusted root-owned path"
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
validate_trusted_path 'rendered env' "$env_file" regular

# Serialize with installer mutations before reading the rendered candidate.
lock_file=${CI_FLEET_INSTALLER_LOCK:-${CI_FLEET_ROOT_PREFIX:-}/run/ci-fleet-installer.lock}
if [[ -n ${CI_FLEET_INSTALLER_LOCK_FD:-} ]]; then
  [[ "$CI_FLEET_INSTALLER_LOCK_FD" == 9 ]] || die 'inherited installer lock must use file descriptor 9'
  [[ $(readlink -f /proc/self/fd/9 2>/dev/null || true) == $(readlink -m "$lock_file") ]] || die 'inherited installer lock does not match the configured lock file'
  flock -n 9 || die 'inherited installer lock is unavailable'
else
  validate_trusted_path CI_FLEET_INSTALLER_LOCK "$lock_file" regular true
  exec python3 - "$lock_file" "$testing" "$repo_root/scripts/apply-docker-network-policy.sh" "${original_args[@]}" <<'PY'
import errno, os, stat, sys

path, testing, script, *args = sys.argv[1:]
expected_owner = os.getuid() if testing == "1" else 0
flags = os.O_RDWR | os.O_NOFOLLOW
try:
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        os.fchmod(fd, 0o600)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise
        fd = os.open(path, flags)
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != expected_owner or metadata.st_mode & 0o022:
        raise OSError
    os.dup2(fd, 9)
    if fd != 9:
        os.close(fd)
    os.set_inheritable(9, True)
    os.environ["CI_FLEET_INSTALLER_LOCK_FD"] = "9"
    os.execv(script, [script, *args])
except OSError:
    print("ERROR: CI_FLEET_INSTALLER_LOCK must remain a trusted root-owned regular file", file=sys.stderr)
    raise SystemExit(2)
PY
fi

temp_parent=${CI_FLEET_TEMP_DIR:-/tmp}
validate_trusted_path 'transaction temp directory' "$temp_parent" tempdir
work_dir=$(mktemp -d "$temp_parent/.ci-fleet-apply.XXXXXX")
chmod 0700 "$work_dir"
trap 'rm -rf "$work_dir"' EXIT
install -m 0600 -- "$env_file" "$work_dir/ci-fleet.env"
env_file=$work_dir/ci-fleet.env

# --- No-op when no network policy is rendered ---
removing=false
if ! grep -q '^CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT=' "$env_file"; then
  if [[ -z "$checkpoint_dir" ]]; then
    printf 'NETWORK_POLICY_NOOP\n'
    exit 0
  fi
  removing=true
else
  [[ -n "$checkpoint_dir" ]] || die '--checkpoint is required when a network policy is configured'
fi

# Validate the locked candidate snapshot before mutation.
if [[ "$removing" == false ]]; then
  desired_pools_json=$(python3 - "$env_file" "$repo_root/scripts" 2>/dev/null <<'PY'
import json, sys
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
  local name=$1 path=$2
  [[ -n "$path" ]] || die "$name is required when network policy is managed"
  validate_trusted_path "$name" "$path" executable
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
validate_trusted_path CI_FLEET_DOCKER_DAEMON_CONFIG "$daemon_config" regular true
validate_trusted_path 'checkpoint directory' "$checkpoint_dir" checkpoint true
checkpoint_state=$checkpoint_dir/docker-network-policy.json
[[ "$removing" != true || ! -L "$checkpoint_state" ]] || die 'network-policy checkpoint state is invalid'
if [[ "$removing" == true && ( ! -e "$checkpoint_dir" || ! -e "$checkpoint_state" ) ]]; then
  printf 'NETWORK_POLICY_NOOP\n'
  exit 0
fi
validate_command CI_FLEET_DOCKER_DRAIN_COMMAND "$drain_command"
validate_command CI_FLEET_DOCKER_RESTART_COMMAND "$restart_command"
validate_command CI_FLEET_CONTROLLER_RESUME_COMMAND "$resume_command"
validate_command CI_FLEET_HEALTH_CHECK_COMMAND "$health_command"
validate_command CI_FLEET_DOCKER_NETWORK_PROBE "$probe_command"

checkpoint_path_is_pinned() {
  [[ ! -L "$checkpoint_dir" && $(readlink -f /proc/self/fd/8) == "$checkpoint_dir" ]]
}
if [[ -d "$checkpoint_dir" ]]; then
  exec 8<"$checkpoint_dir"
  checkpoint_path_is_pinned || die 'checkpoint directory must remain a trusted root-owned path'
  [[ $(readlink -m /proc/self/fd/8/daemon.json) != "$daemon_config" ]] ||
    die 'daemon config and checkpoint entry must be separate paths'
  exec 8<&-
fi

validate_trusted_path 'checkpoint directory' "$checkpoint_dir" checkpoint true true
exec 8<"$checkpoint_dir"
checkpoint_path_is_pinned || die 'checkpoint directory must remain a trusted root-owned path'
state_file=/proc/self/fd/8/docker-network-policy.json

# Snapshot the installer's authoritative pre-transaction environment while
# holding its lock. Rollback must not validate against the rejected candidate.
installed_env=${CI_FLEET_ROOT_PREFIX:-}/etc/ci-fleet/ci-fleet.env
validate_trusted_path 'installed rendered env' "$installed_env" regular
[[ -r "$installed_env" ]] || die "installed rendered env must be readable: $installed_env"
prior_env=$work_dir/prior-ci-fleet.env
install -m 0600 -- "$installed_env" "$prior_env"

drain_failure=
new_marker=false
controller_resumed=false
transaction_recovery=
# shellcheck disable=SC2317 # invoked indirectly by the EXIT trap below
resume_after_failed_drain() {
  local status=$? resume_failed=0 health_failed=0
  trap - EXIT INT TERM
  run_command "$resume_command" --env "$prior_env" || resume_failed=1
  ((resume_failed != 0)) || run_health "$prior_env" || health_failed=1
  if ((resume_failed == 0 && health_failed == 0)) && [[ "$new_marker" == true ]]; then
    clear_managed_marker || resume_failed=1
  fi
  [[ -z "$transaction_recovery" ]] || rm -rf "$transaction_recovery"
  rm -rf "$work_dir"
  if ((resume_failed)); then
    printf 'ERROR: %s; controller resume command failed\n' "$drain_failure" >&2
  elif ((health_failed)); then
    printf 'ERROR: %s; prior controller health check failed\n' "$drain_failure" >&2
  else
    printf 'ERROR: %s\n' "$drain_failure" >&2
  fi
  exit "$status"
}

drain_controller() {
  local status=0
  drain_failure=$1
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap resume_after_failed_drain EXIT
  run_command "$drain_command" || status=$?
  ((status == 0)) || exit "$status"
}

checkpoint_owner=0
[[ "$testing" != 1 ]] || checkpoint_owner=$(id -u)
if [[ -e "$state_file" || -L "$state_file" ]]; then
  [[ -f "$state_file" && ! -L "$state_file" && $(stat -c %u "$state_file") == "$checkpoint_owner" && $(stat -c %a "$state_file") == 600 ]] ||
    die "checkpoint files must be owned by root with mode 0600: $checkpoint_dir/docker-network-policy.json"
fi
[[ ! -e "/proc/self/fd/8/daemon.json" && ! -L "/proc/self/fd/8/daemon.json" ]] ||
  die 'network-policy checkpoint state is invalid'

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

daemon_changed_since_snapshot() {
  local snapshot=$1 was_present=$2
  if [[ "$was_present" == true ]]; then
    if cmp -s "$snapshot" "$daemon_config"; then
      return 1
    fi
    return 0
  else
    [[ -e "$daemon_config" || -L "$daemon_config" ]]
  fi
}

set_verified_generation() {
  checkpoint_path_is_pinned || return 1
  python3 - "$state_file" "${1:-}" "${2:-}" <<'PY'
import json, os, sys, tempfile
path, generation, action = sys.argv[1:]
state = json.load(open(path, encoding="utf-8"))
if action == "clear-removal":
    state.pop("phase", None)
    state.pop("removal_managed_default_address_pools", None)
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

clear_managed_marker() {
  checkpoint_path_is_pinned || return 1
  python3 - "$state_file" <<'PY'
import os, sys
path = sys.argv[1]
os.unlink(path)
directory_fd = os.open(os.path.dirname(path), os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
}

set_removal_pending() {
  checkpoint_path_is_pinned || return 1
  python3 - "$state_file" "$1" <<'PY'
import json, os, sys, tempfile
path, managed_path = sys.argv[1:]
state = json.load(open(path, encoding="utf-8"))
if state.get("phase") != "removal-pending":
    managed = json.load(open(managed_path, encoding="utf-8"))
    state["phase"] = "removal-pending"
    state["removal_managed_default_address_pools"] = managed.get("default-address-pools")
state["verified_generation"] = None
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

persist_recovery() {
  local daemon_source=${1:-} env_source=${2:-$prior_env}
  checkpoint_path_is_pinned || return 1
  python3 - "$state_file" "$daemon_source" "$env_source" <<'PY'
import os, shutil, sys, tempfile

state_path, daemon_source, env_source = sys.argv[1:]
parent = os.path.dirname(state_path)
staged = tempfile.mkdtemp(prefix=".recovery.", dir=parent)
try:
    os.chmod(staged, 0o700)
    sources = [(env_source, "prior-ci-fleet.env")]
    if daemon_source:
        sources.insert(0, (daemon_source, "daemon.json.before"))
    for source, name in sources:
        target = os.path.join(staged, name)
        with open(source, "rb") as source_handle, open(target, "xb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle)
            os.fchmod(target_handle.fileno(), 0o600)
            target_handle.flush()
            os.fsync(target_handle.fileno())
    staged_fd = os.open(staged, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(staged_fd)
    finally:
        os.close(staged_fd)
    final = os.path.join(parent, "recovery." + os.path.basename(staged).removeprefix(".recovery."))
    os.replace(staged, final)
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    print(os.path.realpath(final))
except BaseException:
    shutil.rmtree(staged, ignore_errors=True)
    raise
PY
}

if [[ "$removing" == true ]]; then
  [[ -f "$state_file" && ! -L "$state_file" ]] || die 'network-policy checkpoint state is invalid'
  mapfile -t managed_state < <(python3 - "$state_file" "$repo_root/scripts" 2>/dev/null <<'PY'
import json, re, sys
state = json.load(open(sys.argv[1], encoding="utf-8"))
sys.path.insert(0, sys.argv[2])
from desired_state import validate_docker_address_pools
required = {"managed", "prior_default_address_pools", "prior_default_address_pools_present", "prior_mode", "prior_present"}
pending = {"phase", "removal_managed_default_address_pools"}
schemas = (required, required | {"verified_generation"}, required | pending, required | pending | {"verified_generation"})
if set(state) not in schemas or state["managed"] is not True:
    raise SystemExit(1)
is_pending = "phase" in state
if is_pending and (
    state["phase"] != "removal-pending"
    or state["removal_managed_default_address_pools"] is not None
    and not isinstance(state["removal_managed_default_address_pools"], list)
):
    raise SystemExit(1)
if is_pending and state["removal_managed_default_address_pools"] is not None:
    validate_docker_address_pools(
        state["removal_managed_default_address_pools"],
        path="checkpoint removal managed default address pools",
    )
generation = state.get("verified_generation")
if generation is not None and (not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{64}", generation)):
    raise SystemExit(1)
if not isinstance(state["prior_present"], bool):
    raise SystemExit(1)
if not isinstance(state["prior_default_address_pools_present"], bool):
    raise SystemExit(1)
if state["prior_default_address_pools_present"]:
    if not state["prior_present"]:
        raise SystemExit(1)
    validate_docker_address_pools(
        state["prior_default_address_pools"],
        path="checkpoint prior default address pools",
    )
elif state["prior_default_address_pools"] is not None:
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
print("true" if is_pending else "false")
print("true" if "verified_generation" in state else "false")
PY
  ) || die 'network-policy checkpoint state is invalid'
  [[ ${#managed_state[@]} == 5 ]] || die 'network-policy checkpoint state is invalid'
  prior_present=${managed_state[0]}
  prior_mode=${managed_state[1]}
  prior_verified_generation=${managed_state[2]}
  removal_pending=${managed_state[3]}
  has_verified_generation=${managed_state[4]}
  if [[ "$prior_present" == true ]]; then
    [[ "$prior_mode" =~ ^[0-7]{3,4}$ ]] || die 'network-policy checkpoint state is invalid'
  fi
  if [[ "$prior_present" == false && -z "$prior_verified_generation" && "$removal_pending" == false && "$has_verified_generation" == true && ! -e "$daemon_config" && ! -L "$daemon_config" ]]; then
    run_command "$resume_command" --env "$env_file" || die 'controller resume command failed while recovering interrupted network-policy apply'
    run_health "$env_file" || die 'health check failed while recovering interrupted network-policy apply'
    clear_managed_marker || die 'failed to clear interrupted network-policy marker'
    rm -rf "$work_dir"
    printf 'NETWORK_POLICY_REMOVED\n'
    exit 0
  fi
  [[ -f "$daemon_config" ]] || die 'managed daemon.json is missing before policy removal'

  managed_daemon=$work_dir/daemon.json.managed
  removal_daemon=$work_dir/daemon.json.removal
  python3 - "$daemon_config" "$state_file" "$managed_daemon" "$removal_daemon" 2>/dev/null <<'PY' || {
import json, sys
daemon_path, state_path, managed_path, removal_path = sys.argv[1:]
try:
    current = json.load(open(daemon_path, encoding="utf-8"))
    state = json.load(open(state_path, encoding="utf-8"))
    if not isinstance(current, dict):
        raise ValueError
    if state.get("phase") == "removal-pending":
        managed_pools = state["removal_managed_default_address_pools"]
    else:
        managed_pools = current.get("default-address-pools")
except (OSError, json.JSONDecodeError, KeyError, ValueError):
    raise SystemExit(1)
managed = dict(current)
if managed_pools is None:
    managed.pop("default-address-pools", None)
else:
    managed["default-address-pools"] = managed_pools
with open(managed_path, "w", encoding="utf-8") as handle:
    json.dump(managed, handle, indent=2, sort_keys=True)
    handle.write("\n")
if state["prior_default_address_pools_present"]:
    current["default-address-pools"] = state["prior_default_address_pools"]
else:
    current.pop("default-address-pools", None)
with open(removal_path, "w", encoding="utf-8") as handle:
    json.dump(current, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
    rm -rf "$work_dir"
    die 'managed daemon.json is not a valid JSON object'
  }
  managed_mode=$(stat -c %a "$daemon_config")
  managed_gid=$(stat -c %g "$daemon_config")
  managed_snapshot=$work_dir/daemon.json.before
  cp -- "$daemon_config" "$managed_snapshot" || { rm -rf "$work_dir"; die 'failed to snapshot managed daemon.json'; }

  drain_controller 'drain command failed before network-policy removal'
  if daemon_changed_since_snapshot "$managed_snapshot" true; then
    drain_failure='daemon.json changed during network-policy removal'
    exit 2
  fi
  checkpoint_path_is_pinned || die 'checkpoint directory changed during network-policy removal'

  removal_failure='network-policy removal interrupted'
  # shellcheck disable=SC2317 # invoked indirectly by the EXIT trap below
  rollback_removal() {
    local failed=0 rollback_daemon=$work_dir/daemon.json.rollback
    if [[ "$controller_resumed" == true ]]; then
      run_command "$drain_command" || failed=1
    fi
    if ((failed == 0)); then
      python3 - "$daemon_config" "$managed_daemon" "$rollback_daemon" <<'PY' || failed=1
import json, os, sys
current_path, managed_path, output_path = sys.argv[1:]
try:
    current = json.load(open(current_path, encoding="utf-8")) if os.path.exists(current_path) else {}
    managed = json.load(open(managed_path, encoding="utf-8"))
    if not isinstance(current, dict) or not isinstance(managed, dict):
        raise ValueError
except (OSError, json.JSONDecodeError, ValueError):
    raise SystemExit(1)
if "default-address-pools" in managed:
    current["default-address-pools"] = managed["default-address-pools"]
else:
    current.pop("default-address-pools", None)
with open(output_path, "w", encoding="utf-8") as handle:
    json.dump(current, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
    fi
    if ((failed == 0)); then
      atomic_replace_daemon "$rollback_daemon" "$managed_mode" "$managed_gid" || failed=1
      cmp -s "$rollback_daemon" "$daemon_config" || failed=1
    fi
    if ((failed == 0)); then
      run_command "$restart_command" "$daemon_dir" || failed=1
    fi
    if ((failed == 0)); then
      run_command "$resume_command" --env "$prior_env" || failed=1
    fi
    if ((failed == 0)); then
      run_health "$prior_env" || failed=1
    fi
    if ((failed == 0)); then
      set_verified_generation "$prior_verified_generation" clear-removal || failed=1
    fi
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
      recovery_path=$(persist_recovery "$managed_snapshot" "$prior_env") || {
        printf 'ERROR: %s; rollback verification failed; failed to persist recovery data\n' "$removal_failure" >&2
        exit "$status"
      }
      rm -rf "$work_dir"
      printf 'ERROR: %s; rollback verification failed; recovery data retained at %s\n' "$removal_failure" "$recovery_path" >&2
    fi
    exit "$status"
  }
  trap removal_on_exit EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  set_removal_pending "$managed_daemon" || { removal_failure='failed to persist network-policy removal state'; exit 2; }

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
  run_command "$probe_command" || { removal_failure='capacity probe failed after network-policy removal'; exit 2; }
  run_command "$resume_command" --env "$env_file" || { removal_failure='controller resume command failed during network-policy removal'; exit 2; }
  controller_resumed=true
  run_health "$env_file" || { removal_failure='health check failed after network-policy removal'; exit 2; }

  # Marker deletion commits removal. Ignore catchable signals across the atomic
  # unlink so failure still rolls back and success cannot leave a stale marker.
  trap '' INT TERM
  checkpoint_path_is_pinned || { removal_failure='checkpoint directory changed during network-policy removal'; exit 2; }
  python3 - "$daemon_config" "$removal_daemon" 2>/dev/null <<'PY' || { removal_failure='daemon.json changed after network-policy removal verification'; exit 2; }
import json, os, sys
current = json.load(open(sys.argv[1], encoding="utf-8")) if os.path.exists(sys.argv[1]) else {}
expected = json.load(open(sys.argv[2], encoding="utf-8"))
missing = object()
if (
    not isinstance(current, dict)
    or not isinstance(expected, dict)
    or current.get("default-address-pools", missing) != expected.get("default-address-pools", missing)
):
    raise SystemExit(1)
PY
  if ! clear_managed_marker; then
    removal_failure='failed to clear network-policy managed marker'
    exit 2
  fi
  trap - EXIT INT TERM
  rm -rf "$work_dir"
  printf 'NETWORK_POLICY_REMOVED\n'
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

managed_before=false
prior_verified_generation=
apply_removal_pending=false
if [[ -e "$state_file" ]]; then
  [[ -f "$state_file" && ! -L "$state_file" ]] || { rm -rf "$work_dir"; die 'network-policy checkpoint state is invalid'; }
  apply_checkpoint_state=$(
    python3 - "$state_file" "$repo_root/scripts" 2>/dev/null <<'PY'
import json, re, sys
state = json.load(open(sys.argv[1], encoding="utf-8"))
sys.path.insert(0, sys.argv[2])
from desired_state import validate_docker_address_pools
required = {"managed", "prior_default_address_pools", "prior_default_address_pools_present", "prior_mode", "prior_present"}
pending = {"phase", "removal_managed_default_address_pools"}
if set(state) not in (required, required | {"verified_generation"}, required | pending, required | pending | {"verified_generation"}) or state["managed"] is not True:
    raise SystemExit(1)
is_pending = "phase" in state
if is_pending and state["phase"] != "removal-pending":
    raise SystemExit(1)
if is_pending and state["removal_managed_default_address_pools"] is not None:
    validate_docker_address_pools(
        state["removal_managed_default_address_pools"],
        path="checkpoint removal managed default address pools",
    )
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
print(f"{generation or ''}|{'true' if is_pending else 'false'}")
PY
  ) || { rm -rf "$work_dir"; die 'network-policy checkpoint state is invalid'; }
  IFS='|' read -r prior_verified_generation apply_removal_pending <<<"$apply_checkpoint_state"
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
  local rollback_daemon=$work_dir/daemon.json.apply-rollback rollback_action
  rollback_action=$(python3 - "$daemon_config" "$backup_dir/$backup_name" "$had_prior" "$rollback_daemon" "$staging_daemon" <<'PY'
import json, os, sys
current_path, prior_path, had_prior, output_path, staged_path = sys.argv[1:]
try:
    current = json.load(open(current_path, encoding="utf-8")) if os.path.exists(current_path) else {}
    prior = json.load(open(prior_path, encoding="utf-8")) if had_prior == "true" else {}
    staged = json.load(open(staged_path, encoding="utf-8"))
    if not isinstance(current, dict) or not isinstance(prior, dict) or not isinstance(staged, dict):
        raise ValueError
except (OSError, json.JSONDecodeError, ValueError):
    raise SystemExit(1)
current_unrelated = {key: value for key, value in current.items() if key != "default-address-pools"}
staged_unrelated = {key: value for key, value in staged.items() if key != "default-address-pools"}
if current_unrelated == staged_unrelated:
    print("exact" if had_prior == "true" else "remove")
    raise SystemExit
if "default-address-pools" in prior:
    current["default-address-pools"] = prior["default-address-pools"]
else:
    current.pop("default-address-pools", None)
if not current and had_prior != "true":
    print("remove")
else:
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(current, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print("replace")
PY
  ) || return 1
  if [[ "$rollback_action" == exact ]]; then
    atomic_replace_daemon "$backup_dir/$backup_name" "$daemon_mode" "$daemon_gid"
  elif [[ "$rollback_action" == replace ]]; then
    atomic_replace_daemon "$rollback_daemon" "$daemon_mode" "$daemon_gid"
  else
    rm -f "$daemon_config"
    python3 - "$daemon_dir" <<'PY'
import os, sys
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(fd)
finally:
    os.close(fd)
PY
  fi
}

# Record the original host state before the first drain. The marker lets a
# later no-policy reconciliation resume a controller interrupted by the drain.
if [[ "$managed_before" == false ]]; then
  python3 - "$state_file" "$had_prior" "$daemon_mode" "$backup_dir/$backup_name" "$repo_root/scripts" 2>/dev/null <<'PY' || { transaction_failure='failed to record network-policy checkpoint state'; exit 2; }
import json, os, sys, tempfile
path = sys.argv[1]
prior = json.load(open(sys.argv[4], encoding="utf-8")) if sys.argv[2] == "true" else {}
prior_key_present = "default-address-pools" in prior
if prior_key_present:
    sys.path.insert(0, sys.argv[5])
    from desired_state import validate_docker_address_pools
    validate_docker_address_pools(prior["default-address-pools"], path="existing daemon default address pools")
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
  new_marker=true
fi
if [[ "$managed_before" == true ]]; then
  transaction_recovery=$(persist_recovery "$backup_dir/$backup_name" "$prior_env") || die 'failed to persist network-policy transaction recovery'
fi

rollback_daemon() {
  local failed=0
  if [[ "$controller_resumed" == true ]]; then
    run_command "$drain_command" || failed=1
  fi
  if ((failed == 0)); then
    restore_daemon || failed=1
  fi
  if ((failed == 0)); then
    run_command "$restart_command" "$daemon_dir" || failed=1
  fi
  if ((failed == 0)); then
    run_command "$resume_command" --env "$prior_env" || failed=1
  fi
  if ((failed == 0)); then
    run_health "$prior_env" || failed=1
  fi
  if [[ "$managed_before" == true && "$failed" == 0 ]]; then
    set_verified_generation "$prior_verified_generation" || failed=1
  fi
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
    [[ -z "$transaction_recovery" ]] || rm -rf "$transaction_recovery"
    rm -rf "$work_dir"
    printf 'ERROR: %s; prior daemon.json restored\n' "$transaction_failure" >&2
  else
    if [[ -n "$transaction_recovery" ]]; then
      recovery_path=$transaction_recovery
    else
      recovery_daemon=
      [[ "$had_prior" != true ]] || recovery_daemon=$backup_dir/$backup_name
      recovery_path=$(persist_recovery "$recovery_daemon" "$prior_env") || {
        printf 'ERROR: %s; rollback verification failed; failed to persist recovery data\n' "$transaction_failure" >&2
        exit "$status"
      }
    fi
    rm -rf "$work_dir"
    printf 'ERROR: %s; rollback verification failed; recovery data retained at %s\n' "$transaction_failure" "$recovery_path" >&2
  fi
  exit "$status"
}

# --- Drain after local validation/checkpointing, before mutation or restart ---
drain_controller 'drain command failed before network-policy apply'
if daemon_changed_since_snapshot "$backup_dir/$backup_name" "$had_prior"; then
  drain_failure='daemon.json changed during network-policy apply'
  exit 2
fi
checkpoint_path_is_pinned || die 'checkpoint directory changed during network-policy apply'
trap rollback_on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

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
controller_resumed=true

# Health verification
if ! run_health "$env_file"; then
  fail_after_apply "health check failed after network-policy restart"
fi

python3 - "$daemon_config" "$staging_daemon" 2>/dev/null <<'PY' || fail_after_apply "daemon.json changed after network-policy verification"
import json, sys
current = json.load(open(sys.argv[1], encoding="utf-8"))
staged = json.load(open(sys.argv[2], encoding="utf-8"))
if not isinstance(current, dict) or current.get("default-address-pools") != staged.get("default-address-pools"):
    raise SystemExit(1)
PY
verified_generation=$(file_generation "$daemon_config") || fail_after_apply "failed to identify verified daemon.json generation"
verified_action=
[[ "$apply_removal_pending" != true ]] || verified_action=clear-removal
set_verified_generation "$verified_generation" "$verified_action" || fail_after_apply "failed to record verified daemon.json generation"

# --- Success ---
trap - EXIT INT TERM
[[ -z "$transaction_recovery" ]] || rm -rf "$transaction_recovery"
rm -rf "$work_dir"
printf 'NETWORK_POLICY_APPLIED daemon_config=%s\n' "$daemon_config"
