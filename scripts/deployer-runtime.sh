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
restore_encoded_file() {
  local destination=$1 encoded=$2 temporary
  temporary=$(mktemp "$(dirname "$destination")/.restore.XXXXXX") || return
  printf '%s' "$encoded" | base64 -d >"$temporary" || { rm -f "$temporary"; return 1; }
  chown "$expected_uid" "$temporary" || { rm -f "$temporary"; return 1; }
  chmod 0600 "$temporary" || { rm -f "$temporary"; return 1; }
  mv -Tf "$temporary" "$destination"
}
if [[ $operation == deploy && -z ${CI_FLEET_DEPLOYER_INHIBITED:-} ]]; then
  export CI_FLEET_DEPLOYER_INHIBITED=1
  [[ $testing != 1 || -z ${CI_FLEET_DEPLOYER_TEST_INHIBITOR_LOG:-} ]] || printf '%s\n' deploy >>"$CI_FLEET_DEPLOYER_TEST_INHIBITOR_LOG"
  exec systemd-inhibit --what=shutdown:sleep --mode=block --who=ci-fleet-deployer \
    --why='approved deployment is active' -- "$0" deploy
fi
deploy_exit() {
  local status=$?
  local recorded_status=${adapter_status:-$status}
  local target state_root_safe=1
  # The adapter may recursively clear its writable state root. Recreate only
  # an absent boundary; an unsafe replacement remains a hard failure.
  if [[ ${audit_pending:-0} == 1 && ! -e "$state_root" && ! -L "$state_root" ]]; then
    install -d -m 0700 "$state_root" 2>/dev/null || { status=1; state_root_safe=0; }
  fi
  if [[ -e "$state_root" || -L "$state_root" ]]; then
    [[ -d "$state_root" && ! -L "$state_root" && $(stat -c '%u:%a' "$state_root" 2>/dev/null) == "$expected_uid:700" ]] || { status=1; state_root_safe=0; }
  fi
  # An adapter that deletes its own consumption marker must not defeat replay
  # protection; restore the durable marker rather than clearing audit_pending.
  if [[ ${audit_pending:-0} == 1 && -n ${consumed_marker:-} && ! -e "$consumed_marker" ]]; then
    [[ ! -e "$consumed_root" && ! -L "$consumed_root" ]] || secure_directory "$consumed_root" 'consumed request directory'
    [[ -e "$consumed_root" ]] || install -d -m 0700 "$consumed_root" 2>/dev/null || true
    install -m 0600 /dev/null "$consumed_marker" 2>/dev/null || true
    sync -f "$consumed_marker" 2>/dev/null || sync "$consumed_marker" 2>/dev/null || status=1
    sync -f "$consumed_root" 2>/dev/null || sync "$consumed_root" 2>/dev/null || status=1
  fi
  if [[ ${audit_pending:-0} == 1 ]]; then
    # If the adapter replaced the audit log, restore the durable prefix copy
    # (or the opened inode) under its name before appending the terminal
    # failure record, so the record is never written only to an unlinked inode.
    if [[ -e "$audit_log" && ! -L "$audit_log" && $(stat -c '%u:%a' "$audit_log" 2>/dev/null) != "$expected_uid:600" ]]; then
      chown "$expected_uid" "$audit_log" 2>/dev/null || status=1
      chmod 0600 "$audit_log" 2>/dev/null || status=1
    fi
    if [[ ! -e "$audit_log" || $(stat -Lc '%d:%i' /proc/self/fd/8 2>/dev/null) != $(stat -c '%d:%i' "$audit_log" 2>/dev/null) ]]; then
      rm -f -- "$audit_log"
      if [[ -n ${audit_prefix_backup:-} ]]; then
        restore_encoded_file "$audit_log" "$audit_prefix_backup" 2>/dev/null || status=1
      else
        cat /proc/self/fd/8 >"$audit_log" 2>/dev/null || status=1
      fi
      chmod 0600 "$audit_log" 2>/dev/null || true
      printf 'time=%s environment=%s target=%s source=%s artifact=%s approval=%s approver=%s policy=%s checkpoint=%s authorized_by=%s gate=%s result=failed phase=%s status=%s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${req[ENVIRONMENT]}" "${req[TARGET_ID]}" \
        "${req[SOURCE_COMMIT]}" "${req[ARTIFACT_IMAGE]#*@}" "${req[APPROVAL_ID]}" "${req[APPROVAL_IDENTITY]}" "${req[POLICY_IDENTITY]}" \
        "${checkpoint[CHECKPOINT_ID]:-none}" "${production[AUTHORIZED_BY]:-none}" "${production[GATE_ID]:-none}" \
        "${audit_phase:-post-consumption}" "$recorded_status" >>"$audit_log" || status=1
    else
      printf 'time=%s environment=%s target=%s source=%s artifact=%s approval=%s approver=%s policy=%s checkpoint=%s authorized_by=%s gate=%s result=failed phase=%s status=%s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${req[ENVIRONMENT]}" "${req[TARGET_ID]}" \
        "${req[SOURCE_COMMIT]}" "${req[ARTIFACT_IMAGE]#*@}" "${req[APPROVAL_ID]}" "${req[APPROVAL_IDENTITY]}" "${req[POLICY_IDENTITY]}" \
        "${checkpoint[CHECKPOINT_ID]:-none}" "${production[AUTHORIZED_BY]:-none}" "${production[GATE_ID]:-none}" \
        "${audit_phase:-post-consumption}" "$recorded_status" >&8 || status=1
    fi
    sync -f "$audit_log" 2>/dev/null || sync "$audit_log" 2>/dev/null || status=1

  fi
  # On failure, restore the validated incumbent deployed pointer before the
  # active-operation guard is durably cleared — but only when the new pointer
  # was never published. After publication the new pointer is the truth: the
  # application has changed and restoring the incumbent would falsify state.
  if [[ ${audit_pending:-0} == 1 && -n ${incumbent_pointer:-} && ${snapshot_pointer:-} != "$deployed_current" ]]; then
    # Restore the incumbent snapshot content from process memory when the
    # adapter deleted its writable state root.
    if [[ -n ${incumbent_policy_backup:-} && -n ${incumbent_state_backup:-} && $state_root_safe == 1 ]]; then
      if [[ ! -e "$deployed_root" && ! -L "$deployed_root" ]]; then install -d -m 0700 "$deployed_root" 2>/dev/null || status=1; fi
      [[ -d "$deployed_root" && ! -L "$deployed_root" ]] || status=1
      if [[ -e "$deployed_root/$incumbent_pointer" || -L "$deployed_root/$incumbent_pointer" ]]; then
        [[ -d "$deployed_root/$incumbent_pointer" && ! -L "$deployed_root/$incumbent_pointer" ]] || { rm -rf -- "${deployed_root:?}/$incumbent_pointer"; mkdir -m 0700 "$deployed_root/$incumbent_pointer"; }
        chown "$expected_uid" "$deployed_root/$incumbent_pointer" 2>/dev/null || status=1
        chmod 0700 "$deployed_root/$incumbent_pointer" 2>/dev/null || status=1
      fi
      mkdir -m 0700 "$deployed_root/$incumbent_pointer" 2>/dev/null || true
      restore_encoded_file "$deployed_root/$incumbent_pointer/policy.conf" "$incumbent_policy_backup" 2>/dev/null || status=1
      restore_encoded_file "$deployed_root/$incumbent_pointer/state.json" "$incumbent_state_backup" 2>/dev/null || status=1
    fi
    if [[ ! -L "$deployed_current" || $(readlink "$deployed_current") != "$incumbent_pointer" ]]; then
      rm -f -- "$deployed_current"
      ln -s "$incumbent_pointer" "$deployed_current" 2>/dev/null || status=1
      sync -f "$deployed_root" 2>/dev/null || sync "$deployed_root" 2>/dev/null || status=1
    fi
  fi

  if [[ ${lkg_backed_up:-0} == 1 && $state_root_safe == 1 ]]; then
    restore_encoded_file "$state_root/last-known-good.json" "$lkg_state_backup" 2>/dev/null || status=1
    restore_encoded_file "$state_root/last-known-good-policy.conf" "$lkg_policy_backup" 2>/dev/null || status=1
  fi
  if [[ ${audit_pending:-0} == 1 && ${last_request_backed_up:-0} == 1 && $state_root_safe == 1 ]]; then
    restore_encoded_file "$last_request" "$last_request_backup" 2>/dev/null || status=1
  fi
  rm -f "$active" "${active_temporary:-}" "${request_snapshot:-}" "${policy_snapshot:-}" || true
  sync -f "$state_root" 2>/dev/null || sync "$state_root" 2>/dev/null || status=1
  if [[ -n ${snapshot:-} && -d $snapshot && ! -L $snapshot ]]; then
    target=$(readlink "$deployed_current" 2>/dev/null || true)
    if [[ $target != "${snapshot##*/}" ]]; then rm -rf -- "$snapshot"; fi
  fi
  if [[ -n ${snapshot_pointer:-} && $snapshot_pointer != "$deployed_current" ]]; then rm -f "$snapshot_pointer"; fi
  return "$status"
}
expected_uid=0
[[ "$testing" != 1 ]] || expected_uid=$(id -u)
# Docker checks must target the host's local daemon, not an inherited remote
# DOCKER_HOST or selected context.
DOCKER_HOST=unix://$(root_path /run/docker.sock)
export DOCKER_HOST
unset DOCKER_CONTEXT
declare -A production=()
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
  for path in "$systemd_root/ci-fleet-status-receiver.service" "$(root_path /etc/ci-fleet-status)" "$(root_path /var/lib/ci-fleet-status)"; do
    [[ ! -e "$path" && ! -L "$path" ]] || die 'status receiver state is present'
  done
  for unit in ci-fleet-health.service ci-fleet-health.timer ci-fleet-reconcile.service ci-fleet-reconcile.timer ci-fleet-cleanup.service ci-fleet-cleanup.timer ci-fleet-drift.service ci-fleet-drift.timer actions.runner.service; do
    [[ ! -e "$systemd_root/$unit" && ! -L "$systemd_root/multi-user.target.wants/$unit" && ! -L "$systemd_root/timers.target.wants/$unit" ]] || die 'ordinary CI controller or runner state is present'
  done
  shopt -s nullglob
  for runner_unit in "$systemd_root"/actions.runner.*.service "$systemd_root"/multi-user.target.wants/actions.runner.*.service; do
    shopt -u nullglob
    [[ -n "$runner_unit" ]] && die 'ordinary GitHub Actions runner service is present'
  done
  shopt -u nullglob
  for path in "$(root_path /etc/ci-fleet/ci-fleet.env)" "$(root_path /etc/ci-fleet/host.env)" "$(root_path /etc/ci-fleet/secrets)" "$(root_path /opt/ci-fleet/current)" "$(root_path /var/lib/ci-fleet/install-state.json)"; do
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
if [[ -z ${CI_FLEET_DEPLOYER_REEXEC:-} ]]; then
  exec 9<"$lock_dir"
  flock -n 9 || die 'another deployer operation is running'
