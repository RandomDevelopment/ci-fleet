#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
installer=$repo_root/scripts/install-deployer.sh
runtime=$repo_root/scripts/deployer-runtime.sh

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
expect_success() {
  local output
  output=$("$@" 2>&1) || fail "expected success: $*; output=$output"
  printf '%s\n' "$output"
}
expect_failure() {
  local expected=$1 output
  shift
  if output=$("$@" 2>&1); then fail "expected failure: $*"; fi
  grep -Fq -- "$expected" <<<"$output" || fail "missing [$expected]: $output"
  printf '%s\n' "$output"
}

[[ -x "$installer" && -x "$runtime" ]] || fail 'deployer installer/runtime is missing'
expect_failure 'an explicit operating mode is required' "$installer"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
root=$tmp/root
fake_bin=$tmp/bin
mkdir -p "$fake_bin" "$root/etc/ci-fleet-deployer/adapters" \
  "$root/etc/ci-fleet-deployer/credentials" "$root/etc/ci-fleet-deployer/evidence" \
  "$root/etc/systemd/system" "$root/run/systemd/system"
printf 'ID=debian\nVERSION_ID="12"\n' >"$root/etc/os-release"
chmod 0700 "$root/etc/ci-fleet-deployer" "$root/etc/ci-fleet-deployer/adapters" \
  "$root/etc/ci-fleet-deployer/credentials" "$root/etc/ci-fleet-deployer/evidence"

cat >"$fake_bin/docker" <<'EOF'
#!/usr/bin/env bash
set -u
case "${1:-}" in
  info) exit "${FAKE_DOCKER_INFO_EXIT:-0}" ;;
  compose) [[ "${2:-}" == version ]] && exit "${FAKE_COMPOSE_EXIT:-0}" ;;
  ps) [[ -z "${FAKE_DOCKER_PS:-}" ]] || printf '%s\n' "$FAKE_DOCKER_PS" ;;
  network) [[ "${2:-}" == ls ]] && { [[ -z "${FAKE_DOCKER_NETWORKS:-}" ]] || printf '%s\n' "$FAKE_DOCKER_NETWORKS"; exit 0; } ;;
  volume) [[ "${2:-}" == ls ]] && { [[ -z "${FAKE_DOCKER_VOLUMES:-}" ]] || printf '%s\n' "$FAKE_DOCKER_VOLUMES"; exit 0; } ;;
esac
exit 0
EOF
cat >"$fake_bin/systemctl" <<'EOF'
#!/usr/bin/env bash
set -u
root=${CI_FLEET_DEPLOYER_ROOT:-}
log=${FAKE_SYSTEMCTL_LOG:-/dev/null}
printf '%s\n' "$*" >>"$log"
[[ -z ${FAKE_SYSTEMCTL_FAIL_COMMAND:-} || ${1:-} != "$FAKE_SYSTEMCTL_FAIL_COMMAND" ]] || exit 1
case "${1:-}" in
  is-system-running)
    [[ -z "${FAKE_SYSTEMD_FAIL:-}" ]] || exit 1
    printf '%s\n' "${FAKE_SYSTEMD_STATE:-running}"
    [[ ${FAKE_SYSTEMD_STATE:-running} != degraded ]]
    ;;
  is-enabled|is-active) [[ -e "$root/var/lib/ci-fleet-deployer/unit-${2:-}" ]] ;;
  enable)
    shift
    [[ "${1:-}" != --now ]] || shift
    for unit in "$@"; do : >"$root/var/lib/ci-fleet-deployer/unit-$unit"; done ;;
  disable)
    shift
    [[ "${1:-}" != --now ]] || shift
    for unit in "$@"; do rm -f "$root/var/lib/ci-fleet-deployer/unit-$unit"; done ;;
