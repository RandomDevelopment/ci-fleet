#!/usr/bin/env bash
set -Eeuo pipefail
set +x
export PYTHONDONTWRITEBYTECODE=1

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
mode=
config_repo=
engine_repository=RandomDevelopment/ci-fleet
config_identity_arg=
config_ref=
controller_id=
host_config_arg=
config_source_checkout=
policy_action=
root_prefix=${CI_FLEET_ROOT_PREFIX:-}
testing=${CI_FLEET_TESTING:-0}
transaction_active=false
checkpoint_dir=
staging_paths=()
captured_current_state=

usage() {
  cat >&2 <<'EOF'
usage:
  install-worker-controller.sh --check|--install|--adopt|--upgrade \
    --config-repo OWNER/REPOSITORY|PATH --ref FULL_COMMIT_SHA \
    --controller CONTROLLER_ID [--config-identity OWNER/REPOSITORY]

  install-worker-controller.sh --rollback
  install-worker-controller.sh --uninstall

Modes are mutually exclusive. Remote private repositories use the target host's
preconfigured read-only Git credentials; credentials are never accepted in URLs
or command-line arguments. Managed installs always use /etc/ci-fleet/host.env.
EOF
}

note() { printf '%s\n' "$*"; }
transaction_result_enabled() {
  [[ ${CI_FLEET_TRANSACTION_RESULT_FD:-} == 7 && -e /proc/self/fd/7 ]]
}
write_transaction_result() {
  local outcome=$1
  transaction_result_enabled || return 0
  printf '{"schema_version":1,"outcome":"%s"}\n' "$outcome" >&7
}
die() {
  local result=2 restored=false
  printf 'ERROR: %s\n' "$*" >&2
  trap - ERR
  trap '' TERM
  if [[ ${transaction_active:-false} == true ]] && declare -F restore_checkpoint >/dev/null; then
    if restore_checkpoint; then restored=true; fi
    transaction_active=false
    if transaction_result_enabled; then
      if [[ "$restored" == true ]]; then
        write_transaction_result rollback_verified
        result=20
      else
        write_transaction_result rollback_unverified
        result=21
      fi
    fi
  fi
  trap - ERR
  exit "$result"
}

