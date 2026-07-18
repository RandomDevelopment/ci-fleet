#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fake_bin=$tmp/bin
mkdir -p "$fake_bin"

cat >"$fake_bin/docker" <<'EOF'
#!/usr/bin/env bash
set -u
state=${FAKE_DOCKER_STATE:?}
case "${1:-}" in
  info) exit 0 ;;
  inspect)
    [[ -f "$state" ]] || exit 1
    if [[ "$*" == *'.State.Status'* ]]; then printf 'running\n'; else printf 'running\n'; fi
    ;;
  ps) exit 0 ;;
  volume|network)
    [[ "${2:-}" == ls ]] && exit 0
    [[ "${2:-}" == inspect ]] && exit 1
    exit 0
    ;;
  compose)
    if [[ "${2:-}" == version ]]; then exit 0; fi
    command=
    for argument in "$@"; do
      case "$argument" in config|build|up|stop|pause|unpause|down|logs|rm) command=$argument ;; esac
    done
    case "$command" in
      up)
        if [[ -n "${FAKE_FAIL_UP_ONCE:-}" && -f "$FAKE_FAIL_UP_ONCE" ]]; then
          rm -f "$FAKE_FAIL_UP_ONCE"
          exit 42
        fi
        : >"$state"
        ;;
      stop|down|rm) rm -f "$state" ;;
      config|build|pause|unpause|logs) ;;
      *) exit 1 ;;
    esac
    ;;
  *) exit 1 ;;
esac
EOF

cat >"$fake_bin/systemctl" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod 700 "$fake_bin/docker" "$fake_bin/systemctl"

export PATH="$fake_bin:$PATH"
export FAKE_DOCKER_STATE=$tmp/docker-controller-running
export CI_FLEET_TESTING=1
export CI_FLEET_DOCKER_GID_OVERRIDE=998
export CI_FLEET_STARTUP_WAIT_SECONDS=0
export CI_FLEET_DRAIN_TIMEOUT_SECONDS=2

fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
expect_success() {
  local output
  output=$("$@" 2>&1) || fail "expected success: $*; output=$output"
  printf '%s\n' "$output"
}
expect_failure() {
  local expected=$1 output
  shift
  if output=$("$@" 2>&1); then fail "expected failure: $*"; fi
  grep -Fq -- "$expected" <<<"$output" || fail "missing failure [$expected]: $output"
}

engine_ref=$(git -C "$repo_root" rev-parse 'HEAD^{commit}')
config_repo=$tmp/config-repo
git init -q "$config_repo"
git -C "$config_repo" config user.name fixture
git -C "$config_repo" config user.email fixture@example.invalid

write_config() {
  local state=$1 maximum=$2 budget=$3
  python3 - "$repo_root/templates/config-repository/fleet.json" "$config_repo/fleet.json" "$engine_ref" "$state" "$maximum" "$budget" <<'PY'
import json
import sys
source, target, engine_ref, state, maximum, budget = sys.argv[1:]
value = json.load(open(source, encoding="utf-8"))
controller = value["controllers"]["example-ci-01"]
controller["engine_ref"] = engine_ref
controller["state"] = state
controller["max_runners"] = int(maximum)
value["runner_pools"]["trusted-ci"]["capacity_budget"] = int(budget)
with open(target, "w", encoding="utf-8") as handle:
    json.dump(value, handle, indent=2)
    handle.write("\n")
PY
  git -C "$config_repo" add fleet.json
  git -C "$config_repo" commit -q -m "fixture $state $maximum"
  git -C "$config_repo" rev-parse HEAD
}

root=$tmp/host
export CI_FLEET_ROOT_PREFIX=$root
export CI_FLEET_DOCKER_ROOT=$root/var/lib/docker
mkdir -p "$root/etc/ci-fleet/secrets" "$CI_FLEET_DOCKER_ROOT"
pem=$root/etc/ci-fleet/secrets/github-app.pem
printf 'fixture only\n' >"$pem"
chmod 600 "$pem"
host_config=$root/etc/ci-fleet/host.env
printf '%s\n' \
  'CI_FLEET_GITHUB_APP_CLIENT_ID=Iv1.EXAMPLE' \
  'CI_FLEET_GITHUB_APP_INSTALLATION_ID=123456' \
  "CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE=$pem" \
  'CI_FLEET_RUNNER_TTL=6h' >"$host_config"
chmod 600 "$host_config"

ref_one=$(write_config active 1 1)
installer=$repo_root/scripts/install-worker-controller.sh
base_args=(--config-repo "$config_repo" --controller example-ci-01)