esac
exit 0
EOF
cat >"$fake_bin/systemd-analyze" <<'EOF'
#!/usr/bin/env bash
[[ "${1:-}" == verify ]] || exit 1
exit "${FAKE_SYSTEMD_VERIFY_EXIT:-0}"
EOF
cat >"$fake_bin/timedatectl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "${FAKE_TIME_SYNC:-yes}"
EOF
cat >"$fake_bin/curl" <<'EOF'
#!/usr/bin/env bash
cat >/dev/null
exit "${FAKE_CURL_EXIT:-0}"
EOF
cat >"$fake_bin/systemd-inhibit" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
while (($#)); do
  [[ "$1" != -- ]] || { shift; exec "$@"; }
  shift
done
exit 2
EOF
cat >"$fake_bin/df" <<'EOF'
#!/usr/bin/env bash
if [[ -n ${FAKE_DF_REQUIRE_EXISTING:-} && ! -e ${!#} ]]; then exit 1; fi
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\nfixture 104857600 1 %s 1%% /\n' "${FAKE_DISK_AVAILABLE:-104857599}"
EOF
chmod 0755 "$fake_bin"/*
export PATH="$fake_bin:$PATH"
export FAKE_SYSTEMCTL_LOG=$tmp/systemctl.log
export CI_FLEET_DEPLOYER_TESTING=1 CI_FLEET_DEPLOYER_ROOT=$root
export CI_FLEET_DEPLOYER_EUID_OVERRIDE=0 CI_FLEET_DEPLOYER_TEST_NETWORK=ok

adapter=$root/etc/ci-fleet-deployer/adapters/application-adapter
cat >"$adapter" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
printf '%s\n' "$1" >>"${FAKE_ADAPTER_LOG:?}"
[[ -z ${FAKE_ADAPTER_RECORD_CONFIG:-} ]] || { printf '%s\n' "${CI_FLEET_DEPLOYER_CONFIG:-unset}" >"$FAKE_ADAPTER_RECORD_CONFIG"; : >"$FAKE_ADAPTER_RECORD_CONFIG.seen"; cp "${CI_FLEET_DEPLOYER_CONFIG:-/dev/null}" "$FAKE_ADAPTER_RECORD_CONFIG.content" 2>/dev/null || true; }
if [[ -n ${FAKE_ADAPTER_FORBID_CONFIG_PATH:-} && ${CI_FLEET_DEPLOYER_CONFIG:-} == "$FAKE_ADAPTER_FORBID_CONFIG_PATH" ]]; then exit 43; fi
if [[ "$1" == deploy && -n ${FAKE_ADAPTER_FORBID_REQUEST_PATH:-} && ${CI_FLEET_DEPLOYER_REQUEST:-} == "$FAKE_ADAPTER_FORBID_REQUEST_PATH" ]]; then exit 44; fi
if [[ -n ${FAKE_ADAPTER_REPLACE_PATH:-} && -e $FAKE_ADAPTER_REPLACE_PATH ]]; then
  mv "$FAKE_ADAPTER_REPLACE_PATH" "$FAKE_ADAPTER_REPLACE_PATH.saved"
  printf '#!/usr/bin/env bash\nexit 45\n' >"$FAKE_ADAPTER_REPLACE_PATH"
  chmod 0700 "$FAKE_ADAPTER_REPLACE_PATH"
fi
if [[ ${FAKE_ADAPTER_SLEEP_OPERATION:-} == "$1" ]]; then sleep 2; fi
if [[ "$1" == health && -n ${FAKE_ADAPTER_FAIL_HEALTH_AFTER:-} ]]; then
  health_calls=$(grep -Fxc health "$FAKE_ADAPTER_LOG" || true)
  ((health_calls <= FAKE_ADAPTER_FAIL_HEALTH_AFTER)) || exit 42
fi
if [[ -e "${FAKE_ADAPTER_FAIL:-/nonexistent}" ]]; then
  fail_operation=$(<"$FAKE_ADAPTER_FAIL")
  [[ "$fail_operation" != all && "$fail_operation" != "$1" ]] || exit 42
fi
case "$1" in validate|health|cleanup|deploy|rollback) ;; *) exit 2 ;; esac
if [[ "$1" == rollback && -n ${CI_FLEET_DEPLOYER_ROLLBACK_COMMIT:-} ]]; then
  install -m 0600 /dev/null "$CI_FLEET_DEPLOYER_ROLLBACK_COMMIT"
fi
if [[ -n ${FAKE_ADAPTER_MUTATE_AUDIT_PATH:-} ]]; then
  rm -f "$FAKE_ADAPTER_MUTATE_AUDIT_PATH"
  ln -s "$FAKE_ADAPTER_MUTATE_AUDIT_TARGET" "$FAKE_ADAPTER_MUTATE_AUDIT_PATH"
fi
if [[ "$1" == deploy && -n ${FAKE_ADAPTER_MUTATE_SNAPSHOT_ROOT:-} ]]; then
  snapshot=$(find "$FAKE_ADAPTER_MUTATE_SNAPSHOT_ROOT" -maxdepth 1 -type d -name '.snapshot.*' -printf '%T@ %p\n' | sort -nr | head -n1 | cut -d' ' -f2-)
  [[ -n "$snapshot" ]] && printf 'adapter-mutation\n' >>"$snapshot/policy.conf"
fi
EOF
chmod 0700 "$adapter"
export FAKE_ADAPTER_LOG=$tmp/adapter.log
credential=$root/etc/ci-fleet-deployer/credentials/application.credential
printf 'CANARY_SECRET_VALUE_DO_NOT_PRINT\n' >"$credential"
chmod 0600 "$credential"
approval=$root/etc/ci-fleet-deployer/evidence/approval.conf
checkpoint=$root/etc/ci-fleet-deployer/evidence/checkpoint.conf
production_gate=$root/etc/ci-fleet-deployer/evidence/production-authorization.conf
core_ref=$(git -C "$repo_root" rev-parse HEAD)
source_ref=1111111111111111111111111111111111111111
image='registry.example.invalid/example/app@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
write_evidence() {
  local evidence_environment=${1:-staging} evidence_target=${2:-example-staging}
  cat >"$approval" <<EOF
SCHEMA_VERSION=1
ENVIRONMENT=$evidence_environment
TARGET_ID=$evidence_target
SOURCE_COMMIT=$source_ref
ARTIFACT_IMAGE=$image
APPROVAL_IDENTITY=example-reviewer
POLICY_IDENTITY=example-staging-policy-v1
APPROVAL_ID=approval-20260808-1
APPROVED_AT=2026-08-08T20:00:00Z
EOF
  cat >"$checkpoint" <<EOF
SCHEMA_VERSION=1
ENVIRONMENT=$evidence_environment
TARGET_ID=$evidence_target
CHECKPOINT_ID=checkpoint-20260808-1
RECORDED_AT=2026-08-08T19:55:00Z
EOF
  chmod 0600 "$approval" "$checkpoint"
}
write_production_gate() {
  cat >"$production_gate" <<EOF
SCHEMA_VERSION=1
ENVIRONMENT=production
TARGET_ID=example-production
SOURCE_COMMIT=$source_ref
ARTIFACT_IMAGE=$image
AUTHORIZED_BY=example-production-authorizer
GATE_ID=production-gate-20260808-1
AUTHORIZED_AT=2026-08-08T20:00:00Z
EOF
  chmod 0600 "$production_gate"
}
write_config() {
  local environment=${1:-staging} target=${2:-example-staging} digest=${3:-$image}
  cat >"$root/etc/ci-fleet-deployer/deployer.conf" <<EOF
SCHEMA_VERSION=1
CORE_REF=$core_ref
ENVIRONMENT=$environment
TARGET_ID=$target
DEPLOYER_IDENTITY=${environment}-deployer-01
ADAPTER_PATH=$adapter
ADAPTER_SHA256=$(sha256sum "$adapter" | cut -d' ' -f1)
CREDENTIAL_PROVIDER=file
CREDENTIAL_REF=$credential
CREDENTIAL_SCOPE=$environment
APPROVAL_PROVIDER=manual-exact-head
APPROVAL_EVIDENCE_PATH=$approval
CHECKPOINT_EVIDENCE_PATH=$checkpoint
SOURCE_COMMIT=$source_ref
ARTIFACT_IMAGE=$digest
NETWORK_HOST=registry.example.invalid
MIN_DISK_GIB=10
REQUIRE_COMPOSE=1
EOF
  if [[ "$environment" == production ]]; then printf 'PRODUCTION_AUTHORIZATION_EVIDENCE_PATH=%s\n' "$production_gate" >>"$root/etc/ci-fleet-deployer/deployer.conf"; fi
  chmod 0600 "$root/etc/ci-fleet-deployer/deployer.conf"
}
write_evidence
write_config
config=$root/etc/ci-fleet-deployer/deployer.conf

FAKE_DF_REQUIRE_EXISTING=1 expect_failure 'installed deployer lock boundary is absent or unsafe' "$installer" --check --config "$config" >/dev/null

write_evidence production example-production
write_config production example-production
expect_failure 'production authorization evidence must be a regular file' "$installer" --check --config "$config" >/dev/null
write_production_gate
expect_failure 'installed deployer lock boundary is absent or unsafe' "$installer" --check --config "$config" >/dev/null
rm "$production_gate"
write_evidence
write_config

python3 - "$approval" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('APPROVED_AT=2026-08-08T20:00:00Z', 'APPROVED_AT=2026-99-99T99:99:99Z'))
PY
expect_failure 'approval timestamp must be UTC RFC3339' "$installer" --check --config "$config" >/dev/null
write_evidence
python3 - "$config" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('ENVIRONMENT=staging', 'ENVIRONMENT=bad result=CHANGED'))
PY
unsafe_report=$(expect_failure 'invalid explicit environment' "$installer" --check --config "$config")
grep -Fq 'environment=unknown target=unknown' <<<"$unsafe_report" || fail 'report exposed unvalidated configuration fields'
[[ "$unsafe_report" != *'environment=bad result=CHANGED'* ]] || fail 'report allowed field injection'
write_config

# Read-only preflight and strict policy variants fail closed without reading secrets.
cp "$root/etc/os-release" "$tmp/os-release"
printf 'ID=alpine\nVERSION_ID=3.20\n' >"$root/etc/os-release"
expect_failure 'unsupported Linux distribution or release' "$installer" --check --config "$config" >/dev/null
cp "$tmp/os-release" "$root/etc/os-release"
FAKE_TIME_SYNC=no expect_failure 'host time is not synchronized' "$installer" --check --config "$config" >/dev/null
FAKE_SYSTEMD_FAIL=1 expect_failure 'systemd is unavailable' "$installer" --check --config "$config" >/dev/null
FAKE_DOCKER_INFO_EXIT=1 expect_failure 'Docker Engine is unavailable' "$installer" --check --config "$config" >/dev/null
CI_FLEET_DEPLOYER_TEST_NETWORK=fail expect_failure 'network prerequisite DNS lookup failed' "$installer" --check --config "$config" >/dev/null
FAKE_CURL_EXIT=1 expect_failure 'network prerequisite HTTPS check failed' "$installer" --check --config "$config" >/dev/null
FAKE_DISK_AVAILABLE=1 expect_failure 'insufficient deployer disk capacity' "$installer" --check --config "$config" >/dev/null
FAKE_COMPOSE_EXIT=1 expect_failure 'Docker Compose v2 is required but unavailable' "$installer" --check --config "$config" >/dev/null

chmod 0755 "$root/etc/ci-fleet-deployer"
expect_failure 'unsafe managed directory' "$installer" --check --config "$config" >/dev/null
chmod 0700 "$root/etc/ci-fleet-deployer"
mv "$root/etc/ci-fleet-deployer/adapters" "$root/etc/ci-fleet-deployer/adapters.real"
ln -s adapters.real "$root/etc/ci-fleet-deployer/adapters"
expect_failure 'unsafe symlinked managed directory' "$installer" --check --config "$config" >/dev/null
rm "$root/etc/ci-fleet-deployer/adapters"
mv "$root/etc/ci-fleet-deployer/adapters.real" "$root/etc/ci-fleet-deployer/adapters"

python3 - "$config" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('ARTIFACT_IMAGE=registry.example.invalid/example/app@sha256:', 'ARTIFACT_IMAGE=registry.example.invalid/example/app:latest#'))
PY
expect_failure 'artifact image must be an immutable' "$installer" --check --config "$config" >/dev/null
write_config

capability=$root/etc/ci-fleet-deployer/evidence/github-capability.conf
python3 - "$config" "$capability" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('APPROVAL_PROVIDER=manual-exact-head', 'APPROVAL_PROVIDER=github-environment') + 'APPROVAL_CAPABILITY_EVIDENCE_PATH='+sys.argv[2]+'\n')
PY
expect_failure 'GitHub capability evidence must be a regular file' "$installer" --check --config "$config" >/dev/null
cat >"$capability" <<EOF
SCHEMA_VERSION=1
ENVIRONMENT_PROTECTION=verified
EXACT_HEAD=$source_ref
CAPABILITY_ID=example-capability-check
CHECKED_AT=2026-08-08T20:00:00Z
EOF
chmod 0600 "$capability"
expect_failure 'installed deployer lock boundary is absent or unsafe' "$installer" --check --config "$config" >/dev/null
python3 - "$capability" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text('\n'.join(x for x in p.read_text().splitlines() if not x.startswith('CAPABILITY_ID='))+'\n')
PY
expect_failure 'GitHub Environment capability evidence is missing identity or UTC time' "$installer" --check --config "$config" >/dev/null
cat >"$capability" <<EOF
SCHEMA_VERSION=1
ENVIRONMENT_PROTECTION=verified
EXACT_HEAD=$source_ref
CAPABILITY_ID=example-capability-check
CHECKED_AT=2026-08-08T20:00:00Z
EOF
chmod 0600 "$capability"
write_config
python3 - "$config" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('CREDENTIAL_PROVIDER=file', 'CREDENTIAL_PROVIDER=external').replace(next(x for x in p.read_text().splitlines() if x.startswith('CREDENTIAL_REF=')), 'CREDENTIAL_REF=external:example-vault:staging/deployer'))
PY
expect_failure 'installed deployer lock boundary is absent or unsafe' "$installer" --check --config "$config" >/dev/null
write_config

printf 'UNKNOWN=value\n' >"$tmp/unknown.conf"; chmod 0600 "$tmp/unknown.conf"
expect_failure 'configuration path must be inside' "$installer" --check --config "$tmp/unknown.conf" >/dev/null
printf 'UNKNOWN=value\n' >"$config"
expect_failure 'unknown configuration key: UNKNOWN' "$installer" --check --config "$config" >/dev/null
write_config
printf 'SCHEMA_VERSION=1\n' >>"$config"
expect_failure 'duplicate configuration key: SCHEMA_VERSION' "$installer" --check --config "$config" >/dev/null
write_config

export CI_FLEET_DEPLOYER_EUID_OVERRIDE=1000
expect_failure 'run this mode as root' "$installer" --install --config "$config" >/dev/null
export CI_FLEET_DEPLOYER_EUID_OVERRIDE=0

before=$(find "$root" -printf '%P %y %m\n' | sort | sha256sum)
expect_failure 'result=BLOCKED' "$installer" --check --config "$config" >/dev/null
after=$(find "$root" -printf '%P %y %m\n' | sort | sha256sum)
[[ "$before" == "$after" ]] || fail '--check changed the test host'
fresh_uninstall=$(expect_success "$installer" --uninstall --config "$config")
[[ "$after" == "$(find "$root" -printf '%P %y %m\n' | sort | sha256sum)" ]] || fail 'fresh uninstall mutated an unmanaged host'
grep -Fq 'result=NO_CHANGE' <<<"$fresh_uninstall" || fail 'fresh uninstall did not report NO_CHANGE'

export FAKE_ADAPTER_FORBID_CONFIG_PATH=$config
first=$(expect_success "$installer" --install --config "$config")
unset FAKE_ADAPTER_FORBID_CONFIG_PATH
grep -Fq 'REPORT action=install result=CHANGED environment=staging' <<<"$first" || fail 'fresh install report is incomplete'
[[ -L "$root/opt/ci-fleet-deployer/current" ]] || fail 'fresh install lacks atomic current release'
[[ $(stat -c %a "$root/var/lib/ci-fleet-deployer/install-state.json") == 600 ]] || fail 'install state mode is not 0600'
for unit in ci-fleet-deployer.service ci-fleet-deployer-health.service ci-fleet-deployer-health.timer ci-fleet-deployer-cleanup.service ci-fleet-deployer-cleanup.timer ci-fleet-deployer-drain.service; do
  [[ -f "$root/etc/systemd/system/$unit" ]] || fail "missing unit $unit"
done
second=$(expect_success "$installer" --install --config "$config")
grep -Fq 'result=NO_CHANGE' <<<"$second" || fail 'second install was not idempotent'
check=$(expect_success "$installer" --check --config "$config")
grep -Fq 'REPORT action=check result=NO_CHANGE' <<<"$check" || fail 'check did not report convergence'
FAKE_SYSTEMD_STATE=degraded expect_success "$installer" --check --config "$config" >/dev/null
chmod 0666 "$root/etc/systemd/system/ci-fleet-deployer.service"
expect_failure 'installed deployer state is absent or drifted' "$installer" --check --config "$config" >/dev/null
expect_success "$installer" --repair --config "$config" >/dev/null
[[ $(stat -c %a "$root/etc/systemd/system/ci-fleet-deployer.service") == 644 ]] || fail 'repair did not restore unit mode 0644'

preparing=$root/var/lib/ci-fleet-deployer/.transaction-preparing.interrupted
mkdir -m 0700 "$preparing"
state_before_preparing=$(sha256sum "$root/var/lib/ci-fleet-deployer/install-state.json")
expect_success "$installer" --repair --config "$config" >/dev/null
[[ ! -e "$preparing" && "$state_before_preparing" == "$(sha256sum "$root/var/lib/ci-fleet-deployer/install-state.json")" ]] || fail 'incomplete transaction preparation was treated as recovery state'

interrupted=$root/var/lib/ci-fleet-deployer/.transaction.interrupted
mkdir -m 0700 "$interrupted" "$interrupted/units" "$interrupted/state"
for name in install-state.json active-policy.conf; do
  cp "$root/var/lib/ci-fleet-deployer/$name" "$interrupted/state/$name"
  printf '%s\n' "$name" >>"$interrupted/state-present"
done
for path in "$root"/etc/systemd/system/ci-fleet-deployer*; do
  name=${path##*/}; cp "$path" "$interrupted/units/$name"; printf '%s\n' "$name" >>"$interrupted/units-present"
done
printf '%s\n' "$(readlink "$root/opt/ci-fleet-deployer/current")" >"$interrupted/current-target"
printf '%s\n' ci-fleet-deployer-health.timer ci-fleet-deployer-cleanup.timer >"$interrupted/timers-enabled"
printf 'interrupted\n' >>"$root/var/lib/ci-fleet-deployer/install-state.json"
printf 'interrupted\n' >>"$root/etc/systemd/system/ci-fleet-deployer.service"
expect_failure 'interrupted installer transaction requires recovery' "$installer" --check --config "$config" >/dev/null
expect_success "$installer" --repair --config "$config" >/dev/null
[[ ! -e "$interrupted" ]] || fail 'interrupted transaction was not recovered'
expect_success "$installer" --check --config "$config" >/dev/null

rm "$root/etc/systemd/system/ci-fleet-deployer-health.timer"
expect_failure 'result=BLOCKED' "$installer" --check --config "$config" >/dev/null
repair=$(expect_success "$installer" --repair --config "$config")
grep -Fq 'result=CHANGED' <<<"$repair" || fail 'repair did not report a change'
[[ -f "$root/etc/systemd/system/ci-fleet-deployer-health.timer" ]] || fail 'repair did not restore unit drift'

transaction_state=$(sha256sum "$root/var/lib/ci-fleet-deployer/install-state.json")
printf '# transaction-checkpoint\n' >>"$root/etc/systemd/system/ci-fleet-deployer.service"
transaction_unit=$(sha256sum "$root/etc/systemd/system/ci-fleet-deployer.service")
FAKE_SYSTEMD_VERIFY_EXIT=1 expect_failure 'systemd unit verification failed' "$installer" --repair --config "$config" >/dev/null
[[ "$transaction_state" == "$(sha256sum "$root/var/lib/ci-fleet-deployer/install-state.json")" ]] || fail 'failed transaction replaced healthy state'
[[ "$transaction_unit" == "$(sha256sum "$root/etc/systemd/system/ci-fleet-deployer.service")" ]] || fail 'failed transaction did not restore units'
if compgen -G "$root/var/lib/ci-fleet-deployer/.transaction.*" >/dev/null; then fail 'failed transaction left recovery residue'; fi

stale_stage="$root/opt/ci-fleet-deployer/releases/.${core_ref}.staging.interrupted"
mkdir "$stale_stage"; chmod 0755 "$stale_stage"
printf 'pid=999999\nstarted_at=1\n' >"$root/var/lib/ci-fleet-deployer/active-operation"
chmod 0600 "$root/var/lib/ci-fleet-deployer/active-operation"
rm "$root/etc/systemd/system/ci-fleet-deployer-cleanup.timer"
expect_success "$installer" --repair --config "$config" >/dev/null
[[ ! -e "$stale_stage" && ! -e "$root/var/lib/ci-fleet-deployer/active-operation" ]] || fail 'bounded stale transaction recovery did not converge'
printf 'pid=%s\nstarted_at=1\n' "$$" >"$root/var/lib/ci-fleet-deployer/active-operation"
chmod 0600 "$root/var/lib/ci-fleet-deployer/active-operation"
expect_success "$installer" --repair --config "$config" >/dev/null
[[ ! -e "$root/var/lib/ci-fleet-deployer/active-operation" ]] || fail 'old operation marker survived PID reuse'

exec 8<"$root/var/lock/ci-fleet-deployer"
flock -n 8 || fail 'fixture could not acquire installer lock'
expect_failure 'another deployer installer operation is running' "$installer" --repair --config "$config" >/dev/null
expect_failure 'another deployer operation is running' "$installer" --check --config "$config" >/dev/null
flock -u 8; exec 8>&-

mv "$root/var/lock/ci-fleet-deployer" "$root/var/lock/ci-fleet-deployer.real"
ln -s "$root/var/lock/ci-fleet-deployer.real" "$root/var/lock/ci-fleet-deployer"
expect_failure 'unsafe symlinked managed directory' "$installer" --repair --config "$config" >/dev/null
rm "$root/var/lock/ci-fleet-deployer"
mv "$root/var/lock/ci-fleet-deployer.real" "$root/var/lock/ci-fleet-deployer"

mkdir -p "$root/run"
mv "$root/var/lock" "$root/run/lock"
ln -s ../run/lock "$root/var/lock"
expect_success "$installer" --check --config "$config" >/dev/null
rm "$root/var/lock"; mv "$root/run/lock" "$root/var/lock"

unit_path=$root/etc/systemd/system/ci-fleet-deployer.service
mv "$unit_path" "$unit_path.real"
printf 'unrelated-unit\n' >"$tmp/unrelated-unit"
ln -s "$tmp/unrelated-unit" "$unit_path"
expect_failure 'managed systemd unit has an unsafe owner or type' "$installer" --repair --config "$config" >/dev/null
[[ $(<"$tmp/unrelated-unit") == unrelated-unit ]] || fail 'systemd unit symlink attack changed an unrelated file'
rm "$unit_path"; mv "$unit_path.real" "$unit_path"

printf 'mixed-role\n' >"$root/etc/systemd/system/ci-fleet-health.service"
expect_failure 'ordinary CI controller or runner state is present' "$installer" --check --config "$config" >/dev/null
rm "$root/etc/systemd/system/ci-fleet-health.service"
printf 'runner\n' >"$root/etc/systemd/system/actions.runner.example-org-example-repo.example-runner.service"
expect_failure 'ordinary GitHub Actions runner service is present' "$installer" --check --config "$config" >/dev/null
rm "$root/etc/systemd/system/actions.runner.example-org-example-repo.example-runner.service"
export FAKE_DOCKER_PS='unrelated workload'
expect_failure 'unrelated Docker workload is present' "$installer" --check --config "$config" >/dev/null
unset FAKE_DOCKER_PS
export FAKE_DOCKER_PS='owned|deployer|staging-deployer-01'
expect_success "$installer" --check --config "$config" >/dev/null
export FAKE_DOCKER_PS='wrong|deployer|other-identity'
expect_failure 'unrelated Docker workload is present' "$installer" --check --config "$config" >/dev/null
unset FAKE_DOCKER_PS

chmod 0644 "$credential"
secret_error=$(expect_failure 'credential file must be owner-only mode 0600' "$installer" --check --config "$config")
[[ "$secret_error" != *CANARY_SECRET_VALUE_DO_NOT_PRINT* ]] || fail 'credential content leaked in error output'
chmod 0600 "$credential"

ln -s "$credential" "$root/etc/ci-fleet-deployer/credentials/symlinked"
python3 - "$config" "$root/etc/ci-fleet-deployer/credentials/symlinked" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('CREDENTIAL_REF='+str(p.parent/'credentials/application.credential'), 'CREDENTIAL_REF='+sys.argv[2]))
PY
expect_failure 'credential reference must be a regular file, not a symlink' "$installer" --check --config "$config" >/dev/null
rm "$root/etc/ci-fleet-deployer/credentials/symlinked"
write_config
python3 - "$config" "$root/etc/ci-fleet-deployer/credentials/./application.credential" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('CREDENTIAL_REF='+str(p.parent/'credentials/application.credential'), 'CREDENTIAL_REF='+sys.argv[2]))
PY
expect_failure 'credential reference contains a symlink or non-canonical component' "$installer" --check --config "$config" >/dev/null
write_config

printf 'bad\n' >>"$approval"
approval_error=$(expect_failure 'malformed approval evidence line' "$installer" --check --config "$config")
[[ "$approval_error" != *CANARY_SECRET_VALUE_DO_NOT_PRINT* ]] || fail 'secret content leaked beside evidence failure'
write_evidence

python3 - "$approval" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('APPROVAL_IDENTITY=example-reviewer', 'APPROVAL_IDENTITY=alternate-reviewer'))
PY
expect_success "$installer" --repair --config "$config" >/dev/null
python3 - "$root/var/lib/ci-fleet-deployer/install-state.json" <<'PY' || fail 'approval identity drift was not recorded'
import json, sys
assert json.load(open(sys.argv[1]))['approval_identity'] == 'alternate-reviewer'
PY
write_evidence
expect_success "$installer" --repair --config "$config" >/dev/null

mv "$root/var/lib/ci-fleet-deployer/active-policy.conf" "$root/var/lib/ci-fleet-deployer/active-policy.missing"
expect_success "$installer" --repair --config "$config" >/dev/null
[[ -f "$root/var/lib/ci-fleet-deployer/active-policy.conf" ]] || fail 'repair did not restore a missing active policy'
rm "$root/var/lib/ci-fleet-deployer/active-policy.missing"

printf '# force-transaction\n' >>"$root/etc/systemd/system/ci-fleet-deployer.service"
health_calls_before=$(grep -Fxc health "$FAKE_ADAPTER_LOG" || true)
# Repair bypasses only the untrusted old-policy health probe, so the candidate
# health check after activation is the next (and only) adapter health call.
export FAKE_ADAPTER_FAIL_HEALTH_AFTER=$((health_calls_before)) FAKE_SYSTEMCTL_FAIL_COMMAND=disable
expect_failure 'candidate health check failed after activation' "$installer" --repair --config "$config" >/dev/null
unset FAKE_ADAPTER_FAIL_HEALTH_AFTER FAKE_SYSTEMCTL_FAIL_COMMAND
[[ $(grep -Fxc health "$FAKE_ADAPTER_LOG" || true) == $((health_calls_before + 1)) ]] || fail 'repair did not run exactly the candidate health check'
compgen -G "$root/var/lib/ci-fleet-deployer/.transaction.*" >/dev/null || fail 'failed restoration deleted its recovery transaction'
expect_success "$installer" --repair --config "$config" >/dev/null
if compgen -G "$root/var/lib/ci-fleet-deployer/.transaction.*" >/dev/null; then fail 'retry did not recover the retained transaction'; fi

old_state=$(sha256sum "$root/var/lib/ci-fleet-deployer/install-state.json")
new_image='registry.example.invalid/example/app@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
image=$new_image
write_evidence
write_config
deployed_current=$root/var/lib/ci-fleet-deployer/deployed/current
deployed_target=$(readlink "$deployed_current")
rm "$deployed_current"
expect_failure 'deployed rollback snapshot is missing; restore it before convergence' "$installer" --upgrade --config "$config" >/dev/null
ln -s "$deployed_target" "$deployed_current"
export FAKE_ADAPTER_SLEEP_OPERATION=validate CI_FLEET_DEPLOYER_TEST_TIMEOUT_SECONDS=1
expect_failure 'candidate adapter validation failed' "$installer" --upgrade --config "$config" >/dev/null
unset FAKE_ADAPTER_SLEEP_OPERATION CI_FLEET_DEPLOYER_TEST_TIMEOUT_SECONDS
[[ "$old_state" == "$(sha256sum "$root/var/lib/ci-fleet-deployer/install-state.json")" ]] || fail 'timed-out candidate replaced healthy state'
export FAKE_ADAPTER_FAIL=$tmp/fail-adapter
printf 'validate\n' >"$FAKE_ADAPTER_FAIL"
expect_failure 'candidate adapter validation failed' "$installer" --upgrade --config "$config" >/dev/null
[[ "$old_state" == "$(sha256sum "$root/var/lib/ci-fleet-deployer/install-state.json")" ]] || fail 'failed candidate replaced healthy state'
rm "$FAKE_ADAPTER_FAIL"; unset FAKE_ADAPTER_FAIL

chmod 0644 "$credential"
expect_failure 'credential file must be owner-only mode 0600' "$installer" --upgrade --config "$config" >/dev/null
chmod 0600 "$credential"

install -m 0600 /dev/null "$root/var/lib/ci-fleet-deployer/drained"
health_calls_before=$(grep -Fxc health "$FAKE_ADAPTER_LOG" || true)
export FAKE_ADAPTER_FAIL_HEALTH_AFTER=$((health_calls_before + 1))
expect_failure 'candidate health check failed after activation' "$installer" --upgrade --config "$config" >/dev/null
unset FAKE_ADAPTER_FAIL_HEALTH_AFTER
[[ -f "$root/var/lib/ci-fleet-deployer/drained" ]] || fail 'failed convergence removed the drain marker'
rm "$root/var/lib/ci-fleet-deployer/drained"
upgrade=$(expect_success "$installer" --upgrade --config "$config")
grep -Fq 'result=CHANGED' <<<"$upgrade" || fail 'upgrade did not activate new immutable artifact'
python3 - "$approval" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('APPROVAL_ID=approval-20260808-1', 'APPROVAL_ID=approval-20260808-2'))
PY
expect_success "$installer" --upgrade --config "$config" >/dev/null
grep -Fq 'sha256:aaaaaaaa' "$root/var/lib/ci-fleet-deployer/last-known-good.json" || fail 'second undeployed upgrade replaced the deployed rollback point'
printf 'rollback\n' >"$tmp/fail-rollback"
export FAKE_ADAPTER_FAIL=$tmp/fail-rollback
expect_failure 'application adapter rollback failed' "$installer" --rollback --config "$config" >/dev/null
unset FAKE_ADAPTER_FAIL; rm "$tmp/fail-rollback"
grep -Fq 'sha256:bbbbbbbb' "$root/var/lib/ci-fleet-deployer/install-state.json" || fail 'failed application rollback did not restore current core state'
rollback_calls_before=$(grep -Fxc rollback "$FAKE_ADAPTER_LOG" || true)
FAKE_SYSTEMD_VERIFY_EXIT=1 expect_failure 'systemd unit verification failed' "$installer" --rollback --config "$config" >/dev/null
[[ $(grep -Fxc rollback "$FAKE_ADAPTER_LOG" || true) == "$rollback_calls_before" ]] || fail 'application rollback ran before core rollback staging was proven'
grep -Fq 'sha256:bbbbbbbb' "$root/var/lib/ci-fleet-deployer/install-state.json" || fail 'failed rollback did not preserve current core state'
cp "$root/var/lib/ci-fleet-deployer/last-known-good.json" "$tmp/last-known-good.saved"
python3 - "$root/var/lib/ci-fleet-deployer/last-known-good.json" <<'PY'
import json, sys
p=sys.argv[1]; value=json.load(open(p)); value['core_ref']='2222222222222222222222222222222222222222'; json.dump(value,open(p,'w'),indent=2,sort_keys=True)
PY
expect_failure 'last-known-good state and policy do not match' "$installer" --rollback --config "$config" >/dev/null
cp "$tmp/last-known-good.saved" "$root/var/lib/ci-fleet-deployer/last-known-good.json"; chmod 0600 "$root/var/lib/ci-fleet-deployer/last-known-good.json"
printf 'runner\n' >"$root/etc/systemd/system/actions.runner.rollback-drift.service"
expect_failure 'ordinary GitHub Actions runner service is present' "$installer" --rollback --config "$config" >/dev/null
[[ $(grep -Fxc rollback "$FAKE_ADAPTER_LOG" || true) == "$rollback_calls_before" ]] || fail 'rollback adapter ran after role isolation drift'
rm "$root/etc/systemd/system/actions.runner.rollback-drift.service"
mv "$approval" "$approval.rollback-saved"
mv "$checkpoint" "$checkpoint.rollback-saved"
export FAKE_CURL_EXIT=1
rollback=$(expect_success "$installer" --rollback --config "$config")
unset FAKE_CURL_EXIT
mv "$approval.rollback-saved" "$approval"
mv "$checkpoint.rollback-saved" "$checkpoint"
grep -Fq 'result=CHANGED' <<<"$rollback" || fail 'rollback did not restore last-known-good state'
grep -Fq 'sha256:aaaaaaaa' "$root/var/lib/ci-fleet-deployer/install-state.json" || fail 'rollback state lacks prior artifact'
grep -Fq 'next=restore-host-policy-evidence-then-check' <<<"$rollback" || fail 'rollback report lacks the exact operator reconciliation action'
grep -Fq 'sha256:bbbbbbbb' "$config" || fail 'rollback unexpectedly rewrote operator-owned desired policy'

