#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

root_prefix=${CI_FLEET_ROOT_PREFIX:-}
[[ -z $root_prefix || ${CI_FLEET_TESTING:-0} == 1 ]] || { printf 'ERROR: CI_FLEET_ROOT_PREFIX is test-only\n' >&2; exit 1; }
root_path() { printf '%s%s' "$root_prefix" "$1"; }
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source_revision=unknown
[[ ! -f $script_dir/../.ci-fleet-source-revision ]] || source_revision=$(<"$script_dir/../.ci-fleet-source-revision")
config_file=$(root_path /etc/ci-fleet-tester/tester.env)
environment_dir=$(root_path /etc/ci-fleet-tester/environments)
definition_dir=$(root_path /etc/ci-fleet-tester/definitions)
secret_root=$(root_path /etc/ci-fleet-tester/secrets)
state_dir=$(root_path /var/lib/ci-fleet-tester/environments)
lock_file=$(root_path /run/lock/ci-fleet-tester/runtime.lock)
expected_uid=0
[[ ${CI_FLEET_TESTING:-0} != 1 ]] || expected_uid=$(id -u)

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
report() { printf '%s\n' "$*"; }
usage() { printf 'Usage: tester-runtime.sh {--check|--converge|--reset|--remove|--inspect|--cleanup|--health} [--environment ID]\n'; }

