#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fake_bin=$tmp/bin
mkdir -p "$fake_bin"
export REAL_STAT
REAL_STAT=$(command -v stat)
export REAL_TAR
REAL_TAR=$(command -v tar)

cat >"$fake_bin/docker" <<'EOF'
#!/usr/bin/env bash
set -u
state=${FAKE_DOCKER_STATE:?}
status_file=${FAKE_CONTROLLER_STATUS_FILE:-}
paused_state=${FAKE_PAUSED_STATE:-}
case "${1:-}" in
  info) exit 0 ;;
  inspect)
    [[ -f "$state" ]] || exit 1
    if [[ "$*" == *'.Config.Env'* ]]; then
      [[ -n "${FAKE_CONTROLLER_ENV_FILE:-}" && -f "$FAKE_CONTROLLER_ENV_FILE" ]] || exit 1
      cat "$FAKE_CONTROLLER_ENV_FILE"
    elif [[ "$*" == *'org.opencontainers.image.revision'* ]]; then
      if [[ -n "${FAKE_CONTROLLER_PROVENANCE_FILE:-}" && -f "$FAKE_CONTROLLER_PROVENANCE_FILE" ]]; then cat "$FAKE_CONTROLLER_PROVENANCE_FILE"; else printf '%s\n' "${FAKE_ENGINE_REF:?}"; fi
    elif [[ "$*" == *'{{.Image}}'* ]]; then
      [[ -n "${FAKE_CONTROLLER_IMAGE_ID_FILE:-}" && -f "$FAKE_CONTROLLER_IMAGE_ID_FILE" ]] || exit 1
      cat "$FAKE_CONTROLLER_IMAGE_ID_FILE"
    elif [[ "$*" == *'.State.Status'* ]]; then
      if [[ -n "$status_file" && -f "$status_file" ]]; then cat "$status_file"; else printf '%s\n' "${FAKE_CONTROLLER_STATUS:-running}"; fi
    elif [[ "$*" == *'.State.Paused'* ]]; then
      if [[ -n "$paused_state" && -f "$paused_state" ]]; then printf 'true\n'; else printf 'false\n'; fi
    else
      printf 'running\n'
    fi
    ;;
  ps)
    [[ -z "${FAKE_DOCKER_PS_LOG:-}" ]] || printf '%s\n' "$*" >>"$FAKE_DOCKER_PS_LOG"
    if [[ "$*" == *'--all'* && -n "${FAKE_ALL_RUNNER_STATE:-}" && -f "$FAKE_ALL_RUNNER_STATE" ]]; then
      printf 'managed-runner-all-state\n'
    elif [[ -n "${FAKE_RUNNER_STATE_ONCE:-}" && -f "$FAKE_RUNNER_STATE_ONCE" ]]; then
      rm -f "$FAKE_RUNNER_STATE_ONCE"
      printf 'managed-runner\n'
    elif [[ -n "${FAKE_RUNNER_STATE:-}" && -f "$FAKE_RUNNER_STATE" ]]; then
      printf 'managed-runner\n'
    fi
    exit 0
    ;;
  image)
    [[ "${2:-}" == inspect ]] || exit 1
    image=${!#}
    [[ -z "${FAKE_IMAGE_INSPECT_LOG:-}" ]] || printf '%s\n' "$image" >>"$FAKE_IMAGE_INSPECT_LOG"
    if [[ "$image" == "${FAKE_RUNNER_IMAGE:-}" ]]; then
      image_state=${FAKE_RUNNER_IMAGE_STATE:-}
    elif [[ "$image" == "${FAKE_CONTROLLER_IMAGE:-}" ]]; then
      image_state=${FAKE_CONTROLLER_IMAGE_STATE:-}
    else
      exit 1
    fi
    [[ -f "$image_state" ]] || exit 1
    if [[ "$*" == *'{{.Id}}'* ]]; then printf 'sha256:%s\n' "$(<"$image_state")"; else cat "$image_state"; fi
    ;;
  rm)
    (($# >= 2)) || exit 1
    [[ -z "${FAKE_ALL_RUNNER_STATE:-}" ]] || rm -f "$FAKE_ALL_RUNNER_STATE"
    ;;
  volume|network)
    [[ "${2:-}" == ls ]] && exit 0
    [[ "${2:-}" == inspect ]] && exit 1
    exit 0
    ;;
  compose)
    if [[ "${2:-}" == version ]]; then exit 0; fi
    [[ -z "${COMPOSE_PROJECT_NAME:-}" && -z "${CI_FLEET_MAX_RUNNERS:-}" ]] || exit 44
    command= env_file= previous=
    for argument in "$@"; do
      [[ "$previous" != --env-file ]] || env_file=$argument
      case "$argument" in config|build|up|stop|pause|unpause|kill|down|logs|rm) command=$argument ;; esac
      previous=$argument
    done
    if [[ -n "${FAKE_COMPOSE_LOG:-}" ]]; then
      instance=
      [[ ! -f "$env_file" ]] || instance=$(awk -F= '$1 == "CI_FLEET_INSTANCE" {print $2}' "$env_file")
      printf '%s|%s|%s|%s\n' "$command" "$env_file" "$instance" "$*" >>"$FAKE_COMPOSE_LOG"
    fi
    case "$command" in
      up)
        if [[ -n "${FAKE_FAIL_UP_ONCE:-}" && -f "$FAKE_FAIL_UP_ONCE" ]]; then
          rm -f "$FAKE_FAIL_UP_ONCE"
          exit 42
        fi
        : >"$state"
        [[ -z "${FAKE_CONTROLLER_PROVENANCE_FILE:-}" ]] || printf '%s\n' "${FAKE_ENGINE_REF:?}" >"$FAKE_CONTROLLER_PROVENANCE_FILE"
        [[ -z "${FAKE_CONTROLLER_IMAGE_ID_FILE:-}" ]] || printf 'sha256:%s\n' "${FAKE_ENGINE_REF:?}" >"$FAKE_CONTROLLER_IMAGE_ID_FILE"
        [[ -z "${FAKE_CONTROLLER_ENV_FILE:-}" ]] || cp "$env_file" "$FAKE_CONTROLLER_ENV_FILE"
        if [[ -n "${FAKE_RESTART_AFTER_UP:-}" && -f "$FAKE_RESTART_AFTER_UP" ]]; then
          rm -f "$FAKE_RESTART_AFTER_UP"
          printf 'restarting\n' >"$status_file"
        elif [[ -n "$status_file" ]]; then
          rm -f "$status_file"
        fi
        ;;
      stop)
        if [[ -n "${FAKE_STOP_FAIL:-}" && -f "$FAKE_STOP_FAIL" ]]; then exit 42; fi
        rm -f "$state"; [[ -z "$status_file" ]] || rm -f "$status_file"; [[ -z "$paused_state" ]] || rm -f "$paused_state"; [[ -z "${FAKE_CONTROLLER_PROVENANCE_FILE:-}" ]] || rm -f "$FAKE_CONTROLLER_PROVENANCE_FILE"; [[ -z "${FAKE_CONTROLLER_IMAGE_ID_FILE:-}" ]] || rm -f "$FAKE_CONTROLLER_IMAGE_ID_FILE"; [[ -z "${FAKE_CONTROLLER_ENV_FILE:-}" ]] || rm -f "$FAKE_CONTROLLER_ENV_FILE"
        ;;
      down|rm) rm -f "$state"; [[ -z "$status_file" ]] || rm -f "$status_file"; [[ -z "$paused_state" ]] || rm -f "$paused_state"; [[ -z "${FAKE_CONTROLLER_PROVENANCE_FILE:-}" ]] || rm -f "$FAKE_CONTROLLER_PROVENANCE_FILE"; [[ -z "${FAKE_CONTROLLER_IMAGE_ID_FILE:-}" ]] || rm -f "$FAKE_CONTROLLER_IMAGE_ID_FILE"; [[ -z "${FAKE_CONTROLLER_ENV_FILE:-}" ]] || rm -f "$FAKE_CONTROLLER_ENV_FILE" ;;
      pause)
        [[ -z "$paused_state" ]] || : >"$paused_state"
        [[ -z "${FAKE_RUNNER_STATE:-}" ]] || rm -f "$FAKE_RUNNER_STATE"
        ;;
      unpause) [[ -z "$paused_state" ]] || rm -f "$paused_state" ;;
      kill)
        if [[ -n "${FAKE_FAIL_KILL_ONCE:-}" && -f "$FAKE_FAIL_KILL_ONCE" ]]; then
          rm -f "$FAKE_FAIL_KILL_ONCE"
          exit 43
        fi
        [[ -z "$paused_state" ]] || rm -f "$paused_state"
        ;;
      build)
        [[ -z "${FAKE_RUNNER_IMAGE_STATE:-}" ]] || printf '%s\n' "${FAKE_ENGINE_REF:?}" >"$FAKE_RUNNER_IMAGE_STATE"
        [[ -z "${FAKE_CONTROLLER_IMAGE_STATE:-}" ]] || printf '%s\n' "${FAKE_ENGINE_REF:?}" >"$FAKE_CONTROLLER_IMAGE_STATE"
        ;;
      config|logs) ;;
      *) exit 1 ;;
    esac
    ;;
  *) exit 1 ;;
