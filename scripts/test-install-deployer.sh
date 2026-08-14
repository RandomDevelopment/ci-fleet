#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
installer=$repo_root/scripts/install-deployer.sh
runtime=$repo_root/scripts/deployer-runtime.sh

fail() { printf 'FAIL(line %s): %s\n' "${BASH_LINENO[0]:-?}" "$*" >&2; exit 1; }
expect_success() {
  local output line=${BASH_LINENO[0]}
  output=$("$@" 2>&1) || { printf 'FAIL(line %s): expected success: %s; output=%s\n' "$line" "$*" "$output" >&2; exit 1; }
  printf '%s\n' "$output"
}
expect_failure() {
  local expected=$1 output line=${BASH_LINENO[0]}
  shift
  if output=$("$@" 2>&1); then printf 'FAIL(line %s): expected failure: %s; output=%s\n' "$line" "$*" "$output" >&2; exit 1; fi
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
  is-enabled|is-active)
    [[ -e "$root/var/lib/ci-fleet-deployer/unit-${2:-}" ]] || exit 1
    if [[ $1 == is-enabled ]]; then
      if [[ -e "$root/var/lib/ci-fleet-deployer/unit-${2:-}.runtime" ]]; then printf 'enabled-runtime\n'; else printf '%s\n' "${FAKE_SYSTEMD_IS_ENABLED_OUTPUT:-enabled}"; fi
    fi
    ;;
  enable)
    shift
    runtime_flag=0
    [[ "${1:-}" != --runtime ]] || { runtime_flag=1; shift; }
    [[ "${1:-}" != --now ]] || shift
    for unit in "$@"; do
      if ((runtime_flag)); then : >"$root/var/lib/ci-fleet-deployer/unit-$unit.runtime"; else rm -f "$root/var/lib/ci-fleet-deployer/unit-$unit.runtime"; fi
      : >"$root/var/lib/ci-fleet-deployer/unit-$unit"
    done ;;
  disable)
    shift
    [[ "${1:-}" != --now ]] || shift
    for unit in "$@"; do rm -f "$root/var/lib/ci-fleet-deployer/unit-$unit" "$root/var/lib/ci-fleet-deployer/unit-$unit.runtime"; done ;;
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
if [[ "$1" == rollback && -n ${FAKE_ADAPTER_FAIL_AFTER_MARKER:-} ]]; then exit 42; fi
if [[ -n ${FAKE_ADAPTER_DELETE_CONSUMED_GLOB:-} ]]; then rm -rf $FAKE_ADAPTER_DELETE_CONSUMED_GLOB; fi
if [[ -n ${FAKE_ADAPTER_MUTATE_AUDIT_PATH:-} ]]; then
  if [[ ${FAKE_ADAPTER_MUTATE_AUDIT_MODE:-symlink} == unlink ]]; then
    rm -f "$FAKE_ADAPTER_MUTATE_AUDIT_PATH"; printf 'adapter-replacement\n' >"$FAKE_ADAPTER_MUTATE_AUDIT_PATH"
  else
    rm -f "$FAKE_ADAPTER_MUTATE_AUDIT_PATH"
    ln -s "$FAKE_ADAPTER_MUTATE_AUDIT_TARGET" "$FAKE_ADAPTER_MUTATE_AUDIT_PATH"
  fi
fi
if [[ -n ${FAKE_ADAPTER_CHMOD_DURING:-} ]]; then chmod 0644 "$FAKE_ADAPTER_CHMOD_DURING"; fi
if [[ -n ${FAKE_ADAPTER_CHOWN_DURING:-} && $EUID == 0 ]]; then chown 65534 "$FAKE_ADAPTER_CHOWN_DURING"; fi
if [[ "$1" == deploy && -n ${FAKE_ADAPTER_MUTATE_INCUMBENT_PATH:-} ]]; then
  chmod 0755 "$FAKE_ADAPTER_MUTATE_INCUMBENT_PATH"
  chmod 0644 "$FAKE_ADAPTER_MUTATE_INCUMBENT_PATH"/policy.conf "$FAKE_ADAPTER_MUTATE_INCUMBENT_PATH"/state.json
  if ((EUID == 0)); then chown 65534 "$FAKE_ADAPTER_MUTATE_INCUMBENT_PATH" "$FAKE_ADAPTER_MUTATE_INCUMBENT_PATH"/policy.conf "$FAKE_ADAPTER_MUTATE_INCUMBENT_PATH"/state.json; fi
fi
if [[ "$1" == deploy && -n ${FAKE_ADAPTER_MUTATE_LKG_ROOT:-} ]]; then
  chmod 0644 "$FAKE_ADAPTER_MUTATE_LKG_ROOT"/last-known-good.json "$FAKE_ADAPTER_MUTATE_LKG_ROOT"/last-known-good-policy.conf
  if ((EUID == 0)); then chown 65534 "$FAKE_ADAPTER_MUTATE_LKG_ROOT"/last-known-good.json "$FAKE_ADAPTER_MUTATE_LKG_ROOT"/last-known-good-policy.conf; fi
fi
if [[ "$1" == deploy && -n ${FAKE_ADAPTER_MUTATE_SNAPSHOT_ROOT:-} ]]; then
  snapshot=$(find "$FAKE_ADAPTER_MUTATE_SNAPSHOT_ROOT" -maxdepth 1 -type d -name '.snapshot.*' -printf '%T@ %p\n' | sort -nr | head -n1 | cut -d' ' -f2-)
  [[ -n "$snapshot" ]] && printf 'adapter-mutation\n' >>"$snapshot/policy.conf"
fi
if [[ "$1" == deploy && -n ${FAKE_ADAPTER_SIGNAL_PPID:-} ]]; then kill -TERM "$PPID"; sleep 5; fi
if [[ -e "${FAKE_ADAPTER_FAIL_AFTER_MUTATION:-/nonexistent}" ]]; then exit 42; fi
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
# snapshot_state <tx-dir>: record the full current managed state into a
# recovery-transaction fixture so restoration returns to a valid installation.
snapshot_state() {
  local tx=$1 name
  for name in install-state.json active-policy.conf last-known-good.json last-known-good-policy.conf; do
    [[ ! -e "$root/var/lib/ci-fleet-deployer/$name" ]] || { cp "$root/var/lib/ci-fleet-deployer/$name" "$tx/state/$name"; printf '%s\n' "$name" >>"$tx/state-present"; }
  done
  printf '%s\n' "$(readlink "$root/opt/ci-fleet-deployer/current")" >"$tx/current-target"
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
ENVIRONMENT=staging
TARGET_ID=example-staging
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
ENVIRONMENT=staging
TARGET_ID=example-staging
ENVIRONMENT_PROTECTION=verified
EXACT_HEAD=$source_ref
CAPABILITY_ID=example-capability-check
CHECKED_AT=2026-08-08T20:00:00Z
EOF
chmod 0600 "$capability"
# Capability evidence bound to a different installation must be rejected.
cp "$capability" "$tmp/capability.saved"
python3 - "$capability" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('TARGET_ID=example-staging', 'TARGET_ID=example-other'))
PY
expect_failure 'capability evidence does not match this installation' "$installer" --check --config "$config" >/dev/null
install -m 0600 "$tmp/capability.saved" "$capability"
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
expect_failure 'managed systemd unit has an unsafe owner, mode, or type' "$installer" --repair --config "$config" >/dev/null
chmod 0644 "$root/etc/systemd/system/ci-fleet-deployer.service"
expect_success "$installer" --repair --config "$config" >/dev/null
[[ $(stat -c %a "$root/etc/systemd/system/ci-fleet-deployer.service") == 644 ]] || fail 'repair did not restore unit mode 0644'

# Repair must replace a damaged active release from the validated checkout.
printf 'truncated\n' >"$root/opt/ci-fleet-deployer/releases/$core_ref/scripts/deployer-runtime.sh"
expect_failure 'installed deployer state is absent or drifted' "$installer" --check --config "$config" >/dev/null
expect_success "$installer" --repair --config "$config" >/dev/null
cmp -s "$repo_root/scripts/deployer-runtime.sh" "$root/opt/ci-fleet-deployer/releases/$core_ref/scripts/deployer-runtime.sh" || fail 'repair did not replace the damaged release with reviewed bytes'
expect_success "$installer" --check --config "$config" >/dev/null

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

# An unsafe transaction manifest must fail closed before any recovery mutation.
unsafe_tx=$root/var/lib/ci-fleet-deployer/.transaction.unsafe
mkdir -m 0700 "$unsafe_tx" "$unsafe_tx/units" "$unsafe_tx/state"
cp "$root/var/lib/ci-fleet-deployer/install-state.json" "$unsafe_tx/state/install-state.json"
printf 'install-state.json\n' >"$unsafe_tx/state-present"
printf 'rogue-unit.service\n' >"$unsafe_tx/units-present"
units_before_unsafe=$(find "$root/etc/systemd/system" -mindepth 1 -maxdepth 1 -printf '%P %y\n' | sort | sha256sum)
expect_failure 'transaction unit manifest is unsafe' "$installer" --repair --config "$config" >/dev/null
[[ "$units_before_unsafe" == "$(find "$root/etc/systemd/system" -mindepth 1 -maxdepth 1 -printf '%P %y\n' | sort | sha256sum)" ]] || fail 'unsafe transaction manifest mutated installed units'
[[ -L "$root/opt/ci-fleet-deployer/current" ]] || fail 'unsafe transaction manifest removed the activation pointer'
rm "$unsafe_tx/units-present"
printf 'not-a-release\n' >"$unsafe_tx/current-target"
expect_failure 'transaction current pointer is unsafe' "$installer" --repair --config "$config" >/dev/null
[[ -L "$root/opt/ci-fleet-deployer/current" ]] || fail 'unsafe transaction pointer removed the activation pointer'
rm "$unsafe_tx/current-target"
mkdir "$unsafe_tx/timers-enabled"
expect_failure 'transaction manifest timers-enabled has an unsafe type' "$installer" --repair --config "$config" >/dev/null
[[ -L "$root/opt/ci-fleet-deployer/current" ]] || fail 'unsafe manifest type removed the activation pointer'
rmdir "$unsafe_tx/timers-enabled"
expect_success "$installer" --repair --config "$config" >/dev/null
[[ ! -e "$unsafe_tx" ]] || fail 'corrected transaction was not recovered'
expect_success "$installer" --check --config "$config" >/dev/null

