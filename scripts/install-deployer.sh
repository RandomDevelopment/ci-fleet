#!/usr/bin/env bash
set -Eeuo pipefail
set +x
export PYTHONDONTWRITEBYTECODE=1

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
mode=
config=
validated_config=
error_reported=0
root=${CI_FLEET_DEPLOYER_ROOT:-}
testing=${CI_FLEET_DEPLOYER_TESTING:-0}
effective_uid=${EUID:-$(id -u)}
action=unknown
environment=unknown
target=unknown
core_ref=unknown
artifact=unknown
health=unknown
staging_path=
checkout_snapshot=
transaction_dir=
transaction_preparing=0
transaction_committed=0
recovered_rollback=0
on_exit() {
  local status=$? recovery_status=0
  if [[ -n ${transaction_dir:-} && ${transaction_preparing:-0} == 1 ]]; then
    rm -rf -- "$transaction_dir"
    transaction_dir=
  elif [[ -n ${transaction_dir:-} && ${transaction_committed:-0} != 1 ]]; then
    set +e
    if [[ -e "$transaction_dir/application-rollback-committed" || -L "$transaction_dir/application-rollback-committed" ]]; then finalize_committed_rollback; recovery_status=$?; else restore_transaction; recovery_status=$?; fi
    set -e
    if ((recovery_status != 0)); then
      status=$recovery_status
      printf 'ERROR: transaction recovery failed; retained %s for the next installer recovery\n' "$transaction_dir" >&2
    fi
  fi
  [[ -z ${staging_path:-} ]] || rm -rf -- "$staging_path"
  [[ -z ${checkout_snapshot:-} ]] || rm -rf -- "$checkout_snapshot"
  [[ -z ${validated_config:-} ]] || rm -f -- "$validated_config"
  [[ -z ${policy_check_snapshot:-} ]] || rm -f -- "$policy_check_snapshot"
  if ((status != 0 && error_reported == 0)); then report FAILED no inspect-and-retry "$(rollback_available)" >&2; fi
  return "$status"
}
trap on_exit EXIT

usage() {
  cat >&2 <<'EOF'
usage: install-deployer.sh --check|--install|--upgrade|--repair|--rollback|--drain|--resume|--uninstall --config /etc/ci-fleet-deployer/deployer.conf

Modes are explicit and mutually exclusive. Configuration and credential references
are host-local; secret values are never accepted as arguments.
EOF
}
report() {
  local result=$1 changed=$2 next=$3 rollback=${4:-no}
  printf 'REPORT action=%s result=%s environment=%s target=%s version=%s digest=%s health=%s changed=%s rollback_available=%s next=%s\n' \
    "$action" "$result" "$environment" "$target" "$core_ref" "${artifact#*@}" "$health" "$changed" "$rollback" "$next"
}
rollback_available() {
  [[ -n ${previous_state:-} && -f ${previous_state:-/nonexistent} && ! -L ${previous_state:-/nonexistent} && -n ${previous_policy:-} && -f ${previous_policy:-/nonexistent} && ! -L ${previous_policy:-/nonexistent} ]] || { printf no; return; }
  [[ $(stat -c '%u:%a' "$previous_state" 2>/dev/null) == "$expected_uid:600" && $(stat -c '%u:%a' "$previous_policy" 2>/dev/null) == "$expected_uid:600" ]] || { printf no; return; }
  python3 - "$previous_state" "$previous_policy" <<'PY' >/dev/null 2>&1 || { printf no; return; }
import json, sys
allowed = {"SCHEMA_VERSION","CORE_REF","ENVIRONMENT","TARGET_ID","DEPLOYER_IDENTITY","ADAPTER_PATH","ADAPTER_SHA256","CREDENTIAL_PROVIDER","CREDENTIAL_REF","CREDENTIAL_SCOPE","APPROVAL_PROVIDER","APPROVAL_EVIDENCE_PATH","APPROVAL_CAPABILITY_EVIDENCE_PATH","PRODUCTION_AUTHORIZATION_EVIDENCE_PATH","CHECKPOINT_EVIDENCE_PATH","SOURCE_COMMIT","ARTIFACT_IMAGE","NETWORK_HOST","MIN_DISK_GIB","REQUIRE_COMPOSE"}
try:
    state = json.load(open(sys.argv[1], encoding='utf-8'))
except (OSError, ValueError):
    raise SystemExit(1)
policy = {}
try:
    for line in open(sys.argv[2], encoding='utf-8'):
        line = line.rstrip('\n')
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or key not in allowed or key in policy or not value:
            raise SystemExit(1)
        policy[key] = value
except OSError:
    raise SystemExit(1)
import re
sha = re.compile(r'^[0-9a-f]{40}$')
pairs = (('core_ref','CORE_REF'), ('environment','ENVIRONMENT'), ('target','TARGET_ID'), ('source_commit','SOURCE_COMMIT'), ('artifact','ARTIFACT_IMAGE'), ('deployer_identity','DEPLOYER_IDENTITY'))
ok = (all(state.get(k) and state.get(k) == policy.get(p) for k, p in pairs)
      and bool(sha.match(state['core_ref'])) and bool(sha.match(state['source_commit']))
      and bool(re.search(r'@sha256:[0-9a-f]{64}$', state['artifact']))
      and policy.get('SCHEMA_VERSION') == '1')
raise SystemExit(0 if ok else 1)
PY
  local rollback_adapter_path rollback_adapter_sha rollback_credential_provider rollback_credential_ref
  rollback_adapter_path=$(awk '$0 ~ /^ADAPTER_PATH=/ {sub(/^ADAPTER_PATH=/, ""); print}' "$previous_policy")
  rollback_adapter_sha=$(awk '$0 ~ /^ADAPTER_SHA256=/ {sub(/^ADAPTER_SHA256=/, ""); print}' "$previous_policy")
  [[ $rollback_adapter_path == "$etc_root/adapters/"* ]] || { printf no; return; }
  [[ ! -L "$rollback_adapter_path" && -f "$rollback_adapter_path" && $(stat -c '%u:%a' "$rollback_adapter_path" 2>/dev/null) == "$expected_uid:700" ]] || { printf no; return; }
  [[ $(sha256sum "$rollback_adapter_path" 2>/dev/null | cut -d' ' -f1) == "$rollback_adapter_sha" ]] || { printf no; return; }
  rollback_credential_provider=$(awk '$0 ~ /^CREDENTIAL_PROVIDER=/ {sub(/^CREDENTIAL_PROVIDER=/, ""); print}' "$previous_policy")
  rollback_credential_ref=$(awk '$0 ~ /^CREDENTIAL_REF=/ {sub(/^CREDENTIAL_REF=/, ""); print}' "$previous_policy")
  if [[ $rollback_credential_provider == file ]]; then
    [[ $rollback_credential_ref == "$etc_root/credentials/"* ]] || { printf no; return; }
    [[ ! -L "$rollback_credential_ref" && -f "$rollback_credential_ref" && $(stat -c '%u:%a' "$rollback_credential_ref" 2>/dev/null) == "$expected_uid:600" ]] || { printf no; return; }
  elif [[ $rollback_credential_provider == external ]]; then
    [[ $rollback_credential_ref =~ ^external:[a-z0-9][a-z0-9-]{0,31}:[A-Za-z0-9._/-]{1,128}$ ]] || { printf no; return; }
  else
    printf no; return
  fi
  printf yes
}
die() { error_reported=1; printf 'ERROR: %s\n' "$*" >&2; report FAILED no inspect-and-retry "$(rollback_available)" >&2; exit 2; }
block() { error_reported=1; printf 'BLOCKED: %s\n' "$*" >&2; report BLOCKED no resolve-precondition "$(rollback_available)" >&2; exit 3; }

while (($#)); do
  case "$1" in
    --check|--install|--upgrade|--repair|--rollback|--drain|--resume|--uninstall)
      [[ -z "$mode" ]] || die 'select exactly one operating mode'
      mode=${1#--}; action=$mode; shift ;;
    --config) (($# >= 2)) || die '--config requires a value'; config=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ -n "$mode" ]] || { usage; die 'an explicit operating mode is required'; }
[[ -n "$config" ]] || die '--config is required'
[[ -z "$root" || "$testing" == 1 ]] || die 'CI_FLEET_DEPLOYER_ROOT is test-only'
if [[ "$testing" == 1 ]]; then
  [[ -n "$root" ]] || die 'test mode requires an alternate root'
  effective_uid=${CI_FLEET_DEPLOYER_EUID_OVERRIDE:-$effective_uid}
else
  [[ -z "$root" ]] || die 'alternate root is forbidden'
  [[ -z ${CI_FLEET_DEPLOYER_EUID_OVERRIDE:-} ]] || die 'effective UID override is test-only'
fi
if [[ "$mode" != check && "$effective_uid" != 0 ]]; then die 'run this mode as root'; fi

root_path() { printf '%s%s' "$root" "$1"; }
etc_root=$(root_path /etc/ci-fleet-deployer)
install_root=$(root_path /opt/ci-fleet-deployer)
releases=$install_root/releases
current=$install_root/current
state_root=$(root_path /var/lib/ci-fleet-deployer)
state_file=$state_root/install-state.json
active_policy=$state_root/active-policy.conf
previous_state=$state_root/last-known-good.json
previous_policy=$state_root/last-known-good-policy.conf
deployed_root=$state_root/deployed
deployed_current=$deployed_root/current
drained=$state_root/drained
active_operation=$state_root/active-operation
lock_root=$(root_path /var/lock/ci-fleet-deployer)
log_root=$(root_path /var/log/ci-fleet-deployer)
systemd_root=$(root_path /etc/systemd/system)
unit_source=$repo_root/deploy/deployer
unit_names=(
  ci-fleet-deployer.service
  ci-fleet-deployer-health.service ci-fleet-deployer-health.timer
  ci-fleet-deployer-cleanup.service ci-fleet-deployer-cleanup.timer
  ci-fleet-deployer-drain.service
)
timer_names=(ci-fleet-deployer-health.timer ci-fleet-deployer-cleanup.timer)
config_keys='SCHEMA_VERSION CORE_REF ENVIRONMENT TARGET_ID DEPLOYER_IDENTITY ADAPTER_PATH ADAPTER_SHA256 CREDENTIAL_PROVIDER CREDENTIAL_REF CREDENTIAL_SCOPE APPROVAL_PROVIDER APPROVAL_EVIDENCE_PATH APPROVAL_CAPABILITY_EVIDENCE_PATH PRODUCTION_AUTHORIZATION_EVIDENCE_PATH CHECKPOINT_EVIDENCE_PATH SOURCE_COMMIT ARTIFACT_IMAGE NETWORK_HOST MIN_DISK_GIB REQUIRE_COMPOSE'

expected_uid=0
[[ "$testing" != 1 ]] || expected_uid=$(id -u)
inside() {
  local path=$1 base=$2 normalized normalized_base
  normalized=$(realpath -m -- "$path")
  normalized_base=$(realpath -m -- "$base")
  [[ "$normalized" == "$normalized_base/"* ]]
}
valid_utc() {
  local value=$1
  [[ "$value" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] && [[ $(date -u -d "$value" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null) == "$value" ]]
}
credential_reference_safe() {
  local provider=$1 reference=$2 description=$3
  case $provider in
    file)
      inside "$reference" "$etc_root/credentials" || die "$description credential reference is outside the approved credential directory"
      [[ ! -L $reference && -f $reference ]] || die "$description credential reference must be a regular file, not a symlink"
      [[ $reference == "$(realpath -m -- "$reference")" && $(realpath -e -- "$reference") == "$reference" ]] || die "$description credential reference contains a symlink or non-canonical component"
      [[ $(stat -c '%u:%a' "$reference") == "$expected_uid:600" ]] || die "$description credential file must be owner-only mode 0600"
      ;;
    external)
      [[ $reference =~ ^external:[a-z0-9][a-z0-9-]{0,31}:[A-Za-z0-9._/-]{1,128}$ ]] || die "$description has an invalid external secret-manager adapter reference"
      ;;
    *) die "$description CREDENTIAL_PROVIDER must be file or external" ;;
  esac
}

secure_file() {
  local path=$1 description=$2 mode=${3:-600}
  [[ ! -L "$path" && -f "$path" ]] || block "$description must be a regular file, not a symlink"
  [[ "$path" == "$(realpath -m -- "$path")" ]] || block "$description path contains a symlink or non-canonical component"
  [[ $(realpath -e -- "$path") == $(realpath -m -- "$path") ]] || block "$description path contains a symlink"
  [[ $(stat -c '%u:%a' "$path") == "$expected_uid:$mode" ]] || block "$description must be owned by root with mode 0$mode"
}
secure_directory() {
  local path=$1 mode=$2 create=${3:-0}
  [[ ! -L "$path" ]] || die "unsafe symlinked managed directory: $path"
  if [[ ! -e "$path" ]]; then
    [[ "$create" == 1 ]] || return 1
    install -d -m "$mode" "$path"
  fi
  if [[ "$create" == 1 && -d "$path" && $(stat -c %u "$path") == "$expected_uid" ]]; then chmod "$mode" "$path"; fi
  [[ -d "$path" && $(stat -c '%u:%a' "$path") == "$expected_uid:$mode" ]] || die "unsafe managed directory: $path"
}

parse_file() {
  local path=$1 prefix=$2 kind=$3 allowed=$4 line key value
  declare -gA "$prefix=()"
  local -n output=$prefix
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -n "$line" && ${line:0:1} != '#' ]] || continue
    [[ "$line" == *=* ]] || block "malformed $kind line"
    key=${line%%=*}; value=${line#*=}
    [[ "$key" =~ ^[A-Z][A-Z0-9_]*$ && -n "$value" && "$value" != *$'\r'* && "$value" != *$'\n'* ]] || block "malformed $kind line"
    [[ ! -v "output[$key]" ]] || block "duplicate $kind key: $key"
    [[ " $allowed " == *" $key "* ]] || block "unknown $kind key: $key"
    # key indexes a nameref to an associative array.
    # shellcheck disable=SC2004
    output[$key]=$value
  done <"$path"
}

