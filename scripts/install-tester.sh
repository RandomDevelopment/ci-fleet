#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
report() { printf '%s\n' "$*"; }
usage() { printf 'Usage: install-tester.sh {--check|--install|--upgrade|--rollback|--uninstall|--reset} [--ref COMMIT] [--config /etc/ci-fleet-tester/tester.env] [--environment ID]\n'; }

action=; ref=; config=/etc/ci-fleet-tester/tester.env; environment=
while (($#)); do
  case $1 in
    --check|--install|--upgrade|--rollback|--uninstall|--reset) [[ -z $action ]] || die 'choose one action'; action=$1; shift ;;
    --ref) (($# >= 2)) || die '--ref requires a value'; ref=$2; shift 2 ;;
    --config) (($# >= 2)) || die '--config requires a value'; config=$2; shift 2 ;;
    --environment) (($# >= 2)) || die '--environment requires a value'; environment=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "unknown argument: $1" ;;
  esac
done
[[ -n $action ]] || { usage; exit 2; }
[[ $config == /etc/ci-fleet-tester/tester.env ]] || die 'only the fixed tester configuration path is supported'
case $action in
  --install|--upgrade) [[ $ref =~ ^[0-9a-f]{40}$ ]] || die '--ref must be an immutable 40-character commit'; [[ -z $environment ]] || die '--environment is not valid for this action' ;;
  --reset) [[ $environment =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || die '--reset requires --environment'; [[ -z $ref ]] || die '--ref is not valid for reset' ;;
  *) [[ -z $ref && -z $environment ]] || die '--ref/--environment is not valid for this action' ;;
esac

root_prefix=${CI_FLEET_ROOT_PREFIX:-}
[[ -z $root_prefix || ${CI_FLEET_TESTING:-0} == 1 ]] || die 'CI_FLEET_ROOT_PREFIX is test-only'
root_path() { printf '%s%s' "$root_prefix" "$1"; }
expected_uid=0
[[ ${CI_FLEET_TESTING:-0} != 1 ]] || expected_uid=$(id -u)
if [[ ${CI_FLEET_TESTING:-0} != 1 && ${EUID:-$(id -u)} -ne 0 ]]; then die 'run installer as root'; fi
case $action in
  --install|--upgrade) required_commands='awk bash chmod cmp curl date df dirname docker du find flock getent git grep install ln mktemp mv python3 readlink rm sha256sum shellcheck stat sync systemctl tar wc' ;;
  --check) required_commands='awk bash basename cmp df dirname docker env flock grep mkdir python3 readlink sha256sum stat systemctl' ;;
  --reset) required_commands='bash basename dirname env flock mkdir readlink stat' ;;
  --rollback) required_commands='bash basename chmod dirname env flock install ln mkdir mv readlink rm sha256sum stat sync systemctl awk' ;;
  --uninstall) required_commands='bash chmod dirname docker env find flock grep install mkdir rm stat systemctl' ;;
esac
for command in $required_commands; do command -v "$command" >/dev/null || die "required command is unavailable: $command"; done

if [[ $action == --install || $action == --upgrade ]]; then
  repo_root=$(git -C "$(dirname "${BASH_SOURCE[0]}")/.." rev-parse --show-toplevel 2>/dev/null) || die 'installer must run from a Git checkout'
fi
opt_dir=$(root_path /opt/ci-fleet-tester)
release_dir=$opt_dir/releases
current_link=$opt_dir/current
stable_launcher=$opt_dir/tester-runtime
state_root=$(root_path /var/lib/ci-fleet-tester)
lkg_file=$state_root/last-known-good
systemd_dir=$(root_path /etc/systemd/system)
config_root=$(root_path /etc/ci-fleet-tester)
environment_dir=$config_root/environments
definition_dir=$config_root/definitions
secret_root=$config_root/secrets
runtime_state=$state_root/environments
activation_pending=$state_root/activation-pending
docker_root=$(root_path /var/lib/docker)
docker_socket=$(root_path /var/run/docker.sock)
runtime_lock=$(root_path /run/lock/ci-fleet-tester/runtime.lock)
lifecycle_lock=$(root_path /run/lock/ci-fleet-tester/lifecycle.lock)
services=(ci-fleet-tester-health.service ci-fleet-tester-cleanup.service)
timers=(ci-fleet-tester-health.timer ci-fleet-tester-cleanup.timer)
units=("${services[@]}" "${timers[@]}")
tmpfiles_conf=ci-fleet-tester-lock.conf
tmpfiles_dir=$(root_path /usr/lib/tmpfiles.d)