# A truncated or drifted transaction manifest must block before any recovery mutation.
truncated_tx=$root/var/lib/ci-fleet-deployer/.transaction.truncated
mkdir -m 0700 "$truncated_tx" "$truncated_tx/units" "$truncated_tx/state"
for name in install-state.json active-policy.conf; do
  cp "$root/var/lib/ci-fleet-deployer/$name" "$truncated_tx/state/$name"
  printf '%s\n' "$name" >>"$truncated_tx/state-present"
done
sed -i '$d' "$truncated_tx/state-present"
state_before_truncated=$(find "$root/var/lib/ci-fleet-deployer" -mindepth 1 -maxdepth 1 -printf '%P %y\n' | sort | sha256sum)
expect_failure 'transaction state manifest does not match its backup directory' "$installer" --repair --config "$config" >/dev/null
[[ "$state_before_truncated" == "$(find "$root/var/lib/ci-fleet-deployer" -mindepth 1 -maxdepth 1 -printf '%P %y\n' | sort | sha256sum)" ]] || fail 'truncated transaction manifest mutated managed state'
printf 'active-policy.conf\n' >>"$truncated_tx/state-present"
cp "$root/etc/systemd/system/ci-fleet-deployer.service" "$truncated_tx/units/ci-fleet-deployer.service"
expect_failure 'transaction units manifest is missing but backups remain' "$installer" --repair --config "$config" >/dev/null
rm -rf -- "$truncated_tx"
expect_success "$installer" --check --config "$config" >/dev/null

# Transaction recovery must restore runtime-only timer enablement exactly.
runtime_tx=$root/var/lib/ci-fleet-deployer/.transaction.runtime-enabled
mkdir -m 0700 "$runtime_tx" "$runtime_tx/units" "$runtime_tx/state"
for name in install-state.json active-policy.conf last-known-good.json last-known-good-policy.conf; do
  [[ ! -e "$root/var/lib/ci-fleet-deployer/$name" ]] || { cp "$root/var/lib/ci-fleet-deployer/$name" "$runtime_tx/state/$name"; printf '%s\n' "$name" >>"$runtime_tx/state-present"; }
done
printf '%s\n' "$(readlink "$root/opt/ci-fleet-deployer/current")" >"$runtime_tx/current-target"
: >"$root/var/lib/ci-fleet-deployer/unit-ci-fleet-deployer-health.timer.runtime"
printf 'ci-fleet-deployer-health.timer\n' >"$runtime_tx/timers-enabled-runtime"
expect_success "$installer" --repair --config "$config" >/dev/null
grep -Fq 'enable --runtime ci-fleet-deployer-health.timer' "$FAKE_SYSTEMCTL_LOG" || fail 'recovery did not restore runtime-only enablement'
rm -f "$root/var/lib/ci-fleet-deployer/unit-ci-fleet-deployer-health.timer.runtime"
expect_success "$installer" --repair --config "$config" >/dev/null
expect_success "$installer" --check --config "$config" >/dev/null

# Recovery must remove a snapshot published after the transaction began.
created_tx=$root/var/lib/ci-fleet-deployer/.transaction.created-snapshot
mkdir -m 0700 "$created_tx" "$created_tx/units" "$created_tx/state"
for name in install-state.json active-policy.conf last-known-good.json last-known-good-policy.conf; do
  [[ ! -e "$root/var/lib/ci-fleet-deployer/$name" ]] || { cp "$root/var/lib/ci-fleet-deployer/$name" "$created_tx/state/$name"; printf '%s\n' "$name" >>"$created_tx/state-present"; }
