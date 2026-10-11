#!/usr/bin/env bash
# Trusted host primitives for the Docker network-policy transaction.
set -Eeuo pipefail
set +x
export PYTHONDONTWRITEBYTECODE=1

die() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }

[[ $# -ge 1 ]] || die 'a policy primitive action is required'
action=$1
shift

expected_owner=0
[[ ${CI_FLEET_TESTING:-0} != 1 ]] || expected_owner=$(id -u)
[[ ${EUID:-$(id -u)} -eq 0 || ${CI_FLEET_TESTING:-0} == 1 ]] || die 'policy primitives require root'
[[ ${CI_FLEET_INSTALLER_LOCK_FD:-} == 9 ]] || die 'policy primitives require inherited installer lock fd 9'
installer=${CI_FLEET_POLICY_INSTALLER:-}
[[ "$installer" == /* && -f "$installer" && ! -L "$installer" && -x "$installer" && $(stat -c %u "$installer") == "$expected_owner" ]] || die 'policy installer is not trusted'

invoke_installer() {
  CI_FLEET_INSTALLER_LOCK_FD=9 "$installer" --policy-action "$1"
}

case "$action" in
  drain|rollback-drain)
    [[ $# -eq 0 ]] || die "invalid $action arguments"
    invoke_installer "$action"
    ;;
  restore)
    [[ $# -eq 2 ]] || die 'invalid restore arguments'
    [[ $1 == --env && $2 == /* && -f $2 && ! -L $2 ]] || die 'invalid restore arguments'
    invoke_installer restore
    ;;
  resume|health)
    [[ $# -eq 2 ]] || die "invalid $action environment"
    [[ $1 == --env && $2 == /* && -f $2 && ! -L $2 ]] || die "invalid $action environment"
    if [[ "$action" == health && ! -s $2 && ! -f ${CI_FLEET_ROOT_PREFIX:-}/etc/ci-fleet/ci-fleet.env ]]; then
      exit 0
    fi
    CI_FLEET_POLICY_ENV=$2 invoke_installer "$action"
    ;;
  restart)
    [[ $# -eq 1 ]] || die 'invalid Docker restart target'
    [[ $1 == "$(dirname "${CI_FLEET_DOCKER_DAEMON_CONFIG:-/invalid}")" ]] || die 'invalid Docker restart target'
    systemctl restart docker.service 9>&- 7>&-
    ;;
  probe)
    [[ $# -eq 0 || ( $# -eq 2 && $1 == --daemon-config && $2 == /* && -f $2 && ! -L $2 ) ]] || die 'invalid network probe arguments'
    expected_bip=
    if [[ $# -eq 2 ]]; then
      expected_bip=$(python3 - "$2" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
if not isinstance(value, dict):
    raise SystemExit(1)
print(value.get("bip", ""))
PY
      ) || die 'invalid expected daemon config'
    fi
    if [[ -n "$expected_bip" ]]; then
      effective_bridge=$(docker network inspect bridge --format '{{json .IPAM.Config}}' 9>&- 7>&-) || die 'failed to inspect default bridge network'
      python3 - "$expected_bip" "$effective_bridge" <<'PY' || die 'effective default bridge does not match managed bip'
import ipaddress, json, sys
expected = ipaddress.ip_interface(sys.argv[1])
configs = json.loads(sys.argv[2])
if not isinstance(configs, list) or not any(
    isinstance(item, dict)
    and item.get("Subnet") == str(expected.network)
    and item.get("Gateway") == str(expected.ip)
    for item in configs
):
    raise SystemExit(1)
PY
    fi
    probe=ci-fleet-network-policy-probe-$$
    trap 'docker network rm "$probe" 9>&- 7>&- >/dev/null 2>&1 || true' EXIT
    docker network create \
      --label io.randomdevelopment.ci-fleet.managed=true \
      --label io.randomdevelopment.ci-fleet.kind=network-policy-probe \
      "$probe" 9>&- 7>&- >/dev/null
    docker network rm "$probe" 9>&- 7>&- >/dev/null
    trap - EXIT
    ;;
  *) die "unknown policy primitive action: $action" ;;
esac
