#!/usr/bin/env bash
set -Eeuo pipefail
set +x
export PYTHONDONTWRITEBYTECODE=1

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
mode=
config_repo=
config_identity_arg=
config_ref=
controller_id=
host_config_arg=
config_source_checkout=
root_prefix=${CI_FLEET_ROOT_PREFIX:-}
testing=${CI_FLEET_TESTING:-0}
transaction_active=false
checkpoint_dir=
staging_paths=()

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
die() {
  printf 'ERROR: %s\n' "$*" >&2
  if [[ ${transaction_active:-false} == true ]] && declare -F restore_checkpoint >/dev/null; then
    restore_checkpoint || true
    transaction_active=false
  fi
  exit 2
}

while (($#)); do
  case "$1" in
    --check|--install|--adopt|--upgrade|--rollback|--uninstall)
      [[ -z "$mode" ]] || die 'select exactly one operating mode'
      mode=${1#--}
      shift
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
managed_builder=ci-fleet-managed
managed_buildkit='moby/buildkit:v0.32.2@sha256:28a898719c18a33f4e8000685287fa36fd0dd9560c6440227d3a732d79bb41d8'

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
  local command
  for command in git python3 docker tar install cmp readlink systemctl stat awk grep date flock mktemp; do
    command -v "$command" >/dev/null || die "$command is required"
  done
  docker info >/dev/null 2>&1 || die 'Docker daemon is unavailable'
  docker compose version >/dev/null 2>&1 || die 'Docker Compose v2 is unavailable'
  docker buildx version >/dev/null 2>&1 || die 'Docker Buildx is unavailable'
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
  if runtime_release_complete "$release_dir" "$engine_ref"; then
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

build_managed_images() {
  local release=$1 environment=$2
  (
    set -a
    # shellcheck disable=SC1090
    . "$environment"
    set +a
    cleanup_builder() { docker buildx rm --force "$managed_builder" >/dev/null 2>&1 || true; }
    trap cleanup_builder EXIT
    cleanup_builder
    docker buildx create --name "$managed_builder" --driver docker-container \
      --driver-opt "image=$managed_buildkit" --bootstrap >/dev/null
    [[ $(docker buildx inspect "$managed_builder" | awk '$1 == "BuildKit" && $2 == "version:" {print $3; exit}') == v0.32.2 ]] \
      || die 'reviewed BuildKit backend v0.32.2 is unavailable'
    docker buildx build --builder "$managed_builder" --no-cache --load --provenance=false --sbom=false \
      --build-arg CI_FLEET_COMMIT --build-arg SOURCE_DATE_EPOCH \
      --tag "$CI_FLEET_RUNNER_IMAGE" "$release/runner"
    docker buildx build --builder "$managed_builder" --no-cache --load --provenance=false --sbom=false \
      --build-arg CI_FLEET_COMMIT --build-arg CI_FLEET_VERSION --build-arg SOURCE_DATE_EPOCH \
      --tag "$CI_FLEET_CONTROLLER_IMAGE" "$release/controller"
  )
}

controller_status() {
  docker inspect --format '{{.State.Status}}' "$controller_container" 2>/dev/null || true
}

current_runtime_release() {
  local target
  if [[ -L "$current_link" ]]; then
    target=$(readlink -f "$current_link" 2>/dev/null || true)
    [[ -z "$target" ]] || printf '%s' "$target"
  elif [[ -f "$install_root/deploy/compose.yaml" ]]; then
    printf '%s' "$install_root"
  fi
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
    CI_FLEET_RUNNER_CPUS CI_FLEET_RUNNER_MEMORY_MIB CI_FLEET_RUNNER_TTL CI_FLEET_DOCKER_GID; do
    expected=$(awk -F= -v key="$key" '$1 == key {print substr($0, index($0, "=") + 1)}' "$candidate_env")
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

runtime_release_complete() {
  local path=$1 expected=$2 require_status=${3:-0} require_schema=${4:-0} marker required stored_digest actual_digest
  local -a capability_args=()
  [[ -d "$path" && -f "$path/.ci-fleet-engine-ref" && -f "$path/.ci-fleet-tree-sha256" && -f "$path/deploy/compose.yaml" ]] || return 1
  [[ -x "$path/scripts/preflight.sh" && -x "$path/scripts/healthcheck.sh" && -x "$path/scripts/cleanup.sh" ]] || return 1
  if [[ -e "$path/engine-capabilities.json" || "$require_status" == 1 || "$require_schema" == 1 ]]; then
    [[ ! -L "$path/engine-capabilities.json" && -f "$path/engine-capabilities.json" ]] || return 1
    [[ "$require_schema" != 1 ]] || capability_args+=(--require-status-reporting-config)
    [[ "$require_status" != 1 ]] || capability_args+=(--require-status-reporting)
    python3 "$repo_root/scripts/desired_state.py" validate-engine-capabilities \
      --manifest "$path/engine-capabilities.json" "${capability_args[@]}" >/dev/null || return 1
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

release_matches() {
  runtime_release_complete "$release_dir" "$engine_ref" "$status_reporting_required" "$status_reporting_configured" || return 1
  [[ -L "$current_link" ]] || return 1
  [[ $(readlink -f "$current_link") == $(readlink -f "$release_dir") ]]
}

managed_images_match() {
  local environment=${1:-$candidate_env} digest expected_engine image provenance
  local -a expected_images=()
  expected_engine=$(awk -F= '$1 == "CI_FLEET_ENGINE_REF" {print $2}' "$environment")
  [[ "$expected_engine" =~ ^[0-9a-f]{40}$ ]] || return 1
  mapfile -t expected_images < <(awk -F= '
    $1 == "CI_FLEET_CONTROLLER_IMAGE" {controller=$2}
    $1 == "CI_FLEET_CONTROLLER_IMAGE_DIGEST" {controller_digest=$2}
    $1 == "CI_FLEET_RUNNER_IMAGE" {runner=$2}
    $1 == "CI_FLEET_RUNNER_IMAGE_DIGEST" {runner_digest=$2}
    END {if (controller && controller_digest && runner && runner_digest) {print controller " " controller_digest; print runner " " runner_digest}}
  ' "$environment")
  [[ ${#expected_images[@]} == 2 ]] || return 1
  for expected in "${expected_images[@]}"; do
    read -r image digest <<<"$expected"
    [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || return 1
    provenance=$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image" 2>/dev/null) || return 1
    [[ "$provenance" == "$expected_engine" ]] || return 1
    [[ $(docker image inspect --format '{{.Id}}' "$image" 2>/dev/null) == "$digest" ]] || return 1
  done
}

managed_image_ids_configured() {
  grep -Eq '^CI_FLEET_CONTROLLER_IMAGE_DIGEST=sha256:[0-9a-f]{64}$' "$candidate_env" \
    && grep -Eq '^CI_FLEET_RUNNER_IMAGE_DIGEST=sha256:[0-9a-f]{64}$' "$candidate_env"
}

systemd_matches() {
  local expected_manager unit
  expected_manager=$manager_releases/$engine_ref
  manager_release_complete "$expected_manager" "$engine_ref" "$status_reporting_required" "$status_reporting_configured" || return 1
  [[ -L "$manager_current" ]] || return 1
  [[ $(readlink -f "$manager_current") == $(readlink -f "$expected_manager") ]] || return 1
  for unit in "${unit_names[@]}"; do
    [[ -f "$systemd_dir/$unit" ]] || return 1
    cmp -s "$expected_manager/host/systemd/$unit" "$systemd_dir/$unit" || return 1
  done
  for unit in "${timer_names[@]}"; do
    systemctl is-enabled --quiet "$unit" || return 1
    systemctl is-active --quiet "$unit" || return 1
  done
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
  if managed_image_ids_configured; then
    managed_images_match || { note 'DRIFT managed_images'; count=$((count + 1)); }
  fi
  systemd_matches || { note 'DRIFT maintenance_timers'; count=$((count + 1)); }
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
  local archive checkout resolved staged_release
  if runtime_release_complete "$release_dir" "$engine_ref" "$status_reporting_required" "$status_reporting_configured"; then
    return
  fi
  install -d -m 0755 "$releases_dir"
  archive=$temporary/engine.tar
  if is_git_checkout "$repo_root" && [[ $(git -C "$repo_root" rev-parse 'HEAD^{commit}') == "$engine_ref" ]]; then
    git -C "$repo_root" archive --format=tar --output "$archive" HEAD
  else
    [[ "$engine_repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die 'delivery engine repository is invalid'
    checkout=$temporary/engine-repository
    git init -q "$checkout"
    git -C "$checkout" remote add origin "https://github.com/${engine_repository}.git"
    GIT_TERMINAL_PROMPT=0 git -C "$checkout" fetch -q --depth=1 origin "$engine_ref" || die 'pinned ci-fleet engine commit could not be fetched'
    resolved=$(git -C "$checkout" rev-parse 'FETCH_HEAD^{commit}')
    [[ "$resolved" == "$engine_ref" ]] || die 'fetched ci-fleet engine commit does not match desired state'
    git -C "$checkout" archive --format=tar --output "$archive" FETCH_HEAD
  fi
  staged_release=$(mktemp -d "$releases_dir/.${engine_ref}.staging.XXXXXX")
  staging_paths+=("$staged_release")
  chmod 0755 "$staged_release"
  tar -xf "$archive" -C "$staged_release"
  printf '%s\n' "$engine_ref" >"$staged_release/.ci-fleet-engine-ref"
  chmod 0644 "$staged_release/.ci-fleet-engine-ref"
  release_tree_digest "$staged_release" >"$staged_release/.ci-fleet-tree-sha256"
  chmod 0644 "$staged_release/.ci-fleet-tree-sha256"
  runtime_release_complete "$staged_release" "$engine_ref" "$status_reporting_required" "$status_reporting_configured" || die 'staged engine release is incomplete'
  atomic_replace_directory "$staged_release" "$release_dir"
}

install_manager() {
  local manager_commit manager_release archive staged_manager
  manager_commit=$engine_ref
  [[ "$manager_commit" =~ ^[0-9a-f]{40}$ ]] || die 'installer manager commit is invalid'
  runtime_release_complete "$release_dir" "$manager_commit" "$status_reporting_required" "$status_reporting_configured" || die 'desired engine release is unavailable for installer manager activation'
  manager_release=$manager_releases/$manager_commit
  if ! manager_release_complete "$manager_release" "$manager_commit" "$status_reporting_required" "$status_reporting_configured"; then
    install -d -m 0755 "$manager_releases"
    archive=$temporary/manager.tar
    tar -cf "$archive" -C "$release_dir" .
    staged_manager=$(mktemp -d "$manager_releases/.${manager_commit}.staging.XXXXXX")
    staging_paths+=("$staged_manager")
    chmod 0755 "$staged_manager"
    tar -xf "$archive" -C "$staged_manager"
    printf '%s\n' "$manager_commit" >"$staged_manager/.ci-fleet-engine-ref"
    chmod 0644 "$staged_manager/.ci-fleet-engine-ref"
    manager_release_complete "$staged_manager" "$manager_commit" "$status_reporting_required" "$status_reporting_configured" || die 'staged installer manager release is incomplete'
    atomic_replace_directory "$staged_manager" "$manager_release"
  fi
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
  run_candidate_preflight
  compose "$release_dir" "$candidate_env" config --quiet
  build_managed_images "$release_dir" "$candidate_env"
  managed_images_match "$candidate_env" || die 'managed image digest does not match reviewed desired state'
}

capture_legacy_image_ids() {
  local environment=$1 controller_image runner_image controller_digest runner_digest engine_ref
  engine_ref=$(awk -F= '$1 == "CI_FLEET_ENGINE_REF" {print $2}' "$environment")
  controller_digest=$(awk -F= '$1 == "CI_FLEET_CONTROLLER_IMAGE_DIGEST" {print $2}' "$environment")
  runner_digest=$(awk -F= '$1 == "CI_FLEET_RUNNER_IMAGE_DIGEST" {print $2}' "$environment")
  if [[ -z "$engine_ref" ]]; then
    engine_ref=$(docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$controller_container" 2>/dev/null) \
      || die 'legacy checkpoint cannot capture the installed engine revision'
    printf 'CI_FLEET_ENGINE_REF=%s\n' "$engine_ref" >>"$environment"
  fi
  if [[ -z "$controller_digest" ]]; then
    controller_image=$(awk -F= '$1 == "CI_FLEET_CONTROLLER_IMAGE" {print $2}' "$environment")
    [[ -n "$controller_image" ]] || die 'legacy checkpoint cannot identify the installed controller image'
    controller_digest=$(docker inspect --format '{{.Image}}' "$controller_container" 2>/dev/null \
      || docker image inspect --format '{{.Id}}' "$controller_image" 2>/dev/null) \
      || die 'legacy checkpoint cannot capture the installed controller image ID'
    printf 'CI_FLEET_CONTROLLER_IMAGE_DIGEST=%s\n' "$controller_digest" >>"$environment"
  fi
  if [[ -z "$runner_digest" ]]; then
    runner_image=$(awk -F= '$1 == "CI_FLEET_RUNNER_IMAGE" {print $2}' "$environment")
    [[ -n "$runner_image" ]] || die 'legacy checkpoint cannot identify the installed runner image'
    runner_digest=$(docker image inspect --format '{{.Id}}' "$runner_image" 2>/dev/null) \
      || die 'legacy checkpoint cannot capture the installed runner image ID'
    printf 'CI_FLEET_RUNNER_IMAGE_DIGEST=%s\n' "$runner_digest" >>"$environment"
  fi
  [[ "$engine_ref" =~ ^[0-9a-f]{40}$ && "$controller_digest" =~ ^sha256:[0-9a-f]{64}$ && "$runner_digest" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || die 'legacy checkpoint contains an invalid engine revision or managed image ID'
}

make_checkpoint() {
  local timestamp target unit timer final_checkpoint staged_checkpoint
  timestamp=$(date -u +%Y%m%dT%H%M%SZ)
  final_checkpoint=$checkpoints_dir/${timestamp}-$$
  install -d -m 0700 "$checkpoints_dir"
  staged_checkpoint=$(mktemp -d "$checkpoints_dir/.checkpoint.staging.XXXXXX")
  staging_paths+=("$staged_checkpoint")
  checkpoint_dir=$staged_checkpoint
  install -d -m 0700 "$checkpoint_dir/systemd"
  [[ ! -f "$rendered_env" ]] || {
    install -m 0600 "$rendered_env" "$checkpoint_dir/ci-fleet.env"
    capture_legacy_image_ids "$checkpoint_dir/ci-fleet.env"
  }
  [[ ! -f "$state_file" ]] || install -m 0600 "$state_file" "$checkpoint_dir/install-state.json"
  target=$(current_runtime_release)
  if [[ -n "$target" ]]; then
    printf '%s\n' "$target" >"$checkpoint_dir/release-target"
    chmod 0600 "$checkpoint_dir/release-target"
  fi
  if [[ -L "$manager_current" ]]; then
    target=$(readlink -f "$manager_current")
    printf '%s\n' "$target" >"$checkpoint_dir/manager-target"
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
  local deadline count old_release status paused=false force_nonterminal=${1:-false}
  local drain_env=${2:-$rendered_env} fallback_release=${3:-} shutdown_timeout=${CI_FLEET_DRAIN_TIMEOUT_SECONDS:-300}
  drain_error=
  status=$(controller_status)
  case "$status" in
    running|''|exited|created|dead) ;;
    *)
      if [[ "$force_nonterminal" != true ]]; then
        drain_error="cannot safely drain controller in non-terminal state: $status"
        return 1
      fi
      [[ -f "$drain_env" ]] || { drain_error='cannot stop a non-terminal candidate without its rendered environment'; return 1; }
      old_release=$(current_runtime_release)
      [[ -n "$old_release" ]] || old_release=$fallback_release
      [[ -n "$old_release" ]] || { drain_error='cannot stop a non-terminal candidate without its runtime release'; return 1; }
      compose "$old_release" "$drain_env" stop --timeout "$shutdown_timeout" controller >/dev/null 2>&1 || { drain_error="failed to stop non-terminal candidate state: $status"; return 1; }
      status=
      ;;
  esac
  if [[ "$status" == running ]]; then
    if [[ ! -f "$drain_env" ]]; then drain_error='cannot safely drain a running controller without its rendered environment'; return 1; fi
    old_release=$(current_runtime_release)
    [[ -n "$old_release" ]] || old_release=$fallback_release
    if [[ -z "$old_release" || ! -f "$old_release/deploy/compose.yaml" ]]; then drain_error='cannot locate the running controller Compose release for safe adoption'; return 1; fi
    if ! compose "$old_release" "$drain_env" pause controller >/dev/null; then drain_error='could not pause the controller for drain'; return 1; fi
    paused=true
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
  if [[ "$status" != running ]]; then
    note 'DRAIN_OK managed_runners=0'
    return 0
  fi
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
  note 'DRAIN_OK managed_runners=0'
}

drain_current() {
  try_drain_current || die "$drain_error"
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
  local staged_state
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
  if ! run_health_check "$release_dir" "$rendered_env" true; then
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
        systemctl enable --now "$opt_timer" >/dev/null
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
  local target restored_state failed=0 checkpoint_release='' drain_env=$rendered_env drain_release=''
  [[ -n "$checkpoint_dir" && -d "$checkpoint_dir" ]] || return 1
  [[ ! -f "$checkpoint_dir/release-target" ]] || checkpoint_release=$(<"$checkpoint_dir/release-target")
  drain_release=$(current_runtime_release)
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
  if [[ -f "$checkpoint_dir/release-target" ]]; then
    target=$(<"$checkpoint_dir/release-target")
    if [[ -d "$target" && -f "$target/deploy/compose.yaml" ]]; then
      ln -sfn "$target" "$temporary/rollback-current" && mv -Tf "$temporary/rollback-current" "$current_link" || failed=1
      release_dir=$target
    else
      failed=1
    fi
  else
    rm -f "$current_link" || failed=1
    release_dir=
  fi
  if [[ -f "$checkpoint_dir/manager-target" ]]; then
    target=$(<"$checkpoint_dir/manager-target")
    if [[ "$target" == "$manager_releases/"* && -d "$target" ]]; then
      ln -sfn "$target" "$temporary/rollback-manager" && mv -Tf "$temporary/rollback-manager" "$manager_current" || failed=1
    else
      failed=1
    fi
  else
    rm -f "$manager_current" || failed=1
  fi
  restore_systemd_snapshot || failed=1
  if [[ -n "$release_dir" && -f "$rendered_env" ]]; then
    restored_state=$(awk -F= '$1 == "CI_FLEET_CONTROLLER_STATE" {print $2}' "$rendered_env") || failed=1
    [[ "$restored_state" == active || "$restored_state" == drained || "$restored_state" == disabled ]] || failed=1
    if ((failed == 0)) && grep -Eq '^CI_FLEET_(CONTROLLER|RUNNER)_IMAGE_DIGEST=sha256:[0-9a-f]{64}$' "$rendered_env"; then
      if ! managed_images_match "$rendered_env"; then
        build_managed_images "$release_dir" "$rendered_env" || failed=1
        if ((failed == 0)); then managed_images_match "$rendered_env" || failed=1; fi
      fi
    else
      note 'ROLLBACK_IMAGE_DIGEST_UNAVAILABLE legacy_checkpoint=true'
      failed=1
    fi
    if [[ "$restored_state" == active && "$failed" == 0 ]]; then
      if [[ $(managed_runner_count) != 0 ]]; then
        failed=1
      else
        compose "$release_dir" "$rendered_env" up -d --no-deps controller || failed=1
        if ((failed == 0)); then run_health_check "$release_dir" "$rendered_env" true || failed=1; fi
      fi
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

on_error() {
  local status=$?
  if $transaction_active; then
    restore_checkpoint || true
  fi
  exit "$status"
}
trap on_error ERR

perform_check() {
  local count
  display_last_health
  if ! managed_image_ids_configured; then
    [[ -L "$manager_current" && $(readlink -f "$manager_current") == "$manager_releases/$engine_ref" ]] \
      || die 'manager-only staging is incomplete'
    [[ $(current_runtime_release) != "$release_dir" ]] || die 'manager-only staging activated an unreviewed runtime'
    note "CHECK_STAGED manager_ref=$engine_ref runtime_unchanged=true"
    return
  fi
  drift_count
  count=$DRIFT_COUNT
  if ((count > 0)); then
    note "CHECK_FAILED drift=$count"
    exit 3
  fi
  note "CHECK_OK controller=$controller_id config_ref=$config_ref engine_ref=$engine_ref state=$target_state"
}

perform_converge() {
  local count existing_status installed_engine desired_controller_id=$controller_id
  if [[ "$mode" == upgrade && ! -f "$state_file" ]]; then
    die '--upgrade requires an existing managed installation; use --install or --adopt'
  fi
  existing_status=$(controller_status)
  if [[ "$mode" == adopt && ! -f "$rendered_env" && ! -f "$state_file" && -z "$existing_status" ]]; then
    die '--adopt requires an existing controller or configuration; use --install for a fresh host'
  fi
  if [[ "$mode" == install && -f "$rendered_env" && ! -f "$state_file" ]]; then
    die 'an unmanaged controller configuration exists; use --adopt'
  fi
  if ! managed_image_ids_configured; then
    [[ "$mode" == upgrade && -f "$state_file" && -f "$rendered_env" ]] \
      || die 'managed_images may be omitted only for an existing managed engine upgrade'
    ! grep -Eq '^CI_FLEET_(CONTROLLER|RUNNER)_IMAGE_DIGEST=sha256:[0-9a-f]{64}$' "$rendered_env" \
      || die 'managed_images cannot be removed after image enforcement'
    installed_engine=$(awk -F= '$1 == "CI_FLEET_ENGINE_REF" {print $2}' "$rendered_env")
    [[ "$installed_engine" =~ ^[0-9a-f]{40}$ && "$installed_engine" != "$engine_ref" ]] \
      || die 'manager-only staging requires a new engine revision'
    install_release
    install_manager
    install_systemd_units "$(readlink -f "$manager_current")"
    note "MANAGER_STAGED engine_ref=$engine_ref runtime_ref=$installed_engine"
    return
  fi
  drift_count
  count=$DRIFT_COUNT
  if ((count == 0)); then
    note "NO_CHANGE controller=$controller_id config_ref=$config_ref engine_ref=$engine_ref state=$target_state"
    return
  fi
  install_release
  make_checkpoint
  transaction_active=true
  if [[ -f "$state_file" || -f "$rendered_env" ]]; then
    load_installed_controller_identity
  elif [[ "$mode" == adopt ]]; then
    die '--adopt requires a trusted installed controller identity'
  fi
  drain_current
  controller_id=$desired_controller_id
  [[ "$target_state" == active ]] || remove_inactive_managed_runners
  build_candidate
  activate_candidate
  transaction_active=false
  note "CONVERGED mode=$mode controller=$controller_id config_ref=$config_ref engine_ref=$engine_ref state=$target_state"
}

latest_checkpoint() {
  [[ -d "$checkpoints_dir" ]] || return 0
  { find "$checkpoints_dir" -mindepth 2 -maxdepth 2 -type f -name .complete ! -path "$checkpoints_dir/.checkpoint.staging.*/*" -printf '%T@ %h\n' 2>/dev/null || true; } | sort -nr | awk 'NR == 1 {print $2}'
}

perform_rollback() {
  checkpoint_dir=$(latest_checkpoint)
  [[ -n "$checkpoint_dir" ]] || die 'no controller checkpoint is available'
  load_installed_controller_identity "$checkpoint_dir/install-state.json" "$checkpoint_dir/ci-fleet.env"
  restore_checkpoint || die 'checkpoint restoration failed'
  note "ROLLBACK_OK checkpoint=$checkpoint_dir"
}

perform_uninstall() {
  local old_release=
  load_installed_controller_identity
  old_release=$(current_runtime_release)
  make_checkpoint
  transaction_active=true
  drain_current
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

require_commands
if [[ -n ${CI_FLEET_INSTALLER_LOCK_FD:-} ]]; then
  [[ "$CI_FLEET_INSTALLER_LOCK_FD" == 9 ]] || die 'inherited installer lock must use file descriptor 9'
  [[ $(readlink -f /proc/self/fd/9 2>/dev/null || true) == $(readlink -m "$lock_file") ]] || die 'inherited installer lock does not match the configured lock file'
  flock -n 9 || die 'inherited installer lock is unavailable'
else
  install -d -m 0755 "$(dirname "$lock_file")"
  exec 9>"$lock_file"
  flock -n 9 || die 'another ci-fleet installer or drift check is already running'
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
    if [[ "$mode" == check ]]; then perform_check; else perform_converge; fi
    ;;
  rollback)
    perform_rollback
    ;;
  uninstall)
    perform_uninstall
    ;;
esac
