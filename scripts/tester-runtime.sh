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
case $action in --remove|--cleanup) ;; *) required_commands+=' systemctl' ;; esac
case $action in --converge|--reset|--inspect|--health) required_commands+=' timeout' ;; esac
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
  [[ $probe_host =~ ^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$ && $probe_url =~ ^https://([A-Za-z0-9][A-Za-z0-9.-]{0,252})(:[0-9]+)?/[^[:space:]]*$ ]] || die 'DNS and HTTPS probe targets are required'
  [[ ${BASH_REMATCH[1],,} == "${probe_host,,}" ]] || die 'DNS and HTTPS probes must use the same host'
  [[ ${ENV_VALUES[CI_FLEET_TESTER_ISOLATION_ACK]:-} == test-only-no-production-authority ]] || die 'explicit test-only isolation acknowledgement is required'
}

check_disk_threshold() {
  used=$(df -P "$(root_path /var/lib/docker)" | awk 'NR==2{gsub(/%/,"",$5);print $5}')
  [[ $used =~ ^[0-9]+$ && $used -lt $disk_warn ]] || die 'Docker storage exceeds configured warning threshold'
}

check_network_probes() {
  getent ahosts "$probe_host" >/dev/null || die 'test-host DNS probe failed'
  curl --disable --noproxy '*' --fail --silent --show-error --head --max-time 10 --output /dev/null "$probe_url" || die 'test-host HTTPS/proxy probe failed'
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

compose_snapshot() {
  local project=$1 source=$2 variable replay=$docker_client_config/replay.json
  shift 2
  python3 - "$source" "$replay" <<'PY_REPLAY' || return 1
import json,sys
def escape(value):
    if isinstance(value,str): return value.replace('$','$$')
    if isinstance(value,list): return [escape(item) for item in value]
    if isinstance(value,dict): return {key:escape(item) for key,item in value.items()}
    return value
with open(sys.argv[2],'w') as output: json.dump(escape(json.load(open(sys.argv[1]))),output)
PY_REPLAY
  local -a clean_environment=(env -i "PATH=$PATH" "DOCKER_HOST=$DOCKER_HOST" "DOCKER_CONFIG=$DOCKER_CONFIG")
  if [[ ${CI_FLEET_TESTING:-0} == 1 ]]; then for variable in ${!FAKE_@}; do clean_environment+=("$variable=${!variable}"); done; fi
  : >"$docker_client_config/empty.env"
  "${clean_environment[@]}" docker compose --env-file "$docker_client_config/empty.env" -p "$project" -f "$replay" "$@"
}

validate_compose() {
  local rendered=$1 empty_env variable
  local -a clean_environment=(env -i "PATH=$PATH" "DOCKER_HOST=$DOCKER_HOST" "DOCKER_CONFIG=$DOCKER_CONFIG")
  if ! python3 - "$compose_file" <<'PY'
import sys
try:
    import yaml
except ImportError:
    raise SystemExit('python3-yaml is required for Compose source validation')
try:
    with open(sys.argv[1],encoding='utf-8') as source:
        pending=[yaml.compose(source,Loader=yaml.SafeLoader)]
    seen=set()
    while pending:
        node=pending.pop()
        if node is None or id(node) in seen: continue
        seen.add(id(node))
        if isinstance(node,yaml.MappingNode):
            keys=set()
            for key,value in node.value:
                if not isinstance(key,yaml.ScalarNode) or key.value in keys:
                    raise SystemExit('Compose complex or duplicate mapping keys are forbidden')
                keys.add(key.value)
                if key.value in ('include','label_file','extends','env_file'):
                    raise SystemExit('Compose external file loading is forbidden')
                pending.append(value)
        elif isinstance(node,yaml.SequenceNode): pending.extend(node.value)
except yaml.YAMLError:
    raise SystemExit('Compose source is not valid YAML')
PY
  then return 1; fi
  if [[ ${CI_FLEET_TESTING:-0} == 1 ]]; then for variable in ${!FAKE_@}; do clean_environment+=("$variable=${!variable}"); done; fi
  empty_env=$(mktemp); chmod 600 "$empty_env"
  if ! "${clean_environment[@]}" docker compose --env-file "$empty_env" -p "$compose_project" -f "$compose_file" config --format json >"$rendered"; then rm -f "$empty_env"; return 1; fi
  rm -f "$empty_env"
  chmod 600 "$rendered"
  python3 - "$rendered" "$route_service" "$route_port" "$compose_project" <<'PY' || return 1
import json,math,re,sys
value=json.load(open(sys.argv[1])); route_service=sys.argv[2]; route_port=int(sys.argv[3]); project=sys.argv[4]
if value.get('name') != project: raise SystemExit('rendered Compose project identity differs from the environment')
services=value.get('services')
if not isinstance(services,dict) or route_service not in services: raise SystemExit('route service is missing')
image=re.compile(r'^[a-z0-9.-]+(?::[0-9]+)?/[A-Za-z0-9_./-]+@sha256:[0-9a-f]{64}$')
ports=[]
for name,service in services.items():
    if not image.fullmatch(str(service.get('image',''))): raise SystemExit(f'{name}: image must use an immutable sha256 digest')
    if service.get('privileged') or service.get('provider') or service.get('runtime') or service.get('network_mode') or service.get('pid') or service.get('ipc') or service.get('uts') or service.get('userns_mode') or service.get('external_links') or service.get('pre_start') or service.get('post_start') or service.get('pre_stop'): raise SystemExit(f'{name}: external runtime/namespace/link/logging/privileged lifecycle access is forbidden')
    if service.get('logging') != {'driver':'local','options':{'max-size':'10m','max-file':'3','compress':'true'}}: raise SystemExit(f'{name}: bounded local logging is required')
    deploy=service.get('deploy') or {}; reservations=(deploy.get('resources') or {}).get('reservations') or {}
    if service.get('build') or service.get('devices') or service.get('device_cgroup_rules') or service.get('gpus') or reservations.get('devices') or service.get('cap_add') or service.get('container_name') or service.get('hostname') or service.get('use_api_socket') or service.get('volumes_from'): raise SystemExit(f'{name}: build/device/capability/external mount/global identity is forbidden')
    if service.get('scale',1) != 1 or deploy.get('replicas',1) != 1: raise SystemExit(f'{name}: exactly one replica is required')
    if not all(isinstance(service.get(key),(int,float)) and not isinstance(service[key],bool) and math.isfinite(service[key]) and service[key] > 0 for key in ('cpus','mem_limit','pids_limit')): raise SystemExit(f'{name}: positive CPU, memory, and PID limits are required')
    swap_limit=service.get('memswap_limit',0)
    if not isinstance(swap_limit,(int,float)) or isinstance(swap_limit,bool) or not math.isfinite(swap_limit) or swap_limit < 0 or 0 < swap_limit < service['mem_limit']: raise SystemExit(f'{name}: memory plus swap must use a finite limit at least as large as memory')
    if service.get('oom_kill_disable') or service.get('oom_score_adj',0) < 0: raise SystemExit(f'{name}: OOM priority overrides are forbidden')
    if service.get('models'): raise SystemExit(f'{name}: external model runners are forbidden')
    if service.get('environment') or service.get('env_file') or service.get('configs'): raise SystemExit(f'{name}: alternate credential channels are forbidden')
    if service.get('cgroup') != 'private': raise SystemExit(f'{name}: a private cgroup namespace is required')
    if service.get('profiles'): raise SystemExit(f'{name}: Compose profiles are forbidden')
    if service.get('label_file'): raise SystemExit(f'{name}: external label files are forbidden')
    if service.get('read_only') is not True or set(service.get('cap_drop',[])) != {'ALL'}: raise SystemExit(f'{name}: read_only and cap_drop ALL are required')
    if service.get('security_opt') not in (['no-new-privileges:true'],['no-new-privileges=true']): raise SystemExit(f'{name}: no-new-privileges=true must be the only security option')
    if service.get('tmpfs'): raise SystemExit(f'{name}: service-level tmpfs is unsupported; use volumes with type tmpfs')
    for mount in service.get('volumes',[]):
        if isinstance(mount,str) or mount.get('type') not in ('volume','tmpfs'): raise SystemExit(f'{name}: host bind mounts are forbidden')
        if mount['type']=='volume' and mount.get('source') not in value.get('volumes',{}): raise SystemExit(f'{name}: volume mounts must name a declared project volume')
    for port in service.get('ports',[]):
        if not isinstance(port,dict) or str(port.get('host_ip','')) != '127.0.0.1' or port.get('protocol','tcp') != 'tcp': raise SystemExit(f'{name}: published ports must use TCP on loopback')
        ports.append((name,int(port.get('published',0)),int(port.get('target',0))))
if ports != [(route_service,route_port,ports[0][2] if ports else 0)] or not ports or ports[0][2] < 1: raise SystemExit('exactly one declared loopback route is required')
if value.get('models'): raise SystemExit('external model runners are forbidden')
if value.get('configs'): raise SystemExit('top-level configs are forbidden')
for section in ('networks','volumes'):
    resolved_names=set()
    for name,item in value.get(section,{}).items():
        resolved=item.get('name',f'{project}_{name}')
        if resolved in resolved_names: raise SystemExit(f'{section}.{name}: duplicate resolved resource name')
        resolved_names.add(resolved)
        if item.get('external') or not resolved.startswith(f'{project}_'): raise SystemExit(f'{section}.{name}: external/unscoped names are forbidden')
        if section == 'networks' and (item.get('driver') not in (None,'bridge') or item.get('driver_opts')): raise SystemExit(f'{section}.{name}: custom network drivers are forbidden')
        if section == 'networks' and item.get('enable_ipv4') is False: raise SystemExit(f'{section}.{name}: IPv4 is required')
        if section == 'networks' and item.get('enable_ipv6'): raise SystemExit(f'{section}.{name}: IPv6 is unsupported')
        if section == 'networks' and item.get('ipam'): raise SystemExit(f'{section}.{name}: custom network IPAM configuration is forbidden')
        if section == 'volumes' and (item.get('driver') or item.get('driver_opts')): raise SystemExit(f'{section}.{name}: custom volume drivers are forbidden')
PY
  validate_secrets "$rendered" "$secret_dir" || return 1
  image_digests=$(python3 - "$rendered" <<'PY'
import json,sys
value=json.load(open(sys.argv[1]))
print(','.join(sorted({service['image'].rsplit('@',1)[1] for service in value['services'].values()})))
PY
)
}

validate_secrets() {
  python3 - "$1" "$2" "$expected_uid" <<'PY'
import json,os,stat,sys
secret_dir=sys.argv[2]; expected_uid=int(sys.argv[3])
secrets=json.load(open(sys.argv[1])).get('secrets',{})
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
}

validate_existing_resources() {
  local id=${2:-$environment} live=${3:-false}
  python3 - "$1" "$(project_name "$id")" "$(state_path "$id")" "$live" <<'PY'
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
        existing=[value for value in run('docker',kind,'ls','--format','{{.Name}}','--filter',f'name=^{name}$').splitlines() if value == name]
        if not existing:
            if sys.argv[4]=='true': raise SystemExit(f'live {kind} is missing: {name}')
            continue
        if not tracked: raise SystemExit(f'untracked pre-existing Docker {kind} is forbidden: {name}')
        actual=inspect(kind,name); labels=actual.get('Labels') or {}
        if labels.get('com.docker.compose.project') != project or labels.get(f'com.docker.compose.{kind}') != logical: raise SystemExit(f'{kind} provenance is invalid: {name}')
        if kind == 'volume':
            if actual.get('Driver') != 'local' or actual.get('Options') not in (None,{}): raise SystemExit(f'volume configuration is invalid: {name}')
            continue
        if actual.get('Driver') != 'bridge' or actual.get('Options') not in (None,{}) or actual.get('Ingress'): raise SystemExit(f'network configuration is invalid: {name}')
        if bool(actual.get('Internal')) != bool(item.get('internal')) or bool(actual.get('Attachable')) != bool(item.get('attachable')) or actual.get('EnableIPv6') or item.get('enable_ipv6'): raise SystemExit(f'network configuration differs from the approved model: {name}')
        ipam=actual.get('IPAM') or {}
        if ipam.get('Driver') not in (None,'default') or ipam.get('Options') not in (None,{}): raise SystemExit(f'network IPAM is invalid: {name}')
        ipv4=[]
        for config in ipam.get('Config') or []:
            if not isinstance(config,dict) or config.get('AuxAddress'): raise SystemExit(f'network IPAM is invalid: {name}')
            try: subnet=ipaddress.ip_network(config.get('Subnet',''))
            except ValueError: raise SystemExit(f'network IPAM is invalid: {name}')
            if subnet.version != 4: raise SystemExit(f'network IPv6 allocation is unsupported: {name}')
            ipv4.append((subnet,config.get('Gateway')))
        if len(ipv4) != 1: raise SystemExit(f'network IPv4 allocation is invalid: {name}')
        subnet,gateway=ipv4[0]
        approved=any(subnet.version == ipaddress.ip_network(pool.get('Base','')).version and subnet.prefixlen == pool.get('Size') and subnet.subnet_of(ipaddress.ip_network(pool.get('Base',''))) for pool in pools)
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
  if ! compose_snapshot "$compose_project" "$(deployed_compose_path "$environment")" up -d --remove-orphans --wait --wait-timeout 60; then
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
    compose_snapshot "$(project_name "$id")" "$compose" down --timeout 10 --volumes --remove-orphans || return 1
    project_resources_absent "$id" || return 1
    rm -f -- "$target" "$(deployed_compose_path "$id")"
  else
    reject_orphaned_projects
  fi
  report "REMOVED environment=$id"
}

project_resources_absent() {
  local project inventory kind
  project=$(project_name "$1")
  for kind in container volume network; do
    case $kind in
      container) inventory=$(docker ps -aq --filter "label=com.docker.compose.project=$project") || return 1 ;;
      volume) inventory=$(docker volume ls -q --filter "label=com.docker.compose.project=$project") || return 1 ;;
      network) inventory=$(docker network ls -q --filter "label=com.docker.compose.project=$project") || return 1 ;;
    esac
    [[ -z $inventory ]] || { printf 'ERROR: project %s resources remain after teardown; protected state retained\n' "$kind" >&2; return 1; }
  done
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
if host.get('Runtime') != 'runc' or host.get('LogConfig') != {'Type':'local','Config':{'max-size':'10m','max-file':'3','compress':'true'}}: raise SystemExit('live runtime or logging policy differs')
security=lambda values: sorted(str(value).replace('=true',':true') for value in values or [])
expected_ports={}
for port in service.get('ports',[]):
    key=f"{int(port['target'])}/{port.get('protocol','tcp')}"
    expected_ports.setdefault(key,[]).append({'HostIp':str(port.get('host_ip','')),'HostPort':str(port['published'])})
