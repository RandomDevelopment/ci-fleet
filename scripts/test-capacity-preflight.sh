#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin"

cat >"$tmp/bin/docker" <<'EOF'
#!/usr/bin/env bash
set -u
case "${1:-}" in
  info)
    if [[ "$*" == *'.DockerRootDir'* ]]; then printf '/var/lib/docker\n'; fi
    exit 0
    ;;
  compose)
    [[ "${2:-}" == version ]] && exit 0
    ;;
  inspect)
    args="$*"
    if [[ "$args" == *'.State.Status'* ]]; then
      printf '%s\n' "${FAKE_CONTROLLER_STATE:-running}"
    elif [[ "$args" == *'.State.OOMKilled'* ]]; then
      printf '%s\n' "${FAKE_CONTROLLER_OOM:-false}"
    elif [[ "$args" == *'.Config.Env'* ]]; then
      printf '%s\n' \
        "CI_FLEET_GITHUB_URL=${CI_FLEET_GITHUB_URL}" \
        "CI_FLEET_SCALE_SET_NAME=${CI_FLEET_SCALE_SET_NAME}" \
        "CI_FLEET_LABELS=${CI_FLEET_LABELS}" \
        "CI_FLEET_RUNNER_GROUP=${CI_FLEET_RUNNER_GROUP}" \
        "CI_FLEET_INSTANCE=${CI_FLEET_INSTANCE}" \
        "CI_FLEET_MIN_RUNNERS=${FAKE_EFFECTIVE_MIN:-${CI_FLEET_MIN_RUNNERS}}" \
        "CI_FLEET_MAX_RUNNERS=${FAKE_EFFECTIVE_MAX:-${CI_FLEET_MAX_RUNNERS}}" \
        "CI_FLEET_RUNNER_CPUS=${CI_FLEET_RUNNER_CPUS}" \
        "CI_FLEET_RUNNER_MEMORY_MIB=${CI_FLEET_RUNNER_MEMORY_MIB}" \
        "CI_FLEET_DOCKER_GID=${CI_FLEET_DOCKER_GID}" \
        "UNRELATED_SECRET=${FAKE_SECRET_VALUE:-CAPACITY_TEST_SECRET_SHOULD_NOT_PRINT}"
    elif [[ "$args" == *'com.docker.compose.project'* ]]; then
      printf '%s\n' "${FAKE_COMPOSE_CONTAINER_PROJECT:-${FAKE_COMPOSE_PROJECT:-ci-fleet}}"
    elif [[ "$args" == *'com.docker.compose.service'* ]]; then
      printf '%s\n' "${FAKE_COMPOSE_SERVICE:-controller}"
    elif [[ "$args" == *'{{.Name}}'* ]]; then
      printf '/%s\n' "${FAKE_COMPOSE_CONTAINER_NAME:-ci-fleet-controller-1}"
    else
      exit 1
    fi
    ;;
  ps)
    args="$*"
    if [[ "$args" == *'--format'* ]]; then
      printf '%s\n' "${CI_FLEET_CONTROLLER_CONTAINER:-ci-fleet-controller-1}"
      [[ -z "${FAKE_UNRELATED_CONTAINER:-}" ]] || printf '%s\n' "$FAKE_UNRELATED_CONTAINER"
    elif [[ "$args" == *'label=ci-fleet.repository'* ]]; then
      for ((i=0; i<${FAKE_ACTIVE_JOBS:-0}; i++)); do printf 'job-%s\n' "$i"; done
    elif [[ "$args" == *'label=com.docker.compose.project'* ]]; then
      for ((i=0; i<${FAKE_COMPOSE_CONTAINERS:-0}; i++)); do printf 'compose-container-%s\n' "$i"; done
    elif [[ "$args" == *'io.randomdevelopment.ci-fleet.managed=true'* ]]; then
      for ((i=0; i<${FAKE_MANAGED_RUNNERS:-0}; i++)); do printf 'runner-%s\n' "$i"; done
    fi
    ;;
  volume|network)
    kind=$1
    if [[ "${2:-}" == inspect ]]; then
      args="$*"
      if [[ "$args" == *'com.docker.compose.project'* ]]; then
        if [[ "$kind" == volume ]]; then
          printf '%s\n' "${FAKE_COMPOSE_VOLUME_PROJECT:-${FAKE_COMPOSE_PROJECT:-ci-fleet}}"
        else
          printf '%s\n' "${FAKE_COMPOSE_NETWORK_PROJECT:-${FAKE_COMPOSE_PROJECT:-ci-fleet}}"
        fi
      elif [[ "$args" == *'com.docker.compose.network'* ]]; then
        printf '%s\n' "${FAKE_COMPOSE_NETWORK_LABEL:-default}"
      elif [[ "$args" == *'{{.Name}}'* ]]; then
        printf '%s\n' "${FAKE_COMPOSE_NETWORK_NAME:-ci-fleet_default}"
      else
        exit 1
      fi
      exit 0
    fi
    [[ "${2:-}" == ls ]] || exit 1
    if [[ "$*" == *'label=com.docker.compose.project'* ]]; then
      if [[ "$kind" == volume ]]; then count=${FAKE_COMPOSE_VOLUMES:-0}; else count=${FAKE_COMPOSE_NETWORKS:-0}; fi
    elif [[ "$kind" == volume ]]; then
      count=${FAKE_JOB_VOLUMES:-0}
    else
      count=${FAKE_JOB_NETWORKS:-0}
    fi
    for ((i=0; i<count; i++)); do printf '%s-%s\n' "$kind" "$i"; done
    ;;
  *) exit 1 ;;