validate_config() {
  inside "$config" "$etc_root" || block "configuration path must be inside $etc_root"
  if [[ "$mode" == rollback || "$mode" == uninstall ]]; then
    # Rollback and uninstall must work even when the operator-owned candidate
    # configuration or its directory is missing or malformed.
    declare -gA cfg=()
    if [[ ! -e "$etc_root" ]]; then return; fi
    secure_directory "$etc_root" 700 0 || block 'configuration directory has an unsafe owner, mode, or type'
    if [[ -f "$config" && ! -L "$config" && $(stat -c '%u:%a' "$config" 2>/dev/null) == "$expected_uid:600" ]]; then
      validated_config=$(mktemp)
      install -m 0600 "$config" "$validated_config"
      config=$validated_config
      cfg_dump=$(parse_file "$config" cfg configuration "$config_keys" && declare -p cfg) 2>/dev/null || cfg_dump=
      if [[ -n "$cfg_dump" ]]; then eval "$cfg_dump"; else cfg=(); fi
      if [[ ${cfg[SCHEMA_VERSION]:-} == 1 && ${cfg[ENVIRONMENT]:-} =~ ^[a-z][a-z0-9-]{0,31}$ && ${cfg[TARGET_ID]:-} =~ ^[a-z0-9][a-z0-9._-]{0,63}$ ]]; then
        environment=${cfg[ENVIRONMENT]}; target=${cfg[TARGET_ID]}
      fi
    fi
    return
  fi
  secure_directory "$etc_root" 700 0 || block 'configuration directory is missing'
  secure_file "$config" 'configuration file'
  validated_config=$(mktemp)
  install -m 0600 "$config" "$validated_config"
  config=$validated_config
  parse_file "$config" cfg configuration "$config_keys"
  local key candidate_environment candidate_target candidate_core candidate_artifact
  for key in SCHEMA_VERSION ENVIRONMENT TARGET_ID; do
    [[ -v "cfg[$key]" ]] || block "configuration is missing required key: $key"
  done
  [[ ${cfg[SCHEMA_VERSION]} == 1 ]] || block 'unsupported configuration schema'
  candidate_environment=${cfg[ENVIRONMENT]}; candidate_target=${cfg[TARGET_ID]}
  [[ "$candidate_environment" =~ ^[a-z][a-z0-9-]{0,31}$ ]] || block 'invalid explicit environment'
  [[ "$candidate_target" =~ ^[a-z0-9][a-z0-9._-]{0,63}$ ]] || block 'invalid explicit target identity'
  environment=$candidate_environment; target=$candidate_target
  if [[ "$mode" == drain || "$mode" == uninstall || "$mode" == rollback ]]; then return; fi
  secure_directory "$etc_root/adapters" 700 0 || block 'adapter directory is missing'
  secure_directory "$etc_root/credentials" 700 0 || block 'credential directory is missing'
  secure_directory "$etc_root/evidence" 700 0 || block 'evidence directory is missing'
  for key in SCHEMA_VERSION CORE_REF ENVIRONMENT TARGET_ID DEPLOYER_IDENTITY ADAPTER_PATH ADAPTER_SHA256 CREDENTIAL_PROVIDER CREDENTIAL_REF CREDENTIAL_SCOPE APPROVAL_PROVIDER APPROVAL_EVIDENCE_PATH CHECKPOINT_EVIDENCE_PATH SOURCE_COMMIT ARTIFACT_IMAGE NETWORK_HOST MIN_DISK_GIB REQUIRE_COMPOSE; do
    [[ -v "cfg[$key]" ]] || block "configuration is missing required key: $key"
  done
  [[ ${cfg[DEPLOYER_IDENTITY]} =~ ^[a-z0-9][a-z0-9._-]{0,63}$ ]] || block 'invalid deployer identity'
  [[ ${cfg[CREDENTIAL_SCOPE]} == "$environment" ]] || block 'credential scope must exactly match the explicit environment'
  candidate_core=${cfg[CORE_REF]}; candidate_artifact=${cfg[ARTIFACT_IMAGE]}
  [[ "$candidate_core" =~ ^[0-9a-f]{40}$ && ${cfg[SOURCE_COMMIT]} =~ ^[0-9a-f]{40}$ ]] || block 'core and source revisions must be full lowercase commit SHAs'
  [[ "$candidate_artifact" =~ ^[a-z0-9][a-z0-9.-]*(:[0-9]{1,5})?/[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$ ]] || block 'artifact image must be an immutable qualified digest reference'
  core_ref=$candidate_core; artifact=$candidate_artifact
  [[ ${cfg[ADAPTER_SHA256]} =~ ^[0-9a-f]{64}$ ]] || block 'adapter digest must be lowercase SHA-256'
  [[ ${cfg[NETWORK_HOST]} =~ ^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$ ]] || block 'invalid network prerequisite host'
  [[ ${cfg[MIN_DISK_GIB]} =~ ^[1-9][0-9]{0,3}$ ]] || block 'MIN_DISK_GIB must be a positive integer'
  [[ ${cfg[REQUIRE_COMPOSE]} == 0 || ${cfg[REQUIRE_COMPOSE]} == 1 ]] || block 'REQUIRE_COMPOSE must be 0 or 1'
  inside "${cfg[ADAPTER_PATH]}" "$etc_root/adapters" || block 'adapter path is outside the approved adapter directory'
  secure_file "${cfg[ADAPTER_PATH]}" 'adapter file' 700
  [[ $(sha256sum "${cfg[ADAPTER_PATH]}" | cut -d' ' -f1) == "${cfg[ADAPTER_SHA256]}" ]] || block 'adapter digest does not match the protected regular file'
  case ${cfg[CREDENTIAL_PROVIDER]} in
    file)
      inside "${cfg[CREDENTIAL_REF]}" "$etc_root/credentials" || block 'credential reference is outside the approved credential directory'
      [[ ! -L ${cfg[CREDENTIAL_REF]} && -f ${cfg[CREDENTIAL_REF]} ]] || block 'credential reference must be a regular file, not a symlink'
      [[ ${cfg[CREDENTIAL_REF]} == "$(realpath -m -- "${cfg[CREDENTIAL_REF]}")" && $(realpath -e -- "${cfg[CREDENTIAL_REF]}") == "${cfg[CREDENTIAL_REF]}" ]] || block 'credential reference contains a symlink or non-canonical component'
      [[ $(stat -c '%u:%a' "${cfg[CREDENTIAL_REF]}") == "$expected_uid:600" ]] || block 'credential file must be owner-only mode 0600'
      ;;
    external)
      [[ ${cfg[CREDENTIAL_REF]} =~ ^external:[a-z0-9][a-z0-9-]{0,31}:[A-Za-z0-9._/-]{1,128}$ ]] || block 'invalid external secret-manager adapter reference'
      ;;
    *) block 'CREDENTIAL_PROVIDER must be file or external' ;;
  esac
  validate_evidence
  validate_production_gate
}

validate_production_gate() {
  local key
  if [[ "$environment" != production ]]; then
    [[ ! -v 'cfg[PRODUCTION_AUTHORIZATION_EVIDENCE_PATH]' ]] || block 'production authorization evidence is forbidden outside production'
    return
  fi
  [[ -v 'cfg[PRODUCTION_AUTHORIZATION_EVIDENCE_PATH]' ]] || block 'production requires separate authorization evidence'
  inside "${cfg[PRODUCTION_AUTHORIZATION_EVIDENCE_PATH]}" "$etc_root/evidence" || block 'production authorization evidence is outside the approved evidence directory'
  secure_file "${cfg[PRODUCTION_AUTHORIZATION_EVIDENCE_PATH]}" 'production authorization evidence'
  parse_file "${cfg[PRODUCTION_AUTHORIZATION_EVIDENCE_PATH]}" production_gate 'production authorization evidence' 'SCHEMA_VERSION ENVIRONMENT TARGET_ID SOURCE_COMMIT ARTIFACT_IMAGE AUTHORIZED_BY GATE_ID AUTHORIZED_AT'
  for key in SCHEMA_VERSION ENVIRONMENT TARGET_ID SOURCE_COMMIT ARTIFACT_IMAGE AUTHORIZED_BY GATE_ID AUTHORIZED_AT; do
    [[ -v "production_gate[$key]" ]] || block "production authorization evidence is missing $key"
  done
  [[ ${production_gate[SCHEMA_VERSION]} == 1 && ${production_gate[ENVIRONMENT]} == production ]] || block 'production authorization evidence has the wrong scope'
  for key in TARGET_ID SOURCE_COMMIT ARTIFACT_IMAGE; do [[ ${production_gate[$key]} == "${cfg[$key]}" ]] || block "production authorization evidence does not match exact $key"; done
  [[ ${production_gate[AUTHORIZED_BY]} =~ ^[A-Za-z0-9._:@/-]{1,128}$ && ${production_gate[GATE_ID]} =~ ^[A-Za-z0-9._:@/-]{1,128}$ ]] || block 'production authorization identity is malformed'
  valid_utc "${production_gate[AUTHORIZED_AT]}" || block 'production authorization timestamp must be UTC RFC3339'
}