expected_mounts=[(mount['type'],model['volumes'][mount['source']]['name'] if mount['type']=='volume' else '',mount['target'],not mount.get('read_only',False)) for mount in service.get('volumes',[])]
for secret in service.get('secrets',[]):
    item={'source':secret,'target':secret} if isinstance(secret,str) else secret
    target=item.get('target',item['source'])
    expected_mounts.append(('bind',model['secrets'][item['source']]['file'],target if target.startswith('/') else f'/run/secrets/{target}',False))
expected_mounts=sorted(expected_mounts)
actual_mounts=sorted((mount.get('Type'),mount.get('Source','') if mount.get('Type')=='bind' else mount.get('Name','') if mount.get('Type')=='volume' else '',mount.get('Destination'),bool(mount.get('RW'))) for mount in actual.get('Mounts') or [])
networks={model['networks'][name]['name'] for name in service.get('networks',{'default':{}})}
valid=(host.get('Privileged') is False and host.get('ReadonlyRootfs') is True and set(host.get('CapDrop') or [])=={'ALL'} and
       not host.get('CapAdd') and not host.get('Devices') and not host.get('DeviceRequests') and not host.get('DeviceCgroupRules') and security(host.get('SecurityOpt'))==security(service['security_opt']) and
       host.get('NanoCpus')==int(service['cpus']*1_000_000_000) and host.get('Memory')==service['mem_limit'] and
       host.get('MemorySwap')==(service.get('memswap_limit') or 2*service['mem_limit']) and host['MemorySwap'] >= service['mem_limit'] and
       host.get('CgroupnsMode')=='private' and host.get('PidsLimit')==service['pids_limit'] and (host.get('PortBindings') or {})==expected_ports and actual_mounts==expected_mounts and
       host.get('OomKillDisable') in (None,False) and host.get('OomScoreAdj',0)==service.get('oom_score_adj',0) and
       host.get('NetworkMode') in networks and set((actual.get('NetworkSettings') or {}).get('Networks') or {})==networks)
