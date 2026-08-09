#!/usr/bin/env bash
set -Eeuo pipefail
set +x

operation=${1:-}
case "$operation" in health|cleanup|deploy|rollback|drain) ;; *) printf 'ERROR: usage: deployer-runtime.sh health|cleanup|deploy|rollback|drain\n' >&2; exit 2 ;; esac

root=${CI_FLEET_DEPLOYER_ROOT:-}
testing=${CI_FLEET_DEPLOYER_TESTING:-0}
[[ -z "$root" || "$testing" == 1 ]] || { printf 'ERROR: alternate root is test-only\n' >&2; exit 2; }
if [[ "$testing" == 1 && -z "$root" ]]; then printf 'ERROR: test mode requires an alternate root\n' >&2; exit 2; fi
root_path() { printf '%s%s' "$root" "$1"; }
config=${CI_FLEET_DEPLOYER_CONFIG:-$(root_path /var/lib/ci-fleet-deployer/active-policy.conf)}
request=${CI_FLEET_DEPLOYER_REQUEST:-$(root_path /var/lib/ci-fleet-deployer/request.conf)}
state_root=$(root_path /var/lib/ci-fleet-deployer)
log_root=$(root_path /var/log/ci-fleet-deployer)
lock_dir=$(root_path /var/lock/ci-fleet-deployer)
evidence_dir=$(root_path /etc/ci-fleet-deployer/evidence)
active=$state_root/active-operation
drained=$state_root/drained
last_request=$state_root/last-request.conf
audit_log=$log_root/audit.log

die() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }
expected_uid=0
[[ "$testing" != 1 ]] || expected_uid=$(id -u)
secure_file() {
  local path=$1 description=$2 mode=${3:-600}
  [[ ! -L "$path" && -f "$path" ]] || die "$description must be a regular file, not a symlink"
  [[ $(realpath -e -- "$path") == $(realpath -m -- "$path") ]] || die "$description path contains a symlink"
  [[ $(stat -c '%u:%a' "$path") == "$expected_uid:$mode" ]] || die "$description has unsafe owner or mode"
}
secure_directory() {
  local path=$1 description=$2
  [[ ! -L "$path" && -d "$path" && $(stat -c '%u:%a' "$path") == "$expected_uid:700" ]] || die "$description has unsafe owner, mode, or type"
}
inside() {
  local path=$1 base=$2
  [[ $(realpath -m -- "$path") == "$(realpath -m -- "$base")/"* ]]
}
parse_file() {
  local path=$1 prefix=$2 kind=$3 allowed=$4 line key value
  declare -gA "$prefix=()"
  local -n output=$prefix
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -n "$line" && ${line:0:1} != '#' ]] || continue
    [[ "$line" == *=* ]] || die "malformed $kind line"
    key=${line%%=*}; value=${line#*=}
    [[ "$key" =~ ^[A-Z][A-Z0-9_]*$ && -n "$value" && "$value" != *$'\r'* ]] || die "malformed $kind line"
    [[ " $allowed " == *" $key "* ]] || die "unknown $kind key"
    [[ ! -v "output[$key]" ]] || die "duplicate $kind key"
    # key indexes a nameref to an associative array.
    # shellcheck disable=SC2004
    output[$key]=$value
  done <"$path"
}