protected_file() { [[ -f $1 && ! -L $1 && $(stat -c %u "$1") == "$expected_uid" && $(stat -c %a "$1") == "$2" ]]; }
protected_dir() { [[ -d $1 && ! -L $1 && $(stat -c %u "$1") == "$expected_uid" && $(stat -c %a "$1") == "$2" ]]; }
secure_file() { protected_file "$1" "$2" || die "protected file is unsafe: $1"; }
secure_dir() { protected_dir "$1" "$2" || die "protected directory is unsafe: $1"; }
remove_release_tree() { [[ ! -e $1 ]] || { [[ ${CI_FLEET_TESTING:-0} != 1 ]] || chmod -R u+w "$1"; rm -rf -- "$1"; }; }

reject_git_replacements() {
  local common
  common=$(git -C "$repo_root" rev-parse --path-format=absolute --git-common-dir)
  [[ -z $(git -C "$repo_root" for-each-ref --format='%(refname)' refs/replace) && ! -s $common/info/grafts ]] || die 'Git replacement or graft metadata is forbidden'
}

acquire_lifecycle_lock() {
  mkdir -m 0755 "$(dirname "$runtime_lock")" 2>/dev/null || true
  secure_dir "$(dirname "$runtime_lock")" 755
  exec 9>"$lifecycle_lock"
  flock -x 9
  exec 8>"$runtime_lock"
  flock -x 8
  export CI_FLEET_TESTER_LOCK_FD=8
  exec 7>"$(root_path /run/ci-fleet-role-admission.lock)"
  flock -n 7 || die 'another ci-fleet role installation is already running'
}

reject_other_roles() {
  local path inventory_kind inventory_status inventory unit
  for path in "$(root_path /etc/ci-fleet)" "$(root_path /var/lib/ci-fleet)" "$(root_path /opt/ci-fleet/current)" "$(root_path /opt/ci-fleet/manager/current)" \
    "$(root_path /etc/ci-fleet-deployer)" "$(root_path /var/lib/ci-fleet-deployer)" "$(root_path /var/lib/ci-fleet-deployer-container-credentials)" "$(root_path /opt/ci-fleet-deployer)" \
    "$(root_path /etc/ci-fleet-status)" "$(root_path /var/lib/ci-fleet-status)" \
    "$(root_path /opt/ci-fleet-status)" "$(root_path /var/lib/ci-fleet-status-installer)" \
    "$systemd_dir"/ci-fleet-deployer* "$systemd_dir"/ci-fleet-health* "$systemd_dir"/ci-fleet-cleanup* \
    "$systemd_dir"/ci-fleet-reconcile* "$systemd_dir"/ci-fleet-drift* "$systemd_dir"/ci-fleet-status-receiver* "$systemd_dir"/actions.runner*; do
    [[ ! -e $path && ! -L $path ]] || die 'another ci-fleet host role is present'
  done
  for inventory_kind in list-unit-files list-units; do
    inventory_status=0
    inventory=$(SYSTEMD_COLORS=0 systemctl "$inventory_kind" --all --full --plain --no-legend --no-pager 2>&1) || inventory_status=$?
    # list-unit-files returns status 1 for an empty inventory without diagnostics.
    if ((inventory_status != 0)); then
      [[ $inventory_kind == list-unit-files && $inventory_status == 1 && -z $inventory ]] || die 'foreign systemd unit inventory could not be inspected'
    fi
    while read -r unit _; do
      case $unit in
        ci-fleet-deployer*|ci-fleet-health*|ci-fleet-cleanup*|ci-fleet-reconcile*|ci-fleet-drift*|ci-fleet-status-receiver*|actions.runner*) die 'another ci-fleet host role is present' ;;
      esac
    done <<<"$inventory"
  done
}

