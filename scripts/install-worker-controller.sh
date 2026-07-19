#!/usr/bin/env bash
set -Eeuo pipefail
set +x

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
mode=
config_repo=
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
    --controller CONTROLLER_ID

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
checkpoints_dir=$state_root/checkpoints
systemd_dir=$(root_path /etc/systemd/system)
lock_file=$(root_path /run/ci-fleet-installer.lock)
controller_container=ci-fleet-controller-1
unit_names=(
  ci-fleet-health.service ci-fleet-health.timer
  ci-fleet-cleanup.service ci-fleet-cleanup.timer
  ci-fleet-drift.service ci-fleet-drift.timer
)
timer_names=(ci-fleet-health.timer ci-fleet-cleanup.timer ci-fleet-drift.timer)

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
}

validate_common_arguments() {
  [[ -n "$config_repo" ]] || die '--config-repo is required for this mode'
  [[ "$config_ref" =~ ^[0-9a-f]{40}$ ]] || die '--ref must be a full lowercase commit SHA'
  [[ "$controller_id" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || die '--controller must be a lowercase logical ID'
  [[ -z "$host_config_arg" || "$host_config" == "$default_host_config" ]] || die 'managed installs require the default /etc/ci-fleet/host.env path'
  if [[ "$config_repo" == *://* || "$config_repo" == *@* ]]; then
    die '--config-repo must not contain a URL or embedded credentials; use OWNER/REPOSITORY or a local path'
  fi
}

resolve_config() {
  local resolved checkout
  candidate_config=$temporary/fleet.json
  if is_git_checkout "$config_repo"; then
    config_identity=$(cd "$config_repo" && pwd -P)
    config_source_checkout=$config_identity
    resolved=$(git -C "$config_identity" rev-parse "$config_ref^{commit}" 2>/dev/null || true)
    [[ "$resolved" == "$config_ref" ]] || die 'local configuration repository does not contain the requested commit'
    git -C "$config_identity" show "$config_ref:fleet.json" >"$candidate_config" || die 'fleet.json is absent at the requested configuration commit'
    return
  fi
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
  local tree_paths=$temporary/config-tree-paths
  git -C "$config_source_checkout" ls-tree -rz --name-only "$config_ref" >"$tree_paths" || die 'cannot inspect the configuration commit tree'
  python3 "$repo_root/templates/config-repository/scripts/validate.py" \
    --config "$candidate_config" --strict --tree-paths "$tree_paths" || die 'configuration commit validation failed'
}

prepare_host_config() {
  effective_host_config=$host_config
  if [[ -f "$host_config" ]]; then
    return
  fi
  if [[ -f "$rendered_env" && "$mode" == adopt ]]; then
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

docker_gid() {
  if [[ "$testing" == 1 && -n ${CI_FLEET_DOCKER_GID_OVERRIDE:-} ]]; then
    printf '%s' "$CI_FLEET_DOCKER_GID_OVERRIDE"
    return
  fi
  stat -c '%g' /var/run/docker.sock
}

render_candidate() {
  local -a metadata_values
  candidate_env=$temporary/ci-fleet.env
  candidate_metadata=$temporary/metadata.json
  python3 "$repo_root/scripts/desired_state.py" render \
    --config "$candidate_config" \
    --controller "$controller_id" \
    --host-config "$effective_host_config" \
    --config-repository "$config_identity" \
    --config-ref "$config_ref" \
    --docker-gid "$(docker_gid)" \
    --output "$candidate_env" \
    --metadata-output "$candidate_metadata"
  mapfile -t metadata_values < <(python3 - "$candidate_metadata" <<'PY'
import json
import sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
for key in ("controller_state", "engine_ref", "engine_repository"):
    print(value[key])
PY
  )
  [[ ${#metadata_values[@]} == 3 ]] || die 'rendered controller metadata is incomplete'
  target_state=${metadata_values[0]}
  engine_ref=${metadata_values[1]}
  engine_repository=${metadata_values[2]}
  [[ "$engine_repository" == RandomDevelopment/ci-fleet ]] || die 'delivery engine repository is not the fixed reviewed public engine'
  release_dir=$releases_dir/$engine_ref
}

compose() {
  local release=$1 env=$2
  shift 2
  docker compose --env-file "$env" -f "$release/deploy/compose.yaml" "$@"
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
    --filter label=io.randomdevelopment.ci-fleet.kind=runner | wc -l | tr -d ' '
}

runtime_matches() {
  local expected=$1 status
  status=$(controller_status)
  if [[ "$expected" == active ]]; then
    [[ "$status" == running ]]
  else
    [[ -z "$status" || "$status" == exited || "$status" == created ]]
  fi
}

state_matches() {
  [[ -f "$state_file" ]] || return 1
  python3 - "$state_file" "$candidate_metadata" <<'PY'
import json
import sys
installed = json.load(open(sys.argv[1], encoding="utf-8"))
installed.pop("installed_at", None)
candidate = json.load(open(sys.argv[2], encoding="utf-8"))
raise SystemExit(0 if installed == candidate else 1)
PY
}

release_matches() {
  local marker
  [[ -d "$release_dir" && -f "$release_dir/.ci-fleet-engine-ref" && -f "$release_dir/deploy/compose.yaml" && -x "$release_dir/scripts/cleanup.sh" ]] || return 1
  marker=$(<"$release_dir/.ci-fleet-engine-ref")
  [[ "$marker" == "$engine_ref" ]] || return 1
  [[ -L "$current_link" ]] || return 1
  [[ $(readlink -f "$current_link") == $(readlink -f "$release_dir") ]]
}

systemd_matches() {
  local expected_manager marker unit
  expected_manager=$manager_releases/$engine_ref
  [[ -d "$expected_manager" && -f "$expected_manager/.ci-fleet-engine-ref" ]] || return 1
  [[ -x "$expected_manager/scripts/healthcheck.sh" && -x "$expected_manager/scripts/check-installed-state.sh" ]] || return 1
  marker=$(<"$expected_manager/.ci-fleet-engine-ref")
  [[ "$marker" == "$engine_ref" ]] || return 1
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
  local count=0
  if [[ ! -f "$rendered_env" ]] || ! cmp -s "$candidate_env" "$rendered_env"; then
    note 'DRIFT rendered_environment'
    count=$((count + 1))
  fi
  release_matches || { note 'DRIFT engine_release'; count=$((count + 1)); }
  state_matches || { note 'DRIFT install_state'; count=$((count + 1)); }
  runtime_matches "$target_state" || { note 'DRIFT controller_runtime'; count=$((count + 1)); }
  systemd_matches || { note 'DRIFT maintenance_timers'; count=$((count + 1)); }
  DRIFT_COUNT=$count
}

install_release() {
  local archive marker_commit checkout resolved staged_release
  if [[ -d "$release_dir" ]]; then
    [[ -f "$release_dir/.ci-fleet-engine-ref" && -f "$release_dir/deploy/compose.yaml" && -x "$release_dir/scripts/preflight.sh" && -x "$release_dir/scripts/healthcheck.sh" && -x "$release_dir/scripts/cleanup.sh" ]] || die "existing release is incomplete: $release_dir"
    marker_commit=$(<"$release_dir/.ci-fleet-engine-ref")
    [[ "$marker_commit" == "$engine_ref" ]] || die "existing release marker does not match $engine_ref"
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
  mv "$staged_release" "$release_dir"
}

install_manager() {
  local manager_commit manager_release archive marker staged_manager release_marker
  manager_commit=$engine_ref
  [[ "$manager_commit" =~ ^[0-9a-f]{40}$ ]] || die 'installer manager commit is invalid'
  [[ -d "$release_dir" && -f "$release_dir/.ci-fleet-engine-ref" ]] || die 'desired engine release is unavailable for installer manager activation'
  release_marker=$(<"$release_dir/.ci-fleet-engine-ref")
  [[ "$release_marker" == "$manager_commit" ]] || die 'desired engine release marker is inconsistent'
  manager_release=$manager_releases/$manager_commit
  if [[ ! -d "$manager_release" ]]; then
    install -d -m 0755 "$manager_releases"
    archive=$temporary/manager.tar
    tar -cf "$archive" -C "$release_dir" .
    staged_manager=$(mktemp -d "$manager_releases/.${manager_commit}.staging.XXXXXX")
    staging_paths+=("$staged_manager")
    chmod 0755 "$staged_manager"
    tar -xf "$archive" -C "$staged_manager"
    printf '%s\n' "$manager_commit" >"$staged_manager/.ci-fleet-engine-ref"
    chmod 0644 "$staged_manager/.ci-fleet-engine-ref"
    mv "$staged_manager" "$manager_release"
  else
    [[ -f "$manager_release/.ci-fleet-engine-ref" && -x "$manager_release/scripts/install-worker-controller.sh" && -x "$manager_release/scripts/healthcheck.sh" && -x "$manager_release/scripts/check-installed-state.sh" ]] || die 'existing installer manager release is incomplete'
    marker=$(<"$manager_release/.ci-fleet-engine-ref")
    [[ "$marker" == "$manager_commit" ]] || die 'existing installer manager marker is inconsistent'
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
  [[ "$target_state" == disabled ]] || compose "$release_dir" "$candidate_env" build runner-image controller
}

make_checkpoint() {
  local timestamp target unit timer
  timestamp=$(date -u +%Y%m%dT%H%M%SZ)
  checkpoint_dir=$checkpoints_dir/${timestamp}-$$
  install -d -m 0700 "$checkpoint_dir" "$checkpoint_dir/systemd"
  [[ ! -f "$rendered_env" ]] || install -m 0600 "$rendered_env" "$checkpoint_dir/ci-fleet.env"
  [[ ! -f "$state_file" ]] || install -m 0600 "$state_file" "$checkpoint_dir/install-state.json"
  target=$(current_runtime_release)
  if [[ -n "$target" ]]; then
    printf '%s\n' "$target" >"$temporary/release-target"
    install -m 0600 "$temporary/release-target" "$checkpoint_dir/release-target"
  fi
  if [[ -L "$manager_current" ]]; then
    target=$(readlink -f "$manager_current")
    printf '%s\n' "$target" >"$temporary/manager-target"
    install -m 0600 "$temporary/manager-target" "$checkpoint_dir/manager-target"
  fi
  for unit in "${unit_names[@]}"; do
    [[ ! -f "$systemd_dir/$unit" ]] || install -m 0644 "$systemd_dir/$unit" "$checkpoint_dir/systemd/$unit"
  done
  : >"$temporary/enabled-timers"
  : >"$temporary/active-timers"
  for timer in "${timer_names[@]}"; do
    if systemctl is-enabled --quiet "$timer" 2>/dev/null; then printf '%s\n' "$timer" >>"$temporary/enabled-timers"; fi
    if systemctl is-active --quiet "$timer" 2>/dev/null; then printf '%s\n' "$timer" >>"$temporary/active-timers"; fi
  done
  install -m 0600 "$temporary/enabled-timers" "$checkpoint_dir/enabled-timers"
  install -m 0600 "$temporary/active-timers" "$checkpoint_dir/active-timers"
  note "CHECKPOINT_CREATED path=$checkpoint_dir"
}

try_drain_current() {
  local deadline count old_release status paused=false
  drain_error=
  status=$(controller_status)
  case "$status" in
    running|''|exited|created|dead) ;;
    *) drain_error="cannot safely drain controller in non-terminal state: $status"; return 1 ;;
  esac
  if [[ "$status" == running ]]; then
    if [[ ! -f "$rendered_env" ]]; then drain_error='cannot safely drain a running controller without its rendered environment'; return 1; fi
    old_release=$(current_runtime_release)
    if [[ -z "$old_release" || ! -f "$old_release/deploy/compose.yaml" ]]; then drain_error='cannot locate the running controller Compose release for safe adoption'; return 1; fi
    if ! compose "$old_release" "$rendered_env" pause controller >/dev/null; then drain_error='could not pause the controller for drain'; return 1; fi
    paused=true
  fi
  deadline=$((SECONDS + ${CI_FLEET_DRAIN_TIMEOUT_SECONDS:-300}))
  while :; do
    count=$(managed_runner_count)
    if [[ "$count" == 0 ]]; then break; fi
    if ((SECONDS >= deadline)); then
      if [[ "$paused" == true ]]; then compose "$old_release" "$rendered_env" unpause controller >/dev/null || true; fi
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
  docker compose --project-name ci-fleet --env-file "$rendered_env" -f "$old_release/deploy/compose.yaml" kill --signal SIGTERM controller >/dev/null || {
    drain_error='failed to signal the paused controller for graceful scale-set cleanup'
    return 1
  }
  if [[ $(docker inspect --format '{{.State.Paused}}' "$controller_container" 2>/dev/null || true) == true ]]; then
    docker compose --project-name ci-fleet --env-file "$rendered_env" -f "$old_release/deploy/compose.yaml" unpause controller >/dev/null || {
      drain_error='failed to unpause the signaled controller for graceful shutdown'
      return 1
    }
  fi
  docker compose --project-name ci-fleet --env-file "$rendered_env" -f "$old_release/deploy/compose.yaml" stop controller >/dev/null || {
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
  systemctl daemon-reload
  systemctl enable --now "${timer_names[@]}" >/dev/null
}

remove_systemd_units() {
  systemctl disable --now "${timer_names[@]}" >/dev/null 2>&1 || true
  local unit
  for unit in "${unit_names[@]}"; do rm -f "$systemd_dir/$unit"; done
  systemctl daemon-reload
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
    if ! (
      set -a
      # shellcheck disable=SC1090
      . "$rendered_env"
      set +a
      "$release_dir/scripts/healthcheck.sh"
    ); then
      die 'post-activation health check failed'
    fi
  else
    compose "$release_dir" "$rendered_env" stop controller >/dev/null 2>&1 || true
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
}

restore_systemd_snapshot() {
  local unit timer failed=0
  remove_systemd_units || failed=1
  for unit in "${unit_names[@]}"; do
    [[ ! -f "$checkpoint_dir/systemd/$unit" ]] || install -m 0644 "$checkpoint_dir/systemd/$unit" "$systemd_dir/$unit" || failed=1
  done
  systemctl daemon-reload || failed=1
  for timer in "${timer_names[@]}"; do
    if grep -Fxq "$timer" "$checkpoint_dir/enabled-timers"; then systemctl enable "$timer" >/dev/null || failed=1; else systemctl disable "$timer" >/dev/null 2>&1 || true; fi
    if grep -Fxq "$timer" "$checkpoint_dir/active-timers"; then systemctl start "$timer" || failed=1; else systemctl stop "$timer" >/dev/null 2>&1 || true; fi
  done
  return "$failed"
}

restore_checkpoint() {
  local target restored_state failed=0
  [[ -n "$checkpoint_dir" && -d "$checkpoint_dir" ]] || return 1
  if ! try_drain_current; then
    note "ROLLBACK_FAILED reason=$drain_error"
    return 1
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
    if [[ "$restored_state" == active && "$failed" == 0 ]]; then
      if [[ $(managed_runner_count) != 0 ]]; then
        failed=1
      else
        compose "$release_dir" "$rendered_env" up -d --no-deps controller || failed=1
        if ((failed == 0)); then
          (
            set -a
            # shellcheck disable=SC1090
            . "$rendered_env"
            set +a
            "$release_dir/scripts/healthcheck.sh"
          ) || failed=1
        fi
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
  drift_count
  count=$DRIFT_COUNT
  if ((count > 0)); then
    note "CHECK_FAILED drift=$count"
    exit 3
  fi
  note "CHECK_OK controller=$controller_id config_ref=$config_ref engine_ref=$engine_ref state=$target_state"
}

perform_converge() {
  local count existing_status
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
  drift_count
  count=$DRIFT_COUNT
  if ((count == 0)); then
    note "NO_CHANGE controller=$controller_id config_ref=$config_ref engine_ref=$engine_ref state=$target_state"
    return
  fi
  install_release
  make_checkpoint
  transaction_active=true
  drain_current
  build_candidate
  activate_candidate
  transaction_active=false
  note "CONVERGED mode=$mode controller=$controller_id config_ref=$config_ref engine_ref=$engine_ref state=$target_state"
}

latest_checkpoint() {
  [[ -d "$checkpoints_dir" ]] || return 0
  { find "$checkpoints_dir" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' 2>/dev/null || true; } | sort -nr | awk 'NR == 1 {print $2}'
}

perform_rollback() {
  checkpoint_dir=$(latest_checkpoint)
  [[ -n "$checkpoint_dir" ]] || die 'no controller checkpoint is available'
  drain_current
  restore_checkpoint || die 'checkpoint restoration failed'
  note "ROLLBACK_OK checkpoint=$checkpoint_dir"
}

perform_uninstall() {
  local old_release=
  old_release=$(current_runtime_release)
  make_checkpoint
  transaction_active=true
  drain_current
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
  rm -f "$manager_current"
  transaction_active=false
  note "UNINSTALL_OK host_config_preserved=$host_config secrets_preserved=$etc_dir/secrets"
}

require_commands
install -d -m 0755 "$(dirname "$lock_file")"
exec 9>"$lock_file"
flock -n 9 || die 'another ci-fleet installer or drift check is already running'
case "$mode" in
  check|install|adopt|upgrade)
    validate_common_arguments
    resolve_config
    validate_candidate_config_commit
    prepare_host_config
    verify_host_files
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
