#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
health_timer=$repo_root/host/systemd/ci-fleet-health.timer
grep -Fqx 'OnActiveSec=2min' "$health_timer" || { printf 'FAIL: health timer lacks activation-relative initial trigger\n' >&2; exit 1; }
! grep -Fq 'OnBootSec=' "$health_timer" || { printf 'FAIL: health timer initial trigger is boot-relative\n' >&2; exit 1; }
grep -Fq 'export CI_FLEET_HEALTH_SUPPRESS_DELIVERY=1' "$repo_root/scripts/install-worker-controller.sh" || { printf 'FAIL: installer health check can submit monitoring reports\n' >&2; exit 1; }
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fake_bin=$tmp/bin
mkdir -p "$fake_bin"
export REAL_STAT
REAL_STAT=$(command -v stat)
export REAL_TAR
REAL_TAR=$(command -v tar)
export REAL_GIT
REAL_GIT=$(command -v git)
export REAL_DF
REAL_DF=$(command -v df)

cat >"$fake_bin/docker" <<'EOF'
#!/usr/bin/env bash
set -u
state=${FAKE_DOCKER_STATE:?}
status_file=${FAKE_CONTROLLER_STATUS_FILE:-}
paused_state=${FAKE_PAUSED_STATE:-}
stopped_state=${FAKE_STOPPED_CONTROLLER_STATE:-}
if [[ -n ${FAKE_REQUIRE_LOCAL_DOCKER_ENDPOINT:-} ]]; then
  expected_socket=${CI_FLEET_ROOT_PREFIX:-}/var/run/docker.sock
  [[ ${DOCKER_HOST:-} == "unix://$expected_socket" ]] || {
    printf 'expected local Docker socket %s, got %s\n' "unix://$expected_socket" "${DOCKER_HOST:-<unset>}" >&2
    exit 91
  }
fi
case "${1:-}" in
  info)
    if [[ -n ${FAKE_DOCKER_INFO_FAIL_AFTER_IMAGE_INSPECT:-} && -f $FAKE_DOCKER_INFO_FAIL_AFTER_IMAGE_INSPECT ]]; then exit 1; fi
    [[ "$*" != *DockerRootDir* ]] || printf '%s\n' "${CI_FLEET_DOCKER_ROOT:?}"
    exit 0
    ;;
  inspect)
    [[ -f "$state" || -n "$stopped_state" && -f "$stopped_state" ]] || exit 1
    if [[ "$*" == *'.Config.Env'* ]]; then
      [[ -n "${FAKE_CONTROLLER_ENV_FILE:-}" && -f "$FAKE_CONTROLLER_ENV_FILE" ]] || exit 1
      cat "$FAKE_CONTROLLER_ENV_FILE"
    elif [[ "$*" == *'org.opencontainers.image.revision'* ]]; then
      if [[ -n "${FAKE_CONTROLLER_PROVENANCE_FILE:-}" && -f "$FAKE_CONTROLLER_PROVENANCE_FILE" ]]; then cat "$FAKE_CONTROLLER_PROVENANCE_FILE"; else printf '%s\n' "${FAKE_ENGINE_REF:?}"; fi
    elif [[ "$*" == *'{{.Image}}'* ]]; then
      [[ -n "${FAKE_CONTROLLER_IMAGE_ID_FILE:-}" && -f "$FAKE_CONTROLLER_IMAGE_ID_FILE" ]] || exit 1
      cat "$FAKE_CONTROLLER_IMAGE_ID_FILE"
    elif [[ "$*" == *'.State.Status'* ]]; then
      if [[ -n "$paused_state" && -f "$paused_state" ]]; then printf 'paused\n'; elif [[ -n "$status_file" && -f "$status_file" ]]; then cat "$status_file"; elif [[ -n "$stopped_state" && -f "$stopped_state" ]]; then printf 'exited\n'; else printf '%s\n' "${FAKE_CONTROLLER_STATUS:-running}"; fi
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
    elif [[ "$*" != *'io.randomdevelopment.ci-fleet.kind=runner'* && -n "${FAKE_ACTIVE_MANAGED_STATE:-}" && -f "$FAKE_ACTIVE_MANAGED_STATE" ]]; then
      printf 'active-managed-container\n'
    fi
    exit 0
    ;;
  image)
    case "${2:-}" in
      inspect)
        image=${!#}
        [[ -z "${FAKE_IMAGE_INSPECT_LOG:-}" ]] || printf '%s\n' "$image" >>"$FAKE_IMAGE_INSPECT_LOG"
        if [[ "$image" == "${FAKE_RUNNER_IMAGE:-}" || "$image" == "${FAKE_PRIOR_RUNNER_IMAGE:-}" || "$image" == "${FAKE_PREVIOUS_RUNNER_IMAGE:-}" ]]; then
          image_state=${FAKE_RUNNER_IMAGE_STATE:-}
          image_id_state=${FAKE_RUNNER_IMAGE_ID_STATE:-}
        elif [[ "$image" == "${FAKE_CONTROLLER_IMAGE:-}" || "$image" == "${FAKE_PRIOR_CONTROLLER_IMAGE:-}" || "$image" == "${FAKE_PREVIOUS_CONTROLLER_IMAGE:-}" ]]; then
          image_state=${FAKE_CONTROLLER_IMAGE_STATE:-}
          image_id_state=${FAKE_CONTROLLER_IMAGE_ID_STATE:-}
        else
          exit 1
        fi
        if [[ "$*" == *'{{.Id}}'* ]]; then
          if [[ ! -f "$image_id_state" ]]; then
            [[ -z ${FAKE_DOCKER_INFO_FAIL_AFTER_IMAGE_INSPECT:-} ]] || : >"$FAKE_DOCKER_INFO_FAIL_AFTER_IMAGE_INSPECT"
            exit 1
          fi
          cat "$image_id_state"
        else
          [[ -f "$image_state" ]] && cat "$image_state"
        fi
        ;;
      tag)
        image_id=${3:-}
        image=${4:-}
        [[ -n "${FAKE_AVAILABLE_IMAGE_IDS:-}" && -f "$FAKE_AVAILABLE_IMAGE_IDS" ]] || exit 1
        grep -Fxq "$image_id" "$FAKE_AVAILABLE_IMAGE_IDS" || exit 1
        if [[ "$image" == "${FAKE_RUNNER_IMAGE:-}" || "$image" == "${FAKE_PRIOR_RUNNER_IMAGE:-}" || "$image" == "${FAKE_PREVIOUS_RUNNER_IMAGE:-}" ]]; then
          image_id_state=${FAKE_RUNNER_IMAGE_ID_STATE:-}
        elif [[ "$image" == "${FAKE_CONTROLLER_IMAGE:-}" || "$image" == "${FAKE_PRIOR_CONTROLLER_IMAGE:-}" || "$image" == "${FAKE_PREVIOUS_CONTROLLER_IMAGE:-}" ]]; then
          image_id_state=${FAKE_CONTROLLER_IMAGE_ID_STATE:-}
        else
          exit 1
        fi
        printf '%s\n' "$image_id" >"$image_id_state"
        [[ -z "${FAKE_COMPOSE_LOG:-}" ]] || printf 'image-tag|%s|%s\n' "$image_id" "$image" >>"$FAKE_COMPOSE_LOG"
        ;;
      rm)
        image=${3:-}
        if [[ "$image" == "${FAKE_RUNNER_IMAGE:-}" || "$image" == "${FAKE_PRIOR_RUNNER_IMAGE:-}" || "$image" == "${FAKE_PREVIOUS_RUNNER_IMAGE:-}" ]]; then
          image_state=${FAKE_RUNNER_IMAGE_STATE:-}
          image_id_state=${FAKE_RUNNER_IMAGE_ID_STATE:-}
        elif [[ "$image" == "${FAKE_CONTROLLER_IMAGE:-}" || "$image" == "${FAKE_PRIOR_CONTROLLER_IMAGE:-}" || "$image" == "${FAKE_PREVIOUS_CONTROLLER_IMAGE:-}" ]]; then
          image_state=${FAKE_CONTROLLER_IMAGE_STATE:-}
          image_id_state=${FAKE_CONTROLLER_IMAGE_ID_STATE:-}
        else
          exit 1
        fi
        if [[ -n "$stopped_state" && -f "$stopped_state" && -n "${FAKE_CONTROLLER_IMAGE_ID_FILE:-}" && -f "$FAKE_CONTROLLER_IMAGE_ID_FILE" && -f "$image_id_state" ]] \
          && cmp -s "$FAKE_CONTROLLER_IMAGE_ID_FILE" "$image_id_state"; then
          [[ -z "${FAKE_COMPOSE_LOG:-}" ]] || printf 'image-rm-blocked|%s\n' "$image" >>"$FAKE_COMPOSE_LOG"
          exit 48
        fi
        rm -f "$image_state" "$image_id_state"
        [[ -z "${FAKE_COMPOSE_LOG:-}" ]] || printf 'image-rm|%s\n' "$image" >>"$FAKE_COMPOSE_LOG"
        ;;
      *) exit 1 ;;
    esac
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
        controller_image=$(awk -F= '$1 == "CI_FLEET_CONTROLLER_IMAGE" {print substr($0, index($0, "=") + 1)}' "$env_file")
        "$0" image inspect --format '{{.Id}}' "$controller_image" >/dev/null 2>&1 || {
          printf 'configured controller image is unavailable: %s\n' "$controller_image" >&2
          exit 49
        }
        if [[ -n "${FAKE_DELAY_UP_ONCE:-}" && -f "$FAKE_DELAY_UP_ONCE" ]]; then
          delay_marker=$FAKE_DELAY_UP_ONCE
          rm -f "$delay_marker"
          : >"${delay_marker}.entered"
          sleep 2
        fi
        if [[ -n "${FAKE_FAIL_UP_ONCE:-}" && -f "$FAKE_FAIL_UP_ONCE" ]]; then
          rm -f "$FAKE_FAIL_UP_ONCE"
          exit 42
        fi
        : >"$state"
        [[ -z "$stopped_state" ]] || rm -f "$stopped_state"
        [[ -z "${FAKE_CONTROLLER_PROVENANCE_FILE:-}" ]] || printf '%s\n' "${FAKE_ENGINE_REF:?}" >"$FAKE_CONTROLLER_PROVENANCE_FILE"
        [[ -z "${FAKE_CONTROLLER_IMAGE_ID_FILE:-}" || -z "${FAKE_CONTROLLER_IMAGE_ID_STATE:-}" ]] || cp "$FAKE_CONTROLLER_IMAGE_ID_STATE" "$FAKE_CONTROLLER_IMAGE_ID_FILE"
        [[ -z "${FAKE_CONTROLLER_ENV_FILE:-}" ]] || cp "$env_file" "$FAKE_CONTROLLER_ENV_FILE"
        if [[ -n "${FAKE_RESTART_AFTER_UP:-}" && -f "$FAKE_RESTART_AFTER_UP" ]]; then
          rm -f "$FAKE_RESTART_AFTER_UP"
          printf 'restarting\n' >"$status_file"
        elif [[ -n "$status_file" ]]; then
          rm -f "$status_file"
        fi
        ;;
      stop)
        if [[ -n "$paused_state" && -f "$paused_state" ]]; then exit 42; fi
        if [[ -n "${FAKE_STOP_FAIL:-}" && -f "$FAKE_STOP_FAIL" ]]; then exit 42; fi
        rm -f "$state"; [[ -z "$status_file" ]] || rm -f "$status_file"; [[ -z "$paused_state" ]] || rm -f "$paused_state"
        if [[ -n "$stopped_state" ]]; then
          : >"$stopped_state"
        else
          [[ -z "${FAKE_CONTROLLER_PROVENANCE_FILE:-}" ]] || rm -f "$FAKE_CONTROLLER_PROVENANCE_FILE"; [[ -z "${FAKE_CONTROLLER_IMAGE_ID_FILE:-}" ]] || rm -f "$FAKE_CONTROLLER_IMAGE_ID_FILE"; [[ -z "${FAKE_CONTROLLER_ENV_FILE:-}" ]] || rm -f "$FAKE_CONTROLLER_ENV_FILE"
        fi
        [[ -z "${FAKE_ACTIVE_MANAGED_AFTER_STOP:-}" ]] || : >"$FAKE_ACTIVE_MANAGED_AFTER_STOP"
        ;;
      down|rm) rm -f "$state"; [[ -z "$status_file" ]] || rm -f "$status_file"; [[ -z "$paused_state" ]] || rm -f "$paused_state"; [[ -z "$stopped_state" ]] || rm -f "$stopped_state"; [[ -z "${FAKE_CONTROLLER_PROVENANCE_FILE:-}" ]] || rm -f "$FAKE_CONTROLLER_PROVENANCE_FILE"; [[ -z "${FAKE_CONTROLLER_IMAGE_ID_FILE:-}" ]] || rm -f "$FAKE_CONTROLLER_IMAGE_ID_FILE"; [[ -z "${FAKE_CONTROLLER_ENV_FILE:-}" ]] || rm -f "$FAKE_CONTROLLER_ENV_FILE" ;;
      pause)
        if [[ -n "$paused_state" && -f "$paused_state" ]]; then
          printf 'Error response from daemon: container is already paused\n' >&2
          exit 42
        fi
        [[ -z "$paused_state" ]] || : >"$paused_state"
        [[ -n "${FAKE_KEEP_RUNNER_ON_PAUSE:-}" || -z "${FAKE_RUNNER_STATE:-}" ]] || rm -f "$FAKE_RUNNER_STATE"
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
        [[ -z "${FAKE_FAIL_BUILD:-}" ]] || exit 46
        if [[ -n "${FAKE_PARTIAL_BUILD_FAIL:-}" ]]; then
          printf '%s\n' "${FAKE_PARTIAL_RUNNER_IMAGE_ID:?}" >>"${FAKE_AVAILABLE_IMAGE_IDS:?}"
          printf '%s\n' "$FAKE_PARTIAL_RUNNER_IMAGE_ID" >"${FAKE_RUNNER_IMAGE_ID_STATE:?}"
          exit 46
        fi
        [[ -z "${FAKE_RUNNER_IMAGE_STATE:-}" ]] || printf '%s\n' "${FAKE_ENGINE_REF:?}" >"$FAKE_RUNNER_IMAGE_STATE"
        [[ -z "${FAKE_CONTROLLER_IMAGE_STATE:-}" ]] || printf '%s\n' "${FAKE_ENGINE_REF:?}" >"$FAKE_CONTROLLER_IMAGE_STATE"
        [[ -z "${FAKE_RUNNER_IMAGE_ID_STATE:-}" ]] || printf '%s\n' "${FAKE_CANDIDATE_RUNNER_IMAGE_ID:?}" >"$FAKE_RUNNER_IMAGE_ID_STATE"
        [[ -z "${FAKE_CONTROLLER_IMAGE_ID_STATE:-}" ]] || printf '%s\n' "${FAKE_CANDIDATE_CONTROLLER_IMAGE_ID:?}" >"$FAKE_CONTROLLER_IMAGE_ID_STATE"
        [[ -z "${FAKE_AVAILABLE_IMAGE_IDS:-}" ]] || printf '%s\n%s\n' "$FAKE_CANDIDATE_RUNNER_IMAGE_ID" "$FAKE_CANDIDATE_CONTROLLER_IMAGE_ID" >>"$FAKE_AVAILABLE_IMAGE_IDS"
        [[ -z "${FAKE_DISK_USED_PERCENT_AFTER_BUILD:-}" || -z "${FAKE_DISK_USED_PERCENT_FILE:-}" ]] || printf '%s\n' "$FAKE_DISK_USED_PERCENT_AFTER_BUILD" >"$FAKE_DISK_USED_PERCENT_FILE"
        ;;
      config) [[ -z "${FAKE_FAIL_CONFIG:-}" ]] || exit 47 ;;
      logs) ;;
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
if [[ -n "${FAKE_FAIL_TIMER_ENABLE:-}" && "${1:-}" == enable && "${2:-}" == --now && "$*" == *ci-fleet-reconcile.timer* ]]; then
  exit 97
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

