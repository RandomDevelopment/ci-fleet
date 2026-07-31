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
if [[ -z "$root" && $EUID -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
install_root="$root/opt/ci-fleet-status"
state_root="$root/var/lib/ci-fleet-status"
config_root="$root/etc/ci-fleet-status"
unit_path="$root/etc/systemd/system/ci-fleet-status-receiver.service"
current="$install_root/current"
previous="$state_root/previous-ref"

current_ref() {
  [[ -L "$current" ]] || return 1
  basename "$(readlink "$current")"
}

activate() {
  local target=$1 old=
  [[ -d "$install_root/releases/$target" ]] || { echo "release not installed: $target" >&2; exit 1; }
  old=$(current_ref || true)
  ln -sfn "releases/$target" "$current.new"
  mv -Tf "$current.new" "$current"
  if [[ -n "$old" && "$old" != "$target" ]]; then
    printf '%s\n' "$old" >"$previous.tmp"
    chmod 0600 "$previous.tmp"
    mv -Tf "$previous.tmp" "$previous"
  fi
}

restart_live_service() {
  [[ -n "$root" ]] && return
  systemctl daemon-reload
  if systemctl is-active --quiet ci-fleet-status-receiver.service; then
    systemctl restart ci-fleet-status-receiver.service
  fi
}

if [[ "$mode" == check ]]; then
  installed=$(current_ref) || { echo "receiver is not installed" >&2; exit 1; }
  [[ -x "$current/status_receiver.py" && -r "$current/status_auth.py" ]]
  [[ -f "$unit_path" ]]
  grep -F -- '--bind 127.0.0.1' "$unit_path" >/dev/null
  echo "CHECK_OK $installed"
  exit
fi

if [[ "$mode" == rollback ]]; then
  [[ -s "$previous" ]] || { echo "no rollback release recorded" >&2; exit 1; }
  target=$(<"$previous")
  [[ "$target" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid rollback release" >&2; exit 1; }
  activate "$target"
  restart_live_service
  echo "ROLLED_BACK $target"
  exit
fi

[[ "$ref" =~ ^[0-9a-f]{40}$ ]] || usage
head=$(git -C "$repo_root" rev-parse HEAD)
[[ "$head" == "$ref" ]] || { echo "--ref must equal the reviewed checkout HEAD" >&2; exit 1; }
existing=$(current_ref || true)
if [[ "$existing" == "$ref" ]]; then
  echo NO_CHANGE
  exit
fi

if [[ -z "$root" ]]; then
  id ci-fleet-status >/dev/null 2>&1 || useradd --system --home /nonexistent --shell /usr/sbin/nologin ci-fleet-status
  install -d -o ci-fleet-status -g ci-fleet-status -m 0700 "$state_root" "$config_root"
else
  install -d -m 0700 "$state_root" "$config_root"
fi
install -d -m 0755 "$install_root/releases" "$(dirname "$unit_path")"
staging="$install_root/releases/.staging.$$"
trap 'rm -rf "$staging"' EXIT
install -d -m 0755 "$staging"
install -m 0755 "$repo_root/scripts/status_receiver.py" "$staging/status_receiver.py"
install -m 0644 "$repo_root/scripts/status_auth.py" "$staging/status_auth.py"
mv -T "$staging" "$install_root/releases/$ref"
trap - EXIT
install -m 0644 "$repo_root/deploy/status-receiver/ci-fleet-status-receiver.service" "$unit_path"
activate "$ref"
restart_live_service
if [[ "$mode" == upgrade ]]; then
  echo UPGRADED
else
  echo INSTALLED
fi