done
for path in "$root"/etc/systemd/system/ci-fleet-deployer*; do
  name=${path##*/}; cp "$path" "$created_tx/units/$name"; printf '%s\n' "$name" >>"$created_tx/units-present"
done
printf '%s\n' "$(readlink "$root/opt/ci-fleet-deployer/current")" >"$created_tx/current-target"
printf '%s\n' ci-fleet-deployer-health.timer ci-fleet-deployer-cleanup.timer >"$created_tx/timers-enabled"
orphan_snapshot=$root/var/lib/ci-fleet-deployer/deployed/.snapshot.orphaned
mkdir -m 0700 "$orphan_snapshot"
install -m 0600 "$root/var/lib/ci-fleet-deployer/active-policy.conf" "$orphan_snapshot/policy.conf"
install -m 0600 "$root/var/lib/ci-fleet-deployer/install-state.json" "$orphan_snapshot/state.json"
current_deployed=$(readlink "$root/var/lib/ci-fleet-deployer/deployed/current")
printf '%s\n' "$current_deployed" >"$created_tx/deployed-target"
printf '.snapshot.orphaned\n' >"$created_tx/deployed-created"
rm "$root/var/lib/ci-fleet-deployer/deployed/current"
ln -s .snapshot.orphaned "$root/var/lib/ci-fleet-deployer/deployed/current"
deployed_count_before=$(find "$root/var/lib/ci-fleet-deployer/deployed" -mindepth 1 -maxdepth 1 -name '.snapshot.*' -type d | wc -l)
expect_success "$installer" --repair --config "$config" >/dev/null
[[ ! -e "$orphan_snapshot" ]] || fail 'recovery retained a snapshot created after the transaction began'
[[ $(find "$root/var/lib/ci-fleet-deployer/deployed" -mindepth 1 -maxdepth 1 -name '.snapshot.*' -type d | wc -l) == $((deployed_count_before - 1)) ]] || fail 'recovery removed the wrong snapshot'
[[ $(readlink "$root/var/lib/ci-fleet-deployer/deployed/current") == "$current_deployed" ]] || fail 'recovery did not restore the prior deployed pointer'
expect_success "$installer" --check --config "$config" >/dev/null

# Transaction recovery must tolerate timers whose unit files were never installed.
absent_timer_tx=$root/var/lib/ci-fleet-deployer/.transaction.absent-timer
mkdir -m 0700 "$absent_timer_tx" "$absent_timer_tx/units" "$absent_timer_tx/state"
snapshot_state "$absent_timer_tx"
mv "$root/etc/systemd/system/ci-fleet-deployer-cleanup.timer" "$tmp/cleanup.timer.saved"
expect_success "$installer" --repair --config "$config" >/dev/null
[[ ! -e "$absent_timer_tx" ]] || fail 'absent-timer transaction was not recovered'
expect_success "$installer" --repair --config "$config" >/dev/null
expect_success "$installer" --check --config "$config" >/dev/null

# Read-only checks must not create checkout snapshots inside managed state.
if compgen -G "$root/var/lib/ci-fleet-deployer/.checkout.*" >/dev/null; then fail 'read-only check left a checkout snapshot in managed state'; fi

# A noncanonical activation pointer must fail convergence instead of certifying it.
rm "$root/opt/ci-fleet-deployer/current"
ln -s "$root/opt/ci-fleet-deployer/releases/$core_ref" "$root/opt/ci-fleet-deployer/current"
expect_failure 'installed deployer state is absent or drifted' "$installer" --check --config "$config" >/dev/null
rm "$root/opt/ci-fleet-deployer/current"
ln -s "releases/$core_ref" "$root/opt/ci-fleet-deployer/current"
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

# A symlinked releases boundary must block before any staging cleanup deletion.
mv "$root/opt/ci-fleet-deployer/releases" "$tmp/releases.real"
ln -s "$tmp/releases.real" "$root/opt/ci-fleet-deployer/releases"
mkdir -m 0755 "$tmp/releases.real/.${core_ref}.staging.decoy"
expect_failure 'unsafe symlinked managed directory' "$installer" --repair --config "$config" >/dev/null
[[ -d "$tmp/releases.real/.${core_ref}.staging.decoy" ]] || fail 'symlinked releases boundary allowed staging deletion'
rm "$root/opt/ci-fleet-deployer/releases"
mv "$tmp/releases.real" "$root/opt/ci-fleet-deployer/releases"
rmdir "$root/opt/ci-fleet-deployer/releases/.${core_ref}.staging.decoy"
expect_success "$installer" --check --config "$config" >/dev/null

# A runtime-only timer enablement must not satisfy convergence.
expect_success "$installer" --check --config "$config" >/dev/null
export FAKE_SYSTEMD_IS_ENABLED_OUTPUT=enabled-runtime
expect_failure 'installed deployer state is absent or drifted' "$installer" --check --config "$config" >/dev/null
unset FAKE_SYSTEMD_IS_ENABLED_OUTPUT
expect_success "$installer" --check --config "$config" >/dev/null

# A missing deployed rollback snapshot must break convergence before resume.
deployed_target=$(readlink "$root/var/lib/ci-fleet-deployer/deployed/current")
rm "$root/var/lib/ci-fleet-deployer/deployed/current"
expect_failure 'installed deployer state is absent or drifted' "$installer" --check --config "$config" >/dev/null
ln -s "$deployed_target" "$root/var/lib/ci-fleet-deployer/deployed/current"
expect_success "$installer" --check --config "$config" >/dev/null
printf 'pid=%s\nstarted_at=1\n' "$$" >"$root/var/lib/ci-fleet-deployer/active-operation"
chmod 0600 "$root/var/lib/ci-fleet-deployer/active-operation"
expect_success "$installer" --repair --config "$config" >/dev/null
[[ ! -e "$root/var/lib/ci-fleet-deployer/active-operation" ]] || fail 'old operation marker survived PID reuse'
printf 'pid=%s\nstarted_at=1\n' "$$" >"$root/var/lib/ci-fleet-deployer/active-operation"
chmod 0600 "$root/var/lib/ci-fleet-deployer/active-operation"
CI_FLEET_DEPLOYER_TEST_LIVE_PID=$$ expect_failure 'active deployment prevents this operation' "$installer" --repair --config "$config" >/dev/null
CI_FLEET_DEPLOYER_TEST_LIVE_PID=$$ expect_failure 'active deployment prevents this operation' "$installer" --upgrade --config "$config" >/dev/null
[[ -f "$root/var/lib/ci-fleet-deployer/active-operation" ]] || fail 'live stale-aged operation marker was expired'
rm "$root/var/lib/ci-fleet-deployer/active-operation"

# An unsafe active-operation marker type must fail checks, not certify them.
mkdir "$root/var/lib/ci-fleet-deployer/active-operation"
expect_failure 'active operation marker has an unsafe type' "$installer" --check --config "$config" >/dev/null
expect_failure 'active operation marker has an unsafe type' "$installer" --repair --config "$config" >/dev/null
rmdir "$root/var/lib/ci-fleet-deployer/active-operation"
expect_success "$installer" --check --config "$config" >/dev/null

# A stale marker for a dead process must fail read-only checks until a mutating recovery removes it.
printf 'pid=999999\nstarted_at=1\n' >"$root/var/lib/ci-fleet-deployer/active-operation"
chmod 0600 "$root/var/lib/ci-fleet-deployer/active-operation"
expect_failure 'stale operation marker requires a mutating recovery' "$installer" --check --config "$config" >/dev/null
expect_success "$installer" --repair --config "$config" >/dev/null
[[ ! -e "$root/var/lib/ci-fleet-deployer/active-operation" ]] || fail 'repair retained the stale operation marker'
expect_success "$installer" --check --config "$config" >/dev/null

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
expect_failure 'managed systemd unit has an unsafe owner, mode, or type' "$installer" --repair --config "$config" >/dev/null
[[ $(<"$tmp/unrelated-unit") == unrelated-unit ]] || fail 'systemd unit symlink attack changed an unrelated file'
rm "$unit_path"; mv "$unit_path.real" "$unit_path"

printf 'mixed-role\n' >"$root/etc/systemd/system/ci-fleet-health.service"
expect_failure 'ordinary CI controller or runner state is present' "$installer" --check --config "$config" >/dev/null
rm "$root/etc/systemd/system/ci-fleet-health.service"
printf 'mixed-role\n' >"$root/etc/systemd/system/ci-fleet-drift.timer"
expect_failure 'ordinary CI controller or runner state is present' "$installer" --check --config "$config" >/dev/null
rm "$root/etc/systemd/system/ci-fleet-drift.timer"
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

# Candidate validate must revalidate credentials drifted after configuration validation.
export FAKE_ADAPTER_CHMOD_DURING=$credential
expect_failure 'candidate policy credential file must be owner-only mode 0600' "$installer" --upgrade --config "$config" >/dev/null
unset FAKE_ADAPTER_CHMOD_DURING
chmod 0600 "$credential"
[[ "$old_state" == "$(sha256sum "$root/var/lib/ci-fleet-deployer/install-state.json")" ]] || fail 'credential-drifted candidate replaced healthy state'

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

# Failed active-policy health checks must not accumulate policy-check snapshots.
compgen -G "$root/var/lib/ci-fleet-deployer/.policy-check.*" >/dev/null && fail 'policy-check snapshot leaked before drifted-health regression'
export FAKE_ADAPTER_FAIL=$tmp/fail-adapter-health
printf 'health\n' >"$FAKE_ADAPTER_FAIL"
expect_failure 'active deployer health check failed' "$installer" --check --config "$config" >/dev/null
expect_failure 'active deployer health check failed' "$installer" --check --config "$config" >/dev/null
unset FAKE_ADAPTER_FAIL; rm "$tmp/fail-adapter-health"
compgen -G "$root/var/lib/ci-fleet-deployer/.policy-check.*" >/dev/null && fail 'failed health validation leaked policy-check snapshots'
expect_success "$installer" --check --config "$config" >/dev/null

# Deployed policy drift in an operational field must block promotion.
deployed_policy=$(readlink -f "$root/var/lib/ci-fleet-deployer/deployed/current")/policy.conf
python3 - "$deployed_policy" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('CREDENTIAL_PROVIDER=file', 'CREDENTIAL_PROVIDER=external'))
PY
expect_failure 'deployed rollback policy has an invalid external secret-manager adapter reference' "$installer" --upgrade --config "$config" >/dev/null
python3 - "$deployed_policy" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('CREDENTIAL_PROVIDER=external', 'CREDENTIAL_PROVIDER=file'))
PY
expect_success "$installer" --check --config "$config" >/dev/null

printf 'rollback\n' >"$tmp/fail-rollback"
# A candidate never deployed must not be a rollback target.
expect_failure 'no completed deployment is available to roll back to' "$installer" --rollback --config "$config" >/dev/null
# Rollback requires a completed deployment; simulate one for the rollback battery.
install -m 0600 /dev/null "$root/var/lib/ci-fleet-deployer/last-request.conf"
export FAKE_ADAPTER_FAIL=$tmp/fail-rollback
expect_failure 'application adapter rollback failed' "$installer" --rollback --config "$config" >/dev/null
unset FAKE_ADAPTER_FAIL; rm "$tmp/fail-rollback"
grep -Fq 'sha256:bbbbbbbb' "$root/var/lib/ci-fleet-deployer/install-state.json" || fail 'failed application rollback did not restore current core state'

# An adapter that commits the rollback marker but exits nonzero must not be
# reported as an unchanged failure: recovery finalizes the committed rollback.
marker_rollback=$(FAKE_ADAPTER_FAIL_AFTER_MARKER=1 "$installer" --rollback --config "$config" 2>&1) || true
printf '%s\n' "$marker_rollback" | grep -Fq 'result=CHANGED' || fail 'marker-committed rollback was reported as an unchanged failure'
printf '%s\n' "$marker_rollback" | grep -Fq 'next=restore-host-policy-evidence-then-check' || fail 'marker-committed rollback lacks the operator reconciliation action'
grep -Fq 'sha256:aaaaaaaa' "$root/var/lib/ci-fleet-deployer/install-state.json" || fail 'marker-committed rollback did not restore last-known-good state'
expect_success "$installer" --repair --config "$config" >/dev/null
expect_success "$installer" --check --config "$config" >/dev/null
# Rebuild the retained pair consumed by the marker-committed rollback.
image='registry.example.invalid/example/app@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
write_evidence
write_config
expect_success "$installer" --upgrade --config "$config" >/dev/null
image='registry.example.invalid/example/app@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
write_evidence
write_config
expect_success "$installer" --upgrade --config "$config" >/dev/null
expect_success "$installer" --check --config "$config" >/dev/null
[[ -f "$root/var/lib/ci-fleet-deployer/last-known-good.json" ]] || fail 'retained rollback pair was not rebuilt'
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

# An unsafe rollback commit marker type must block before recovery mutation.
recovery_transaction=$root/var/lib/ci-fleet-deployer/.transaction.marker-type
mkdir -m 0700 "$recovery_transaction" "$recovery_transaction/units" "$recovery_transaction/state"
mkdir "$recovery_transaction/application-rollback-committed"
units_before_marker=$(find "$root/etc/systemd/system" -mindepth 1 -maxdepth 1 -printf '%P %y\n' | sort | sha256sum)
policy_before_marker=$(sha256sum "$root/var/lib/ci-fleet-deployer/active-policy.conf")
expect_failure 'application rollback commit marker is unsafe' "$installer" --repair --config "$config" >/dev/null
[[ $units_before_marker == "$(find "$root/etc/systemd/system" -mindepth 1 -maxdepth 1 -printf '%P %y\n' | sort | sha256sum)" && $policy_before_marker == "$(sha256sum "$root/var/lib/ci-fleet-deployer/active-policy.conf")" ]] || fail 'directory rollback marker mutated managed state'
[[ -d "$recovery_transaction" ]] || fail 'blocked rollback marker transaction was discarded'
rmdir "$recovery_transaction/application-rollback-committed"
ln -s "$tmp/missing-rollback-target" "$recovery_transaction/application-rollback-committed"
expect_failure 'application rollback commit marker is unsafe' "$installer" --repair --config "$config" >/dev/null
[[ $units_before_marker == "$(find "$root/etc/systemd/system" -mindepth 1 -maxdepth 1 -printf '%P %y\n' | sort | sha256sum)" && $policy_before_marker == "$(sha256sum "$root/var/lib/ci-fleet-deployer/active-policy.conf")" ]] || fail 'broken-symlink rollback marker mutated managed state'
rm -rf -- "$recovery_transaction"
expect_success "$installer" --check --config "$config" >/dev/null

