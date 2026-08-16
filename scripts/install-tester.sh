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
for command in awk bash chmod cmp curl date df dirname docker du find flock getent git grep install ln mktemp mv python3 readlink rm shellcheck stat systemctl tar wc; do command -v "$command" >/dev/null || die "required command is unavailable: $command"; done

repo_root=$(git -C "$(dirname "${BASH_SOURCE[0]}")/.." rev-parse --show-toplevel 2>/dev/null) || die 'installer must run from a Git checkout'
opt_dir=$(root_path /opt/ci-fleet-tester)
release_dir=$opt_dir/releases
current_link=$opt_dir/current
state_root=$(root_path /var/lib/ci-fleet-tester)
lkg_file=$state_root/last-known-good
systemd_dir=$(root_path /etc/systemd/system)
config_root=$(root_path /etc/ci-fleet-tester)
environment_dir=$config_root/environments
definition_dir=$config_root/definitions
secret_root=$config_root/secrets
runtime_state=$state_root/environments
docker_root=$(root_path /var/lib/docker)
docker_socket=$(root_path /var/run/docker.sock)
units=(ci-fleet-tester-health.service ci-fleet-tester-health.timer ci-fleet-tester-cleanup.service ci-fleet-tester-cleanup.timer)
timers=(ci-fleet-tester-health.timer ci-fleet-tester-cleanup.timer)

secure_file() { [[ -f $1 && ! -L $1 && $(stat -c %u "$1") == "$expected_uid" && $(stat -c %a "$1") == "$2" ]] || die "protected file is unsafe: $1"; }
secure_dir() { [[ -d $1 && ! -L $1 && $(stat -c %u "$1") == "$expected_uid" && $(stat -c %a "$1") == "$2" ]] || die "protected directory is unsafe: $1"; }

host_preflight() {
  local os_release docker_context actual_root used
  os_release=$(root_path /etc/os-release)
  [[ -f $os_release ]] || die 'supported Debian os-release is missing'
  # shellcheck disable=SC1090
  . "$os_release"
  [[ ${ID:-} == debian && ${VERSION_ID:-} =~ ^[0-9]+$ && ${VERSION_ID%%.*} -ge 12 ]] || die 'tester hosts require Debian 12 or newer'
  docker_context=$(docker context show); [[ $docker_context == default ]] || die 'tester requires the local default Docker context'
  [[ -S $docker_socket || ( ${CI_FLEET_TESTING:-0} == 1 && -e $docker_socket ) ]] || die 'local Docker socket is unavailable'
  actual_root=$(docker info --format '{{.DockerRootDir}}'); [[ $actual_root == "$docker_root" ]] || die 'Docker root does not match the local managed root'
  docker compose version >/dev/null
  used=$(df -P "$docker_root" | awk 'NR==2{gsub(/%/,"",$5);print $5}')
  [[ $used =~ ^[0-9]+$ && $used -private-repository 80 ]] || die 'Docker storage is at or above 80%'
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
  local path=$1 expected=$2 unit
  [[ -d $path && ! -L $path && -x $path/scripts/tester-runtime.sh && -f $path/.ci-fleet-source-revision ]] || return 1
  [[ $(<"$path/.ci-fleet-source-revision") == "$expected" ]] || return 1
  for unit in "${units[@]}"; do [[ -f $path/host/systemd/$unit ]] || return 1; done
}

stage_release() {
  local commit=$1 target=$release_dir/$1 staging=$release_dir/.staging-$1
  if [[ -e $target ]]; then release_complete "$target" "$commit" || die 'existing tester release is incomplete'; return; fi
  rm -rf -- "$staging"; install -d -m 0755 "$staging"
  git -C "$repo_root" cat-file -e "$commit^{commit}" 2>/dev/null || die 'requested source commit is unavailable locally'
  git -C "$repo_root" archive "$commit" scripts/tester-runtime.sh host/systemd/ci-fleet-tester-health.service host/systemd/ci-fleet-tester-health.timer host/systemd/ci-fleet-tester-cleanup.service host/systemd/ci-fleet-tester-cleanup.timer | tar -x -C "$staging"
  printf '%s\n' "$commit" >"$staging/.ci-fleet-source-revision"; chmod 0644 "$staging/.ci-fleet-source-revision"
  chmod 0755 "$staging/scripts/tester-runtime.sh"; shellcheck "$staging/scripts/tester-runtime.sh"; bash -n "$staging/scripts/tester-runtime.sh"
  mv -T "$staging" "$target"
}