# An interrupted committed rollback must report recovery without converging.
recovery_transaction=$root/var/lib/ci-fleet-deployer/.transaction.rollback-interrupted
rm -rf "$root/var/lib/ci-fleet-deployer/deployed"
mkdir -m 0700 "$recovery_transaction" "$recovery_transaction/units" "$recovery_transaction/state"
for name in install-state.json active-policy.conf last-known-good.json last-known-good-policy.conf; do
  [[ ! -e "$root/var/lib/ci-fleet-deployer/$name" ]] || { cp "$root/var/lib/ci-fleet-deployer/$name" "$recovery_transaction/state/$name"; printf '%s\n' "$name" >>"$recovery_transaction/state-present"; }
done
for path in "$root"/etc/systemd/system/ci-fleet-deployer*; do
  name=${path##*/}; cp "$path" "$recovery_transaction/units/$name"; printf '%s\n' "$name" >>"$recovery_transaction/units-present"
done
printf '%s\n' "$(readlink "$root/opt/ci-fleet-deployer/current")" >"$recovery_transaction/current-target"
printf '%s\n' ci-fleet-deployer-health.timer ci-fleet-deployer-cleanup.timer >"$recovery_transaction/timers-enabled"
install -m 0600 /dev/null "$recovery_transaction/application-rollback-committed"
deploy_calls_before=$(grep -Fxc deploy "$FAKE_ADAPTER_LOG" || true)
health_calls_before=$(grep -Fxc health "$FAKE_ADAPTER_LOG" || true)
recovery=$(expect_success "$installer" --upgrade --config "$config")
grep -Fq 'next=restore-host-policy-evidence-then-check' <<<"$recovery" || fail 'interrupted committed rollback recovery lacks the operator reconciliation action'
[[ $(grep -Fxc deploy "$FAKE_ADAPTER_LOG" || true) == "$deploy_calls_before" ]] || fail 'convergence ran after committed rollback recovery'
[[ $(grep -Fxc health "$FAKE_ADAPTER_LOG" || true) == "$health_calls_before" ]] || fail 'convergence health ran after committed rollback recovery'
[[ ! -e "$recovery_transaction" ]] || fail 'committed rollback recovery retained its transaction'
expect_success "$installer" --repair --config "$config" >/dev/null
expect_success "$installer" --check --config "$config" >/dev/null

# A lost install state with surviving release/units must not be treated as a fresh install.
mv "$root/var/lib/ci-fleet-deployer/install-state.json" "$tmp/install-state.saved"
expect_failure 'restore install state before convergence' "$installer" --repair --config "$config" >/dev/null
mv "$tmp/install-state.saved" "$root/var/lib/ci-fleet-deployer/install-state.json"
chmod 0600 "$root/var/lib/ci-fleet-deployer/install-state.json"
expect_success "$installer" --repair --config "$config" >/dev/null

# Rollback must work from the retained pair without a usable candidate config.
mv "$config" "$tmp/config.saved"
rollback=$(expect_success "$installer" --rollback --config "$config")
grep -Fq 'result=CHANGED' <<<"$rollback" || fail 'config-independent rollback did not report change'
mv "$tmp/config.saved" "$config"; chmod 0600 "$config"
expect_success "$installer" --repair --config "$config" >/dev/null
printf 'malformed line\n' >"$config"; chmod 0600 "$config"
rollback=$(expect_success "$installer" --rollback --config "$config")
grep -Fq 'result=CHANGED' <<<"$rollback" || fail 'malformed-config rollback did not report change'
expect_success "$installer" --uninstall --config "$config" >/dev/null
[[ ! -L "$root/opt/ci-fleet-deployer/current" ]] || fail 'malformed-config uninstall retained the activation pointer'
write_config
image='registry.example.invalid/example/app@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
write_evidence
write_config
expect_success "$installer" --install --config "$config" >/dev/null
image='registry.example.invalid/example/app@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
write_evidence
write_config
expect_success "$installer" --upgrade --config "$config" >/dev/null
expect_success "$installer" --check --config "$config" >/dev/null

# rollback_available must reflect a validated retained pair.
[[ -f "$root/var/lib/ci-fleet-deployer/last-known-good.json" ]] || fail 'expected a retained rollback point'
chmod 0644 "$root/var/lib/ci-fleet-deployer/last-known-good.json"
available_check=$(expect_success "$installer" --check --config "$config")
grep -Fq 'rollback_available=no' <<<"$available_check" || fail 'drifted retained pair still reported rollback_available=yes'
chmod 0600 "$root/var/lib/ci-fleet-deployer/last-known-good.json"
available_check=$(expect_success "$installer" --check --config "$config")
grep -Fq 'rollback_available=yes' <<<"$available_check" || fail 'valid retained pair was not reported rollback_available=yes'

write_production_gate
write_evidence production example-production
write_config production example-production
expect_failure 'installed environment and target identity cannot change in place' "$installer" --upgrade --config "$config" >/dev/null
write_evidence
write_config staging example-staging

active=$root/var/lib/ci-fleet-deployer/active-operation
printf 'unrelated-active\n' >"$tmp/unrelated-active"
ln -s "$tmp/unrelated-active" "$active"
expect_failure 'active operation marker is an unsafe symlink' "$installer" --repair --config "$config" >/dev/null
rm "$active"
printf 'pid=%s\nstarted_at=%s\n' "$$" "$(date +%s)" >"$active"
chmod 0600 "$active"
expect_failure 'active deployment prevents this operation' "$installer" --upgrade --config "$config" >/dev/null
expect_failure 'active deployment prevents drain' "$installer" --drain --config "$config" >/dev/null
rm "$active"
drain=$(expect_success "$installer" --drain --config "$config")
grep -Fq 'result=CHANGED' <<<"$drain" || fail 'drain marker was not created'
[[ -f "$root/var/lib/ci-fleet-deployer/drained" ]] || fail 'drain state is absent'
mv "$fake_bin/docker" "$fake_bin/docker.unavailable"
expect_success "$installer" --drain --config "$config" >/dev/null
mv "$fake_bin/docker.unavailable" "$fake_bin/docker"
chmod 0666 "$root/etc/systemd/system/ci-fleet-deployer.service"
expect_failure 'installed deployer state is absent or drifted; repair before resume' "$installer" --resume --config "$config" >/dev/null
[[ -f "$root/var/lib/ci-fleet-deployer/drained" ]] || fail 'failed resume removed drain state'
expect_success "$installer" --repair --config "$config" >/dev/null
[[ -f "$root/var/lib/ci-fleet-deployer/drained" ]] || fail 'repair implicitly resumed the deployer'
resume=$(expect_success "$installer" --resume --config "$config")
grep -Fq 'result=CHANGED' <<<"$resume" || fail 'resume did not clear drain state'
grep -Fq 'health=healthy' <<<"$resume" || fail 'resume report omitted verified health'
[[ ! -e "$root/var/lib/ci-fleet-deployer/drained" ]] || fail 'resume retained drain state'
chmod 0666 "$root/etc/systemd/system/ci-fleet-deployer.service"
expect_failure 'installed deployer state is absent or drifted; repair before resume' "$installer" --resume --config "$config" >/dev/null
expect_success "$installer" --repair --config "$config" >/dev/null
repeat_resume=$(expect_success "$installer" --resume --config "$config")
grep -Fq 'result=NO_CHANGE' <<<"$repeat_resume" || fail 'repeated resume was not idempotent'
expect_success "$installer" --drain --config "$config" >/dev/null
rm "$root/var/lib/ci-fleet-deployer/drained"
printf 'unrelated-drain-target\n' >"$tmp/unrelated-drain-target"
ln -s "$tmp/unrelated-drain-target" "$root/var/lib/ci-fleet-deployer/drained"
expect_failure 'drain marker must be a regular file, not a symlink' "$installer" --uninstall --config "$config" >/dev/null
[[ $(<"$tmp/unrelated-drain-target") == unrelated-drain-target ]] || fail 'uninstall followed an unsafe drain marker'
rm "$root/var/lib/ci-fleet-deployer/drained"
install -m 0600 /dev/null "$root/var/lib/ci-fleet-deployer/drained"

unrelated=$root/var/lib/ci-fleet-deployer/operator-note
printf 'preserve\n' >"$unrelated"
mv "$root/etc/ci-fleet-deployer/adapters" "$root/etc/ci-fleet-deployer/adapters.retained"
mv "$root/etc/ci-fleet-deployer/credentials" "$root/etc/ci-fleet-deployer/credentials.retained"
mv "$root/etc/ci-fleet-deployer/evidence" "$root/etc/ci-fleet-deployer/evidence.retained"
export FAKE_DOCKER_INFO_EXIT=1
uninstall=$(expect_success "$installer" --uninstall --config "$config")
unset FAKE_DOCKER_INFO_EXIT
mv "$root/etc/ci-fleet-deployer/adapters.retained" "$root/etc/ci-fleet-deployer/adapters"
mv "$root/etc/ci-fleet-deployer/credentials.retained" "$root/etc/ci-fleet-deployer/credentials"
mv "$root/etc/ci-fleet-deployer/evidence.retained" "$root/etc/ci-fleet-deployer/evidence"
grep -Fq 'result=CHANGED' <<<"$uninstall" || fail 'uninstall did not report change'
[[ -f "$config" && -f "$credential" && -f "$unrelated" ]] || fail 'uninstall removed retained operator state or credentials'
[[ ! -L "$root/opt/ci-fleet-deployer/current" ]] || fail 'uninstall retained activation pointer'
repeat_uninstall=$(expect_success "$installer" --uninstall --config "$config")
grep -Fq 'result=NO_CHANGE' <<<"$repeat_uninstall" || fail 'repeated uninstall was not idempotent'

# A drifted managed unit directory must fail closed before any uninstall mutation.
expect_success "$installer" --install --config "$config" >/dev/null
rm "$root/etc/systemd/system/ci-fleet-deployer-drain.service"
mkdir "$root/etc/systemd/system/ci-fleet-deployer-drain.service"
uninstall_before=$(find "$root/etc/systemd/system" -mindepth 1 -maxdepth 1 -printf '%P %y %m\n' | sort | sha256sum)
[[ ! -e "$root/var/lib/ci-fleet-deployer/drained" ]] || fail 'test setup expected no drain marker before drifted uninstall'
expect_failure 'managed unit ci-fleet-deployer-drain.service has an unsafe type' "$installer" --uninstall --config "$config" >/dev/null
[[ "$uninstall_before" == "$(find "$root/etc/systemd/system" -mindepth 1 -maxdepth 1 -printf '%P %y %m\n' | sort | sha256sum)" ]] || fail 'unsafe unit type partially mutated the host during uninstall'
[[ -L "$root/opt/ci-fleet-deployer/current" ]] || fail 'failed closed uninstall removed the activation pointer'
[[ ! -e "$root/var/lib/ci-fleet-deployer/drained" ]] || fail 'failed closed uninstall created the drain marker'
rmdir "$root/etc/systemd/system/ci-fleet-deployer-drain.service"
expect_success "$installer" --uninstall --config "$config" >/dev/null
[[ ! -L "$root/opt/ci-fleet-deployer/current" ]] || fail 'retry after correcting unit drift did not uninstall'

# Runtime contract: exact-head request/evidence, drain and scoped adapter calls.
write_evidence staging example-staging
write_config staging example-staging
python3 - "$config" "$capability" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('APPROVAL_PROVIDER=manual-exact-head', 'APPROVAL_PROVIDER=github-environment') + 'APPROVAL_CAPABILITY_EVIDENCE_PATH='+sys.argv[2]+'\n')
PY
expect_success "$installer" --install --config "$config" >/dev/null
request=$root/var/lib/ci-fleet-deployer/request.conf
cp "$approval" "$request"
chmod 0600 "$request"
export CI_FLEET_DEPLOYER_CONFIG=$config CI_FLEET_DEPLOYER_REQUEST=$request
expect_failure 'usage: deployer-runtime.sh health|cleanup|deploy|drain' "$runtime" rollback >/dev/null
mv "$adapter" "$adapter.saved"
expect_success "$runtime" drain >/dev/null
[[ -f "$root/var/lib/ci-fleet-deployer/drained" ]] || fail 'runtime drain required a healthy adapter'
rm "$root/var/lib/ci-fleet-deployer/drained"
mv "$adapter.saved" "$adapter"
export FAKE_ADAPTER_REPLACE_PATH=$adapter
expect_success "$runtime" health >/dev/null
unset FAKE_ADAPTER_REPLACE_PATH
rm "$adapter"; mv "$adapter.saved" "$adapter"
expect_success "$runtime" health >/dev/null
expect_success "$runtime" cleanup >/dev/null
adapter_config=$tmp/adapter-config
FAKE_ADAPTER_RECORD_CONFIG=$adapter_config expect_success "$runtime" health >/dev/null
unset FAKE_ADAPTER_RECORD_CONFIG
[[ -e "$adapter_config.seen" ]] || fail 'adapter config snapshot was not recorded'
[[ $(<"$adapter_config") == "$root"/var/lib/ci-fleet-deployer/.active-policy.* ]] || fail 'adapter did not receive an immutable policy snapshot path'
cmp -s "$adapter_config.content" "$root/var/lib/ci-fleet-deployer/active-policy.conf" || fail 'adapter policy snapshot content differs from the validated policy'
compgen -G "$root/var/lib/ci-fleet-deployer/.active-policy.*" >/dev/null && fail 'policy snapshot was not cleaned up'
chmod 0644 "$credential"
cleanup_calls_before=$(grep -Fxc cleanup "$FAKE_ADAPTER_LOG" || true)
expect_failure 'credential file has unsafe owner or mode' "$runtime" cleanup >/dev/null
[[ $(grep -Fxc cleanup "$FAKE_ADAPTER_LOG" || true) == "$cleanup_calls_before" ]] || fail 'cleanup ran with unsafe file credentials'
chmod 0600 "$credential"
mv "$root/etc/ci-fleet-deployer/credentials" "$root/etc/ci-fleet-deployer/credentials.real"
ln -s "$root/etc/ci-fleet-deployer/credentials.real" "$root/etc/ci-fleet-deployer/credentials"
expect_failure 'credential directory has unsafe owner, mode, or type' "$runtime" cleanup >/dev/null
rm "$root/etc/ci-fleet-deployer/credentials"
mv "$root/etc/ci-fleet-deployer/credentials.real" "$root/etc/ci-fleet-deployer/credentials"
mkdir "$root/var/lib/ci-fleet-deployer/.transaction.interrupted"
deploy_calls_before=$(grep -Fxc deploy "$FAKE_ADAPTER_LOG" || true)
expect_failure 'interrupted installer transaction requires recovery' "$runtime" deploy >/dev/null
[[ $(grep -Fxc deploy "$FAKE_ADAPTER_LOG" || true) == "$deploy_calls_before" ]] || fail 'deployment ran during an interrupted installer transaction'
rmdir "$root/var/lib/ci-fleet-deployer/.transaction.interrupted"
printf 'pid=999999\nstarted_at=1\n' >"$root/var/lib/ci-fleet-deployer/active-operation"
chmod 0600 "$root/var/lib/ci-fleet-deployer/active-operation"
deploy_calls_before=$(grep -Fxc deploy "$FAKE_ADAPTER_LOG" || true)
expect_failure 'active operation marker requires recovery' "$runtime" deploy >/dev/null
[[ $(grep -Fxc deploy "$FAKE_ADAPTER_LOG" || true) == "$deploy_calls_before" ]] || fail 'deployment replaced an unresolved active marker'
rm "$root/var/lib/ci-fleet-deployer/active-operation"
printf 'runner\n' >"$root/etc/systemd/system/actions.runner.late-added.service"
deploy_calls_before=$(grep -Fxc deploy "$FAKE_ADAPTER_LOG" || true)
cleanup_calls_before=$(grep -Fxc cleanup "$FAKE_ADAPTER_LOG" || true)
health_calls_before=$(grep -Fxc health "$FAKE_ADAPTER_LOG" || true)
expect_failure 'ordinary GitHub Actions runner service is present' "$runtime" deploy >/dev/null
expect_failure 'ordinary GitHub Actions runner service is present' "$runtime" cleanup >/dev/null
expect_failure 'ordinary GitHub Actions runner service is present' "$runtime" health >/dev/null
[[ $(grep -Fxc deploy "$FAKE_ADAPTER_LOG" || true) == "$deploy_calls_before" ]] || fail 'deployment ran after role isolation drift'
[[ $(grep -Fxc cleanup "$FAKE_ADAPTER_LOG" || true) == "$cleanup_calls_before" ]] || fail 'cleanup ran after role isolation drift'
[[ $(grep -Fxc health "$FAKE_ADAPTER_LOG" || true) == "$health_calls_before" ]] || fail 'health adapter ran after role isolation drift'
rm "$root/etc/systemd/system/actions.runner.late-added.service"
python3 - "$config" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('APPROVAL_PROVIDER=github-environment', 'APPROVAL_PROVIDER=github-environmnt'))
PY
expect_failure 'unsupported approval provider' "$runtime" deploy >/dev/null
python3 - "$config" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('SCHEMA_VERSION=1', 'SCHEMA_VERSION=2', 1))
PY
expect_failure 'configuration has an unsupported or missing schema version' "$runtime" deploy >/dev/null
python3 - "$config" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('SCHEMA_VERSION=2', 'SCHEMA_VERSION=1', 1))
PY
python3 - "$config" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text('\n'.join(x for x in p.read_text().splitlines() if not x.startswith('CORE_REF='))+'\n')
PY
expect_failure 'configuration is missing a valid core revision' "$runtime" deploy >/dev/null
python3 - "$config" "$core_ref" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text() + 'CORE_REF='+sys.argv[2]+'\n')
PY
python3 - "$config" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('APPROVAL_PROVIDER=github-environmnt', 'APPROVAL_PROVIDER=external-exact-head').replace('APPROVAL_CAPABILITY_EVIDENCE_PATH='+str(p.parent/'evidence/github-capability.conf'), '').rstrip()+'\n')
PY
cp "$approval" "$request"; chmod 0600 "$request"
expect_success "$runtime" deploy >/dev/null
write_evidence staging example-staging
python3 - "$approval" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('APPROVAL_ID=approval-20260808-1', 'APPROVAL_ID=approval-20260808-2').replace('APPROVED_AT=2026-08-08T20:00:00Z', 'APPROVED_AT=2026-08-08T20:01:00Z'))
PY
cp "$approval" "$request"; chmod 0600 "$request"
python3 - "$config" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('APPROVAL_PROVIDER=external-exact-head', 'APPROVAL_PROVIDER=github-environment') + 'APPROVAL_CAPABILITY_EVIDENCE_PATH='+str(p.parent/'evidence/github-capability.conf')+'\n')
PY
python3 - "$approval" "$request" <<'PY'
from pathlib import Path
import sys
for name in sys.argv[1:]:
    p=Path(name); p.write_text(p.read_text().replace('APPROVED_AT=2026-08-08T20:01:00Z', 'APPROVED_AT=2026-99-99T99:99:99Z'))