# Finalizing a committed rollback must publish over an unusable incumbent deployed snapshot.
deployed_dir=$(readlink -f "$root/var/lib/ci-fleet-deployer/deployed/current")
cp "$deployed_dir/policy.conf" "$tmp/incumbent-policy.saved"
python3 - "$deployed_dir/policy.conf" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('ADAPTER_SHA256=', 'ADAPTER_SHA256=' + 'e'*64 + '\n#', 1))
PY
recovery_transaction=$root/var/lib/ci-fleet-deployer/.transaction.unusable-incumbent
mkdir -m 0700 "$recovery_transaction" "$recovery_transaction/units" "$recovery_transaction/state"
for name in install-state.json active-policy.conf last-known-good.json last-known-good-policy.conf; do
  [[ ! -e "$root/var/lib/ci-fleet-deployer/$name" ]] || { cp "$root/var/lib/ci-fleet-deployer/$name" "$recovery_transaction/state/$name"; printf '%s\n' "$name" >>"$recovery_transaction/state-present"; }
done
printf '%s\n' "$(readlink "$root/opt/ci-fleet-deployer/current")" >"$recovery_transaction/current-target"
install -m 0600 /dev/null "$recovery_transaction/application-rollback-committed"
recovery=$(expect_success "$installer" --upgrade --config "$config")
grep -Fq 'next=restore-host-policy-evidence-then-check' <<<"$recovery" || fail 'unusable-incumbent recovery lacks the operator reconciliation action'
[[ ! -e "$recovery_transaction" ]] || fail 'unusable-incumbent recovery retained its transaction'
expect_success "$installer" --repair --config "$config" >/dev/null
expect_success "$installer" --check --config "$config" >/dev/null

# A deployed rollback pair whose adapter bytes no longer match its recorded digest must not promote.
# Rebuild the retained pair consumed by the unusable-incumbent recovery fixture.
image='registry.example.invalid/example/app@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
write_evidence
write_config
expect_success "$installer" --upgrade --config "$config" >/dev/null
image='registry.example.invalid/example/app@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
write_evidence
write_config
expect_success "$installer" --upgrade --config "$config" >/dev/null
expect_success "$installer" --check --config "$config" >/dev/null
[[ -f "$root/var/lib/ci-fleet-deployer/last-known-good.json" ]] || fail 'retained rollback pair was not rebuilt'
deployed_dir=$(readlink -f "$root/var/lib/ci-fleet-deployer/deployed/current")
deployed_target=$(readlink "$root/var/lib/ci-fleet-deployer/deployed/current")
cp "$deployed_dir/policy.conf" "$tmp/deployed-policy.saved"
python3 - "$deployed_dir/policy.conf" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('ADAPTER_SHA256=', 'ADAPTER_SHA256=' + 'f'*64 + '\n#', 1))
PY
if [[ -f "$root/var/lib/ci-fleet-deployer/last-known-good.json" ]]; then lkg_before_digest=$(sha256sum "$root/var/lib/ci-fleet-deployer/last-known-good.json"); else lkg_before_digest=absent; fi
expect_failure 'deployed rollback adapter digest does not match its snapshot policy' "$installer" --upgrade --config "$config" >/dev/null
lkg_after_digest=absent; [[ ! -f "$root/var/lib/ci-fleet-deployer/last-known-good.json" ]] || lkg_after_digest=$(sha256sum "$root/var/lib/ci-fleet-deployer/last-known-good.json"); [[ $lkg_before_digest == "$lkg_after_digest" ]] || fail 'digest-mismatched deployed pair replaced last-known-good'
if compgen -G "$root/var/lib/ci-fleet-deployer/.transaction.*" >/dev/null; then fail 'blocked digest promotion left a recovery transaction'; fi
# Rollback intentionally publishes the retained pair over the unusable
# incumbent snapshot: the drifted deployed pair is retired, never promoted.
rollback=$(expect_success "$installer" --rollback --config "$config")
grep -Fq 'next=restore-host-policy-evidence-then-check' <<<"$rollback" || fail 'drifted-incumbent rollback lacks the operator reconciliation action'
[[ ! -e "$deployed_dir" ]] || fail 'drifted deployed snapshot survived its rollback retirement'
expect_success "$installer" --repair --config "$config" >/dev/null
expect_success "$installer" --check --config "$config" >/dev/null
# Rebuild the retained pair consumed by the drifted-incumbent rollback.
image='registry.example.invalid/example/app@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
write_evidence
write_config
expect_success "$installer" --upgrade --config "$config" >/dev/null
image='registry.example.invalid/example/app@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
write_evidence
write_config
expect_success "$installer" --upgrade --config "$config" >/dev/null
expect_success "$installer" --check --config "$config" >/dev/null
[[ -f "$root/var/lib/ci-fleet-deployer/last-known-good.json" ]] || fail 'retained rollback pair was not rebuilt'

# A deployed rollback pair whose credential reference drifted must not promote.
deployed_dir=$(readlink -f "$root/var/lib/ci-fleet-deployer/deployed/current")
cp "$deployed_dir/policy.conf" "$tmp/deployed-policy-cred.saved"
python3 - "$deployed_dir/policy.conf" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('CREDENTIAL_PROVIDER=file', 'CREDENTIAL_PROVIDER=external').replace(next(x for x in p.read_text().splitlines() if x.startswith('CREDENTIAL_REF=')), 'CREDENTIAL_REF=external:bad'))
PY
if [[ -f "$root/var/lib/ci-fleet-deployer/last-known-good.json" ]]; then lkg_before_credential=$(sha256sum "$root/var/lib/ci-fleet-deployer/last-known-good.json"); else lkg_before_credential=absent; fi
expect_failure 'deployed rollback policy has an invalid external secret-manager adapter reference' "$installer" --upgrade --config "$config" >/dev/null
lkg_after_credential=absent; [[ ! -f "$root/var/lib/ci-fleet-deployer/last-known-good.json" ]] || lkg_after_credential=$(sha256sum "$root/var/lib/ci-fleet-deployer/last-known-good.json"); [[ $lkg_before_credential == "$lkg_after_credential" ]] || fail 'credential-drifted deployed pair replaced last-known-good'
# Rollback intentionally publishes the retained pair over the unusable
# incumbent snapshot: the credential-drifted pair is retired, never promoted.
rollback=$(expect_success "$installer" --rollback --config "$config")
grep -Fq 'next=restore-host-policy-evidence-then-check' <<<"$rollback" || fail 'credential-drifted rollback lacks the operator reconciliation action'
[[ ! -e "$deployed_dir" ]] || fail 'credential-drifted deployed snapshot survived its rollback retirement'
expect_success "$installer" --repair --config "$config" >/dev/null
expect_success "$installer" --check --config "$config" >/dev/null

# Finalizing a committed rollback with drifted adapter bytes must not publish or delete retained state.
recovery_transaction=$root/var/lib/ci-fleet-deployer/.transaction.drifted-adapter
rm -rf "$root/var/lib/ci-fleet-deployer/deployed"
mkdir -m 0700 "$recovery_transaction" "$recovery_transaction/units" "$recovery_transaction/state"
for name in install-state.json active-policy.conf last-known-good.json last-known-good-policy.conf; do
  [[ ! -e "$root/var/lib/ci-fleet-deployer/$name" ]] || { cp "$root/var/lib/ci-fleet-deployer/$name" "$recovery_transaction/state/$name"; printf '%s\n' "$name" >>"$recovery_transaction/state-present"; }
done
printf '%s\n' "$(readlink "$root/opt/ci-fleet-deployer/current")" >"$recovery_transaction/current-target"
install -m 0600 /dev/null "$recovery_transaction/application-rollback-committed"
cp "$adapter" "$tmp/adapter.saved"
printf '# drifted\n' >>"$adapter"
if [[ -f "$root/var/lib/ci-fleet-deployer/last-known-good.json" ]]; then lkg_before_finalize=$(sha256sum "$root/var/lib/ci-fleet-deployer/last-known-good.json"); else lkg_before_finalize=absent; fi
expect_failure 'adapter digest does not match the protected regular file' "$installer" --repair --config "$config" >/dev/null
[[ -d "$recovery_transaction" ]] || fail 'failed finalize discarded its recovery transaction'
lkg_after_finalize=absent; [[ ! -f "$root/var/lib/ci-fleet-deployer/last-known-good.json" ]] || lkg_after_finalize=$(sha256sum "$root/var/lib/ci-fleet-deployer/last-known-good.json")
[[ $lkg_before_finalize == "$lkg_after_finalize" ]] || fail 'failed finalize deleted the retained rollback pair'
cat "$tmp/adapter.saved" >"$adapter"
expect_success "$installer" --repair --config "$config" >/dev/null
expect_success "$installer" --repair --config "$config" >/dev/null
expect_success "$installer" --check --config "$config" >/dev/null

# A symlinked systemd boundary must block before transaction recovery deletes through it.
rm "$root/etc/systemd/system/ci-fleet-deployer-cleanup.timer"
boundary_tx=$root/var/lib/ci-fleet-deployer/.transaction.boundary
mkdir -m 0700 "$boundary_tx" "$boundary_tx/units" "$boundary_tx/state"
mv "$root/etc/systemd/system" "$tmp/systemd.real"
ln -s "$tmp/systemd.real" "$root/etc/systemd/system"
printf 'decoy\n' >"$tmp/systemd.real/ci-fleet-deployer.service"
expect_failure 'systemd unit directory has an unsafe owner or type' "$installer" --repair --config "$config" >/dev/null
[[ $(<"$tmp/systemd.real/ci-fleet-deployer.service") == decoy ]] || fail 'recovery deleted through a symlinked systemd boundary'
[[ -d "$boundary_tx" ]] || fail 'blocked boundary recovery discarded its transaction'
rm "$root/etc/systemd/system"
mv "$tmp/systemd.real" "$root/etc/systemd/system"
rm "$root/etc/systemd/system/ci-fleet-deployer.service"
rm -rf -- "$boundary_tx"
expect_success "$installer" --repair --config "$config" >/dev/null
expect_success "$installer" --check --config "$config" >/dev/null