reject_unit_dropins() {
  local unit directory dropin kind
  for unit in "${units[@]}"; do
    kind=${unit##*.}
    for directory in /etc/systemd/system /run/systemd/system /usr/lib/systemd/system /lib/systemd/system; do
      for dropin in "$unit.d" "$kind.d" "ci-.$kind.d" "ci-fleet-.$kind.d" "ci-fleet-tester-.$kind.d"; do
        [[ ! -e $(root_path "$directory")/$dropin && ! -L $(root_path "$directory")/$dropin ]] || die 'unmanaged tester systemd drop-ins are forbidden'
      done
    done
  done
}

reject_tmpfiles_overrides() {
  local directory
  for directory in /etc/tmpfiles.d /run/tmpfiles.d /usr/local/lib/tmpfiles.d; do
    [[ ! -e $(root_path "$directory")/$tmpfiles_conf && ! -L $(root_path "$directory")/$tmpfiles_conf ]] || die 'unmanaged tester tmpfiles overrides are forbidden'
  done
}

effective_units_match() {
  local unit properties key value fragment dropins reload
  for unit in "${units[@]}"; do
    properties=$(systemctl show --property=FragmentPath --property=DropInPaths --property=NeedDaemonReload "$unit") || return 1
    fragment=; dropins=; reload=
    while IFS='=' read -r key value; do
      case $key in FragmentPath) fragment=$value ;; DropInPaths) dropins=$value ;; NeedDaemonReload) reload=$value ;; esac
    done <<<"$properties"
    [[ $fragment == "$systemd_dir/$unit" && -z $dropins && $reload == no ]] || return 1
  done
}

pin_local_docker() {
  local socket_mode docker_context actual_root
  [[ -z ${DOCKER_HOST:-} && -z ${DOCKER_CONTEXT:-} ]] || die 'Docker environment selectors are forbidden'
  unset DOCKER_CONTEXT
  export DOCKER_HOST="unix://$docker_socket"
  [[ -S $docker_socket || ( ${CI_FLEET_TESTING:-0} == 1 && -e $docker_socket ) ]] || die 'local Docker socket is unavailable'
  socket_mode=$(stat -c %a "$docker_socket")
  [[ ! -L $docker_socket && $(stat -c %u "$docker_socket") == "$expected_uid" ]] || die 'local root-owned Docker socket is unavailable'
  (( (8#$socket_mode & 0002) == 0 )) || die 'local Docker socket is writable outside its administration group'
  docker_context=$(docker context show); [[ $docker_context == default ]] || die 'tester requires the local default Docker context'
  actual_root=$(docker info --format '{{.DockerRootDir}}'); [[ $actual_root == "$docker_root" ]] || die 'Docker root does not match the local managed root'
}

reject_managed_compose_resources() {
  local kind=$1 inventory resource project format
  local -a inspect_command
  case $kind in
    container) inventory=$(docker ps -aq --filter label=com.docker.compose.project) || die "Docker Compose $kind inventory could not be inspected"; inspect_command=(docker inspect); format='{{index .Config.Labels "com.docker.compose.project"}}' ;;
    volume) inventory=$(docker volume ls -q --filter label=com.docker.compose.project) || die "Docker Compose $kind inventory could not be inspected"; inspect_command=(docker volume inspect); format='{{index .Labels "com.docker.compose.project"}}' ;;
    network) inventory=$(docker network ls -q --filter label=com.docker.compose.project) || die "Docker Compose $kind inventory could not be inspected"; inspect_command=(docker network inspect); format='{{index .Labels "com.docker.compose.project"}}' ;;
  esac
  while IFS= read -r resource; do
    [[ -n $resource ]] || continue
    project=$("${inspect_command[@]}" --format "$format" "$resource") || die "Docker Compose $kind identity could not be inspected"
    [[ $project != ci-fleet-test-* ]] || die 'remove every managed Docker Compose project before uninstalling the tester service'
  done <<<"$inventory"
}

host_preflight() {
  local os_release used
  os_release=$(root_path /etc/os-release)
  [[ -f $os_release ]] || die 'supported Debian os-release is missing'
  # shellcheck disable=SC1090
  . "$os_release"
  [[ ${ID:-} == debian && ${VERSION_ID:-} =~ ^[0-9]+$ && ${VERSION_ID%%.*} -ge 12 ]] || die 'tester hosts require Debian 12 or newer'
  reject_other_roles
  reject_unit_dropins
  reject_tmpfiles_overrides
  python3 -c 'import yaml' 2>/dev/null || die 'python3-yaml is required for Compose source validation'
  pin_local_docker
  [[ $(docker info --format '{{len .DefaultAddressPools}}') =~ ^[1-9][0-9]*$ ]] || die 'explicit Docker default address pools are required'
  [[ $(docker info --format '{{.DefaultRuntime}}') == runc ]] || die 'Docker default runtime must be runc'
  docker compose version >/dev/null
  printf 'services: {}\n' | docker compose -f - config --format json >/dev/null || die 'Compose JSON rendering is unavailable'
  docker compose up --help | grep -q -- '--wait-timeout' || die 'Compose wait-timeout support is unavailable'
  used=$(df -P "$docker_root" | awk 'NR==2{gsub(/%/,"",$5);print $5}')
  [[ $used =~ ^[0-9]+$ && $used -lt 80 ]] || die 'Docker storage is at or above 80%'
}