PY
expect_failure 'deployment request has an invalid approval time' "$runtime" deploy >/dev/null
compgen -G "$root/var/lib/ci-fleet-deployer/.active-policy.*" >/dev/null && fail 'rejected request left a stale policy snapshot'
write_evidence staging example-staging
python3 - "$approval" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('APPROVAL_ID=approval-20260808-1', 'APPROVAL_ID=approval-20260808-3').replace('APPROVED_AT=2026-08-08T20:00:00Z', 'APPROVED_AT=2026-08-08T20:02:00Z'))
PY
cp "$approval" "$request"; chmod 0600 "$request"
python3 - "$request" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('APPROVAL_ID=approval-20260808-3', 'APPROVAL_ID=forged-approval'))
PY
expect_failure 'deployment request does not match protected approval APPROVAL_ID' "$runtime" deploy >/dev/null
cp "$approval" "$request"; chmod 0600 "$request"
mv "$checkpoint" "$checkpoint.saved"
deploy_calls_before=$(grep -Fxc deploy "$FAKE_ADAPTER_LOG" || true)
expect_failure 'checkpoint evidence must be a regular file' "$runtime" deploy >/dev/null
[[ $(grep -Fxc deploy "$FAKE_ADAPTER_LOG" || true) == "$deploy_calls_before" ]] || fail 'deployment ran without checkpoint evidence'
mv "$checkpoint.saved" "$checkpoint"
mv "$capability" "$capability.saved"
deploy_calls_before=$(grep -Fxc deploy "$FAKE_ADAPTER_LOG" || true)
expect_failure 'GitHub capability evidence must be a regular file' "$runtime" deploy >/dev/null
[[ $(grep -Fxc deploy "$FAKE_ADAPTER_LOG" || true) == "$deploy_calls_before" ]] || fail 'deployment ran without GitHub capability evidence'
mv "$capability.saved" "$capability"
deployed_target=$(readlink "$deployed_current")
rm "$deployed_current"
expect_failure 'deployed rollback snapshot is missing' "$runtime" deploy >/dev/null
ln -s "$deployed_target" "$deployed_current"
chmod 0644 "$credential"
deploy_calls_before=$(grep -Fxc deploy "$FAKE_ADAPTER_LOG" || true)
expect_failure 'credential file has unsafe owner or mode' "$runtime" deploy >/dev/null
[[ $(grep -Fxc deploy "$FAKE_ADAPTER_LOG" || true) == "$deploy_calls_before" ]] || fail 'deployment ran with unsafe file credentials'
chmod 0600 "$credential"
printf 'unrelated-audit\n' >"$tmp/unrelated-audit"
rm -f "$root/var/log/ci-fleet-deployer/audit.log"
ln -s "$tmp/unrelated-audit" "$root/var/log/ci-fleet-deployer/audit.log"
deploy_calls_before=$(grep -Fxc deploy "$FAKE_ADAPTER_LOG" || true)
expect_failure 'deployer audit log must be a regular file, not a symlink' "$runtime" deploy >/dev/null
[[ $(grep -Fxc deploy "$FAKE_ADAPTER_LOG" || true) == "$deploy_calls_before" && $(<"$tmp/unrelated-audit") == unrelated-audit ]] || fail 'unsafe audit storage was touched after adapter execution'
rm "$root/var/log/ci-fleet-deployer/audit.log"
write_evidence staging example-staging
python3 - "$approval" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('APPROVAL_ID=approval-20260808-1', 'APPROVAL_ID=failed-adapter-attempt').replace('APPROVED_AT=2026-08-08T20:00:00Z', 'APPROVED_AT=2026-08-08T20:03:00Z'))
PY
cp "$approval" "$request"; chmod 0600 "$request"
export FAKE_ADAPTER_FAIL=$tmp/fail-adapter
printf 'deploy\n' >"$FAKE_ADAPTER_FAIL"
expect_failure 'deployment adapter failed after approval consumption' "$runtime" deploy >/dev/null
unset FAKE_ADAPTER_FAIL; rm "$tmp/fail-adapter"
grep -Fq 'approval=failed-adapter-attempt approver=example-reviewer policy=example-staging-policy-v1 checkpoint=checkpoint-20260808-1 authorized_by=none gate=none result=failed phase=adapter status=42' "$root/var/log/ci-fleet-deployer/audit.log" || fail 'consumed failed deployment was not audited'
rm -f "$root/var/lib/ci-fleet-deployer/last-request.conf"
rm -rf "$root/var/lib/ci-fleet-deployer/consumed-requests"
write_evidence staging example-staging
python3 - "$approval" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('APPROVAL_ID=approval-20260808-1', 'APPROVAL_ID=snapshot-mutation-attempt').replace('APPROVED_AT=2026-08-08T20:00:00Z', 'APPROVED_AT=2026-08-08T20:04:00Z'))
PY
cp "$approval" "$request"; chmod 0600 "$request"
export FAKE_ADAPTER_MUTATE_SNAPSHOT_ROOT=$root/var/lib/ci-fleet-deployer/deployed
expect_failure 'prepared deployed snapshot changed during deployment' "$runtime" deploy >/dev/null
unset FAKE_ADAPTER_MUTATE_SNAPSHOT_ROOT
grep -Fq 'approval=snapshot-mutation-attempt approver=example-reviewer policy=example-staging-policy-v1 checkpoint=checkpoint-20260808-1 authorized_by=none gate=none result=failed phase=post-adapter' "$root/var/log/ci-fleet-deployer/audit.log" || fail 'post-adapter deployment failure was not audited'
write_evidence staging example-staging
python3 - "$approval" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('APPROVAL_ID=approval-20260808-1', 'APPROVAL_ID=audit-replacement-attempt').replace('APPROVED_AT=2026-08-08T20:00:00Z', 'APPROVED_AT=2026-08-08T20:05:00Z'))
PY
cp "$approval" "$request"; chmod 0600 "$request"
export FAKE_ADAPTER_MUTATE_AUDIT_PATH=$root/var/log/ci-fleet-deployer/audit.log FAKE_ADAPTER_MUTATE_AUDIT_TARGET=$tmp/unrelated-audit
expect_failure 'deployer audit log must be a regular file, not a symlink' "$runtime" deploy >/dev/null
unset FAKE_ADAPTER_MUTATE_AUDIT_PATH FAKE_ADAPTER_MUTATE_AUDIT_TARGET
[[ $(<"$tmp/unrelated-audit") == unrelated-audit ]] || fail 'adapter audit replacement redirected the trusted append'
rm "$root/var/log/ci-fleet-deployer/audit.log"