# A group- or world-writable systemd boundary must block before transaction backups.
rm "$root/etc/systemd/system/ci-fleet-deployer-cleanup.timer"
chmod 0777 "$root/etc/systemd/system"
units_before_writable=$(find "$root/etc/systemd/system" -mindepth 1 -maxdepth 1 -printf '%P %y %m\n' | sort | sha256sum)
expect_failure 'systemd unit directory is group- or world-writable' "$installer" --repair --config "$config" >/dev/null
[[ $units_before_writable == "$(find "$root/etc/systemd/system" -mindepth 1 -maxdepth 1 -printf '%P %y %m\n' | sort | sha256sum)" ]] || fail 'writable systemd boundary allowed a transaction backup'
boundary_tx=$root/var/lib/ci-fleet-deployer/.transaction.writable-recovery
mkdir -m 0700 "$boundary_tx" "$boundary_tx/units" "$boundary_tx/state"
expect_failure 'systemd unit directory is group- or world-writable' "$installer" --repair --config "$config" >/dev/null
[[ $units_before_writable == "$(find "$root/etc/systemd/system" -mindepth 1 -maxdepth 1 -printf '%P %y %m\n' | sort | sha256sum)" ]] || fail 'writable systemd boundary allowed a recovery restore'
rm -rf -- "$boundary_tx"
chmod 0755 "$root/etc/systemd/system"
expect_success "$installer" --repair --config "$config" >/dev/null
expect_success "$installer" --check --config "$config" >/dev/null

# A symlinked install root must block before recovery restores pointers.
boundary_tx=$root/var/lib/ci-fleet-deployer/.transaction.install-boundary
mkdir -m 0700 "$boundary_tx" "$boundary_tx/units" "$boundary_tx/state"
mv "$root/opt/ci-fleet-deployer" "$tmp/install-root.real"
ln -s "$tmp/install-root.real" "$root/opt/ci-fleet-deployer"
expect_failure 'unsafe symlinked managed directory' "$installer" --repair --config "$config" >/dev/null
[[ -L "$tmp/install-root.real/current" ]] || fail 'symlinked install boundary mutated the activation pointer'
rm "$root/opt/ci-fleet-deployer"
mv "$tmp/install-root.real" "$root/opt/ci-fleet-deployer"
rm -rf -- "$boundary_tx"
expect_success "$installer" --repair --config "$config" >/dev/null
expect_success "$installer" --check --config "$config" >/dev/null

# A managed-unit drop-in must break convergence until removed.
mkdir "$root/etc/systemd/system/ci-fleet-deployer.service.d"
printf '[Service]\nExecStart=\n' >"$root/etc/systemd/system/ci-fleet-deployer.service.d/override.conf"
expect_failure 'installed deployer state is absent or drifted' "$installer" --check --config "$config" >/dev/null
expect_failure 'has an unreviewed drop-in override' "$installer" --repair --config "$config" >/dev/null
[[ -f "$root/etc/systemd/system/ci-fleet-deployer.service.d/override.conf" ]] || fail 'repair discarded an unmanaged drop-in'
rm -rf "$root/etc/systemd/system/ci-fleet-deployer.service.d"
expect_success "$installer" --repair --config "$config" >/dev/null
expect_success "$installer" --check --config "$config" >/dev/null

# A group- or world-writable managed unit must block before transaction backup.
rm "$root/etc/systemd/system/ci-fleet-deployer-cleanup.timer"
chmod 0666 "$root/etc/systemd/system/ci-fleet-deployer.service"
units_before_mode=$(find "$root/etc/systemd/system" -mindepth 1 -maxdepth 1 -printf '%P %y %m\n' | sort | sha256sum)
expect_failure 'managed systemd unit has an unsafe owner, mode, or type' "$installer" --repair --config "$config" >/dev/null
[[ $units_before_mode == "$(find "$root/etc/systemd/system" -mindepth 1 -maxdepth 1 -printf '%P %y %m\n' | sort | sha256sum)" ]] || fail 'writable unit allowed a transaction backup'
chmod 0644 "$root/etc/systemd/system/ci-fleet-deployer.service"
expect_success "$installer" --repair --config "$config" >/dev/null
expect_success "$installer" --check --config "$config" >/dev/null

# Rebuild a retained rollback pair consumed by the drifted-incumbent rollback
# and finalize recovery fixtures.
image='registry.example.invalid/example/app@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
write_evidence
write_config
expect_success "$installer" --upgrade --config "$config" >/dev/null
image='registry.example.invalid/example/app@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
write_evidence
write_config
expect_success "$installer" --upgrade --config "$config" >/dev/null
expect_success "$installer" --check --config "$config" >/dev/null
[[ -f "$root/var/lib/ci-fleet-deployer/last-known-good.json" ]] || fail 'retained rollback pair was not rebuilt'

# A lost install state with surviving release/units must not be treated as a fresh install.
mv "$root/var/lib/ci-fleet-deployer/install-state.json" "$tmp/install-state.saved"
expect_failure 'restore install state before convergence' "$installer" --repair --config "$config" >/dev/null
mv "$tmp/install-state.saved" "$root/var/lib/ci-fleet-deployer/install-state.json"
chmod 0600 "$root/var/lib/ci-fleet-deployer/install-state.json"
expect_success "$installer" --repair --config "$config" >/dev/null

# Lost install state with only a retained deployed snapshot is still an installation.
mv "$root/var/lib/ci-fleet-deployer/install-state.json" "$tmp/install-state.saved"
rm -f "$root/opt/ci-fleet-deployer/current"
for stale_unit in "$root"/etc/systemd/system/ci-fleet-deployer*; do rm -f "$stale_unit"; done
expect_failure 'restore install state before convergence' "$installer" --repair --config "$config" >/dev/null
mv "$tmp/install-state.saved" "$root/var/lib/ci-fleet-deployer/install-state.json"
chmod 0600 "$root/var/lib/ci-fleet-deployer/install-state.json"
expect_success "$installer" --repair --config "$config" >/dev/null
expect_success "$installer" --check --config "$config" >/dev/null

# Rollback must work from the retained pair without a usable candidate config.
mv "$config" "$tmp/config.saved"
rollback=$(expect_success "$installer" --rollback --config "$config")
grep -Fq 'result=CHANGED' <<<"$rollback" || fail 'config-independent rollback did not report change'
mv "$tmp/config.saved" "$config"; chmod 0600 "$config"
expect_success "$installer" --repair --config "$config" >/dev/null
# Rebuild the retained pair consumed by the config-independent rollback.
image='registry.example.invalid/example/app@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
write_evidence
write_config
expect_success "$installer" --upgrade --config "$config" >/dev/null
image='registry.example.invalid/example/app@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
write_evidence
write_config
expect_success "$installer" --upgrade --config "$config" >/dev/null
expect_success "$installer" --check --config "$config" >/dev/null
[[ -f "$root/var/lib/ci-fleet-deployer/last-known-good.json" ]] || fail 'retained rollback pair was not rebuilt'
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
python3 - "$root/var/lib/ci-fleet-deployer/last-known-good-policy.conf" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('ADAPTER_SHA256=', 'ADAPTER_SHA256=' + 'f'*64 + '\n#', 1))
PY
available_check=$(expect_success "$installer" --check --config "$config")
grep -Fq 'rollback_available=no' <<<"$available_check" || fail 'digest-mismatched retained adapter still reported rollback_available=yes'
install -m 0600 "$tmp/lkg-policy.saved" "$root/var/lib/ci-fleet-deployer/last-known-good-policy.conf" 2>/dev/null || true
# A retained pair whose release is missing must report rollback_available=no.
missing_core=cccccccccccccccccccccccccccccccccccccccc
cp "$root/var/lib/ci-fleet-deployer/last-known-good-policy.conf" "$tmp/lkg-policy-release.saved"
cp "$root/var/lib/ci-fleet-deployer/last-known-good.json" "$tmp/lkg-state-release.saved"
python3 - "$root/var/lib/ci-fleet-deployer/last-known-good-policy.conf" "$root/var/lib/ci-fleet-deployer/last-known-good.json" "$missing_core" <<'PY'
from pathlib import Path
import json, re, sys
policy, state, missing = sys.argv[1], sys.argv[2], sys.argv[3]
p = Path(policy); p.write_text(re.sub(r'(?m)^CORE_REF=[0-9a-f]{40}$', 'CORE_REF=' + missing, p.read_text()))
s = json.loads(Path(state).read_text()); s['core_ref'] = missing
Path(state).write_text(json.dumps(s, indent=2, sort_keys=True) + '\n')
PY
chmod 0600 "$root/var/lib/ci-fleet-deployer/last-known-good-policy.conf" "$root/var/lib/ci-fleet-deployer/last-known-good.json"
available_check=$(expect_success "$installer" --check --config "$config")
grep -Fq 'rollback_available=no' <<<"$available_check" || fail 'release-missing retained pair still reported rollback_available=yes'
install -m 0600 "$tmp/lkg-policy-release.saved" "$root/var/lib/ci-fleet-deployer/last-known-good-policy.conf"
install -m 0600 "$tmp/lkg-state-release.saved" "$root/var/lib/ci-fleet-deployer/last-known-good.json"
image='registry.example.invalid/example/app@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
write_evidence
write_config
expect_success "$installer" --upgrade --config "$config" >/dev/null
image='registry.example.invalid/example/app@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
write_evidence
write_config
expect_success "$installer" --upgrade --config "$config" >/dev/null
expect_success "$installer" --check --config "$config" >/dev/null

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
CI_FLEET_DEPLOYER_TEST_LIVE_PID=$$ expect_failure 'active deployment prevents this operation' "$installer" --upgrade --config "$config" >/dev/null
CI_FLEET_DEPLOYER_TEST_LIVE_PID=$$ expect_failure 'active deployment prevents drain' "$installer" --drain --config "$config" >/dev/null
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
chmod 0644 "$root/etc/systemd/system/ci-fleet-deployer.service"
expect_success "$installer" --repair --config "$config" >/dev/null
[[ -f "$root/var/lib/ci-fleet-deployer/drained" ]] || fail 'repair implicitly resumed the deployer'
resume=$(expect_success "$installer" --resume --config "$config")
grep -Fq 'result=CHANGED' <<<"$resume" || fail 'resume did not clear drain state'
grep -Fq 'health=healthy' <<<"$resume" || fail 'resume report omitted verified health'
[[ ! -e "$root/var/lib/ci-fleet-deployer/drained" ]] || fail 'resume retained drain state'
chmod 0666 "$root/etc/systemd/system/ci-fleet-deployer.service"
expect_failure 'installed deployer state is absent or drifted; repair before resume' "$installer" --resume --config "$config" >/dev/null
chmod 0644 "$root/etc/systemd/system/ci-fleet-deployer.service"
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