while (($#)); do
  case "$1" in
    --check|--install|--adopt|--upgrade|--rollback|--uninstall)
      [[ -z "$mode" ]] || die 'select exactly one operating mode'
      mode=${1#--}
      shift
      ;;
    --policy-action)
      [[ -z "$mode" && $# -ge 2 ]] || die '--policy-action requires one exclusive action'
      mode=policy-action
      policy_action=$2
      shift 2
      ;;
    --config-repo)
      (($# >= 2)) || die '--config-repo requires a value'
      config_repo=$2
      shift 2
      ;;
    --config-identity)
      (($# >= 2)) || die '--config-identity requires a value'
      config_identity_arg=$2
      shift 2
      ;;
    --ref)
      (($# >= 2)) || die '--ref requires a value'
      config_ref=$2
      shift 2
      ;;
    --controller)
      (($# >= 2)) || die '--controller requires a value'
      controller_id=$2
      shift 2
      ;;
    --host-config)
      (($# >= 2)) || die '--host-config requires a value'
      host_config_arg=$2
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

[[ -n "$mode" ]] || { usage; die 'an explicit operating mode is required'; }
if [[ -n "$root_prefix" && "$testing" != 1 ]]; then
  die 'CI_FLEET_ROOT_PREFIX is test-only and requires CI_FLEET_TESTING=1'
fi
if [[ "$testing" != 1 && ${EUID:-$(id -u)} -ne 0 ]]; then
  die 'run this installer as root'
fi

root_path() { printf '%s%s' "$root_prefix" "$1"; }
is_git_checkout() { git -C "$1" rev-parse --is-inside-work-tree >/dev/null 2>&1; }

install_root=$(root_path /opt/ci-fleet)
releases_dir=$install_root/releases
current_link=$install_root/current
manager_root=$install_root/manager
manager_releases=$manager_root/releases
manager_current=$manager_root/current
etc_dir=$(root_path /etc/ci-fleet)
rendered_env=$etc_dir/ci-fleet.env
default_host_config=$etc_dir/host.env
host_config=${host_config_arg:-$default_host_config}
state_root=$(root_path /var/lib/ci-fleet)
state_file=$state_root/install-state.json
health_report=$state_root/health/latest.json
checkpoints_dir=$state_root/checkpoints
network_policy_checkpoint=$state_root/docker-network-policy
docker_daemon_config=$(root_path /etc/docker/daemon.json)
systemd_dir=$(root_path /etc/systemd/system)
lock_file=${CI_FLEET_INSTALLER_LOCK:-$(root_path /run/ci-fleet-installer.lock)}
controller_container=ci-fleet-controller-1
unit_names=(
  ci-fleet-health.service ci-fleet-health.timer
  ci-fleet-cleanup.service ci-fleet-cleanup.timer
  ci-fleet-drift.service ci-fleet-drift.timer
)
timer_names=(ci-fleet-health.timer ci-fleet-cleanup.timer ci-fleet-drift.timer)
optional_unit_names=(
  ci-fleet-reconcile.service ci-fleet-reconcile.timer
)

temporary=$(mktemp -d)
cleanup_temporary() {
  local path
  rm -rf "$temporary"
  for path in "${staging_paths[@]}"; do
    [[ -z "$path" ]] || rm -rf -- "$path"
  done
}
trap cleanup_temporary EXIT

require_commands() {
  local command docker_root disk_used os_id os_release os_version socket
  local -a required=(python3 docker install readlink systemctl stat awk grep date flock mktemp)
  [[ "$mode" == uninstall ]] || required+=(git tar)
  if [[ "$mode" != rollback && "$mode" != uninstall ]]; then required+=(cmp); fi
  for command in "${required[@]}"; do
    command -v "$command" >/dev/null || die "$command is required"
  done
  socket=$(root_path /var/run/docker.sock)
  [[ -z ${DOCKER_CONTEXT:-} || ${DOCKER_CONTEXT} == default ]] || die 'alternate Docker contexts are not supported; use the local Docker socket'
  [[ -z ${DOCKER_HOST:-} || ${DOCKER_HOST} == "unix://$socket" ]] || die 'alternate Docker endpoints are not supported; use the local Docker socket'
  DOCKER_HOST=unix://$socket
  export DOCKER_HOST
  unset DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH
  docker info >/dev/null 2>&1 || die 'Docker daemon is unavailable'
  docker compose version >/dev/null 2>&1 || die 'Docker Compose v2 is unavailable'
  [[ "$mode" == rollback || "$mode" == uninstall ]] && return

  for command in curl jq df openssl; do command -v "$command" >/dev/null || die "$command is required"; done
  os_release=$(root_path /etc/os-release)
  [[ -r "$os_release" ]] || die 'supported Linux release metadata is unavailable'
  os_id=$(awk -F= '$1 == "ID" {gsub(/"/, "", $2); print $2}' "$os_release")
  os_version=$(awk -F= '$1 == "VERSION_ID" {gsub(/"/, "", $2); print $2}' "$os_release")
  [[ "$os_id" == debian && "$os_version" =~ ^[0-9]+$ ]] || die 'supported Linux is Debian 12 or newer'
  ((10#$os_version >= 12)) || die 'supported Linux is Debian 12 or newer'
  [[ -r $(root_path /etc/ssl/certs/ca-certificates.crt) ]] || die 'CA certificate bundle is unavailable'
  [[ -S "$socket" && -r "$socket" && -w "$socket" || "$testing" == 1 && -e "$socket" ]] || die 'Docker socket is unavailable or inaccessible'
  docker_root=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null) || die 'Docker root directory is unavailable'
  [[ "$docker_root" == /* ]] || die 'Docker root directory is invalid'
  disk_used=$(df -P "$docker_root" 2>/dev/null | awk 'NR == 2 {gsub(/%/, "", $5); print $5}')
  [[ "$disk_used" =~ ^[0-9]{1,3}$ ]] || die 'Docker disk capacity could not be determined'
  ((disk_used < 80)) || die 'Docker filesystem must remain below 80% utilization'
}

validate_common_arguments() {
  [[ -n "$config_repo" ]] || die '--config-repo is required for this mode'
  [[ "$config_ref" =~ ^[0-9a-f]{40}$ ]] || die '--ref must be a full lowercase commit SHA'
  [[ "$controller_id" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || die '--controller must be a lowercase logical ID'
  [[ -z "$host_config_arg" || "$host_config" == "$default_host_config" ]] || die 'managed installs require the default /etc/ci-fleet/host.env path'
  if [[ "$config_repo" == *://* || "$config_repo" == *@* ]]; then
    die '--config-repo must not contain a URL or embedded credentials; use OWNER/REPOSITORY or a local path'
  fi
  [[ -z "$config_identity_arg" || "$config_identity_arg" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die '--config-identity must be OWNER/REPOSITORY'
}

resolve_config() {
  local resolved checkout
  candidate_config=$temporary/fleet.json
  if is_git_checkout "$config_repo"; then
    config_source_checkout=$(cd "$config_repo" && pwd -P)
    config_identity=${config_identity_arg:-$config_source_checkout}
    resolved=$(git -C "$config_source_checkout" rev-parse "$config_ref^{commit}" 2>/dev/null || true)
    [[ "$resolved" == "$config_ref" ]] || die 'local configuration repository does not contain the requested commit'
    git -C "$config_source_checkout" show "$config_ref:fleet.json" >"$candidate_config" || die 'fleet.json is absent at the requested configuration commit'
    return
  fi
  [[ -z "$config_identity_arg" ]] || die '--config-identity is valid only with a local Git checkout'
  [[ "$config_repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die '--config-repo must be OWNER/REPOSITORY or a local Git checkout'
  checkout=$temporary/config-repository
  config_source_checkout=$checkout
  git init -q "$checkout"
  git -C "$checkout" remote add origin "https://github.com/${config_repo}.git"
  if ! GIT_TERMINAL_PROMPT=0 git -C "$checkout" fetch -q --filter=blob:none --depth=1 origin "$config_ref"; then
    die 'configuration fetch failed; configure a read-only credential on this host or use a local pinned checkout'
  fi
  resolved=$(git -C "$checkout" rev-parse 'FETCH_HEAD^{commit}')
  [[ "$resolved" == "$config_ref" ]] || die 'fetched configuration commit does not match --ref'
  git -C "$checkout" show "$config_ref:fleet.json" >"$candidate_config" || die 'fleet.json is absent at the requested configuration commit'
  config_identity=$config_repo
}

validate_candidate_config_commit() {
  local tree_paths=$temporary/config-tree-paths evidence=$temporary/engine-rollout-evidence.json
  local args=(--config "$candidate_config" --strict --tree-paths "$tree_paths")
  git -C "$config_source_checkout" ls-tree -rz --name-only "$config_ref" >"$tree_paths" || die 'cannot inspect the configuration commit tree'
  if git -C "$config_source_checkout" cat-file -e "$config_ref:engine-rollout-evidence.json" 2>/dev/null; then
    git -C "$config_source_checkout" show "$config_ref:engine-rollout-evidence.json" >"$evidence" || die 'cannot read engine rollout evidence'
    args+=(--rollout-evidence "$evidence")
  fi
  python3 "$repo_root/templates/config-repository/scripts/validate.py" \
    "${args[@]}" || die 'configuration commit validation failed'
  python3 "$repo_root/scripts/scan_committed_secrets.py" \
    --repository "$config_source_checkout" --commit "$config_ref" || die 'configuration commit secret scan failed'
}

prepare_host_config() {
  local expected_owner=0
  effective_host_config=$host_config
  if [[ -f "$host_config" ]]; then
    return
  fi
  if [[ -f "$rendered_env" && "$mode" == adopt ]]; then
    [[ "$testing" != 1 ]] || expected_owner=$(id -u)
    [[ $(stat -c %u "$rendered_env") == "$expected_owner" && $(stat -c %a "$rendered_env") == 600 ]] || die "rendered environment must be owned by root with mode 0600: $rendered_env"
    install -d -m 0700 "$etc_dir"
    python3 "$repo_root/scripts/desired_state.py" extract-host-env \
      --source "$rendered_env" --output "$effective_host_config"
    return
  fi
  die "host-local GitHub App configuration is missing: $host_config"
}

verify_host_files() {
  local mode_bits owner expected_owner key_file
  expected_owner=0
  [[ "$testing" != 1 ]] || expected_owner=$(id -u)
  mode_bits=$(stat -c '%a' "$effective_host_config")
  owner=$(stat -c '%u' "$effective_host_config")
  [[ "$mode_bits" == 600 ]] || die "host configuration must have mode 0600: $effective_host_config"
  [[ "$owner" == "$expected_owner" ]] || die 'host configuration must be owned by root'
  key_file=$(awk -F= '$1 == "CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE" {print substr($0, index($0, "=") + 1)}' "$effective_host_config")
  [[ -n "$key_file" && -f "$key_file" ]] || die 'GitHub App PEM file is missing'
  mode_bits=$(stat -c '%a' "$key_file")
  owner=$(stat -c '%u' "$key_file")
  [[ "$mode_bits" == 600 && "$owner" == "$expected_owner" ]] || die 'GitHub App PEM must be owned by root and have mode 0600'
}

load_installed_controller_identity() {
  local source_state=${1:-$state_file} source_env=${2:-$rendered_env} expected_owner=0
  [[ "$testing" != 1 ]] || expected_owner=$(id -u)
  if [[ -f "$source_state" && $(stat -c %u "$source_state") == "$expected_owner" && $(stat -c %a "$source_state") == 600 ]]; then
    controller_id=$(python3 - "$source_state" <<'PY'
import json
import sys
try:
    value = json.load(open(sys.argv[1], encoding="utf-8"))["controller"]
except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(2)
print(value)
PY
    ) || die "installed controller identity is invalid: $source_state"
  elif [[ -f "$source_env" ]]; then
    [[ $(stat -c %u "$source_env") == "$expected_owner" && $(stat -c %a "$source_env") == 600 ]] || die "rendered environment must be owned by root with mode 0600: $source_env"
    controller_id=$(awk -F= '$1 == "CI_FLEET_INSTANCE" {count++; value=substr($0, index($0, "=") + 1)} END {if (count != 1) exit 1; print value}' "$source_env") || die "installed controller identity is invalid: $source_env"
  elif [[ -f "$source_state" ]]; then
    die "install state must be owned by root with mode 0600: $source_state"
  else
    die 'installed controller identity is unavailable'
  fi
  [[ "$controller_id" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || die 'installed controller identity is invalid'
}

docker_gid() {
  if [[ "$testing" == 1 && -n ${CI_FLEET_DOCKER_GID_OVERRIDE:-} ]]; then
    printf '%s' "$CI_FLEET_DOCKER_GID_OVERRIDE"
    return
  fi
  stat -c '%g' /var/run/docker.sock
}

select_engine() {
  local -a selected
  mapfile -t selected < <(python3 "$repo_root/scripts/desired_state.py" engine \
    --config "$candidate_config" --controller "$controller_id")
  [[ ${#selected[@]} == 2 ]] || die 'selected engine metadata is incomplete'
  engine_ref=${selected[0]}
  engine_repository=${selected[1]}
  [[ "$engine_repository" == RandomDevelopment/ci-fleet ]] || die 'delivery engine repository is not the fixed reviewed public engine'
  release_dir=$releases_dir/$engine_ref
}

prepare_engine_capabilities() {
  local checkout resolved manifest_mode
  engine_capabilities=$temporary/engine-capabilities.json
  if runtime_release_complete "$release_dir" "$engine_ref" && release_tree_permissions_trusted "$release_dir"; then
    if [[ -f "$release_dir/engine-capabilities.json" ]]; then
      cp "$release_dir/engine-capabilities.json" "$engine_capabilities"
    else
      rm -f "$engine_capabilities"
    fi
    return
  fi
  if is_git_checkout "$repo_root" && git -C "$repo_root" cat-file -e "$engine_ref^{commit}" 2>/dev/null; then
    manifest_mode=$(git -C "$repo_root" ls-tree "$engine_ref" -- engine-capabilities.json | awk '{print $1}')
    [[ "$manifest_mode" == 100644 ]] || { rm -f "$engine_capabilities"; return; }
    git -C "$repo_root" show "$engine_ref:engine-capabilities.json" >"$engine_capabilities" 2>/dev/null || rm -f "$engine_capabilities"
    return
  fi
  checkout=$temporary/engine-capabilities-repository
  git init -q "$checkout"
  git -C "$checkout" remote add origin "https://github.com/${engine_repository}.git"
  GIT_TERMINAL_PROMPT=0 git -C "$checkout" fetch -q --depth=1 origin "$engine_ref" || die 'pinned ci-fleet engine commit could not be fetched for capability validation'
  resolved=$(git -C "$checkout" rev-parse 'FETCH_HEAD^{commit}')
  [[ "$resolved" == "$engine_ref" ]] || die 'fetched ci-fleet engine commit does not match desired state'
  manifest_mode=$(git -C "$checkout" ls-tree FETCH_HEAD -- engine-capabilities.json | awk '{print $1}')
  [[ "$manifest_mode" == 100644 ]] || { rm -f "$engine_capabilities"; return; }
  git -C "$checkout" show "FETCH_HEAD:engine-capabilities.json" >"$engine_capabilities" 2>/dev/null || rm -f "$engine_capabilities"
}

render_candidate() {
  local -a metadata_values capability_args=()
  candidate_env=$temporary/ci-fleet.env
  candidate_metadata=$temporary/metadata.json
  [[ ! -f "$engine_capabilities" ]] || capability_args=(--engine-capabilities "$engine_capabilities")
  python3 "$repo_root/scripts/desired_state.py" render \
    --config "$candidate_config" \
    --controller "$controller_id" \
    --host-config "$effective_host_config" \
    --config-repository "$config_identity" \
    --config-ref "$config_ref" \
    --docker-gid "$(docker_gid)" \
    "${capability_args[@]}" \
    --output "$candidate_env" \
    --metadata-output "$candidate_metadata"
  mapfile -t metadata_values < <(python3 - "$candidate_metadata" <<'PY'
import json
import sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
for key in ("controller_state", "engine_ref", "engine_repository"):
    print(value[key])
print(1 if value["status_reporting_required"] else 0)
print(1 if value["status_reporting_configured"] else 0)
PY
  )
  [[ ${#metadata_values[@]} == 5 ]] || die 'rendered controller metadata is incomplete'
  target_state=${metadata_values[0]}
  [[ ${metadata_values[1]} == "$engine_ref" && ${metadata_values[2]} == "$engine_repository" ]] || die 'rendered engine metadata changed during validation'
  status_reporting_required=${metadata_values[3]}
  status_reporting_configured=${metadata_values[4]}
}

compose() {
  local release=$1 env_file=$2 variable
  local -a clean_environment=(env -i "PATH=$PATH" "HOME=${HOME:-/root}")
  shift 2
  for variable in DOCKER_HOST DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH DOCKER_CONFIG XDG_RUNTIME_DIR; do
    [[ ! -v $variable ]] || clean_environment+=("$variable=${!variable}")
  done
  if [[ "$testing" == 1 ]]; then
    for variable in ${!FAKE_@}; do clean_environment+=("$variable=${!variable}"); done
  fi
  "${clean_environment[@]}" docker compose --project-name ci-fleet --env-file "$env_file" -f "$release/deploy/compose.yaml" "$@"
}

controller_status() {
  docker inspect --format '{{.State.Status}}' "$controller_container" 2>/dev/null || true
}

current_runtime_release() {
  local target='' marker
  if [[ -L "$current_link" ]]; then
    target=$(readlink -f "$current_link" 2>/dev/null || true)
  elif [[ -f "$install_root/deploy/compose.yaml" ]]; then
    target=$install_root
  fi
  [[ -n "$target" && -f "$target/.ci-fleet-engine-ref" ]] || return 0
  marker=$(<"$target/.ci-fleet-engine-ref")
  [[ "$marker" =~ ^[0-9a-f]{40}$ ]] && runtime_release_complete "$target" "$marker" \
    && release_tree_permissions_trusted "$target" || return 0
  printf '%s' "$target"
}

managed_runner_count() {
  docker ps -q \
    --filter label=io.randomdevelopment.ci-fleet.managed=true \
    --filter label=io.randomdevelopment.ci-fleet.kind=runner \
    --filter "label=io.randomdevelopment.ci-fleet.instance=$controller_id" | wc -l | tr -d ' '
}

managed_runner_total_count() {
  docker ps --all -q \
    --filter label=io.randomdevelopment.ci-fleet.managed=true \
    --filter label=io.randomdevelopment.ci-fleet.kind=runner \
    --filter "label=io.randomdevelopment.ci-fleet.instance=$controller_id" | wc -l | tr -d ' '
}

remove_inactive_managed_runners() {
  local -a containers=()
  mapfile -t containers < <(docker ps --all -q \
    --filter label=io.randomdevelopment.ci-fleet.managed=true \
    --filter label=io.randomdevelopment.ci-fleet.kind=runner \
    --filter "label=io.randomdevelopment.ci-fleet.instance=$controller_id")
  ((${#containers[@]} == 0)) || docker rm "${containers[@]}" >/dev/null
}

controller_environment_matches() {
  local actual expected key live
  live=$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$controller_container" 2>/dev/null) || return 1
  for key in \
    CI_FLEET_GITHUB_URL CI_FLEET_SCALE_SET_NAME CI_FLEET_LABELS CI_FLEET_RUNNER_GROUP \
    CI_FLEET_RUNNER_IMAGE CI_FLEET_INSTANCE CI_FLEET_GITHUB_APP_CLIENT_ID \
    CI_FLEET_GITHUB_APP_INSTALLATION_ID CI_FLEET_MIN_RUNNERS CI_FLEET_MAX_RUNNERS \
    CI_FLEET_RUNNER_CPUS CI_FLEET_RUNNER_MEMORY_MIB CI_FLEET_RUNNER_TTL CI_FLEET_DOCKER_GID \
    CI_FLEET_DOCKER_NETWORKS_PER_RUNNER CI_FLEET_DOCKER_NETWORK_RESERVE_SUBNETS; do
    expected=$(awk -F= -v key="$key" '$1 == key {print substr($0, index($0, "=") + 1)}' "$candidate_env")
    if [[ -z "$expected" && ( "$key" == CI_FLEET_DOCKER_NETWORKS_PER_RUNNER || "$key" == CI_FLEET_DOCKER_NETWORK_RESERVE_SUBNETS ) ]]; then
      actual=$(awk -F= -v key="$key" '$1 == key {count++; value=substr($0, index($0, "=") + 1)} END {if (count > 1) exit 1; print value}' <<<"$live") || return 1
      [[ -z "$actual" || "$actual" == 0 ]] || return 1
      continue
    fi
    [[ -n "$expected" ]] || return 1
    actual=$(awk -F= -v key="$key" '$1 == key {count++; value=substr($0, index($0, "=") + 1)} END {if (count != 1) exit 1; print value}' <<<"$live") || return 1
    [[ "$actual" == "$expected" ]] || return 1
  done
}

runtime_matches() {
  local expected=$1 expected_image expected_image_ref live_image provenance status
  status=$(controller_status)
  if [[ "$expected" == active ]]; then
    [[ "$status" == running ]] || return 1
    expected_image_ref=$(awk -F= '$1 == "CI_FLEET_CONTROLLER_IMAGE" {print substr($0, index($0, "=") + 1)}' "$candidate_env")
    [[ -n "$expected_image_ref" ]] || return 1
    live_image=$(docker inspect --format '{{.Image}}' "$controller_container" 2>/dev/null) || return 1
    expected_image=$(docker image inspect --format '{{.Id}}' "$expected_image_ref" 2>/dev/null) || return 1
    [[ "$live_image" == "$expected_image" ]] || return 1
    provenance=$(docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$controller_container" 2>/dev/null) || return 1
    [[ "$provenance" == "$engine_ref" ]] || return 1
    controller_environment_matches
  else
    [[ -z "$status" || "$status" == exited || "$status" == created ]]
  fi
}

state_matches() {
  local expected_owner=0
  [[ -f "$state_file" ]] || return 1
  [[ "$testing" != 1 ]] || expected_owner=$(id -u)
  [[ $(stat -c %u "$state_file") == "$expected_owner" && $(stat -c %a "$state_file") == 600 ]] || return 1
  python3 - "$state_file" "$candidate_metadata" <<'PY'
import json
import sys
installed = json.load(open(sys.argv[1], encoding="utf-8"))
installed.pop("installed_at", None)
candidate = json.load(open(sys.argv[2], encoding="utf-8"))
raise SystemExit(0 if installed == candidate else 1)
PY
}

release_tree_digest() {
  python3 - "$1" <<'PY'
import hashlib
import os
import stat
import sys

root = os.path.abspath(sys.argv[1])
excluded = {".ci-fleet-engine-ref", ".ci-fleet-tree-sha256"}
digest = hashlib.sha256()


def add(kind, relative, mode, payload=b""):
    digest.update(kind)
    digest.update(b"\0")
    digest.update(relative.encode("utf-8", "surrogateescape"))
    digest.update(b"\0")
    digest.update(f"{mode:o}".encode("ascii"))
    digest.update(b"\0")
    digest.update(payload)
    digest.update(b"\0")


def visit(directory):
    for entry in sorted(os.scandir(directory), key=lambda item: item.name):
        relative = os.path.relpath(entry.path, root)
        if relative in excluded:
            continue
        metadata = entry.stat(follow_symlinks=False)
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISDIR(metadata.st_mode):
            add(b"directory", relative, mode)
            visit(entry.path)
        elif stat.S_ISREG(metadata.st_mode):
            content = hashlib.sha256()
            with open(entry.path, "rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    content.update(block)
            add(b"file", relative, mode, content.digest())
        elif stat.S_ISLNK(metadata.st_mode):
            add(b"symlink", relative, mode, os.readlink(entry.path).encode("utf-8", "surrogateescape"))
        else:
            raise SystemExit(f"unsupported release entry: {relative}")


visit(root)
print(digest.hexdigest())
PY
}

release_tree_permissions_trusted() {
  local path=$1 expected_owner=0 boundary=${root_prefix:-/}
  [[ "$testing" != 1 ]] || expected_owner=$(id -u)
  python3 - "$path" "$expected_owner" "$boundary" <<'PY'
import os
import stat
import sys

root = os.path.abspath(sys.argv[1])
expected_owner = int(sys.argv[2])
boundary = os.path.abspath(sys.argv[3])


def trusted(path):
    metadata = os.lstat(path)
    if metadata.st_uid != expected_owner:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        return False
    return (
        (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode))
        and stat.S_IMODE(metadata.st_mode) & 0o022 == 0
    )


try:
    if os.path.commonpath((root, boundary)) != boundary:
        raise ValueError
    anchor = root
    while True:
        metadata = os.lstat(anchor)
        if metadata.st_uid != expected_owner or not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o022:
            raise ValueError
        if anchor == boundary:
            break
        parent = os.path.dirname(anchor)
        if parent == anchor:
            raise ValueError
        anchor = parent
    for directory, directories, files in os.walk(root, followlinks=False):
        if any(not trusted(os.path.join(directory, name)) for name in directories + files):
            raise ValueError
except (OSError, ValueError):
    raise SystemExit(1)
PY
}

runtime_release_complete() {
  local path=$1 expected=$2 require_status=${3:-0} require_schema=${4:-0} marker required stored_digest actual_digest policy_script
  local -a capability_args=()
  [[ -d "$path" && -f "$path/.ci-fleet-engine-ref" && -f "$path/.ci-fleet-tree-sha256" && -f "$path/deploy/compose.yaml" ]] || return 1
  [[ -x "$path/scripts/preflight.sh" && -x "$path/scripts/healthcheck.sh" && -x "$path/scripts/cleanup.sh" ]] || return 1
  if [[ -e "$path/engine-capabilities.json" || "$require_status" == 1 || "$require_schema" == 1 ]]; then
    [[ ! -L "$path/engine-capabilities.json" && -f "$path/engine-capabilities.json" ]] || return 1
    [[ "$require_schema" != 1 ]] || capability_args+=(--require-status-reporting-config)
    [[ "$require_status" != 1 ]] || capability_args+=(--require-status-reporting)
    python3 "$repo_root/scripts/desired_state.py" validate-engine-capabilities \
      --manifest "$path/engine-capabilities.json" "${capability_args[@]}" >/dev/null || return 1
    for required in docker_network_policy_config:apply-docker-network-policy.sh docker_network_policy_adapter:docker-network-policy-adapter.sh; do
      policy_script=${required#*:}
      if python3 - "$path/engine-capabilities.json" "${required%%:*}" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
raise SystemExit(value.get("capabilities", {}).get(sys.argv[2]) is not True)
PY
      then
        [[ -f "$path/scripts/$policy_script" && ! -L "$path/scripts/$policy_script" && -x "$path/scripts/$policy_script" ]] || return 1
      fi
    done
  fi
  if grep -Fq 'scripts/health.py' "$path/scripts/healthcheck.sh"; then
    [[ -f "$path/scripts/health.py" ]] || return 1
    if grep -Fq 'build_status_report' "$path/scripts/health.py"; then
      [[ -f "$path/scripts/status_auth.py" && -f "$path/controller/status.go" ]] || return 1
    fi
  fi
  for required in controller/Dockerfile controller/go.mod controller/main.go controller/config.go controller/scaler.go controller/state.go runner/Dockerfile; do
    [[ -f "$path/$required" ]] || return 1
  done
  marker=$(<"$path/.ci-fleet-engine-ref")
  [[ "$marker" == "$expected" ]] || return 1
  stored_digest=$(<"$path/.ci-fleet-tree-sha256")
  [[ "$stored_digest" =~ ^[0-9a-f]{64}$ ]] || return 1
  actual_digest=$(release_tree_digest "$path") || return 1
  [[ "$actual_digest" == "$stored_digest" ]]
}

manager_release_complete() {
  local path=$1 expected=$2 require_status=${3:-0} require_schema=${4:-0} marker required unit
  runtime_release_complete "$path" "$expected" "$require_status" "$require_schema" || return 1
  [[ -x "$path/scripts/install-worker-controller.sh" && -x "$path/scripts/check-installed-state.sh" ]] || return 1
  for required in scripts/desired_state.py scripts/scan_committed_secrets.py templates/config-repository/fleet.schema.json templates/config-repository/scripts/validate.py; do
    [[ -f "$path/$required" ]] || return 1
  done
  [[ -x "$path/templates/config-repository/scripts/validate.sh" ]] || return 1
  for unit in "${unit_names[@]}"; do [[ -f "$path/host/systemd/$unit" ]] || return 1; done
  marker=$(<"$path/.ci-fleet-engine-ref")
  [[ "$marker" == "$expected" ]]
}

raw_link_target_path() {
  local target=$1 link=$2
  if [[ "$target" != /* ]]; then
    target=$(dirname "$link")/$target
  fi
  printf '%s' "$target"
}

raw_pointer_target_exists() {
  local link=$1 target
  target=$(readlink -n "$link" 2>/dev/null && printf x) || return 1
  target=${target%x}
  target=$(raw_link_target_path "$target" "$link") || return 1
  [[ -e "$target" || -L "$target" ]]
}

resolve_link_target() {
  local target=$1 link=$2
  if [[ "$target" != /* ]]; then
    [[ "/$target/" != *"/../"* ]] || return 1
    target=$(realpath -ms -- "$(dirname "$link")/$target") || return 1
  fi
  printf '%s' "$target"
}

canonical_release_target() {
  local target=$1 releases=$2 relative
  [[ "$target" == "$releases/"* ]] || return 1
  relative=${target#"$releases/"}
  [[ "$relative" =~ ^[0-9a-f]{40}$ && ! -L "$target" ]] || return 1
  printf '%s' "$target"
}

canonical_release_target_from_raw_pointer() {
  local link=$1 releases=$2 target
  target=$(readlink -n "$link" 2>/dev/null && printf x) || return 1
  target=${target%x}
  canonical_release_target "$target" "$releases"
}

release_target_from_raw_pointer() {
  local link=$1 releases=$2 target relative ref
  target=$(canonical_release_target_from_raw_pointer "$link" "$releases") || return 1
  relative=${target#"$releases/"}
  [[ -f "$target/.ci-fleet-engine-ref" ]] || return 1
  ref=$(<"$target/.ci-fleet-engine-ref")
  [[ "$relative" == "$ref" ]] || return 1
  printf '%s' "$target"
}

manager_release_from_raw_pointer() {
  local target ref
  target=$(release_target_from_raw_pointer "$manager_current" "$manager_releases") || return 1
  ref=$(<"$target/.ci-fleet-engine-ref")
  manager_release_complete "$target" "$ref" && release_tree_permissions_trusted "$target" || return 1
  printf '%s' "$target"
}

release_matches() {
  local target
  runtime_release_complete "$release_dir" "$engine_ref" "$status_reporting_required" "$status_reporting_configured" || return 1
  release_tree_permissions_trusted "$release_dir" || return 1
  target=$(canonical_release_target_from_raw_pointer "$current_link" "$releases_dir") || return 1
  [[ "$target" == "$release_dir" ]]
}

managed_images_match() {
  local image provenance
  local -a expected_images=()
  mapfile -t expected_images < <(awk -F= '$1 == "CI_FLEET_CONTROLLER_IMAGE" || $1 == "CI_FLEET_RUNNER_IMAGE" {print substr($0, index($0, "=") + 1)}' "$candidate_env")
  [[ ${#expected_images[@]} == 2 ]] || return 1
  for image in "${expected_images[@]}"; do
    provenance=$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image" 2>/dev/null) || return 1
    [[ "$provenance" == "$engine_ref" ]] || return 1
  done
}

systemd_matches() {
  local expected_manager unit
  expected_manager=$manager_releases/$engine_ref
  manager_release_complete "$expected_manager" "$engine_ref" "$status_reporting_required" "$status_reporting_configured" || return 1
  release_tree_permissions_trusted "$expected_manager" || return 1
  [[ $(manager_release_from_raw_pointer || true) == "$expected_manager" ]] || return 1
  for unit in "${unit_names[@]}"; do
    [[ -f "$systemd_dir/$unit" ]] || return 1
    cmp -s "$expected_manager/host/systemd/$unit" "$systemd_dir/$unit" || return 1
  done
  for unit in "${timer_names[@]}"; do
    systemctl is-enabled --quiet "$unit" || return 1
    systemctl is-active --quiet "$unit" || return 1
  done
}

docker_daemon_config_trusted() {
  local expected_owner=0
  [[ "$testing" != 1 ]] || expected_owner=$(id -u)
  python3 - "$docker_daemon_config" "$expected_owner" "$root_prefix" <<'PY'
import json
import os
import stat
import sys

path, expected_owner, root_prefix = sys.argv[1:]
expected_owner = int(expected_owner)
anchor = os.path.realpath(root_prefix) if root_prefix else "/"
try:
    if (
        not path.startswith("/")
        or os.path.normpath(path) != path
        or os.path.realpath(path) != path
        or os.path.commonpath((anchor, path)) != anchor
    ):
        raise ValueError
    current = os.path.dirname(path)
    while True:
        metadata = os.lstat(current)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != expected_owner
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise ValueError
        if current == anchor:
            break
        current = os.path.dirname(current)
    if not os.path.lexists(path):
        raise SystemExit(0)
    metadata = os.lstat(path)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != expected_owner
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or not isinstance(json.load(open(path, encoding="utf-8")), dict)
    ):
        raise ValueError
except (OSError, ValueError, TypeError, json.JSONDecodeError):
    raise SystemExit(1)
PY
}

docker_network_policy_matches() {
  local expected_owner=0
  [[ "$testing" != 1 ]] || expected_owner=$(id -u)
  if [[ ! -e "$network_policy_checkpoint/docker-network-policy.json" && ! -L "$network_policy_checkpoint/docker-network-policy.json" ]] \
    && ! grep -q '^CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT=' "$candidate_env"; then
    return 0
  fi
  docker_daemon_config_trusted || return 1
  python3 - "$candidate_env" "$docker_daemon_config" "$network_policy_checkpoint/docker-network-policy.json" "$repo_root/scripts" "$expected_owner" "$network_policy_checkpoint" "$root_prefix" <<'PY'
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

environment, daemon_path, marker_path, scripts_path, expected_owner, checkpoint_dir, root_prefix = sys.argv[1:]
sys.path.insert(0, scripts_path)
from desired_state import parse_env, render_docker_daemon_config, validate_docker_address_pools

values = parse_env(Path(environment), allow_unknown=True)
managed = "CI_FLEET_DOCKER_DEFAULT_ADDRESS_POOL_COUNT" in values
marker_exists = os.path.lexists(marker_path)
if not managed:
    raise SystemExit(1 if marker_exists else 0)
if not marker_exists or not os.path.exists(daemon_path):
    raise SystemExit(1)
try:
    anchor = os.path.realpath(root_prefix) if root_prefix else "/"
    if (
        not checkpoint_dir.startswith("/")
        or os.path.normpath(checkpoint_dir) != checkpoint_dir
        or os.path.realpath(checkpoint_dir) != checkpoint_dir
        or os.path.commonpath((anchor, checkpoint_dir)) != anchor
    ):
        raise ValueError
    checkpoint_meta = os.lstat(checkpoint_dir)
    if (
        not stat.S_ISDIR(checkpoint_meta.st_mode)
        or checkpoint_meta.st_uid != int(expected_owner)
        or stat.S_IMODE(checkpoint_meta.st_mode) != 0o700
    ):
        raise ValueError
    current = os.path.dirname(checkpoint_dir)
    while True:
        ancestor_meta = os.lstat(current)
        if (
            not stat.S_ISDIR(ancestor_meta.st_mode)
            or ancestor_meta.st_uid != int(expected_owner)
            or ancestor_meta.st_mode & 0o022
        ):
            raise ValueError
        if current == anchor:
            break
        current = os.path.dirname(current)
    marker_meta = os.lstat(marker_path)
    daemon_meta = os.lstat(daemon_path)
    if (
        not stat.S_ISREG(marker_meta.st_mode)
        or stat.S_ISLNK(marker_meta.st_mode)
        or marker_meta.st_uid != int(expected_owner)
        or stat.S_IMODE(marker_meta.st_mode) != 0o600
        or not stat.S_ISREG(daemon_meta.st_mode)
        or stat.S_ISLNK(daemon_meta.st_mode)
    ):
        raise ValueError
    marker = json.load(open(marker_path, encoding="utf-8"))
    daemon_bytes = Path(daemon_path).read_bytes()
    daemon = json.loads(daemon_bytes)
    desired = render_docker_daemon_config(values)
    required = {
        "managed",
        "prior_default_address_pools",
        "prior_default_address_pools_present",
        "prior_mode",
        "prior_present",
        "verified_generation",
    }
    if not isinstance(marker, dict) or set(marker) != required or marker["managed"] is not True:
        raise ValueError
    if not isinstance(marker["prior_present"], bool) or not isinstance(marker["prior_default_address_pools_present"], bool):
        raise ValueError
    if marker["prior_default_address_pools_present"]:
        if not marker["prior_present"]:
            raise ValueError
        validate_docker_address_pools(marker["prior_default_address_pools"], path="checkpoint prior default address pools")
    elif marker["prior_default_address_pools"] is not None:
        raise ValueError
    mode = marker["prior_mode"]
    if marker["prior_present"]:
        if not isinstance(mode, str) or not re.fullmatch(r"[0-7]{3,4}", mode):
            raise ValueError
    elif mode is not None:
        raise ValueError
    generation = marker["verified_generation"]
    if (
        not isinstance(generation, str)
        or not re.fullmatch(r"[0-9a-f]{64}", generation)
        or not isinstance(daemon, dict)
        or daemon.get("default-address-pools") != desired.get("default-address-pools")
        or hashlib.sha256(daemon_bytes).hexdigest() != generation
    ):
        raise ValueError
except (OSError, ValueError, TypeError, json.JSONDecodeError):
    raise SystemExit(1)
PY
}

drift_count() {
  local count=0 expected_owner=0
  [[ "$testing" != 1 ]] || expected_owner=$(id -u)
  if [[ ! -f "$rendered_env" ]] \
    || [[ $(stat -c %u "$rendered_env") != "$expected_owner" ]] \
    || [[ $(stat -c %a "$rendered_env") != 600 ]] \
    || ! cmp -s "$candidate_env" "$rendered_env"; then
    note 'DRIFT rendered_environment'
    count=$((count + 1))
  fi
  release_matches || { note 'DRIFT engine_release'; count=$((count + 1)); }
  state_matches || { note 'DRIFT install_state'; count=$((count + 1)); }
  runtime_matches "$target_state" || { note 'DRIFT controller_runtime'; count=$((count + 1)); }
  if [[ "$target_state" != active && $(managed_runner_total_count) != 0 ]]; then
    note 'DRIFT managed_runners'
    count=$((count + 1))
  fi
  managed_images_match || { note 'DRIFT managed_images'; count=$((count + 1)); }
  systemd_matches || { note 'DRIFT maintenance_timers'; count=$((count + 1)); }
  DOCKER_NETWORK_POLICY_DRIFT=false
  docker_network_policy_matches || { note 'DRIFT docker_network_policy'; count=$((count + 1)); DOCKER_NETWORK_POLICY_DRIFT=true; }
  DRIFT_COUNT=$count
}

atomic_replace_directory() {
  local replacement=$1 target=$2
  if [[ ! -e "$target" && ! -L "$target" ]]; then
    mv "$replacement" "$target"
    return
  fi
  python3 - "$replacement" "$target" <<'PY'
import ctypes
import os
import sys

replacement, target = map(os.fsencode, sys.argv[1:])
libc = ctypes.CDLL(None, use_errno=True)
renameat2 = getattr(libc, "renameat2", None)
if renameat2 is None:
    raise OSError("atomic directory exchange is unavailable")
renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
if renameat2(-100, replacement, -100, target, 2) != 0:  # AT_FDCWD, RENAME_EXCHANGE
    error = ctypes.get_errno()
    raise OSError(error, os.strerror(error), os.fsdecode(target))
parent = os.open(os.path.dirname(target), os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(parent)
finally:
    os.close(parent)
PY
}

install_release() {
  local install_ref=${1:-$engine_ref} install_dir=${2:-$release_dir}
  local require_status=${3:-$status_reporting_required} require_schema=${4:-$status_reporting_configured}
  local archive checkout resolved staged_release
  if runtime_release_complete "$install_dir" "$install_ref" "$require_status" "$require_schema" &&
    release_tree_permissions_trusted "$install_dir"; then
    return
  fi
  install -d -m 0755 "$releases_dir"
  archive=$temporary/engine-$install_ref.tar
  if is_git_checkout "$repo_root" && git -C "$repo_root" cat-file -e "$install_ref^{commit}" 2>/dev/null; then
    git -C "$repo_root" archive --format=tar --output "$archive" "$install_ref" || return 1
  else
    [[ "$engine_repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die 'delivery engine repository is invalid'
    checkout=$temporary/engine-repository-$install_ref
    git init -q "$checkout"
    git -C "$checkout" remote add origin "https://github.com/${engine_repository}.git"
    GIT_TERMINAL_PROMPT=0 git -C "$checkout" fetch -q --depth=1 origin "$install_ref" || die 'pinned ci-fleet engine commit could not be fetched'
    resolved=$(git -C "$checkout" rev-parse 'FETCH_HEAD^{commit}')
    [[ "$resolved" == "$install_ref" ]] || die 'fetched ci-fleet engine commit does not match desired state'
    git -C "$checkout" archive --format=tar --output "$archive" FETCH_HEAD || return 1
  fi
  staged_release=$(mktemp -d "$releases_dir/.${install_ref}.staging.XXXXXX")
  staging_paths+=("$staged_release")
  chmod 0755 "$staged_release"
  (umask 0022; tar --no-same-permissions -xf "$archive" -C "$staged_release") || return 1
  printf '%s\n' "$install_ref" >"$staged_release/.ci-fleet-engine-ref"
  chmod 0644 "$staged_release/.ci-fleet-engine-ref"
  release_tree_digest "$staged_release" >"$staged_release/.ci-fleet-tree-sha256"
  chmod 0644 "$staged_release/.ci-fleet-tree-sha256"
  if ! runtime_release_complete "$staged_release" "$install_ref" "$require_status" "$require_schema" ||
    ! release_tree_permissions_trusted "$staged_release"; then
    die 'staged engine release is incomplete or untrusted'
  fi
  atomic_replace_directory "$staged_release" "$install_dir" || return 1
}

install_manager() {
  local manager_commit=${1:-$engine_ref} source_release=${2:-$release_dir}
  local manager_release=${3:-$manager_releases/$manager_commit}
  local require_status=${4:-$status_reporting_required} require_schema=${5:-$status_reporting_configured}
  local activate=${6:-true} archive staged_manager
  [[ "$manager_commit" =~ ^[0-9a-f]{40}$ ]] || die 'installer manager commit is invalid'
  runtime_release_complete "$source_release" "$manager_commit" "$require_status" "$require_schema" || die 'desired engine release is unavailable for installer manager activation'
  if ! manager_release_complete "$manager_release" "$manager_commit" "$require_status" "$require_schema" ||
    ! release_tree_permissions_trusted "$manager_release"; then
    install -d -m 0755 "$manager_releases"
    archive=$temporary/manager-$manager_commit.tar
    tar -cf "$archive" -C "$source_release" . || return 1
    staged_manager=$(mktemp -d "$manager_releases/.${manager_commit}.staging.XXXXXX")
    staging_paths+=("$staged_manager")
    chmod 0755 "$staged_manager"
    (umask 0022; tar --no-same-permissions -xf "$archive" -C "$staged_manager") || return 1
    printf '%s\n' "$manager_commit" >"$staged_manager/.ci-fleet-engine-ref"
    chmod 0644 "$staged_manager/.ci-fleet-engine-ref"
    if ! manager_release_complete "$staged_manager" "$manager_commit" "$require_status" "$require_schema" ||
      ! release_tree_permissions_trusted "$staged_manager"; then
      die 'staged installer manager release is incomplete or untrusted'
    fi
    atomic_replace_directory "$staged_manager" "$manager_release" || return 1
  fi
  [[ "$activate" == true ]] || return 0
  install -d -m 0755 "$manager_root"
  ln -sfn "$manager_release" "$temporary/manager-current"
  mv -Tf "$temporary/manager-current" "$manager_current"
}

run_candidate_preflight() {
  (
    set -a
    # shellcheck disable=SC1090
    . "$candidate_env"
    set +a
    CI_FLEET_TESTING=$testing "$release_dir/scripts/preflight.sh" --managed
  )
}

build_candidate() {
  compose "$release_dir" "$candidate_env" build runner-image controller
}

load_checkpoint_images() {
  local environment=$1 image_ids=${2:-} output
  output=$(python3 - "$environment" "$image_ids" "$repo_root/scripts" <<'PY'
import re
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[3])
from desired_state import parse_env

image_keys = ("CI_FLEET_RUNNER_IMAGE", "CI_FLEET_CONTROLLER_IMAGE")
values = parse_env(Path(sys.argv[1]), allow_unknown=True)
if any(not values.get(key) for key in image_keys):
    raise SystemExit(1)
for key in image_keys:
    print(values[key])

if sys.argv[2]:
    id_keys = tuple(f"{key}_ID" for key in image_keys)
    values = parse_env(Path(sys.argv[2]), allow_unknown=True)
    live_key = "CI_FLEET_CONTROLLER_LIVE_IMAGE_ID"
    if not set(id_keys).issubset(values) or not set(values) <= {*id_keys, live_key} or any(values[key] != "absent" and not re.fullmatch(r"sha256:[0-9a-f]{64}", values[key]) for key in id_keys):
        raise SystemExit(1)
    if live_key in values and (not re.fullmatch(r"sha256:[0-9a-f]{64}", values[live_key]) or values[live_key] == values[id_keys[1]]):
        raise SystemExit(1)
    for key in id_keys:
        print(values[key])
    if live_key in values:
        print(values[live_key])
PY
  ) || return 1
  mapfile -t checkpoint_images <<<"$output"
  [[ ${#checkpoint_images[@]} == 2 || -n "$image_ids" && ( ${#checkpoint_images[@]} == 4 || ${#checkpoint_images[@]} == 5 ) ]]
}

capture_current_pointer() {
  local expected_owner=0
  [[ -z "$captured_current_state" ]] || return 0
  [[ "$testing" != 1 ]] || expected_owner=$(id -u)
  if [[ -L "$current_link" ]]; then
    if ! python3 - "$current_link" "$temporary/current-link" "$expected_owner" <<'PY'
import os
import stat
import sys

source, destination = map(os.fsencode, sys.argv[1:3])
metadata = os.lstat(source)
if not stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != int(sys.argv[3]) or stat.S_IMODE(metadata.st_mode) != 0o777:
    raise SystemExit(1)
target = os.readlink(source)
if not target or len(target) > 4095:
    raise SystemExit(1)
descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "wb") as output:
    output.write(target)
PY
    then
      die 'current pointer is invalid'
    fi
    captured_current_state='link'
  elif [[ -e "$current_link" ]]; then
    die 'current pointer must be a symlink or absent'
  else
    captured_current_state=absent
  fi
}

repair_pending_checkpoint_authority() {
  local checkpoint=$1 kind file target ref expected_owner=0 source staged_link release_authority=''
  [[ "$testing" != 1 ]] || expected_owner=$(id -u)
  for kind in release manager; do
    file=$checkpoint/$kind-target
    [[ -e "$file" || -L "$file" ]] || continue
    [[ -f "$file" && ! -L "$file" && $(stat -c %u "$file") == "$expected_owner" \
      && $(stat -c %a "$file") == 600 && $(stat -c %s "$file") -ge 2 \
      && $(stat -c %s "$file") -le 4096 && $(wc -l <"$file") == 1 ]] || return 1
    target=$(<"$file")
    [[ -f "$target/.ci-fleet-engine-ref" && ! -L "$target" ]] || return 1
    ref=$(<"$target/.ci-fleet-engine-ref")
    [[ "$ref" =~ ^[0-9a-f]{40}$ ]] || return 1
    if [[ "$kind" == release ]]; then
      [[ "$target" == "$releases_dir/$ref" ]] || return 1
      runtime_release_complete "$target" "$ref" || return 1
      if ! release_tree_permissions_trusted "$target"; then
        install_release "$ref" "$target" 0 0
      fi
      runtime_release_complete "$target" "$ref" && release_tree_permissions_trusted "$target" || return 1
    else
      [[ "$target" == "$manager_releases/$ref" ]] || return 1
      manager_release_complete "$target" "$ref" || return 1
      if ! release_tree_permissions_trusted "$target"; then
        source=$releases_dir/$ref
        install_release "$ref" "$source" 0 0 || return 1
        release_tree_permissions_trusted "$source" || return 1
        install_manager "$ref" "$source" "$target" 0 0 false || return 1
      fi
      manager_release_complete "$target" "$ref" && release_tree_permissions_trusted "$target" || return 1
    fi
    [[ "$kind" != release ]] || release_authority=$target
  done
  file=$checkpoint/current-link
  [[ -e "$file" || -L "$file" ]] || return 0
  [[ -f "$file" && ! -L "$file" && $(stat -c %u "$file") == "$expected_owner" \
    && $(stat -c %a "$file") == 600 && $(stat -c %s "$file") -ge 1 \
    && $(stat -c %s "$file") -le 4095 ]] || return 1
  if IFS= read -r -d '' target <"$file"; then return 1; fi
  [[ "$target" != *$'\n'* ]] || return 1
  source=$(raw_link_target_path "$target" "$current_link") || return 1
  if [[ ! -e "$source" && ! -L "$source" ]]; then
    [[ -n "$release_authority" ]] || return 1
    staged_link=$(mktemp "$checkpoint/.current-link.XXXXXX") || return 1
    if ! chmod 600 "$staged_link" || ! printf '%s' "$release_authority" >"$staged_link" \
      || [[ ! -f "$staged_link" || -L "$staged_link" || $(stat -c %u "$staged_link") != "$expected_owner" \
        || $(stat -c %a "$staged_link") != 600 || $(<"$staged_link") != "$release_authority" ]] \
      || ! mv -f -- "$staged_link" "$file"; then
      rm -f -- "$staged_link"
      return 1
    fi
    [[ -f "$file" && ! -L "$file" && $(stat -c %u "$file") == "$expected_owner" \
      && $(stat -c %a "$file") == 600 && $(<"$file") == "$release_authority" ]] || return 1
    return 0
  fi
  target=$(resolve_link_target "$target" "$current_link") || return 1
  target=$(canonical_release_target "$target" "$releases_dir") || return 1
  ref=${target##*/}
  runtime_release_complete "$target" "$ref" || return 1
  if ! release_tree_permissions_trusted "$target"; then
    install_release "$ref" "$target" 0 0
  fi
  runtime_release_complete "$target" "$ref" && release_tree_permissions_trusted "$target" || return 1
}

pending_policy_checkpoint() {
  local expected_owner=0
  [[ "$testing" != 1 ]] || expected_owner=$(id -u)
  python3 - "$network_policy_checkpoint" "$checkpoints_dir" "$expected_owner" <<'PY'
import json
import os
import stat
import sys

policy_dir, checkpoints_dir, expected_owner = sys.argv[1], sys.argv[2], int(sys.argv[3])
marker_path = os.path.join(policy_dir, "docker-network-policy.json")
if not os.path.lexists(marker_path):
    raise SystemExit(1)
try:
    marker_meta = os.lstat(marker_path)
    marker = json.load(open(marker_path, encoding="utf-8"))
except (OSError, ValueError, TypeError, json.JSONDecodeError):
    raise SystemExit(2)
if (
    not stat.S_ISREG(marker_meta.st_mode)
    or stat.S_ISLNK(marker_meta.st_mode)
    or marker_meta.st_uid != expected_owner
    or stat.S_IMODE(marker_meta.st_mode) != 0o600
):
    raise SystemExit(2)
if marker.get("phase") not in {"first-apply-pending", "reapply-pending", "removal-pending"}:
    raise SystemExit(1)
try:
    recoveries = [entry for entry in os.scandir(policy_dir) if entry.name.startswith("recovery.")]
    if len(recoveries) != 1:
        raise ValueError
    recovery = recoveries[0]
    recovery_meta = recovery.stat(follow_symlinks=False)
    metadata_path = os.path.join(recovery.path, "controller-checkpoint")
    metadata = os.lstat(metadata_path)
    if (
        not stat.S_ISDIR(recovery_meta.st_mode)
        or recovery_meta.st_uid != expected_owner
        or stat.S_IMODE(recovery_meta.st_mode) != 0o700
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != expected_owner
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size < 2
        or metadata.st_size > 4096
    ):
        raise ValueError
    raw_target = open(metadata_path, "rb").read()
    if raw_target.count(b"\n") != 1 or not raw_target.endswith(b"\n") or b"\0" in raw_target:
        raise ValueError
    target = os.fsdecode(raw_target[:-1])
    if (
        not target.startswith("/")
        or os.path.normpath(target) != target
        or os.path.realpath(target) != target
        or os.path.dirname(target) != checkpoints_dir
    ):
        raise ValueError
    checkpoints_meta = os.lstat(checkpoints_dir)
    target_meta = os.lstat(target)
    complete_meta = os.lstat(os.path.join(target, ".complete"))
    if (
        not stat.S_ISDIR(checkpoints_meta.st_mode)
        or checkpoints_meta.st_uid != expected_owner
        or stat.S_IMODE(checkpoints_meta.st_mode) != 0o700
        or not stat.S_ISDIR(target_meta.st_mode)
        or target_meta.st_uid != expected_owner
        or stat.S_IMODE(target_meta.st_mode) != 0o700
        or not stat.S_ISREG(complete_meta.st_mode)
        or complete_meta.st_uid != expected_owner
        or stat.S_IMODE(complete_meta.st_mode) != 0o600
        or complete_meta.st_size != 0
    ):
        raise ValueError
except (OSError, ValueError):
    raise SystemExit(2)
print(target)
PY
}

make_checkpoint() {
  local timestamp target unit timer final_checkpoint staged_checkpoint expected_owner=0 runner_id controller_id controller_live_id='' fallback_release=${1:-} fallback_ref fallback_candidate status manager_target='' manager_ref
  capture_current_pointer
  [[ "$testing" != 1 ]] || expected_owner=$(id -u)
  status=$(controller_status)
  if [[ -L "$manager_current" ]]; then
    if [[ "$mode" == uninstall && -z "$status" ]]; then
      manager_target=$(manager_release_from_raw_pointer || true)
    else
      manager_target=$(readlink -f "$manager_current" 2>/dev/null || true)
      [[ "$manager_target" == "$manager_releases/"* && -f "$manager_target/.ci-fleet-engine-ref" ]] || die 'manager current pointer is invalid'
      manager_ref=$(<"$manager_target/.ci-fleet-engine-ref")
      if [[ ! "$manager_ref" =~ ^[0-9a-f]{40}$ ]] || ! manager_release_complete "$manager_target" "$manager_ref" ||
        ! release_tree_permissions_trusted "$manager_target"; then
        die 'manager current pointer is invalid'
      fi
    fi
  elif [[ -e "$manager_current" ]]; then
    die 'manager current pointer is invalid'
  fi
  target=$(current_runtime_release)
  if [[ -z "$target" && -n "$fallback_release" ]]; then
    fallback_candidate=$(canonical_release_target "$fallback_release" "$releases_dir" || true)
    if [[ -n "$fallback_candidate" && -f "$fallback_candidate/.ci-fleet-engine-ref" ]]; then
      fallback_ref=$(<"$fallback_candidate/.ci-fleet-engine-ref")
      if [[ "$fallback_ref" =~ ^[0-9a-f]{40}$ ]] && runtime_release_complete "$fallback_candidate" "$fallback_ref" \
        && release_tree_permissions_trusted "$fallback_candidate"; then target=$fallback_candidate; fi
    fi
  fi
  [[ -n "$target" || ( -z "$status" && "$captured_current_state" != link ) ]] || die 'a trusted complete runtime release is required before controller mutation'
  timestamp=$(date -u +%Y%m%dT%H%M%SZ)
  final_checkpoint=$checkpoints_dir/${timestamp}-$$
  install -d -m 0700 "$checkpoints_dir"
  staged_checkpoint=$(mktemp -d "$checkpoints_dir/.checkpoint.staging.XXXXXX")
  staging_paths+=("$staged_checkpoint")
  checkpoint_dir=$staged_checkpoint
  install -d -m 0700 "$checkpoint_dir/systemd"
  printf '3\n' >"$checkpoint_dir/format-version"
  chmod 0600 "$checkpoint_dir/format-version"
  if [[ "$captured_current_state" == link ]]; then
    install -m 0600 "$temporary/current-link" "$checkpoint_dir/current-link"
  else
    : >"$checkpoint_dir/current-absent"
    chmod 0600 "$checkpoint_dir/current-absent"
  fi
  if [[ -f "$rendered_env" ]]; then
    [[ "$testing" != 1 ]] || expected_owner=$(id -u)
    [[ $(stat -c %u "$rendered_env") == "$expected_owner" && $(stat -c %a "$rendered_env") == 600 ]] || die "rendered environment must be owned by root with mode 0600: $rendered_env"
    install -m 0600 "$rendered_env" "$checkpoint_dir/ci-fleet.env"
    load_checkpoint_images "$checkpoint_dir/ci-fleet.env" || die 'installed image tags are invalid'
    if ! runner_id=$(docker image inspect --format '{{.Id}}' "${checkpoint_images[0]}" 2>/dev/null); then
      docker info >/dev/null 2>&1 || die 'Docker daemon is unavailable'
      runner_id=absent
    fi
    if ! controller_id=$(docker image inspect --format '{{.Id}}' "${checkpoint_images[1]}" 2>/dev/null); then
      docker info >/dev/null 2>&1 || die 'Docker daemon is unavailable'
      controller_id=absent
    fi
    if controller_live_id=$(docker inspect --format '{{.Image}}' "$controller_container" 2>/dev/null); then
      [[ "$controller_live_id" != "$controller_id" ]] || controller_live_id=
    else
      docker info >/dev/null 2>&1 || die 'Docker daemon is unavailable'
      controller_live_id=
    fi
    [[ "$runner_id" == absent || "$runner_id" =~ ^sha256:[0-9a-f]{64}$ ]] || die 'installed runner image ID is invalid'
    [[ "$controller_id" == absent || "$controller_id" =~ ^sha256:[0-9a-f]{64}$ ]] || die 'installed controller image ID is invalid'
    [[ -z "$controller_live_id" || "$controller_live_id" =~ ^sha256:[0-9a-f]{64}$ ]] || die 'live controller image ID is invalid'
    printf 'CI_FLEET_RUNNER_IMAGE_ID=%s\nCI_FLEET_CONTROLLER_IMAGE_ID=%s\n' "$runner_id" "$controller_id" >"$checkpoint_dir/image-ids.env"
    [[ -z "$controller_live_id" ]] || printf 'CI_FLEET_CONTROLLER_LIVE_IMAGE_ID=%s\n' "$controller_live_id" >>"$checkpoint_dir/image-ids.env"
    chmod 0600 "$checkpoint_dir/image-ids.env"
  fi
  [[ ! -f "$state_file" ]] || install -m 0600 "$state_file" "$checkpoint_dir/install-state.json"
  if [[ -n "$target" ]]; then
    printf '%s\n' "$target" >"$checkpoint_dir/release-target"
    chmod 0600 "$checkpoint_dir/release-target"
  fi
  if [[ -n "$manager_target" ]]; then
    printf '%s\n' "$manager_target" >"$checkpoint_dir/manager-target"
    chmod 0600 "$checkpoint_dir/manager-target"
  fi
  for unit in "${unit_names[@]}" "${optional_unit_names[@]}"; do
    [[ ! -f "$systemd_dir/$unit" ]] || install -m 0644 "$systemd_dir/$unit" "$checkpoint_dir/systemd/$unit"
  done
  : >"$checkpoint_dir/enabled-timers"
  : >"$checkpoint_dir/active-timers"
  for timer in "${timer_names[@]}"; do
    if systemctl is-enabled --quiet "$timer" 2>/dev/null; then printf '%s\n' "$timer" >>"$checkpoint_dir/enabled-timers"; fi
    if systemctl is-active --quiet "$timer" 2>/dev/null; then printf '%s\n' "$timer" >>"$checkpoint_dir/active-timers"; fi
  done
  local opt_name
  for opt_name in "${optional_unit_names[@]}"; do
    case "$opt_name" in *.timer)
      if systemctl is-enabled --quiet "$opt_name" 2>/dev/null; then printf '%s\n' "$opt_name" >>"$checkpoint_dir/enabled-timers"; fi
      if systemctl is-active --quiet "$opt_name" 2>/dev/null; then printf '%s\n' "$opt_name" >>"$checkpoint_dir/active-timers"; fi
    ;; esac
  done
  chmod 0600 "$checkpoint_dir/enabled-timers" "$checkpoint_dir/active-timers"
  : >"$checkpoint_dir/.complete"
  chmod 0600 "$checkpoint_dir/.complete"
  mv "$checkpoint_dir" "$final_checkpoint"
  checkpoint_dir=$final_checkpoint
  note "CHECKPOINT_CREATED path=$checkpoint_dir"
}

try_drain_current() {
  local deadline count old_release='' status paused=false force_nonterminal=${1:-false}
  local drain_env=${2:-$rendered_env} fallback_release=${3:-} shutdown_timeout=${CI_FLEET_DRAIN_TIMEOUT_SECONDS:-300}
  drain_error=
  status=$(controller_status)
  case "$status" in
    running|'') ;;
    exited|created|dead)
      [[ -f "$drain_env" ]] || { drain_error="cannot stop restartable controller state without its rendered environment: $status"; return 1; }
      old_release=$(current_runtime_release)
      [[ -n "$old_release" ]] || old_release=$fallback_release
      [[ -n "$old_release" ]] || { drain_error="cannot stop restartable controller state without its runtime release: $status"; return 1; }
      compose "$old_release" "$drain_env" stop --timeout "$shutdown_timeout" controller >/dev/null 2>&1 || { drain_error="failed to stop restartable controller state: $status"; return 1; }
      status=
      ;;
    *)
      if [[ "$force_nonterminal" != true ]]; then
        drain_error="cannot safely drain controller in non-terminal state: $status"
        return 1
      fi
      [[ -f "$drain_env" ]] || { drain_error='cannot stop a non-terminal candidate without its rendered environment'; return 1; }
      old_release=$(current_runtime_release)
      [[ -n "$old_release" ]] || old_release=$fallback_release
      [[ -n "$old_release" ]] || { drain_error='cannot stop a non-terminal candidate without its runtime release'; return 1; }
      if [[ "$status" == paused ]]; then
        compose "$old_release" "$drain_env" unpause controller >/dev/null 2>&1 || { drain_error="failed to unpause non-terminal candidate state: $status"; return 1; }
      fi
      compose "$old_release" "$drain_env" stop --timeout "$shutdown_timeout" controller >/dev/null 2>&1 || { drain_error="failed to stop non-terminal candidate state: $status"; return 1; }
      status=
      ;;
  esac
  if [[ "$status" == running ]]; then
    if [[ ! -f "$drain_env" ]]; then drain_error='cannot safely drain a running controller without its rendered environment'; return 1; fi
    old_release=$(current_runtime_release)
    [[ -n "$old_release" ]] || old_release=$fallback_release
    if [[ -z "$old_release" || ! -f "$old_release/deploy/compose.yaml" ]]; then drain_error='cannot locate the running controller Compose release for safe adoption'; return 1; fi
    if [[ $(docker inspect --format '{{.State.Paused}}' "$controller_container" 2>/dev/null || true) == true ]]; then
      paused=true
    elif compose "$old_release" "$drain_env" pause controller >/dev/null; then
      paused=true
    else
      drain_error='could not pause the controller for drain'
      return 1
    fi
  fi
  deadline=$((SECONDS + ${CI_FLEET_DRAIN_TIMEOUT_SECONDS:-300}))
  while :; do
    count=$(managed_runner_count)
    if [[ "$count" == 0 ]]; then break; fi
    if ((SECONDS >= deadline)); then
      if [[ "$paused" == true ]]; then compose "$old_release" "$drain_env" unpause controller >/dev/null || true; fi
      drain_error="drain timed out with $count managed runner(s) still present"
      return 1
    fi
    sleep 2
  done
  note 'DRAIN_READY managed_runners=0'
  if [[ "$status" == running ]]; then
    compose "$old_release" "$drain_env" kill --signal SIGTERM controller >/dev/null || {
      compose "$old_release" "$drain_env" unpause controller >/dev/null 2>&1 || true
      drain_error='failed to signal the paused controller for graceful scale-set cleanup'
      return 1
    }
    if [[ $(docker inspect --format '{{.State.Paused}}' "$controller_container" 2>/dev/null || true) == true ]]; then
      compose "$old_release" "$drain_env" unpause controller >/dev/null || {
        drain_error='failed to unpause the signaled controller for graceful shutdown'
        return 1
      }
    fi
    compose "$old_release" "$drain_env" stop --timeout "$shutdown_timeout" controller >/dev/null || {
      drain_error='could not stop the drained controller'
      return 1
    }
  fi
  if [[ "$force_nonterminal" == true && -n "$old_release" ]]; then
    compose "$old_release" "$drain_env" rm -f controller >/dev/null || {
      drain_error='could not remove the stopped candidate controller'
      return 1
    }
  fi
  note 'DRAIN_OK managed_runners=0'
}

drain_current() {
  try_drain_current "$@" || die "$drain_error"
}

install_systemd_units() {
  local source=${1:-$repo_root}
  install -d -m 0755 "$systemd_dir"
  install -m 0644 "$source/host/systemd/ci-fleet-health.service" "$systemd_dir/"
  install -m 0644 "$source/host/systemd/ci-fleet-health.timer" "$systemd_dir/"
  install -m 0644 "$source/host/systemd/ci-fleet-cleanup.service" "$systemd_dir/"
  install -m 0644 "$source/host/systemd/ci-fleet-cleanup.timer" "$systemd_dir/"
  install -m 0644 "$source/host/systemd/ci-fleet-drift.service" "$systemd_dir/"
  install -m 0644 "$source/host/systemd/ci-fleet-drift.timer" "$systemd_dir/"
  local unit
  for unit in "${optional_unit_names[@]}"; do
    [[ -f "$source/host/systemd/$unit" ]] && install -m 0644 "$source/host/systemd/$unit" "$systemd_dir/"
  done
  systemctl daemon-reload
}

remove_systemd_units() {
  systemctl disable --now "${timer_names[@]}" >/dev/null 2>&1 || true
  local unit
  for unit in "${optional_unit_names[@]}"; do
    case "$unit" in *.timer) systemctl disable --now "$unit" >/dev/null 2>&1 || true ;; esac
  done
  for unit in "${unit_names[@]}" "${optional_unit_names[@]}"; do rm -f "$systemd_dir/$unit"; done
  systemctl daemon-reload
}

run_health_check() {
  local release=$1 environment=$2 bootstrap=${3:-false} result=0
  (
    trap - ERR
    local testing_value=${CI_FLEET_TESTING:-} root_value=${CI_FLEET_ROOT_PREFIX:-} variable
    while IFS= read -r variable; do unset "$variable"; done < <(compgen -A variable CI_FLEET_)
    [[ -z "$testing_value" ]] || export CI_FLEET_TESTING=$testing_value
    [[ -z "$root_value" ]] || export CI_FLEET_ROOT_PREFIX=$root_value
    set -a
    # shellcheck disable=SC1090
    . "$environment"
    set +a
    [[ "$bootstrap" != true ]] || export CI_FLEET_HEALTH_BOOTSTRAP=1
    export CI_FLEET_HEALTH_SUPPRESS_DELIVERY=1
    "$release/scripts/healthcheck.sh"
  ) || result=$?
  ((result < 2))
}

display_last_health() {
  if [[ ! -f "$health_report" ]]; then
    note 'HEALTH last=missing'
    return
  fi
  python3 - "$health_report" <<'PY'
import json
import sys
try:
    report = json.load(open(sys.argv[1], encoding="utf-8"))
    status = report["status"]
    timestamp = int(report["timestamp"])
    if status not in {"healthy", "warning", "unhealthy", "maintenance"}:
        raise ValueError
except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
    print("HEALTH last=invalid")
else:
    print(f"HEALTH last={status} timestamp={timestamp}")
PY
}

activate_candidate() {
  local check_health=${1:-true} staged_state
  [[ "$target_state" == active ]] || remove_inactive_managed_runners
  install -d -m 0700 "$etc_dir" "$state_root" "$checkpoints_dir"
  install -m 0600 "$candidate_env" "$rendered_env"
  ln -sfn "$release_dir" "$temporary/current"
  mv -Tf "$temporary/current" "$current_link"
  install_manager
  install_systemd_units "$(readlink -f "$manager_current")"
  if [[ "$target_state" == active ]]; then
    compose "$release_dir" "$rendered_env" up -d --no-deps controller
    sleep "${CI_FLEET_STARTUP_WAIT_SECONDS:-2}"
    runtime_matches active || die 'controller did not remain running after activation'
  else
    compose "$release_dir" "$rendered_env" stop controller >/dev/null 2>&1 || true
    if ! runtime_matches "$target_state"; then
      compose "$release_dir" "$rendered_env" down --remove-orphans >/dev/null
      runtime_matches "$target_state" || die 'controller did not reach the requested non-active state'
    fi
  fi
  if [[ "$check_health" == true ]] && ! run_health_check "$release_dir" "$rendered_env" true; then
    die 'post-activation health check failed'
  fi
  staged_state=$(mktemp "$state_root/.install-state.XXXXXX")
  staging_paths+=("$staged_state")
  python3 - "$candidate_metadata" "$staged_state" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" <<'PY'
import json
import sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
value["installed_at"] = sys.argv[3]
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    json.dump(value, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
  chmod 0600 "$staged_state"
  mv -f "$staged_state" "$state_file"
  systemctl enable --now "${timer_names[@]}" >/dev/null
  local opt_timer
  for opt_timer in "${optional_unit_names[@]}"; do
    case "$opt_timer" in *.timer)
      # Only enable remote reconciliation timers when config is
      # identified as an OWNER/REPO (not a local checkout path)
      if [[ "$config_identity" == *"/"* && "$config_identity" != "/"* ]]; then
        if [[ "$mode" == install && ! -f "$checkpoint_dir/install-state.json" && ! -f "$checkpoint_dir/ci-fleet.env" ]] || grep -Fxq "$opt_timer" "$checkpoint_dir/enabled-timers"; then
          systemctl enable --now "$opt_timer" >/dev/null
        elif [[ -f "$systemd_dir/$opt_timer" ]]; then systemctl disable --now "$opt_timer" >/dev/null; fi
      elif [[ -f "$systemd_dir/$opt_timer" ]]; then
        # Local checkout path — disable and stop any previously enabled timer
        systemctl disable --now "$opt_timer" >/dev/null
      fi
    ;; esac
  done
}

restore_systemd_snapshot() {
  local unit timer failed=0
  remove_systemd_units || failed=1
  for unit in "${unit_names[@]}"; do
    [[ ! -f "$checkpoint_dir/systemd/$unit" ]] || install -m 0644 "$checkpoint_dir/systemd/$unit" "$systemd_dir/$unit" || failed=1
  done
  for unit in "${optional_unit_names[@]}"; do
    [[ ! -f "$checkpoint_dir/systemd/$unit" ]] || install -m 0644 "$checkpoint_dir/systemd/$unit" "$systemd_dir/$unit" || failed=1
  done
  systemctl daemon-reload || failed=1
  for timer in "${timer_names[@]}"; do
    if grep -Fxq "$timer" "$checkpoint_dir/enabled-timers"; then systemctl enable "$timer" >/dev/null || failed=1; else systemctl disable "$timer" >/dev/null 2>&1 || true; fi
    if grep -Fxq "$timer" "$checkpoint_dir/active-timers"; then systemctl start "$timer" || failed=1; else systemctl stop "$timer" >/dev/null 2>&1 || true; fi
  done
  local opt_name
  for opt_name in "${optional_unit_names[@]}"; do
    case "$opt_name" in *.timer)
      if grep -Fxq "$opt_name" "$checkpoint_dir/enabled-timers"; then systemctl enable "$opt_name" >/dev/null || failed=1; else systemctl disable "$opt_name" >/dev/null 2>&1 || true; fi
      if grep -Fxq "$opt_name" "$checkpoint_dir/active-timers"; then systemctl start "$opt_name" || failed=1; else systemctl stop "$opt_name" >/dev/null 2>&1 || true; fi
    ;; esac
  done
  return "$failed"
}

restore_checkpoint() {
  local target restored_state actual index runtime_target expected_owner=0 failed=0 checkpoint_release='' drain_env=$rendered_env drain_release='' restore_images=false new_format=false checkpoint_format='' current_temporary='' validated_current_target='' validated_manager_target=''
  local restore_controller_tag_after_start=false
  local format_marker=$checkpoint_dir/format-version image_ids=$checkpoint_dir/image-ids.env
  [[ -n "$checkpoint_dir" && -d "$checkpoint_dir" ]] || return 1
  [[ "$testing" != 1 ]] || expected_owner=$(id -u)
  if [[ -e "$format_marker" || -L "$format_marker" ]]; then
    if [[ -L "$format_marker" || ! -f "$format_marker" || $(stat -c %u "$format_marker") != "$expected_owner" \
      || $(stat -c %a "$format_marker") != 600 || $(stat -c %s "$format_marker") != 2 ]]; then
      note 'ROLLBACK_FAILED reason=checkpoint format marker is invalid'
      return 1
    fi
    checkpoint_format=$(<"$format_marker")
    if [[ "$checkpoint_format" != 2 && "$checkpoint_format" != 3 ]]; then
      note 'ROLLBACK_FAILED reason=checkpoint format marker is invalid'
      return 1
    fi
    new_format=true
  elif [[ -e "$image_ids" || -L "$image_ids" ]]; then
    note 'ROLLBACK_FAILED reason=checkpoint image mappings are invalid'
    return 1
  elif [[ -f "$checkpoint_dir/ci-fleet.env" ]]; then
    note 'ROLLBACK_LEGACY_IMAGE_STATE_UNVERIFIED'
  fi
  if [[ -e "$checkpoint_dir/fallback-release" || -L "$checkpoint_dir/fallback-release" ]]; then
    note 'ROLLBACK_FAILED reason=checkpoint fallback release is unsupported'
    return 1
  fi
  if [[ "$checkpoint_format" == 3 ]]; then
    if [[ -f "$checkpoint_dir/current-link" && ! -L "$checkpoint_dir/current-link" && ! -e "$checkpoint_dir/current-absent" && ! -L "$checkpoint_dir/current-absent" ]]; then
      validated_current_target=$temporary/validated-current-link
      if ! python3 - "$checkpoint_dir/current-link" "$validated_current_target" "$expected_owner" <<'PY'
import os
import stat
import sys

source, destination = map(os.fsencode, sys.argv[1:3])
metadata = os.lstat(source)
if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != int(sys.argv[3]) or stat.S_IMODE(metadata.st_mode) != 0o600:
    raise SystemExit(1)
with open(source, "rb") as checkpoint:
    target = checkpoint.read(4096)
if not target or len(target) > 4095 or b"\0" in target:
    raise SystemExit(1)
descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "wb") as output:
    output.write(target)
PY
      then
        note 'ROLLBACK_FAILED reason=checkpoint current state is invalid'
        return 1
      fi
    elif [[ ! -e "$checkpoint_dir/current-link" && ! -L "$checkpoint_dir/current-link" \
      && -f "$checkpoint_dir/current-absent" && ! -L "$checkpoint_dir/current-absent" \
      && $(stat -c %u "$checkpoint_dir/current-absent") == "$expected_owner" \
      && $(stat -c %a "$checkpoint_dir/current-absent") == 600 && $(stat -c %s "$checkpoint_dir/current-absent") == 0 ]]; then
      :
    else
      note 'ROLLBACK_FAILED reason=checkpoint current state is invalid'
      return 1
    fi
  fi
  if [[ -e "$checkpoint_dir/release-target" || -L "$checkpoint_dir/release-target" ]]; then
    if [[ -L "$checkpoint_dir/release-target" || ! -f "$checkpoint_dir/release-target" \
      || $(stat -c %u "$checkpoint_dir/release-target") != "$expected_owner" || $(stat -c %a "$checkpoint_dir/release-target") != 600 \
      || $(stat -c %s "$checkpoint_dir/release-target") -lt 2 || $(stat -c %s "$checkpoint_dir/release-target") -gt 4096 \
      || $(wc -l <"$checkpoint_dir/release-target") != 1 ]]; then
      note 'ROLLBACK_FAILED reason=checkpoint release target is invalid'
      return 1
    fi
    checkpoint_release=$(<"$checkpoint_dir/release-target")
    if [[ ! -f "$checkpoint_release/.ci-fleet-engine-ref" ]]; then
      note 'ROLLBACK_FAILED reason=checkpoint release target is invalid'
      return 1
    fi
    target=$(<"$checkpoint_release/.ci-fleet-engine-ref")
    if [[ ! "$target" =~ ^[0-9a-f]{40}$ ]] || ! runtime_release_complete "$checkpoint_release" "$target" ||
      ! release_tree_permissions_trusted "$checkpoint_release"; then
      note 'ROLLBACK_FAILED reason=checkpoint release target is invalid'
      return 1
    fi
    runtime_target=$(canonical_release_target "$checkpoint_release" "$releases_dir" || true)
    if [[ -z "$runtime_target" ]]; then
      if [[ "$checkpoint_release" != "$manager_releases/"* || -L "$checkpoint_release" ]]; then
        note 'ROLLBACK_FAILED reason=checkpoint release target is invalid'
        return 1
      fi
      runtime_target=$releases_dir/$target
      if ! install_release "$target" "$runtime_target" 0 0; then
        note 'ROLLBACK_FAILED reason=checkpoint release target normalization failed'
        return 1
      fi
    fi
    checkpoint_release=$runtime_target
  fi

  if [[ -e "$checkpoint_dir/manager-target" || -L "$checkpoint_dir/manager-target" ]]; then
    if [[ -L "$checkpoint_dir/manager-target" || ! -f "$checkpoint_dir/manager-target" \
      || $(stat -c %u "$checkpoint_dir/manager-target") != "$expected_owner" || $(stat -c %a "$checkpoint_dir/manager-target") != 600 \
      || $(stat -c %s "$checkpoint_dir/manager-target") -gt 4096 ]]; then
      note 'ROLLBACK_FAILED reason=checkpoint manager target is invalid'
      return 1
    fi
    target=$(<"$checkpoint_dir/manager-target")
    if [[ "$target" != "$manager_releases/"* || ! -f "$target/.ci-fleet-engine-ref" ]]; then
      note 'ROLLBACK_FAILED reason=checkpoint manager target is invalid'
      return 1
    fi
    restored_state=$(<"$target/.ci-fleet-engine-ref")
    if [[ ! "$restored_state" =~ ^[0-9a-f]{40}$ ]] || ! manager_release_complete "$target" "$restored_state" ||
      ! release_tree_permissions_trusted "$target"; then
      note 'ROLLBACK_FAILED reason=checkpoint manager target is invalid'
      return 1
    fi
    validated_manager_target=$manager_releases/$restored_state
    if [[ "$target" != "$validated_manager_target" ]]; then
      if ! install_manager "$restored_state" "$target" "$validated_manager_target" 0 0 false; then
        note 'ROLLBACK_FAILED reason=checkpoint manager target normalization failed'
        return 1
      fi
    fi
  fi
  if [[ -n "$validated_current_target" ]]; then
    if IFS= read -r -d '' restored_state <"$validated_current_target"; then
      note 'ROLLBACK_FAILED reason=checkpoint current target is invalid'
      return 1
    fi
    source=$(raw_link_target_path "$restored_state" "$current_link") || {
      note 'ROLLBACK_FAILED reason=checkpoint current target is invalid'
      return 1
    }
    if [[ ! -e "$source" && ! -L "$source" && -n "$checkpoint_release" ]]; then
      printf '%s' "$checkpoint_release" >"$validated_current_target"
    else
      if [[ "$restored_state" == *$'\n'* ]]; then
        note 'ROLLBACK_FAILED reason=checkpoint current target is invalid'
        return 1
      fi
      restored_state=$(resolve_link_target "$restored_state" "$current_link") || {
        note 'ROLLBACK_FAILED reason=checkpoint current target is invalid'
        return 1
      }
      target=$(canonical_release_target "$restored_state" "$releases_dir") || {
        note 'ROLLBACK_FAILED reason=checkpoint current target is invalid'
        return 1
      }
      restored_state=${target##*/}
      if ! runtime_release_complete "$target" "$restored_state" || ! release_tree_permissions_trusted "$target"; then
        note 'ROLLBACK_FAILED reason=checkpoint current target is invalid'
        return 1
      fi
      printf '%s' "$target" >"$validated_current_target" || {
        note 'ROLLBACK_FAILED reason=checkpoint current target normalization failed'
        return 1
      }
      [[ $(<"$validated_current_target") == "$target" ]] || {
        note 'ROLLBACK_FAILED reason=checkpoint current target normalization failed'
        return 1
      }
    fi
  fi
  if $new_format && [[ -f "$checkpoint_dir/ci-fleet.env" ]]; then
    if [[ ! -f "$image_ids" || -L "$image_ids" || $(stat -c %u "$image_ids") != "$expected_owner" || $(stat -c %a "$image_ids") != 600 ]] \
      || ! load_checkpoint_images "$checkpoint_dir/ci-fleet.env" "$image_ids"; then
      note 'ROLLBACK_FAILED reason=checkpoint image mappings are invalid'
      return 1
    fi
    restore_images=true
  elif $new_format && [[ -e "$image_ids" || -L "$image_ids" ]]; then
    note 'ROLLBACK_FAILED reason=checkpoint image mappings are invalid'
    return 1
  fi
  drain_release=$(current_runtime_release)
  [[ -n "$drain_release" ]] || drain_release=$checkpoint_release
  if [[ -f "$rendered_env" ]]; then
    load_installed_controller_identity "$temporary/no-install-state" "$rendered_env"
  elif [[ -f "$checkpoint_dir/install-state.json" || -f "$checkpoint_dir/ci-fleet.env" ]]; then
    load_installed_controller_identity "$checkpoint_dir/install-state.json" "$checkpoint_dir/ci-fleet.env"
    drain_env=$checkpoint_dir/ci-fleet.env
    [[ -n "$drain_release" ]] || drain_release=$checkpoint_release
  fi
  if ! try_drain_current true "$drain_env" "$drain_release"; then
    note "ROLLBACK_FAILED reason=$drain_error"
    return 1
  fi
  if ! remove_inactive_managed_runners; then
    note 'ROLLBACK_FAILED reason=could not remove inactive managed runners'
    return 1
  fi
  if [[ -f "$checkpoint_dir/install-state.json" || -f "$checkpoint_dir/ci-fleet.env" ]]; then
    load_installed_controller_identity "$checkpoint_dir/install-state.json" "$checkpoint_dir/ci-fleet.env"
  fi
  trap - ERR
  set +e
  if [[ -f "$checkpoint_dir/ci-fleet.env" ]]; then
    install -m 0600 "$checkpoint_dir/ci-fleet.env" "$rendered_env" || failed=1
  else
    rm -f "$rendered_env" || failed=1
  fi
  if [[ -f "$checkpoint_dir/install-state.json" ]]; then
    install -m 0600 "$checkpoint_dir/install-state.json" "$state_file" || failed=1
  else
    rm -f "$state_file" || failed=1
  fi
  release_dir=$checkpoint_release
  if [[ "$checkpoint_format" == 3 && -n "$validated_current_target" ]]; then
    current_temporary=$install_root/.current.rollback.$$
    staging_paths+=("$current_temporary")
    python3 - "$validated_current_target" "$current_temporary" <<'PY' && mv -Tf "$current_temporary" "$current_link" || failed=1
import os
import sys

source, destination = map(os.fsencode, sys.argv[1:])
with open(source, "rb") as checkpoint:
    target = checkpoint.read()
os.symlink(target, destination)
PY
  elif [[ "$checkpoint_format" == 3 ]]; then
    rm -f "$current_link" || failed=1
  elif [[ -n "$checkpoint_release" ]]; then
    ln -sfn "$checkpoint_release" "$temporary/rollback-current" && mv -Tf "$temporary/rollback-current" "$current_link" || failed=1
  elif [[ "$checkpoint_format" == 2 ]]; then
    rm -f "$current_link" || failed=1
  else
    if [[ ! -L "$current_link" || -e "$current_link" ]]; then rm -f "$current_link" || failed=1; fi
  fi
  if [[ -n "$validated_manager_target" ]]; then
    ln -sfn "$validated_manager_target" "$temporary/rollback-manager" && mv -Tf "$temporary/rollback-manager" "$manager_current" || failed=1
  else
    rm -f "$manager_current" || failed=1
  fi
  if $restore_images; then
    for index in 0 1; do
      if [[ "$index" == 1 && ${#checkpoint_images[@]} == 5 ]]; then
        docker image tag "${checkpoint_images[4]}" "${checkpoint_images[index]}" || failed=1
        actual=$(docker image inspect --format '{{.Id}}' "${checkpoint_images[index]}" 2>/dev/null) || failed=1
        [[ "$actual" == "${checkpoint_images[4]}" ]] || failed=1
        restore_controller_tag_after_start=true
      elif [[ ${checkpoint_images[index + 2]} == absent ]]; then
        if docker image inspect --format '{{.Id}}' "${checkpoint_images[index]}" >/dev/null 2>&1; then
          docker image rm "${checkpoint_images[index]}" >/dev/null || failed=1
        else
          docker info >/dev/null 2>&1 || failed=1
        fi
        if docker image inspect --format '{{.Id}}' "${checkpoint_images[index]}" >/dev/null 2>&1; then
          failed=1
        else
          docker info >/dev/null 2>&1 || failed=1
        fi
      else
        docker image tag "${checkpoint_images[index + 2]}" "${checkpoint_images[index]}" || failed=1
        actual=$(docker image inspect --format '{{.Id}}' "${checkpoint_images[index]}" 2>/dev/null) || failed=1
        [[ "$actual" == "${checkpoint_images[index + 2]}" ]] || failed=1
      fi
    done
  fi
  restore_systemd_snapshot || failed=1
  if [[ -n "$release_dir" && -f "$rendered_env" ]]; then
    restored_state=$(awk -F= '$1 == "CI_FLEET_CONTROLLER_STATE" {print $2}' "$rendered_env") || failed=1
    [[ "$restored_state" == active || "$restored_state" == drained || "$restored_state" == disabled ]] || failed=1
    if [[ "$restored_state" == active && "$failed" == 0 ]]; then
      if [[ $(managed_runner_count) != 0 ]]; then
        failed=1
      else
        compose "$release_dir" "$rendered_env" up -d --no-deps controller || failed=1
        if ((failed == 0)); then run_health_check "$release_dir" "$rendered_env" true || failed=1; fi
      fi
    fi
  fi
  if $restore_controller_tag_after_start && ((failed == 0)); then
    if [[ ${checkpoint_images[3]} == absent ]]; then
      docker image rm --force "${checkpoint_images[1]}" >/dev/null || failed=1
      if docker image inspect --format '{{.Id}}' "${checkpoint_images[1]}" >/dev/null 2>&1; then
        failed=1
      else
        docker info >/dev/null 2>&1 || failed=1
      fi
    else
      docker image tag "${checkpoint_images[3]}" "${checkpoint_images[1]}" || failed=1
      actual=$(docker image inspect --format '{{.Id}}' "${checkpoint_images[1]}" 2>/dev/null) || failed=1
      [[ "$actual" == "${checkpoint_images[3]}" ]] || failed=1
    fi
  fi
  set -e
  trap on_error ERR
  if ((failed != 0)); then
    note "ROLLBACK_FAILED checkpoint=$checkpoint_dir"
    return 1
  fi
  note "ROLLBACK_RESTORED checkpoint=$checkpoint_dir"
}

load_policy_action_context() {
  local path expected_owner=0
  [[ ${CI_FLEET_INSTALLER_LOCK_FD:-} == 9 ]] || die 'policy actions require the inherited installer lock'
  [[ "$policy_action" =~ ^(drain|rollback-drain|resume|restore|health)$ ]] || die 'unknown policy adapter action'
  [[ "$testing" != 1 ]] || expected_owner=$(id -u)
  release_dir=${CI_FLEET_POLICY_RELEASE:-}
  candidate_metadata=${CI_FLEET_POLICY_METADATA:-}
  checkpoint_dir=${CI_FLEET_POLICY_CHECKPOINT:-}
  candidate_env=${CI_FLEET_POLICY_ENV:-}
  config_identity=${CI_FLEET_POLICY_CONFIG_IDENTITY:-}
  engine_ref=${CI_FLEET_POLICY_ENGINE_REF:-}
  status_reporting_required=${CI_FLEET_POLICY_STATUS_REQUIRED:-0}
  status_reporting_configured=${CI_FLEET_POLICY_STATUS_CONFIGURED:-0}
  build_before_drain=${CI_FLEET_POLICY_PREBUILT:-false}
  for path in "$candidate_env" "$candidate_metadata"; do
    [[ "$path" == /* && -f "$path" && ! -L "$path" && $(stat -c %u "$path") == "$expected_owner" ]] || die 'policy action context is invalid'
  done
  [[ "$checkpoint_dir" == /* && -d "$checkpoint_dir" && ! -L "$checkpoint_dir" && $(stat -c %u "$checkpoint_dir") == "$expected_owner" && $(stat -c %a "$checkpoint_dir") == 700 ]] || die 'policy action checkpoint is invalid'
  [[ "$engine_ref" =~ ^[0-9a-f]{40}$ ]] || die 'policy action engine ref is invalid'
  runtime_release_complete "$release_dir" "$engine_ref" "$status_reporting_required" "$status_reporting_configured" || die 'policy action release is invalid'
  controller_id=$(awk -F= '$1 == "CI_FLEET_INSTANCE" {count++; value=substr($0, index($0, "=") + 1)} END {if (count != 1) exit 1; print value}' "$candidate_env") || die 'policy action controller identity is invalid'
  target_state=$(awk -F= '$1 == "CI_FLEET_CONTROLLER_STATE" {count++; value=substr($0, index($0, "=") + 1)} END {if (count != 1) exit 1; print value}' "$candidate_env") || die 'policy action controller state is invalid'
  [[ "$controller_id" =~ ^[a-z0-9][a-z0-9-]{0,62}$ && "$target_state" =~ ^(active|drained|disabled)$ ]] || die 'policy action candidate is invalid'
}

perform_policy_action() {
  local health_release
  load_policy_action_context
  case "$policy_action" in
    drain)
      if [[ -f "$state_file" || -f "$rendered_env" ]]; then load_installed_controller_identity; fi
      drain_current false "$rendered_env" "$release_dir"
      ;;
    rollback-drain)
      if [[ -f "$state_file" || -f "$rendered_env" ]]; then load_installed_controller_identity; fi
      drain_current true "$rendered_env" "$release_dir"
      ;;
    resume)
      run_candidate_preflight
      if [[ "$build_before_drain" != true ]]; then build_candidate; fi
      mode=${CI_FLEET_POLICY_MODE:-upgrade}
      activate_candidate false
      ;;
    restore)
      restore_checkpoint || die 'checkpoint restoration failed'
      ;;
    health)
      health_release=$(current_runtime_release)
      [[ -n "$health_release" ]] || health_release=$release_dir
      run_health_check "$health_release" "$candidate_env" true || die 'policy action health check failed'
      ;;
  esac
}

rollback_and_exit() {
  local status=$1 restored=false
  trap - ERR
  trap '' TERM
  if $transaction_active; then
    if restore_checkpoint; then restored=true; fi
    transaction_active=false
    if transaction_result_enabled; then
      if [[ "$restored" == true ]]; then
        write_transaction_result rollback_verified
        status=20
      else
        write_transaction_result rollback_unverified
        status=21
      fi
    fi
  fi
  trap - ERR
  exit "$status"
}
on_error() {
  local status=$?
  rollback_and_exit "$status"
}
on_term() { rollback_and_exit 143; }
on_policy_term() {
  policy_wait_interrupted=true
  policy_term_pending=true
  [[ -z ${policy_pid:-} ]] || kill -TERM "$policy_pid" 2>/dev/null || true
}
trap on_error ERR
trap on_term TERM

perform_check() {
  local count
  display_last_health
  drift_count
  count=$DRIFT_COUNT
  if ((count > 0)); then
    note "CHECK_FAILED drift=$count"
    exit 3
  fi
  note "CHECK_OK controller=$controller_id config_ref=$config_ref engine_ref=$engine_ref state=$target_state"
}

perform_converge() {
  local count existing_status candidate_runner_image candidate_controller_image installed_runner_image installed_controller_image live_runner_image expected_owner=0 current_target current_ref manager_target manager_ref manager_source policy_status policy_env policy_metadata pending_checkpoint pending_checkpoint_status policy_pid='' policy_wait_interrupted policy_term_pending=false
  local desired_controller_id=$controller_id build_before_drain=false
  if [[ "$mode" == upgrade && ! -f "$state_file" ]]; then
    die '--upgrade requires an existing managed installation; use --install or --adopt'
  fi
  if ! docker_network_policy_matches && ! docker_daemon_config_trusted; then
    die 'failed to stage Docker network policy'
  fi
  existing_status=$(controller_status)
  if [[ "$mode" == adopt && ! -f "$rendered_env" && ! -f "$state_file" && -z "$existing_status" ]]; then
    die '--adopt requires an existing controller or configuration; use --install for a fresh host'
  fi
  if [[ "$mode" == install && -f "$rendered_env" && ! -f "$state_file" ]]; then
    die 'an unmanaged controller configuration exists; use --adopt'
  fi
  if [[ -L "$current_link" ]]; then
    current_target=$(canonical_release_target_from_raw_pointer "$current_link" "$releases_dir" || true)
    if [[ -n "$current_target" ]]; then
      current_ref=${current_target##*/}
      if ! runtime_release_complete "$current_target" "$current_ref"; then
        [[ "$mode" == install ]] || die 'current release is incomplete; operator recovery or reinstall required'
        install_release "$current_ref" "$current_target" 0 0
      elif ! release_tree_permissions_trusted "$current_target"; then
        install_release "$current_ref" "$current_target" 0 0
      fi
    elif raw_pointer_target_exists "$current_link"; then
      die 'current pointer is invalid'
    fi
  fi
  if [[ -L "$manager_current" ]]; then
    manager_target=$(canonical_release_target_from_raw_pointer "$manager_current" "$manager_releases" || true)
    [[ -n "$manager_target" ]] || die 'manager current pointer is invalid'
    manager_ref=${manager_target##*/}
    if [[ "$mode" == upgrade ]] && release_tree_permissions_trusted "$manager_target"; then
      CI_FLEET_INSTALLER_LOCK_FD=9 "$repo_root/scripts/repair-manager-bytecode-drift.py" --lock-file "$lock_file" "$manager_current" \
        || die 'manager current pointer is invalid'
    elif ! manager_release_complete "$manager_target" "$manager_ref"; then
      [[ "$mode" == install ]] || die 'manager current pointer is incomplete; operator recovery or reinstall required'
      manager_source=$releases_dir/$manager_ref
      install_release "$manager_ref" "$manager_source" 0 0
      install_manager "$manager_ref" "$manager_source" "$manager_target" 0 0 false
    elif ! release_tree_permissions_trusted "$manager_target"; then
      manager_source=$releases_dir/$manager_ref
      install_release "$manager_ref" "$manager_source" 0 0
      install_manager "$manager_ref" "$manager_source" "$manager_target" 0 0 false
    fi
    if ! manager_release_complete "$manager_target" "$manager_ref" || ! release_tree_permissions_trusted "$manager_target"; then
      die 'manager current pointer is invalid'
    fi
  elif [[ -e "$manager_current" ]]; then
    die 'manager current pointer is invalid'
  fi
  drift_count
  count=$DRIFT_COUNT
  if ((count == 0)); then
    note "NO_CHANGE controller=$controller_id config_ref=$config_ref engine_ref=$engine_ref state=$target_state"
    return
  fi
  capture_current_pointer
  install_release
  compose "$release_dir" "$candidate_env" config --quiet
  candidate_runner_image=$(awk -F= '$1 == "CI_FLEET_RUNNER_IMAGE" {count++; value=substr($0, index($0, "=") + 1)} END {if (count != 1) exit 1; print value}' "$candidate_env") || die 'rendered candidate runner image is invalid'
  candidate_controller_image=$(awk -F= '$1 == "CI_FLEET_CONTROLLER_IMAGE" {count++; value=substr($0, index($0, "=") + 1)} END {if (count != 1) exit 1; print value}' "$candidate_env") || die 'rendered candidate controller image is invalid'
  [[ "$testing" != 1 ]] || expected_owner=$(id -u)
  case "$existing_status" in
    '')
      [[ "$mode" == install && ! -f "$rendered_env" && ! -f "$state_file" ]] && build_before_drain=true
      ;;
    running|exited|created|dead)
      if [[ -f "$rendered_env" && $(stat -c %u "$rendered_env") == "$expected_owner" && $(stat -c %a "$rendered_env") == 600 ]] \
        && installed_runner_image=$(awk -F= '$1 == "CI_FLEET_RUNNER_IMAGE" {count++; value=substr($0, index($0, "=") + 1)} END {if (count != 1) exit 1; print value}' "$rendered_env") \
        && installed_controller_image=$(awk -F= '$1 == "CI_FLEET_CONTROLLER_IMAGE" {count++; value=substr($0, index($0, "=") + 1)} END {if (count != 1) exit 1; print value}' "$rendered_env") \
        && live_runner_image=$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$controller_container" 2>/dev/null | awk -F= '$1 == "CI_FLEET_RUNNER_IMAGE" {count++; value=substr($0, index($0, "=") + 1)} END {if (count != 1) exit 1; print value}') \
        && [[ -n "$installed_runner_image" && -n "$installed_controller_image" && -n "$live_runner_image" ]]; then
        if [[ "$candidate_runner_image" != "$installed_runner_image" && "$candidate_runner_image" != "$live_runner_image" \
          && "$candidate_controller_image" != "$installed_controller_image" ]]; then build_before_drain=true; fi
      fi
      ;;
  esac
  if $build_before_drain; then build_candidate; require_commands; fi
  pending_checkpoint_status=0
  pending_checkpoint=$(pending_policy_checkpoint) || pending_checkpoint_status=$?
  case "$pending_checkpoint_status" in
    0)
      repair_pending_checkpoint_authority "$pending_checkpoint" || die 'pending network-policy controller checkpoint is invalid'
      checkpoint_dir=$pending_checkpoint
      ;;
    1) make_checkpoint "$release_dir" ;;
    *) die 'pending network-policy controller checkpoint is invalid' ;;
  esac
  transaction_active=true
  if [[ "$DOCKER_NETWORK_POLICY_DRIFT" == true ]]; then
    policy_env=$(mktemp "$state_root/.policy-candidate-env.XXXXXX")
    policy_metadata=$(mktemp "$state_root/.policy-candidate-metadata.XXXXXX")
    staging_paths+=("$policy_env" "$policy_metadata")
    install -m 0600 "$candidate_env" "$policy_env"
    install -m 0600 "$candidate_metadata" "$policy_metadata"
    export CI_FLEET_DOCKER_DAEMON_CONFIG=$docker_daemon_config
    export CI_FLEET_DOCKER_NETWORK_POLICY_ADAPTER=$release_dir/scripts/docker-network-policy-adapter.sh
    export CI_FLEET_INSTALLER_LOCK=$lock_file
    export CI_FLEET_POLICY_INSTALLER=$release_dir/scripts/install-worker-controller.sh
    export CI_FLEET_POLICY_RELEASE=$release_dir
    export CI_FLEET_POLICY_ENV=$policy_env
    export CI_FLEET_POLICY_METADATA=$policy_metadata
    export CI_FLEET_POLICY_CHECKPOINT=$checkpoint_dir
    export CI_FLEET_POLICY_CONFIG_IDENTITY=$config_identity
    export CI_FLEET_POLICY_ENGINE_REF=$engine_ref
    export CI_FLEET_POLICY_STATUS_REQUIRED=$status_reporting_required
    export CI_FLEET_POLICY_STATUS_CONFIGURED=$status_reporting_configured
    export CI_FLEET_POLICY_PREBUILT=$build_before_drain
    export CI_FLEET_POLICY_MODE=$mode
    policy_status=0
    trap on_policy_term TERM
    if transaction_result_enabled; then
      "$release_dir/scripts/apply-docker-network-policy.sh" --env "$policy_env" --checkpoint "$network_policy_checkpoint" &
    else
      CI_FLEET_TRANSACTION_RESULT_FD=7 \
        "$release_dir/scripts/apply-docker-network-policy.sh" --env "$policy_env" --checkpoint "$network_policy_checkpoint" \
        7>/dev/null &
    fi
    policy_pid=$!
    if $policy_term_pending; then kill -TERM "$policy_pid" 2>/dev/null || true; fi
    while :; do
      policy_wait_interrupted=false
      if wait "$policy_pid"; then policy_status=0; else policy_status=$?; fi
      if [[ "$policy_wait_interrupted" != true ]]; then
        break
      fi
      kill -0 "$policy_pid" 2>/dev/null || break
    done
    transaction_active=false
    trap on_term TERM
    if ((policy_status == 0)); then
      note "CONVERGED mode=$mode controller=$controller_id config_ref=$config_ref engine_ref=$engine_ref state=$target_state"
      return
    fi
    if ((policy_status == 20)); then
      note "ROLLBACK_RESTORED checkpoint=$checkpoint_dir"
      return 20
    fi
    if docker_network_policy_matches; then
      write_transaction_result applied
      return "$policy_status"
    fi
    return 21
  fi
  if [[ -f "$state_file" || -f "$rendered_env" ]]; then
    load_installed_controller_identity
  elif [[ "$mode" == adopt ]]; then
    die '--adopt requires a trusted installed controller identity'
  fi
  drain_current false "$rendered_env" "$release_dir"
  if [[ "$testing" == 1 && -n ${CI_FLEET_TEST_PAUSE_AFTER_DRAIN_FILE:-} ]]; then
    : >"$CI_FLEET_TEST_PAUSE_AFTER_DRAIN_FILE"
    while [[ ! -f "$CI_FLEET_TEST_PAUSE_AFTER_DRAIN_FILE.continue" ]]; do sleep 0.05; done
  fi
  controller_id=$desired_controller_id
  run_candidate_preflight
  if ! $build_before_drain; then build_candidate; fi
  activate_candidate
  transaction_active=false
  note "CONVERGED mode=$mode controller=$controller_id config_ref=$config_ref engine_ref=$engine_ref state=$target_state"
}

latest_checkpoint() {
  [[ -d "$checkpoints_dir" ]] || return 0
  { find "$checkpoints_dir" -mindepth 2 -maxdepth 2 -type f -name .complete ! -path "$checkpoints_dir/.checkpoint.staging.*/*" -printf '%T@ %h\n' 2>/dev/null || true; } | sort -nr | awk 'NR == 1 {print $2}'
}

perform_rollback() {
  local checkpoint_env
  checkpoint_dir=$(latest_checkpoint)
  [[ -n "$checkpoint_dir" ]] || die 'no controller checkpoint is available'
  if [[ -f "$checkpoint_dir/ci-fleet.env" ]]; then
    checkpoint_env=$checkpoint_dir/ci-fleet.env
  else
    checkpoint_env=$temporary/rollback-empty.env
    install -m 0600 /dev/null "$checkpoint_env"
  fi
  candidate_env=$checkpoint_env
  docker_network_policy_matches || die 'rollback requires Docker network-policy reconciliation through --upgrade'
  restore_checkpoint || die 'checkpoint restoration failed'
  note "ROLLBACK_OK checkpoint=$checkpoint_dir"
}

perform_uninstall() {
  local candidate current_candidate='' manager_candidate='' old_release='' old_ref='' status
  load_installed_controller_identity
  status=$(controller_status)
  if [[ -L "$current_link" ]] && raw_pointer_target_exists "$current_link"; then
    current_candidate=$(canonical_release_target_from_raw_pointer "$current_link" "$releases_dir" || true)
    [[ -n "$current_candidate" ]] || die 'current pointer is invalid; operator recovery or reinstall required'
    if ! runtime_release_complete "$current_candidate" "${current_candidate##*/}" || ! release_tree_permissions_trusted "$current_candidate"; then
      die 'a trusted complete canonical runtime release is required to uninstall'
    fi
  fi
  manager_candidate=$(manager_release_from_raw_pointer || true)
  if [[ -z "$manager_candidate" && ( -n "$status" || ( -L "$manager_current" && -e "$manager_current" ) ) ]]; then
    die 'a trusted complete canonical manager release is required to uninstall the running controller'
  fi
  for candidate in "$(current_runtime_release)" "$releases_dir/${manager_candidate##*/}"; do
    candidate=$(canonical_release_target "$candidate" "$releases_dir" || true)
    [[ -n "$candidate" && -f "$candidate/.ci-fleet-engine-ref" ]] || continue
    old_ref=$(<"$candidate/.ci-fleet-engine-ref")
    if [[ "$old_ref" =~ ^[0-9a-f]{40}$ ]] && runtime_release_complete "$candidate" "$old_ref" \
      && release_tree_permissions_trusted "$candidate"; then old_release=$candidate; break; fi
  done
  [[ -n "$old_release" || -z "$status" ]] || die 'a trusted complete release is required to uninstall the controller'
  make_checkpoint "$old_release"
  transaction_active=true
  drain_current false "$rendered_env" "$old_release"
  remove_inactive_managed_runners
  if [[ -n "$old_release" && -f "$rendered_env" ]]; then
    compose "$old_release" "$rendered_env" down --remove-orphans || true
    (
      set -a
      # shellcheck disable=SC1090
      . "$rendered_env"
      set +a
      "$old_release/scripts/cleanup.sh" --apply --instance "${CI_FLEET_INSTANCE:-}" || true
    )
  fi
  remove_systemd_units
  rm -f "$current_link" "$rendered_env" "$state_file"
  rm -rf -- "$state_root/health"
  rm -f "$manager_current"
  transaction_active=false
  note "UNINSTALL_OK host_config_preserved=$host_config secrets_preserved=$etc_dir/secrets"
}

if [[ "$mode" == policy-action ]]; then
  [[ -n ${CI_FLEET_INSTALLER_LOCK_FD:-} ]] || die 'policy actions require the inherited installer lock'
else
  require_commands
fi
if [[ -n ${CI_FLEET_INSTALLER_LOCK_FD:-} ]]; then
  [[ "$CI_FLEET_INSTALLER_LOCK_FD" == 9 ]] || die 'inherited installer lock must use file descriptor 9'
  [[ $(readlink -f /proc/self/fd/9 2>/dev/null || true) == $(readlink -m "$lock_file") ]] || die 'inherited installer lock does not match the configured lock file'
  flock -n 9 || die 'inherited installer lock is unavailable'
else
  install -d -m 0755 "$(dirname "$lock_file")"
  exec 9>"$lock_file"
  flock -n 9 || die 'another ci-fleet installer or drift check is already running'
fi
export CI_FLEET_INSTALLER_LOCK_FD=9
if [[ "$mode" == policy-action ]]; then
  perform_policy_action
  exit 0
fi
case "$mode" in
  check|install|adopt|upgrade)
    validate_common_arguments
    resolve_config
    validate_candidate_config_commit
    prepare_host_config
    verify_host_files
    select_engine
    prepare_engine_capabilities
    render_candidate
    if [[ "$mode" == check ]]; then
      perform_check
    else
      perform_converge
      write_transaction_result applied
    fi
    ;;
  rollback)
    perform_rollback
    ;;
  uninstall)
    perform_uninstall
    ;;
esac