cat >"$fake_bin/git" <<'EOF'
#!/usr/bin/env bash
if [[ -n ${FAKE_FAIL_GIT_FETCH:-} && " $* " == *" fetch "* ]]; then
  exit 90
fi
exec "$REAL_GIT" "$@"
EOF
chmod 700 "$fake_bin/git"

cat >"$fake_bin/df" <<'EOF'
#!/usr/bin/env bash
if [[ -n ${FAKE_DISK_USED_PERCENT_FILE:-} && -f $FAKE_DISK_USED_PERCENT_FILE ]]; then
  used=$(<"$FAKE_DISK_USED_PERCENT_FILE")
elif [[ -n ${FAKE_DISK_USED_PERCENT:-} ]]; then
  used=$FAKE_DISK_USED_PERCENT
else
  exec "$REAL_DF" "$@"
fi
if [[ -n ${used:-} ]]; then
  printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n'
  printf 'fixture 100 90 10 %s%% /fixture\n' "$used"
  exit 0
fi
EOF
chmod 700 "$fake_bin/df"

export PATH="$fake_bin:$PATH"

# Build a PATH that mirrors the real one but omits openssl, so the
# installer's command-presence preflight can be exercised for the
# remote-reconciliation dependency without disturbing the rest of the test.
no_ssl_dir=$tmp/bin-no-ssl
mkdir -p "$no_ssl_dir"
IFS=: read -ra path_dirs <<< "$PATH"
for dir in "${path_dirs[@]}"; do
  [[ -d "$dir" ]] || continue
  for entry in "$dir"/*; do
    base=$(basename "$entry")
    [[ "$base" == openssl ]] && continue
    [[ -e "$no_ssl_dir/$base" ]] || ln -sf "$entry" "$no_ssl_dir/$base" 2>/dev/null
  done
done
export NO_SSL_PATH="$no_ssl_dir"

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
export FAKE_RUNNER_IMAGE_ID_STATE=$tmp/runner-image-id
export FAKE_CONTROLLER_IMAGE_ID_STATE=$tmp/controller-image-id
export FAKE_AVAILABLE_IMAGE_IDS=$tmp/available-image-ids
export FAKE_CANDIDATE_RUNNER_IMAGE_ID=sha256:1111111111111111111111111111111111111111111111111111111111111111
export FAKE_CANDIDATE_CONTROLLER_IMAGE_ID=sha256:2222222222222222222222222222222222222222222222222222222222222222
export FAKE_PARTIAL_RUNNER_IMAGE_ID=sha256:3333333333333333333333333333333333333333333333333333333333333333
export FAKE_IMAGE_INSPECT_LOG=$tmp/image-inspects
for dockerfile in "$repo_root/controller/Dockerfile" "$repo_root/runner/Dockerfile"; do
  grep -Fq "LABEL org.opencontainers.image.revision=\"\${CI_FLEET_COMMIT}\"" "$dockerfile" || fail "managed image lacks engine provenance label: $dockerfile"
  grep -Fq 'io.randomdevelopment.ci-fleet.managed="true"' "$dockerfile" || fail "managed image lacks fleet ownership label: $dockerfile"
done
grep -Fq '    user: "0:0"' "$repo_root/deploy/compose.yaml" || fail 'controller cannot read the required root-owned mode-0600 GitHub App PEM'
grep -Fq 'export PYTHONDONTWRITEBYTECODE=1' "$repo_root/scripts/install-worker-controller.sh" || fail 'managed validation may write Python bytecode into the immutable manager release'
grep -Fq '    trap - ERR' "$repo_root/scripts/install-worker-controller.sh" || fail 'warning health subprocess inherits the transactional rollback trap'
grep -Fq "CI_FLEET_COMMIT: \${CI_FLEET_COMMIT:-unknown}" "$repo_root/deploy/compose.yaml" || fail 'runner build lacks engine provenance argument'
config_repo=$tmp/config-repo
git init -q "$config_repo"
git -C "$config_repo" config user.name fixture
git -C "$config_repo" config user.email fixture@example.invalid

write_config() {
  local state=$1 maximum=$2 budget=$3
  local desired_engine=${4:-$engine_ref} reporting=${5:-false} network_policy=${6:-present}
  python3 - "$repo_root/templates/config-repository/fleet.json" "$config_repo/fleet.json" "$config_repo/engine-rollout-evidence.json" "$desired_engine" "$state" "$maximum" "$budget" "$reporting" "$network_policy" <<'PY'
import json
import sys
source, target, evidence_target, engine_ref, state, maximum, budget, reporting, network_policy = sys.argv[1:]
value = json.load(open(source, encoding="utf-8"))
value["organization"]["slug"] = "fixture-org"
value["runner_pools"]["trusted-ci"]["allowed_repositories"] = ["fixture-org/example-app"]
value["projects"]["example-app"]["repository"] = "fixture-org/example-app"
controller = value["controllers"]["example-ci-01"]
controller["docker_network_policy"]["default_address_pools"][0]["base"] = "10.64.0.0/24"
controller["engine_ref"] = engine_ref
controller["state"] = state
controller["max_runners"] = int(maximum)
if network_policy == "omit":
    controller.pop("docker_network_policy")
if reporting == "omit":
    controller.pop("status_reporting", None)
else:
    controller["status_reporting"] = {
        "enabled": reporting == "true",
        "config_file": "/etc/ci-fleet/monitoring.env",
    }
value["runner_pools"]["trusted-ci"]["capacity_budget"] = int(budget)
with open(target, "w", encoding="utf-8") as handle:
    json.dump(value, handle, indent=2)
    handle.write("\n")
with open(evidence_target, "w", encoding="utf-8") as handle:
    json.dump({
        "schema_version": 1,
        "status_reporting_engine_capabilities": {
            "example-ci-01": {
                "engine_ref": engine_ref,
                "status_reporting_config": True,
                "required_status_reporting": True,
                "docker_network_policy_config": network_policy != "omit",
            },
        },
    }, handle, indent=2)
    handle.write("\n")
PY
  git -C "$config_repo" add fleet.json engine-rollout-evidence.json
  git -C "$config_repo" commit -q -m "fixture $state $maximum"
  git -C "$config_repo" rev-parse HEAD
}

root=$tmp/host
export CI_FLEET_ROOT_PREFIX=$root
export CI_FLEET_DOCKER_ROOT=$root/var/lib/docker
mkdir -p "$root/etc/ci-fleet/secrets" "$root/etc/ssl/certs" "$root/var/run" "$CI_FLEET_DOCKER_ROOT"
printf 'ID=debian\nVERSION_ID="12"\n' >"$root/etc/os-release"
printf 'fixture CA bundle\n' >"$root/etc/ssl/certs/ca-certificates.crt"
: >"$root/var/run/docker.sock"
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

expect_failure 'alternate Docker endpoints are not supported' env DOCKER_HOST=tcp://example.invalid:2376 "$installer" --check "${base_args[@]}" --ref "$ref_one"
expect_failure 'alternate Docker contexts are not supported' env DOCKER_CONTEXT=remote "$installer" --check "${base_args[@]}" --ref "$ref_one"
printf 'ID=example\nVERSION_ID="1"\n' >"$root/etc/os-release"
expect_failure 'supported Linux is Debian 12 or newer' "$installer" --check "${base_args[@]}" --ref "$ref_one"
printf 'ID=debian\nVERSION_ID="12"\n' >"$root/etc/os-release"
export FAKE_DISK_USED_PERCENT=80
expect_failure 'Docker filesystem must remain below 80% utilization' "$installer" --check "${base_args[@]}" --ref "$ref_one"
unset FAKE_DISK_USED_PERCENT

# The installed maintenance scripts must pin the local Docker daemon themselves,
# not just inherit it from the installer.
export FAKE_REQUIRE_LOCAL_DOCKER_ENDPOINT=1
expect_success "$repo_root/scripts/cleanup.sh" --apply
unset FAKE_REQUIRE_LOCAL_DOCKER_ENDPOINT

# Remote reconciliation enables the ci-fleet-reconcile timer during install,
# and reconciliation signs the GitHub App JWT with openssl. Require openssl
# before install/check so the enabled timer cannot fail at runtime.
expect_failure 'openssl is required' env PATH="$NO_SSL_PATH" "$installer" --check "${base_args[@]}" --ref "$ref_one"

staged_checkpoint="$root/var/lib/ci-fleet/checkpoints/.checkpoint.staging.interrupted"
mkdir -p "$staged_checkpoint"
: >"$staged_checkpoint/.complete"
mv "$root/etc/os-release" "$root/etc/os-release.missing"
expect_failure 'no controller checkpoint is available' "$installer" --rollback
mv "$root/etc/os-release.missing" "$root/etc/os-release"
rm -rf "$staged_checkpoint"
expect_failure 'secret-bearing files are forbidden' "$installer" --check "${base_args[@]}" --ref "$forbidden_ref"
expect_failure 'possible committed secret detected' "$installer" --check "${base_args[@]}" --ref "$secret_ref"
export FAKE_WRONG_HOST_CONFIG_OWNER=$host_config
expect_failure 'host configuration must be owned by root' "$installer" --install "${base_args[@]}" --ref "$ref_one"
unset FAKE_WRONG_HOST_CONFIG_OWNER
expect_failure 'managed installs require the default' "$installer" --check "${base_args[@]}" --ref "$ref_one" --host-config "$tmp/custom-host.env"

first=$(expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one")
expect_success env DOCKER_CONTEXT=default "$installer" --check "${base_args[@]}" --ref "$ref_one"
grep -Fq 'CONVERGED mode=install' <<<"$first" || fail 'fresh install did not converge'
[[ -L "$root/opt/ci-fleet/current" && -f "$root/var/lib/ci-fleet/install-state.json" ]] || fail 'fresh install state is incomplete'
[[ $(readlink -f "$root/opt/ci-fleet/manager/current") == "$root/opt/ci-fleet/manager/releases/$engine_ref" ]] || fail 'installer manager did not activate the desired engine release'
[[ -f "$FAKE_DOCKER_STATE" ]] || fail 'active controller was not started'
fresh_checkpoint=$(find "$root/var/lib/ci-fleet/checkpoints" -mindepth 1 -maxdepth 1 -type d ! -name '.checkpoint.staging.*' -print -quit)
fresh_format_marker=$fresh_checkpoint/format-version
[[ -f "$fresh_format_marker" && ! -L "$fresh_format_marker" && $(stat -c '%u:%a:%s' "$fresh_format_marker") == "$(id -u):600:2" && $(<"$fresh_format_marker") == 2 ]] || fail 'fresh checkpoint lacks the exact root-owned mode-0600 format marker'
[[ ! -e "$fresh_checkpoint/ci-fleet.env" && ! -e "$fresh_checkpoint/image-ids.env" ]] || fail 'fresh checkpoint stored prior managed image state'
if find "$root/var/lib/ci-fleet/checkpoints" -name image-ids.env -print -quit | grep -q .; then fail 'fresh install checkpoint stored a prior image map'; fi
install_state=$root/var/lib/ci-fleet/install-state.json
chmod 644 "$install_state"
expect_failure 'install state must be owned by root with mode 0600' env CI_FLEET_INSTALL_STATE_FILE="$install_state" CI_FLEET_INSTALLER="$installer" "$repo_root/scripts/check-installed-state.sh"
expect_failure 'DRIFT install_state' "$installer" --check "${base_args[@]}" --ref "$ref_one"
expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one" >/dev/null
[[ $(stat -c %a "$install_state") == 600 ]] || fail 'convergence did not repair install-state mode'
rendered_env=$root/etc/ci-fleet/ci-fleet.env
chmod 644 "$rendered_env"
expect_failure 'DRIFT rendered_environment' "$installer" --check "${base_args[@]}" --ref "$ref_one"
expect_failure 'rendered environment must be owned by root with mode 0600' "$installer" --install "${base_args[@]}" --ref "$ref_one"
[[ $(stat -c %a "$rendered_env") == 644 ]] || fail 'untrusted rendered environment was changed before checkpoint validation'
chmod 600 "$rendered_env"
export FAKE_REQUIRE_LOCAL_DOCKER_ENDPOINT=1
manual_health_result=0
"$repo_root/scripts/healthcheck.sh" >/dev/null || manual_health_result=$?
((manual_health_result < 2)) || fail 'manual healthcheck did not source rendered capacity'
unset FAKE_REQUIRE_LOCAL_DOCKER_ENDPOINT
export FAKE_WRONG_INSTALL_STATE_OWNER=$install_state
expect_failure 'install state must be owned by root with mode 0600' env CI_FLEET_INSTALL_STATE_FILE="$install_state" CI_FLEET_INSTALLER="$installer" "$repo_root/scripts/check-installed-state.sh"
unset FAKE_WRONG_INSTALL_STATE_OWNER
expect_success env CI_FLEET_INSTALL_STATE_FILE="$install_state" CI_FLEET_INSTALLER="$installer" "$repo_root/scripts/check-installed-state.sh" >/dev/null

remote_reconciler=$tmp/fake-remote-reconciler
remote_reconciler_log=$tmp/fake-remote-reconciler.log
# shellcheck disable=SC2016
printf '#!/usr/bin/env bash\nprintf "%%s\\n%%s\\n" "$*" "$CI_FLEET_REMOTE_STATE_FILE" >"$REMOTE_RECONCILER_LOG"\n' >"$remote_reconciler"
chmod 0755 "$remote_reconciler"
python3 - "$install_state" <<'PY'
import json, sys
path = sys.argv[1]
state = json.load(open(path))
state["config_repository"] = "example/private-config"
with open(path, "w") as output:
    json.dump(state, output)
PY
expect_success env CI_FLEET_INSTALL_STATE_FILE="$install_state" CI_FLEET_INSTALLER="$installer" CI_FLEET_REMOTE_RECONCILER="$remote_reconciler" REMOTE_RECONCILER_LOG="$remote_reconciler_log" "$repo_root/scripts/check-installed-state.sh"
mapfile -t remote_call <"$remote_reconciler_log"
[[ ${remote_call[0]} == "--check-only --installed-ref" && ${remote_call[1]} == "$install_state" ]] || fail 'remote drift check did not delegate the exact installed state to authenticated reconciliation'
expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one" >/dev/null

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
installed_installer=$root/opt/ci-fleet/manager/current/scripts/install-worker-controller.sh
expect_success env FAKE_FAIL_GIT_FETCH=1 "$installed_installer" --check "${base_args[@]}" --ref "$ref_one" >/dev/null
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
[[ $(<"$FAKE_CONTROLLER_IMAGE_ID_FILE") == "$FAKE_CANDIDATE_CONTROLLER_IMAGE_ID" ]] || fail 'controller convergence did not restore live image identity'
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
printf '{"schema_version":1,"capabilities":null}\n' >"$active_release/engine-capabilities.json"
expect_failure 'DRIFT engine_release' "$installer" --check "${base_args[@]}" --ref "$ref_one"
expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one" >/dev/null
python3 "$repo_root/scripts/desired_state.py" validate-engine-capabilities --manifest "$active_release/engine-capabilities.json" >/dev/null || fail 'engine capability declaration was not repaired'
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

remote_args=(--config-repo "$config_repo" --config-identity fixture-org/fleet-config --controller example-ci-01 --ref "$ref_one")
export FAKE_FAIL_TIMER_ENABLE=1
expect_command_failure "$installer" --install "${remote_args[@]}"
unset FAKE_FAIL_TIMER_ENABLE
grep -Fq "CI_FLEET_CONFIG_REPOSITORY=$config_repo" "$root/etc/ci-fleet/ci-fleet.env" || fail 'timer activation failure did not restore the local identity'
expect_success "$installer" --install "${remote_args[@]}" >/dev/null
grep -Fq 'CI_FLEET_CONFIG_REPOSITORY=fixture-org/fleet-config' "$root/etc/ci-fleet/ci-fleet.env" || fail 'local checkout did not retain its durable repository identity'
grep -Fq '"config_repository": "fixture-org/fleet-config"' "$install_state" || fail 'install state did not retain the durable repository identity'
custom_lock=$root/run/custom-installer.lock
exec 9>"$custom_lock"
flock -n 9 || fail 'fixture could not acquire installer lock'
expect_success env CI_FLEET_INSTALLER_LOCK="$custom_lock" CI_FLEET_INSTALLER_LOCK_FD=9 "$installer" --check "${remote_args[@]}" >/dev/null
flock -u 9
exec 9>&-
expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one" >/dev/null

printf '\n' >>"$root/etc/ci-fleet/ci-fleet.env"
printf '\n# drift\n' >>"$root/etc/systemd/system/ci-fleet-health.timer"
expect_failure 'DRIFT rendered_environment' "$installer" --check "${base_args[@]}" --ref "$ref_one"
expect_failure 'DRIFT maintenance_timers' "$installer" --check "${base_args[@]}" --ref "$ref_one"
complete_release_inode=$(stat -c '%i' "$active_release")
expect_success "$installer" --install "${base_args[@]}" --ref "$ref_one" >/dev/null
[[ $(stat -c '%i' "$active_release") == "$complete_release_inode" ]] || fail 'complete immutable release was replaced instead of reused'

warning_ref=$(write_config active 1 2)
printf '%s\n' \
  'CI_FLEET_HEALTH_DISK_WARN_PERCENT=0' \
  'CI_FLEET_HEALTH_STATUS_URL=https://status.example.invalid/v1/status' >"$root/etc/ci-fleet/monitoring.env"
chmod 600 "$root/etc/ci-fleet/monitoring.env"
warning_output=$tmp/warning-upgrade.out
"$installer" --upgrade "${base_args[@]}" --ref "$warning_ref" >"$warning_output" 2>&1
warning_upgrade=$(<"$warning_output")
if grep -Fq 'ERROR: alternate Docker endpoints are not supported' <<<"$warning_upgrade"; then fail 'healthcheck lost its test root while clearing candidate environment'; fi
grep -Fq 'WARNING disk_root' <<<"$warning_upgrade" || fail 'warning health fixture did not produce a warning result'
if grep -Fq 'WARNING status_delivery' <<<"$warning_upgrade"; then fail 'ad-hoc reconciliation health check submitted duplicate status'; fi
grep -Fq 'CONVERGED mode=upgrade' <<<"$warning_upgrade" || fail 'warning health result did not report convergence'
grep -Fq "CI_FLEET_CONFIG_REF=$warning_ref" "$root/etc/ci-fleet/ci-fleet.env" || fail 'warning health result rolled back an otherwise healthy activation'
rm -f "$root/etc/ci-fleet/monitoring.env"
ref_one=$warning_ref

prior_manager=$root/opt/ci-fleet/manager/releases/prior-manager
cp -a "$(readlink -f "$root/opt/ci-fleet/manager/current")" "$prior_manager"
ln -sfn "$prior_manager" "$root/opt/ci-fleet/manager/current"
expect_failure 'DRIFT maintenance_timers' "$installer" --check "${base_args[@]}" --ref "$ref_one"

ref_two=$(write_config active 2 2)
prior_runner_image=ci-fleet-runner:prior
prior_controller_image=ci-fleet-controller:prior
export FAKE_PRIOR_RUNNER_IMAGE=$prior_runner_image
export FAKE_PRIOR_CONTROLLER_IMAGE=$prior_controller_image
for environment in "$rendered_env" "$FAKE_CONTROLLER_ENV_FILE"; do
  python3 -c 'from pathlib import Path; import sys; path = Path(sys.argv[1]); path.write_text(path.read_text().replace(sys.argv[2], sys.argv[3]))' "$environment" "$FAKE_RUNNER_IMAGE" "$prior_runner_image"
done
python3 -c 'from pathlib import Path; import sys; path = Path(sys.argv[1]); path.write_text(path.read_text().replace(sys.argv[2], sys.argv[3]))' "$rendered_env" "$FAKE_CONTROLLER_IMAGE" "$prior_controller_image"
build_failure_output=$tmp/build-failure.out
build_failure_root=$tmp/build-failure-root
cp -a "$root" "$build_failure_root"
export FAKE_COMPOSE_LOG=$tmp/stopped-distinct-tag-build-compose.log
: >"$FAKE_COMPOSE_LOG"
printf 'exited\n' >"$FAKE_CONTROLLER_STATUS_FILE"
export FAKE_FAIL_BUILD=1
if "$installer" --upgrade "${base_args[@]}" --ref "$ref_two" >"$build_failure_output" 2>&1; then
  fail 'candidate build failure unexpectedly succeeded'
fi
unset FAKE_FAIL_BUILD
if grep -Eq 'CHECKPOINT_CREATED|DRAIN_READY|ROLLBACK_' "$build_failure_output"; then fail 'candidate build failure entered the transaction'; fi
grep -q '^build|' "$FAKE_COMPOSE_LOG" || fail 'stopped controller with distinct trusted tags did not prebuild'
if grep -q '^stop|' "$FAKE_COMPOSE_LOG"; then fail 'stopped controller with distinct trusted tags was stopped before prebuild'; fi
diff -r "$build_failure_root" "$root" >/dev/null || fail 'candidate build failure changed host state'
[[ -f "$FAKE_DOCKER_STATE" ]] || fail 'candidate build failure stopped the installed controller'
unset FAKE_COMPOSE_LOG
python3 -c 'from pathlib import Path; import sys; path = Path(sys.argv[1]); path.write_text(path.read_text().replace(sys.argv[2], sys.argv[3]))' "$rendered_env" "$prior_controller_image" "$FAKE_CONTROLLER_IMAGE"
same_controller_output=$tmp/same-controller-build.out
export FAKE_COMPOSE_LOG=$tmp/same-controller-build-compose.log
: >"$FAKE_COMPOSE_LOG"
export FAKE_FAIL_BUILD=1
if "$installer" --upgrade "${base_args[@]}" --ref "$ref_two" >"$same_controller_output" 2>&1; then
  fail 'same-controller-tag build failure unexpectedly succeeded'
fi
unset FAKE_FAIL_BUILD
stop_line=$(grep -n -m1 '^stop|' "$FAKE_COMPOSE_LOG" | cut -d: -f1 || true)
build_line=$(grep -n -m1 '^build|' "$FAKE_COMPOSE_LOG" | cut -d: -f1 || true)
[[ -n "$stop_line" && -n "$build_line" && "$stop_line" -lt "$build_line" ]] || fail 'installed controller tag was rebuilt before checkpoint and stop'
grep -Fq 'CHECKPOINT_CREATED' "$same_controller_output" || fail 'same controller tag was rebuilt without a checkpoint'
grep -Fq 'ROLLBACK_RESTORED' "$same_controller_output" || fail 'same-controller-tag build failure did not restore the checkpoint'
grep -Fxq "CI_FLEET_RUNNER_IMAGE=$prior_runner_image" "$rendered_env" || fail 'same-controller-tag build failure changed installed runner state'
grep -Fxq "CI_FLEET_CONTROLLER_IMAGE=$FAKE_CONTROLLER_IMAGE" "$rendered_env" || fail 'same-controller-tag build failure changed installed controller state'
[[ $(readlink -f "$root/opt/ci-fleet/manager/current") == "$prior_manager" ]] || fail 'same-controller-tag build failure changed the installed manager'
unset FAKE_COMPOSE_LOG
python3 -c 'from pathlib import Path; import sys; path = Path(sys.argv[1]); path.write_text(path.read_text().replace(sys.argv[2], "CI_FLEET_RUNNER_IMAGE="))' "$FAKE_CONTROLLER_ENV_FILE" "CI_FLEET_RUNNER_IMAGE=$prior_runner_image"
empty_live_image_output=$tmp/empty-live-image-build.out
export FAKE_COMPOSE_LOG=$tmp/empty-live-image-build-compose.log
: >"$FAKE_COMPOSE_LOG"
export FAKE_FAIL_BUILD=1
if "$installer" --upgrade "${base_args[@]}" --ref "$ref_two" >"$empty_live_image_output" 2>&1; then
  fail 'empty-live-image candidate build failure unexpectedly succeeded'
fi
unset FAKE_FAIL_BUILD
stop_line=$(grep -n -m1 '^stop|' "$FAKE_COMPOSE_LOG" | cut -d: -f1 || true)
build_line=$(grep -n -m1 '^build|' "$FAKE_COMPOSE_LOG" | cut -d: -f1 || true)
[[ -n "$stop_line" && -n "$build_line" && "$stop_line" -lt "$build_line" ]] || fail 'empty live runner image was accepted before drain'
grep -Fq 'DRAIN_OK managed_runners=0' "$empty_live_image_output" || fail 'empty live runner image build did not wait for drain'
grep -Fq 'ROLLBACK_RESTORED' "$empty_live_image_output" || fail 'empty live runner image build failure did not restore the checkpoint'
[[ -f "$FAKE_DOCKER_STATE" ]] || fail 'empty live runner image build failure stopped the installed controller'
grep -Fxq "CI_FLEET_RUNNER_IMAGE=$prior_runner_image" "$rendered_env" || fail 'empty live runner image build failure changed installed environment'
grep -Fxq "CI_FLEET_RUNNER_IMAGE=$prior_runner_image" "$FAKE_CONTROLLER_ENV_FILE" || fail 'empty live runner image build failure did not restore the installed controller'
grep -Fq 'CI_FLEET_MAX_RUNNERS=1' "$rendered_env" || fail 'empty live runner image build failure changed installed state'
[[ $(readlink -f "$root/opt/ci-fleet/manager/current") == "$prior_manager" ]] || fail 'empty live runner image build failure changed the installed manager'
unset FAKE_COMPOSE_LOG
[[ ${CI_FLEET_TEST_STOP_AFTER_EMPTY_LIVE_IMAGE_BUILD:-0} != 1 ]] || { printf 'EMPTY_LIVE_IMAGE_BUILD_REGRESSION_OK\n'; exit 0; }
python3 -c 'from pathlib import Path; import sys; path = Path(sys.argv[1]); path.write_text(path.read_text().replace(sys.argv[2], sys.argv[3]))' "$FAKE_CONTROLLER_ENV_FILE" "$prior_runner_image" "$FAKE_RUNNER_IMAGE"
live_drift_build_output=$tmp/live-drift-build.out
export FAKE_COMPOSE_LOG=$tmp/live-drift-build-compose.log
: >"$FAKE_COMPOSE_LOG"
export FAKE_FAIL_BUILD=1
if "$installer" --upgrade "${base_args[@]}" --ref "$ref_two" >"$live_drift_build_output" 2>&1; then
  fail 'live-drift candidate build failure unexpectedly succeeded'
fi
unset FAKE_FAIL_BUILD
stop_line=$(grep -n -m1 '^stop|' "$FAKE_COMPOSE_LOG" | cut -d: -f1 || true)
build_line=$(grep -n -m1 '^build|' "$FAKE_COMPOSE_LOG" | cut -d: -f1 || true)
[[ -n "$stop_line" && -n "$build_line" && "$stop_line" -lt "$build_line" ]] || fail 'live drift runner tag was built before drain'
grep -Fq 'DRAIN_OK managed_runners=0' "$live_drift_build_output" || fail 'live drift runner tag build did not wait for drain'
grep -Fq 'ROLLBACK_RESTORED' "$live_drift_build_output" || fail 'live drift runner tag build failure did not restore the checkpoint'
unset FAKE_COMPOSE_LOG
[[ ${CI_FLEET_TEST_STOP_AFTER_LIVE_DRIFT_BUILD:-0} != 1 ]] || { printf 'LIVE_DRIFT_BUILD_REGRESSION_OK\n'; exit 0; }
python3 -c 'from pathlib import Path; import sys; path = Path(sys.argv[1]); path.write_text(path.read_text().replace(sys.argv[2], sys.argv[3]))' "$rendered_env" "$FAKE_CONTROLLER_IMAGE" "$prior_controller_image"
postbuild_capacity_output=$tmp/postbuild-capacity.out
export FAKE_DISK_USED_PERCENT_FILE=$tmp/docker-disk-used-percent
export FAKE_DISK_USED_PERCENT_AFTER_BUILD=80
printf '79\n' >"$FAKE_DISK_USED_PERCENT_FILE"
if "$installer" --upgrade "${base_args[@]}" --ref "$ref_two" >"$postbuild_capacity_output" 2>&1; then
  fail 'post-build Docker capacity failure unexpectedly succeeded'
fi
unset FAKE_DISK_USED_PERCENT_AFTER_BUILD FAKE_DISK_USED_PERCENT_FILE
grep -Fq 'Docker filesystem' "$postbuild_capacity_output" || fail 'post-build Docker capacity failure was not reported'
if grep -Eq 'CHECKPOINT_CREATED|DRAIN_READY|ROLLBACK_' "$postbuild_capacity_output"; then fail 'post-build Docker capacity failure entered the transaction'; fi
[[ -f "$FAKE_DOCKER_STATE" ]] || fail 'post-build Docker capacity failure stopped the installed controller'
grep -Fq 'CI_FLEET_MAX_RUNNERS=1' "$rendered_env" || fail 'post-build Docker capacity failure changed installed state'
[[ ${CI_FLEET_TEST_STOP_AFTER_POSTBUILD_CAPACITY:-0} != 1 ]] || { printf 'POSTBUILD_CAPACITY_REGRESSION_OK\n'; exit 0; }
python3 -c 'from pathlib import Path; import sys; path = Path(sys.argv[1]); path.write_text(path.read_text().replace(sys.argv[2], sys.argv[3]))' "$rendered_env" "$prior_controller_image" "$FAKE_CONTROLLER_IMAGE"
for environment in "$rendered_env" "$FAKE_CONTROLLER_ENV_FILE"; do
  python3 -c 'from pathlib import Path; import sys; path = Path(sys.argv[1]); path.write_text(path.read_text().replace(sys.argv[2], sys.argv[3]))' "$environment" "$prior_runner_image" "$FAKE_RUNNER_IMAGE"
done
config_failure_output=$tmp/config-failure.out
export FAKE_FAIL_CONFIG=1
if "$installer" --upgrade "${base_args[@]}" --ref "$ref_two" >"$config_failure_output" 2>&1; then
  fail 'candidate Compose validation failure unexpectedly succeeded'
fi
unset FAKE_FAIL_CONFIG
if grep -Eq 'CHECKPOINT_CREATED|DRAIN_READY|ROLLBACK_' "$config_failure_output"; then fail 'candidate Compose validation failure entered the transaction'; fi
[[ -f "$FAKE_DOCKER_STATE" ]] || fail 'candidate Compose validation failure stopped the installed controller'
invalid_image_output=$tmp/invalid-image-id.out
export FAKE_COMPOSE_LOG=$tmp/invalid-image-id-compose.log
: >"$FAKE_COMPOSE_LOG"
prior_runner_image_id=$(<"$FAKE_RUNNER_IMAGE_ID_STATE")
printf 'not-a-sha256-image-id\n' >"$FAKE_RUNNER_IMAGE_ID_STATE"
if "$installer" --upgrade "${base_args[@]}" --ref "$ref_two" >"$invalid_image_output" 2>&1; then
  fail 'invalid installed image ID unexpectedly reached the transaction'
fi
printf '%s\n' "$prior_runner_image_id" >"$FAKE_RUNNER_IMAGE_ID_STATE"
grep -Fq 'installed runner image ID is invalid' "$invalid_image_output" || fail 'invalid installed image ID was not rejected'
if grep -Eq 'CHECKPOINT_CREATED|DRAIN_READY|ROLLBACK_' "$invalid_image_output" || grep -Eq '^(stop|build)\|' "$FAKE_COMPOSE_LOG"; then fail 'invalid installed image ID caused a transaction side effect'; fi
[[ -f "$FAKE_DOCKER_STATE" ]] || fail 'invalid installed image ID stopped the installed controller'
grep -Fq 'CI_FLEET_MAX_RUNNERS=1' "$rendered_env" || fail 'invalid installed image ID changed installed state'
[[ $(readlink -f "$root/opt/ci-fleet/manager/current") == "$prior_manager" ]] || fail 'invalid installed image ID changed the installed manager'
missing_tag_output=$tmp/missing-tag-build.out
export FAKE_COMPOSE_LOG=$tmp/missing-tag-build-compose.log
: >"$FAKE_COMPOSE_LOG"
prior_runner_image_id=$(<"$FAKE_RUNNER_IMAGE_ID_STATE")
prior_controller_image_id=$(<"$FAKE_CONTROLLER_IMAGE_ID_STATE")
rm -f "$FAKE_RUNNER_IMAGE_STATE" "$FAKE_RUNNER_IMAGE_ID_STATE"
export FAKE_PARTIAL_BUILD_FAIL=1
if "$installer" --upgrade "${base_args[@]}" --ref "$ref_two" >"$missing_tag_output" 2>&1; then
  fail 'missing-tag candidate build failure unexpectedly succeeded'
fi
unset FAKE_PARTIAL_BUILD_FAIL
grep -Fq 'DRAIN_OK managed_runners=0' "$missing_tag_output" || fail "missing managed tag did not reach the deferred build failure: $(<"$missing_tag_output")"
grep -Fq 'ROLLBACK_RESTORED' "$missing_tag_output" || fail 'missing-tag build failure did not restore the checkpoint'
checkpoint_path=$(awk '$1 == "CHECKPOINT_CREATED" {sub(/^path=/, "", $2); value=$2} END {print value}' "$missing_tag_output")
grep -Fxq 'CI_FLEET_RUNNER_IMAGE_ID=absent' "$checkpoint_path/image-ids.env" || fail 'checkpoint did not record the missing runner tag'
[[ ! -e "$FAKE_RUNNER_IMAGE_STATE" && ! -e "$FAKE_RUNNER_IMAGE_ID_STATE" ]] || fail 'rollback retained a runner tag that was absent before repair'
[[ $(<"$FAKE_CONTROLLER_IMAGE_ID_STATE") == "$prior_controller_image_id" ]] || fail 'missing-tag rollback did not restore the prior controller image ID'
grep -Fxq "CI_FLEET_RUNNER_IMAGE=$FAKE_RUNNER_IMAGE" "$rendered_env" || fail 'missing-tag rollback changed the installed environment'
grep -Fq '"configured_max_runners": 1' "$install_state" || fail 'missing-tag rollback changed installed state'
[[ $(readlink -f "$root/opt/ci-fleet/manager/current") == "$prior_manager" ]] || fail 'missing-tag rollback changed the installed manager'
runner_rm_line=$(grep -n -m1 "^image-rm|$FAKE_RUNNER_IMAGE$" "$FAKE_COMPOSE_LOG" | cut -d: -f1 || true)
controller_tag_line=$(grep -n -m1 "^image-tag|$prior_controller_image_id|$FAKE_CONTROLLER_IMAGE$" "$FAKE_COMPOSE_LOG" | cut -d: -f1 || true)
rollback_up_line=$(grep -n '^up|' "$FAKE_COMPOSE_LOG" | tail -n1 | cut -d: -f1 || true)
[[ -n "$runner_rm_line" && -n "$controller_tag_line" && -n "$rollback_up_line" && "$runner_rm_line" -lt "$rollback_up_line" && "$controller_tag_line" -lt "$rollback_up_line" ]] || fail 'missing-tag rollback restarted the controller before restoring both image states'
daemon_failure_output=$tmp/missing-tag-daemon-failure.out
export FAKE_DOCKER_INFO_FAIL_AFTER_IMAGE_INSPECT=$tmp/docker-info-fail-after-image-inspect
: >"$FAKE_COMPOSE_LOG"
if "$installer" --upgrade "${base_args[@]}" --ref "$ref_two" >"$daemon_failure_output" 2>&1; then
  fail 'missing-tag daemon failure unexpectedly succeeded'
fi
rm -f "$FAKE_DOCKER_INFO_FAIL_AFTER_IMAGE_INSPECT"
unset FAKE_DOCKER_INFO_FAIL_AFTER_IMAGE_INSPECT
grep -Fq 'Docker daemon is unavailable' "$daemon_failure_output" || fail 'image absence was accepted without confirming Docker availability'
if grep -Eq 'CHECKPOINT_CREATED|DRAIN_READY|ROLLBACK_' "$daemon_failure_output" || grep -Eq '^(stop|build)\|' "$FAKE_COMPOSE_LOG"; then fail 'Docker daemon failure entered the transaction'; fi
uninstall_output=$(expect_success "$installer" --uninstall)
grep -Fq 'UNINSTALL_OK' <<<"$uninstall_output" || fail 'uninstall rejected a missing managed image tag'
expect_success "$installer" --rollback >/dev/null
[[ ! -e "$FAKE_RUNNER_IMAGE_STATE" && ! -e "$FAKE_RUNNER_IMAGE_ID_STATE" && -f "$FAKE_DOCKER_STATE" ]] || fail 'uninstall rollback did not restore the missing-tag installation'
printf '%s\n' "$engine_ref" >"$FAKE_RUNNER_IMAGE_STATE"
printf '%s\n' "$prior_runner_image_id" >"$FAKE_RUNNER_IMAGE_ID_STATE"
unset FAKE_COMPOSE_LOG
absent_controller_output=$tmp/absent-controller-timer-rollback.out
export FAKE_COMPOSE_LOG=$tmp/absent-controller-timer-rollback-compose.log
export FAKE_STOPPED_CONTROLLER_STATE=$tmp/stopped-controller
: >"$FAKE_COMPOSE_LOG"
prior_controller_image_id=sha256:4444444444444444444444444444444444444444444444444444444444444444
printf '%s\n' "$prior_controller_image_id" >>"$FAKE_AVAILABLE_IMAGE_IDS"
printf '%s\n' "$prior_controller_image_id" >"$FAKE_CONTROLLER_IMAGE_ID_STATE"
printf '%s\n' "$prior_controller_image_id" >"$FAKE_CONTROLLER_IMAGE_ID_FILE"
rm -f "$FAKE_CONTROLLER_IMAGE_STATE" "$FAKE_CONTROLLER_IMAGE_ID_STATE"
[[ $(<"$FAKE_CONTROLLER_IMAGE_ID_FILE") == "$prior_controller_image_id" ]] || fail 'untagged running controller lost its exact image ID'
export FAKE_FAIL_TIMER_ENABLE=1
if "$installer" --upgrade --config-repo "$config_repo" --config-identity fixture-org/fleet-config --controller example-ci-01 --ref "$ref_two" >"$absent_controller_output" 2>&1; then
  fail 'absent-controller timer activation failure unexpectedly succeeded'
fi
unset FAKE_FAIL_TIMER_ENABLE
grep -Fq 'ROLLBACK_RESTORED' "$absent_controller_output" || fail "absent-controller timer failure did not restore the checkpoint: $(<"$absent_controller_output"); compose log: $(<"$FAKE_COMPOSE_LOG")"
checkpoint_path=$(awk '$1 == "CHECKPOINT_CREATED" {sub(/^path=/, "", $2); value=$2} END {print value}' "$absent_controller_output")
grep -Fxq 'CI_FLEET_CONTROLLER_IMAGE_ID=absent' "$checkpoint_path/image-ids.env" || fail 'checkpoint did not preserve the absent controller tag'
grep -Fxq "CI_FLEET_CONTROLLER_LIVE_IMAGE_ID=$prior_controller_image_id" "$checkpoint_path/image-ids.env" || fail 'checkpoint omitted the untagged live controller image ID'
[[ ! -e "$FAKE_CONTROLLER_IMAGE_STATE" && ! -e "$FAKE_CONTROLLER_IMAGE_ID_STATE" ]] || fail 'timer-failure rollback retained a controller tag that was previously absent'
[[ -f "$FAKE_DOCKER_STATE" && ! -f "$FAKE_STOPPED_CONTROLLER_STATE" ]] || fail 'timer-failure rollback did not restart the prior controller'
[[ $(<"$FAKE_CONTROLLER_IMAGE_ID_FILE") == "$prior_controller_image_id" ]] || fail 'timer-failure rollback did not recreate the exact prior controller image'
candidate_up_line=$(grep -n -m1 '^up|' "$FAKE_COMPOSE_LOG" | cut -d: -f1 || true)
rollback_stop_line=$(grep -n '^stop|' "$FAKE_COMPOSE_LOG" | tail -n1 | cut -d: -f1 || true)
candidate_rm_line=$(grep -n -m1 '^rm|' "$FAKE_COMPOSE_LOG" | cut -d: -f1 || true)
controller_tag_line=$(grep -n -m1 "^image-tag|$prior_controller_image_id|$FAKE_CONTROLLER_IMAGE$" "$FAKE_COMPOSE_LOG" | cut -d: -f1 || true)
rollback_up_line=$(grep -n '^up|' "$FAKE_COMPOSE_LOG" | tail -n1 | cut -d: -f1 || true)
controller_rm_line=$(grep -n "^image-rm|$FAKE_CONTROLLER_IMAGE$" "$FAKE_COMPOSE_LOG" | tail -n1 | cut -d: -f1 || true)
[[ -n "$candidate_up_line" && -n "$rollback_stop_line" && -n "$candidate_rm_line" && -n "$controller_tag_line" && -n "$rollback_up_line" && -n "$controller_rm_line" \
  && "$candidate_up_line" -lt "$rollback_stop_line" && "$rollback_stop_line" -lt "$candidate_rm_line" && "$candidate_rm_line" -lt "$controller_tag_line" \
  && "$controller_tag_line" -lt "$rollback_up_line" && "$rollback_up_line" -lt "$controller_rm_line" ]] \
  || fail 'timer-failure rollback did not remove the candidate, recreate the prior controller, and remove its temporary tag'
printf '%s\n' "$engine_ref" >"$FAKE_CONTROLLER_IMAGE_STATE"
printf '%s\n' "$prior_controller_image_id" >"$FAKE_CONTROLLER_IMAGE_ID_STATE"
printf '%s\n' "$prior_controller_image_id" >"$FAKE_CONTROLLER_IMAGE_ID_FILE"
printf '%s\n' "$engine_ref" >"$FAKE_CONTROLLER_PROVENANCE_FILE"
unset FAKE_STOPPED_CONTROLLER_STATE FAKE_COMPOSE_LOG
[[ ${CI_FLEET_TEST_STOP_AFTER_ABSENT_CONTROLLER_ROLLBACK:-0} != 1 ]] || { printf 'ABSENT_CONTROLLER_ROLLBACK_REGRESSION_OK\n'; exit 0; }
restartable_tag_build_output=$tmp/restartable-tag-build.out
export FAKE_COMPOSE_LOG=$tmp/restartable-tag-build-compose.log
: >"$FAKE_COMPOSE_LOG"
printf 'exited\n' >"$FAKE_CONTROLLER_STATUS_FILE"
prior_runner_image_id=$(<"$FAKE_RUNNER_IMAGE_ID_STATE")
prior_controller_image_id=$(<"$FAKE_CONTROLLER_IMAGE_ID_STATE")
export FAKE_PARTIAL_BUILD_FAIL=1
if "$installer" --upgrade "${base_args[@]}" --ref "$ref_two" >"$restartable_tag_build_output" 2>&1; then
  fail 'restartable-tag candidate build failure unexpectedly succeeded'
fi
unset FAKE_PARTIAL_BUILD_FAIL
stop_line=$(grep -n -m1 '^stop|' "$FAKE_COMPOSE_LOG" | cut -d: -f1 || true)
build_line=$(grep -n -m1 '^build|' "$FAKE_COMPOSE_LOG" | cut -d: -f1 || true)
[[ -n "$stop_line" && -n "$build_line" && "$stop_line" -lt "$build_line" ]] || fail 'restartable controller runner tag was built before stop'
grep -Fq 'DRAIN_OK managed_runners=0' "$restartable_tag_build_output" || fail 'restartable controller runner tag build did not wait for drain'
grep -Fq 'ROLLBACK_RESTORED' "$restartable_tag_build_output" || fail 'restartable controller runner tag build failure did not restore the checkpoint'
[[ -f "$FAKE_DOCKER_STATE" && ! -f "$FAKE_CONTROLLER_STATUS_FILE" ]] || fail 'restartable controller runner tag build failure did not restore the installed controller'
grep -Fxq "CI_FLEET_RUNNER_IMAGE=$FAKE_RUNNER_IMAGE" "$rendered_env" || fail 'restartable controller runner tag build failure changed installed environment'
grep -Fxq "CI_FLEET_RUNNER_IMAGE=$FAKE_RUNNER_IMAGE" "$FAKE_CONTROLLER_ENV_FILE" || fail 'restartable controller runner tag build failure did not restore the installed controller environment'
grep -Fq 'CI_FLEET_MAX_RUNNERS=1' "$rendered_env" || fail 'restartable controller runner tag build failure changed installed state'
[[ $(readlink -f "$root/opt/ci-fleet/manager/current") == "$prior_manager" ]] || fail 'restartable controller runner tag build failure changed the installed manager'
[[ $(<"$FAKE_RUNNER_IMAGE_ID_STATE") == "$prior_runner_image_id" ]] || fail 'rollback reported restored but left the runner tag on the partial build image'
[[ $(<"$FAKE_CONTROLLER_IMAGE_ID_STATE") == "$prior_controller_image_id" ]] || fail 'rollback did not restore the prior controller tag image ID'
checkpoint_path=$(awk '$1 == "CHECKPOINT_CREATED" {sub(/^path=/, "", $2); value=$2} END {print value}' "$restartable_tag_build_output")
image_ids_file=$checkpoint_path/image-ids.env
format_marker=$checkpoint_path/format-version
[[ -d "$checkpoint_path" && $(stat -c %a "$checkpoint_path") == 700 ]] || fail 'image rollback checkpoint is not a mode-0700 directory'
[[ -f "$format_marker" && ! -L "$format_marker" && $(stat -c '%u:%a:%s' "$format_marker") == "$(id -u):600:2" && $(<"$format_marker") == 2 ]] || fail 'managed checkpoint lacks the exact root-owned mode-0600 format marker'
[[ -f "$image_ids_file" && $(stat -c %a "$image_ids_file") == 600 && $(wc -l <"$image_ids_file") == 2 ]] || fail 'checkpoint image map is not exactly one mode-0600 two-ID file'
grep -Fxq "CI_FLEET_RUNNER_IMAGE_ID=$prior_runner_image_id" "$image_ids_file" || fail 'checkpoint omitted the prior runner image ID'
grep -Fxq "CI_FLEET_CONTROLLER_IMAGE_ID=$prior_controller_image_id" "$image_ids_file" || fail 'checkpoint omitted the prior controller image ID'
runner_tag_line=$(grep -n -m1 "^image-tag|$prior_runner_image_id|$FAKE_RUNNER_IMAGE$" "$FAKE_COMPOSE_LOG" | cut -d: -f1 || true)
controller_tag_line=$(grep -n -m1 "^image-tag|$prior_controller_image_id|$FAKE_CONTROLLER_IMAGE$" "$FAKE_COMPOSE_LOG" | cut -d: -f1 || true)
rollback_up_line=$(grep -n '^up|' "$FAKE_COMPOSE_LOG" | tail -n1 | cut -d: -f1 || true)
[[ -n "$runner_tag_line" && -n "$controller_tag_line" && -n "$rollback_up_line" && "$runner_tag_line" -lt "$rollback_up_line" && "$controller_tag_line" -lt "$rollback_up_line" ]] || fail 'rollback started the prior controller before restoring both image tags'
[[ ${CI_FLEET_TEST_STOP_AFTER_RESTARTABLE_TAG_BUILD:-0} != 1 ]] || { printf 'RESTARTABLE_TAG_BUILD_REGRESSION_OK\n'; exit 0; }
printf 'CI_FLEET_RUNNER_IMAGE_ID=invalid\nCI_FLEET_CONTROLLER_IMAGE_ID=%s\n' "$prior_controller_image_id" >"$image_ids_file"
: >"$FAKE_COMPOSE_LOG"
rm -f "$FAKE_DOCKER_STATE" "$FAKE_CONTROLLER_STATUS_FILE" "$FAKE_CONTROLLER_PROVENANCE_FILE" "$FAKE_CONTROLLER_IMAGE_ID_FILE" "$FAKE_CONTROLLER_ENV_FILE"
expect_failure 'checkpoint image mappings are invalid' "$installer" --rollback
if grep -q '^up|' "$FAKE_COMPOSE_LOG"; then fail 'malformed checkpoint image ID restarted the prior controller'; fi
[[ ! -f "$FAKE_DOCKER_STATE" ]] || fail 'malformed checkpoint image ID restored the prior controller'
printf 'CI_FLEET_RUNNER_IMAGE_ID=%s\nCI_FLEET_CONTROLLER_IMAGE_ID=%s\n' "$prior_runner_image_id" "$prior_controller_image_id" >"$image_ids_file"
expect_success "$installer" --rollback >/dev/null
[[ -f "$FAKE_DOCKER_STATE" ]] || fail 'repaired checkpoint did not restore the prior controller'
legacy_checkpoint=$root/var/lib/ci-fleet/checkpoints/legacy-checkpoint
cp -a "$checkpoint_path" "$legacy_checkpoint"
rm -f "$legacy_checkpoint/format-version" "$legacy_checkpoint/image-ids.env"
touch "$legacy_checkpoint/.complete"
: >"$FAKE_COMPOSE_LOG"
legacy_output=$tmp/legacy-checkpoint.out
if ! "$installer" --rollback >"$legacy_output" 2>&1; then
  fail "legacy checkpoint was rejected: $(<"$legacy_output")"
fi
grep -Fq 'ROLLBACK_LEGACY_IMAGE_STATE_UNVERIFIED' "$legacy_output" || fail 'legacy rollback omitted its image-state warning'
if grep -Eq '^image-(tag|rm)\|' "$FAKE_COMPOSE_LOG"; then fail 'legacy rollback claimed or changed image identity'; fi
printf '3\n' >"$legacy_checkpoint/format-version"
chmod 0600 "$legacy_checkpoint/format-version"
: >"$FAKE_COMPOSE_LOG"
expect_failure 'checkpoint format marker is invalid' "$installer" --rollback
if [[ ! -f "$FAKE_DOCKER_STATE" ]] || grep -Eq '^(stop|up|image-(tag|rm))\|' "$FAKE_COMPOSE_LOG"; then fail 'unknown checkpoint version caused rollback side effects'; fi
printf '2\n' >"$legacy_checkpoint/format-version"
: >"$FAKE_COMPOSE_LOG"
expect_failure 'checkpoint image mappings are invalid' "$installer" --rollback
if [[ ! -f "$FAKE_DOCKER_STATE" ]] || grep -Eq '^(stop|up|image-(tag|rm))\|' "$FAKE_COMPOSE_LOG"; then fail 'version-2 checkpoint without image state caused rollback side effects'; fi
rm -rf "$legacy_checkpoint"
unset FAKE_COMPOSE_LOG
[[ ${CI_FLEET_TEST_STOP_AFTER_CHECKPOINT_COMPAT:-0} != 1 ]] || { printf 'CHECKPOINT_COMPAT_REGRESSIONS_OK\n'; exit 0; }
[[ ${CI_FLEET_TEST_STOP_AFTER_IMAGE_ROLLBACK:-0} != 1 ]] || { printf 'IMAGE_ROLLBACK_REGRESSION_OK\n'; exit 0; }
terminate_upgrade() {
  local output=$1 marker=$2 second_term_marker=${3:-} pid status=0 attempt
  export CI_FLEET_TEST_PAUSE_AFTER_DRAIN_FILE=$marker
  if [[ -n "$second_term_marker" ]]; then
    : >"$second_term_marker"
    export FAKE_DELAY_UP_ONCE=$second_term_marker
  fi
  "$installer" --upgrade "${base_args[@]}" --ref "$ref_two" >"$output" 2>&1 &
  pid=$!
  for ((attempt = 0; attempt < 200; attempt++)); do
    [[ ! -f "$marker" ]] || break
    sleep 0.05
  done
  if [[ ! -f "$marker" ]]; then
    kill -TERM "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    fail 'TERM regression did not reach the drained transaction'
  fi
  kill -TERM "$pid"
  if [[ -n "$second_term_marker" ]]; then
    for ((attempt = 0; attempt < 200; attempt++)); do
      [[ ! -f "${second_term_marker}.entered" ]] || break
      sleep 0.05
    done
    [[ -f "${second_term_marker}.entered" ]] || fail 'TERM regression did not enter checkpoint restoration'
    kill -TERM "$pid"
  fi
  if wait "$pid"; then status=0; else status=$?; fi
  unset CI_FLEET_TEST_PAUSE_AFTER_DRAIN_FILE FAKE_DELAY_UP_ONCE
  [[ "$status" == 143 ]] || fail "TERM regression changed the signal exit status: $status"
}
paused_term_output=$tmp/paused-term-rollback.out
export FAKE_RUNNER_STATE=$tmp/paused-term-managed-runner
export FAKE_KEEP_RUNNER_ON_PAUSE=1
: >"$FAKE_RUNNER_STATE"
rm -f "$FAKE_PAUSED_STATE"
"$installer" --upgrade "${base_args[@]}" --ref "$ref_two" >"$paused_term_output" 2>&1 &
paused_term_pid=$!
for ((attempt = 0; attempt < 200; attempt++)); do
  [[ ! -f "$FAKE_PAUSED_STATE" ]] || break
  sleep 0.05
done
if [[ ! -f "$FAKE_PAUSED_STATE" ]]; then
  kill -TERM "$paused_term_pid" 2>/dev/null || true
  wait "$paused_term_pid" 2>/dev/null || true
  fail 'TERM regression did not reach the paused drain phase'
fi
if grep -Fq 'DRAIN_OK managed_runners=0' "$paused_term_output"; then fail 'TERM regression passed the drain phase before signaling'; fi
kill -TERM "$paused_term_pid"
rm -f "$FAKE_RUNNER_STATE"
if wait "$paused_term_pid"; then paused_term_status=0; else paused_term_status=$?; fi
unset FAKE_KEEP_RUNNER_ON_PAUSE FAKE_RUNNER_STATE
[[ "$paused_term_status" == 143 ]] || fail "paused TERM regression changed the signal exit status: $paused_term_status"
grep -Fq 'ROLLBACK_RESTORED' "$paused_term_output" || fail "paused TERM did not report checkpoint restoration: $(<"$paused_term_output")"
[[ ! -f "$FAKE_PAUSED_STATE" && -f "$FAKE_DOCKER_STATE" ]] || fail 'TERM before DRAIN_OK did not restore the active controller'
grep -Fq 'CI_FLEET_MAX_RUNNERS=1' "$rendered_env" || fail 'TERM before DRAIN_OK did not restore installed state'
[[ $(readlink -f "$root/opt/ci-fleet/manager/current") == "$prior_manager" ]] || fail 'TERM before DRAIN_OK did not restore the prior manager release'
[[ ${CI_FLEET_TEST_STOP_AFTER_PAUSED_TERM:-0} != 1 ]] || { printf 'PAUSED_TERM_REGRESSION_OK\n'; exit 0; }
term_output=$tmp/term-rollback.out
terminate_upgrade "$term_output" "$tmp/term-pause" "$tmp/term-rollback-up"
grep -Fq 'ROLLBACK_RESTORED' "$term_output" || fail "TERM did not report checkpoint restoration: $(<"$term_output")"
[[ ! -f "$FAKE_PAUSED_STATE" && -f "$FAKE_DOCKER_STATE" ]] || fail 'TERM did not restore the active controller'
grep -Fq 'CI_FLEET_MAX_RUNNERS=1' "$rendered_env" || fail 'TERM did not restore installed state'
term_failure_output=$tmp/term-rollback-failure.out
export FAKE_FAIL_UP_ONCE=$tmp/term-rollback-fail-up
: >"$FAKE_FAIL_UP_ONCE"
terminate_upgrade "$term_failure_output" "$tmp/term-failure-pause"
unset FAKE_FAIL_UP_ONCE
grep -Fq 'ROLLBACK_FAILED' "$term_failure_output" || fail 'TERM rollback failure was not reported'
expect_success "$installer" --rollback >/dev/null
[[ -f "$FAKE_DOCKER_STATE" ]] || fail 'explicit rollback did not recover after TERM rollback failure'
[[ ${CI_FLEET_TEST_STOP_AFTER_TERM_ROLLBACK:-0} != 1 ]] || { printf 'TERM_ROLLBACK_REGRESSION_OK\n'; exit 0; }
managed_preflight_output=$tmp/managed-preflight.out
export FAKE_ACTIVE_MANAGED_STATE=$tmp/active-managed-after-drain
export FAKE_ACTIVE_MANAGED_AFTER_STOP=$FAKE_ACTIVE_MANAGED_STATE
if "$installer" --upgrade "${base_args[@]}" --ref "$ref_two" >"$managed_preflight_output" 2>&1; then
  fail 'managed candidate preflight skipped an active managed container after drain'
fi
unset FAKE_ACTIVE_MANAGED_AFTER_STOP FAKE_ACTIVE_MANAGED_STATE
grep -Fq 'DRAIN_OK managed_runners=0' "$managed_preflight_output" || fail 'managed candidate preflight ran before drain completed'
grep -Fq 'managed containers already active for this instance' "$managed_preflight_output" || fail 'managed candidate preflight did not check active managed containers'
grep -Fq 'ROLLBACK_RESTORED' "$managed_preflight_output" || fail 'managed candidate preflight failure did not restore the checkpoint'
rm -f "$tmp/active-managed-after-drain"
[[ ${CI_FLEET_TEST_STOP_AFTER_MANAGED_PREFLIGHT:-0} != 1 ]] || { printf 'MANAGED_PREFLIGHT_REGRESSION_OK\n'; exit 0; }
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
[[ ! -f "$FAKE_RUNNER_STATE" ]] || fail 'upgrade did not drain the active runner before activation'
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
expect_success "$installer" --upgrade "${base_args[@]}" --ref "$ref_three" >/dev/null
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
mkdir -p "$adopt_root/etc/ci-fleet/secrets" "$adopt_root/etc/ssl/certs" "$adopt_root/var/run" "$adopt_root/opt/ci-fleet/deploy" "$adopt_root/opt/ci-fleet/scripts"
printf 'ID=debian\nVERSION_ID="12"\n' >"$adopt_root/etc/os-release"
printf 'fixture CA bundle\n' >"$adopt_root/etc/ssl/certs/ca-certificates.crt"
: >"$adopt_root/var/run/docker.sock"
adopt_pem=$adopt_root/etc/ci-fleet/secrets/github-app.pem
printf 'fixture only\n' >"$adopt_pem"
chmod 600 "$adopt_pem"
cp "$repo_root/deploy/compose.yaml" "$adopt_root/opt/ci-fleet/deploy/compose.yaml"
cp "$repo_root/scripts/healthcheck.sh" "$adopt_root/opt/ci-fleet/scripts/healthcheck.sh"
cp "$repo_root/scripts/health.py" "$adopt_root/opt/ci-fleet/scripts/health.py"
cp "$repo_root/scripts/status_auth.py" "$adopt_root/opt/ci-fleet/scripts/status_auth.py"
cp "$repo_root/scripts/cleanup.sh" "$adopt_root/opt/ci-fleet/scripts/cleanup.sh"
chmod 0755 "$adopt_root/opt/ci-fleet/scripts/healthcheck.sh" "$adopt_root/opt/ci-fleet/scripts/cleanup.sh"
printf '%s\n' \
  'CI_FLEET_GITHUB_APP_CLIENT_ID=Iv1.EXAMPLE' \
  'CI_FLEET_GITHUB_APP_INSTALLATION_ID=123456' \
  "CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE=$adopt_pem" \
  'CI_FLEET_RUNNER_TTL=6h' \
  'CI_FLEET_CONTROLLER_STATE=active' \
  "CI_FLEET_RUNNER_IMAGE=$FAKE_RUNNER_IMAGE" \
  "CI_FLEET_CONTROLLER_IMAGE=$FAKE_CONTROLLER_IMAGE" \
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
legacy_disabled_ref=$(write_config active 1 1 "$legacy_engine_ref" false omit)
expect_failure 'selected engine does not support status reporting configuration' "$installer" --upgrade "${base_args[@]}" --ref "$legacy_disabled_ref"
legacy_required_ref=$(write_config active 1 1 "$legacy_engine_ref" true omit)
expect_failure 'selected engine does not advertise required status reporting' "$installer" --upgrade "${base_args[@]}" --ref "$legacy_required_ref"
legacy_ref=$(write_config active 1 1 "$legacy_engine_ref" omit omit)
export FAKE_PREVIOUS_RUNNER_IMAGE=$FAKE_RUNNER_IMAGE
export FAKE_PREVIOUS_CONTROLLER_IMAGE=$FAKE_CONTROLLER_IMAGE
export FAKE_ENGINE_REF=$legacy_engine_ref
export FAKE_RUNNER_IMAGE=ci-fleet-runner:${legacy_engine_ref:0:12}
export FAKE_CONTROLLER_IMAGE=ci-fleet-controller:${legacy_engine_ref:0:12}
expect_success "$installer" --upgrade "${base_args[@]}" --ref "$legacy_ref" >/dev/null
[[ $(readlink -f "$adopt_root/opt/ci-fleet/current") == "$adopt_root/opt/ci-fleet/releases/$legacy_engine_ref" ]] || fail 'upgrade could not restore a pre-health-contract engine'

grep -Fq 'Issue #7' "$repo_root/docs/DESIGN-DECISIONS.md" || fail 'isolated proof approval is not recorded'
if grep -Fq '/etc/ci-fleet/ci-fleet.env.before-max2' "$repo_root/docs/CAPACITY-PROMOTION.md"; then fail 'capacity runbook still edits rendered host state'; fi
grep -Fq -- '--upgrade' "$repo_root/docs/CAPACITY-PROMOTION.md" || fail 'capacity runbook does not apply reviewed desired state through the installer'

printf 'INSTALLER_TESTS_OK\n'