ensure_directories() {
  local directory
  for directory in "$opt_dir" "$release_dir"; do
    if [[ -e $directory || -L $directory ]]; then secure_dir "$directory" 755; else install -d -m 0755 "$directory"; fi
  done
  for directory in "$config_root" "$environment_dir" "$definition_dir" "$secret_root" "$state_root" "$runtime_state"; do
    if [[ -e $directory || -L $directory ]]; then secure_dir "$directory" 700; else install -d -m 0700 "$directory"; fi
  done
  install -d -m 0755 "$systemd_dir"
}

release_complete() {
  local path=$1 expected=$2 directory file unit
  for directory in "$path" "$path/scripts" "$path/host" "$path/host/systemd"; do protected_dir "$directory" 555 || return 1; done
  for file in "$path/scripts/tester-runtime.sh" "$path/scripts/tester-launcher.sh"; do protected_file "$file" 555 || return 1; done
  for file in "$path/.ci-fleet-source-revision" "$path/.ci-fleet-release.sha256"; do protected_file "$file" 444 || return 1; done
  [[ $(<"$path/.ci-fleet-source-revision") == "$expected" ]] || return 1
  for unit in "${units[@]}" "$tmpfiles_conf"; do protected_file "$path/host/systemd/$unit" 444 || return 1; done
  (cd "$path" && sha256sum --status -c .ci-fleet-release.sha256) || return 1
}