raise SystemExit(0 if valid else 1)
PY
  rm -f "$inspection"
  return "$rc"
}

inspect_environment() {
  local target=$1 id compose route_port status=running resource value bytes=0 mount expected running_state service configured_image
  local -a containers=()
  local -A expected_images=() observed_services=()
  id=$(basename "$target" .state); secure_file "$target" 600
  while IFS='=' read -r key value; do
    case $key in ENVIRONMENT|PROJECT|OWNER|ROUTE_PORT|EXPIRES_AT|SOURCE_REVISION|IMAGE_DIGESTS|UPDATED_AT) printf '%s=%s ' "$key" "$value" ;; esac
  done <"$target"
  compose=$(awk -F= '$1=="COMPOSE_FILE"{print substr($0,index($0,"=")+1)}' "$target")
  [[ $compose == "$(deployed_compose_path "$id")" ]] || die 'stored compose path is unexpected'
  secure_file "$compose" 600
  route_port=$(awk -F= '$1=="ROUTE_PORT"{print $2}' "$target")
  [[ $route_port =~ ^[0-9]+$ && $route_port -ge 1024 && $route_port -le 65535 ]] || die 'stored route port is invalid'
  validate_secrets "$compose" "$secret_root/$id" || status=unhealthy
  validate_existing_resources "$compose" "$id" true || status=unhealthy
  expected=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["services"]))' "$compose")
  while read -r service configured_image; do expected_images[$service]=$configured_image; done < <(python3 -c 'import json,sys; value=json.load(open(sys.argv[1])); print("\n".join("%s %s" % (name,service["image"]) for name,service in value["services"].items()))' "$compose")
  mapfile -t containers < <(compose_snapshot "$(project_name "$id")" "$compose" ps -q)
  [[ ${#containers[@]} == "$expected" ]] || status=unhealthy
  for resource in "${containers[@]}"; do
    running_state=$(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$resource")
    read -r service configured_image < <(docker inspect --format '{{index .Config.Labels "com.docker.compose.service"}} {{.Config.Image}}' "$resource")
    [[ -n $service ]] || { status=unhealthy; continue; }
    [[ -n ${expected_images[$service]:-} && $configured_image == "${expected_images[$service]}" ]] || status=unhealthy
    live_container_matches "$compose" "$service" "$resource" || status=unhealthy
    observed_services[$service]=$((${observed_services[$service]:-0} + 1))
    [[ $running_state == 'running healthy' || $running_state == 'running none' ]] || status=unhealthy
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
  # shellcheck disable=SC2016 # The child shell receives the validated port.
  timeout 5 bash -c 'exec 3<>/dev/tcp/127.0.0.1/"$1"' bash "$route_port" 2>/dev/null || status=unhealthy
  printf 'STATUS=%s DISK_BYTES=%s\n' "$status" "$bytes"
}

secure_directory "$(dirname "$state_dir")" 700
secure_directory "$state_dir" 700
if [[ $action != --remove && $action != --cleanup ]]; then
  load_global
  for role_path in /etc/ci-fleet /var/lib/ci-fleet /opt/ci-fleet/current /opt/ci-fleet/manager/current \
    /etc/ci-fleet-deployer /var/lib/ci-fleet-deployer /var/lib/ci-fleet-deployer-container-credentials /opt/ci-fleet-deployer \
    /etc/ci-fleet-status /var/lib/ci-fleet-status /opt/ci-fleet-status /var/lib/ci-fleet-status-installer; do
    [[ ! -e $(root_path "$role_path") && ! -L $(root_path "$role_path") ]] || die 'another ci-fleet host role is present'
  done
  for role_unit in "$(root_path /etc/systemd/system)"/ci-fleet-deployer* "$(root_path /etc/systemd/system)"/ci-fleet-health* "$(root_path /etc/systemd/system)"/ci-fleet-cleanup* "$(root_path /etc/systemd/system)"/ci-fleet-reconcile* "$(root_path /etc/systemd/system)"/ci-fleet-drift* "$(root_path /etc/systemd/system)"/ci-fleet-status-receiver* "$(root_path /etc/systemd/system)"/actions.runner*; do
    [[ ! -e $role_unit && ! -L $role_unit ]] || die 'another ci-fleet host role is present'
  done
  for inventory_kind in list-unit-files list-units; do
    inventory_status=0
    inventory=$(SYSTEMD_COLORS=0 systemctl "$inventory_kind" --all --full --plain --no-legend --no-pager 2>&1) || inventory_status=$?
    # list-unit-files returns status 1 for an empty inventory without diagnostics.
    if ((inventory_status != 0)); then
      [[ $inventory_kind == list-unit-files && $inventory_status == 1 && -z $inventory ]] || die 'foreign systemd unit inventory could not be inspected'
    fi
    while read -r unit _; do
      case $unit in
        ci-fleet-deployer*|ci-fleet-health*|ci-fleet-cleanup*|ci-fleet-reconcile*|ci-fleet-drift*|ci-fleet-status-receiver*|actions.runner*) die 'another ci-fleet host role is present' ;;
      esac
    done <<<"$inventory"
  done
fi
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
# Docker client proxies and credentials must not enter application containers.
docker_client_config=$(mktemp -d)
trap 'rm -rf -- "$docker_client_config"' EXIT
export DOCKER_CONFIG=$docker_client_config
[[ $(docker info --format '{{.DockerRootDir}}') == "$(root_path /var/lib/docker)" ]] || die 'Docker daemon root is not the expected local path'
case $action in
  --check|--health|--converge|--reset)
    [[ $(docker info --format '{{len .DefaultAddressPools}}') =~ ^[1-9][0-9]*$ ]] || die 'explicit Docker default address pools are required'
    ;;
esac
if [[ ${CI_FLEET_TESTER_LOCK_FD:-} == 8 && -e /proc/$$/fd/8 && $(readlink -f /proc/$$/fd/8) == "$lock_file" ]] && flock -n 8; then
  unset CI_FLEET_TESTER_LOCK_FD
else
  mkdir -m 0755 "$(dirname "$lock_file")" 2>/dev/null || true
  secure_directory "$(dirname "$lock_file")" 755
  exec 9>"$lock_file"
  flock -x 9
fi
if [[ $action != --remove && $action != --cleanup ]]; then
  for kind in container network volume; do
    case $kind in
      container) inventory=$(docker ps -a --format '{{.ID}}|{{.Label "com.docker.compose.project"}}') ;;
      network) inventory=$(docker network ls --filter type=custom --format '{{.ID}}|{{.Label "com.docker.compose.project"}}') ;;
      volume) inventory=$(docker volume ls --format '{{.Name}}|{{.Label "com.docker.compose.project"}}') ;;
    esac
    while IFS='|' read -r resource resource_project; do
      [[ -n $resource ]] || continue
      [[ $resource_project =~ ^ci-fleet-test-([a-z0-9][a-z0-9-]{0,62})$ && -f $(state_path "${BASH_REMATCH[1]}") ]] || die 'unrelated or untracked Docker workload is present'
    done <<<"$inventory"
  done
fi
case $action in
  --check)
    docker info --format '{{.DockerRootDir}}' >/dev/null
    [[ $(docker info --format '{{.DefaultRuntime}}') == runc ]] || die 'Docker default runtime must be runc'
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