secure_file "$config" 'deployer configuration'
config_keys='SCHEMA_VERSION CORE_REF ENVIRONMENT TARGET_ID DEPLOYER_IDENTITY ADAPTER_PATH ADAPTER_SHA256 CREDENTIAL_PROVIDER CREDENTIAL_REF CREDENTIAL_SCOPE APPROVAL_PROVIDER APPROVAL_EVIDENCE_PATH APPROVAL_CAPABILITY_EVIDENCE_PATH PRODUCTION_AUTHORIZATION_EVIDENCE_PATH CHECKPOINT_EVIDENCE_PATH SOURCE_COMMIT ARTIFACT_IMAGE NETWORK_HOST MIN_DISK_GIB REQUIRE_COMPOSE'
parse_file "$config" cfg configuration "$config_keys"
for key in ENVIRONMENT TARGET_ID ADAPTER_PATH ADAPTER_SHA256 SOURCE_COMMIT ARTIFACT_IMAGE; do [[ -v "cfg[$key]" ]] || die "configuration is missing $key"; done
[[ ${cfg[ENVIRONMENT]} =~ ^[a-z][a-z0-9-]{0,31}$ && ${cfg[TARGET_ID]} =~ ^[a-z0-9][a-z0-9._-]{0,63}$ ]] || die 'invalid environment or target identity'
[[ ${cfg[ADAPTER_SHA256]} =~ ^[0-9a-f]{64}$ ]] || die 'invalid adapter digest'
secure_file "${cfg[ADAPTER_PATH]}" 'application adapter' 700
[[ $(sha256sum "${cfg[ADAPTER_PATH]}" | cut -d' ' -f1) == "${cfg[ADAPTER_SHA256]}" ]] || die 'application adapter digest mismatch'

secure_directory "$state_root" 'deployer state directory'
secure_directory "$log_root" 'deployer log directory'
secure_directory "$lock_dir" 'deployer lock directory'
exec 9<"$lock_dir"
flock -n 9 || die 'another deployer operation is running'