# Signal at the deployed-snapshot publication boundary must not delete the published snapshot.
write_evidence staging example-staging
python3 - "$approval" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('APPROVAL_ID=approval-20260808-1', 'APPROVAL_ID=signal-at-publication-attempt').replace('APPROVED_AT=2026-08-08T20:00:00Z', 'APPROVED_AT=2026-08-08T20:06:00Z'))
PY
cp "$approval" "$request"; chmod 0600 "$request"
export FAKE_ADAPTER_MUTATE_AUDIT_PATH=$root/var/log/ci-fleet-deployer/audit.log FAKE_ADAPTER_MUTATE_AUDIT_TARGET=$tmp/unrelated-audit
expect_failure 'deployer audit log must be a regular file, not a symlink' "$runtime" deploy >/dev/null
unset FAKE_ADAPTER_MUTATE_AUDIT_PATH FAKE_ADAPTER_MUTATE_AUDIT_TARGET
[[ -L "$deployed_current" && -e "$deployed_current" && -f "$deployed_current/policy.conf" && -f "$deployed_current/state.json" ]] || fail 'post-publication failure left deployed/current dangling'
cmp -s "$deployed_current/policy.conf" "$config" || fail 'published deployed policy does not match the deployed configuration'
rm "$root/var/log/ci-fleet-deployer/audit.log"
write_evidence staging example-staging
cp "$approval" "$request"; chmod 0600 "$request"
export FAKE_ADAPTER_FORBID_REQUEST_PATH=$request
expect_success "$runtime" deploy >/dev/null
unset FAKE_ADAPTER_FORBID_REQUEST_PATH
[[ ! -e "$request" && -f "$root/var/lib/ci-fleet-deployer/last-request.conf" ]] || fail 'completed request was not consumed atomically'
deployed_current=$root/var/lib/ci-fleet-deployer/deployed/current
[[ -L "$deployed_current" && -f "$deployed_current/policy.conf" && -f "$deployed_current/state.json" ]] || fail 'deployed policy/state pair was not published through one pointer'
{ printf '# semantic replay with different bytes\n'; tac "$root/var/lib/ci-fleet-deployer/last-request.conf"; } >"$request"
chmod 0600 "$request"
expect_failure 'deployment request was already completed' "$runtime" deploy >/dev/null
cp "$root/var/lib/ci-fleet-deployer/last-request.conf" "$tmp/request-a"
cp "$approval" "$tmp/approval-a"
python3 - "$approval" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('APPROVAL_ID=approval-20260808-1', 'APPROVAL_ID=approval-20260808-2').replace('APPROVED_AT=2026-08-08T20:00:00Z', 'APPROVED_AT=2026-08-08T20:01:00Z'))
PY
cp "$approval" "$request"; chmod 0600 "$request"
expect_success "$runtime" deploy >/dev/null
cp "$tmp/approval-a" "$approval"; chmod 0600 "$approval"
cp "$tmp/request-a" "$request"; chmod 0600 "$request"
expect_failure 'deployment request was already consumed' "$runtime" deploy >/dev/null
grep -Fq 'sha256:bbbbbbbb' "$root/var/log/ci-fleet-deployer/audit.log" || fail 'audit log omitted the immutable digest'
if grep -Fq 'registry.example.invalid' "$root/var/log/ci-fleet-deployer/audit.log"; then fail 'audit log exposed a private-capable endpoint'; fi
for operation in health cleanup deploy; do grep -Fxq "$operation" "$FAKE_ADAPTER_LOG" || fail "runtime did not invoke adapter $operation"; done
: >"$root/var/lib/ci-fleet-deployer/drained"
chmod 0600 "$root/var/lib/ci-fleet-deployer/drained"
expect_failure 'deployer is drained' "$runtime" deploy >/dev/null
expect_failure 'deployer is drained' "$runtime" cleanup >/dev/null
rm "$root/var/lib/ci-fleet-deployer/drained"
ln -s "$tmp/missing-drain-target" "$root/var/lib/ci-fleet-deployer/drained"
expect_failure 'drain marker must be a regular file, not a symlink' "$runtime" deploy >/dev/null
expect_failure 'drain marker must be a regular file, not a symlink' "$runtime" cleanup >/dev/null

