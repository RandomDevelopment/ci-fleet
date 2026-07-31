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
  [[ -L "$current" ]] || return 1
  basename "$(readlink "$current")"
}

write_metadata() {
  local destination=$1 value=$2 temporary
  temporary=$(mktemp "$metadata_root/.metadata.XXXXXX")
  printf '%s\n' "$value" >"$temporary"
  chmod 0600 "$temporary"
  mv -Tf "$temporary" "$destination"
}

link_unit() {
  ln -sfn "$unit_target" "$unit_path.new"
  mv -Tf "$unit_path.new" "$unit_path"
}

activate() {
  local target=$1 record_previous=${2:-1} old=
  [[ -d "$install_root/releases/$target" ]] || { echo "release not installed: $target" >&2; exit 1; }
  old=$(current_ref || true)
  if [[ "$record_previous" == 1 && -n "$old" && "$old" != "$target" ]]; then
    write_metadata "$previous" "$old"
  fi
  ln -sfn "releases/$target" "$current.new"
  mv -Tf "$current.new" "$current"
}

restart_live_service() {
  local force=${1:-0}
  [[ -n "$root" ]] && return
  systemctl daemon-reload
  if [[ "$force" == 1 ]] || systemctl is-active --quiet ci-fleet-status-receiver.service; then
    systemctl restart ci-fleet-status-receiver.service
  fi
}

if [[ "$mode" == check ]]; then
  installed=$(current_ref) || { echo "receiver is not installed" >&2; exit 1; }
  [[ -x "$current/status_receiver.py" && -r "$current/status_auth.py" ]]
  [[ -L "$unit_path" && $(readlink "$unit_path") == "$unit_target" ]]
  echo "CHECK_OK $installed"
  exit
fi

if [[ "$mode" == rollback ]]; then
  [[ -s "$previous" ]] || { echo "no rollback release recorded" >&2; exit 1; }
  target=$(<"$previous")
  [[ "$target" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid rollback release" >&2; exit 1; }
  force=0
  [[ -f "$restart_required" ]] && force=1
  activate "$target" 0
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
/usr/bin/python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 9))' || {
  echo "Python 3.9 or newer is required" >&2
  exit 1
}

if [[ -z "$root" ]]; then
  getent passwd ci-fleet-status >/dev/null || \
    useradd --system --user-group --home /nonexistent --shell /usr/sbin/nologin ci-fleet-status
  IFS=: read -r account _ uid gid _ home shell < <(getent passwd ci-fleet-status)
  IFS=: read -r group _ group_gid _ < <(getent group ci-fleet-status)
  [[ "$account" == ci-fleet-status && "$group" == ci-fleet-status ]]
  [[ "$uid" != 0 && "$gid" == "$group_gid" ]]
  [[ "$home" == /nonexistent && "$shell" == /usr/sbin/nologin ]]
  install -d -o ci-fleet-status -g ci-fleet-status -m 0700 "$state_root" "$config_root"
  install -d -o root -g root -m 0700 "$metadata_root"
else
  install -d -m 0700 "$state_root" "$config_root" "$metadata_root"
fi
install -d -m 0755 "$install_root/releases" "$(dirname "$unit_path")"
existing=$(current_ref || true)
if [[ "$mode" == install && -n "$existing" && "$existing" != "$ref" ]]; then
  echo "use --upgrade to change an active release" >&2
  exit 1
fi
release="$install_root/releases/$ref"
if [[ -d "$release" ]]; then
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
  mv -T "$staging" "$release"
  trap - EXIT
fi

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