esac
EOF

cat >"$fake_bin/systemctl" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == enable && "${2:-}" == --now && ! -f "${CI_FLEET_ROOT_PREFIX:-}/var/lib/ci-fleet/install-state.json" ]]; then
  exit 98
fi
if [[ -n "${FAKE_DISABLED_TIMER:-}" && ( "${1:-}" == is-enabled || "${1:-}" == is-active ) && $# == 3 && "${3:-}" == "$FAKE_DISABLED_TIMER" ]]; then
  exit 1
fi
exit 0
EOF

cat >"$fake_bin/stat" <<'EOF'
#!/usr/bin/env bash
if [[ -n "${FAKE_WRONG_HOST_CONFIG_OWNER:-}" && "${1:-}" == -c && "${2:-}" == %u && "${3:-}" == "$FAKE_WRONG_HOST_CONFIG_OWNER" ]]; then
  printf '99999\n'
  exit 0
fi
if [[ -n "${FAKE_WRONG_INSTALL_STATE_OWNER:-}" && "${1:-}" == -c && "${2:-}" == %u && "${3:-}" == "$FAKE_WRONG_INSTALL_STATE_OWNER" ]]; then
  printf '99999\n'
  exit 0
fi
exec "$REAL_STAT" "$@"
EOF
chmod 700 "$fake_bin/docker" "$fake_bin/systemctl" "$fake_bin/stat"

cat >"$fake_bin/tar" <<'EOF'
#!/usr/bin/env bash
if [[ -n "${FAKE_FAIL_TAR_ONCE:-}" && -f "$FAKE_FAIL_TAR_ONCE" ]]; then
  rm -f "$FAKE_FAIL_TAR_ONCE"
  exit 45
fi
exec "$REAL_TAR" "$@"
EOF
chmod 700 "$fake_bin/tar"

export PATH="$fake_bin:$PATH"
export FAKE_DOCKER_STATE=$tmp/docker-controller-running
export FAKE_CONTROLLER_STATUS_FILE=$tmp/docker-controller-status
export FAKE_PAUSED_STATE=$tmp/docker-controller-paused
export FAKE_CONTROLLER_PROVENANCE_FILE=$tmp/docker-controller-provenance
export FAKE_CONTROLLER_IMAGE_ID_FILE=$tmp/docker-controller-image-id
export FAKE_CONTROLLER_ENV_FILE=$tmp/docker-controller-env
export FAKE_DOCKER_PS_LOG=$tmp/docker-ps.log
export CI_FLEET_TESTING=1
export CI_FLEET_DOCKER_GID_OVERRIDE=998
export CI_FLEET_STARTUP_WAIT_SECONDS=0
export CI_FLEET_DRAIN_TIMEOUT_SECONDS=2
export COMPOSE_PROJECT_NAME=caller-controlled-project
export CI_FLEET_MAX_RUNNERS=999

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
expect_command_failure() {
  local output
  if output=$("$@" 2>&1); then fail "expected failure: $*"; fi
}

engine_ref=$(git -C "$repo_root" rev-parse 'HEAD^{commit}')
export FAKE_ENGINE_REF=$engine_ref
runner_image="ci-fleet-runner:${engine_ref:0:12}"
export FAKE_RUNNER_IMAGE=$runner_image
export FAKE_CONTROLLER_IMAGE=ci-fleet-controller:${engine_ref:0:12}
export FAKE_RUNNER_IMAGE_STATE=$tmp/runner-image-present
export FAKE_CONTROLLER_IMAGE_STATE=$tmp/controller-image-present
export FAKE_IMAGE_INSPECT_LOG=$tmp/image-inspects
for dockerfile in "$repo_root/controller/Dockerfile" "$repo_root/runner/Dockerfile"; do
  grep -Fq "LABEL org.opencontainers.image.revision=\"\${CI_FLEET_COMMIT}\"" "$dockerfile" || fail "managed image lacks engine provenance label: $dockerfile"
  grep -Fq 'io.randomdevelopment.ci-fleet.managed="true"' "$dockerfile" || fail "managed image lacks fleet ownership label: $dockerfile"
done
grep -Fq '    user: "0:0"' "$repo_root/deploy/compose.yaml" || fail 'controller cannot read the required root-owned mode-0600 GitHub App PEM'
grep -Fq 'export PYTHONDONTWRITEBYTECODE=1' "$repo_root/scripts/install-worker-controller.sh" || fail 'managed validation may write Python bytecode into the immutable manager release'
grep -Fq "CI_FLEET_COMMIT: \${CI_FLEET_COMMIT:-unknown}" "$repo_root/deploy/compose.yaml" || fail 'runner build lacks engine provenance argument'
config_repo=$tmp/config-repo
git init -q "$config_repo"
git -C "$config_repo" config user.name fixture
git -C "$config_repo" config user.email fixture@example.invalid

write_config() {
  local state=$1 maximum=$2 budget=$3
  local desired_engine=${4:-$engine_ref}
  python3 - "$repo_root/templates/config-repository/fleet.json" "$config_repo/fleet.json" "$desired_engine" "$state" "$maximum" "$budget" <<'PY'
import json
import sys
source, target, engine_ref, state, maximum, budget = sys.argv[1:]
value = json.load(open(source, encoding="utf-8"))
value["organization"]["slug"] = "fixture-org"
value["runner_pools"]["trusted-ci"]["allowed_repositories"] = ["fixture-org/example-app"]
value["projects"]["example-app"]["repository"] = "fixture-org/example-app"
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
printf 'fixture only\n' >"$config_repo/.env"
git -C "$config_repo" add -f .env
git -C "$config_repo" commit -q -m 'forbidden config path fixture'
forbidden_ref=$(git -C "$config_repo" rev-parse HEAD)
git -C "$config_repo" reset -q --hard "$ref_one"
printf 'ghp_%020d\n' 0 >"$config_repo/README.md"
git -C "$config_repo" add README.md
git -C "$config_repo" commit -q -m 'forbidden config content fixture'
secret_ref=$(git -C "$config_repo" rev-parse HEAD)
git -C "$config_repo" reset -q --hard "$ref_one"
installer=$repo_root/scripts/install-worker-controller.sh
base_args=(--config-repo "$config_repo" --controller example-ci-01)

staged_checkpoint="$root/var/lib/ci-fleet/checkpoints/.checkpoint.staging.interrupted"
mkdir -p "$staged_checkpoint"
: >"$staged_checkpoint/.complete"
expect_failure 'no controller checkpoint is available' "$installer" --rollback
rm -rf "$staged_checkpoint"
expect_failure 'secret-bearing files are forbidden' "$installer" --check "${base_args[@]}" --ref "$forbidden_ref"
expect_failure 'possible committed secret detected' "$installer" --check "${base_args[@]}" --ref "$secret_ref"
export FAKE_WRONG_HOST_CONFIG_OWNER=$host_config
expect_failure 'host configuration must be owned by root' "$installer" --install "${base_args[@]}" --ref "$ref_one"
unset FAKE_WRONG_HOST_CONFIG_OWNER
expect_failure 'managed installs require the default' "$installer" --check "${base_args[@]}" --ref "$ref_one" --host-config "$tmp/custom-host.env"

first=$(expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one")
grep -Fq 'CONVERGED mode=install' <<<"$first" || fail 'fresh install did not converge'
[[ -L "$root/opt/ci-fleet/current" && -f "$root/var/lib/ci-fleet/install-state.json" ]] || fail 'fresh install state is incomplete'
[[ $(readlink -f "$root/opt/ci-fleet/manager/current") == "$root/opt/ci-fleet/manager/releases/$engine_ref" ]] || fail 'installer manager did not activate the desired engine release'
[[ -f "$FAKE_DOCKER_STATE" ]] || fail 'active controller was not started'
install_state=$root/var/lib/ci-fleet/install-state.json
chmod 644 "$install_state"
expect_failure 'install state must be owned by root with mode 0600' env CI_FLEET_INSTALL_STATE_FILE="$install_state" CI_FLEET_INSTALLER="$installer" "$repo_root/scripts/check-installed-state.sh"
expect_failure 'DRIFT install_state' "$installer" --check "${base_args[@]}" --ref "$ref_one"
expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one" >/dev/null
[[ $(stat -c %a "$install_state") == 600 ]] || fail 'convergence did not repair install-state mode'
rendered_env=$root/etc/ci-fleet/ci-fleet.env
chmod 644 "$rendered_env"
expect_failure 'DRIFT rendered_environment' "$installer" --check "${base_args[@]}" --ref "$ref_one"
expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one" >/dev/null
[[ $(stat -c %a "$rendered_env") == 600 ]] || fail 'convergence did not repair rendered-environment mode'
manual_health_result=0
"$repo_root/scripts/healthcheck.sh" >/dev/null || manual_health_result=$?
((manual_health_result < 2)) || fail 'manual healthcheck did not source rendered capacity'
export FAKE_WRONG_INSTALL_STATE_OWNER=$install_state
expect_failure 'install state must be owned by root with mode 0600' env CI_FLEET_INSTALL_STATE_FILE="$install_state" CI_FLEET_INSTALLER="$installer" "$repo_root/scripts/check-installed-state.sh"
unset FAKE_WRONG_INSTALL_STATE_OWNER
expect_success env CI_FLEET_INSTALL_STATE_FILE="$install_state" CI_FLEET_INSTALLER="$installer" "$repo_root/scripts/check-installed-state.sh" >/dev/null

export FAKE_DISABLED_TIMER=ci-fleet-cleanup.timer
expect_failure 'DRIFT maintenance_timers' "$installer" --check "${base_args[@]}" --ref "$ref_one"
unset FAKE_DISABLED_TIMER

export FAKE_CONTROLLER_STATUS=restarting
expect_failure 'cannot safely drain controller in non-terminal state: restarting' "$installer" --uninstall
unset FAKE_CONTROLLER_STATUS

mv "$host_config" "$host_config.missing"
expect_failure 'host-local GitHub App configuration is missing' "$installer" --check "${base_args[@]}" --ref "$ref_one"
mv "$host_config.missing" "$host_config"

second=$(expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one")
grep -Fq 'NO_CHANGE' <<<"$second" || fail 'idempotent rerun changed the host'
check=$(expect_success "$installer" --check "${base_args[@]}" --ref "$ref_one")
grep -Fq 'HEALTH last=' <<<"$check" || fail 'check output omitted the last redacted health result'
[[ ! -d "$root/opt/ci-fleet/manager/releases/$engine_ref/templates/config-repository/scripts/__pycache__" ]] || fail 'manager validation wrote Python bytecode into the immutable release'
python3 - "$install_state" <<'PY'
import json
import sys
path = sys.argv[1]
value = json.load(open(path, encoding="utf-8"))
value["controller"] = "legacy-ci-01"
with open(path, "w", encoding="utf-8") as handle:
    json.dump(value, handle)
    handle.write("\n")
PY
python3 -c 'from pathlib import Path; import sys; path = Path(sys.argv[1]); path.write_text(path.read_text().replace("CI_FLEET_INSTANCE=example-ci-01", "CI_FLEET_INSTANCE=legacy-ci-01"))' "$rendered_env"
chmod 600 "$install_state" "$rendered_env"
[[ $(stat -c '%u:%a' "$install_state") == "$(id -u):600" ]] || fail 'installed-identity fixture metadata is invalid'
export FAKE_RUNNER_STATE_ONCE=$tmp/repeat-install-managed-runner
: >"$FAKE_RUNNER_STATE_ONCE"
: >"$FAKE_DOCKER_PS_LOG"
expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one" >/dev/null
grep -Fq 'label=io.randomdevelopment.ci-fleet.instance=legacy-ci-01' "$FAKE_DOCKER_PS_LOG" || fail 'repeat install did not drain the installed controller identity'
grep -Fq 'CI_FLEET_INSTANCE=example-ci-01' "$rendered_env" || fail 'repeat install did not restore the desired controller identity'
unset FAKE_RUNNER_STATE_ONCE
printf '%040d\n' 0 >"$FAKE_CONTROLLER_PROVENANCE_FILE"
expect_failure 'DRIFT controller_runtime' "$installer" --check "${base_args[@]}" --ref "$ref_one"
expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one" >/dev/null
[[ $(<"$FAKE_CONTROLLER_PROVENANCE_FILE") == "$engine_ref" ]] || fail 'controller convergence did not restore running image provenance'
printf 'sha256:%040d\n' 0 >"$FAKE_CONTROLLER_IMAGE_ID_FILE"
expect_failure 'DRIFT controller_runtime' "$installer" --check "${base_args[@]}" --ref "$ref_one"
expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one" >/dev/null
[[ $(<"$FAKE_CONTROLLER_IMAGE_ID_FILE") == "sha256:$engine_ref" ]] || fail 'controller convergence did not restore live image identity'
python3 -c 'from pathlib import Path; import sys; path = Path(sys.argv[1]); path.write_text(path.read_text().replace("CI_FLEET_MAX_RUNNERS=1", "CI_FLEET_MAX_RUNNERS=9"))' "$FAKE_CONTROLLER_ENV_FILE"
grep -Fxq 'CI_FLEET_MAX_RUNNERS=9' "$FAKE_CONTROLLER_ENV_FILE" || fail 'live-environment fixture did not mutate'
expect_failure 'DRIFT controller_runtime' "$installer" --check "${base_args[@]}" --ref "$ref_one"
expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one" >/dev/null
grep -Fxq 'CI_FLEET_MAX_RUNNERS=1' "$FAKE_CONTROLLER_ENV_FILE" || fail 'controller convergence did not restore live environment'

rm -f "$FAKE_RUNNER_IMAGE_STATE"
expect_failure 'DRIFT managed_images' "$installer" --check "${base_args[@]}" --ref "$ref_one"
image_repair=$(expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one")
grep -Fq 'CONVERGED mode=install' <<<"$image_repair" || fail 'missing runner image did not trigger convergence'
[[ -f "$FAKE_RUNNER_IMAGE_STATE" && -f "$FAKE_CONTROLLER_IMAGE_STATE" ]] || fail 'candidate build did not restore both managed images'
rm -f "$FAKE_CONTROLLER_IMAGE_STATE"
expect_failure 'DRIFT managed_images' "$installer" --check "${base_args[@]}" --ref "$ref_one"
expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one" >/dev/null
[[ -f "$FAKE_RUNNER_IMAGE_STATE" && -f "$FAKE_CONTROLLER_IMAGE_STATE" ]] || fail 'candidate build did not restore the controller image'
printf '%040d\n' 0 >"$FAKE_RUNNER_IMAGE_STATE"
expect_failure 'DRIFT managed_images' "$installer" --check "${base_args[@]}" --ref "$ref_one"
expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one" >/dev/null
[[ $(<"$FAKE_RUNNER_IMAGE_STATE") == "$engine_ref" && $(<"$FAKE_CONTROLLER_IMAGE_STATE") == "$engine_ref" ]] || fail 'candidate build did not restore managed image provenance'
if docker image inspect unrelated:image >/dev/null 2>&1; then fail 'unrelated image fixture unexpectedly exists'; fi
: >"$FAKE_IMAGE_INSPECT_LOG"
expect_success "$installer" --check "${base_args[@]}" --ref "$ref_one" >/dev/null
grep -Fxq "$FAKE_RUNNER_IMAGE" "$FAKE_IMAGE_INSPECT_LOG" || fail 'runner image was not inspected'
grep -Fxq "$FAKE_CONTROLLER_IMAGE" "$FAKE_IMAGE_INSPECT_LOG" || fail 'controller image was not inspected'
if grep -Fvx -e "$FAKE_RUNNER_IMAGE" -e "$FAKE_CONTROLLER_IMAGE" "$FAKE_IMAGE_INSPECT_LOG" >/dev/null; then fail 'an unrelated image was inspected'; fi

active_release=$(readlink -f "$root/opt/ci-fleet/current")
unrelated_release=$root/opt/ci-fleet/releases/unrelated-release
mkdir -p "$unrelated_release"
: >"$unrelated_release/preserve"
mv "$active_release/deploy/compose.yaml" "$active_release/deploy/compose.yaml.missing"
expect_failure 'DRIFT engine_release' "$installer" --check "${base_args[@]}" --ref "$ref_one"
expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one" >/dev/null
[[ -f "$active_release/deploy/compose.yaml" && -f "$unrelated_release/preserve" ]] || fail 'Compose repair removed unrelated release state'
rm -f "$active_release/scripts/preflight.sh"
expect_failure 'DRIFT engine_release' "$installer" --check "${base_args[@]}" --ref "$ref_one"
expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one" >/dev/null
[[ -x "$active_release/scripts/preflight.sh" ]] || fail 'required runtime script was not repaired'
rm -f "$active_release/controller/main.go" "$active_release/runner/Dockerfile"
expect_failure 'DRIFT engine_release' "$installer" --check "${base_args[@]}" --ref "$ref_one"
expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one" >/dev/null
[[ -f "$active_release/controller/main.go" && -f "$active_release/runner/Dockerfile" ]] || fail 'runtime build inputs were not repaired'
rm -f "$active_release/.ci-fleet-engine-ref"
expect_failure 'DRIFT engine_release' "$installer" --check "${base_args[@]}" --ref "$ref_one"
expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one" >/dev/null
[[ $(<"$active_release/.ci-fleet-engine-ref") == "$engine_ref" ]] || fail 'missing release marker was not repaired'
if [[ ${engine_ref: -1} == 0 ]]; then bad_marker="${engine_ref%?}1"; else bad_marker="${engine_ref%?}0"; fi
printf '%s\n' "$bad_marker" >"$active_release/.ci-fleet-engine-ref"
expect_failure 'DRIFT engine_release' "$installer" --check "${base_args[@]}" --ref "$ref_one"
expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one" >/dev/null
[[ $(<"$active_release/.ci-fleet-engine-ref") == "$engine_ref" ]] || fail 'bad release marker was not repaired'
printf '\n# tampered runtime fixture\n' >>"$active_release/scripts/preflight.sh"
expect_failure 'DRIFT engine_release' "$installer" --check "${base_args[@]}" --ref "$ref_one"
expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one" >/dev/null
if grep -Fq 'tampered runtime fixture' "$active_release/scripts/preflight.sh"; then fail 'modified runtime release was reused'; fi
rm -f "$active_release/deploy/compose.yaml"
export FAKE_FAIL_TAR_ONCE=$tmp/fail-tar-once
: >"$FAKE_FAIL_TAR_ONCE"
expect_command_failure "$installer" --install "${base_args[@]}" --ref "$ref_one"
unset FAKE_FAIL_TAR_ONCE
[[ ! -f "$active_release/deploy/compose.yaml" ]] || fail 'interrupted repair replaced the detectable prior state'
if compgen -G "$root/opt/ci-fleet/releases/.${engine_ref}.staging.*" >/dev/null; then fail 'interrupted release staging was not cleaned'; fi
expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one" >/dev/null
manager_release=$(readlink -f "$root/opt/ci-fleet/manager/current")
printf '\n# tampered manager fixture\n' >>"$manager_release/scripts/check-installed-state.sh"
expect_failure 'DRIFT maintenance_timers' "$installer" --check "${base_args[@]}" --ref "$ref_one"
expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one" >/dev/null
if grep -Fq 'tampered manager fixture' "$manager_release/scripts/check-installed-state.sh"; then fail 'modified manager release was reused'; fi
rm -f "$manager_release/scripts/check-installed-state.sh"
expect_failure 'DRIFT maintenance_timers' "$installer" --check "${base_args[@]}" --ref "$ref_one"
expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one" >/dev/null
[[ -x "$manager_release/scripts/check-installed-state.sh" ]] || fail 'incomplete manager release was not repaired consistently'
rm -f "$manager_release/scripts/desired_state.py" "$manager_release/templates/config-repository/fleet.schema.json"
expect_failure 'DRIFT maintenance_timers' "$installer" --check "${base_args[@]}" --ref "$ref_one"
expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one" >/dev/null
[[ -f "$manager_release/scripts/desired_state.py" && -f "$manager_release/templates/config-repository/fleet.schema.json" ]] || fail 'manager helper inputs were not repaired'
mv "$active_release" "$active_release.saved"
expect_failure 'DRIFT engine_release' "$installer" --check "${base_args[@]}" --ref "$ref_one"
mv "$active_release.saved" "$active_release"

relative=$(cd "$tmp" && expect_success "$installer" --install --config-repo config-repo --controller example-ci-01 --ref "$ref_one")
grep -Fq 'NO_CHANGE' <<<"$relative" || fail 'relative configuration path was not normalized before drift comparison'
grep -Fq "CI_FLEET_CONFIG_REPOSITORY=$config_repo" "$root/etc/ci-fleet/ci-fleet.env" || fail 'rendered configuration path is not absolute'

printf '\n' >>"$root/etc/ci-fleet/ci-fleet.env"
printf '\n# drift\n' >>"$root/etc/systemd/system/ci-fleet-health.timer"
expect_failure 'DRIFT rendered_environment' "$installer" --check "${base_args[@]}" --ref "$ref_one"
expect_failure 'DRIFT maintenance_timers' "$installer" --check "${base_args[@]}" --ref "$ref_one"
complete_release_inode=$(stat -c '%i' "$active_release")
expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one" >/dev/null
[[ $(stat -c '%i' "$active_release") == "$complete_release_inode" ]] || fail 'complete immutable release was replaced instead of reused'

prior_manager=$root/opt/ci-fleet/manager/releases/prior-manager
cp -a "$(readlink -f "$root/opt/ci-fleet/manager/current")" "$prior_manager"
ln -sfn "$prior_manager" "$root/opt/ci-fleet/manager/current"
expect_failure 'DRIFT maintenance_timers' "$installer" --check "${base_args[@]}" --ref "$ref_one"

ref_two=$(write_config active 2 2)
export FAKE_FAIL_KILL_ONCE=$tmp/fail-kill-once
: >"$FAKE_FAIL_KILL_ONCE"
expect_failure 'failed to signal the paused controller' "$installer" --upgrade "${base_args[@]}" --ref "$ref_two"
unset FAKE_FAIL_KILL_ONCE
[[ ! -f "$FAKE_PAUSED_STATE" && -f "$FAKE_DOCKER_STATE" ]] || fail 'failed drain left the prior controller paused'
export FAKE_RUNNER_STATE=$tmp/managed-runner-active
: >"$FAKE_RUNNER_STATE"
export FAKE_FAIL_UP_ONCE=$tmp/fail-up-once
: >"$FAKE_FAIL_UP_ONCE"
expect_failure 'ROLLBACK_RESTORED' "$installer" --upgrade "${base_args[@]}" --ref "$ref_two"
[[ ! -f "$FAKE_RUNNER_STATE" ]] || fail 'upgrade preflight ran before the active runner was drained'
unset FAKE_RUNNER_STATE
unset FAKE_FAIL_UP_ONCE
grep -Fq 'CI_FLEET_MAX_RUNNERS=1' "$root/etc/ci-fleet/ci-fleet.env" || fail 'failed activation did not restore capacity one'
[[ $(readlink -f "$root/opt/ci-fleet/manager/current") == "$prior_manager" ]] || fail 'failed activation did not restore the prior manager release'
[[ -f "$FAKE_DOCKER_STATE" ]] || fail 'failed activation did not restore the prior controller runtime'
export FAKE_RESTART_AFTER_UP=$tmp/restart-after-up
: >"$FAKE_RESTART_AFTER_UP"
expect_failure 'ROLLBACK_RESTORED' "$installer" --upgrade "${base_args[@]}" --ref "$ref_two"
unset FAKE_RESTART_AFTER_UP
[[ ! -f "$FAKE_CONTROLLER_STATUS_FILE" && -f "$FAKE_DOCKER_STATE" ]] || fail 'restarting candidate blocked checkpoint restoration'
grep -Fq 'CI_FLEET_MAX_RUNNERS=1' "$root/etc/ci-fleet/ci-fleet.env" || fail 'restarting candidate rollback did not restore capacity one'
export FAKE_COMPOSE_LOG=$tmp/upgrade-compose.log
: >"$FAKE_COMPOSE_LOG"
expect_success "$installer" --upgrade "${base_args[@]}" --ref "$ref_two" >/dev/null
grep -Eq 'stop\|.*\|example-ci-01\|.* stop --timeout 2 controller$' "$FAKE_COMPOSE_LOG" || fail 'controller stop did not use the explicit graceful-shutdown timeout'
unset FAKE_COMPOSE_LOG
grep -Fq 'CI_FLEET_MAX_RUNNERS=2' "$root/etc/ci-fleet/ci-fleet.env" || fail 'upgrade did not apply capacity two'

mkdir -p "$root/var/lib/ci-fleet/checkpoints/99999999-incomplete"
printf 'restarting\n' >"$FAKE_CONTROLLER_STATUS_FILE"
rm -f "$root/var/lib/ci-fleet/install-state.json" "$root/etc/ci-fleet/ci-fleet.env"
expect_success "$installer" --rollback >/dev/null
[[ ! -f "$FAKE_CONTROLLER_STATUS_FILE" ]] || fail 'explicit rollback did not recover a restarting controller'
grep -Fq 'CI_FLEET_MAX_RUNNERS=1' "$root/etc/ci-fleet/ci-fleet.env" || fail 'rollback did not restore capacity one'

ref_three=$(write_config drained 2 2)
printf 'dead\n' >"$FAKE_CONTROLLER_STATUS_FILE"
export FAKE_STOP_FAIL=$tmp/stop-dead-fails
: >"$FAKE_STOP_FAIL"
expect_success "$installer" --upgrade "${base_args[@]}" --ref "$ref_three" >/dev/null
unset FAKE_STOP_FAIL
[[ ! -f "$FAKE_DOCKER_STATE" && ! -f "$FAKE_CONTROLLER_STATUS_FILE" ]] || fail 'non-active convergence retained a dead controller'
grep -Fq 'CI_FLEET_CONTROLLER_STATE=drained' "$root/etc/ci-fleet/ci-fleet.env" || fail 'drained state was not rendered'
grep -Fq 'CI_FLEET_MAX_RUNNERS=0' "$root/etc/ci-fleet/ci-fleet.env" || fail 'drained controller retained effective capacity'
[[ ! -f "$FAKE_DOCKER_STATE" ]] || fail 'drained controller remained running'
export FAKE_RUNNER_STATE=$tmp/drained-managed-runner
: >"$FAKE_RUNNER_STATE"
expect_failure 'DRIFT managed_runners' "$installer" --check "${base_args[@]}" --ref "$ref_three"
rm -f "$FAKE_RUNNER_STATE"
unset FAKE_RUNNER_STATE
export FAKE_ALL_RUNNER_STATE=$tmp/drained-exited-managed-runner
: >"$FAKE_ALL_RUNNER_STATE"
: >"$FAKE_DOCKER_PS_LOG"
expect_failure 'DRIFT managed_runners' "$installer" --check "${base_args[@]}" --ref "$ref_three"
expect_success "$installer" --install "${base_args[@]}" --ref "$ref_three" >/dev/null
[[ ! -f "$FAKE_ALL_RUNNER_STATE" ]] || fail 'non-active convergence did not remove stopped managed runners'
grep -Fq 'label=io.randomdevelopment.ci-fleet.instance=example-ci-01' "$FAKE_DOCKER_PS_LOG" || fail 'managed runner cleanup was not scoped to the selected instance'
unset FAKE_ALL_RUNNER_STATE

ref_four=$(write_config disabled 2 2)
expect_success "$installer" --upgrade "${base_args[@]}" --ref "$ref_four" >/dev/null
grep -Fq 'CI_FLEET_CONTROLLER_STATE=disabled' "$root/etc/ci-fleet/ci-fleet.env" || fail 'disabled state was not rendered'
export FAKE_RUNNER_STATE=$tmp/disabled-managed-runner
: >"$FAKE_RUNNER_STATE"
expect_failure 'DRIFT managed_runners' "$installer" --check "${base_args[@]}" --ref "$ref_four"
rm -f "$FAKE_RUNNER_STATE"
unset FAKE_RUNNER_STATE

export FAKE_RUNNER_STATE_ONCE=$tmp/orphaned-managed-runner
: >"$FAKE_RUNNER_STATE_ONCE"
export FAKE_ALL_RUNNER_STATE=$tmp/uninstall-stopped-managed-runner
: >"$FAKE_ALL_RUNNER_STATE"
: >"$root/etc/ci-fleet/monitoring.env"
mkdir -p "$root/var/lib/ci-fleet/health"
printf '{"status":"healthy"}\n' >"$root/var/lib/ci-fleet/health/latest.json"
: >"$FAKE_DOCKER_PS_LOG"
expect_success "$installer" --uninstall >/dev/null
[[ ! -f "$FAKE_RUNNER_STATE_ONCE" ]] || fail 'uninstall did not wait for an orphaned managed runner'
[[ ! -f "$FAKE_ALL_RUNNER_STATE" ]] || fail 'uninstall retained stopped managed runners'
grep -Fq 'label=io.randomdevelopment.ci-fleet.instance=example-ci-01' "$FAKE_DOCKER_PS_LOG" || fail 'uninstall runner cleanup was not scoped to the installed instance'
if grep -Eq 'label=io.randomdevelopment.ci-fleet.instance=$' "$FAKE_DOCKER_PS_LOG"; then fail 'uninstall runner cleanup used an empty instance filter'; fi
unset FAKE_RUNNER_STATE_ONCE FAKE_ALL_RUNNER_STATE
[[ ! -e "$root/opt/ci-fleet/current" && ! -e "$root/var/lib/ci-fleet/install-state.json" ]] || fail 'uninstall left active installation state'
[[ -f "$host_config" && -f "$pem" ]] || fail 'uninstall removed preserved host credentials'
[[ -f "$root/etc/ci-fleet/monitoring.env" ]] || fail 'uninstall removed host-local monitoring configuration'
[[ ! -e "$root/var/lib/ci-fleet/health" ]] || fail 'uninstall retained fleet-owned health state'

adopt_root=$tmp/adopt-host
export CI_FLEET_ROOT_PREFIX=$adopt_root
export FAKE_DOCKER_STATE=$tmp/adopt-controller-running
mkdir -p "$adopt_root/etc/ci-fleet/secrets" "$adopt_root/opt/ci-fleet/deploy" "$adopt_root/opt/ci-fleet/scripts"
adopt_pem=$adopt_root/etc/ci-fleet/secrets/github-app.pem
printf 'fixture only\n' >"$adopt_pem"
chmod 600 "$adopt_pem"
cp "$repo_root/deploy/compose.yaml" "$adopt_root/opt/ci-fleet/deploy/compose.yaml"
cp "$repo_root/scripts/healthcheck.sh" "$adopt_root/opt/ci-fleet/scripts/healthcheck.sh"
cp "$repo_root/scripts/health.py" "$adopt_root/opt/ci-fleet/scripts/health.py"
cp "$repo_root/scripts/cleanup.sh" "$adopt_root/opt/ci-fleet/scripts/cleanup.sh"
chmod 0755 "$adopt_root/opt/ci-fleet/scripts/healthcheck.sh" "$adopt_root/opt/ci-fleet/scripts/cleanup.sh"
printf '%s\n' \
  'CI_FLEET_GITHUB_APP_CLIENT_ID=Iv1.EXAMPLE' \
  'CI_FLEET_GITHUB_APP_INSTALLATION_ID=123456' \
  "CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE=$adopt_pem" \
  'CI_FLEET_RUNNER_TTL=6h' \
  'CI_FLEET_CONTROLLER_STATE=active' \
  'CI_FLEET_INSTANCE=legacy-ci-01' >"$adopt_root/etc/ci-fleet/ci-fleet.env"
chmod 600 "$adopt_root/etc/ci-fleet/ci-fleet.env"
printf 'CI_FLEET_HEALTH_DISK_WARN_PERCENT=75\n' >"$adopt_root/etc/ci-fleet/monitoring.env"
chmod 600 "$adopt_root/etc/ci-fleet/monitoring.env"
: >"$FAKE_DOCKER_STATE"
chmod 644 "$adopt_root/etc/ci-fleet/ci-fleet.env"
expect_failure 'rendered environment must be owned by root with mode 0600' "$installer" --adopt "${base_args[@]}" --ref "$ref_one"
chmod 600 "$adopt_root/etc/ci-fleet/ci-fleet.env"

export FAKE_COMPOSE_LOG=$tmp/adopt-compose.log
: >"$FAKE_COMPOSE_LOG"
export FAKE_RESTART_AFTER_UP=$tmp/adopt-restart-after-up
: >"$FAKE_RESTART_AFTER_UP"
expect_failure 'ROLLBACK_RESTORED' "$installer" --adopt "${base_args[@]}" --ref "$ref_one"
grep -Fxq 'CI_FLEET_HEALTH_DISK_WARN_PERCENT=75' "$adopt_root/etc/ci-fleet/monitoring.env" || fail 'rollback changed host-local monitoring configuration'
unset FAKE_RESTART_AFTER_UP
grep -Fq "stop|$adopt_root/etc/ci-fleet/ci-fleet.env|example-ci-01" "$FAKE_COMPOSE_LOG" || fail 'rollback did not drain the candidate with its rendered environment and identity'
grep -Fq 'CI_FLEET_INSTANCE=legacy-ci-01' "$adopt_root/etc/ci-fleet/ci-fleet.env" || fail 'failed adoption did not restore the installed controller identity'
: >"$FAKE_COMPOSE_LOG"
export FAKE_RUNNER_STATE_ONCE=$tmp/adopt-managed-runner
: >"$FAKE_RUNNER_STATE_ONCE"
: >"$FAKE_DOCKER_PS_LOG"
adopt=$(expect_success "$installer" --adopt "${base_args[@]}" --ref "$ref_one")
grep -Fq 'CONVERGED mode=adopt' <<<"$adopt" || fail 'adoption did not converge'
[[ -f "$adopt_root/etc/ci-fleet/host.env" ]] || fail 'adoption did not separate host-local values'
grep -Fq 'label=io.randomdevelopment.ci-fleet.instance=legacy-ci-01' "$FAKE_DOCKER_PS_LOG" || fail 'adoption did not drain the installed controller instance'
unset FAKE_RUNNER_STATE_ONCE FAKE_COMPOSE_LOG

# Public pre-health engine fixture; do not depend on a local remote-tracking ref.
legacy_engine_ref=af9c0c13cd12866ce75dd6c43a4cda01915507e1
legacy_ref=$(write_config active 1 1 "$legacy_engine_ref")
export FAKE_ENGINE_REF=$legacy_engine_ref
export FAKE_RUNNER_IMAGE=ci-fleet-runner:${legacy_engine_ref:0:12}
export FAKE_CONTROLLER_IMAGE=ci-fleet-controller:${legacy_engine_ref:0:12}
expect_success "$installer" --upgrade "${base_args[@]}" --ref "$legacy_ref" >/dev/null
[[ $(readlink -f "$adopt_root/opt/ci-fleet/current") == "$adopt_root/opt/ci-fleet/releases/$legacy_engine_ref" ]] || fail 'upgrade could not restore a pre-health-contract engine'

grep -Fq 'Issue #7' "$repo_root/docs/DESIGN-DECISIONS.md" || fail 'isolated proof approval is not recorded'
if grep -Fq '/etc/ci-fleet/ci-fleet.env.before-max2' "$repo_root/docs/CAPACITY-PROMOTION.md"; then fail 'capacity runbook still edits rendered host state'; fi
grep -Fq -- '--upgrade' "$repo_root/docs/CAPACITY-PROMOTION.md" || fail 'capacity runbook does not apply reviewed desired state through the installer'

printf 'INSTALLER_TESTS_OK\n'