# A symlinked install root must block uninstall before its target's pointer is touched.
expect_success "$installer" --install --config "$config" >/dev/null
mv "$root/opt/ci-fleet-deployer" "$tmp/install-root-uninstall.real"
ln -s "$tmp/install-root-uninstall.real" "$root/opt/ci-fleet-deployer"
expect_failure 'unsafe symlinked managed directory' "$installer" --uninstall --config "$config" >/dev/null
[[ -L "$tmp/install-root-uninstall.real/current" ]] || fail 'uninstall removed a pointer through a symlinked install root'
rm "$root/opt/ci-fleet-deployer"
mv "$tmp/install-root-uninstall.real" "$root/opt/ci-fleet-deployer"
expect_success "$installer" --uninstall --config "$config" >/dev/null
[[ ! -L "$root/opt/ci-fleet-deployer/current" ]] || fail 'retry after correcting install-root drift did not uninstall'

# Uninstall must fail when systemd cannot reload after unit removal.
expect_success "$installer" --install --config "$config" >/dev/null
FAKE_SYSTEMCTL_FAIL_COMMAND=daemon-reload expect_failure 'systemd manager reload failed after unit removal' "$installer" --uninstall --config "$config" >/dev/null
unset FAKE_SYSTEMCTL_FAIL_COMMAND
expect_success "$installer" --uninstall --config "$config" >/dev/null
[[ ! -L "$root/opt/ci-fleet-deployer/current" ]] || fail 'retry after reload failure did not uninstall'

# Uninstall must still remove the deployment surface when the configuration directory is absent.
expect_success "$installer" --install --config "$config" >/dev/null
mv "$root/etc/ci-fleet-deployer" "$tmp/etc-deployer.saved"
uninstall=$(expect_success "$installer" --uninstall --config "$config")
grep -Fq 'result=CHANGED' <<<"$uninstall" || fail 'uninstall without configuration did not report change'
[[ ! -L "$root/opt/ci-fleet-deployer/current" ]] || fail 'uninstall without configuration retained the activation pointer'
if compgen -G "$root/etc/systemd/system/ci-fleet-deployer*" >/dev/null; then fail 'uninstall without configuration retained managed units'; fi
mv "$tmp/etc-deployer.saved" "$root/etc/ci-fleet-deployer"

# Uninstall must stop a timer whose unit file has drifted away.
expect_success "$installer" --install --config "$config" >/dev/null
rm "$root/etc/systemd/system/ci-fleet-deployer-cleanup.timer"
FAKE_SYSTEMCTL_LOG=$tmp/systemctl-drift.log expect_success "$installer" --uninstall --config "$config" >/dev/null
grep -Fq 'disable --now ci-fleet-deployer-cleanup.timer' "$tmp/systemctl-drift.log" || fail 'uninstall left a loaded drifted timer running'
if compgen -G "$root/etc/systemd/system/ci-fleet-deployer*" >/dev/null; then fail 'drifted-timer uninstall retained managed units'; fi

# An untracked symlink in the deployer unit source must block checkout validation.
expect_success "$installer" --install --config "$config" >/dev/null
ln -s "$credential" "$repo_root/deploy/deployer/leak"
expect_failure 'deployer unit source contains an unsafe or untracked entry' "$installer" --repair --config "$config" >/dev/null
[[ $(stat -c %a "$credential") == 600 ]] || fail 'untracked checkout symlink exposed credential bytes'
rm "$repo_root/deploy/deployer/leak"
printf 'unreviewed\n' >"$repo_root/deploy/deployer/extra-unit.service"
expect_failure 'checkout snapshot contains unreviewed entries' "$installer" --repair --config "$config" >/dev/null
rm "$repo_root/deploy/deployer/extra-unit.service"
expect_success "$installer" --repair --config "$config" >/dev/null
expect_success "$installer" --check --config "$config" >/dev/null

# A retained policy naming a credential outside the protected directory, or a
# malformed external reference, must report rollback_available=no.
[[ -f "$root/var/lib/ci-fleet-deployer/last-known-good-policy.conf" ]] || fail 'test setup expected a retained policy'
cp "$root/var/lib/ci-fleet-deployer/last-known-good-policy.conf" "$tmp/lkg-policy.saved"
python3 - "$root/var/lib/ci-fleet-deployer/last-known-good-policy.conf" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('CREDENTIAL_PROVIDER=file', 'CREDENTIAL_PROVIDER=external').replace(next(x for x in p.read_text().splitlines() if x.startswith('CREDENTIAL_REF=')), 'CREDENTIAL_REF=external:bad'))
PY
available_check=$(expect_success "$installer" --check --config "$config")
grep -Fq 'rollback_available=no' <<<"$available_check" || fail 'malformed external retained credential still reported rollback_available=yes'
install -m 0600 "$tmp/lkg-policy.saved" "$root/var/lib/ci-fleet-deployer/last-known-good-policy.conf"
python3 - "$root/var/lib/ci-fleet-deployer/last-known-good-policy.conf" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace(next(x for x in p.read_text().splitlines() if x.startswith('CREDENTIAL_REF=')), 'CREDENTIAL_REF=/etc/hostname'))
PY
available_check=$(expect_success "$installer" --check --config "$config")
grep -Fq 'rollback_available=no' <<<"$available_check" || fail 'out-of-directory retained credential still reported rollback_available=yes'
install -m 0600 "$tmp/lkg-policy.saved" "$root/var/lib/ci-fleet-deployer/last-known-good-policy.conf"
available_check=$(expect_success "$installer" --check --config "$config")
grep -Fq 'rollback_available=yes' <<<"$available_check" || fail 'restored retained pair was not reported rollback_available=yes'

# Retained-transaction recovery must validate the systemd boundary before stopping timers.
expect_success "$installer" --uninstall --config "$config" >/dev/null
expect_success "$installer" --install --config "$config" >/dev/null
boundary_recovery=$root/var/lib/ci-fleet-deployer/.transaction.writable-boundary
mkdir -m 0700 "$boundary_recovery" "$boundary_recovery/units" "$boundary_recovery/state"
for name in install-state.json active-policy.conf last-known-good.json last-known-good-policy.conf; do
  [[ ! -e "$root/var/lib/ci-fleet-deployer/$name" ]] || { cp "$root/var/lib/ci-fleet-deployer/$name" "$boundary_recovery/state/$name"; printf '%s\n' "$name" >>"$boundary_recovery/state-present"; }
done
printf '%s\n' "$(readlink "$root/opt/ci-fleet-deployer/current")" >"$boundary_recovery/current-target"
printf '%s\n' ci-fleet-deployer-health.timer ci-fleet-deployer-cleanup.timer >"$boundary_recovery/timers-enabled"
chmod 0777 "$root/etc/systemd/system"
expect_failure 'systemd unit directory is group- or world-writable' "$installer" --repair --config "$config" >/dev/null
[[ $(stat -c %a "$root/etc/systemd/system") == 777 ]] || fail 'writable boundary recovery mutated the unit directory'
rm -rf -- "$boundary_recovery"
chmod 0755 "$root/etc/systemd/system"
expect_success "$installer" --repair --config "$config" >/dev/null
expect_success "$installer" --check --config "$config" >/dev/null

# Retained-transaction recovery must validate the deployed-state boundary before pointer mutation.
boundary_recovery=$root/var/lib/ci-fleet-deployer/.transaction.deployed-symlink
mkdir -m 0700 "$boundary_recovery" "$boundary_recovery/units" "$boundary_recovery/state"
for name in install-state.json active-policy.conf last-known-good.json last-known-good-policy.conf; do
  [[ ! -e "$root/var/lib/ci-fleet-deployer/$name" ]] || { cp "$root/var/lib/ci-fleet-deployer/$name" "$boundary_recovery/state/$name"; printf '%s\n' "$name" >>"$boundary_recovery/state-present"; }