install_units() {
  local source=$1 unit
  for unit in "${units[@]}"; do install -m 0644 "$source/host/systemd/$unit" "$systemd_dir/$unit"; done
  systemctl daemon-reload
  systemctl enable --now "${timers[@]}" >/dev/null
}

activate_release() {
  local commit=$1 target=$release_dir/$1 previous=
  release_complete "$target" "$commit" || die 'candidate tester release is incomplete'
  [[ ! -L $current_link ]] || previous=$(basename "$(readlink -f "$current_link")")
  ln -sfn "$target" "$current_link.new"; mv -Tf "$current_link.new" "$current_link"
  install_units "$target"
  if ! "$target/scripts/tester-runtime.sh" --check || ! "$target/scripts/tester-runtime.sh" --health; then
    if [[ $previous =~ ^[0-9a-f]{40}$ ]] && release_complete "$release_dir/$previous" "$previous"; then
      ln -sfn "$release_dir/$previous" "$current_link.new"; mv -Tf "$current_link.new" "$current_link"; install_units "$release_dir/$previous"
    fi
    die 'candidate tester activation failed; previous release restored when available'
  fi
  [[ ! $previous =~ ^[0-9a-f]{40}$ || $previous == "$commit" ]] || printf '%s\n' "$previous" >"$lkg_file"
  report "INSTALL_OK source_revision=$commit previous_revision=${previous:-none} config=$config"
}

installed_revision() {
  [[ -L $current_link ]] || return 1
  local target; target=$(readlink -f "$current_link")
  [[ $target == "$release_dir"/* ]] || return 1
  basename "$target"
}

case $action in
  --install|--upgrade)
    host_preflight; ensure_directories
    secure_file "$(root_path "$config")" 600
    [[ $(git -C "$repo_root" rev-parse 'HEAD^{commit}') == "$ref" ]] || die 'reviewed checkout HEAD does not match --ref'
    if ! git -C "$repo_root" diff --quiet || ! git -C "$repo_root" diff --cached --quiet; then die 'reviewed checkout has tracked changes'; fi
    current=$(installed_revision || true)
    [[ $action != --install || -z $current || $current == "$ref" ]] || die 'tester is already installed at another revision; use --upgrade'
    [[ $action != --upgrade || -n $current ]] || die 'tester is not installed; use --install'
    stage_release "$ref"; activate_release "$ref"
    ;;
  --check)
    host_preflight; ensure_directories
    secure_file "$(root_path "$config")" 600
    current=$(installed_revision) || die 'tester is not installed'
    release_complete "$release_dir/$current" "$current" || die 'installed release is incomplete'
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
    ensure_directories; secure_file "$lkg_file" 600
    target=$(<"$lkg_file"); [[ $target =~ ^[0-9a-f]{40}$ ]] || die 'last-known-good revision is invalid'
    current=$(installed_revision || true)
    activate_release "$target"
    [[ ! $current =~ ^[0-9a-f]{40}$ || $current == "$target" ]] || printf '%s\n' "$current" >"$lkg_file"
    report "ROLLBACK_OK source_revision=$target"
    ;;
  --uninstall)
    ensure_directories
    if find "$runtime_state" -maxdepth 1 -type f -name '*.state' | grep -q .; then die 'remove every test environment before uninstalling the tester service'; fi
    systemctl disable --now "${timers[@]}" >/dev/null 2>&1 || true
    for unit in "${units[@]}"; do rm -f -- "$systemd_dir/$unit"; done
    systemctl daemon-reload
    rm -f -- "$current_link" "$lkg_file"; rm -rf -- "$release_dir"; install -d -m 0755 "$release_dir"
    report 'UNINSTALL_OK preserved_config=true preserved_definitions=true preserved_secrets=true'
    ;;
esac