grep -Fq 'DEPLOYER-HOST.md' "$repo_root/docs/README.md" || fail 'operator index does not link the deployer runbook'
[[ -x "$repo_root/scripts/test-deployer-units.sh" ]] || fail 'real systemd unit verification is not wired'
grep -Fq 'scripts/test-deployer-units.sh' "$repo_root/scripts/validate.sh" || fail 'repository validation omits systemd unit verification'
lock_line=$(grep -n 'flock -n 9' "$runtime" | cut -d: -f1)
policy_line=$(grep -n "secure_file \"\$config\" 'deployer configuration'" "$runtime" | cut -d: -f1)
((lock_line < policy_line)) || fail 'runtime loads active policy before acquiring the shared operation lock'
for phrase in '--check' '--install' '--upgrade' '--repair' '--drain' '--resume' '--rollback' '--uninstall' 'manual-exact-head' 'github-environment' 'GitHub Free' 'PRODUCTION_AUTHORIZATION_EVIDENCE_PATH' 'CI_FLEET_DEPLOYER_ROLLBACK_COMMIT' 'application-owned' 'REPORT action='; do
  grep -Fq -- "$phrase" "$repo_root/docs/DEPLOYER-HOST.md" || fail "deployer runbook omits $phrase"
done
for unit in "$repo_root"/deploy/deployer/*; do
  grep -Fq 'ci-fleet-deployer' "$unit" || fail "unit is not deployer-scoped: $unit"
done

printf 'DEPLOYER_INSTALL_TESTS_OK\n'
