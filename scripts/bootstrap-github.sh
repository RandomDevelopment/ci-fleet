#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

usage() {
  cat <<'EOF'
Usage: bootstrap-github.sh [--dry-run|--check] --organization ORG --instance ID
  --config-repository ORG/PRIVATE_CONFIG_REPO --runner-group GROUP
  --allow-repository ORG/PROJECT=NUMERIC_ID [--allow-repository ORG/PROJECT=NUMERIC_ID ...]
  [--bind ADDRESS --callback-host HOST --port PORT --timeout SECONDS]
  [--install --config-repo OWNER/REPO_OR_PATH --config-ref COMMIT]
EOF
}
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
note() { printf '%s\n' "$*"; }

mode=live; organization=; instance=; runner_group=; config_repository=; bind=127.0.0.1; callback_host=127.0.0.1
port=8765; timeout=600; run_installer=false; config_repo=; config_ref=
repositories=()
while (($#)); do
  case $1 in
    --dry-run) mode=dry-run; shift ;;
    --check) mode=check; shift ;;
    --organization) (($# >= 2)) || die '--organization requires a value'; organization=$2; shift 2 ;;
    --instance) (($# >= 2)) || die '--instance requires a value'; instance=$2; shift 2 ;;
    --runner-group) (($# >= 2)) || die '--runner-group requires a value'; runner_group=$2; shift 2 ;;
    --config-repository) (($# >= 2)) || die '--config-repository requires a value'; config_repository=$2; shift 2 ;;
    --allow-repository) (($# >= 2)) || die '--allow-repository requires a value'; repositories+=("$2"); shift 2 ;;
    --bind) (($# >= 2)) || die '--bind requires a value'; bind=$2; shift 2 ;;
    --callback-host) (($# >= 2)) || die '--callback-host requires a value'; callback_host=$2; shift 2 ;;
    --port) (($# >= 2)) || die '--port requires a value'; port=$2; shift 2 ;;
    --timeout) (($# >= 2)) || die '--timeout requires a value'; timeout=$2; shift 2 ;;
    --install) run_installer=true; shift ;;
    --config-repo) (($# >= 2)) || die '--config-repo requires a value'; config_repo=$2; shift 2 ;;
    --config-ref) (($# >= 2)) || die '--config-ref requires a value'; config_ref=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "unknown argument: $1" ;;
  esac
done
[[ $organization =~ ^[a-z0-9][a-z0-9-]{0,38}$ ]] || die 'organization must be lowercase'
[[ $instance =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || die 'invalid instance identity'
[[ $runner_group =~ ^[a-z0-9][a-z0-9-]{0,62}$ && $runner_group != default ]] || die 'runner group must be a lowercase schema-v3 slug and non-default'
[[ $config_repository =~ ^${organization}/[A-Za-z0-9_.-]+$ ]] || die "configuration repository must belong to $organization"
app_name=ci-fleet-$organization-$instance
((${#app_name} <= 34)) || die 'generated GitHub App name exceeds 34 characters'
((${#repositories[@]} > 0)) || die 'at least one --allow-repository is required'
[[ $port =~ ^[0-9]+$ && $port -ge 1 && $port -le 65535 ]] || die 'invalid callback port'
[[ $timeout =~ ^[0-9]+$ && $timeout -ge 30 && $timeout -le 1800 ]] || die 'timeout must be 30-1800 seconds'
for repository in "${repositories[@]}"; do
  [[ $repository =~ ^${organization}/[A-Za-z0-9_.-]+=([1-9][0-9]*)$ ]] || die "project repository must use ORG/REPO=NUMERIC_ID: $repository"
done
mapfile -t repositories < <(printf '%s\n' "${repositories[@]}" | LC_ALL=C sort -u)
((${#repositories[@]} > 0)) || die 'repository allowlist is empty'
repository_names=(); repository_id_values=()
for repository in "${repositories[@]}"; do repository_names+=("${repository%=*}"); repository_id_values+=("${repository##*=}"); done
if $run_installer; then
  [[ $mode == live ]] || die '--install is available only in live mode'
  [[ -n $config_repo && $config_ref =~ ^[0-9a-f]{40}$ ]] || die '--install requires --config-repo and a 40-character --config-ref'
fi
for command in bash curl openssl python3 install mktemp stat; do command -v "$command" >/dev/null || die "required command is unavailable: $command"; done
if $run_installer; then
  command -v git >/dev/null || die 'required command is unavailable: git'
  if git -C "$config_repo" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    [[ $(git -C "$config_repo" rev-parse "$config_ref^{commit}" 2>/dev/null || true) == "$config_ref" ]] || die 'local configuration repository does not contain --config-ref'
  else
    [[ ${config_repo,,} == "${config_repository,,}" ]] || die '--config-repo must match --config-repository unless it is a pinned local checkout'
  fi
fi

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
root_prefix=${CI_FLEET_ROOT_PREFIX:-}
if [[ -n $root_prefix && ${CI_FLEET_TESTING:-0} != 1 ]]; then die 'CI_FLEET_ROOT_PREFIX is test-only'; fi
root_path() { printf '%s%s' "$root_prefix" "$1"; }
etc_dir=$(root_path /etc/ci-fleet)
secret_dir=$etc_dir/secrets
pem=$secret_dir/github-app.pem
host_env=$etc_dir/host.env
bootstrap_state=$etc_dir/bootstrap-app.env
bootstrap_recovery=$etc_dir/bootstrap-recovery.json
bootstrap_pending=$etc_dir/bootstrap-recovery.pending
temporary=$(mktemp -d)
callback_pid=
expected_uid=0
[[ ${CI_FLEET_TESTING:-0} != 1 ]] || expected_uid=$(id -u)
cleanup() {
  local status=$?
  trap - EXIT INT TERM HUP
  [[ -z $callback_pid ]] || kill "$callback_pid" >/dev/null 2>&1 || true
  rm -rf "$temporary"
  exit "$status"
}
trap cleanup EXIT INT TERM HUP

if [[ $mode != dry-run && ${CI_FLEET_TESTING:-0} != 1 && ${EUID:-$(id -u)} -ne 0 ]]; then die 'run live/check bootstrap as root'; fi
[[ $bind == 127.0.0.1 && $callback_host == 127.0.0.1 ]] || die 'callback bind/host must both be IPv4 loopback 127.0.0.1'

write_auth_config() {
  local credential=$1 method=$2 url=$3 output=$4 payload=${5:-}
  local config=$temporary/curl.$RANDOM.conf
  python3 - "$credential" "$method" "$url" "$output" "$payload" "$config" <<'PY'
from pathlib import Path
import sys
credential, method, url, output, payload, target = sys.argv[1:]
token = Path(credential).read_text().strip()
lines = ['silent', 'show-error', 'fail-with-body', f'request = "{method}"', f'url = "{url}"',
         f'output = "{output}"', 'header = "Accept: application/vnd.github+json"',
         'header = "X-GitHub-Api-Version: 2022-11-28"', f'header = "Authorization: Bearer {token}"']
if payload:
    lines += ['header = "Content-Type: application/json"', f'data-binary = "@{payload}"']
Path(target).write_text('\n'.join(lines) + '\n')
PY
  chmod 600 "$config"
  local status=0
  curl --config "$config" || status=$?
  rm -f "$config"
  return "$status"
}

make_app_jwt() {
  local app_id=$1 key=$2 output=$3 now header payload
  now=$(date +%s)
  header=$(printf '%s' '{"alg":"RS256","typ":"JWT"}' | openssl base64 -A | tr '+/' '-_' | tr -d '=')
  payload=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' "$((now - 60))" "$((now + 540))" "$app_id" | openssl base64 -A | tr '+/' '-_' | tr -d '=')
  printf '%s.%s' "$header" "$payload" >"$temporary/jwt-input"
  openssl dgst -sha256 -sign "$key" "$temporary/jwt-input" | openssl base64 -A | tr '+/' '-_' | tr -d '=' >"$temporary/jwt-signature"
  printf '%s.%s\n' "$(<"$temporary/jwt-input")" "$(<"$temporary/jwt-signature")" >"$output"
  chmod 600 "$output"
}

load_metadata() {
  if [[ -f $bootstrap_state && ! -L $bootstrap_state && $(stat -c %a "$bootstrap_state") == 600 && $(stat -c %u "$bootstrap_state") == "$expected_uid" ]]; then
    # shellcheck disable=SC1090
    . "$bootstrap_state"
    [[ ${CI_FLEET_BOOTSTRAP_ORGANIZATION:-} == "$organization" && ${CI_FLEET_BOOTSTRAP_INSTANCE:-} == "$instance" ]] || die 'existing bootstrap state belongs to another identity'
    if [[ -e $host_env || -L $host_env ]]; then
      [[ -f $host_env && ! -L $host_env && $(stat -c %a "$host_env") == 600 && $(stat -c %u "$host_env") == "$expected_uid" ]] || die 'existing host configuration is invalid'
      # shellcheck disable=SC1090
      . "$host_env"
    fi
  else
    return 1
  fi
  [[ ${CI_FLEET_GITHUB_APP_ID:-} =~ ^([0-9]+|(Iv1\.)?[A-Za-z0-9]+)$ && ${CI_FLEET_GITHUB_APP_CLIENT_ID:-} =~ ^(Iv1\.)?[A-Za-z0-9]+$ && ${CI_FLEET_GITHUB_APP_SLUG:-} =~ ^[a-z0-9-]+$ ]] || die 'existing bootstrap state is invalid'
  [[ ${CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE:-/etc/ci-fleet/secrets/github-app.pem} == /etc/ci-fleet/secrets/github-app.pem ]] || die 'existing bootstrap PEM path is unsupported'
  [[ -f $pem && ! -L $pem && $(stat -c %a "$pem") == 600 && $(stat -c %u "$pem") == "$expected_uid" ]] || die 'existing bootstrap PEM is invalid'
}

recover_conversion() {
  [[ -f $bootstrap_recovery && ! -L $bootstrap_recovery && $(stat -c %a "$bootstrap_recovery") == 600 && $(stat -c %u "$bootstrap_recovery") == "$expected_uid" ]] || die 'bootstrap recovery record is invalid'
  local staged_pem=$temporary/recovered.pem staged_state=$temporary/recovered.env
  python3 - "$bootstrap_recovery" "$staged_pem" "$staged_state" "$organization" "$instance" <<'PY' || die 'bootstrap recovery record is malformed'
import json, os, re, sys
from pathlib import Path
source, pem, state = map(Path, sys.argv[1:4]); organization, instance = sys.argv[4:]
value=json.loads(source.read_text())
if not isinstance(value.get('pem'),str) or 'PRIVATE KEY' not in value['pem']: raise SystemExit(1)
if not isinstance(value.get('id'),int) or not re.fullmatch(r'(Iv1\.)?[A-Za-z0-9]+',str(value.get('client_id',''))): raise SystemExit(1)
slug=str(value.get('slug',''))
if slug != f'ci-fleet-{organization}-{instance}' or not re.fullmatch(r'[a-z0-9-]+',slug): raise SystemExit(1)
pem.write_text(value['pem']); os.chmod(pem,0o600)
state.write_text(f'CI_FLEET_BOOTSTRAP_ORGANIZATION={organization}\nCI_FLEET_BOOTSTRAP_INSTANCE={instance}\nCI_FLEET_GITHUB_APP_ID={value["id"]}\nCI_FLEET_GITHUB_APP_CLIENT_ID={value["client_id"]}\nCI_FLEET_GITHUB_APP_SLUG={slug}\n'); os.chmod(state,0o600)
PY
  install -m 0600 "$staged_pem" "$pem"
  install -m 0600 "$staged_state" "$bootstrap_state"
  load_metadata
  rm -f "$bootstrap_recovery"
}

if [[ $mode == dry-run ]]; then
  note "DRY_RUN organization=$organization instance=$instance runner_group=$runner_group repositories=${#repositories[@]}"
  note "CALLBACK_URL http://$callback_host:$port/"
  note 'NO_GITHUB_MUTATION credentials_written=false installer_run=false'
  exit 0
fi

for directory in "$etc_dir" "$secret_dir"; do
  if [[ -e $directory || -L $directory ]]; then
    [[ -d $directory && ! -L $directory && $(stat -c %a "$directory") == 700 && $(stat -c %u "$directory") == "$expected_uid" ]] || die "protected directory is unsafe: $directory"
  else
    install -d -m 0700 "$directory"
  fi
done
if [[ -f $bootstrap_pending && ! -L $bootstrap_pending && $(stat -c %u "$bootstrap_pending") == "$expected_uid" && $(stat -c %a "$bootstrap_pending") == 600 ]]; then
  if python3 - "$bootstrap_pending" <<'PY'
import json,sys
value=json.load(open(sys.argv[1]))
if any(not value.get(key) for key in ('id','client_id','pem','slug')): raise SystemExit(1)
PY
  then mv -fT "$bootstrap_pending" "$bootstrap_recovery"
  else die "incomplete protected conversion response remains at $bootstrap_pending; do not retry or delete it without owner recovery/abandonment approval"
  fi
elif [[ -e $bootstrap_pending || -L $bootstrap_pending ]]; then
  die 'pending conversion response has unsafe ownership, mode, or type'
fi
if [[ -e $bootstrap_recovery || -L $bootstrap_recovery ]]; then
  [[ $mode != check ]] || die 'bootstrap credential recovery is pending; rerun live bootstrap first'
  recover_conversion
fi
if ! load_metadata; then
  [[ $mode != check ]] || die 'bootstrap state is missing; run live bootstrap first'
  [[ ! -e $pem && ! -L $pem && ! -e $host_env && ! -L $host_env ]] || die 'existing credentials lack matching bootstrap identity; no replacement made'
  state=$(openssl rand -hex 32)
  callback_url="http://$callback_host:$port/callback"
  manifest=$(python3 - "$organization" "$instance" "$callback_url" <<'PY'
import json, sys
organization, instance, callback = sys.argv[1:]
print(json.dumps({
  'name': f'ci-fleet-{organization}-{instance}', 'url': 'https://github.com/RandomDevelopment/ci-fleet',
  'redirect_url': callback, 'public': False, 'hook_attributes': {'active': False, 'url': callback},
  'default_permissions': {'contents': 'read', 'metadata': 'read', 'organization_self_hosted_runners': 'write'},
  'default_events': []
}, separators=(',', ':')))
PY
)
  code_file=$temporary/manifest-code
  handoff_file=$temporary/installation-url
  python3 "$script_dir/bootstrap-github-callback.py" --bind "$bind" --port "$port" --organization "$organization" --state "$state" --manifest "$manifest" --output "$code_file" --handoff "$handoff_file" --timeout "$timeout" &
  callback_pid=$!
  note "REGISTRATION_URL http://$callback_host:$port/"
  note 'Open the URL, authenticate to GitHub, and approve App creation. Return here without copying any value.'
  deadline=$((SECONDS + timeout))
  while [[ ! -f $code_file && $SECONDS -lt $deadline ]]; do
    kill -0 "$callback_pid" 2>/dev/null || die 'registration callback stopped before receiving approval'
    sleep 1
  done
  [[ -f $code_file ]] || die 'registration callback timed out'
  conversion_config=$temporary/conversion.conf
  python3 - "$code_file" "$bootstrap_pending" "$conversion_config" <<'PY'
from pathlib import Path
import sys
code_file, output, config = map(Path, sys.argv[1:])
code = code_file.read_text().strip()
if not code or len(code) > 512 or any(ch.isspace() for ch in code): raise SystemExit(1)
config.write_text('\n'.join(['silent','show-error','fail-with-body','request = "POST"',
 f'url = "https://api.github.com/app-manifests/{code}/conversions"', f'output = "{output}"',
 'header = "Accept: application/vnd.github+json"','header = "X-GitHub-Api-Version: 2022-11-28"']) + '\n')
PY
  chmod 600 "$conversion_config"
  conversion_status=0
  curl --config "$conversion_config" || conversion_status=$?
  chmod 600 "$bootstrap_pending"
  rm -f "$code_file" "$conversion_config"
  if python3 - "$bootstrap_pending" <<'PY'
import json,sys
value=json.load(open(sys.argv[1]))
if any(not value.get(key) for key in ('id','client_id','pem','slug')): raise SystemExit(1)
PY
  then mv -fT "$bootstrap_pending" "$bootstrap_recovery"
  else die "manifest conversion did not produce a complete response (curl status $conversion_status); protected pending bytes retained for owner recovery"
  fi
  if [[ ${CI_FLEET_TEST_FAIL_AFTER_CONVERSION:-0} == 1 && ${CI_FLEET_TESTING:-0} == 1 ]]; then die 'injected failure after manifest conversion'; fi
  recover_conversion
fi

app_jwt=$temporary/app.jwt
make_app_jwt "$CI_FLEET_GITHUB_APP_ID" "$pem" "$app_jwt"
app_response=$temporary/app.json
write_auth_config "$app_jwt" GET https://api.github.com/app "$app_response"
mapfile -t app_data < <(python3 - "$app_response" "$organization" "$CI_FLEET_GITHUB_APP_SLUG" "$CI_FLEET_GITHUB_APP_CLIENT_ID" <<'PY'
import json,sys
v=json.load(open(sys.argv[1])); owner=v.get('owner',{}); expected=sys.argv[3]
if (expected and v.get('slug') != expected) or v.get('client_id') != sys.argv[4] or owner.get('login','').lower() != sys.argv[2].lower() or owner.get('type') != 'Organization': raise SystemExit(1)
permissions=v.get('permissions',{})
expected_permissions={'contents':'read','metadata':'read','organization_self_hosted_runners':'write'}
if permissions != expected_permissions or v.get('events') not in ([], None) or v.get('public') is not False: raise SystemExit(1)
print(v['slug']); print(v['id'])
PY
) || die 'existing App identity or ownership is unexpected'
((${#app_data[@]} == 2)) || die 'existing App response is incomplete'
CI_FLEET_GITHUB_APP_SLUG=${app_data[0]}
verified_app_id=${app_data[1]}
rm -f "$app_response"
if [[ -n ${handoff_file:-} ]]; then
  printf 'https://github.com/apps/%s/installations/new\n' "$CI_FLEET_GITHUB_APP_SLUG" >"$handoff_file"
fi

installation_id=${CI_FLEET_GITHUB_APP_INSTALLATION_ID:-}
if [[ -z $installation_id ]]; then
  note "INSTALLATION_URL https://github.com/apps/$CI_FLEET_GITHUB_APP_SLUG/installations/new"
  note 'Install the App for only the listed private repositories, then return here; polling continues automatically.'
  deadline=$((SECONDS + timeout))
  while ((SECONDS < deadline)); do
    installations=$temporary/installations.json
    make_app_jwt "$CI_FLEET_GITHUB_APP_ID" "$pem" "$app_jwt"
    if write_auth_config "$app_jwt" GET https://api.github.com/app/installations "$installations"; then
      installation_id=$(python3 - "$installations" "$organization" <<'PY'
import json,sys
for value in json.load(open(sys.argv[1])):
    account=value.get('account',{})
    if value.get('repository_selection')=='selected' and account.get('type')=='Organization' and account.get('login','').lower()==sys.argv[2].lower():
        print(value['id']); break
PY
)
    fi
    rm -f "$installations"
    [[ $installation_id =~ ^[0-9]+$ ]] && break
    sleep 5
  done
  [[ $installation_id =~ ^[0-9]+$ ]] || die 'GitHub App installation was not observed before timeout'
fi

make_app_jwt "$CI_FLEET_GITHUB_APP_ID" "$pem" "$app_jwt"
installations=$temporary/installations.json
write_auth_config "$app_jwt" GET https://api.github.com/app/installations "$installations"
python3 - "$installations" "$organization" "$installation_id" <<'PY' || die 'App installation identity or repository selection is unexpected'
import json,sys
matches=[]
for value in json.load(open(sys.argv[1])):
    account=value.get('account',{})
    if str(value.get('id'))==sys.argv[3] and account.get('type')=='Organization' and account.get('login','').lower()==sys.argv[2].lower() and value.get('repository_selection')=='selected': matches.append(value)
if len(matches)!=1: raise SystemExit(1)
PY
rm -f "$installations"

installation_response=$temporary/installation-token.json
empty_payload=$temporary/empty.json
printf '{}\n' >"$empty_payload"
make_app_jwt "$CI_FLEET_GITHUB_APP_ID" "$pem" "$app_jwt"
write_auth_config "$app_jwt" POST "https://api.github.com/app/installations/$installation_id/access_tokens" "$installation_response" "$empty_payload"
installation_token=$temporary/installation.token
python3 - "$installation_response" "$installation_token" <<'PY'
import json,os,sys
from pathlib import Path
value=json.load(open(sys.argv[1])); token=value.get('token')
if not isinstance(token,str) or len(token)<20 or value.get('permissions',{}).get('contents') != 'read': raise SystemExit(1)
Path(sys.argv[2]).write_text(token); os.chmod(sys.argv[2],0o600)
PY
rm -f "$installation_response"

repository_ids=$temporary/repository-ids
printf '%s\n' "${repository_id_values[@]}" | sort -n -u >"$repository_ids"
(($(wc -l <"$repository_ids") == ${#repository_id_values[@]})) || die 'duplicate project repository IDs are forbidden'

visible=$temporary/visible.json
write_auth_config "$installation_token" GET 'https://api.github.com/installation/repositories?per_page=100' "$visible"
python3 - "$visible" "$config_repository" <<'PY' || die 'App installation must select only the private configuration repository'
import json,sys
value=json.load(open(sys.argv[1])); repositories=value.get('repositories',[])
if value.get('total_count') != len(repositories): raise SystemExit(1)
actual=[v for v in repositories if v.get('full_name','').lower()==sys.argv[2].lower() and v.get('private') and not v.get('archived')]
if len(repositories)!=1 or len(actual)!=1: raise SystemExit(1)
PY
rm -f "$visible"

groups=$temporary/groups.json
write_auth_config "$installation_token" GET "https://api.github.com/orgs/$organization/actions/runner-groups?per_page=100" "$groups"
group_id=$(python3 - "$groups" "$runner_group" <<'PY'
import json,sys
response=json.load(open(sys.argv[1])); groups=response.get('runner_groups',[])
if response.get('total_count') != len(groups): raise SystemExit(1)
for value in groups:
    if value.get('name')==sys.argv[2]: print(value['id']); break
PY
)
rm -f "$groups"
created_group=false
if [[ -z $group_id ]]; then
  [[ $mode != check ]] || die 'runner group is missing'
  payload=$temporary/group-create.json
  python3 - "$payload" "$runner_group" "$repository_ids" <<'PY'
import json,sys
from pathlib import Path
ids=[int(v) for v in Path(sys.argv[3]).read_text().splitlines()]
Path(sys.argv[1]).write_text(json.dumps({'name':sys.argv[2],'visibility':'selected','allows_public_repositories':False,'restricted_to_workflows':False,'selected_repository_ids':ids}))
PY
  response=$temporary/group.json
  write_auth_config "$installation_token" POST "https://api.github.com/orgs/$organization/actions/runner-groups" "$response" "$payload"
  group_id=$(python3 - "$response" <<'PY'
import json,sys
print(json.load(open(sys.argv[1]))['id'])
PY
)
  created_group=true
  rm -f "$payload" "$response"
fi
[[ $group_id =~ ^[0-9]+$ ]] || die 'runner group ID is invalid'
rollback_new_group() {
  $created_group || return 0
  local deleted=$temporary/group-delete.json
  write_auth_config "$installation_token" DELETE "https://api.github.com/orgs/$organization/actions/runner-groups/$group_id" "$deleted" || return 1
  rm -f "$deleted"
  created_group=false
}
response=$temporary/group.json
if ! write_auth_config "$installation_token" GET "https://api.github.com/orgs/$organization/actions/runner-groups/$group_id" "$response"; then
  if $created_group; then rollback_new_group || die 'new runner group inspection and rollback both failed'; die 'runner group inspection failed; new group was rolled back'; fi
  die 'existing runner group inspection failed; no change made'
fi
if ! python3 - "$response" "$runner_group" <<'PY'
import json,sys
v=json.load(open(sys.argv[1]))
if v.get('name')!=sys.argv[2] or v.get('visibility')!='selected' or v.get('default') or v.get('allows_public_repositories') is not False or v.get('restricted_to_workflows') is not False: raise SystemExit(1)
PY
then
  if $created_group; then rollback_new_group || die 'new runner group failed identity verification and rollback failed'; die 'new runner group failed identity verification and was rolled back'; fi
  die 'existing runner group is broader or has unexpected identity; no change made'
fi
rm -f "$response"
selected=$temporary/group-repositories.json
if ! write_auth_config "$installation_token" GET "https://api.github.com/orgs/$organization/actions/runner-groups/$group_id/repositories?per_page=100" "$selected"; then
  if $created_group; then rollback_new_group || die 'new runner group repository inspection and rollback both failed'; die 'runner group repository inspection failed; new group was rolled back'; fi
  die 'existing runner group repository inspection failed; no change made'
fi
if ! python3 - "$selected" "$repository_ids" "${repository_names[@]}" <<'PY'
import json,sys
from pathlib import Path
response=json.load(open(sys.argv[1])); repositories=response.get('repositories',[])
if response.get('total_count') != len(repositories): raise SystemExit(1)
actual=sorted(v['id'] for v in repositories); expected=sorted(int(v) for v in Path(sys.argv[2]).read_text().splitlines())
actual_names=sorted(v['full_name'].lower() for v in repositories); expected_names=sorted(v.lower() for v in sys.argv[3:])
if actual!=expected or actual_names!=expected_names: raise SystemExit(1)
PY
then
  if $created_group; then
    rollback_new_group || die 'new runner group failed verification and rollback failed'
    die 'new runner group repository access differed and was rolled back'
  fi
  die 'existing runner group repository access differs; no change made'
fi
rm -f "$selected"

staged=$temporary/host.env
printf '%s\n' "CI_FLEET_GITHUB_APP_CLIENT_ID=$CI_FLEET_GITHUB_APP_CLIENT_ID" "CI_FLEET_GITHUB_APP_INSTALLATION_ID=$installation_id" "CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE=/etc/ci-fleet/secrets/github-app.pem" 'CI_FLEET_RUNNER_TTL=6h' >"$staged"
if [[ $mode == check ]]; then
  cmp -s "$staged" "$host_env" || die 'host configuration differs from verified GitHub state'
else
  install -m 0600 "$staged" "$host_env"
fi
note "BOOTSTRAP_OK organization=$organization instance=$instance app=$CI_FLEET_GITHUB_APP_SLUG app_id=$verified_app_id installation_id=$installation_id runner_group=$runner_group runner_group_id=$group_id repositories=${#repositories[@]}"
if [[ $mode == check ]]; then note "CREDENTIALS_VERIFIED pem=$pem host_config=$host_env"; else note "CREDENTIALS_WRITTEN pem=$pem host_config=$host_env"; fi
if $run_installer; then
  "$script_dir/install-worker-controller.sh" --install --config-repo "$config_repo" --controller "$instance" --ref "$config_ref"
else
  note "NEXT sudo $script_dir/install-worker-controller.sh --install --config-repo OWNER/REPO --controller $instance --ref REVIEWED_COMMIT"
fi