done
printf '%s\n' "$(readlink "$root/opt/ci-fleet-deployer/current")" >"$boundary_recovery/current-target"
printf 'absent\n' >"$boundary_recovery/deployed-target"
mv "$root/var/lib/ci-fleet-deployer/deployed" "$tmp/deployed.real"
mkdir "$tmp/deployed.real-target"; ln -s "$tmp/deployed.real-target" "$root/var/lib/ci-fleet-deployer/deployed"
printf 'decoy\n' >"$tmp/deployed.real-target/current"
expect_failure 'deployed snapshot directory is unsafe' "$installer" --repair --config "$config" >/dev/null
[[ $(<"$tmp/deployed.real-target/current") == decoy ]] || fail 'recovery mutated state through a symlinked deployed directory'
[[ -d "$boundary_recovery" ]] || fail 'blocked deployed-boundary recovery discarded its transaction'
rm "$root/var/lib/ci-fleet-deployer/deployed"; mv "$tmp/deployed.real" "$root/var/lib/ci-fleet-deployer/deployed"
rm -rf -- "$boundary_recovery"
expect_success "$installer" --repair --config "$config" >/dev/null
expect_success "$installer" --check --config "$config" >/dev/null

# A failed timer shutdown must fail the uninstall before any unit removal.
expect_success "$installer" --install --config "$config" >/dev/null
uninstall_before=$(find "$root/etc/systemd/system" -mindepth 1 -maxdepth 1 -printf '%P %y %m\n' | sort | sha256sum)
FAKE_SYSTEMCTL_FAIL_COMMAND=disable expect_failure 'deployer timers did not stop during uninstall' "$installer" --uninstall --config "$config" >/dev/null
[[ "$uninstall_before" == "$(find "$root/etc/systemd/system" -mindepth 1 -maxdepth 1 -printf '%P %y %m\n' | sort | sha256sum)" ]] || fail 'failed timer shutdown partially uninstalled units'
[[ -L "$root/opt/ci-fleet-deployer/current" ]] || fail 'failed timer shutdown removed the activation pointer'
expect_success "$installer" --uninstall --config "$config" >/dev/null
repeat_uninstall=$(expect_success "$installer" --uninstall --config "$config")
grep -Fq 'result=NO_CHANGE' <<<"$repeat_uninstall" || fail 'repeated uninstall was not idempotent after timer removal'

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
# Production deployment remains separately gated: the runtime must reject it.
cp "$config" "$tmp/config.staging"
write_production_gate
write_evidence production example-production
write_config production example-production
python3 - "$request" "$approval" <<'PY'
from pathlib import Path
import sys
for name in sys.argv[1:]:
    p=Path(name); p.write_text(p.read_text().replace('ENVIRONMENT=staging', 'ENVIRONMENT=production').replace('TARGET_ID=example-staging', 'TARGET_ID=example-production'))
PY
expect_failure 'production deployment is not authorized by the current accepted scope' "$runtime" deploy >/dev/null
[[ $(grep -Fxc deploy "$FAKE_ADAPTER_LOG" || true) == 0 ]] || fail 'gated production path reached the deployment adapter'
install -m 0600 "$tmp/config.staging" "$config"
write_evidence staging example-staging
cp "$approval" "$request"; chmod 0600 "$request"
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
deploy_calls_before=$(grep -Fxc deploy "$FAKE_ADAPTER_LOG" || true)
# A signal arriving after adapter success must not abort publication: the
# approval is consumed and the application change is applied, so the runtime
# masks INT/TERM until the new snapshot pointer and audit record are durable.
deployed_before_signal=$(readlink "$deployed_current")
CI_FLEET_DEPLOYER_TEST_SIGNAL_SELF=TERM expect_success "$runtime" deploy >/dev/null
[[ $(grep -Fxc deploy "$FAKE_ADAPTER_LOG" || true) == $((deploy_calls_before + 1)) ]] || fail 'signaled runtime did not reach the adapter'
[[ -L "$deployed_current" && $(readlink "$deployed_current") != "$deployed_before_signal" ]] || fail 'masked signal prevented deployed pointer publication'
[[ ! -e "$active" ]] || fail 'completed deployment left the active operation marker'
if compgen -G "$root/var/lib/ci-fleet-deployer/.active.*" >/dev/null; then fail 'completed deployment left an unpublished active marker temporary'; fi
rm -f "$root/var/lib/ci-fleet-deployer/last-request.conf"
rm -rf "$root/var/lib/ci-fleet-deployer/consumed-requests"
cp "$approval" "$request"; chmod 0600 "$request"
deployed_target=$(readlink "$deployed_current")
rm "$deployed_current"
expect_failure 'deployed rollback snapshot is missing' "$runtime" deploy >/dev/null
ln -s "$deployed_target" "$deployed_current"
rm "$deployed_current"
ln -s "$root/var/lib/ci-fleet-deployer/deployed/$deployed_target" "$deployed_current"
deploy_calls_before=$(grep -Fxc deploy "$FAKE_ADAPTER_LOG" || true)
expect_failure 'deployed snapshot pointer target is not canonical' "$runtime" deploy >/dev/null
[[ $(grep -Fxc deploy "$FAKE_ADAPTER_LOG" || true) == "$deploy_calls_before" ]] || fail 'deployment consumed approval with a noncanonical deployed pointer'
[[ ! -e "$active" ]] || fail 'rejected deployment left the active operation marker'
rm "$deployed_current"
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
grep -Fq 'approval=snapshot-mutation-attempt approver=example-reviewer policy=example-staging-policy-v1 checkpoint=checkpoint-20260808-1 authorized_by=none gate=none result=failed phase=post-adapter status=2' "$root/var/log/ci-fleet-deployer/audit.log" || fail 'post-adapter deployment failure was not audited with its real status'
[[ $(find "$root/var/lib/ci-fleet-deployer/deployed" -mindepth 1 -maxdepth 1 -name '.snapshot.*' -type d | wc -l) == 1 ]] || fail 'failed publication leaked an unreachable prepared snapshot'
[[ -e "$deployed_current" ]] || fail 'failed publication dangled the deployed pointer'

# A signal while the adapter runs must remove only the unreachable prepared snapshot.
write_evidence staging example-staging
python3 - "$approval" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('APPROVAL_ID=approval-20260808-1', 'APPROVAL_ID=signal-mid-adapter-attempt').replace('APPROVED_AT=2026-08-08T20:00:00Z', 'APPROVED_AT=2026-08-08T20:04:30Z'))
PY
cp "$approval" "$request"; chmod 0600 "$request"
export FAKE_ADAPTER_SIGNAL_PPID=1
if "$runtime" deploy >/dev/null 2>&1; then fail 'signaled deployment reported success'; fi
unset FAKE_ADAPTER_SIGNAL_PPID
[[ $(find "$root/var/lib/ci-fleet-deployer/deployed" -mindepth 1 -maxdepth 1 -name '.snapshot.*' -type d | wc -l) == 1 ]] || fail 'interrupted deployment leaked an unreachable prepared snapshot'
[[ -e "$deployed_current" ]] || fail 'interrupted deployment dangled the deployed pointer'
[[ ! -e "$active" ]] || fail 'interrupted deployment left the active operation marker'
grep -Fq 'approval=signal-mid-adapter-attempt' "$root/var/log/ci-fleet-deployer/audit.log" || fail 'interrupted deployment was not audited as failed'
rm -f "$root/var/lib/ci-fleet-deployer/last-request.conf"
rm -rf "$root/var/lib/ci-fleet-deployer/consumed-requests"
write_evidence staging example-staging
python3 - "$approval" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('APPROVAL_ID=approval-20260808-1', 'APPROVAL_ID=audit-replacement-attempt').replace('APPROVED_AT=2026-08-08T20:00:00Z', 'APPROVED_AT=2026-08-08T20:05:00Z'))
PY
cp "$approval" "$request"; chmod 0600 "$request"
export FAKE_ADAPTER_MUTATE_AUDIT_PATH=$root/var/log/ci-fleet-deployer/audit.log FAKE_ADAPTER_MUTATE_AUDIT_TARGET=$tmp/unrelated-audit
expect_failure 'deployer audit log changed during deployment' "$runtime" deploy >/dev/null
unset FAKE_ADAPTER_MUTATE_AUDIT_PATH FAKE_ADAPTER_MUTATE_AUDIT_TARGET
[[ $(<"$tmp/unrelated-audit") == unrelated-audit ]] || fail 'adapter audit replacement redirected the trusted append'
rm "$root/var/log/ci-fleet-deployer/audit.log"

# An adapter that replaces the audit log must not lose the deployment record.
write_evidence staging example-staging
python3 - "$approval" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('APPROVAL_ID=approval-20260808-1', 'APPROVAL_ID=audit-unlink-attempt').replace('APPROVED_AT=2026-08-08T20:00:00Z', 'APPROVED_AT=2026-08-08T20:05:30Z'))
PY
cp "$approval" "$request"; chmod 0600 "$request"
export FAKE_ADAPTER_MUTATE_AUDIT_PATH=$root/var/log/ci-fleet-deployer/audit.log FAKE_ADAPTER_MUTATE_AUDIT_MODE=unlink
expect_failure 'deployer audit log changed during deployment' "$runtime" deploy >/dev/null
unset FAKE_ADAPTER_MUTATE_AUDIT_PATH FAKE_ADAPTER_MUTATE_AUDIT_MODE
grep -Fq 'approval=audit-unlink-attempt' "$root/var/log/ci-fleet-deployer/audit.log" || fail 'adapter audit replacement lost the deployment audit record'
[[ $(<"$root/var/log/ci-fleet-deployer/audit.log") != *adapter-replacement* ]] || fail 'adapter replacement content entered the trusted audit log'
rm "$root/var/log/ci-fleet-deployer/audit.log"