else
  # The re-executed process inherits the locked descriptor 9 from its parent;
  # reopening it would drop the lock during the handoff.
  :
fi
shopt -s nullglob
transactions=("$state_root"/.transaction.*)
shopt -u nullglob
((${#transactions[@]} == 0)) || die 'interrupted installer transaction requires recovery'

if [[ "$operation" == drain ]]; then
  [[ ! -e "$active" && ! -L "$active" ]] || die 'active deployment prevents drain'
  temporary=$(mktemp "$state_root/.drained.XXXXXX")
  chmod 0600 "$temporary"
  mv -Tf "$temporary" "$drained"
  sync -f "$drained" 2>/dev/null || sync "$drained" 2>/dev/null || die 'drain marker is not durable'
  sync -f "$state_root" 2>/dev/null || die 'drain marker publication is not durable'
  exit 0
fi

secure_file "$config" 'deployer configuration'
policy_snapshot=$(mktemp "$state_root/.active-policy.XXXXXX")
install -m 0600 "$config" "$policy_snapshot"
config=$policy_snapshot
trap 'rm -f "${policy_snapshot:-}"' EXIT
config_keys='SCHEMA_VERSION CORE_REF ENVIRONMENT TARGET_ID DEPLOYER_IDENTITY ADAPTER_PATH ADAPTER_SHA256 CREDENTIAL_PROVIDER CREDENTIAL_REF CREDENTIAL_SCOPE APPROVAL_PROVIDER APPROVAL_EVIDENCE_PATH APPROVAL_CAPABILITY_EVIDENCE_PATH PRODUCTION_AUTHORIZATION_EVIDENCE_PATH CHECKPOINT_EVIDENCE_PATH SOURCE_COMMIT ARTIFACT_IMAGE NETWORK_HOST MIN_DISK_GIB REQUIRE_COMPOSE'
parse_file "$config" cfg configuration "$config_keys"
for key in ENVIRONMENT TARGET_ID DEPLOYER_IDENTITY ADAPTER_PATH ADAPTER_SHA256 CREDENTIAL_PROVIDER CREDENTIAL_REF CREDENTIAL_SCOPE APPROVAL_PROVIDER CHECKPOINT_EVIDENCE_PATH SOURCE_COMMIT ARTIFACT_IMAGE; do [[ -v "cfg[$key]" ]] || die "configuration is missing $key"; done
[[ ${cfg[SCHEMA_VERSION]:-} == 1 ]] || die 'configuration has an unsupported or missing schema version'
[[ ${cfg[CORE_REF]:-} =~ ^[0-9a-f]{40}$ ]] || die 'configuration is missing a valid core revision'
# A concurrent upgrade may switch current between ExecStart resolution and the
# lock; re-exec the release matching the active policy before trusting it.
own_realpath=$(realpath -e -- "${BASH_SOURCE[0]}")
if [[ $own_realpath == */releases/* ]]; then
  install_prefix=${own_realpath%%/releases/*}
  own_release=${own_realpath#*/releases/}
  own_release=${own_release%%/*}
  if [[ $own_release != "${cfg[CORE_REF]}" ]]; then
    selected=$install_prefix/releases/${cfg[CORE_REF]}/scripts/deployer-runtime.sh
    [[ -x $selected && ! -L $selected ]] || die 'active policy revision runtime is unavailable'
    [[ -z ${CI_FLEET_DEPLOYER_REEXEC:-} ]] || die 'runtime re-exec did not select the active revision'
    # exec replaces this process without running the EXIT trap; the policy
    # snapshot is passed by content to the new process via the environment, so
    # remove the unmanaged copy before re-exec.
    rm -f "$policy_snapshot"
    policy_snapshot=
    export CI_FLEET_DEPLOYER_REEXEC=1
    exec "$selected" "$operation"
  fi
fi
[[ ${cfg[ENVIRONMENT]} =~ ^[a-z][a-z0-9-]{0,31}$ && ${cfg[TARGET_ID]} =~ ^[a-z0-9][a-z0-9._-]{0,63}$ ]] || die 'invalid environment or target identity'
[[ ${cfg[ADAPTER_SHA256]} =~ ^[0-9a-f]{64}$ ]] || die 'invalid adapter digest'
inside "${cfg[ADAPTER_PATH]}" "$(root_path /etc/ci-fleet-deployer/adapters)" || die 'application adapter is outside the protected adapter directory'
secure_file "${cfg[ADAPTER_PATH]}" 'application adapter' 700
exec 7<"${cfg[ADAPTER_PATH]}"
[[ $(sha256sum /proc/$$/fd/7 | cut -d' ' -f1) == "${cfg[ADAPTER_SHA256]}" ]] || die 'application adapter digest mismatch'
adapter_path=/proc/$$/fd/7
validate_credential

secure_directory "$log_root" 'deployer log directory'

case "$operation" in
  health)
    reject_mixed_role
    validate_credential
    env CI_FLEET_DEPLOYER_CONFIG="$config" "$adapter_path" "$operation"
    ;;
  cleanup)
    not_drained
    reject_mixed_role
    validate_credential
    # Scheduled cleanup mutates application-owned resources; production paths
    # remain separately gated like deploy.
    [[ ${cfg[ENVIRONMENT]} != production ]] || die 'production cleanup is not authorized by the current accepted scope'
    env CI_FLEET_DEPLOYER_CONFIG="$config" "$adapter_path" cleanup
    ;;
  deploy)
    not_drained
    [[ ! -e "$active" && ! -L "$active" ]] || die 'active operation marker requires recovery'
    reject_mixed_role
    secure_file "$request" 'deployment request'
    request_snapshot=$(mktemp "$state_root/.request.XXXXXX")
    install -m 0600 "$request" "$request_snapshot"
    request_snapshot_sha=$(sha256sum "$request_snapshot" | cut -d' ' -f1)
    request_snapshot_backup=$(base64 -w0 "$request_snapshot")
    trap 'rm -f "${request_snapshot:-}" "${policy_snapshot:-}"' EXIT
    trap 'exit 2' INT TERM
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
        capability_keys='SCHEMA_VERSION ENVIRONMENT TARGET_ID ENVIRONMENT_PROTECTION EXACT_HEAD CAPABILITY_ID CHECKED_AT'
        parse_file "${cfg[APPROVAL_CAPABILITY_EVIDENCE_PATH]}" capability 'capability evidence' "$capability_keys"
        for key in $capability_keys; do [[ -v "capability[$key]" ]] || die "capability evidence is missing $key"; done
        [[ ${capability[ENVIRONMENT]} == "${req[ENVIRONMENT]}" && ${capability[TARGET_ID]} == "${req[TARGET_ID]}" ]] || die 'GitHub Environment capability evidence does not match this installation'
        [[ ${capability[SCHEMA_VERSION]} == 1 && ${capability[ENVIRONMENT_PROTECTION]} == verified && ${capability[EXACT_HEAD]} == "${req[SOURCE_COMMIT]}" ]] || die 'GitHub Environment capability evidence is not exact-head verified'
        if [[ ! ${capability[CAPABILITY_ID]} =~ ^[A-Za-z0-9._:@/-]{1,128}$ ]] || ! valid_utc "${capability[CHECKED_AT]}"; then die 'GitHub Environment capability evidence is malformed'; fi
        ;;
      *) die 'unsupported approval provider' ;;
    esac
    if [[ ${cfg[ENVIRONMENT]} == production ]]; then
      # Production deployment paths remain separately gated
      # (docs/DESIGN-DECISIONS.md); no accepted decision enables them yet.
      die 'production deployment is not authorized by the current accepted scope'
    fi
    if [[ -e "$consumed_root" || -L "$consumed_root" ]]; then secure_directory "$consumed_root" 'consumed request directory'; else install -d -m 0700 "$consumed_root"; fi
    request_id=$(for key in $request_keys; do printf '%s=%s\0' "$key" "${req[$key]}"; done | sha256sum | cut -d' ' -f1)
    consumed_marker=$consumed_root/$request_id
    [[ ! -e "$consumed_marker" && ! -L "$consumed_marker" ]] || die 'deployment request was already consumed'
    if [[ -e "$audit_log" || -L "$audit_log" ]]; then secure_file "$audit_log" 'deployer audit log'; else install -m 0600 /dev/null "$audit_log"; fi
    exec 8>>"$audit_log"
    secure_file "$install_state" 'deployer install state'
    python3 - "$install_state" "${cfg[CORE_REF]}" "${cfg[ENVIRONMENT]}" "${cfg[TARGET_ID]}" "${cfg[DEPLOYER_IDENTITY]}" "${cfg[SOURCE_COMMIT]}" "${cfg[ARTIFACT_IMAGE]}" <<'PY' >/dev/null 2>&1 || die 'deployer install state is malformed'
import json, sys
try: value = json.load(open(sys.argv[1], encoding='utf-8'))
except (OSError, ValueError): raise SystemExit(1)
keys = ('core_ref', 'environment', 'target', 'deployer_identity', 'source_commit', 'artifact')
raise SystemExit(0 if all(value.get(k) == v for k, v in zip(keys, sys.argv[2:])) else 1)
PY
    [[ ${cfg[CORE_REF]} =~ ^[0-9a-f]{40}$ ]] || die 'configuration is missing a valid core revision'
    if [[ -e "$deployed_root" || -L "$deployed_root" ]]; then secure_directory "$deployed_root" 'deployed snapshot directory'; else install -d -m 0700 "$deployed_root"; fi
    [[ -e "$deployed_current" || -L "$deployed_current" ]] || die 'deployed rollback snapshot is missing'
    # The rollback path consumes the retained last-known-good pair, not the
    # deployed snapshot; once that pair exists it must be complete and valid
    # before approval consumption.
    previous_state=$state_root/last-known-good.json
    previous_policy=$state_root/last-known-good-policy.conf
    if [[ -e "$previous_state" || -L "$previous_state" || -e "$previous_policy" || -L "$previous_policy" ]]; then
      secure_file "$previous_state" 'last-known-good state'
      secure_file "$previous_policy" 'last-known-good policy'
      declare -A lkg_policy=()
      parse_file "$previous_policy" lkg_policy 'last-known-good policy' "$config_keys"
      [[ ${lkg_policy[CORE_REF]:-} =~ ^[0-9a-f]{40}$ ]] || die 'last-known-good policy has an invalid core revision'
      # Validate the entire retained release as perform_rollback's
      # release_complete does: ownership, modes, types, and tree digest.
      lkg_release=$(root_path /opt/ci-fleet-deployer)/releases/${lkg_policy[CORE_REF]}
      [[ -d "$lkg_release" && ! -L "$lkg_release" && $(stat -c '%u:%a' "$lkg_release") == "$expected_uid:755" ]] || die 'last-known-good release is incomplete'
      for dir in "$lkg_release/scripts" "$lkg_release/deploy" "$lkg_release/deploy/deployer"; do [[ -d "$dir" && ! -L "$dir" && $(stat -c '%u:%a' "$dir") == "$expected_uid:755" ]] || die 'last-known-good release is incomplete'; done
      for entry in "$lkg_release/scripts/install-deployer.sh" "$lkg_release/scripts/deployer-runtime.sh"; do [[ ! -L "$entry" && -f "$entry" && $(stat -c '%u:%a' "$entry") == "$expected_uid:755" ]] || die 'last-known-good release is incomplete'; done
      [[ -f "$lkg_release/.ci-fleet-tree-sha256" ]] || die 'last-known-good release is incomplete'
      [[ $(<"$lkg_release/.ci-fleet-tree-sha256") == "$(cd "$lkg_release" && sha256sum scripts/install-deployer.sh scripts/deployer-runtime.sh deploy/deployer/* | sha256sum | cut -d' ' -f1)" ]] || die 'last-known-good release tree digest mismatch'
      for entry in "$lkg_release"/deploy/deployer/*; do [[ -f "$entry" && ! -L "$entry" && $(stat -c '%u:%a' "$entry") == "$expected_uid:644" ]] || die 'last-known-good release is incomplete'; done
      # Cross-validate the retained pair exactly as the rollback path does:
      # state must match policy, and the retained adapter must match its pin.
      python3 - "$previous_state" "$previous_policy" <<'PY' >/dev/null 2>&1 || die 'last-known-good state and policy do not cross-validate'
import json, re, sys
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
        if sep and key not in policy:
            policy[key] = value
except OSError:
    raise SystemExit(1)
sha = re.compile(r'^[0-9a-f]{40}$')
pairs = (('core_ref','CORE_REF'), ('environment','ENVIRONMENT'), ('target','TARGET_ID'), ('source_commit','SOURCE_COMMIT'), ('artifact','ARTIFACT_IMAGE'), ('deployer_identity','DEPLOYER_IDENTITY'))
ok = all(state.get(k) and state.get(k) == policy.get(p) for k, p in pairs)
ok = ok and bool(sha.match(state['core_ref'])) and bool(sha.match(state['source_commit']))
ok = ok and bool(re.search(r'@sha256:[0-9a-f]{64}$', state['artifact']))
ok = ok and policy.get('CREDENTIAL_SCOPE') == policy.get('ENVIRONMENT')
raise SystemExit(0 if ok else 1)
PY
      [[ -v 'lkg_policy[ADAPTER_PATH]' && -v 'lkg_policy[ADAPTER_SHA256]' ]] || die 'last-known-good policy is missing its adapter pin'
      inside "${lkg_policy[ADAPTER_PATH]}" "$deployer_etc/adapters" || die 'last-known-good adapter is outside the protected adapter directory'
      [[ ! -L "${lkg_policy[ADAPTER_PATH]}" && -f "${lkg_policy[ADAPTER_PATH]}" && $(stat -c '%u:%a' "${lkg_policy[ADAPTER_PATH]}") == "$expected_uid:700" ]] || die 'last-known-good adapter is missing or unsafe'
      [[ $(sha256sum "${lkg_policy[ADAPTER_PATH]}" | cut -d' ' -f1) == "${lkg_policy[ADAPTER_SHA256]}" ]] || die 'last-known-good adapter digest does not match its retained policy'
    fi
    [[ -L "$deployed_current" ]] || die 'deployed snapshot pointer is absent or unsafe'
    [[ $(readlink "$deployed_current") =~ ^\.snapshot\.[A-Za-z0-9._-]+$ ]] || die 'deployed snapshot pointer target is not canonical'
    deployed_snapshot=$(readlink -f "$deployed_current")
    inside "$deployed_snapshot" "$deployed_root" || die 'deployed snapshot pointer escapes managed state'
    secure_directory "$deployed_snapshot" 'deployed snapshot'
    secure_file "$deployed_snapshot/policy.conf" 'deployed rollback policy'
    secure_file "$deployed_snapshot/state.json" 'deployed rollback state'
    # Deploying consumes the approval; the retained rollback point must be
    # fully usable first, or the change proceeds with no way back.
    declare -A deployed_policy=()
    parse_file "$deployed_snapshot/policy.conf" deployed_policy 'deployed rollback policy' "$config_keys"
    for key in ADAPTER_PATH ADAPTER_SHA256 CREDENTIAL_PROVIDER CREDENTIAL_REF CREDENTIAL_SCOPE ENVIRONMENT TARGET_ID; do [[ -v "deployed_policy[$key]" ]] || die "deployed rollback policy is missing $key"; done
    inside "${deployed_policy[ADAPTER_PATH]}" "$deployer_etc/adapters" || die 'deployed rollback adapter is outside the protected adapter directory'
    [[ ! -L "${deployed_policy[ADAPTER_PATH]}" && -f "${deployed_policy[ADAPTER_PATH]}" && $(stat -c '%u:%a' "${deployed_policy[ADAPTER_PATH]}") == "$expected_uid:700" ]] || die 'deployed rollback adapter is missing or unsafe'
    [[ $(sha256sum "${deployed_policy[ADAPTER_PATH]}" | cut -d' ' -f1) == "${deployed_policy[ADAPTER_SHA256]}" ]] || die 'deployed rollback adapter digest does not match its snapshot policy'
    [[ ${deployed_policy[CREDENTIAL_SCOPE]} == "${deployed_policy[ENVIRONMENT]}" ]] || die 'deployed rollback credential scope does not match its environment'
    if [[ ${deployed_policy[CREDENTIAL_PROVIDER]} == file ]]; then
      inside "${deployed_policy[CREDENTIAL_REF]}" "$credential_dir" || die 'deployed rollback credential is outside the protected credential directory'
      secure_file "${deployed_policy[CREDENTIAL_REF]}" 'deployed rollback credential'
    elif [[ ${deployed_policy[CREDENTIAL_PROVIDER]} == external ]]; then
      [[ ${deployed_policy[CREDENTIAL_REF]} =~ ^external:[a-z0-9][a-z0-9-]{0,31}:[A-Za-z0-9._/-]{1,128}$ ]] || die 'deployed rollback policy has an invalid external secret-manager adapter reference'
    else
      die 'deployed rollback policy has an unsupported credential provider'
    fi
    snapshot=$(mktemp -d "$deployed_root/.snapshot.XXXXXX")
    chmod 0700 "$snapshot"
    install -m 0600 "$config" "$snapshot/policy.conf"
    install -m 0600 "$install_state" "$snapshot/state.json"
    snapshot_policy_backup=$(base64 -w0 "$snapshot/policy.conf")
    snapshot_state_backup=$(base64 -w0 "$snapshot/state.json")
    reject_mixed_role
    validate_credential
    audit_pending=1
    audit_phase=pre-adapter
    adapter_status=
    trap deploy_exit EXIT
    trap 'exit 2' INT TERM
    install -m 0600 /dev/null "$consumed_marker" || die 'deployment request consumption marker failed'
    sync -f "$consumed_marker" 2>/dev/null || sync "$consumed_marker" 2>/dev/null || die 'deployment request consumption marker is not durable'
    sync -f "$consumed_root" 2>/dev/null || sync "$consumed_root" 2>/dev/null || die 'deployment request consumption is not durable'
    umask 077
    temporary=$(mktemp "$state_root/.active.XXXXXX")
    active_temporary=$temporary
    printf 'pid=%s\nstarted_at=%s\n' "$$" "$(date +%s)" >"$temporary"
    if [[ $testing != 1 ]]; then
      printf 'boot_id=%s\nstart_time=%s\n' "$(</proc/sys/kernel/random/boot_id)" "$(awk '{print $22}' /proc/$$/stat)" >>"$temporary"
    fi
    mv -Tf "$temporary" "$active"
    active_temporary=
    sync -f "$active" 2>/dev/null || sync "$active" 2>/dev/null || die 'active operation marker is not durable'
    sync -f "$state_root" 2>/dev/null || die 'active operation marker publication is not durable'
    # Record the durably consumed approval before the adapter runs, so even a
    # SIGKILL or power loss mid-adapter leaves the attempt's identities.
    printf 'time=%s environment=%s target=%s source=%s artifact=%s approval=%s approver=%s policy=%s checkpoint=%s authorized_by=%s gate=%s result=consumed phase=pre-adapter status=none\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${req[ENVIRONMENT]}" "${req[TARGET_ID]}" \
      "${req[SOURCE_COMMIT]}" "${req[ARTIFACT_IMAGE]#*@}" "${req[APPROVAL_ID]}" "${req[APPROVAL_IDENTITY]}" "${req[POLICY_IDENTITY]}" \
      "${checkpoint[CHECKPOINT_ID]:-none}" "${production[AUTHORIZED_BY]:-none}" "${production[GATE_ID]:-none}" \
      >&8 || die 'deployment consumption audit record failed'
    sync -f "$audit_log" 2>/dev/null || sync "$audit_log" 2>/dev/null || die 'deployment consumption audit record is not durable'
    audit_prefix_sha=$(sha256sum "$audit_log" | cut -d' ' -f1)
    audit_prefix_backup=$(base64 -w0 "$audit_log")
    # Preserve the retained rollback pair in unexported process memory. A
    # filesystem backup under the adapter-writable state root would be lost to
    # the same recursive cleanup as the retained files.
    if [[ -f "$previous_state" && -f "$previous_policy" ]]; then
      lkg_state_backup=$(base64 -w0 "$previous_state")
      lkg_policy_backup=$(base64 -w0 "$previous_policy")
      lkg_backed_up=1
    fi
    if [[ -f "$last_request" && ! -L "$last_request" ]]; then
      last_request_backup=$(base64 -w0 "$last_request")
      last_request_backed_up=1
    fi
    # Capture the validated incumbent pointer independently of adapter-writable
    # state so a failed adapter cannot destroy or falsify the rollback point.
    incumbent_pointer=$(readlink "$deployed_current")
    incumbent_policy_backup=$(base64 -w0 "$deployed_current/policy.conf")
    incumbent_state_backup=$(base64 -w0 "$deployed_current/state.json")
    set +e
    env CI_FLEET_DEPLOYER_CONFIG="$config" CI_FLEET_DEPLOYER_REQUEST="$request_snapshot" "$adapter_path" deploy
    adapter_status=$?
    set -e
    if ((adapter_status == 0)); then
      adapter_status=
      audit_phase=post-adapter
      # The application has changed. Rebuild the prepared state from the
      # protected in-memory copy and publish it before any later check can fail.
      trap '' INT TERM
      if [[ ! -d "$deployed_root" || -L "$deployed_root" || $(stat -c '%u:%a' "$deployed_root" 2>/dev/null) != "$expected_uid:700" ]]; then
        rm -rf -- "$deployed_root"
        install -d -m 0700 "$deployed_root"
      fi
      restore_encoded_file "$request_snapshot" "$request_snapshot_backup" || die 'deployment request snapshot restoration failed'
      rm -rf -- "$snapshot"
      install -d -m 0700 "$snapshot"
      restore_encoded_file "$snapshot/policy.conf" "$snapshot_policy_backup" || die 'prepared deployed policy restoration failed'
      restore_encoded_file "$snapshot/state.json" "$snapshot_state_backup" || die 'prepared deployed state restoration failed'
      pointer=$(mktemp -u "$deployed_root/.current.XXXXXX")
      snapshot_pointer=$pointer
      ln -s "${snapshot##*/}" "$pointer"
      sync -f "$snapshot/policy.conf" "$snapshot/state.json" 2>/dev/null || die 'prepared deployed snapshot is not durable'
      sync -f "$snapshot" 2>/dev/null || sync "$snapshot" 2>/dev/null || die 'prepared deployed snapshot is not durable'
      mv -Tf "$pointer" "$deployed_current"
      sync -f "$deployed_root" 2>/dev/null || sync "$deployed_root" 2>/dev/null || die 'deployed snapshot pointer is not durable'
      snapshot_pointer=$deployed_current
      snapshot=
    fi
    # An in-place truncation keeps the same inode; the durable prefix must survive.
    if [[ $(sha256sum "$audit_log" | cut -d' ' -f1) != "$audit_prefix_sha" ]]; then
      restore_encoded_file "$audit_log" "$audit_prefix_backup" 2>/dev/null || true
      die 'deployer audit log changed during deployment'
    fi
    if ((adapter_status != 0)); then
      audit_phase=adapter
      die 'deployment adapter failed after approval consumption'
    fi
    secure_directory "$deployed_root" 'deployed snapshot directory'
    # The adapter receives the request snapshot path; only verified bytes may
    # become the completed-request record.
    secure_file "$request_snapshot" 'deployment request snapshot'
    [[ $(sha256sum "$request_snapshot" | cut -d' ' -f1) == "$request_snapshot_sha" ]] || die 'deployment request snapshot changed during deployment'

    # Retire the validated pre-adapter incumbent, never an adapter-writable
    # pointer target read after the adapter ran.
    retired_snapshot=$incumbent_pointer
    [[ -z "$retired_snapshot" || "$retired_snapshot" =~ ^\.snapshot\.[A-Za-z0-9._-]+$ ]] || die 'current deployed snapshot pointer is unsafe'
    [[ -z ${CI_FLEET_DEPLOYER_TEST_SIGNAL_SELF:-} || $testing != 1 ]] || kill -"$CI_FLEET_DEPLOYER_TEST_SIGNAL_SELF" $$
    if [[ -f "$request" && ! -L "$request" ]] && cmp -s "$request_snapshot" "$request"; then rm -f "$request"; fi
    mv -Tf "$request_snapshot" "$last_request"
    request_snapshot=
    secure_file "$audit_log" 'deployer audit log'
    [[ $(stat -Lc '%d:%i' /proc/self/fd/8) == $(stat -c '%d:%i' "$audit_log") ]] || die 'deployer audit log changed during deployment'
    printf 'time=%s environment=%s target=%s source=%s artifact=%s approval=%s approver=%s policy=%s checkpoint=%s authorized_by=%s gate=%s result=success\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${req[ENVIRONMENT]}" "${req[TARGET_ID]}" \
      "${req[SOURCE_COMMIT]}" "${req[ARTIFACT_IMAGE]#*@}" "${req[APPROVAL_ID]}" "${req[APPROVAL_IDENTITY]}" "${req[POLICY_IDENTITY]}" \
      "${checkpoint[CHECKPOINT_ID]:-none}" "${production[AUTHORIZED_BY]:-none}" "${production[GATE_ID]:-none}" \
      >&8
    sync -f "$audit_log" 2>/dev/null || sync "$audit_log" 2>/dev/null || die 'deployment success audit is not durable'
    # An adapter that deleted its consumption marker must not weaken replay
    # protection on success either; ensure the durable marker before the exit
    # guard is disabled.
    if [[ ! -e "$consumed_marker" ]]; then
      [[ -e "$consumed_root" ]] || install -d -m 0700 "$consumed_root"
      install -m 0600 /dev/null "$consumed_marker" || die 'deployment request consumption marker failed'
      sync -f "$consumed_marker" 2>/dev/null || sync "$consumed_marker" 2>/dev/null || die 'deployment request consumption marker is not durable'
      sync -f "$consumed_root" 2>/dev/null || sync "$consumed_root" 2>/dev/null || die 'deployment request consumption is not durable'
    fi
    # All fallible commit work is done; restore the retained pair if the
    # adapter touched it, then retire the incumbent snapshot.
    if [[ ${lkg_backed_up:-0} == 1 ]]; then
      restore_encoded_file "$state_root/last-known-good.json" "$lkg_state_backup" || die 'retained rollback state restoration failed'
      restore_encoded_file "$state_root/last-known-good-policy.conf" "$lkg_policy_backup" || die 'retained rollback policy restoration failed'
      sync -f "$state_root" 2>/dev/null || die 'retained rollback pair restoration is not durable'
      lkg_backed_up=0
    fi
    if [[ -n "$retired_snapshot" && -d "$deployed_root/$retired_snapshot" && ! -L "$deployed_root/$retired_snapshot" ]]; then rm -rf -- "${deployed_root:?}/$retired_snapshot"; fi
    sync -f "$deployed_root" 2>/dev/null || sync "$deployed_root" 2>/dev/null || die 'retired deployed snapshot is not durable'
    audit_pending=0
    ;;
esac