case "$operation" in
  drain)
    [[ ! -e "$active" ]] || die 'active deployment prevents drain'
    temporary=$(mktemp "$state_root/.drained.XXXXXX")
    chmod 0600 "$temporary"
    mv -Tf "$temporary" "$drained"
    ;;
  health|rollback)
    "${cfg[ADAPTER_PATH]}" "$operation"
    ;;
  cleanup)
    [[ ! -e "$drained" ]] || die 'deployer is drained'
    "${cfg[ADAPTER_PATH]}" cleanup
    ;;
  deploy)
    [[ ! -e "$drained" ]] || die 'deployer is drained'
    secure_file "$request" 'deployment request'
    if [[ -e "$last_request" || -L "$last_request" ]]; then
      secure_file "$last_request" 'last completed deployment request'
      cmp -s "$request" "$last_request" && die 'deployment request was already completed'
    fi
    request_keys='SCHEMA_VERSION ENVIRONMENT TARGET_ID SOURCE_COMMIT ARTIFACT_IMAGE APPROVAL_IDENTITY POLICY_IDENTITY APPROVAL_ID APPROVED_AT'
    parse_file "$request" req request "$request_keys"
    for key in SCHEMA_VERSION ENVIRONMENT TARGET_ID SOURCE_COMMIT ARTIFACT_IMAGE APPROVAL_IDENTITY POLICY_IDENTITY APPROVAL_ID APPROVED_AT; do
      [[ -v "req[$key]" ]] || die "deployment request is missing $key"
    done
    [[ ${req[SCHEMA_VERSION]} == 1 ]] || die 'unsupported deployment request schema'
    for key in ENVIRONMENT TARGET_ID SOURCE_COMMIT ARTIFACT_IMAGE; do
      [[ ${req[$key]} == "${cfg[$key]}" ]] || die "deployment request $key does not match installed policy"
    done
    [[ ${req[SOURCE_COMMIT]} =~ ^[0-9a-f]{40}$ && ${req[ARTIFACT_IMAGE]} =~ ^[a-z0-9][a-z0-9.-]*(:[0-9]{1,5})?/[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$ ]] || die 'deployment request is not immutable and qualified'
    for key in APPROVAL_IDENTITY POLICY_IDENTITY APPROVAL_ID; do [[ ${req[$key]} =~ ^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$ ]] || die "deployment request has an unsafe $key"; done
    [[ ${req[APPROVED_AT]} =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] || die 'deployment request has an invalid approval time'
    [[ -v 'cfg[APPROVAL_EVIDENCE_PATH]' ]] || die 'installed policy is missing approval evidence'
    inside "${cfg[APPROVAL_EVIDENCE_PATH]}" "$evidence_dir" || die 'approval evidence is outside the protected evidence directory'
    secure_file "${cfg[APPROVAL_EVIDENCE_PATH]}" 'approval evidence'
    approval_keys='SCHEMA_VERSION ENVIRONMENT TARGET_ID SOURCE_COMMIT ARTIFACT_IMAGE APPROVAL_IDENTITY POLICY_IDENTITY APPROVAL_ID APPROVED_AT'
    parse_file "${cfg[APPROVAL_EVIDENCE_PATH]}" approved 'approval evidence' "$approval_keys"
    for key in $approval_keys; do
      [[ -v "approved[$key]" && ${req[$key]} == "${approved[$key]}" ]] || die "deployment request does not match protected approval $key"
    done
    if [[ ${cfg[ENVIRONMENT]} == production ]]; then
      [[ -v 'cfg[PRODUCTION_AUTHORIZATION_EVIDENCE_PATH]' ]] || die 'production policy is missing separate authorization evidence'
      inside "${cfg[PRODUCTION_AUTHORIZATION_EVIDENCE_PATH]}" "$evidence_dir" || die 'production authorization evidence is outside the protected evidence directory'
      secure_file "${cfg[PRODUCTION_AUTHORIZATION_EVIDENCE_PATH]}" 'production authorization evidence'
      production_keys='SCHEMA_VERSION ENVIRONMENT TARGET_ID SOURCE_COMMIT ARTIFACT_IMAGE AUTHORIZED_BY GATE_ID AUTHORIZED_AT'
      parse_file "${cfg[PRODUCTION_AUTHORIZATION_EVIDENCE_PATH]}" production 'production authorization evidence' "$production_keys"
      for key in $production_keys; do [[ -v "production[$key]" ]] || die "production authorization evidence is missing $key"; done
      [[ ${production[SCHEMA_VERSION]} == 1 && ${production[ENVIRONMENT]} == production ]] || die 'production authorization evidence has the wrong scope'
      [[ ${production[AUTHORIZED_BY]} =~ ^[A-Za-z0-9._:@/-]{1,128}$ && ${production[GATE_ID]} =~ ^[A-Za-z0-9._:@/-]{1,128}$ && ${production[AUTHORIZED_AT]} =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] || die 'production authorization evidence is malformed'
      for key in ENVIRONMENT TARGET_ID SOURCE_COMMIT ARTIFACT_IMAGE; do
        [[ -v "production[$key]" && ${req[$key]} == "${production[$key]}" ]] || die "deployment request does not match production authorization $key"
      done
    fi
    if [[ -e "$audit_log" || -L "$audit_log" ]]; then secure_file "$audit_log" 'deployer audit log'; else install -m 0600 /dev/null "$audit_log"; fi
    : >>"$audit_log"
    umask 077
    temporary=$(mktemp "$state_root/.active.XXXXXX")
    printf 'pid=%s\nstarted_at=%s\n' "$$" "$(date +%s)" >"$temporary"
    mv -Tf "$temporary" "$active"
    trap 'rm -f "$active"' EXIT INT TERM
    systemd-inhibit --what=shutdown:sleep --mode=block --who=ci-fleet-deployer \
      --why='approved deployment is active' -- "${cfg[ADAPTER_PATH]}" deploy
    mv -Tf "$request" "$last_request"
    printf 'time=%s environment=%s target=%s source=%s artifact=%s approval=%s policy=%s result=success\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${req[ENVIRONMENT]}" "${req[TARGET_ID]}" \
      "${req[SOURCE_COMMIT]}" "${req[ARTIFACT_IMAGE]#*@}" "${req[APPROVAL_ID]}" "${req[POLICY_IDENTITY]}" \
      >>"$audit_log"
    ;;
esac
