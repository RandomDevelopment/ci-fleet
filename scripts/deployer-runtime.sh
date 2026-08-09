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
consumed_root=$state_root/consumed-requests
install_state=$state_root/install-state.json
deployed_policy=$state_root/deployed-policy.conf
deployed_state=$state_root/deployed-state.json
audit_log=$log_root/audit.log
systemd_root=$(root_path /etc/systemd/system)

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
for key in ENVIRONMENT TARGET_ID DEPLOYER_IDENTITY ADAPTER_PATH ADAPTER_SHA256 APPROVAL_PROVIDER CHECKPOINT_EVIDENCE_PATH SOURCE_COMMIT ARTIFACT_IMAGE; do [[ -v "cfg[$key]" ]] || die "configuration is missing $key"; done
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
    reject_mixed_role
    secure_file "$request" 'deployment request'
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
    [[ ${checkpoint[CHECKPOINT_ID]} =~ ^[A-Za-z0-9._:@/-]{1,128}$ && ${checkpoint[RECORDED_AT]} =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] || die 'checkpoint evidence is malformed'
    [[ -v 'cfg[APPROVAL_EVIDENCE_PATH]' ]] || die 'installed policy is missing approval evidence'
    inside "${cfg[APPROVAL_EVIDENCE_PATH]}" "$evidence_dir" || die 'approval evidence is outside the protected evidence directory'
    secure_file "${cfg[APPROVAL_EVIDENCE_PATH]}" 'approval evidence'
    approval_keys='SCHEMA_VERSION ENVIRONMENT TARGET_ID SOURCE_COMMIT ARTIFACT_IMAGE APPROVAL_IDENTITY POLICY_IDENTITY APPROVAL_ID APPROVED_AT'
    parse_file "${cfg[APPROVAL_EVIDENCE_PATH]}" approved 'approval evidence' "$approval_keys"
    for key in $approval_keys; do
      [[ -v "approved[$key]" && ${req[$key]} == "${approved[$key]}" ]] || die "deployment request does not match protected approval $key"
    done
    if [[ ${cfg[APPROVAL_PROVIDER]} == github-environment ]]; then
      [[ -v 'cfg[APPROVAL_CAPABILITY_EVIDENCE_PATH]' ]] || die 'GitHub Environment approval is missing capability evidence'
      inside "${cfg[APPROVAL_CAPABILITY_EVIDENCE_PATH]}" "$evidence_dir" || die 'capability evidence is outside the protected evidence directory'
      secure_file "${cfg[APPROVAL_CAPABILITY_EVIDENCE_PATH]}" 'GitHub capability evidence'
      capability_keys='SCHEMA_VERSION ENVIRONMENT_PROTECTION EXACT_HEAD CAPABILITY_ID CHECKED_AT'
      parse_file "${cfg[APPROVAL_CAPABILITY_EVIDENCE_PATH]}" capability 'capability evidence' "$capability_keys"
      for key in $capability_keys; do [[ -v "capability[$key]" ]] || die "capability evidence is missing $key"; done
      [[ ${capability[SCHEMA_VERSION]} == 1 && ${capability[ENVIRONMENT_PROTECTION]} == verified && ${capability[EXACT_HEAD]} == "${req[SOURCE_COMMIT]}" ]] || die 'GitHub Environment capability evidence is not exact-head verified'
      [[ ${capability[CAPABILITY_ID]} =~ ^[A-Za-z0-9._:@/-]{1,128}$ && ${capability[CHECKED_AT]} =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] || die 'GitHub Environment capability evidence is malformed'
    fi
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
    if [[ -e "$consumed_root" || -L "$consumed_root" ]]; then secure_directory "$consumed_root" 'consumed request directory'; else install -d -m 0700 "$consumed_root"; fi
    request_id=$(for key in $request_keys; do printf '%s=%s\0' "$key" "${req[$key]}"; done | sha256sum | cut -d' ' -f1)
    consumed_marker=$consumed_root/$request_id
    [[ ! -e "$consumed_marker" && ! -L "$consumed_marker" ]] || die 'deployment request was already consumed'
    if [[ -e "$audit_log" || -L "$audit_log" ]]; then secure_file "$audit_log" 'deployer audit log'; else install -m 0600 /dev/null "$audit_log"; fi
    : >>"$audit_log"
    secure_file "$install_state" 'deployer install state'
    for path in "$deployed_policy" "$deployed_state"; do [[ ! -e "$path" && ! -L "$path" ]] || secure_file "$path" 'deployed rollback state'; done
    install -m 0600 "$config" "$state_root/.deployed-policy.new"
    install -m 0600 "$install_state" "$state_root/.deployed-state.new"
    install -m 0600 /dev/null "$consumed_marker"
    umask 077
    temporary=$(mktemp "$state_root/.active.XXXXXX")
    printf 'pid=%s\nstarted_at=%s\n' "$$" "$(date +%s)" >"$temporary"
    mv -Tf "$temporary" "$active"
    trap 'rm -f "$active" "$state_root/.deployed-policy.new" "$state_root/.deployed-state.new"' EXIT INT TERM
    systemd-inhibit --what=shutdown:sleep --mode=block --who=ci-fleet-deployer \
      --why='approved deployment is active' -- "${cfg[ADAPTER_PATH]}" deploy
    mv -Tf "$state_root/.deployed-policy.new" "$deployed_policy"
    mv -Tf "$state_root/.deployed-state.new" "$deployed_state"
    mv -Tf "$request" "$last_request"
    printf 'time=%s environment=%s target=%s source=%s artifact=%s approval=%s policy=%s result=success\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${req[ENVIRONMENT]}" "${req[TARGET_ID]}" \
      "${req[SOURCE_COMMIT]}" "${req[ARTIFACT_IMAGE]#*@}" "${req[APPROVAL_ID]}" "${req[POLICY_IDENTITY]}" \
      >>"$audit_log"
    ;;
esac