stage_release() {
  local commit=$1 target=$release_dir/$1 staging=$release_dir/.staging-$1 replaced=$release_dir/.replaced-$1
  if [[ -e $target ]] && release_complete "$target" "$commit"; then return; fi
  remove_release_tree "$staging"; remove_release_tree "$replaced"; install -d -m 0755 "$staging"
  GIT_NO_REPLACE_OBJECTS=1 git -C "$repo_root" cat-file -e "$commit^{commit}" 2>/dev/null || die 'requested source commit is unavailable locally'
  GIT_NO_REPLACE_OBJECTS=1 git -C "$repo_root" archive "$commit" scripts/tester-runtime.sh scripts/tester-launcher.sh host/systemd/ci-fleet-tester-health.service host/systemd/ci-fleet-tester-health.timer host/systemd/ci-fleet-tester-cleanup.service host/systemd/ci-fleet-tester-cleanup.timer host/systemd/ci-fleet-tester-lock.conf | tar -x -C "$staging"
  # Reject any non-regular or symlinked member before chmod can dereference it
  # (chmod follows symlinks and would change the host target's permissions).
  while IFS= read -r -d '' entry; do
    if [[ -L $entry || ! -f $entry ]]; then remove_release_tree "$staging"; die "release archive contains a non-regular or symlinked member: $entry"; fi
  done < <(find "$staging" ! -type d -print0)
  printf '%s\n' "$commit" >"$staging/.ci-fleet-source-revision"; chmod 0644 "$staging/.ci-fleet-source-revision"
  chmod 0755 "$staging/scripts/tester-runtime.sh" "$staging/scripts/tester-launcher.sh"; shellcheck "$staging/scripts/tester-runtime.sh" "$staging/scripts/tester-launcher.sh"; bash -n "$staging/scripts/tester-runtime.sh" "$staging/scripts/tester-launcher.sh"
  (cd "$staging" && sha256sum scripts/tester-runtime.sh scripts/tester-launcher.sh .ci-fleet-source-revision host/systemd/* >.ci-fleet-release.sha256)
  chmod 0444 "$staging/.ci-fleet-source-revision" "$staging/.ci-fleet-release.sha256" "$staging"/host/systemd/*
  chmod 0555 "$staging" "$staging/scripts" "$staging/host" "$staging/host/systemd" "$staging/scripts/tester-runtime.sh" "$staging/scripts/tester-launcher.sh"
  [[ ! -e $target ]] || mv -T "$target" "$replaced"
  if ! mv -T "$staging" "$target"; then [[ ! -e $replaced ]] || mv -T "$replaced" "$target"; die 'could not replace tester release'; fi
  remove_release_tree "$replaced"
}

install_units() {
  local source=$1 unit
  for unit in "${units[@]}"; do install -m 0644 "$source/host/systemd/$unit" "$systemd_dir/$unit.new" || return 1; done
  for unit in "${units[@]}"; do mv -fT "$systemd_dir/$unit.new" "$systemd_dir/$unit" || return 1; done
  install -d -m 0755 "$tmpfiles_dir"
  install -m 0644 "$source/host/systemd/$tmpfiles_conf" "$tmpfiles_dir/$tmpfiles_conf.new" || return 1
  mv -fT "$tmpfiles_dir/$tmpfiles_conf.new" "$tmpfiles_dir/$tmpfiles_conf" || return 1
  systemctl daemon-reload || return 1
  effective_units_match
}

create_tmpfiles() {
  # Recreate the volatile tester lock directory at install time (and at boot via
  # the installed tmpfiles.d drop-in) so maintenance units work after a reboot.
  mkdir -m 0755 "$(dirname "$runtime_lock")" 2>/dev/null || true
  secure_dir "$(dirname "$runtime_lock")" 755
  if command -v systemd-tmpfiles >/dev/null 2>&1; then systemd-tmpfiles --create "$tmpfiles_dir/$tmpfiles_conf" 2>/dev/null; fi
}

enable_timers() { systemctl enable --now "${timers[@]}" >/dev/null; }
start_maintenance() {
  local status=0
  systemctl start --no-block "${services[@]}" >/dev/null || return 1
  flock -u 8; unset CI_FLEET_TESTER_LOCK_FD
  systemctl start "${services[@]}" >/dev/null || status=$?
  flock -x 8 || return 1
  export CI_FLEET_TESTER_LOCK_FD=8
  return "$status"
}
install_launcher() { install -m 0555 "$1/scripts/tester-launcher.sh" "$stable_launcher.new" && mv -fT "$stable_launcher.new" "$stable_launcher"; }

remove_units() {
  local unit directory load_state present_units=() present_timers=() present_services=()
  for unit in "${units[@]}"; do
    if [[ -L $systemd_dir/$unit && ! -e $systemd_dir/$unit ]]; then
      load_state=$(systemctl show --property=LoadState --value "$unit") || return 1
      [[ $load_state == not-found || $load_state == loaded ]] || return 1
      if [[ $load_state == loaded ]]; then systemctl stop "$unit" >/dev/null 2>&1 || return 1; fi
      rm -f -- "$systemd_dir/$unit" || return 1
      for directory in timers.target.wants multi-user.target.wants; do
        [[ ! -L $systemd_dir/$directory/$unit ]] || rm -f -- "$systemd_dir/$directory/$unit" || return 1
      done
    fi
    [[ ! -e $systemd_dir/$unit && ! -L $systemd_dir/$unit ]] || present_units+=("$unit")
  done
  for unit in "${timers[@]}"; do
    if [[ -e $systemd_dir/$unit || -L $systemd_dir/$unit ]]; then
      present_timers+=("$unit")
    else
      load_state=$(systemctl show --property=LoadState --value "$unit") || return 1
      [[ -n $load_state ]] || return 1
      [[ $load_state == not-found ]] || present_timers+=("$unit")
    fi
  done
  if (( ${#present_timers[@]} )); then
    systemctl disable --now "${present_timers[@]}" >/dev/null 2>&1 || return 1
  fi
  for unit in "${services[@]}"; do
    if [[ -e $systemd_dir/$unit || -L $systemd_dir/$unit ]]; then
      present_services+=("$unit")
    else
      load_state=$(systemctl show --property=LoadState --value "$unit") || return 1
      [[ -n $load_state ]] || return 1
      [[ $load_state == not-found ]] || present_services+=("$unit")
    fi
  done
  if (( ${#present_services[@]} )); then
    systemctl stop "${present_services[@]}" >/dev/null 2>&1 || return 1
  fi
  for unit in "${units[@]}"; do rm -f -- "$systemd_dir/$unit" "$systemd_dir/$unit.new" || return 1; done
  rm -f -- "$tmpfiles_dir/$tmpfiles_conf" "$tmpfiles_dir/$tmpfiles_conf.new" "$stable_launcher.new" || return 1
  systemctl daemon-reload
}

write_lkg() {
  printf '%s\n' "$1" >"$lkg_file.new"
  chmod 0600 "$lkg_file.new"
  sync -f "$lkg_file.new" || return 1
  mv -fT "$lkg_file.new" "$lkg_file" || return 1
  sync -f "$state_root"
}

retire_activation() {
  local directory
  for directory in "$opt_dir" "$state_root" "$systemd_dir" "$tmpfiles_dir"; do
    [[ ! -d $directory ]] || sync -f "$directory" || return 1
  done
  rm -f -- "$activation_pending" || return 1
  sync -f "$state_root"
}

recover_activation() {
  local previous candidate record_previous phase pid started boot extra
  for command in basename ln mv sha256sum sync; do command -v "$command" >/dev/null || die "activation recovery requires $command"; done
  secure_dir "$state_root" 700
  secure_dir "$opt_dir" 755
  secure_dir "$release_dir" 755
  secure_file "$activation_pending" 600
  read -r previous candidate record_previous phase pid started boot extra <"$activation_pending"
  [[ ( $previous == none || $previous =~ ^[0-9a-f]{40}$ ) && $candidate =~ ^[0-9a-f]{40}$ && $record_previous =~ ^[01]$ && $phase =~ ^(validate|probes)$ && $pid =~ ^[1-9][0-9]*$ && $started =~ ^[0-9]+$ && $boot =~ ^[a-f0-9-]{36}$ && -z $extra ]] || die 'activation journal is invalid'
  reject_other_roles
  reject_unit_dropins
  reject_tmpfiles_overrides
  if [[ $previous != none ]]; then
    release_complete "$release_dir/$previous" "$previous" || die 'interrupted activation incumbent is incomplete; operator recovery is required'
    ln -sfn "$release_dir/$previous" "$current_link.new"; mv -Tf "$current_link.new" "$current_link"
    if ! install_launcher "$release_dir/$previous" || ! install_units "$release_dir/$previous" || ! create_tmpfiles || ! enable_timers; then die 'interrupted activation incumbent unit restore failed; journal retained'; fi
  else
    remove_units || die 'interrupted fresh installation teardown failed; journal retained'
    rm -f -- "$current_link" "$stable_launcher"
  fi
  retire_activation || die 'activation recovery retirement failed'
  recovered_rollback=$((1 - record_previous))
  report "ACTIVATION_RECOVERED previous_revision=$previous candidate_revision=$candidate"
}

write_activation_journal() {
  local previous candidate record_previous phase pid started boot extra
  if [[ -e $activation_pending || -L $activation_pending ]]; then
    secure_file "$activation_pending" 600
    read -r previous candidate record_previous phase pid started boot extra <"$activation_pending"
    [[ ${4:-validate} == probes && $phase == validate && $previous == "${1:-none}" && $candidate == "$2" && $record_previous == "$3" && $pid == "$$" && -z $extra ]] || die 'outstanding activation requires recovery'
  fi
  [[ ! -e $activation_pending.new && ! -L $activation_pending.new ]] || secure_file "$activation_pending.new" 600
  printf '%s %s %s %s %s %s %s\n' "${1:-none}" "$2" "$3" "${4:-validate}" "$$" "$(awk '{print $22}' /proc/$$/stat)" "$(</proc/sys/kernel/random/boot_id)" >"$activation_pending.new"
  chmod 0600 "$activation_pending.new"
  sync -f "$activation_pending.new" || die 'activation journal is not durable'
  mv -Tf "$activation_pending.new" "$activation_pending"
  sync -f "$state_root" || die 'activation journal publication is not durable'
}

activate_release() {
  local commit=$1 target=$release_dir/$1 previous='' record_previous=${2:-1}
  reject_other_roles
  reject_unit_dropins
  reject_tmpfiles_overrides
  release_complete "$target" "$commit" || die 'candidate tester release is incomplete'
  [[ ! -L $current_link ]] || previous=$(basename "$(readlink -f "$current_link")")
  if [[ $previous =~ ^[0-9a-f]{40}$ ]] && ! systemctl disable --now "${timers[@]}" >/dev/null; then
    enable_timers || die 'could not quiesce or restore tester maintenance timers'
    die 'could not quiesce tester maintenance timers; incumbent timers restored'
  fi
  write_activation_journal "$previous" "$commit" "$record_previous"
  ln -sfn "$target" "$current_link.new"; mv -Tf "$current_link.new" "$current_link"
  sync -f "$opt_dir" || die 'candidate pointer is not durable'
  if ! install_launcher "$target" || ! install_units "$target" || ! create_tmpfiles || ! CI_FLEET_TESTER_ACTIVATION_FD=9 "$stable_launcher" --check || ! CI_FLEET_TESTER_ACTIVATION_FD=9 "$stable_launcher" --health; then
    if [[ $previous =~ ^[0-9a-f]{40}$ ]] && release_complete "$release_dir/$previous" "$previous"; then
      ln -sfn "$release_dir/$previous" "$current_link.new"; mv -Tf "$current_link.new" "$current_link"
      if ! install_launcher "$release_dir/$previous" || ! install_units "$release_dir/$previous" || ! create_tmpfiles || ! enable_timers; then die 'candidate activation failed and incumbent unit restore failed; launcher and incumbent link retained for recovery'; fi
    else
      remove_units || die 'candidate activation failed and fresh-install unit teardown also failed; candidate retained for recovery'
      rm -f -- "$current_link" "$stable_launcher"
    fi
    retire_activation || die 'candidate restore succeeded but activation journal retirement failed'
    die 'candidate tester activation failed; previous release restored when available'
  fi
  if [[ $record_previous == 1 && $previous =~ ^[0-9a-f]{40}$ && $previous != "$commit" ]] && release_complete "$release_dir/$previous" "$previous" && ! write_lkg "$previous"; then
    ln -sfn "$release_dir/$previous" "$current_link.new"; mv -Tf "$current_link.new" "$current_link"
    if ! install_launcher "$release_dir/$previous" || ! install_units "$release_dir/$previous" || ! enable_timers; then die 'last-known-good recording failed and incumbent restore failed; incumbent link retained for recovery'; fi
    retire_activation || die 'incumbent restore succeeded but activation journal retirement failed'
    die 'last-known-good recording failed; previous release restored'
  fi
  if ! enable_timers; then
    recover_activation
    die 'candidate timer activation failed; incumbent restored'
  fi
  write_activation_journal "$previous" "$commit" "$record_previous" probes
  if ! start_maintenance; then
    recover_activation
    die 'candidate maintenance probes failed; incumbent restored'
  fi
  retire_activation || die 'completed activation journal retirement failed'
  report "INSTALL_OK source_revision=$commit previous_revision=${previous:-none} config=$config"
}

installed_revision() {
  [[ -L $current_link ]] || return 1
  local target; target=$(readlink -f "$current_link")
  [[ $target == "$release_dir"/* ]] || return 1
  basename "$target"
}

installation_matches() {
  local revision=$1 unit
  effective_units_match || return 1
  release_complete "$release_dir/$revision" "$revision" || return 1
  protected_file "$stable_launcher" 555 && cmp -s "$release_dir/$revision/scripts/tester-launcher.sh" "$stable_launcher" || return 1
  for unit in "${units[@]}"; do
    protected_file "$systemd_dir/$unit" 644 && cmp -s "$release_dir/$revision/host/systemd/$unit" "$systemd_dir/$unit" || return 1
  done
  protected_file "$tmpfiles_dir/$tmpfiles_conf" 644 && cmp -s "$release_dir/$revision/host/systemd/$tmpfiles_conf" "$tmpfiles_dir/$tmpfiles_conf" || return 1
  for unit in "${timers[@]}"; do
    systemctl is-enabled --quiet "$unit" && systemctl is-active --quiet "$unit" || return 1
  done
}

case $action in --install|--upgrade|--check|--reset|--rollback|--uninstall) acquire_lifecycle_lock ;; esac

if [[ -e $activation_pending || -L $activation_pending ]]; then
  case $action in
    --install|--upgrade|--rollback|--uninstall) recover_activation ;;
    *) die 'interrupted tester activation requires installer retry or rollback' ;;
  esac
  if [[ $action == --rollback && $recovered_rollback == 0 ]]; then report "ROLLBACK_OK source_revision=$(installed_revision || printf none) recovered_pending=true"; exit 0; fi
fi

case $action in
  --install|--upgrade)
    host_preflight
    secure_dir "$config_root" 700
    secure_file "$(root_path "$config")" 600
    reject_git_replacements
    [[ $(GIT_NO_REPLACE_OBJECTS=1 git -C "$repo_root" rev-parse 'HEAD^{commit}') == "$ref" ]] || die 'reviewed checkout HEAD does not match --ref'
    if ! git -C "$repo_root" diff --quiet || ! git -C "$repo_root" diff --cached --quiet; then die 'reviewed checkout has tracked changes'; fi
    current=$(installed_revision || true)
    [[ $action != --install || -z $current || $current == "$ref" ]] || die 'tester is already installed at another revision; use --upgrade'
    [[ $action != --upgrade || -n $current ]] || die 'tester is not installed; use --install'
    ensure_directories
    if [[ $current == "$ref" ]] && installation_matches "$ref"; then
      "$stable_launcher" --check
      "$stable_launcher" --health
      report "INSTALL_OK source_revision=$ref changed=false"
      exit 0
    fi
    stage_release "$ref"; activate_release "$ref"
    ;;
  --check)
    host_preflight
    effective_units_match || die 'effective tester units differ from the installed policy'
    for directory in "$opt_dir" "$release_dir" "$systemd_dir"; do secure_dir "$directory" 755; done
    for directory in "$config_root" "$environment_dir" "$definition_dir" "$secret_root" "$state_root" "$runtime_state"; do secure_dir "$directory" 700; done
    secure_file "$(root_path "$config")" 600
    current=$(installed_revision) || die 'tester is not installed'
    release_complete "$release_dir/$current" "$current" || die 'installed release is incomplete'
    secure_file "$stable_launcher" 555
    cmp -s "$release_dir/$current/scripts/tester-launcher.sh" "$stable_launcher" || die 'installed launcher differs from active release'
    for unit in "${units[@]}"; do
      secure_file "$systemd_dir/$unit" 644
      cmp -s "$release_dir/$current/host/systemd/$unit" "$systemd_dir/$unit" || die "installed unit differs from active release: $unit"
    done
    secure_file "$tmpfiles_dir/$tmpfiles_conf" 644
    cmp -s "$release_dir/$current/host/systemd/$tmpfiles_conf" "$tmpfiles_dir/$tmpfiles_conf" || die 'installed tmpfiles rule differs from active release'
    for timer in "${timers[@]}"; do
      if ! systemctl is-enabled --quiet "$timer" || ! systemctl is-active --quiet "$timer"; then die "timer is inactive: $timer"; fi
    done
    "$current_link/scripts/tester-runtime.sh" --check
    "$current_link/scripts/tester-runtime.sh" --health
    report "CHECK_OK source_revision=$current"
    ;;
  --reset)
    current=$(installed_revision) || die 'tester is not installed'
    "$current_link/scripts/tester-runtime.sh" --reset --environment "$environment"
    ;;
  --rollback)
    secure_dir "$state_root" 700
    secure_dir "$opt_dir" 755
    secure_dir "$release_dir" 755
    secure_file "$lkg_file" 600
    target=$(<"$lkg_file"); [[ $target =~ ^[0-9a-f]{40}$ ]] || die 'last-known-good revision is invalid'
    release_complete "$release_dir/$target" "$target" || die 'last-known-good release is incomplete'
    reject_other_roles
    ensure_directories
    activate_release "$target" 0
    report "ROLLBACK_OK source_revision=$target"
    ;;
  --uninstall)
    for directory in "$opt_dir" "$release_dir"; do
      [[ ! -e $directory && ! -L $directory ]] || secure_dir "$directory" 755
    done
    for directory in "$config_root" "$environment_dir" "$definition_dir" "$secret_root" "$state_root" "$runtime_state"; do
      [[ ! -e $directory && ! -L $directory ]] || secure_dir "$directory" 700
    done
    if [[ -d $runtime_state ]] && find "$runtime_state" -maxdepth 1 -type f -name '*.state' | grep -q .; then die 'remove every test environment before uninstalling the tester service'; fi
    pin_local_docker
    reject_managed_compose_resources container
    reject_managed_compose_resources volume
    reject_managed_compose_resources network
    remove_units || die 'could not stop and disable tester maintenance units'
    rm -f -- "$current_link" "$stable_launcher" "$lkg_file"
    if [[ -d $release_dir ]]; then
      [[ ${CI_FLEET_TESTING:-0} != 1 ]] || chmod -R u+w "$release_dir"
      rm -rf -- "$release_dir"
    fi
    report 'UNINSTALL_OK preserved_config=true preserved_definitions=true preserved_secrets=true'
    ;;
esac