# An adapter that deletes its consumption marker and fails must not enable replay.
write_evidence staging example-staging
python3 - "$approval" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('APPROVAL_ID=approval-20260808-1', 'APPROVAL_ID=marker-delete-attempt').replace('APPROVED_AT=2026-08-08T20:00:00Z', 'APPROVED_AT=2026-08-08T20:05:45Z'))
PY
cp "$approval" "$request"; chmod 0600 "$request"
printf 'deploy\n' >"$tmp/fail-deploy-marker"; export FAKE_ADAPTER_FAIL=$tmp/fail-deploy-marker
export FAKE_ADAPTER_DELETE_CONSUMED_GLOB="$root/var/lib/ci-fleet-deployer/consumed-requests/*"
expect_failure 'deployment adapter failed after approval consumption' "$runtime" deploy >/dev/null
unset FAKE_ADAPTER_FAIL FAKE_ADAPTER_DELETE_CONSUMED_GLOB; rm "$tmp/fail-deploy-marker"
if ! compgen -G "$root/var/lib/ci-fleet-deployer/consumed-requests/*" >/dev/null; then fail 'adapter-deleted consumption marker was not restored'; fi
cp "$approval" "$request"; chmod 0600 "$request"
expect_failure 'deployment request was already consumed' "$runtime" deploy >/dev/null
rm -rf "$root/var/lib/ci-fleet-deployer/consumed-requests"

# An adapter that deletes the whole consumption directory and fails must not enable replay.
write_evidence staging example-staging
python3 - "$approval" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('APPROVAL_ID=approval-20260808-1', 'APPROVAL_ID=dir-delete-attempt').replace('APPROVED_AT=2026-08-08T20:00:00Z', 'APPROVED_AT=2026-08-08T20:05:50Z'))
PY
cp "$approval" "$request"; chmod 0600 "$request"
printf 'deploy\n' >"$tmp/fail-deploy-dir"; export FAKE_ADAPTER_FAIL=$tmp/fail-deploy-dir
export FAKE_ADAPTER_DELETE_CONSUMED_GLOB="$root/var/lib/ci-fleet-deployer/consumed-requests"
expect_failure 'deployment adapter failed after approval consumption' "$runtime" deploy >/dev/null
unset FAKE_ADAPTER_FAIL FAKE_ADAPTER_DELETE_CONSUMED_GLOB; rm "$tmp/fail-deploy-dir"
if ! compgen -G "$root/var/lib/ci-fleet-deployer/consumed-requests/*" >/dev/null; then fail 'adapter-deleted consumption directory was not restored with its marker'; fi
cp "$approval" "$request"; chmod 0600 "$request"
expect_failure 'deployment request was already consumed' "$runtime" deploy >/dev/null
rm -rf "$root/var/lib/ci-fleet-deployer/consumed-requests"

# A failing adapter cannot leave recovery state or the audit log with unsafe metadata.
write_evidence staging example-staging
python3 - "$approval" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('APPROVAL_ID=approval-20260808-1', 'APPROVAL_ID=metadata-drift-attempt').replace('APPROVED_AT=2026-08-08T20:00:00Z', 'APPROVED_AT=2026-08-08T20:05:55Z'))
PY
cp "$approval" "$request"; chmod 0600 "$request"
incumbent_dir=$(readlink -f "$deployed_current")
printf 'deploy\n' >"$tmp/fail-after-metadata"
export FAKE_ADAPTER_MUTATE_INCUMBENT_PATH=$incumbent_dir
export FAKE_ADAPTER_MUTATE_LKG_ROOT=$root/var/lib/ci-fleet-deployer
export FAKE_ADAPTER_CHMOD_DURING=$root/var/log/ci-fleet-deployer/audit.log
export FAKE_ADAPTER_CHOWN_DURING=$root/var/log/ci-fleet-deployer/audit.log
export FAKE_ADAPTER_FAIL_AFTER_MUTATION=$tmp/fail-after-metadata
expect_failure 'deployment adapter failed after approval consumption' "$runtime" deploy >/dev/null
unset FAKE_ADAPTER_MUTATE_INCUMBENT_PATH FAKE_ADAPTER_MUTATE_LKG_ROOT FAKE_ADAPTER_CHMOD_DURING FAKE_ADAPTER_CHOWN_DURING FAKE_ADAPTER_FAIL_AFTER_MUTATION
expected_uid=$(id -u)
[[ $(stat -c '%u:%a' "$incumbent_dir") == "$expected_uid:700" ]] || fail 'failed adapter left incumbent directory metadata unsafe'
for file in "$incumbent_dir"/policy.conf "$incumbent_dir"/state.json "$root/var/lib/ci-fleet-deployer/last-known-good.json "$root/var/lib/ci-fleet-deployer/last-known-good-policy.conf "$root/var/log/ci-fleet-deployer/audit.log"; do
  [[ $(stat -c '%u:%a' "$file") == "$expected_uid:600" ]] || fail "failed adapter left recovery metadata unsafe: $file"
done
rm -rf "$root/var/lib/ci-fleet-deployer/consumed-requests"

# A successful adapter also cannot leave the retained rollback pair unsafe.
write_evidence staging example-staging
python3 - "$approval" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('APPROVAL_ID=approval-20260808-1', 'APPROVAL_ID=lkg-metadata-drift-attempt').replace('APPROVED_AT=2026-08-08T20:00:00Z', 'APPROVED_AT=2026-08-08T20:05:56Z'))
PY
cp "$approval" "$request"; chmod 0600 "$request"
FAKE_ADAPTER_MUTATE_LKG_ROOT=$root/var/lib/ci-fleet-deployer expect_success "$runtime" deploy >/dev/null
for file in "$root/var/lib/ci-fleet-deployer/last-known-good.json" "$root/var/lib/ci-fleet-deployer/last-known-good-policy.conf"; do
  [[ $(stat -c '%u:%a' "$file") == "$expected_uid:600" ]] || fail "successful adapter left retained rollback metadata unsafe: $file"
done

# Signal at the deployed-snapshot publication boundary must not delete the published snapshot.
write_evidence staging example-staging
python3 - "$approval" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); p.write_text(p.read_text().replace('APPROVAL_ID=approval-20260808-1', 'APPROVAL_ID=signal-at-publication-attempt').replace('APPROVED_AT=2026-08-08T20:00:00Z', 'APPROVED_AT=2026-08-08T20:06:00Z'))
PY
cp "$approval" "$request"; chmod 0600 "$request"
export FAKE_ADAPTER_MUTATE_AUDIT_PATH=$root/var/log/ci-fleet-deployer/audit.log FAKE_ADAPTER_MUTATE_AUDIT_TARGET=$tmp/unrelated-audit
expect_failure 'deployer audit log changed during deployment' "$runtime" deploy >/dev/null
unset FAKE_ADAPTER_MUTATE_AUDIT_PATH FAKE_ADAPTER_MUTATE_AUDIT_TARGET
[[ -L "$deployed_current" && -e "$deployed_current" && -f "$deployed_current/policy.conf" && -f "$deployed_current/state.json" ]] || fail 'post-publication failure left deployed/current dangling'
cmp -s "$deployed_current/policy.conf" "$config" || fail 'published deployed policy does not match the deployed configuration'
rm "$root/var/log/ci-fleet-deployer/audit.log"
write_evidence staging example-staging
cp "$approval" "$request"; chmod 0600 "$request"
export FAKE_ADAPTER_FORBID_REQUEST_PATH=$request
export CI_FLEET_DEPLOYER_TEST_INHIBITOR_LOG=$tmp/inhibitor.log
expect_success "$runtime" deploy >/dev/null
unset FAKE_ADAPTER_FORBID_REQUEST_PATH CI_FLEET_DEPLOYER_TEST_INHIBITOR_LOG
[[ $(<"$tmp/inhibitor.log") == deploy ]] || fail 'deployment transaction was not enclosed by the shutdown inhibitor'
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

# Bytes substituted into the live checkout after review must never reach a staged release.
cp "$runtime" "$tmp/runtime.saved"
restore_live_checkout() {
  [[ ! -f "$tmp/runtime.saved" ]] || cat "$tmp/runtime.saved" >"$runtime"
  git -C "$repo_root" replace -d "$core_ref" 2>/dev/null || true
}
trap 'restore_live_checkout; rm -rf "$tmp"' EXIT
release_before=$(sha256sum "$root/opt/ci-fleet-deployer/releases/$core_ref/scripts/deployer-runtime.sh")
printf '# substituted-live-bytes\n' >>"$runtime"
expect_failure 'differs from the reviewed commit' "$installer" --repair --config "$config" >/dev/null
[[ $release_before == "$(sha256sum "$root/opt/ci-fleet-deployer/releases/$core_ref/scripts/deployer-runtime.sh")" ]] || fail 'mutated live checkout bytes entered the trusted release'
replacement=$(git -C "$repo_root" commit-tree 'HEAD^{tree}' -m replace-fixture 2>/dev/null || true)
if [[ -n $replacement ]]; then
  git -C "$repo_root" replace "$core_ref" "$replacement"
  expect_failure 'differs from the reviewed commit' "$installer" --repair --config "$config" >/dev/null
  [[ $release_before == "$(sha256sum "$root/opt/ci-fleet-deployer/releases/$core_ref/scripts/deployer-runtime.sh")" ]] || fail 'replacement ref bytes entered the trusted release'
  git -C "$repo_root" replace -d "$core_ref"
fi
cat "$tmp/runtime.saved" >"$runtime"
git -C "$repo_root" show "HEAD:scripts/deployer-runtime.sh" | cmp -s - "$runtime" || fail 'live checkout restoration diverged from HEAD'
rm "$root/var/lib/ci-fleet-deployer/drained"

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
