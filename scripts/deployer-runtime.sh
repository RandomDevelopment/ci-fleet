#!/usr/bin/env bash
set -Eeuo pipefail
set +x

operation=${1:-}
case "$operation" in health|cleanup|deploy|drain) ;; *) printf 'ERROR: usage: deployer-runtime.sh health|cleanup|deploy|drain\n' >&2; exit 2 ;; esac

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
deployer_etc=$(root_path /etc/ci-fleet-deployer)
credential_dir=$deployer_etc/credentials
active=$state_root/active-operation
drained=$state_root/drained
last_request=$state_root/last-request.conf
consumed_root=$state_root/consumed-requests
install_state=$state_root/install-state.json
deployed_root=$state_root/deployed
deployed_current=$deployed_root/current
audit_log=$log_root/audit.log
systemd_root=$(root_path /etc/systemd/system)

die() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }
deploy_exit() {
  local status=$?
  local recorded_status=${adapter_status:-$status}
  if [[ ${audit_pending:-0} == 1 ]]; then
    printf 'time=%s environment=%s target=%s source=%s artifact=%s approval=%s policy=%s result=failed phase=%s status=%s\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${req[ENVIRONMENT]}" "${req[TARGET_ID]}" \
      "${req[SOURCE_COMMIT]}" "${req[ARTIFACT_IMAGE]#*@}" "${req[APPROVAL_ID]}" "${req[POLICY_IDENTITY]}" \
      "${audit_phase:-post-consumption}" "$recorded_status" >&8 || true
  fi
  rm -f "$active" "${request_snapshot:-}" || true
  return "$status"
}
expected_uid=0
[[ "$testing" != 1 ]] || expected_uid=$(id -u)
secure_file() {
  local path=$1 description=$2 mode=${3:-600}
  [[ ! -L "$path" && -f "$path" ]] || die "$description must be a regular file, not a symlink"
  [[ "$path" == "$(realpath -m -- "$path")" ]] || die "$description path contains a symlink or non-canonical component"
  [[ $(realpath -e -- "$path") == $(realpath -m -- "$path") ]] || die "$description path contains a symlink"
  [[ $(stat -c '%u:%a' "$path") == "$expected_uid:$mode" ]] || die "$description has unsafe owner or mode"
}
secure_directory() {
  local path=$1 description=$2
  [[ ! -L "$path" && -d "$path" && $(stat -c '%u:%a' "$path") == "$expected_uid:700" ]] || die "$description has unsafe owner, mode, or type"
}
reject_mixed_role() {
  local unit runner_unit line output expected="deployer|${cfg[DEPLOYER_IDENTITY]}"
  for unit in ci-fleet-health.service ci-fleet-reconcile.service ci-fleet-cleanup.service actions.runner.service; do
    [[ ! -e "$systemd_root/$unit" ]] || die 'ordinary CI controller or runner state is present'
  done
  shopt -s nullglob
  for runner_unit in "$systemd_root"/actions.runner.*.service "$systemd_root"/multi-user.target.wants/actions.runner.*.service; do
    shopt -u nullglob
    [[ -n "$runner_unit" ]] && die 'ordinary GitHub Actions runner service is present'
  done
  shopt -u nullglob
  for path in "$(root_path /etc/ci-fleet/ci-fleet.env)" "$(root_path /opt/ci-fleet/current)" "$(root_path /var/lib/ci-fleet/install-state.json)"; do
    [[ ! -e "$path" && ! -L "$path" ]] || die 'ordinary CI controller or runner state is present'
  done
  output=$(docker ps -a --format '{{.ID}}|{{.Label "io.randomdevelopment.ci-fleet.role"}}|{{.Label "io.randomdevelopment.ci-fleet.identity"}}') || die 'Docker workload inventory failed'
  while IFS= read -r line; do [[ -z "$line" || ${line#*|} == "$expected" ]] || die 'unrelated Docker workload is present'; done <<<"$output"
  output=$(docker network ls --filter type=custom --format '{{.ID}}|{{.Label "io.randomdevelopment.ci-fleet.role"}}|{{.Label "io.randomdevelopment.ci-fleet.identity"}}') || die 'Docker network inventory failed'
  while IFS= read -r line; do [[ -z "$line" || ${line#*|} == "$expected" ]] || die 'incompatible custom Docker network is present'; done <<<"$output"
  output=$(docker volume ls --format '{{.Name}}|{{.Label "io.randomdevelopment.ci-fleet.role"}}|{{.Label "io.randomdevelopment.ci-fleet.identity"}}') || die 'Docker volume inventory failed'
  while IFS= read -r line; do [[ -z "$line" || ${line#*|} == "$expected" ]] || die 'incompatible Docker volume is present'; done <<<"$output"
}
inside() {
  local path=$1 base=$2
  [[ $(realpath -m -- "$path") == "$(realpath -m -- "$base")/"* ]]
}
valid_utc() {
  local value=$1
  [[ "$value" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] && [[ $(date -u -d "$value" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null) == "$value" ]]
}
not_drained() {
  if [[ -e "$drained" || -L "$drained" ]]; then
    secure_file "$drained" 'drain marker'
    die 'deployer is drained'
  fi
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

validate_credential() {
  secure_directory "$deployer_etc" 'deployer configuration directory'
  secure_directory "$credential_dir" 'credential directory'
  [[ ${cfg[CREDENTIAL_SCOPE]} == "${cfg[ENVIRONMENT]}" ]] || die 'credential scope does not match the deployment environment'
  case ${cfg[CREDENTIAL_PROVIDER]} in
    file)
      inside "${cfg[CREDENTIAL_REF]}" "$credential_dir" || die 'credential reference is outside the protected credential directory'
      secure_file "${cfg[CREDENTIAL_REF]}" 'credential file'
      ;;
    external) [[ ${cfg[CREDENTIAL_REF]} =~ ^external:[a-z0-9][a-z0-9-]{0,31}:[A-Za-z0-9._/-]{1,128}$ ]] || die 'external credential reference is malformed' ;;
    *) die 'unsupported credential provider' ;;
  esac
}

secure_directory "$state_root" 'deployer state directory'
secure_directory "$lock_dir" 'deployer lock directory'
exec 9<"$lock_dir"
flock -n 9 || die 'another deployer operation is running'
shopt -s nullglob
transactions=("$state_root"/.transaction.*)
shopt -u nullglob
((${#transactions[@]} == 0)) || die 'interrupted installer transaction requires recovery'

if [[ "$operation" == drain ]]; then
  [[ ! -e "$active" && ! -L "$active" ]] || die 'active deployment prevents drain'
  temporary=$(mktemp "$state_root/.drained.XXXXXX")
  chmod 0600 "$temporary"
  mv -Tf "$temporary" "$drained"
  exit 0
fi

secure_file "$config" 'deployer configuration'
config_keys='SCHEMA_VERSION CORE_REF ENVIRONMENT TARGET_ID DEPLOYER_IDENTITY ADAPTER_PATH ADAPTER_SHA256 CREDENTIAL_PROVIDER CREDENTIAL_REF CREDENTIAL_SCOPE APPROVAL_PROVIDER APPROVAL_EVIDENCE_PATH APPROVAL_CAPABILITY_EVIDENCE_PATH PRODUCTION_AUTHORIZATION_EVIDENCE_PATH CHECKPOINT_EVIDENCE_PATH SOURCE_COMMIT ARTIFACT_IMAGE NETWORK_HOST MIN_DISK_GIB REQUIRE_COMPOSE'
parse_file "$config" cfg configuration "$config_keys"
for key in ENVIRONMENT TARGET_ID DEPLOYER_IDENTITY ADAPTER_PATH ADAPTER_SHA256 CREDENTIAL_PROVIDER CREDENTIAL_REF CREDENTIAL_SCOPE APPROVAL_PROVIDER CHECKPOINT_EVIDENCE_PATH SOURCE_COMMIT ARTIFACT_IMAGE; do [[ -v "cfg[$key]" ]] || die "configuration is missing $key"; done
[[ ${cfg[SCHEMA_VERSION]:-} == 1 ]] || die 'configuration has an unsupported or missing schema version'
[[ ${cfg[ENVIRONMENT]} =~ ^[a-z][a-z0-9-]{0,31}$ && ${cfg[TARGET_ID]} =~ ^[a-z0-9][a-z0-9._-]{0,63}$ ]] || die 'invalid environment or target identity'
[[ ${cfg[ADAPTER_SHA256]} =~ ^[0-9a-f]{64}$ ]] || die 'invalid adapter digest'
secure_file "${cfg[ADAPTER_PATH]}" 'application adapter' 700
exec 7<"${cfg[ADAPTER_PATH]}"
[[ $(sha256sum /proc/$$/fd/7 | cut -d' ' -f1) == "${cfg[ADAPTER_SHA256]}" ]] || die 'application adapter digest mismatch'
adapter_path=/proc/$$/fd/7
validate_credential

secure_directory "$log_root" 'deployer log directory'

case "$operation" in
  health)
    reject_mixed_role
    "$adapter_path" "$operation"
    ;;
  cleanup)
    not_drained
    reject_mixed_role
    "$adapter_path" cleanup
    ;;
  deploy)
    not_drained
    [[ ! -e "$active" && ! -L "$active" ]] || die 'active operation marker requires recovery'
    reject_mixed_role
    secure_file "$request" 'deployment request'
    request_snapshot=$(mktemp "$state_root/.request.XXXXXX")
    install -m 0600 "$request" "$request_snapshot"
    trap 'rm -f "${request_snapshot:-}"' EXIT INT TERM
    request_keys='SCHEMA_VERSION ENVIRONMENT TARGET_ID SOURCE_COMMIT ARTIFACT_IMAGE APPROVAL_IDENTITY POLICY_IDENTITY APPROVAL_ID APPROVED_AT'
    parse_file "$request_snapshot" req request "$request_keys"
    for key in SCHEMA_VERSION ENVIRONMENT TARGET_ID SOURCE_COMMIT ARTIFACT_IMAGE APPROVAL_IDENTITY POLICY_IDENTITY APPROVAL_ID APPROVED_AT; do
      [[ -v "req[$key]" ]] || die "deployment request is missing $key"
    done
    [[ ${req[SCHEMA_VERSION]} == 1 ]] || die 'unsupported deployment request schema'
    for key in ENVIRONMENT TARGET_ID SOURCE_COMMIT ARTIFACT_IMAGE; do
      [[ ${req[$key]} == "${cfg[$key]}" ]] || die "deployment request $key does not match installed policy"
    done
    [[ ${req[SOURCE_COMMIT]} =~ ^[0-9a-f]{40}$ && ${req[ARTIFACT_IMAGE]} =~ ^[a-z0-9][a-z0-9.-]*(:[0-9]{1,5})?/[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$ ]] || die 'deployment request is not immutable and qualified'
    for key in APPROVAL_IDENTITY POLICY_IDENTITY APPROVAL_ID; do [[ ${req[$key]} =~ ^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$ ]] || die "deployment request has an unsafe $key"; done
    valid_utc "${req[APPROVED_AT]}" || die 'deployment request has an invalid approval time'
    if [[ -e "$last_request" || -L "$last_request" ]]; then
      secure_file "$last_request" 'last completed deployment request'
      parse_file "$last_request" completed 'last completed deployment request' "$request_keys"
      replay=1
      for key in $request_keys; do
        [[ -v "completed[$key]" && ${req[$key]} == "${completed[$key]}" ]] || replay=0
      done
      ((replay == 0)) || die 'deployment request was already completed'
    fi
    inside "${cfg[CHECKPOINT_EVIDENCE_PATH]}" "$evidence_dir" || die 'checkpoint evidence is outside the protected evidence directory'
    secure_file "${cfg[CHECKPOINT_EVIDENCE_PATH]}" 'checkpoint evidence'
    checkpoint_keys='SCHEMA_VERSION ENVIRONMENT TARGET_ID CHECKPOINT_ID RECORDED_AT'
    parse_file "${cfg[CHECKPOINT_EVIDENCE_PATH]}" checkpoint 'checkpoint evidence' "$checkpoint_keys"
    for key in $checkpoint_keys; do [[ -v "checkpoint[$key]" ]] || die "checkpoint evidence is missing $key"; done
    [[ ${checkpoint[SCHEMA_VERSION]} == 1 && ${checkpoint[ENVIRONMENT]} == "${req[ENVIRONMENT]}" && ${checkpoint[TARGET_ID]} == "${req[TARGET_ID]}" ]] || die 'checkpoint evidence does not match the deployment target'
    if [[ ! ${checkpoint[CHECKPOINT_ID]} =~ ^[A-Za-z0-9._:@/-]{1,128}$ ]] || ! valid_utc "${checkpoint[RECORDED_AT]}"; then die 'checkpoint evidence is malformed'; fi
    [[ -v 'cfg[APPROVAL_EVIDENCE_PATH]' ]] || die 'installed policy is missing approval evidence'
    inside "${cfg[APPROVAL_EVIDENCE_PATH]}" "$evidence_dir" || die 'approval evidence is outside the protected evidence directory'
    secure_file "${cfg[APPROVAL_EVIDENCE_PATH]}" 'approval evidence'
    approval_keys='SCHEMA_VERSION ENVIRONMENT TARGET_ID SOURCE_COMMIT ARTIFACT_IMAGE APPROVAL_IDENTITY POLICY_IDENTITY APPROVAL_ID APPROVED_AT'
    parse_file "${cfg[APPROVAL_EVIDENCE_PATH]}" approved 'approval evidence' "$approval_keys"
    for key in $approval_keys; do
      [[ -v "approved[$key]" && ${req[$key]} == "${approved[$key]}" ]] || die "deployment request does not match protected approval $key"
    done
    case ${cfg[APPROVAL_PROVIDER]} in
      manual-exact-head|external-exact-head) ;;
      github-environment)
        [[ -v 'cfg[APPROVAL_CAPABILITY_EVIDENCE_PATH]' ]] || die 'GitHub Environment approval is missing capability evidence'
        inside "${cfg[APPROVAL_CAPABILITY_EVIDENCE_PATH]}" "$evidence_dir" || die 'capability evidence is outside the protected evidence directory'
        secure_file "${cfg[APPROVAL_CAPABILITY_EVIDENCE_PATH]}" 'GitHub capability evidence'
        capability_keys='SCHEMA_VERSION ENVIRONMENT_PROTECTION EXACT_HEAD CAPABILITY_ID CHECKED_AT'
        parse_file "${cfg[APPROVAL_CAPABILITY_EVIDENCE_PATH]}" capability 'capability evidence' "$capability_keys"
        for key in $capability_keys; do [[ -v "capability[$key]" ]] || die "capability evidence is missing $key"; done
        [[ ${capability[SCHEMA_VERSION]} == 1 && ${capability[ENVIRONMENT_PROTECTION]} == verified && ${capability[EXACT_HEAD]} == "${req[SOURCE_COMMIT]}" ]] || die 'GitHub Environment capability evidence is not exact-head verified'
        if [[ ! ${capability[CAPABILITY_ID]} =~ ^[A-Za-z0-9._:@/-]{1,128}$ ]] || ! valid_utc "${capability[CHECKED_AT]}"; then die 'GitHub Environment capability evidence is malformed'; fi
        ;;
      *) die 'unsupported approval provider' ;;
    esac
    if [[ ${cfg[ENVIRONMENT]} == production ]]; then
      [[ -v 'cfg[PRODUCTION_AUTHORIZATION_EVIDENCE_PATH]' ]] || die 'production policy is missing separate authorization evidence'
      inside "${cfg[PRODUCTION_AUTHORIZATION_EVIDENCE_PATH]}" "$evidence_dir" || die 'production authorization evidence is outside the protected evidence directory'
      secure_file "${cfg[PRODUCTION_AUTHORIZATION_EVIDENCE_PATH]}" 'production authorization evidence'
      production_keys='SCHEMA_VERSION ENVIRONMENT TARGET_ID SOURCE_COMMIT ARTIFACT_IMAGE AUTHORIZED_BY GATE_ID AUTHORIZED_AT'
      parse_file "${cfg[PRODUCTION_AUTHORIZATION_EVIDENCE_PATH]}" production 'production authorization evidence' "$production_keys"
      for key in $production_keys; do [[ -v "production[$key]" ]] || die "production authorization evidence is missing $key"; done
      [[ ${production[SCHEMA_VERSION]} == 1 && ${production[ENVIRONMENT]} == production ]] || die 'production authorization evidence has the wrong scope'
      if [[ ! ${production[AUTHORIZED_BY]} =~ ^[A-Za-z0-9._:@/-]{1,128}$ || ! ${production[GATE_ID]} =~ ^[A-Za-z0-9._:@/-]{1,128}$ ]] || ! valid_utc "${production[AUTHORIZED_AT]}"; then die 'production authorization evidence is malformed'; fi
      for key in ENVIRONMENT TARGET_ID SOURCE_COMMIT ARTIFACT_IMAGE; do
        [[ -v "production[$key]" && ${req[$key]} == "${production[$key]}" ]] || die "deployment request does not match production authorization $key"
      done
    fi
    if [[ -e "$consumed_root" || -L "$consumed_root" ]]; then secure_directory "$consumed_root" 'consumed request directory'; else install -d -m 0700 "$consumed_root"; fi
    request_id=$(for key in $request_keys; do printf '%s=%s\0' "$key" "${req[$key]}"; done | sha256sum | cut -d' ' -f1)
    consumed_marker=$consumed_root/$request_id
    [[ ! -e "$consumed_marker" && ! -L "$consumed_marker" ]] || die 'deployment request was already consumed'
    if [[ -e "$audit_log" || -L "$audit_log" ]]; then secure_file "$audit_log" 'deployer audit log'; else install -m 0600 /dev/null "$audit_log"; fi
    exec 8>>"$audit_log"
    secure_file "$install_state" 'deployer install state'
    if [[ -e "$deployed_root" || -L "$deployed_root" ]]; then secure_directory "$deployed_root" 'deployed snapshot directory'; else install -d -m 0700 "$deployed_root"; fi
    [[ -e "$deployed_current" || -L "$deployed_current" ]] || die 'deployed rollback snapshot is missing'
    [[ -L "$deployed_current" ]] || die 'deployed snapshot pointer is absent or unsafe'
    deployed_snapshot=$(readlink -f "$deployed_current")
    inside "$deployed_snapshot" "$deployed_root" || die 'deployed snapshot pointer escapes managed state'
    secure_directory "$deployed_snapshot" 'deployed snapshot'
    secure_file "$deployed_snapshot/policy.conf" 'deployed rollback policy'
    secure_file "$deployed_snapshot/state.json" 'deployed rollback state'
    snapshot=$(mktemp -d "$deployed_root/.snapshot.XXXXXX")
    chmod 0700 "$snapshot"
    install -m 0600 "$config" "$snapshot/policy.conf"
    install -m 0600 "$install_state" "$snapshot/state.json"
    snapshot_policy_sha=$(sha256sum "$snapshot/policy.conf" | cut -d' ' -f1)
    snapshot_state_sha=$(sha256sum "$snapshot/state.json" | cut -d' ' -f1)
    install -m 0600 /dev/null "$consumed_marker"
    audit_pending=1
    audit_phase=pre-adapter
    adapter_status=
    trap deploy_exit EXIT
    trap 'exit 2' INT TERM
    umask 077
    temporary=$(mktemp "$state_root/.active.XXXXXX")
    printf 'pid=%s\nstarted_at=%s\n' "$$" "$(date +%s)" >"$temporary"
    mv -Tf "$temporary" "$active"
    set +e
    systemd-inhibit --what=shutdown:sleep --mode=block --who=ci-fleet-deployer \
      --why='approved deployment is active' -- env CI_FLEET_DEPLOYER_REQUEST="$request_snapshot" "$adapter_path" deploy
    adapter_status=$?
    set -e
    if ((adapter_status != 0)); then
      audit_phase=adapter
      die 'deployment adapter failed after approval consumption'
    fi
    audit_phase=post-adapter
    secure_directory "$deployed_root" 'deployed snapshot directory'
    inside "$snapshot" "$deployed_root" || die 'prepared deployed snapshot escaped managed state'
    secure_directory "$snapshot" 'prepared deployed snapshot'
    secure_file "$snapshot/policy.conf" 'prepared deployed policy'
    secure_file "$snapshot/state.json" 'prepared deployed state'
    [[ $(sha256sum "$snapshot/policy.conf" | cut -d' ' -f1) == "$snapshot_policy_sha" && $(sha256sum "$snapshot/state.json" | cut -d' ' -f1) == "$snapshot_state_sha" ]] || die 'prepared deployed snapshot changed during deployment'
    pointer=$deployed_root/.current.$$
    ln -s "${snapshot##*/}" "$pointer"
    mv -Tf "$pointer" "$deployed_current"
    snapshot=
    if [[ -f "$request" && ! -L "$request" ]] && cmp -s "$request_snapshot" "$request"; then rm -f "$request"; fi
    mv -Tf "$request_snapshot" "$last_request"
    request_snapshot=
    secure_file "$audit_log" 'deployer audit log'
    [[ $(stat -Lc '%d:%i' /proc/self/fd/8) == $(stat -c '%d:%i' "$audit_log") ]] || die 'deployer audit log changed during deployment'
    printf 'time=%s environment=%s target=%s source=%s artifact=%s approval=%s policy=%s result=success\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${req[ENVIRONMENT]}" "${req[TARGET_ID]}" \
      "${req[SOURCE_COMMIT]}" "${req[ARTIFACT_IMAGE]#*@}" "${req[APPROVAL_ID]}" "${req[POLICY_IDENTITY]}" \
      >&8
    audit_pending=0
    ;;
esac