first=$(expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one")
grep -Fq 'CONVERGED mode=install' <<<"$first" || fail 'fresh install did not converge'
[[ -L "$root/opt/ci-fleet/current" && -f "$root/var/lib/ci-fleet/install-state.json" ]] || fail 'fresh install state is incomplete'
[[ -f "$FAKE_DOCKER_STATE" ]] || fail 'active controller was not started'

second=$(expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one")
grep -Fq 'NO_CHANGE' <<<"$second" || fail 'idempotent rerun changed the host'
expect_success "$installer" --check "${base_args[@]}" --ref "$ref_one" >/dev/null

printf '\n' >>"$root/etc/ci-fleet/ci-fleet.env"
expect_failure 'DRIFT rendered_environment' "$installer" --check "${base_args[@]}" --ref "$ref_one"
expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one" >/dev/null

prior_manager=$root/opt/ci-fleet/manager/releases/prior-manager
cp -a "$(readlink -f "$root/opt/ci-fleet/manager/current")" "$prior_manager"
ln -sfn "$prior_manager" "$root/opt/ci-fleet/manager/current"

ref_two=$(write_config active 2 2)
export FAKE_FAIL_UP_ONCE=$tmp/fail-up-once
: >"$FAKE_FAIL_UP_ONCE"
expect_failure 'ROLLBACK_RESTORED' "$installer" --upgrade "${base_args[@]}" --ref "$ref_two"
unset FAKE_FAIL_UP_ONCE
grep -Fq 'CI_FLEET_MAX_RUNNERS=1' "$root/etc/ci-fleet/ci-fleet.env" || fail 'failed activation did not restore capacity one'
[[ $(readlink -f "$root/opt/ci-fleet/manager/current") == "$prior_manager" ]] || fail 'failed activation did not restore the prior manager release'
[[ -f "$FAKE_DOCKER_STATE" ]] || fail 'failed activation did not restore the prior controller runtime'
expect_success "$installer" --upgrade "${base_args[@]}" --ref "$ref_two" >/dev/null
grep -Fq 'CI_FLEET_MAX_RUNNERS=2' "$root/etc/ci-fleet/ci-fleet.env" || fail 'upgrade did not apply capacity two'

expect_success "$installer" --rollback >/dev/null
grep -Fq 'CI_FLEET_MAX_RUNNERS=1' "$root/etc/ci-fleet/ci-fleet.env" || fail 'rollback did not restore capacity one'

ref_three=$(write_config drained 2 2)
expect_success "$installer" --upgrade "${base_args[@]}" --ref "$ref_three" >/dev/null
grep -Fq 'CI_FLEET_CONTROLLER_STATE=drained' "$root/etc/ci-fleet/ci-fleet.env" || fail 'drained state was not rendered'
grep -Fq 'CI_FLEET_MAX_RUNNERS=0' "$root/etc/ci-fleet/ci-fleet.env" || fail 'drained controller retained effective capacity'
[[ ! -f "$FAKE_DOCKER_STATE" ]] || fail 'drained controller remained running'

expect_success "$installer" --uninstall >/dev/null
[[ ! -e "$root/opt/ci-fleet/current" && ! -e "$root/var/lib/ci-fleet/install-state.json" ]] || fail 'uninstall left active installation state'
[[ -f "$host_config" && -f "$pem" ]] || fail 'uninstall removed preserved host credentials'

adopt_root=$tmp/adopt-host
export CI_FLEET_ROOT_PREFIX=$adopt_root
export FAKE_DOCKER_STATE=$tmp/adopt-controller-running
mkdir -p "$adopt_root/etc/ci-fleet/secrets" "$adopt_root/opt/ci-fleet/deploy"
adopt_pem=$adopt_root/etc/ci-fleet/secrets/github-app.pem
printf 'fixture only\n' >"$adopt_pem"
chmod 600 "$adopt_pem"
cp "$repo_root/deploy/compose.yaml" "$adopt_root/opt/ci-fleet/deploy/compose.yaml"
printf '%s\n' \
  'CI_FLEET_GITHUB_APP_CLIENT_ID=Iv1.EXAMPLE' \
  'CI_FLEET_GITHUB_APP_INSTALLATION_ID=123456' \
  "CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE=$adopt_pem" \
  'CI_FLEET_RUNNER_TTL=6h' >"$adopt_root/etc/ci-fleet/ci-fleet.env"
chmod 600 "$adopt_root/etc/ci-fleet/ci-fleet.env"
: >"$FAKE_DOCKER_STATE"

adopt=$(expect_success "$installer" --adopt "${base_args[@]}" --ref "$ref_one")
grep -Fq 'CONVERGED mode=adopt' <<<"$adopt" || fail 'adoption did not converge'
[[ -f "$adopt_root/etc/ci-fleet/host.env" ]] || fail 'adoption did not separate host-local values'

printf 'INSTALLER_TESTS_OK\n'