esac
EOF

cat >"$tmp/bin/stat" <<'EOF'
#!/usr/bin/env bash
case "${2:-}" in
  %g) printf '%s\n' "${CI_FLEET_DOCKER_GID:-999}" ;;
  %a) printf '600\n' ;;
  %u) printf '0\n' ;;
  *) exit 1 ;;
esac
EOF

cat >"$tmp/bin/df" <<'EOF'
#!/usr/bin/env bash
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n'
printf '/dev/test 100000 5000 95000 %s%% /var/lib/docker\n' "${FAKE_DISK_USED:-5}"
EOF

cat >"$tmp/bin/getconf" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "${FAKE_TOTAL_CPUS:-16}"
EOF

cat >"$tmp/bin/free" <<'EOF'
#!/usr/bin/env bash
total=${FAKE_TOTAL_MEMORY_MIB:-32768}
available=${FAKE_AVAILABLE_MEMORY_MIB:-30000}
printf '              total        used        free      shared  buff/cache   available\n'
printf 'Mem:          %s        1000        1000           0       1000       %s\n' "$total" "$available"
EOF

cat >"$tmp/bin/dmesg" <<'EOF'
#!/usr/bin/env bash
[[ "${FAKE_OOM_EVIDENCE:-0}" == 0 ]] || printf 'Out of memory: Killed process 123\n'
EOF
chmod 700 "$tmp/bin"/*

export PATH="$tmp/bin:$PATH"
export CI_FLEET_GITHUB_URL=https://github.com/EXAMPLE-ORG
export CI_FLEET_SCALE_SET_NAME=docker-ci-validation
export CI_FLEET_LABELS=docker-ci-experimental
export CI_FLEET_RUNNER_GROUP=trusted-private-ci-experimental
export CI_FLEET_INSTANCE=validation
export CI_FLEET_GITHUB_APP_CLIENT_ID=validation
export CI_FLEET_GITHUB_APP_INSTALLATION_ID=1
export CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE="$tmp/github-app.pem"
export CI_FLEET_DOCKER_GID=999
export CI_FLEET_MIN_RUNNERS=0
export CI_FLEET_MAX_RUNNERS=1
export CI_FLEET_RUNNER_CPUS=4
export CI_FLEET_RUNNER_MEMORY_MIB=8192
export CI_FLEET_CONTROLLER_CONTAINER=ci-fleet-controller-1
export CI_FLEET_DISK_WARN_PERCENT=80
printf 'fixture only\n' >"$CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE"
chmod 600 "$CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE"

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
reset_fixture() {
  export CI_FLEET_MIN_RUNNERS=0 CI_FLEET_MAX_RUNNERS=1
  export FAKE_EFFECTIVE_MIN=0 FAKE_EFFECTIVE_MAX=1
  export FAKE_TOTAL_CPUS=16 FAKE_TOTAL_MEMORY_MIB=32768 FAKE_AVAILABLE_MEMORY_MIB=30000 FAKE_DISK_USED=5
  export FAKE_MANAGED_RUNNERS=0 FAKE_ACTIVE_JOBS=0 FAKE_JOB_VOLUMES=0 FAKE_JOB_NETWORKS=0
  export FAKE_COMPOSE_CONTAINERS=1 FAKE_COMPOSE_VOLUMES=0 FAKE_COMPOSE_NETWORKS=1 FAKE_COMPOSE_PROJECT=ci-fleet
  export FAKE_COMPOSE_CONTAINER_PROJECT=ci-fleet FAKE_COMPOSE_VOLUME_PROJECT=ci-fleet FAKE_COMPOSE_NETWORK_PROJECT=ci-fleet
  export FAKE_COMPOSE_SERVICE=controller FAKE_COMPOSE_CONTAINER_NAME=ci-fleet-controller-1
  export FAKE_COMPOSE_NETWORK_LABEL=default FAKE_COMPOSE_NETWORK_NAME=ci-fleet_default
  export FAKE_CONTROLLER_STATE=running FAKE_CONTROLLER_OOM=false
  export FAKE_OOM_EVIDENCE=0 FAKE_UNRELATED_CONTAINER=
}

reset_fixture
pilot=$(expect_success "$repo_root/scripts/preflight.sh")
grep -Fq 'PREFLIGHT_OK warnings=0' <<<"$pilot" || fail 'pilot MAX=1 gate did not pass'
CI_FLEET_MAX_RUNNERS=2 expect_failure 'CI_FLEET_MAX_RUNNERS must be 1 for the pilot' "$repo_root/scripts/preflight.sh"

valid=$(expect_success "$repo_root/scripts/capacity-preflight.sh" --phase pre-change --target-max 2)
grep -Fq 'CAPACITY_PREFLIGHT_OK phase=pre-change target_max=2 configured_max=1 effective_max=1' <<<"$valid" || fail 'valid MAX=2 pre-change summary missing'
grep -Fq 'cpu_budget_millicpus=' <<<"$valid" || fail 'safe CPU budget missing'
grep -Fq 'memory_budget_mib=' <<<"$valid" || fail 'safe memory budget missing'

reset_fixture
FAKE_TOTAL_CPUS=10 expect_failure 'CPU capacity is insufficient' "$repo_root/scripts/capacity-preflight.sh" --phase pre-change --target-max 2
reset_fixture
FAKE_TOTAL_MEMORY_MIB=20000 FAKE_AVAILABLE_MEMORY_MIB=18000 expect_failure 'memory capacity is insufficient' "$repo_root/scripts/capacity-preflight.sh" --phase pre-change --target-max 2
reset_fixture
FAKE_DISK_USED=80 expect_failure 'Docker filesystem must remain below 80%' "$repo_root/scripts/capacity-preflight.sh" --phase pre-change --target-max 2
reset_fixture
FAKE_MANAGED_RUNNERS=1 expect_failure 'active managed runner' "$repo_root/scripts/capacity-preflight.sh" --phase pre-change --target-max 2
reset_fixture
FAKE_ACTIVE_JOBS=1 expect_failure 'active fleet job container' "$repo_root/scripts/capacity-preflight.sh" --phase pre-change --target-max 2
reset_fixture
FAKE_JOB_VOLUMES=1 expect_failure 'fleet job volume residue' "$repo_root/scripts/capacity-preflight.sh" --phase pre-change --target-max 2
reset_fixture
FAKE_JOB_NETWORKS=1 expect_failure 'fleet job network residue' "$repo_root/scripts/capacity-preflight.sh" --phase pre-change --target-max 2
reset_fixture
FAKE_COMPOSE_NETWORKS=0 FAKE_COMPOSE_CONTAINER_PROJECT=project-run expect_failure 'foreign Compose container residue' "$repo_root/scripts/capacity-preflight.sh" --phase pre-change --target-max 2
reset_fixture
FAKE_COMPOSE_VOLUMES=1 FAKE_COMPOSE_NETWORKS=0 FAKE_COMPOSE_VOLUME_PROJECT=project-run expect_failure 'foreign Compose volume residue' "$repo_root/scripts/capacity-preflight.sh" --phase pre-change --target-max 2
reset_fixture
FAKE_COMPOSE_NETWORK_PROJECT=project-run expect_failure 'foreign Compose network residue' "$repo_root/scripts/capacity-preflight.sh" --phase pre-change --target-max 2
reset_fixture
FAKE_COMPOSE_CONTAINER_NAME=rogue FAKE_COMPOSE_SERVICE=task expect_failure 'unexpected Compose container residue' "$repo_root/scripts/capacity-preflight.sh" --phase pre-change --target-max 2
reset_fixture
FAKE_COMPOSE_VOLUMES=1 expect_failure 'unexpected Compose volume residue' "$repo_root/scripts/capacity-preflight.sh" --phase pre-change --target-max 2
reset_fixture
FAKE_COMPOSE_NETWORK_NAME=ci-fleet_extra expect_failure 'unexpected Compose network residue' "$repo_root/scripts/capacity-preflight.sh" --phase pre-change --target-max 2
reset_fixture
CI_FLEET_MIN_RUNNERS=1 FAKE_EFFECTIVE_MIN=1 expect_failure 'CI_FLEET_MIN_RUNNERS must be 0' "$repo_root/scripts/capacity-preflight.sh" --phase pre-change --target-max 2
reset_fixture
FAKE_UNRELATED_CONTAINER=database expect_failure 'unrelated running Docker workload' "$repo_root/scripts/capacity-preflight.sh" --phase pre-change --target-max 2
reset_fixture
FAKE_OOM_EVIDENCE=1 expect_failure 'OOM evidence exists for the current boot' "$repo_root/scripts/capacity-preflight.sh" --phase pre-change --target-max 2

reset_fixture
expect_failure 'explicit --target-max is required' "$repo_root/scripts/capacity-preflight.sh" --phase pre-change
for malformed in 0 -1 2x 1.5; do
  expect_failure 'target MAX must be a positive integer' "$repo_root/scripts/capacity-preflight.sh" --phase pre-change --target-max "$malformed"
done
reset_fixture
FAKE_TOTAL_CPUS=32 FAKE_TOTAL_MEMORY_MIB=65536 FAKE_AVAILABLE_MEMORY_MIB=60000 \
  expect_failure 'target MAX must be exactly 2' "$repo_root/scripts/capacity-preflight.sh" --phase pre-change --target-max 3

reset_fixture
CI_FLEET_MAX_RUNNERS=2 FAKE_EFFECTIVE_MAX=2 expect_success "$repo_root/scripts/capacity-preflight.sh" --phase post-change --target-max 2 >/dev/null
reset_fixture
CI_FLEET_MAX_RUNNERS=2 FAKE_EFFECTIVE_MAX=1 expect_failure 'effective controller MAX does not match requested target' "$repo_root/scripts/capacity-preflight.sh" --phase post-change --target-max 2
reset_fixture
CI_FLEET_MAX_RUNNERS=2 FAKE_EFFECTIVE_MAX=2
secret_output=$("$repo_root/scripts/capacity-preflight.sh" --phase post-change --target-max 2 2>&1) || fail 'secret-output fixture unexpectedly failed'
if grep -Fq 'CAPACITY_TEST_SECRET_SHOULD_NOT_PRINT' <<<"$secret_output"; then fail 'capacity preflight printed an unrelated environment value'; fi
xtrace_output=$(bash -x "$repo_root/scripts/capacity-preflight.sh" --phase post-change --target-max 2 2>&1) || fail 'xtrace secret-output fixture unexpectedly failed'
if grep -Fq 'CAPACITY_TEST_SECRET_SHOULD_NOT_PRINT' <<<"$xtrace_output"; then fail 'capacity preflight exposed controller environment through inherited xtrace'; fi

for term in 'private desired state' --upgrade --config-repo --ref healthcheck retain restore checkpoint; do
  grep -Fqi -- "$term" "$repo_root/docs/CAPACITY-PROMOTION.md" || fail "capacity procedure is missing $term"
done
grep -Fq 'PREVIOUS_PRIVATE_CONFIGURATION_COMMIT' "$repo_root/docs/CAPACITY-PROMOTION.md" || fail 'capacity rollback does not apply the previous reviewed desired state'
[[ $(grep -Fc '. /etc/ci-fleet/ci-fleet.env' "$repo_root/docs/CAPACITY-PROMOTION.md") -ge 2 ]] || fail 'capacity procedure does not load rendered state for both preflight phases'
grep -Fq 'env -i' "$repo_root/docs/CAPACITY-PROMOTION.md" || fail 'capacity procedure does not isolate preflight from the caller environment'
if grep -Eq 'Edit only|force-recreate|ci-fleet\.env\.before-max2' "$repo_root/docs/CAPACITY-PROMOTION.md"; then fail 'capacity procedure still edits rendered host state'; fi
grep -Fq 'scripts/capacity-preflight.sh' "$repo_root/docs/ADDING-A-HOST.md" || fail 'host guide does not link the capacity procedure'
if grep -Riq --exclude='test-capacity-preflight.sh' 'docker system prune' "$repo_root/scripts"; then fail 'unrestricted prune exists in scripts'; fi

printf 'Capacity preflight tests passed.\n'