validate_evidence() {
  local allowed='SCHEMA_VERSION ENVIRONMENT TARGET_ID SOURCE_COMMIT ARTIFACT_IMAGE APPROVAL_IDENTITY POLICY_IDENTITY APPROVAL_ID APPROVED_AT'
  inside "${cfg[APPROVAL_EVIDENCE_PATH]}" "$etc_root/evidence" || block 'approval evidence is outside the approved evidence directory'
  secure_file "${cfg[APPROVAL_EVIDENCE_PATH]}" 'approval evidence'
  parse_file "${cfg[APPROVAL_EVIDENCE_PATH]}" approval 'approval evidence' "$allowed"
  local key
  for key in SCHEMA_VERSION ENVIRONMENT TARGET_ID SOURCE_COMMIT ARTIFACT_IMAGE APPROVAL_IDENTITY POLICY_IDENTITY APPROVAL_ID APPROVED_AT; do
    [[ -v "approval[$key]" ]] || block "approval evidence is missing $key"
  done
  [[ ${approval[SCHEMA_VERSION]} == 1 ]] || block 'unsupported approval evidence schema'
  for key in ENVIRONMENT TARGET_ID SOURCE_COMMIT ARTIFACT_IMAGE; do
    [[ ${approval[$key]} == "${cfg[$key]}" ]] || block "approval evidence does not match exact $key"
  done
  [[ ${approval[APPROVAL_IDENTITY]} =~ ^[A-Za-z0-9._:@/-]{1,128}$ && ${approval[POLICY_IDENTITY]} =~ ^[A-Za-z0-9._:@/-]{1,128}$ && ${approval[APPROVAL_ID]} =~ ^[A-Za-z0-9._:@/-]{1,128}$ ]] || block 'approval identity is malformed'
  valid_utc "${approval[APPROVED_AT]}" || block 'approval timestamp must be UTC RFC3339'
  case ${cfg[APPROVAL_PROVIDER]} in
    manual-exact-head|external-exact-head) ;;
    github-environment)
      [[ -v 'cfg[APPROVAL_CAPABILITY_EVIDENCE_PATH]' ]] || block 'GitHub Environment approval requires capability evidence'
      inside "${cfg[APPROVAL_CAPABILITY_EVIDENCE_PATH]}" "$etc_root/evidence" || block 'capability evidence is outside the approved evidence directory'
      secure_file "${cfg[APPROVAL_CAPABILITY_EVIDENCE_PATH]}" 'GitHub capability evidence'
      parse_file "${cfg[APPROVAL_CAPABILITY_EVIDENCE_PATH]}" capability 'capability evidence' 'SCHEMA_VERSION ENVIRONMENT TARGET_ID ENVIRONMENT_PROTECTION EXACT_HEAD CAPABILITY_ID CHECKED_AT'
      [[ ${capability[ENVIRONMENT]:-} == "${cfg[ENVIRONMENT]}" && ${capability[TARGET_ID]:-} == "${cfg[TARGET_ID]}" ]] || block 'GitHub Environment capability evidence does not match this installation'
      [[ ${capability[SCHEMA_VERSION]:-} == 1 && ${capability[ENVIRONMENT_PROTECTION]:-} == verified && ${capability[EXACT_HEAD]:-} == "${cfg[SOURCE_COMMIT]}" ]] || block 'GitHub Environment capability evidence is not exact-head verified'
      if [[ ! ${capability[CAPABILITY_ID]:-} =~ ^[A-Za-z0-9._:@/-]{1,128}$ ]] || ! valid_utc "${capability[CHECKED_AT]:-}"; then block 'GitHub Environment capability evidence is missing identity or UTC time'; fi
      ;;
    *) block 'unsupported approval provider' ;;
  esac
  inside "${cfg[CHECKPOINT_EVIDENCE_PATH]}" "$etc_root/evidence" || block 'checkpoint evidence is outside the approved evidence directory'
  secure_file "${cfg[CHECKPOINT_EVIDENCE_PATH]}" 'checkpoint evidence'
  parse_file "${cfg[CHECKPOINT_EVIDENCE_PATH]}" checkpoint 'checkpoint evidence' 'SCHEMA_VERSION ENVIRONMENT TARGET_ID CHECKPOINT_ID RECORDED_AT'
  [[ ${checkpoint[SCHEMA_VERSION]:-} == 1 && ${checkpoint[ENVIRONMENT]:-} == "$environment" && ${checkpoint[TARGET_ID]:-} == "$target" ]] || block 'checkpoint evidence does not match the explicit environment and target'
  if [[ ! ${checkpoint[CHECKPOINT_ID]:-} =~ ^[A-Za-z0-9._:@/-]{1,128}$ ]] || ! valid_utc "${checkpoint[RECORDED_AT]:-}"; then block 'checkpoint evidence is malformed'; fi
}

require_host() {
  local command os_id os_version available required systemd_state disk_path
  for command in bash awk cut sort stat sha256sum readlink realpath install cmp mv cp rm mkdir mktemp chmod ln flock kill timeout env git python3 docker systemctl systemd-analyze systemd-inhibit timedatectl curl df date; do
    command -v "$command" >/dev/null || block "$command is required"
  done
  local os_release
  os_release=$(root_path /etc/os-release)
  [[ -f "$os_release" && ! -L "$os_release" ]] || block 'supported Linux os-release metadata is missing'
  os_id=$(awk -F= '$1=="ID" {gsub(/"/,"",$2); print $2}' "$os_release")
  os_version=$(awk -F= '$1=="VERSION_ID" {gsub(/"/,"",$2); print $2}' "$os_release")
  [[ "$os_id" == debian && "$os_version" =~ ^(12|13)(\.|$) || "$os_id" == ubuntu && "$os_version" =~ ^(22\.04|24\.04)$ ]] || block 'unsupported Linux distribution or release'
  [[ -d $(root_path /run/systemd/system) ]] || block 'systemd is not the active init system'
  systemd_state=$(systemctl is-system-running 2>/dev/null || true)
  [[ "$systemd_state" == running || "$systemd_state" == degraded ]] || block 'systemd is unavailable'
  docker info >/dev/null 2>&1 || block 'Docker Engine is unavailable'
  [[ ${cfg[REQUIRE_COMPOSE]} != 1 ]] || docker compose version >/dev/null 2>&1 || block 'Docker Compose v2 is required but unavailable'
  [[ $(timedatectl show -p NTPSynchronized --value 2>/dev/null) == yes ]] || block 'host time is not synchronized'
  disk_path=$state_root
  while [[ ! -e "$disk_path" ]]; do disk_path=$(dirname "$disk_path"); done
  available=$(df -Pk "$disk_path" | awk 'NR==2 {print $4}')
  required=$((cfg[MIN_DISK_GIB] * 1024 * 1024))
  ((available >= required)) || block 'insufficient deployer disk capacity'
  if [[ "$testing" != 1 || ${CI_FLEET_DEPLOYER_TEST_NETWORK:-} != ok ]]; then
    printf '%s\n' "${cfg[NETWORK_HOST]}" | python3 -c 'import socket,sys; socket.getaddrinfo(sys.stdin.readline().strip(), 443)' >/dev/null 2>&1 || block 'network prerequisite DNS lookup failed'
  fi
  printf 'url = "https://%s/"\nconnect-timeout = 5\nmax-time = 10\nhead\nsilent\n' "${cfg[NETWORK_HOST]}" | curl --config - >/dev/null 2>&1 || block 'network prerequisite HTTPS check failed'
  reject_mixed_role
}

require_maintenance_host() {
  local command
  for command in bash awk cut stat sha256sum readlink realpath install cp rm mkdir mktemp chmod ln mv flock kill timeout env python3 systemctl systemd-analyze date; do
    command -v "$command" >/dev/null || block "$command is required for maintenance"
  done
  [[ -d "$systemd_root" && ! -L "$systemd_root" ]] || block 'systemd unit directory is unavailable'
}