action=; environment=
while (($#)); do
  case $1 in
    --check|--cleanup|--health) [[ -z $action ]] || die 'choose one action'; action=$1; shift ;;
    --converge|--reset|--remove|--inspect) [[ -z $action ]] || die 'choose one action'; action=$1; shift ;;
    --environment) (($# >= 2)) || die '--environment requires a value'; environment=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "unknown argument: $1" ;;
  esac
done
[[ -n $action ]] || { usage; exit 2; }
case $action in --converge|--reset|--remove|--inspect) [[ $environment =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || die 'environment ID is invalid' ;; *) [[ -z $environment ]] || die '--environment is not valid for this action' ;; esac
required_commands='awk basename chmod cmp date df dirname docker du env find flock grep install mkdir mktemp mv python3 readlink rm stat wc'
case $action in --check|--health) required_commands+=' curl getent' ;; esac
for command in $required_commands; do command -v "$command" >/dev/null || die "required command is unavailable: $command"; done

secure_directory() {
  local path=$1 mode=$2
  [[ -d $path && ! -L $path && $(stat -c %u "$path") == "$expected_uid" && $(stat -c %a "$path") == "$mode" ]] || die "protected directory is unsafe: $path"
}
secure_file() {
  local path=$1 mode=$2
  [[ -f $path && ! -L $path && $(stat -c %u "$path") == "$expected_uid" && $(stat -c %a "$path") == "$mode" ]] || die "protected file is unsafe: $path"
}
load_exact_env() {
  local file=$1 allowed=$2 line key value
  declare -gA ENV_VALUES=()
  while IFS= read -r line || [[ -n $line ]]; do
    [[ -z $line || $line == \#* ]] && continue
    [[ $line =~ ^([A-Z][A-Z0-9_]*)=(.*)$ ]] || die "invalid configuration line in $file"
    key=${BASH_REMATCH[1]}; value=${BASH_REMATCH[2]}
    [[ " $allowed " == *" $key "* ]] || die "unsupported configuration key: $key"
    [[ -z ${ENV_VALUES[$key]+x} ]] || die "duplicate configuration key: $key"
    [[ $value != *$'\n'* && $value != *$'\r'* ]] || die "invalid configuration value: $key"
    ENV_VALUES[$key]=$value
  done <"$file"
}

load_global() {
  secure_directory "$(dirname "$config_file")" 700
  secure_directory "$environment_dir" 700
  secure_directory "$definition_dir" 700
  secure_directory "$secret_root" 700
  secure_directory "$(dirname "$state_dir")" 700
  secure_directory "$state_dir" 700
  secure_file "$config_file" 600
  load_exact_env "$config_file" 'CI_FLEET_TESTER_DEFAULT_TTL_SECONDS CI_FLEET_TESTER_MAX_ENVIRONMENTS CI_FLEET_TESTER_DISK_WARN_PERCENT CI_FLEET_TESTER_NETWORK_PROBE_HOST CI_FLEET_TESTER_HTTPS_PROBE_URL CI_FLEET_TESTER_ISOLATION_ACK'
  default_ttl=${ENV_VALUES[CI_FLEET_TESTER_DEFAULT_TTL_SECONDS]:-86400}
  max_environments=${ENV_VALUES[CI_FLEET_TESTER_MAX_ENVIRONMENTS]:-20}
  disk_warn=${ENV_VALUES[CI_FLEET_TESTER_DISK_WARN_PERCENT]:-80}
  probe_host=${ENV_VALUES[CI_FLEET_TESTER_NETWORK_PROBE_HOST]:-}
  probe_url=${ENV_VALUES[CI_FLEET_TESTER_HTTPS_PROBE_URL]:-}
  [[ $default_ttl =~ ^[0-9]+$ && $default_ttl -ge 300 && $default_ttl -le 604800 ]] || die 'default TTL must be 300-604800 seconds'
  [[ $max_environments =~ ^[0-9]+$ && $max_environments -ge 1 && $max_environments -le 100 ]] || die 'max environments must be 1-100'
  [[ $disk_warn =~ ^[0-9]+$ && $disk_warn -ge 50 && $disk_warn -le 95 ]] || die 'disk warning threshold must be 50-95'
  [[ $probe_host =~ ^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$ && $probe_url =~ ^https://[A-Za-z0-9][A-Za-z0-9.-]{0,252}(:[0-9]+)?/[^[:space:]]*$ ]] || die 'DNS and HTTPS probe targets are required'
  [[ ${ENV_VALUES[CI_FLEET_TESTER_ISOLATION_ACK]:-} == test-only-no-production-authority ]] || die 'explicit test-only isolation acknowledgement is required'
}

check_disk_threshold() {
  used=$(df -P "$(root_path /var/lib/docker)" | awk 'NR==2{gsub(/%/,"",$5);print $5}')
  [[ $used =~ ^[0-9]+$ && $used -lt $disk_warn ]] || die 'Docker storage exceeds configured warning threshold'
}

check_network_probes() {
  getent ahosts "$probe_host" >/dev/null || die 'test-host DNS probe failed'
  curl --fail --silent --show-error --head --max-time 10 --output /dev/null "$probe_url" || die 'test-host HTTPS/proxy probe failed'
}

project_name() { printf 'ci-fleet-test-%s' "$1"; }
state_path() { printf '%s/%s.state' "$state_dir" "$1"; }
deployed_compose_path() { printf '%s/%s.compose.json' "$state_dir" "$1"; }
spec_path() { printf '%s/%s.env' "$environment_dir" "$1"; }

load_spec() {
  local id=$1 canonical
  spec=$(spec_path "$id")
  secure_file "$spec" 600
  load_exact_env "$spec" 'CI_FLEET_TESTER_PROJECT CI_FLEET_TESTER_OWNER CI_FLEET_TESTER_COMPOSE_FILE CI_FLEET_TESTER_EXPIRES_AT CI_FLEET_TESTER_ROUTE_SERVICE CI_FLEET_TESTER_ROUTE_PORT'
  project=${ENV_VALUES[CI_FLEET_TESTER_PROJECT]:-}
  owner=${ENV_VALUES[CI_FLEET_TESTER_OWNER]:-}
  compose_file=${ENV_VALUES[CI_FLEET_TESTER_COMPOSE_FILE]:-}
  expires_at=${ENV_VALUES[CI_FLEET_TESTER_EXPIRES_AT]:-}
  route_service=${ENV_VALUES[CI_FLEET_TESTER_ROUTE_SERVICE]:-}
  route_port=${ENV_VALUES[CI_FLEET_TESTER_ROUTE_PORT]:-}
  [[ $project =~ ^[a-z0-9][a-z0-9-]{0,62}$ && $owner =~ ^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$ ]] || die 'project or owner is invalid'
  [[ $route_service =~ ^[a-z0-9][a-z0-9-]{0,62}$ && $route_port =~ ^[0-9]+$ && $route_port -ge 1024 && $route_port -le 65535 ]] || die 'route service/port is invalid'
  if [[ -z $expires_at && -f $(state_path "$id") ]]; then expires_at=$(awk -F= '$1=="EXPIRES_AT"{print $2}' "$(state_path "$id")"); fi
  if [[ -z $expires_at ]]; then expires_at=$(($(date +%s) + default_ttl)); fi
  [[ $expires_at =~ ^[0-9]+$ && $expires_at -gt $(date +%s) && $expires_at -le $(($(date +%s) + 604800)) ]] || die 'expiration must be in the future and at most seven days away'
  canonical=$(readlink -f -- "$compose_file") || die 'compose file is unavailable'
  [[ $canonical == "$definition_dir"/* && $canonical == "$compose_file" ]] || die 'compose file must be a canonical file below the protected definitions directory'
  secure_file "$compose_file" 644
  compose_project=$(project_name "$id")
  secret_dir=$secret_root/$id
}

validate_compose() {
  local rendered=$1 empty_env variable
  local -a clean_environment=(env -i "PATH=$PATH" "DOCKER_HOST=$DOCKER_HOST")
  if ! python3 - "$compose_file" <<'PY'
import re,sys
text=open(sys.argv[1],encoding='utf-8').read()
key=r'(?:(?:!!str|!<[^>\n]+>)[ \t]+)?(?:include|label_file|"(?:include|label_file)"|\x27(?:include|label_file)\x27)[ \t]*:'
explicit_key=r'(?m)(?:^[ \t]*|[,{][ \t]*)\?'
escaped_key=r'"[^"\n]*\\[^"\n]*"[ \t]*:'
alias_key=r'(?m)(?:^[ \t]*|[,{][ \t]*)\*[A-Za-z0-9_-]+[ \t]*:'
anchor_key=r'(?m)(?:^[ \t]*|[,{][ \t]*)&[A-Za-z0-9_-]+[ \t]+'
if re.search(r'(?m)^[ \t]*'+key,text) or re.search(r'[,{][ \t]*'+key,text) or re.search(escaped_key,text) or re.search(explicit_key,text) or re.search(alias_key,text) or re.search(anchor_key,text): raise SystemExit('Compose include, label_file, or indirect mapping key is forbidden')
PY
  then return 1; fi
  if [[ ${CI_FLEET_TESTING:-0} == 1 ]]; then for variable in ${!FAKE_@}; do clean_environment+=("$variable=${!variable}"); done; fi
  empty_env=$(mktemp); chmod 600 "$empty_env"
  if ! "${clean_environment[@]}" docker compose --env-file "$empty_env" -p "$compose_project" -f "$compose_file" config --format json >"$rendered"; then rm -f "$empty_env"; return 1; fi
  rm -f "$empty_env"
  chmod 600 "$rendered"
  python3 - "$rendered" "$route_service" "$route_port" "$compose_project" "$secret_dir" "$expected_uid" <<'PY' || return 1
import json,math,os,re,stat,sys
value=json.load(open(sys.argv[1])); route_service=sys.argv[2]; route_port=int(sys.argv[3]); project=sys.argv[4]; secret_dir=sys.argv[5]; expected_uid=int(sys.argv[6])
services=value.get('services')
if not isinstance(services,dict) or route_service not in services: raise SystemExit('route service is missing')
image=re.compile(r'^[a-z0-9.-]+(?::[0-9]+)?/[A-Za-z0-9_./-]+@sha256:[0-9a-f]{64}$')
ports=[]
for name,service in services.items():
    if not image.fullmatch(str(service.get('image',''))): raise SystemExit(f'{name}: image must use an immutable sha256 digest')
    if service.get('privileged') or service.get('runtime') or service.get('network_mode') or service.get('pid') or service.get('ipc') or service.get('uts') or service.get('userns_mode') or service.get('cgroup') or service.get('external_links') or service.get('logging') or service.get('post_start') or service.get('pre_stop'): raise SystemExit(f'{name}: external runtime/namespace/link/logging/privileged lifecycle access is forbidden')
    deploy=service.get('deploy') or {}; reservations=(deploy.get('resources') or {}).get('reservations') or {}
    if service.get('build') or service.get('devices') or service.get('gpus') or reservations.get('devices') or service.get('cap_add') or service.get('container_name') or service.get('hostname') or service.get('use_api_socket') or service.get('volumes_from'): raise SystemExit(f'{name}: build/device/capability/external mount/global identity is forbidden')
    if service.get('scale',1) != 1 or deploy.get('replicas',1) != 1: raise SystemExit(f'{name}: exactly one replica is required')
    if not all(isinstance(service.get(key),(int,float)) and not isinstance(service[key],bool) and math.isfinite(service[key]) and service[key] > 0 for key in ('cpus','mem_limit','pids_limit')): raise SystemExit(f'{name}: positive CPU, memory, and PID limits are required')
    if service.get('oom_kill_disable') or service.get('oom_score_adj',0) < 0: raise SystemExit(f'{name}: OOM priority overrides are forbidden')
    if service.get('environment') or service.get('env_file') or service.get('configs'): raise SystemExit(f'{name}: alternate credential channels are forbidden')
    if service.get('profiles'): raise SystemExit(f'{name}: Compose profiles are forbidden')
    if service.get('label_file'): raise SystemExit(f'{name}: external label files are forbidden')
    if service.get('read_only') is not True or 'ALL' not in service.get('cap_drop',[]): raise SystemExit(f'{name}: read_only and cap_drop ALL are required')
    if service.get('security_opt') not in (['no-new-privileges:true'],['no-new-privileges=true']): raise SystemExit(f'{name}: no-new-privileges=true must be the only security option')
    for mount in service.get('volumes',[]):
        if isinstance(mount,str) or mount.get('type') not in ('volume','tmpfs'): raise SystemExit(f'{name}: host bind mounts are forbidden')
    for port in service.get('ports',[]):
        if not isinstance(port,dict) or str(port.get('host_ip','')) != '127.0.0.1' or port.get('protocol','tcp') != 'tcp': raise SystemExit(f'{name}: published ports must use TCP on loopback')
        ports.append((name,int(port.get('published',0)),int(port.get('target',0))))
if ports != [(route_service,route_port,ports[0][2] if ports else 0)] or not ports or ports[0][2] < 1: raise SystemExit('exactly one declared loopback route is required')
if value.get('configs'): raise SystemExit('top-level configs are forbidden')
for section in ('networks','volumes'):
    for name,item in value.get(section,{}).items():
        resolved=item.get('name',f'{project}_{name}')
        if item.get('external') or not resolved.startswith(f'{project}_'): raise SystemExit(f'{section}.{name}: external/unscoped names are forbidden')
        if section == 'networks' and (item.get('driver') not in (None,'bridge') or item.get('driver_opts')): raise SystemExit(f'{section}.{name}: custom network drivers are forbidden')
        if section == 'networks' and item.get('ipam'): raise SystemExit(f'{section}.{name}: custom network IPAM configuration is forbidden')
        if section == 'volumes' and (item.get('driver') or item.get('driver_opts')): raise SystemExit(f'{section}.{name}: custom volume drivers are forbidden')
secrets=value.get('secrets',{})
if secrets:
    try: metadata=os.lstat(secret_dir)
    except FileNotFoundError: raise SystemExit('environment secret directory is missing')
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != expected_uid or stat.S_IMODE(metadata.st_mode) != 0o700: raise SystemExit('environment secret directory must be owner-controlled mode 0700')
for name,item in secrets.items():
    path=item.get('file')
    if item.get('external') or not isinstance(path,str) or os.path.realpath(path).rsplit('/',1)[0] != secret_dir: raise SystemExit(f'secrets.{name}: secret must be a host-local file in the environment secret directory')
    metadata=os.lstat(path)
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_uid != expected_uid or stat.S_IMODE(metadata.st_mode) != 0o600: raise SystemExit(f'secrets.{name}: secret must be a singly linked owner-controlled mode-0600 file')
PY
  image_digests=$(python3 - "$rendered" <<'PY'
import json,sys
value=json.load(open(sys.argv[1]))
print(','.join(sorted({service['image'].rsplit('@',1)[1] for service in value['services'].values()})))
PY
)
}

validate_existing_resources() {
  python3 - "$1" "$compose_project" "$(state_path "$environment")" <<'PY'
import ipaddress,json,os,subprocess,sys
model=json.load(open(sys.argv[1])); project=sys.argv[2]; tracked=os.path.isfile(sys.argv[3])
def run(*args):
    result=subprocess.run(args,text=True,capture_output=True)
    if result.returncode: raise SystemExit(f'Docker resource inventory failed: {" ".join(args[:3])}')
    return result.stdout
def inspect(kind,name):
    value=json.loads(run('docker',kind,'inspect',name))
    if len(value) != 1 or value[0].get('Name') != name: raise SystemExit(f'{kind} identity is invalid: {name}')
    return value[0]
pools=json.loads(run('docker','info','--format','{{json .DefaultAddressPools}}'))
for kind in ('volume','network'):
    for logical,item in model.get(kind+'s',{}).items():
        name=item.get('name',f'{project}_{logical}')
        existing=[value for value in run('docker',kind,'ls','-q','--filter',f'name=^{name}$').splitlines() if value == name]
        if not existing: continue
        if not tracked: raise SystemExit(f'untracked pre-existing Docker {kind} is forbidden: {name}')
        actual=inspect(kind,name); labels=actual.get('Labels') or {}
        if labels.get('com.docker.compose.project') != project or labels.get(f'com.docker.compose.{kind}') != logical: raise SystemExit(f'{kind} provenance is invalid: {name}')
        if kind == 'volume':
            if actual.get('Driver') != 'local' or actual.get('Options') not in (None,{}): raise SystemExit(f'volume configuration is invalid: {name}')
            continue
        if actual.get('Driver') != 'bridge' or actual.get('Options') not in (None,{}) or actual.get('Ingress'): raise SystemExit(f'network configuration is invalid: {name}')
        if bool(actual.get('Internal')) != bool(item.get('internal')) or bool(actual.get('Attachable')) != bool(item.get('attachable')) or bool(actual.get('EnableIPv6')) != bool(item.get('enable_ipv6')): raise SystemExit(f'network configuration differs from the approved model: {name}')
        ipam=actual.get('IPAM') or {}
        if ipam.get('Driver') not in (None,'default') or ipam.get('Options') not in (None,{}): raise SystemExit(f'network IPAM is invalid: {name}')
        ipv4=[]
        for config in ipam.get('Config') or []:
            if not isinstance(config,dict) or config.get('AuxAddress'): raise SystemExit(f'network IPAM is invalid: {name}')
            try: subnet=ipaddress.ip_network(config.get('Subnet',''))
            except ValueError: raise SystemExit(f'network IPAM is invalid: {name}')
            if subnet.version == 4: ipv4.append((subnet,config.get('Gateway')))
        if len(ipv4) != 1: raise SystemExit(f'network IPv4 allocation is invalid: {name}')
        subnet,gateway=ipv4[0]
        approved=any(subnet.prefixlen == pool.get('Size') and subnet.subnet_of(ipaddress.ip_network(pool.get('Base',''))) for pool in pools)
        if not approved or gateway is not None and ipaddress.ip_address(gateway) not in subnet: raise SystemExit(f'network IPAM is outside approved Docker address pools: {name}')
PY
}

check_port_unique() {
  local file key value other_port
  for file in "$state_dir"/*.state; do
    [[ -e $file ]] || continue
    [[ $file == "$(state_path "$environment")" ]] && continue
    other_port=$(awk -F= '$1=="ROUTE_PORT"{print $2}' "$file")
    [[ $other_port != "$route_port" ]] || die "loopback route port is already owned by another environment: $route_port"
  done
}

write_state() {
  local target tmp
  target=$(state_path "$environment"); tmp=$target.new
  printf 'ENVIRONMENT=%s\nPROJECT=%s\nOWNER=%s\nCOMPOSE_FILE=%s\nROUTE_SERVICE=%s\nROUTE_PORT=%s\nEXPIRES_AT=%s\nSOURCE_REVISION=%s\nIMAGE_DIGESTS=%s\nUPDATED_AT=%s\n' \
    "$environment" "$project" "$owner" "$(deployed_compose_path "$environment")" "$route_service" "$route_port" "$expires_at" "$source_revision" "$image_digests" "$(date +%s)" >"$tmp"
  chmod 600 "$tmp"; mv -fT "$tmp" "$target"
}

prepare_converge() {
  local count
  load_spec "$environment"
  check_port_unique
  count=$(find "$state_dir" -maxdepth 1 -type f -name '*.state' | wc -l)
  [[ -f $(state_path "$environment") || $count -lt $max_environments ]] || die 'maximum environment count reached'
  prepared_rendered=$(mktemp)
  if ! validate_compose "$prepared_rendered"; then rm -f "$prepared_rendered"; die 'compose policy validation failed'; fi
  if [[ $action == --converge && -f $(state_path "$environment") ]]; then
    secure_file "$(deployed_compose_path "$environment")" 600
    if ! cmp -s "$prepared_rendered" "$(deployed_compose_path "$environment")"; then rm -f "$prepared_rendered"; die 'changing an installed Compose model requires reset'; fi
  fi
}

apply_converge() {
  validate_existing_resources "$prepared_rendered" || die 'pre-existing Docker resource validation failed'
  [[ -f $(state_path "$environment") ]] || install -m 0600 "$prepared_rendered" "$(deployed_compose_path "$environment")"
  write_state
  if ! docker compose -p "$compose_project" -f "$(deployed_compose_path "$environment")" up -d --remove-orphans --wait --wait-timeout 60; then
    rm -f "$prepared_rendered"
    die 'environment activation failed; tracked state retained for cleanup'
  fi
  rm -f "$prepared_rendered"
  inspect_environment "$(state_path "$environment")" | grep -q 'STATUS=running' || die 'environment route is unhealthy; tracked state retained for cleanup'
  report "CONVERGED environment=$environment project=$project owner=$owner route=loopback:$route_port expires_at=$expires_at"
}

converge() { prepare_converge; apply_converge; }

remove_environment() {
  local target compose id=$1
  target=$(state_path "$id")
  if [[ -f $target ]]; then
    secure_file "$target" 600
    compose=$(awk -F= '$1=="COMPOSE_FILE"{print substr($0,index($0,"=")+1)}' "$target")
    [[ $compose == "$(deployed_compose_path "$id")" ]] || die 'stored compose path is unexpected'
    secure_file "$compose" 600
    docker compose -p "$(project_name "$id")" -f "$compose" down --timeout 10 --volumes --remove-orphans || return 1
    rm -f -- "$target" "$(deployed_compose_path "$id")"
  fi
  report "REMOVED environment=$id"
}

reject_orphaned_projects() {
  local kind inventory resource project format
  local -a inspect_command
  for kind in container volume network; do
    case $kind in
      container) inventory=$(docker ps -aq --filter label=com.docker.compose.project) || die "Docker Compose $kind inventory could not be inspected"; inspect_command=(docker inspect); format='{{index .Config.Labels "com.docker.compose.project"}}' ;;
      volume) inventory=$(docker volume ls -q --filter label=com.docker.compose.project) || die "Docker Compose $kind inventory could not be inspected"; inspect_command=(docker volume inspect); format='{{index .Labels "com.docker.compose.project"}}' ;;
      network) inventory=$(docker network ls -q --filter label=com.docker.compose.project) || die "Docker Compose $kind inventory could not be inspected"; inspect_command=(docker network inspect); format='{{index .Labels "com.docker.compose.project"}}' ;;
    esac
    while IFS= read -r resource; do
      [[ -n $resource ]] || continue
      project=$("${inspect_command[@]}" --format "$format" "$resource") || die "Docker Compose $kind identity could not be inspected"
      [[ $project != ci-fleet-test-* ]] && continue
      [[ $project =~ ^ci-fleet-test-([a-z0-9][a-z0-9-]{0,62})$ && -f $(state_path "${BASH_REMATCH[1]}") ]] || die "orphaned tester Compose project exists: $project"
    done <<<"$inventory"
  done
}

live_container_matches() {
  local compose=$1 service=$2 resource=$3 inspection rc=0
  inspection=$(mktemp)
  if ! docker inspect "$resource" >"$inspection"; then rm -f "$inspection"; return 1; fi
  python3 - "$compose" "$service" "$inspection" <<'PY' || rc=$?
import json,sys
model=json.load(open(sys.argv[1])); service=model['services'][sys.argv[2]]
value=json.load(open(sys.argv[3])); actual=value[0] if len(value)==1 else {}; host=actual.get('HostConfig') or {}
security=lambda values: sorted(str(value).replace('=true',':true') for value in values or [])
expected_ports={}
for port in service.get('ports',[]):
    key=f"{int(port['target'])}/{port.get('protocol','tcp')}"
    expected_ports.setdefault(key,[]).append({'HostIp':str(port.get('host_ip','')),'HostPort':str(port['published'])})
expected_mounts=[(mount['type'],'',mount['target'],not mount.get('read_only',False)) for mount in service.get('volumes',[])]
for secret in service.get('secrets',[]):
    item={'source':secret,'target':secret} if isinstance(secret,str) else secret
    expected_mounts.append(('bind',model['secrets'][item['source']]['file'],f"/run/secrets/{item.get('target',item['source'])}",False))
expected_mounts=sorted(expected_mounts)
actual_mounts=sorted((mount.get('Type'),mount.get('Source','') if mount.get('Type')=='bind' else '',mount.get('Destination'),bool(mount.get('RW'))) for mount in actual.get('Mounts') or [])
valid=(host.get('Privileged') is False and host.get('ReadonlyRootfs') is True and set(host.get('CapDrop') or [])=={'ALL'} and
       not host.get('CapAdd') and security(host.get('SecurityOpt'))==security(service['security_opt']) and
       host.get('NanoCpus')==int(service['cpus']*1_000_000_000) and host.get('Memory')==service['mem_limit'] and
       host.get('PidsLimit')==service['pids_limit'] and (host.get('PortBindings') or {})==expected_ports and actual_mounts==expected_mounts)
raise SystemExit(0 if valid else 1)
PY
  rm -f "$inspection"
  return "$rc"
}

inspect_environment() {
  local target=$1 id compose route_service status=running resource value bytes=0 mount expected running_state service configured_image
  local -a containers=()
  local -A expected_images=() observed_services=()
  id=$(basename "$target" .state); secure_file "$target" 600
  while IFS='=' read -r key value; do
    case $key in ENVIRONMENT|PROJECT|OWNER|ROUTE_PORT|EXPIRES_AT|SOURCE_REVISION|IMAGE_DIGESTS|UPDATED_AT) printf '%s=%s ' "$key" "$value" ;; esac
  done <"$target"
  compose=$(awk -F= '$1=="COMPOSE_FILE"{print substr($0,index($0,"=")+1)}' "$target")
  route_service=$(awk -F= '$1=="ROUTE_SERVICE"{print $2}' "$target")
  expected=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["services"]))' "$compose")
  while read -r service configured_image; do expected_images[$service]=$configured_image; done < <(python3 -c 'import json,sys; value=json.load(open(sys.argv[1])); print("\n".join("%s %s" % (name,service["image"]) for name,service in value["services"].items()))' "$compose")
  mapfile -t containers < <(docker compose -p "$(project_name "$id")" -f "$compose" ps -q)
  [[ ${#containers[@]} == "$expected" ]] || status=unhealthy
  for resource in "${containers[@]}"; do
    running_state=$(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$resource")
    read -r service configured_image < <(docker inspect --format '{{index .Config.Labels "com.docker.compose.service"}} {{.Config.Image}}' "$resource")
    [[ -n $service ]] || { status=unhealthy; continue; }
    [[ -n ${expected_images[$service]:-} && $configured_image == "${expected_images[$service]}" ]] || status=unhealthy
    live_container_matches "$compose" "$service" "$resource" || status=unhealthy
    observed_services[$service]=$((${observed_services[$service]:-0} + 1))
    if [[ $service == "$route_service" ]]; then
      [[ $running_state == 'running healthy' ]] || status=unhealthy
    else
      [[ $running_state == 'running healthy' || $running_state == 'running none' ]] || status=unhealthy
    fi
  done
  for service in "${!expected_images[@]}"; do [[ ${observed_services[$service]:-0} == 1 ]] || status=unhealthy; done
  inventory=$(docker ps -aq --filter "label=com.docker.compose.project=$(project_name "$id")") || die 'container inventory failed'
  while IFS= read -r resource; do
    [[ -n $resource ]] || continue
    value=$(docker inspect --size --format '{{.SizeRw}}' "$resource"); [[ $value =~ ^[0-9]+$ ]] || die 'container disk size is invalid'; bytes=$((bytes + value))
  done <<<"$inventory"
  inventory=$(docker volume ls -q --filter "label=com.docker.compose.project=$(project_name "$id")") || die 'volume inventory failed'
  while IFS= read -r resource; do
    [[ -n $resource ]] || continue
    mount=$(docker volume inspect --format '{{.Mountpoint}}' "$resource"); value=$(du -sb "$mount" | awk '{print $1}'); [[ $value =~ ^[0-9]+$ ]] || die 'volume disk size is invalid'; bytes=$((bytes + value))
  done <<<"$inventory"
  printf 'STATUS=%s DISK_BYTES=%s\n' "$status" "$bytes"
}

load_global
docker_socket=$(root_path /var/run/docker.sock)
if [[ ${CI_FLEET_TESTING:-0} == 1 ]]; then
  [[ -f $docker_socket ]] || die 'local Docker socket is unavailable'
else
  [[ -S $docker_socket ]] || die 'local Docker socket is unavailable'
fi
socket_mode=$(stat -c %a "$docker_socket")
[[ ! -L $docker_socket && $(stat -c %u "$docker_socket") == "$expected_uid" ]] || die 'local root-owned Docker socket is unavailable'
(( (8#$socket_mode & 0002) == 0 )) || die 'local Docker socket is writable outside its administration group'
unset DOCKER_CONTEXT
export DOCKER_HOST="unix://$docker_socket"
[[ $(docker info --format '{{.DockerRootDir}}') == "$(root_path /var/lib/docker)" ]] || die 'Docker daemon root is not the expected local path'
if [[ ${CI_FLEET_TESTER_LOCK_FD:-} == 8 && -e /proc/$$/fd/8 && $(readlink -f /proc/$$/fd/8) == "$lock_file" ]] && flock -n 8; then
  unset CI_FLEET_TESTER_LOCK_FD
else
  mkdir -m 0755 "$(dirname "$lock_file")" 2>/dev/null || true
  secure_directory "$(dirname "$lock_file")" 755
  exec 9>"$lock_file"
  flock -x 9
fi
case $action in
  --check)
    docker info --format '{{.DockerRootDir}}' >/dev/null
    docker compose version >/dev/null
    check_network_probes
    check_disk_threshold
    report "CHECK_OK max_environments=$max_environments disk_used_percent=$used"
    ;;
  --converge) converge ;;
  --reset) prepare_converge; remove_environment "$environment"; apply_converge ;;
  --remove) remove_environment "$environment" ;;
  --inspect) [[ -f $(state_path "$environment") ]] || die 'environment is not installed'; inspect_environment "$(state_path "$environment")" ;;
  --cleanup)
    now=$(date +%s); failed=0
    for target in "$state_dir"/*.state; do
      [[ -e $target ]] || continue
      if ! (
        secure_file "$target" 600
        expires=$(awk -F= '$1=="EXPIRES_AT"{print $2}' "$target")
        [[ $expires =~ ^[0-9]+$ ]] || die "invalid expiration in $target"
        ((expires > now)) || remove_environment "$(basename "$target" .state)"
      ); then failed=1; fi
    done
    reject_orphaned_projects
    ((failed == 0)) || die 'one or more expired environments could not be removed'
    report 'CLEANUP_OK'
    ;;
  --health)
    failed=0
    for target in "$state_dir"/*.state; do [[ -e $target ]] || continue; inspect_environment "$target" | grep -q 'STATUS=running' || failed=1; done
    ((failed == 0)) || die 'one or more test environments are unhealthy'
    check_disk_threshold
    check_network_probes
    report 'HEALTH_OK'
    ;;
esac
