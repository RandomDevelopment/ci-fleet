#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  echo "usage: $0 (--install|--upgrade|--rollback|--check) [--ref COMMIT]" >&2
  exit 2
}

mode=
ref=
while (($#)); do
  case "$1" in
    --install|--upgrade|--rollback|--check) [[ -z "$mode" ]] || usage; mode=${1#--}; shift ;;
    --ref) (($# >= 2)) || usage; ref=$2; shift 2 ;;
    *) usage ;;
  esac
done
[[ -n "$mode" ]] || usage

root=${CI_FLEET_STATUS_ROOT:-}
test_mode=${CI_FLEET_STATUS_TEST_MODE:-0}
if [[ (-n "$root" && "$test_mode" != 1) || (-z "$root" && "$test_mode" != 0) ]]; then
  echo "alternate root requires CI_FLEET_STATUS_TEST_MODE=1 and a nonempty root" >&2
  exit 1
fi
if [[ -z "$root" && $EUID -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
install_root="$root/opt/ci-fleet-status"
state_root="$root/var/lib/ci-fleet-status"
metadata_root="$root/var/lib/ci-fleet-status-installer"
config_root="$root/etc/ci-fleet-status"
unit_path="$root/etc/systemd/system/ci-fleet-status-receiver.service"
current="$install_root/current"
unit_target="$current/ci-fleet-status-receiver.service"
previous="$metadata_root/previous-ref"
restart_required="$metadata_root/restart-required"
mkdir -p "$root/run/lock"
lock_directory="$root/run/lock/ci-fleet-status"
expected_lock_uid=0
[[ -z "$root" ]] || expected_lock_uid=$EUID
if ! mkdir -m 0700 "$lock_directory" 2>/dev/null; then
  [[ -d "$lock_directory" && ! -L "$lock_directory" ]]
  [[ $(stat -c '%u:%a' "$lock_directory") == "$expected_lock_uid:700" ]]
fi
exec 9<"$lock_directory"
flock 9

current_ref() {
  local link release releases target
  [[ -L "$current" ]] || return 1
  link=$(readlink "$current")
  [[ "$link" =~ ^releases/([0-9a-f]{40})$ ]] || return 1
  release=${BASH_REMATCH[1]}
  releases=$(realpath -e "$install_root/releases") || return 1
  target=$(realpath -e "$current") || return 1
  [[ -d "$target" && $(dirname "$target") == "$releases" && "$target" == "$releases/$release" ]] || return 1
  printf '%s\n' "$release"
}

write_metadata() {
  local destination=$1 value=$2 temporary
  temporary=$(mktemp "$metadata_root/.metadata.XXXXXX")
  printf '%s\n' "$value" >"$temporary"
  chmod 0600 "$temporary"
  mv -Tf "$temporary" "$destination"
}

link_unit() {
  local installed
  installed=$(current_ref) || { echo "receiver is not installed" >&2; exit 1; }
  validate_release "$install_root/releases/$installed"
  ensure_systemd_directory
  ln -sfn "$unit_target" "$unit_path.new"
  mv -Tf "$unit_path.new" "$unit_path"
}

activate() {
  local target=$1 record_previous=${2:-1} old=
  validate_release "$install_root/releases/$target" || { echo "release not installed safely: $target" >&2; exit 1; }
  old=$(current_ref || true)
  if [[ "$record_previous" == 1 && -n "$old" && "$old" != "$target" ]]; then
    write_metadata "$previous" "$old"
  fi
  ln -sfn "releases/$target" "$current.new"
  mv -Tf "$current.new" "$current"
}

restart_live_service() {
  local force=${1:-0}
  local installed
  installed=$(current_ref) || { echo "receiver is not installed" >&2; exit 1; }
  validate_release "$install_root/releases/$installed"
  ensure_systemd_directory
  [[ -n "$root" ]] && return
  systemctl daemon-reload
  if [[ "$force" == 1 ]] || systemctl is-active --quiet ci-fleet-status-receiver.service; then
    systemctl restart ci-fleet-status-receiver.service
  fi
}

managed_uid=0
expected_release_uid=0
if [[ -n "$root" ]]; then
  [[ ${CI_FLEET_STATUS_TEST_EXPECTED_OWNER:-} =~ ^[0-9]+$ ]] || {
    echo "test mode requires CI_FLEET_STATUS_TEST_EXPECTED_OWNER" >&2
    exit 1
  }
  managed_uid=$EUID
  expected_release_uid=$CI_FLEET_STATUS_TEST_EXPECTED_OWNER
fi
managed_directory() {
  local path=$1 create=${2:-0}
  if [[ -L "$path" ]]; then
    echo "unsafe managed release directory: $path" >&2
    exit 1
  elif [[ -e "$path" ]]; then
    [[ -d "$path" && $(stat -c '%u:%a' "$path") == "$managed_uid:755" ]] || {
      echo "unsafe managed release directory: $path" >&2
      exit 1
    }
  elif [[ "$create" == 1 ]]; then
    install -d -m 0755 "$path"
    [[ $(stat -c '%u:%a' "$path") == "$managed_uid:755" ]]
  else
    return 1
  fi
}

ensure_systemd_directory() {
  local path mode owner
  path=$(dirname "$unit_path")
  if [[ -L "$path" ]]; then
    echo "unsafe systemd directory: $path" >&2
    exit 1
  elif [[ ! -e "$path" ]]; then
    [[ "$test_mode" == 1 ]] || { echo "systemd directory is missing: $path" >&2; exit 1; }
    install -d -m 0755 "$path"
  fi
  [[ -d "$path" ]] || { echo "unsafe systemd directory: $path" >&2; exit 1; }
  owner=$(stat -c %u "$path")
  mode=$(stat -c %a "$path")
  [[ "$owner" == "$managed_uid" && $((8#$mode & 0300)) == $((8#0300)) && $((8#$mode & 022)) == 0 ]] || {
    echo "unsafe systemd directory: $path" >&2
    exit 1
  }
}

validate_release() {
  local release=$1 entry expected name mode stored_digest actual_digest
  local -a entries=()
  [[ ! -L "$release" && -d "$release" && $(stat -c '%F:%u:%a' "$release") == "directory:$expected_release_uid:755" ]] || {
    echo "unsafe receiver release: $release" >&2
    return 1
  }
  mapfile -d '' entries < <(find "$release" -mindepth 1 -maxdepth 1 -print0)
  ((${#entries[@]} == 4)) || { echo "unexpected receiver release contents: $release" >&2; return 1; }
  for expected in status_receiver.py:755 status_auth.py:644 ci-fleet-status-receiver.service:644 .ci-fleet-tree-sha256:644; do
    name=${expected%%:*}
    mode=${expected##*:}
    entry=$release/$name
    [[ ! -L "$entry" && $(stat -c '%F:%u:%a' "$entry" 2>/dev/null) == "regular file:$expected_release_uid:$mode" ]] || {
      echo "unsafe receiver artifact: $entry" >&2
      return 1
    }
  done
  stored_digest=$(<"$release/.ci-fleet-tree-sha256")
  [[ "$stored_digest" =~ ^[0-9a-f]{64}$ ]] || { echo "invalid receiver release digest: $release" >&2; return 1; }
  actual_digest=$(cd "$release" && sha256sum status_receiver.py status_auth.py ci-fleet-status-receiver.service | sha256sum | cut -d' ' -f1)
  [[ "$actual_digest" == "$stored_digest" ]] || { echo "modified receiver release: $release" >&2; return 1; }
}

validate_service_account() {
  local passwd_record=$1 group_record=$2 groups=$3 account uid gid home shell group group_gid
  IFS=: read -r account _ uid gid _ home shell <<<"$passwd_record"
  IFS=: read -r group _ group_gid _ <<<"$group_record"
  [[ "$account" == ci-fleet-status && "$group" == ci-fleet-status ]]
  [[ "$uid" =~ ^[0-9]+$ && "$gid" =~ ^[0-9]+$ && "$uid" != 0 && "$gid" != 0 && "$gid" == "$group_gid" ]]
  [[ "$home" == /nonexistent && "$shell" == /usr/sbin/nologin ]]
  [[ "$groups" == "$gid" ]] || { echo "ci-fleet-status has unexpected supplementary groups" >&2; return 1; }
}

python=/usr/bin/python3
if [[ "$test_mode" == 1 && -n ${CI_FLEET_STATUS_TEST_PYTHON:-} ]]; then
  python=$CI_FLEET_STATUS_TEST_PYTHON
fi
"$python" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))' || {
  echo "Python 3.9 or newer is required" >&2
  exit 1
}
sqlite_version=$("$python" -c 'import sqlite3; print(".".join(map(str, sqlite3.sqlite_version_info)))') || {
  echo "Python SQLite version detection failed" >&2
  exit 1
}
if [[ "$test_mode" == 1 && -n ${CI_FLEET_STATUS_TEST_SQLITE_VERSION:-} ]]; then
  sqlite_version=$CI_FLEET_STATUS_TEST_SQLITE_VERSION
fi
IFS=. read -r sqlite_major sqlite_minor sqlite_patch sqlite_extra <<<"$sqlite_version"
if [[ -n ${sqlite_extra:-} || ! ${sqlite_major:-} =~ ^[0-9]+$ || ! ${sqlite_minor:-} =~ ^[0-9]+$ || ! ${sqlite_patch:-} =~ ^[0-9]+$ ]] ||
   ((sqlite_major < 3 || (sqlite_major == 3 && sqlite_minor < 25))); then
  echo "SQLite 3.25.0 or newer is required by the Python receiver (found $sqlite_version)" >&2
  exit 1
fi

if [[ "$mode" == install || "$mode" == upgrade ]]; then
  managed_directory "$install_root" 1
  managed_directory "$install_root/releases" 1
else
  managed_directory "$install_root" || { echo "status receiver is not installed" >&2; exit 1; }
  managed_directory "$install_root/releases" || { echo "status receiver is not installed" >&2; exit 1; }
fi

if [[ -n ${CI_FLEET_STATUS_TEST_ACCOUNT_GROUPS:-} ]]; then
  validate_service_account 'ci-fleet-status:x:12345:12345::/nonexistent:/usr/sbin/nologin' \
    'ci-fleet-status:x:12345:' "$CI_FLEET_STATUS_TEST_ACCOUNT_GROUPS"
elif [[ -z "$root" && ("$mode" == check || "$mode" == rollback) ]]; then
  passwd_record=$(getent passwd ci-fleet-status) || { echo "ci-fleet-status account is missing" >&2; exit 1; }
  group_record=$(getent group ci-fleet-status) || { echo "ci-fleet-status group is missing" >&2; exit 1; }
  validate_service_account "$passwd_record" "$group_record" "$(id -G ci-fleet-status)"
fi

if [[ "$mode" == check ]]; then
  installed=$(current_ref) || { echo "receiver is not installed" >&2; exit 1; }
  validate_release "$install_root/releases/$installed"
  ensure_systemd_directory
  [[ -L "$unit_path" && $(readlink "$unit_path") == "$unit_target" ]]
  echo "CHECK_OK $installed"
  exit
fi

if [[ "$mode" == rollback ]]; then
  [[ -s "$previous" ]] || { echo "no rollback release recorded" >&2; exit 1; }
  target=$(<"$previous")
  [[ "$target" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid rollback release" >&2; exit 1; }
  validate_release "$install_root/releases/$target"
  ensure_systemd_directory
  force=0
  [[ -f "$restart_required" ]] && force=1
  activate "$target" 0
  link_unit
  restart_live_service "$force"
  rm -f "$restart_required"
  echo "ROLLED_BACK $target"
  exit
fi

[[ "$ref" =~ ^[0-9a-f]{40}$ ]] || usage
head=$(git -C "$repo_root" rev-parse HEAD)
[[ "$head" == "$ref" ]] || { echo "--ref must equal the reviewed checkout HEAD" >&2; exit 1; }
inputs=(
  scripts/install-status-receiver.sh
  scripts/status_auth.py
  scripts/status_receiver.py
  deploy/status-receiver/ci-fleet-status-receiver.service
)
git -C "$repo_root" diff --quiet HEAD -- "${inputs[@]}" || {
  echo "reviewed receiver inputs differ from HEAD" >&2
  exit 1
}
if [[ -z "$root" ]]; then
  getent passwd ci-fleet-status >/dev/null || \
    useradd --system --user-group --home /nonexistent --shell /usr/sbin/nologin ci-fleet-status
  validate_service_account "$(getent passwd ci-fleet-status)" "$(getent group ci-fleet-status)" "$(id -G ci-fleet-status)"
  install -d -o ci-fleet-status -g ci-fleet-status -m 0700 "$state_root" "$config_root"
  install -d -o root -g root -m 0700 "$metadata_root"
else
  install -d -m 0700 "$state_root" "$config_root" "$metadata_root"
fi
ensure_systemd_directory
existing=$(current_ref || true)
if [[ "$mode" == install && -n "$existing" && "$existing" != "$ref" ]]; then
  echo "use --upgrade to change an active release" >&2
  exit 1
fi
release="$install_root/releases/$ref"
if [[ -d "$release" ]]; then
  validate_release "$release"
  cmp -s "$repo_root/scripts/status_receiver.py" "$release/status_receiver.py"
  cmp -s "$repo_root/scripts/status_auth.py" "$release/status_auth.py"
  cmp -s "$repo_root/deploy/status-receiver/ci-fleet-status-receiver.service" \
    "$release/ci-fleet-status-receiver.service"
else
  staging=$(mktemp -d "$install_root/releases/.staging.XXXXXX")
  trap 'rm -rf "$staging"' EXIT
  chmod 0755 "$staging"
  install -m 0755 "$repo_root/scripts/status_receiver.py" "$staging/status_receiver.py"
  install -m 0644 "$repo_root/scripts/status_auth.py" "$staging/status_auth.py"
  install -m 0644 "$repo_root/deploy/status-receiver/ci-fleet-status-receiver.service" \
    "$staging/ci-fleet-status-receiver.service"
  (cd "$staging" && sha256sum status_receiver.py status_auth.py ci-fleet-status-receiver.service | sha256sum | cut -d' ' -f1) \
    >"$staging/.ci-fleet-tree-sha256"
  chmod 0644 "$staging/.ci-fleet-tree-sha256"
  mv -T "$staging" "$release"
  trap - EXIT
fi
validate_release "$release"

if [[ "$existing" == "$ref" ]]; then
  changed=0
  [[ -L "$unit_path" && $(readlink "$unit_path") == "$unit_target" ]] || changed=1
  link_unit
  [[ "$changed" == 0 ]] || restart_live_service
  echo NO_CHANGE
  exit
fi

if [[ "$mode" == upgrade && -z "$root" ]]; then
  if systemctl is-active --quiet ci-fleet-status-receiver.service; then
    write_metadata "$restart_required" 1
  else
    rm -f "$restart_required"
  fi
fi
activate "$ref"
link_unit
restart_live_service
if [[ "$mode" == upgrade ]]; then
  echo UPGRADED
else
  rm -f "$restart_required"
  echo INSTALLED
fi