reject_mixed_role() {
  local unit line output expected="deployer|${cfg[DEPLOYER_IDENTITY]}" runner_unit
  for unit in ci-fleet-health.service ci-fleet-reconcile.service ci-fleet-cleanup.service actions.runner.service; do
    [[ ! -e "$systemd_root/$unit" ]] || block 'ordinary CI controller or runner state is present'
  done
  shopt -s nullglob
  for runner_unit in "$systemd_root"/actions.runner.*.service "$systemd_root"/multi-user.target.wants/actions.runner.*.service; do
    shopt -u nullglob
    [[ -n "$runner_unit" ]] && block 'ordinary GitHub Actions runner service is present'
  done
  shopt -u nullglob
  for path in "$(root_path /etc/ci-fleet/ci-fleet.env)" "$(root_path /opt/ci-fleet/current)" "$(root_path /var/lib/ci-fleet/install-state.json)"; do
    [[ ! -e "$path" && ! -L "$path" ]] || block 'ordinary CI controller or runner state is present'
  done
  output=$(docker ps -a --format '{{.ID}}|{{.Label "io.randomdevelopment.ci-fleet.role"}}|{{.Label "io.randomdevelopment.ci-fleet.identity"}}') || block 'Docker workload inventory failed'
  while IFS= read -r line; do [[ -z "$line" || ${line#*|} == "$expected" ]] || block 'unrelated Docker workload is present'; done <<<"$output"
  output=$(docker network ls --filter type=custom --format '{{.ID}}|{{.Label "io.randomdevelopment.ci-fleet.role"}}|{{.Label "io.randomdevelopment.ci-fleet.identity"}}') || block 'Docker network inventory failed'
  while IFS= read -r line; do [[ -z "$line" || ${line#*|} == "$expected" ]] || block 'incompatible custom Docker network is present'; done <<<"$output"
  output=$(docker volume ls --format '{{.Name}}|{{.Label "io.randomdevelopment.ci-fleet.role"}}|{{.Label "io.randomdevelopment.ci-fleet.identity"}}') || block 'Docker volume inventory failed'
  while IFS= read -r line; do [[ -z "$line" || ${line#*|} == "$expected" ]] || block 'incompatible Docker volume is present'; done <<<"$output"
}

validate_checkout() {
  local head
  git_checkout() { git -c core.fsmonitor= -c core.hooksPath=/dev/null -C "$repo_root" "$@"; }
  head=$(git_checkout --no-replace-objects rev-parse 'HEAD^{commit}') || block 'installer checkout is not Git-authored'
  [[ "$head" == "$core_ref" ]] || block 'CORE_REF must equal the exact reviewed checkout HEAD'
  if [[ "$testing" != 1 ]]; then
    git_checkout diff --quiet HEAD -- scripts/install-deployer.sh scripts/deployer-runtime.sh deploy/deployer || block 'reviewed deployer inputs differ from HEAD'
  fi
  # Pin the reviewed inputs before any privileged copy: copy the worktree
  # bytes once into a root-controlled snapshot outside managed state, then
  # require each copied file's Git blob identity to equal the pinned commit's
  # tree entry. The commit SHA is content-addressed, so no mutation of the
  # worktree, refs, or loose objects can substitute bytes under $head.
  local path blob tree_listing
  checkout_snapshot=$(mktemp -d)
  chmod 0700 "$checkout_snapshot"
  install -d -m 0700 "$checkout_snapshot/scripts" "$checkout_snapshot/deploy/deployer"
  install -m 0755 "$repo_root/scripts/install-deployer.sh" "$repo_root/scripts/deployer-runtime.sh" "$checkout_snapshot/scripts/"
  local entry
  for entry in "$repo_root"/deploy/deployer/*; do
    [[ -f "$entry" && ! -L "$entry" ]] || block 'deployer unit source contains an unsafe or untracked entry'
    install -m 0644 "$entry" "$checkout_snapshot/deploy/deployer/"
  done
  tree_listing=$(git_checkout --no-replace-objects ls-tree -r "$head" -- scripts/install-deployer.sh scripts/deployer-runtime.sh deploy/deployer) || block 'reviewed commit tree is unreadable'
  [[ -n $tree_listing ]] || block 'reviewed commit tree is unreadable'
  while read -r _ _ blob path; do
    [[ $blob == "$(git hash-object "$checkout_snapshot/$path")" ]] || block "checkout input $path differs from the reviewed commit"
  done <<<"$tree_listing"
  # The snapshot must contain exactly the reviewed entries: any extra copied
  # path (for example an untracked symlink target) is unreviewed content.
  local snapshot_listing
  snapshot_listing=$(cd "$checkout_snapshot" && find scripts deploy -type f | sort)
  [[ $snapshot_listing == "$(awk '{print $4}' <<<"$tree_listing" | sort)" ]] || block 'checkout snapshot contains unreviewed entries'
  repo_root=$checkout_snapshot
  unit_source=$repo_root/deploy/deployer
}

active_deployment() {
  local pid started now boot_id start_time live_start
  [[ -f "$active_operation" && ! -L "$active_operation" ]] || return 1
  pid=$(awk -F= '$1=="pid" {print $2}' "$active_operation")
  started=$(awk -F= '$1=="started_at" {print $2}' "$active_operation")
  [[ "$pid" =~ ^[1-9][0-9]*$ && "$started" =~ ^[0-9]+$ ]] || return 0
  if kill -0 "$pid" 2>/dev/null; then
    if [[ "$testing" == 1 ]]; then
      [[ -n ${CI_FLEET_DEPLOYER_TEST_LIVE_PID:-} && $pid == "$CI_FLEET_DEPLOYER_TEST_LIVE_PID" ]] && return 0
      return 1
    fi
    boot_id=$(awk -F= '$1=="boot_id" {print $2}' "$active_operation")
    start_time=$(awk -F= '$1=="start_time" {print $2}' "$active_operation")
    if [[ -n $boot_id && -n $start_time ]]; then
      [[ $boot_id == "$(</proc/sys/kernel/random/boot_id)" ]] || return 1
      live_start=$(awk '{print $22}' /proc/"$pid"/stat 2>/dev/null)
      [[ -n $live_start ]] || return 0
      [[ $live_start == "$start_time" ]] || return 1
    fi
    return 0
  fi
  now=$(date +%s)
  ((started <= now)) || return 0
  ((now - started <= 3600)) || return 1
  return 0
}

release_complete() {
  local release=$1 stored actual unit entry dir
  [[ -d "$release" && ! -L "$release" && $(stat -c '%u:%a' "$release") == "$expected_uid:755" && -x "$release/scripts/install-deployer.sh" && -x "$release/scripts/deployer-runtime.sh" ]] || return 1
  for dir in "$release/scripts" "$release/deploy" "$release/deploy/deployer"; do [[ -d "$dir" && ! -L "$dir" && $(stat -c '%u:%a' "$dir") == "$expected_uid:755" ]] || return 1; done
  for entry in "$release/scripts/install-deployer.sh" "$release/scripts/deployer-runtime.sh"; do [[ ! -L "$entry" && $(stat -c '%u:%a' "$entry") == "$expected_uid:755" ]] || return 1; done
  [[ -f "$release/.ci-fleet-tree-sha256" ]] || return 1
  stored=$(<"$release/.ci-fleet-tree-sha256")
  actual=$(cd "$release" && sha256sum scripts/install-deployer.sh scripts/deployer-runtime.sh deploy/deployer/* | sha256sum | cut -d' ' -f1)
  [[ "$stored" == "$actual" ]] || return 1
  for unit in "${unit_names[@]}"; do [[ -f "$release/deploy/deployer/$unit" && ! -L "$release/deploy/deployer/$unit" && $(stat -c '%u:%a' "$release/deploy/deployer/$unit") == "$expected_uid:644" ]] || return 1; done
}

state_matches() {
  [[ -f "$state_file" && ! -L "$state_file" && $(stat -c '%u:%a' "$state_file") == "$expected_uid:600" ]] || return 1
  [[ -f "$active_policy" && ! -L "$active_policy" && $(stat -c '%u:%a' "$active_policy") == "$expected_uid:600" && $(cmp -s "$config" "$active_policy"; echo $?) == 0 ]] || return 1
  printf '%s\n' "$core_ref" "$environment" "$target" "${cfg[DEPLOYER_IDENTITY]}" "${cfg[SOURCE_COMMIT]}" "$artifact" "${approval[APPROVAL_ID]}" "${approval[APPROVAL_IDENTITY]}" "${approval[POLICY_IDENTITY]}" "${cfg[APPROVAL_PROVIDER]}" "${checkpoint[CHECKPOINT_ID]}" | python3 -c '
import json, sys
try: value=json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError): raise SystemExit(1)
keys=("core_ref","environment","target","deployer_identity","source_commit","artifact","approval_id","approval_identity","policy_identity","approval_provider","checkpoint_id")
expected=[line.rstrip("\n") for line in sys.stdin]
raise SystemExit(0 if all(value.get(k)==v for k,v in zip(keys,expected)) else 1)
' "$state_file"
}

units_match() {
  local unit
  for unit in "${unit_names[@]}"; do
    [[ -f "$systemd_root/$unit" && ! -L "$systemd_root/$unit" && $(stat -c '%u:%a' "$systemd_root/$unit") == "$expected_uid:644" ]] || return 1
    cmp -s "$unit_source/$unit" "$systemd_root/$unit" || return 1
    [[ ! -e "$systemd_root/$unit.d" && ! -L "$systemd_root/$unit.d" ]] || return 1
  done
  for unit in "${timer_names[@]}"; do [[ $(systemctl is-enabled "$unit" 2>/dev/null) == enabled ]] && systemctl is-active "$unit" >/dev/null 2>&1 || return 1; done
}

current_matches() {
  local target_path=$releases/$core_ref
  [[ -L "$current" && $(readlink "$current") == "releases/$core_ref" ]] || return 1
  release_complete "$target_path"
}

managed_boundaries_match() {
  local path mode
  for path in "$install_root:755" "$releases:755" "$state_root:700" "$lock_root:700" "$log_root:700"; do
    mode=${path##*:}; path=${path%:*}
    [[ -d "$path" && ! -L "$path" && $(stat -c '%u:%a' "$path") == "$expected_uid:$mode" ]] || return 1
  done
  [[ -d "$systemd_root" && ! -L "$systemd_root" && $(stat -c %u "$systemd_root") == "$expected_uid" ]] || return 1
  mode=$(stat -c %a "$systemd_root")
  (((8#$mode & 8#022) == 0))
}

converged() {
  [[ -e "$deployed_current" || -L "$deployed_current" ]] || return 1
  (load_deployed_snapshot) >/dev/null 2>&1 || return 1
  managed_boundaries_match && current_matches && state_matches && units_match
}

acquire_lock() {
  local path
  secure_directory "$lock_root" 700 1
  if [[ -e "$state_root" || -L "$state_root" ]]; then secure_directory "$state_root" 700 0 || block 'managed state boundary is unsafe'; fi
  exec 9<"$lock_root"
  flock -n 9 || block 'another deployer installer operation is running'
  [[ ! -L "$active_operation" ]] || block 'active operation marker is an unsafe symlink'
  if [[ -e "$active_operation" && ! -f "$active_operation" ]]; then block 'active operation marker has an unsafe type'; fi
  if [[ -e "$active_operation" ]] && ! active_deployment; then
    [[ ! -L "$active_operation" && -f "$active_operation" && $(stat -c '%u:%a' "$active_operation") == "$expected_uid:600" ]] || block 'stale operation state is unsafe'
    rm -f "$active_operation"
  fi
  if [[ -d "$releases" ]]; then
    secure_directory "$install_root" 755 0
    secure_directory "$releases" 755 0
    shopt -s nullglob
    for path in "$releases"/."$core_ref".staging.*; do
      [[ ! -L "$path" && -d "$path" && $(stat -c '%u:%a' "$path") == "$expected_uid:755" ]] || block 'interrupted release staging state is unsafe'
      rm -rf -- "$path"
    done
    shopt -u nullglob
  fi
  shopt -s nullglob
  for path in "$state_root"/.transaction-preparing.*; do
    [[ ! -L "$path" && -d "$path" && $(stat -c '%u:%a' "$path") == "$expected_uid:700" ]] || block 'incomplete transaction preparation is unsafe'
    rm -rf -- "$path"
  done
  shopt -u nullglob
  recover_interrupted_transaction
}

acquire_check_lock() {
  [[ -d "$lock_root" && ! -L "$lock_root" && $(stat -c '%u:%a' "$lock_root") == "$expected_uid:700" ]] || block 'installed deployer lock boundary is absent or unsafe'
  if [[ -e "$active_operation" || -L "$active_operation" ]]; then
    [[ -f "$active_operation" && ! -L "$active_operation" ]] || block 'active operation marker has an unsafe type'
  fi
  exec 9<"$lock_root"
  flock -n 9 || block 'another deployer operation is running'
}

begin_transaction() {
  local name path current_target transaction_name transaction_ready deployed_target enabled_state systemd_mode
  [[ -d "$systemd_root" && ! -L "$systemd_root" && $(stat -c %u "$systemd_root") == "$expected_uid" ]] || block 'systemd unit directory has an unsafe owner or type'
  systemd_mode=$(stat -c %a "$systemd_root")
  (((8#$systemd_mode & 8#022) == 0)) || block 'systemd unit directory is group- or world-writable'
  [[ -d "$install_root" && ! -L "$install_root" && $(stat -c '%u:%a' "$install_root") == "$expected_uid:755" ]] || block 'managed install boundary has an unsafe owner, mode, or type'
  for name in install-state.json active-policy.conf last-known-good.json last-known-good-policy.conf; do
    path=$state_root/$name
    [[ ! -e "$path" && ! -L "$path" ]] || [[ -f "$path" && ! -L "$path" && $(stat -c '%u:%a' "$path") == "$expected_uid:600" ]] || block 'managed transaction state has an unsafe type, owner, or mode'
  done
  if [[ -L "$current" ]]; then
    current_target=$(readlink "$current")
    [[ "$current_target" =~ ^releases/[0-9a-f]{40}$ ]] || block 'current release pointer is unsafe'
  elif [[ -e "$current" ]]; then block 'current release pointer has an unsafe type'
  fi
  for name in "${unit_names[@]}"; do
    path=$systemd_root/$name
    [[ ! -e "$path" && ! -L "$path" ]] || [[ -f "$path" && ! -L "$path" && $(stat -c '%u:%a' "$path") == "$expected_uid:644" ]] || block 'managed systemd unit has an unsafe owner, mode, or type'
  done
  transaction_dir=$(mktemp -d "$state_root/.transaction-preparing.XXXXXX")
  transaction_preparing=1
  chmod 0700 "$transaction_dir"
  install -d -m 0700 "$transaction_dir/units" "$transaction_dir/state"
  for name in install-state.json active-policy.conf last-known-good.json last-known-good-policy.conf; do
    path=$state_root/$name
    if [[ -f "$path" ]]; then
      install -m 0600 "$path" "$transaction_dir/state/$name"
      printf '%s\n' "$name" >>"$transaction_dir/state-present"
    fi
  done
  [[ -z ${current_target:-} ]] || printf '%s\n' "$current_target" >"$transaction_dir/current-target"
  for name in "${unit_names[@]}"; do
    path=$systemd_root/$name
    if [[ -f "$path" ]]; then
      install -m 0644 "$path" "$transaction_dir/units/$name"
      printf '%s\n' "$name" >>"$transaction_dir/units-present"
    fi
  done
  for name in "${timer_names[@]}"; do
    enabled_state=$(systemctl is-enabled "$name" 2>/dev/null) || enabled_state=
    if [[ $enabled_state == enabled ]]; then printf '%s\n' "$name" >>"$transaction_dir/timers-enabled"; fi
    if [[ $enabled_state == enabled-runtime ]]; then printf '%s\n' "$name" >>"$transaction_dir/timers-enabled-runtime"; fi
    if systemctl is-active "$name" >/dev/null 2>&1; then printf '%s\n' "$name" >>"$transaction_dir/timers-active"; fi
  done
  if [[ -L "$deployed_current" ]]; then
    deployed_target=$(readlink "$deployed_current")
    [[ "$deployed_target" =~ ^\.snapshot\.[A-Za-z0-9._-]+$ ]] || block 'deployed snapshot pointer is unsafe'
    printf '%s\n' "$deployed_target" >"$transaction_dir/deployed-target"
  elif [[ -e "$deployed_current" ]]; then block 'deployed snapshot pointer has an unsafe type'
  else printf 'absent\n' >"$transaction_dir/deployed-target"
  fi
  transaction_name=${transaction_dir##*/}
  transaction_ready=$state_root/.transaction.${transaction_name#.transaction-preparing.}
  sync -f "$transaction_dir" 2>/dev/null || sync "$transaction_dir" 2>/dev/null || true
  mv "$transaction_dir" "$transaction_ready"
  transaction_dir=$transaction_ready
  sync -f "$state_root" 2>/dev/null || sync "$state_root" 2>/dev/null || true
  transaction_preparing=0
}

restore_transaction() {
  local name target_value backed_up systemd_restore_mode
  [[ -n ${transaction_dir:-} && -d $transaction_dir ]] || return 0
  transaction_committed=1
  for name in units-present state-present timers-enabled timers-enabled-runtime timers-active current-target deployed-target deployed-created; do
    [[ ! -e "$transaction_dir/$name" && ! -L "$transaction_dir/$name" ]] || [[ ! -L "$transaction_dir/$name" && -f "$transaction_dir/$name" ]] || block "transaction manifest $name has an unsafe type"
  done
  if [[ -f "$transaction_dir/units-present" ]]; then
    while IFS= read -r name; do
      [[ " ${unit_names[*]} " == *" $name "* && -f "$transaction_dir/units/$name" && ! -L "$transaction_dir/units/$name" ]] || block 'transaction unit manifest is unsafe'
    done <"$transaction_dir/units-present"
  fi
  if [[ -f "$transaction_dir/state-present" ]]; then
    while IFS= read -r name; do
      [[ " install-state.json active-policy.conf last-known-good.json last-known-good-policy.conf " == *" $name "* && -f "$transaction_dir/state/$name" && ! -L "$transaction_dir/state/$name" ]] || block 'transaction state manifest is unsafe'
    done <"$transaction_dir/state-present"
  fi
  for name in timers-enabled timers-enabled-runtime timers-active; do
    if [[ -f "$transaction_dir/$name" ]]; then
      while IFS= read -r target_value; do
        [[ " ${timer_names[*]} " == *" $target_value "* ]] || block 'transaction timer manifest is unsafe'
      done <"$transaction_dir/$name"
    fi
  done
  if [[ -f "$transaction_dir/current-target" ]]; then
    [[ $(<"$transaction_dir/current-target") =~ ^releases/[0-9a-f]{40}$ ]] || block 'transaction current pointer is unsafe'
  fi
  if [[ -f "$transaction_dir/deployed-target" ]]; then
    target_value=$(<"$transaction_dir/deployed-target")
    [[ "$target_value" == absent || "$target_value" =~ ^\.snapshot\.[A-Za-z0-9._-]+$ ]] || block 'transaction deployed pointer is unsafe'
  fi
  for name in units state; do
    [[ -d "$transaction_dir/$name" && ! -L "$transaction_dir/$name" ]] || block "transaction $name backup directory is unsafe"
    backed_up=$(cd "$transaction_dir/$name" && shopt -s nullglob; printf '%s\n' * | sort)
    if [[ -f "$transaction_dir/$name-present" ]]; then
      [[ $backed_up == "$(sort "$transaction_dir/$name-present")" ]] || block "transaction $name manifest does not match its backup directory"
    else
      [[ -z $backed_up ]] || block "transaction $name manifest is missing but backups remain"
    fi
  done
  [[ -d "$systemd_root" && ! -L "$systemd_root" && $(stat -c %u "$systemd_root") == "$expected_uid" ]] || block 'systemd unit directory has an unsafe owner or type'
  systemd_restore_mode=$(stat -c %a "$systemd_root")
  (((8#$systemd_restore_mode & 8#022) == 0)) || block 'systemd unit directory is group- or world-writable'
  [[ -d "$install_root" && ! -L "$install_root" && $(stat -c '%u:%a' "$install_root") == "$expected_uid:755" ]] || block 'managed install boundary has an unsafe owner, mode, or type'
  for name in "${timer_names[@]}"; do
    if [[ -e "$systemd_root/$name" || -L "$systemd_root/$name" ]]; then
      systemctl disable --now "$name" >/dev/null 2>&1 || return
    fi
  done
  for name in "${unit_names[@]}"; do rm -f -- "$systemd_root/$name" || return; done
  if [[ -f "$transaction_dir/units-present" ]]; then
    while IFS= read -r name; do
      install -m 0644 "$transaction_dir/units/$name" "$systemd_root/$name" || return
    done <"$transaction_dir/units-present"
  fi
  for name in install-state.json active-policy.conf last-known-good.json last-known-good-policy.conf; do rm -f -- "$state_root/$name" || return; done
  if [[ -f "$transaction_dir/state-present" ]]; then
    while IFS= read -r name; do
      install -m 0600 "$transaction_dir/state/$name" "$state_root/$name" || return
    done <"$transaction_dir/state-present"
  fi
  rm -f -- "$current" "$install_root/.current.new" "$state_root/.install-state.new" "$active_policy.new" "$state_file.new" || return
  if [[ -f "$transaction_dir/current-target" ]]; then
    target_value=$(<"$transaction_dir/current-target")
    ln -s "$target_value" "$current" || return
  fi
  if [[ -f "$transaction_dir/deployed-target" ]]; then
    target_value=$(<"$transaction_dir/deployed-target")
    if [[ "$target_value" == absent ]]; then
      rm -f -- "$deployed_current" || return
    else
      rm -f -- "$deployed_current" || return
      ln -s "$target_value" "$deployed_current" || return
    fi
  fi
  if [[ -f "$transaction_dir/deployed-created" ]]; then
    target_value=$(<"$transaction_dir/deployed-created")
    [[ $target_value =~ ^\.snapshot\.[A-Za-z0-9._-]+$ ]] || block 'transaction created snapshot name is unsafe'
    if [[ ! -e $deployed_current && ! -L $deployed_current ]] || [[ $(readlink "$deployed_current" 2>/dev/null) != "$target_value" ]]; then
      [[ ! -d "$deployed_root/$target_value" || -L "$deployed_root/$target_value" ]] || rm -rf -- "${deployed_root:?}/$target_value"
    fi
  fi
  systemctl daemon-reload >/dev/null 2>&1 || return
  if [[ -f "$transaction_dir/timers-enabled" ]]; then
    while IFS= read -r name; do
      systemctl enable "$name" >/dev/null 2>&1 || return
    done <"$transaction_dir/timers-enabled"
  fi
  if [[ -f "$transaction_dir/timers-enabled-runtime" ]]; then
    while IFS= read -r name; do
      systemctl enable --runtime "$name" >/dev/null 2>&1 || return
    done <"$transaction_dir/timers-enabled-runtime"
  fi
  if [[ -f "$transaction_dir/timers-active" ]]; then
    while IFS= read -r name; do
      systemctl start "$name" >/dev/null 2>&1 || return
    done <"$transaction_dir/timers-active"
  fi
  # Restored state, pointer, and units may live on separate filesystems; make
  # them durable before the only recovery journal is deleted, then persist the
  # journal retirement itself.
  sync -f "$state_root" 2>/dev/null || block 'restored host state is not durable'
  sync -f "$install_root" 2>/dev/null || block 'restored install root is not durable'
  sync -f "$systemd_root" 2>/dev/null || block 'restored systemd boundary is not durable'
  rm -rf -- "$transaction_dir" || return
  transaction_dir=
  sync -f "$state_root" 2>/dev/null || block 'retired recovery journal is not durable'
}

recover_interrupted_transaction() {
  local candidates=() candidate
  [[ -d "$state_root" ]] || return 0
  shopt -s nullglob
  candidates=("$state_root"/.transaction.*)
  shopt -u nullglob
  ((${#candidates[@]} <= 1)) || block 'multiple interrupted installer transactions require operator recovery'
  ((${#candidates[@]} == 1)) || return 0
  candidate=${candidates[0]}
  [[ ! -L "$candidate" && -d "$candidate" && $(stat -c '%u:%a' "$candidate") == "$expected_uid:700" ]] || block 'interrupted installer transaction is unsafe'
  transaction_dir=$candidate
  if [[ -e "$transaction_dir/application-rollback-committed" || -L "$transaction_dir/application-rollback-committed" ]]; then
    finalize_committed_rollback
    recovered_rollback=1
    transaction_committed=0
    return
  fi
  # Restoration does not consume deployed-snapshot dependencies; requiring
  # them here would wedge recovery behind unrelated deployed drift.
  restore_transaction
  transaction_committed=0
}

finalize_committed_rollback() {
  local marker=$transaction_dir/application-rollback-committed retired
  local final_adapter_path final_adapter_sha final_credential_provider final_credential_ref
  [[ ! -L "$marker" && -f "$marker" && $(stat -c '%u:%a' "$marker") == "$expected_uid:600" ]] || block 'application rollback commit marker is unsafe'
  secure_file "$active_policy" 'active policy' || return
  secure_file "$state_file" 'deployer install state' || return
  final_adapter_path=$(awk '$0 ~ /^ADAPTER_PATH=/ {sub(/^ADAPTER_PATH=/, ""); print}' "$active_policy")
  final_adapter_sha=$(awk '$0 ~ /^ADAPTER_SHA256=/ {sub(/^ADAPTER_SHA256=/, ""); print}' "$active_policy")
  inside "$final_adapter_path" "$etc_root/adapters" || return
  [[ ! -L "$final_adapter_path" && -f "$final_adapter_path" && $(stat -c '%u:%a' "$final_adapter_path") == "$expected_uid:700" ]] || return
  [[ $(sha256sum "$final_adapter_path" | cut -d' ' -f1) == "$final_adapter_sha" ]] || return
  final_credential_provider=$(awk '$0 ~ /^CREDENTIAL_PROVIDER=/ {sub(/^CREDENTIAL_PROVIDER=/, ""); print}' "$active_policy")
  final_credential_ref=$(awk '$0 ~ /^CREDENTIAL_REF=/ {sub(/^CREDENTIAL_REF=/, ""); print}' "$active_policy")
  credential_reference_safe "$final_credential_provider" "$final_credential_ref" 'committed rollback policy' || return
  python3 - "$state_file" "$active_policy" "$etc_root/adapters" "$etc_root/credentials" <<'PY' >/dev/null 2>&1 || return
import json, re, sys
try:
    state = json.load(open(sys.argv[1], encoding='utf-8'))
except (OSError, ValueError):
    raise SystemExit(1)
policy = {}
allowed = {"SCHEMA_VERSION","CORE_REF","ENVIRONMENT","TARGET_ID","DEPLOYER_IDENTITY","ADAPTER_PATH","ADAPTER_SHA256","CREDENTIAL_PROVIDER","CREDENTIAL_REF","CREDENTIAL_SCOPE","APPROVAL_PROVIDER","APPROVAL_EVIDENCE_PATH","APPROVAL_CAPABILITY_EVIDENCE_PATH","PRODUCTION_AUTHORIZATION_EVIDENCE_PATH","CHECKPOINT_EVIDENCE_PATH","SOURCE_COMMIT","ARTIFACT_IMAGE","NETWORK_HOST","MIN_DISK_GIB","REQUIRE_COMPOSE"}
try:
    for line in open(sys.argv[2], encoding='utf-8'):
        line = line.rstrip('\n')
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or key in policy or key not in allowed:
            raise SystemExit(1)
        policy[key] = value
except OSError:
    raise SystemExit(1)
sha = re.compile(r'^[0-9a-f]{40}$')
pairs = (('core_ref','CORE_REF'), ('environment','ENVIRONMENT'), ('target','TARGET_ID'), ('source_commit','SOURCE_COMMIT'), ('artifact','ARTIFACT_IMAGE'), ('deployer_identity','DEPLOYER_IDENTITY'))
ok = (policy.get('SCHEMA_VERSION') == '1'
      and all(state.get(k) and state.get(k) == policy.get(p) for k, p in pairs)
      and bool(sha.match(state['core_ref'])) and bool(sha.match(state['source_commit']))
      and bool(re.search(r'@sha256:[0-9a-f]{64}$', state['artifact'])))
ok = ok and all(policy.get(k) for k in ('ADAPTER_PATH','ADAPTER_SHA256','CREDENTIAL_PROVIDER','CREDENTIAL_REF','CREDENTIAL_SCOPE'))
ok = ok and bool(re.fullmatch(r'[0-9a-f]{64}', policy.get('ADAPTER_SHA256','')))
ok = ok and policy.get('ADAPTER_PATH','').startswith(sys.argv[3] + '/')
ok = ok and policy.get('CREDENTIAL_PROVIDER') in ('file','external')
ok = ok and policy.get('CREDENTIAL_SCOPE') == policy.get('ENVIRONMENT')
if policy.get('CREDENTIAL_PROVIDER') == 'file':
    ok = ok and policy.get('CREDENTIAL_REF','').startswith(sys.argv[4] + '/')
else:
    ok = ok and bool(re.fullmatch(r'external:[a-z0-9][a-z0-9-]{0,31}:[A-Za-z0-9._/-]{1,128}', policy.get('CREDENTIAL_REF','')))
raise SystemExit(0 if ok else 1)
PY
  publish_deployed_snapshot "$active_policy" "$state_file" || return
  rm -f "$previous_state" "$previous_policy" || return
  retired=$state_root/.retired.$$.transaction
  mv -Tf "$transaction_dir" "$retired" || return
  transaction_dir=
  transaction_committed=1
  rm -rf -- "$retired"
  # The journal rename and deletion must persist with the finalization, or a
  # reboot can resurrect the committed-rollback transaction and repeat it.
  sync -f "$state_root" 2>/dev/null || block 'retired recovery journal is not durable'
}

commit_transaction() {
  local retired
  sync -f "$state_file" "$active_policy" 2>/dev/null || block 'committed host state is not durable'
  sync -f "$state_root" 2>/dev/null || block 'committed host state is not durable'
  sync -f "$install_root" 2>/dev/null || block 'committed install root is not durable'
  sync -f "$systemd_root" 2>/dev/null || block 'committed systemd boundary is not durable'
  retired=$state_root/.retired.$$.transaction
  mv -Tf "$transaction_dir" "$retired" || return
  transaction_dir=
  transaction_committed=1
  rm -rf -- "$retired"
  # The journal rename and deletion must persist with the commit, or a reboot
  # can resurrect the pre-operation transaction and restore stale state.
  sync -f "$state_root" 2>/dev/null || block 'retired recovery journal is not durable'
}

atomic_replace_directory() {
  local replacement=$1 target_path=$2
  if [[ ! -e "$target_path" && ! -L "$target_path" ]]; then mv "$replacement" "$target_path"; return; fi
  [[ ! -L "$target_path" && -d "$target_path" ]] || block 'managed release target has an unsafe type'
  python3 - "$replacement" "$target_path" <<'PY'
import ctypes, os, sys
source, target = map(os.fsencode, sys.argv[1:])
libc = ctypes.CDLL(None, use_errno=True)
renameat2 = getattr(libc, 'renameat2', None)
if renameat2 is None:
    raise OSError('atomic directory exchange is unavailable')
renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
if renameat2(-100, source, -100, target, 2) != 0:
    error = ctypes.get_errno(); raise OSError(error, os.strerror(error))
fd = os.open(os.path.dirname(target), os.O_RDONLY | os.O_DIRECTORY)
try: os.fsync(fd)
finally: os.close(fd)
PY
}

install_release() {
  local release=$releases/$core_ref staging
  secure_directory "$install_root" 755 1
  secure_directory "$releases" 755 1
  if release_complete "$release"; then return; fi
  staging=$(mktemp -d "$releases/.${core_ref}.staging.XXXXXX")
  chmod 0755 "$staging"
  staging_path=$staging
  install -d -m 0755 "$staging/scripts" "$staging/deploy/deployer"
  install -m 0755 "$repo_root/scripts/install-deployer.sh" "$repo_root/scripts/deployer-runtime.sh" "$staging/scripts/"
  install -m 0644 "$unit_source"/* "$staging/deploy/deployer/"
  (cd "$staging" && sha256sum scripts/install-deployer.sh scripts/deployer-runtime.sh deploy/deployer/* | sha256sum | cut -d' ' -f1) >"$staging/.ci-fleet-tree-sha256"
  chmod 0644 "$staging/.ci-fleet-tree-sha256"
  release_complete "$staging" || die 'staged deployer release is incomplete'
  atomic_replace_directory "$staging" "$release"
  [[ ! -e "$staging" ]] || rm -rf -- "$staging"
  staging_path=
}

write_state() {
  local destination=$1 temporary
  temporary=$(mktemp "$state_root/.state.XXXXXX")
  printf '%s\n' "$core_ref" "$environment" "$target" "${cfg[DEPLOYER_IDENTITY]}" "${cfg[SOURCE_COMMIT]}" "$artifact" "${approval[APPROVAL_ID]}" "${approval[APPROVAL_IDENTITY]}" "${approval[POLICY_IDENTITY]}" "${cfg[APPROVAL_PROVIDER]}" "${checkpoint[CHECKPOINT_ID]}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" | python3 -c '
import json,sys
keys=("core_ref","environment","target","deployer_identity","source_commit","artifact","approval_id","approval_identity","policy_identity","approval_provider","checkpoint_id","installed_at")
values=[line.rstrip("\n") for line in sys.stdin]
with open(sys.argv[1],"w",encoding="utf-8") as f: json.dump(dict(zip(keys,values)),f,indent=2,sort_keys=True); f.write("\n")
' "$temporary"
  chmod 0600 "$temporary"
  mv -Tf "$temporary" "$destination"
}

load_deployed_snapshot() {
  local snapshot deployed_adapter_path deployed_adapter_sha
  local deployed_credential_provider deployed_credential_ref deployed_credential_scope deployed_environment
  secure_directory "$deployed_root" 700 0 || block 'deployed snapshot directory is missing'
  [[ -L "$deployed_current" ]] || block 'deployed snapshot pointer is absent or unsafe'
  [[ $(readlink "$deployed_current") =~ ^\.snapshot\.[A-Za-z0-9._-]+$ ]] || block 'deployed snapshot pointer target is not canonical'
  snapshot=$(readlink -f "$deployed_current")
  inside "$snapshot" "$deployed_root" || block 'deployed snapshot pointer escapes managed state'
  secure_directory "$snapshot" 700 0 || block 'deployed snapshot is unsafe'
  secure_file "$snapshot/policy.conf" 'deployed rollback policy'
  secure_file "$snapshot/state.json" 'deployed rollback state'
  deployed_snapshot_policy=$snapshot/policy.conf
  deployed_snapshot_state=$snapshot/state.json
  deployed_adapter_path=$(awk '$0 ~ /^ADAPTER_PATH=/ {sub(/^ADAPTER_PATH=/, ""); print}' "$deployed_snapshot_policy")
  deployed_adapter_sha=$(awk '$0 ~ /^ADAPTER_SHA256=/ {sub(/^ADAPTER_SHA256=/, ""); print}' "$deployed_snapshot_policy")
  inside "$deployed_adapter_path" "$etc_root/adapters" || block 'deployed rollback adapter is outside the protected adapter directory'
  [[ ! -L "$deployed_adapter_path" && -f "$deployed_adapter_path" && $(stat -c '%u:%a' "$deployed_adapter_path") == "$expected_uid:700" ]] || block 'deployed rollback adapter is missing or unsafe'
  [[ $(sha256sum "$deployed_adapter_path" | cut -d' ' -f1) == "$deployed_adapter_sha" ]] || block 'deployed rollback adapter digest does not match its snapshot policy'
  deployed_credential_provider=$(awk '$0 ~ /^CREDENTIAL_PROVIDER=/ {sub(/^CREDENTIAL_PROVIDER=/, ""); print}' "$deployed_snapshot_policy")
  deployed_credential_ref=$(awk '$0 ~ /^CREDENTIAL_REF=/ {sub(/^CREDENTIAL_REF=/, ""); print}' "$deployed_snapshot_policy")
  credential_reference_safe "$deployed_credential_provider" "$deployed_credential_ref" 'deployed rollback policy'
  deployed_credential_scope=$(awk '$0 ~ /^CREDENTIAL_SCOPE=/ {sub(/^CREDENTIAL_SCOPE=/, ""); print}' "$deployed_snapshot_policy")
  deployed_environment=$(awk '$0 ~ /^ENVIRONMENT=/ {sub(/^ENVIRONMENT=/, ""); print}' "$deployed_snapshot_policy")
  [[ $deployed_credential_scope == "$deployed_environment" ]] || block 'deployed rollback credential scope does not match its environment'
  python3 - "$deployed_snapshot_state" "$deployed_snapshot_policy" "$etc_root/adapters" "$etc_root/credentials" <<'PY' >/dev/null 2>&1 || block 'deployed rollback snapshot state and policy do not cross-validate'
import json, re, sys
try:
    state = json.load(open(sys.argv[1], encoding='utf-8'))
except (OSError, ValueError):
    raise SystemExit(1)
policy = {}
allowed = {"SCHEMA_VERSION","CORE_REF","ENVIRONMENT","TARGET_ID","DEPLOYER_IDENTITY","ADAPTER_PATH","ADAPTER_SHA256","CREDENTIAL_PROVIDER","CREDENTIAL_REF","CREDENTIAL_SCOPE","APPROVAL_PROVIDER","APPROVAL_EVIDENCE_PATH","APPROVAL_CAPABILITY_EVIDENCE_PATH","PRODUCTION_AUTHORIZATION_EVIDENCE_PATH","CHECKPOINT_EVIDENCE_PATH","SOURCE_COMMIT","ARTIFACT_IMAGE","NETWORK_HOST","MIN_DISK_GIB","REQUIRE_COMPOSE"}
try:
    for line in open(sys.argv[2], encoding='utf-8'):
        line = line.rstrip('\n')
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or key in policy or key not in allowed:
            raise SystemExit(1)
        policy[key] = value
except OSError:
    raise SystemExit(1)
sha = re.compile(r'^[0-9a-f]{40}$')
pairs = (('core_ref','CORE_REF'), ('environment','ENVIRONMENT'), ('target','TARGET_ID'), ('source_commit','SOURCE_COMMIT'), ('artifact','ARTIFACT_IMAGE'), ('deployer_identity','DEPLOYER_IDENTITY'))
ok = (policy.get('SCHEMA_VERSION') == '1'
      and all(state.get(k) and state.get(k) == policy.get(p) for k, p in pairs)
      and bool(sha.match(state['core_ref'])) and bool(sha.match(state['source_commit']))
      and bool(re.search(r'@sha256:[0-9a-f]{64}$', state['artifact'])))
ok = ok and all(policy.get(k) for k in ('ADAPTER_PATH','ADAPTER_SHA256','CREDENTIAL_PROVIDER','CREDENTIAL_REF','CREDENTIAL_SCOPE'))
ok = ok and bool(re.fullmatch(r'[0-9a-f]{64}', policy.get('ADAPTER_SHA256','')))
ok = ok and policy.get('ADAPTER_PATH','').startswith(sys.argv[3] + '/')
ok = ok and policy.get('CREDENTIAL_PROVIDER') in ('file','external')
ok = ok and policy.get('CREDENTIAL_SCOPE') == policy.get('ENVIRONMENT')
if policy.get('CREDENTIAL_PROVIDER') == 'file':
    ok = ok and policy.get('CREDENTIAL_REF','').startswith(sys.argv[4] + '/')
else:
    ok = ok and bool(re.fullmatch(r'external:[a-z0-9][a-z0-9-]{0,31}:[A-Za-z0-9._/-]{1,128}', policy.get('CREDENTIAL_REF','')))
raise SystemExit(0 if ok else 1)
PY
}

publish_deployed_snapshot() {
  local policy=$1 state=$2 snapshot pointer retired incumbent
  secure_directory "$deployed_root" 700 1 || return
  if [[ -e "$deployed_current" || -L "$deployed_current" ]]; then
    # An unusable incumbent snapshot must not block publication of a freshly
    # validated rollback pair; only identical bytes short-circuit.
    if [[ -L "$deployed_current" ]]; then
      incumbent=$(readlink -f "$deployed_current")
      if [[ -f "$incumbent/policy.conf" && -f "$incumbent/state.json" ]] && cmp -s "$incumbent/policy.conf" "$policy" && cmp -s "$incumbent/state.json" "$state"; then return; fi
    fi
    retired=$(readlink "$deployed_current")
    [[ "$retired" =~ ^\.snapshot\.[A-Za-z0-9._-]+$ ]] || block 'current deployed snapshot pointer is unsafe'
    rm -f -- "$deployed_current" || return
  fi
  snapshot=$(mktemp -d "$deployed_root/.snapshot.XXXXXX") || return
  chmod 0700 "$snapshot" || return
  install -m 0600 "$policy" "$snapshot/policy.conf" || return
  install -m 0600 "$state" "$snapshot/state.json" || return
  pointer=$(mktemp -u "$deployed_root/.current.XXXXXX") || return
  ln -s "${snapshot##*/}" "$pointer" || return
  sync -f "$snapshot/policy.conf" "$snapshot/state.json" 2>/dev/null || block 'replacement deployed snapshot is not durable'
  sync -f "$snapshot" 2>/dev/null || block 'replacement deployed snapshot is not durable'
  mv -Tf "$pointer" "$deployed_current" || return
  sync -f "$deployed_root" 2>/dev/null || block 'deployed snapshot pointer is not durable'
  if [[ -n ${retired:-} && -d "$deployed_root/$retired" && ! -L "$deployed_root/$retired" ]]; then rm -rf -- "${deployed_root:?}/$retired"; fi
}

install_units() {
  local unit systemd_mode
  [[ -d "$systemd_root" && ! -L "$systemd_root" && $(stat -c %u "$systemd_root") == "$expected_uid" ]] || block 'systemd unit directory has an unsafe owner or type'
  systemd_mode=$(stat -c %a "$systemd_root")
  (((8#$systemd_mode & 8#022) == 0)) || block 'systemd unit directory is group- or world-writable'
  for unit in "${unit_names[@]}"; do
    [[ ! -e "$systemd_root/$unit.d" && ! -L "$systemd_root/$unit.d" ]] || block "managed unit $unit has an unreviewed drop-in override"
    if [[ -e "$systemd_root/$unit" || -L "$systemd_root/$unit" ]]; then
      [[ ! -L "$systemd_root/$unit" && -f "$systemd_root/$unit" && $(stat -c %u "$systemd_root/$unit") == "$expected_uid" ]] || block 'managed systemd unit has an unsafe owner or type'
    fi
    install -m 0644 "$unit_source/$unit" "$systemd_root/$unit"
  done
  systemd-analyze verify "${unit_names[@]/#/$systemd_root/}" >/dev/null || die 'systemd unit verification failed'
  systemctl daemon-reload
  systemctl enable --now "${timer_names[@]}" >/dev/null
}

adapter_deadline() {
  local operation_name=$1 seconds=120
  [[ "$operation_name" != rollback ]] || seconds=2700
  if [[ "$testing" == 1 && -n ${CI_FLEET_DEPLOYER_TEST_TIMEOUT_SECONDS:-} ]]; then
    [[ ${CI_FLEET_DEPLOYER_TEST_TIMEOUT_SECONDS} =~ ^[1-9][0-9]?$ ]] || die 'invalid test-only adapter timeout'
    seconds=$CI_FLEET_DEPLOYER_TEST_TIMEOUT_SECONDS
  fi
  printf '%s' "$seconds"
}

run_adapter() {
  local policy=$1 adapter_path=$2 operation_name=$3 marker=${4:-} seconds
  seconds=$(adapter_deadline "$operation_name")
  if [[ -n "$marker" ]]; then
    timeout --signal=TERM --kill-after=10s "${seconds}s" env CI_FLEET_DEPLOYER_CONFIG="$policy" CI_FLEET_DEPLOYER_ROLLBACK_COMMIT="$marker" "$adapter_path" "$operation_name"
  else
    timeout --signal=TERM --kill-after=10s "${seconds}s" env CI_FLEET_DEPLOYER_CONFIG="$policy" "$adapter_path" "$operation_name"
  fi
}

run_verified_adapter() {
  local policy=$1 path=$2 digest=$3 operation_name=$4 marker=${5:-}
  secure_file "$path" "$operation_name adapter" 700
  exec 7<"$path"
  [[ $(sha256sum /proc/$$/fd/7 | cut -d' ' -f1) == "$digest" ]] || die "$operation_name adapter digest mismatch"
  run_adapter "$policy" "/proc/$$/fd/7" "$operation_name" "$marker"
}

policy_adapter_operation() {
  local policy=$1 operation_name=$2 description=$3 marker=${4:-} snapshot=${5:-1} key
  local -A policy_cfg=()
  secure_file "$policy" "$description"
  reject_mixed_role
  if [[ "$snapshot" == 1 ]]; then
    snapshot=$(mktemp "$state_root/.policy-check.XXXXXX")
    policy_check_snapshot=$snapshot
    install -m 0600 "$policy" "$snapshot"
    policy=$snapshot
  fi
  parse_file "$policy" policy_cfg "$description" "$config_keys"
  [[ ${policy_cfg[SCHEMA_VERSION]:-} == 1 ]] || die "$description has an unsupported or missing schema version"
  for key in ADAPTER_PATH ADAPTER_SHA256 CREDENTIAL_PROVIDER CREDENTIAL_REF CREDENTIAL_SCOPE ENVIRONMENT; do [[ -v "policy_cfg[$key]" ]] || die "$description is missing $key"; done
  [[ ${policy_cfg[ADAPTER_SHA256]} =~ ^[0-9a-f]{64}$ ]] || die "$description has an invalid adapter digest"
  inside "${policy_cfg[ADAPTER_PATH]}" "$etc_root/adapters" || die "$description adapter path is outside the protected adapter directory"
  [[ ${policy_cfg[CREDENTIAL_SCOPE]} == "${policy_cfg[ENVIRONMENT]}" ]] || die "$description credential scope does not match its environment"
  credential_reference_safe "${policy_cfg[CREDENTIAL_PROVIDER]}" "${policy_cfg[CREDENTIAL_REF]}" "$description"
  local status
  run_verified_adapter "$policy" "${policy_cfg[ADAPTER_PATH]}" "${policy_cfg[ADAPTER_SHA256]}" "$operation_name" "$marker" >/dev/null
  status=$?
  if [[ "$snapshot" != 0 ]]; then rm -f "$snapshot"; policy_check_snapshot=; fi
  return "$status"
}

perform_check() {
  local transactions=()
  shopt -s nullglob
  transactions=("$state_root"/.transaction.*)
  shopt -u nullglob
  ((${#transactions[@]} == 0)) || block 'interrupted installer transaction requires recovery'
  if active_deployment; then block 'active deployment prevents a consistent check'; fi
  if [[ -e "$active_operation" || -L "$active_operation" ]]; then block 'stale operation marker requires a mutating recovery'; fi
  converged || block 'installed deployer state is absent or drifted'
  policy_adapter_operation "$active_policy" health 'active policy' '' 0 || block 'active deployer health check failed'
  health=healthy
  report NO_CHANGE no none "$(rollback_available)"
}

perform_converge() {
  local had_state=0 old_environment old_target candidate_changed=1 old_release
  [[ -f "$state_file" ]] && had_state=1
  if ((!had_state)) && { [[ -L "$current" ]] || compgen -G "$systemd_root/ci-fleet-deployer*" >/dev/null; }; then
    block 'installed deployer state is absent or drifted; restore install state before convergence'
  fi
  if ((had_state)); then
    read -r old_environment old_target < <(python3 - "$state_file" <<'PY'
import json,sys
v=json.load(open(sys.argv[1])); print(v.get('environment',''),v.get('target',''))
PY
)
    [[ "$old_environment" == "$environment" && "$old_target" == "$target" ]] || block 'installed environment and target identity cannot change in place'
    old_deployer_identity=$(python3 - "$state_file" <<'PY'
import json,sys
print(json.load(open(sys.argv[1])).get('deployer_identity',''))
PY
)
    [[ -z "$old_deployer_identity" || "$old_deployer_identity" == "${cfg[DEPLOYER_IDENTITY]}" ]] || block 'installed deployer ownership identity cannot change in place'
    [[ -e "$deployed_current" || -L "$deployed_current" ]] || block 'deployed rollback snapshot is missing; restore it before convergence'
    load_deployed_snapshot
    if [[ "$mode" == install && -L "$current" ]] && ! state_matches; then block 'install cannot select a new candidate; use --upgrade or --repair'; fi
  elif [[ "$mode" == upgrade ]]; then
    block '--upgrade requires an existing installation'
  fi
  if state_matches; then candidate_changed=0; fi
  if [[ "$mode" == upgrade && ! -L "$current" ]]; then block '--upgrade requires an active installation; use --install after uninstall'; fi
  if active_deployment; then block 'active deployment prevents this operation'; fi
  if converged; then
    policy_adapter_operation "$active_policy" health 'active policy' || block 'active deployer health check failed'
    health=healthy; report NO_CHANGE no none "$(rollback_available)"; return
  fi
  if [[ -L "$current" ]]; then
    old_release=$(readlink -f "$current")
    release_complete "$old_release" || block 'active deployer release is incomplete'
    if [[ "$mode" != repair ]]; then policy_adapter_operation "$active_policy" health 'active policy' || block 'active deployer is unhealthy; recover or roll back before replacement'; fi
  fi
  reject_mixed_role
  credential_reference_safe "${cfg[CREDENTIAL_PROVIDER]}" "${cfg[CREDENTIAL_REF]}" 'candidate policy'
  run_verified_adapter "$config" "${cfg[ADAPTER_PATH]}" "${cfg[ADAPTER_SHA256]}" validate >/dev/null || die 'candidate adapter validation failed'
  install_release
  secure_directory "$state_root" 700 1
  secure_directory "$log_root" 700 1
  begin_transaction
  if ((had_state && candidate_changed)); then
    if [[ -e "$deployed_current" || -L "$deployed_current" ]]; then
      load_deployed_snapshot
      install -m 0600 "$deployed_snapshot_state" "$previous_state"
      install -m 0600 "$deployed_snapshot_policy" "$previous_policy"
    fi
  fi
  ln -sfn "releases/$core_ref" "$install_root/.current.new"
  # Publish the candidate activation pointer before unit verification so the
  # units' /opt/ci-fleet-deployer/current/... ExecStart paths resolve on a
  # fresh install; transaction recovery restores the prior pointer.
  mv -Tf "$install_root/.current.new" "$current"
  install_units
  install -m 0600 "$config" "$active_policy.new"
  write_state "$state_root/.install-state.new"
  reject_mixed_role
  mv -Tf "$active_policy.new" "$active_policy"
  mv -Tf "$state_root/.install-state.new" "$state_file"
  policy_adapter_operation "$active_policy" health 'candidate policy' || die 'candidate health check failed after activation'
  if [[ ! -e "$deployed_current" && ! -L "$deployed_current" ]]; then
    publish_deployed_snapshot "$active_policy" "$state_file"
    if [[ -n ${transaction_dir:-} && -d $transaction_dir ]]; then readlink "$deployed_current" >"$transaction_dir/deployed-created"; fi
  fi
  commit_transaction
  health=healthy
  report CHANGED yes run-check "$(rollback_available)"
}

perform_rollback() {
  local source_commit deployer_identity key
  local -A rollback_policy=()
  if ((recovered_rollback)); then health=healthy; report CHANGED yes restore-host-policy-evidence-then-check no; return; fi
  active_deployment && block 'active deployment prevents rollback'
  [[ -f "$previous_state" && -f "$previous_policy" ]] || block 'no last-known-good release is available'
  secure_directory "$state_root" 700 0
  secure_file "$previous_policy" 'last-known-good policy'
  parse_file "$previous_policy" rollback_policy 'last-known-good policy' "$config_keys"
  for key in CORE_REF ENVIRONMENT TARGET_ID DEPLOYER_IDENTITY SOURCE_COMMIT ARTIFACT_IMAGE; do [[ -v "rollback_policy[$key]" ]] || block "last-known-good policy is missing $key"; done
  [[ ${rollback_policy[SCHEMA_VERSION]:-} == 1 ]] || block 'last-known-good policy has an unsupported or missing schema version'
  read -r core_ref environment target source_commit artifact deployer_identity < <(python3 - "$previous_state" <<'PY'
import json, sys
try: value=json.load(open(sys.argv[1], encoding='utf-8'))
except (OSError, ValueError): raise SystemExit(1)
print(*(value.get(k, '') for k in ('core_ref','environment','target','source_commit','artifact','deployer_identity')))
PY
  ) || block 'last-known-good state is malformed'
  [[ "$core_ref" =~ ^[0-9a-f]{40}$ && "$source_commit" =~ ^[0-9a-f]{40}$ && "$artifact" =~ @sha256:[0-9a-f]{64}$ ]] || block 'last-known-good state has unsafe immutable identifiers'
  [[ "$core_ref" == "${rollback_policy[CORE_REF]}" && "$environment" == "${rollback_policy[ENVIRONMENT]}" && "$target" == "${rollback_policy[TARGET_ID]}" && "$source_commit" == "${rollback_policy[SOURCE_COMMIT]}" && "$artifact" == "${rollback_policy[ARTIFACT_IMAGE]}" && "$deployer_identity" == "${rollback_policy[DEPLOYER_IDENTITY]}" ]] || block 'last-known-good state and policy do not match'
  [[ "$environment" =~ ^[a-z][a-z0-9-]{0,31}$ && "$target" =~ ^[a-z0-9][a-z0-9._-]{0,63}$ ]] || block 'last-known-good identity is malformed'
  cfg[DEPLOYER_IDENTITY]=${rollback_policy[DEPLOYER_IDENTITY]}
  command -v docker >/dev/null || block 'docker is required for rollback isolation validation'
  reject_mixed_role
  begin_transaction
  install -m 0600 "$previous_state" "$state_file.new"
  install -m 0600 "$previous_policy" "$active_policy.new"

  release_complete "$releases/$core_ref" || die 'last-known-good release is incomplete'
  unit_source=$releases/$core_ref/deploy/deployer
  ln -sfn "releases/$core_ref" "$install_root/.current.new"
  # Publish before unit verification so the units' current/... paths resolve;
  # transaction recovery restores the prior pointer.
  mv -Tf "$install_root/.current.new" "$current"
  install_units
  mv -Tf "$active_policy.new" "$active_policy"
  mv -Tf "$state_file.new" "$state_file"
  reject_mixed_role
  # The commit marker is authoritative: if the adapter created it but the
  # wrapper did not observe a zero exit, recovery finalizes the committed
  # rollback, so the report must not claim failure with no change.
  if ! policy_adapter_operation "$active_policy" rollback 'last-known-good policy' "$transaction_dir/application-rollback-committed" && [[ ! -f "$transaction_dir/application-rollback-committed" || -L "$transaction_dir/application-rollback-committed" ]]; then
    die 'application adapter rollback failed'
  fi
  sync -f "$transaction_dir/application-rollback-committed" 2>/dev/null || sync "$transaction_dir/application-rollback-committed" 2>/dev/null || die 'application rollback commit marker is not durable'
  sync -f "$transaction_dir" 2>/dev/null || sync "$transaction_dir" 2>/dev/null || true
  # The rollback already replaced the activation pointer and units on possibly
  # separate filesystems; persist all committed boundaries before finalization
  # consumes the journal and last-known-good pair.
  sync -f "$state_file" "$active_policy" 2>/dev/null || die 'rolled-back host state is not durable'
  sync -f "$state_root" 2>/dev/null || die 'rolled-back host state is not durable'
  sync -f "$install_root" 2>/dev/null || die 'rolled-back install root is not durable'
  sync -f "$systemd_root" 2>/dev/null || die 'rolled-back systemd boundary is not durable'
  finalize_committed_rollback
  recovered_rollback=0
  health=healthy
  report CHANGED yes restore-host-policy-evidence-then-check no
}

perform_drain() {
  if active_deployment; then block 'active deployment prevents drain'; fi
  secure_directory "$state_root" 700 1
  if [[ -e "$drained" || -L "$drained" ]]; then
    secure_file "$drained" 'drain marker'
    report NO_CHANGE no safe-to-maintain "$(rollback_available)"; return
  fi
  temporary=$(mktemp "$state_root/.drained.XXXXXX")
  chmod 0600 "$temporary"
  mv -Tf "$temporary" "$drained"
  sync -f "$drained" 2>/dev/null || sync "$drained" 2>/dev/null || true
  sync -f "$state_root" 2>/dev/null || true
  report CHANGED yes safe-to-maintain "$(rollback_available)"
}

perform_resume() {
  secure_directory "$state_root" 700 0 || block 'deployer state directory is missing'
  if active_deployment; then block 'active deployment prevents resume'; fi
  local was_drained=0
  if [[ -e "$drained" || -L "$drained" ]]; then secure_file "$drained" 'drain marker'; was_drained=1; fi
  converged || block 'installed deployer state is absent or drifted; repair before resume'
  policy_adapter_operation "$active_policy" health 'active policy' || block 'active deployer health check failed; repair before resume'
  health=healthy
  ((was_drained == 1)) || { report NO_CHANGE no ready-to-deploy "$(rollback_available)"; return; }
  rm -f "$drained"
  report CHANGED yes ready-to-deploy "$(rollback_available)"
}

perform_uninstall() {
  local changed=no unit managed_present=no
  if [[ -e "$state_root" || -L "$state_root" || -e "$lock_root" || -L "$lock_root" || -e "$current" || -L "$current" ]]; then managed_present=yes; fi
  for unit in "${unit_names[@]}"; do [[ ! -e "$systemd_root/$unit" && ! -L "$systemd_root/$unit" ]] || managed_present=yes; done
  if [[ "$managed_present" == no ]]; then report NO_CHANGE no retained-state "$(rollback_available)"; return; fi
  if active_deployment; then block 'active deployment prevents this operation'; fi
  acquire_lock
  secure_directory "$state_root" 700 1
  for unit in "${unit_names[@]}"; do [[ ! -e "$systemd_root/$unit" || -L "$systemd_root/$unit" || -f "$systemd_root/$unit" ]] || block "managed unit $unit has an unsafe type"; done
  if [[ -e "$drained" || -L "$drained" ]]; then
    secure_file "$drained" 'drain marker'
  else
    temporary=$(mktemp "$state_root/.drained.XXXXXX")
    chmod 0600 "$temporary"
    mv -Tf "$temporary" "$drained"
  fi
  if active_deployment; then block 'active deployment started while draining'; fi
  for unit in "${timer_names[@]}"; do
    if [[ -e "$systemd_root/$unit" || -L "$systemd_root/$unit" ]]; then
      systemctl disable --now "$unit" >/dev/null 2>&1 || block 'deployer timers did not stop during uninstall'
    fi
  done
  if [[ -L "$current" ]]; then
    [[ -d "$install_root" && ! -L "$install_root" && $(stat -c '%u:%a' "$install_root") == "$expected_uid:755" ]] || block 'managed install boundary has an unsafe owner, mode, or type'
    rm -f "$current"; changed=yes
  fi
  [[ ! -e "$current" ]] || block 'activation pointer has an unsafe type'
  for unit in "${unit_names[@]}"; do if [[ -e "$systemd_root/$unit" || -L "$systemd_root/$unit" ]]; then rm -f "$systemd_root/$unit"; changed=yes; fi; done
  systemctl daemon-reload >/dev/null 2>&1 || block 'systemd manager reload failed after unit removal'
  # Persist pointer, unit, and timer removals across their filesystems before
  # the drain marker guarding this maintenance window is cleared.
  sync -f "$install_root" 2>/dev/null || block 'uninstalled install root is not durable'
  sync -f "$systemd_root" 2>/dev/null || block 'uninstalled systemd boundary is not durable'
  sync -f "$state_root" 2>/dev/null || block 'uninstalled host state is not durable'
  rm -f "$drained" "$active_operation"
  sync -f "$state_root" 2>/dev/null || block 'cleared drain marker is not durable'
  if [[ "$changed" == yes ]]; then report CHANGED yes retained-state "$(rollback_available)"; else report NO_CHANGE no retained-state "$(rollback_available)"; fi
}

validate_config
if [[ "$mode" == drain || "$mode" == uninstall || "$mode" == rollback ]]; then
  require_maintenance_host
else
  validate_checkout
  require_host
fi
case "$mode" in
  check) acquire_check_lock; perform_check ;;
  install|upgrade|repair) acquire_lock
    if ((recovered_rollback)); then health=healthy; report CHANGED yes restore-host-policy-evidence-then-check no; else perform_converge; fi ;;
  rollback) acquire_lock; perform_rollback ;;
  drain) acquire_lock; perform_drain ;;
  resume) acquire_lock; perform_resume ;;
  uninstall) perform_uninstall ;;
esac
