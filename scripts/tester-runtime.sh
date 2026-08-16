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
lock_file=$(root_path /run/lock/ci-fleet-tester.lock)
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
for command in awk basename chmod cmp curl date df dirname docker du env find flock getent grep install mktemp mv python3 readlink rm stat wc; do command -v "$command" >/dev/null || die "required command is unavailable: $command"; done

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
  secure_directory "$secret_dir" 700
}

validate_compose() {
  local rendered=$1 empty_env variable
  local -a clean_environment=(env -i "PATH=$PATH" "DOCKER_HOST=$DOCKER_HOST")
  if [[ ${CI_FLEET_TESTING:-0} == 1 ]]; then for variable in ${!FAKE_@}; do clean_environment+=("$variable=${!variable}"); done; fi
  empty_env=$(mktemp); chmod 600 "$empty_env"
  if ! "${clean_environment[@]}" docker compose --env-file "$empty_env" -p "$compose_project" -f "$compose_file" config --format json >"$rendered"; then rm -f "$empty_env"; return 1; fi
  rm -f "$empty_env"
  chmod 600 "$rendered"
  python3 - "$rendered" "$route_service" "$route_port" "$compose_project" "$secret_dir" "$expected_uid" <<'PY' || return 1
import json,os,re,stat,sys
value=json.load(open(sys.argv[1])); route_service=sys.argv[2]; route_port=int(sys.argv[3]); project=sys.argv[4]; secret_dir=sys.argv[5]; expected_uid=int(sys.argv[6])
services=value.get('services')
if not isinstance(services,dict) or route_service not in services: raise SystemExit('route service is missing')
image=re.compile(r'^[a-z0-9.-]+(?::[0-9]+)?/[A-Za-z0-9_./-]+@sha256:[0-9a-f]{64}$')
ports=[]
for name,service in services.items():
    if not image.fullmatch(str(service.get('image',''))): raise SystemExit(f'{name}: image must use an immutable sha256 digest')
    if service.get('privileged') or service.get('network_mode') or service.get('pid') or service.get('ipc') or service.get('userns_mode') or service.get('cgroup') or service.get('external_links') or service.get('post_start') or service.get('pre_stop'): raise SystemExit(f'{name}: external namespace/link/privileged lifecycle access is forbidden')
    deploy=service.get('deploy') or {}; reservations=(deploy.get('resources') or {}).get('reservations') or {}
    if service.get('build') or service.get('devices') or service.get('gpus') or reservations.get('devices') or service.get('cap_add') or service.get('container_name') or service.get('hostname') or service.get('use_api_socket') or service.get('volumes_from'): raise SystemExit(f'{name}: build/device/capability/external mount/global identity is forbidden')
    if deploy.get('replicas',1) != 1: raise SystemExit(f'{name}: exactly one replica is required')
    if service.get('environment') or service.get('env_file') or service.get('configs'): raise SystemExit(f'{name}: alternate credential channels are forbidden')
    if service.get('read_only') is not True or 'ALL' not in service.get('cap_drop',[]): raise SystemExit(f'{name}: read_only and cap_drop ALL are required')
    if service.get('security_opt') not in (['no-new-privileges:true'],['no-new-privileges=true']): raise SystemExit(f'{name}: no-new-privileges=true must be the only security option')
    for mount in service.get('volumes',[]):
        if isinstance(mount,str) or mount.get('type') not in ('volume','tmpfs'): raise SystemExit(f'{name}: host bind mounts are forbidden')
    for port in service.get('ports',[]):
        if not isinstance(port,dict) or str(port.get('host_ip','')) != '127.0.0.1': raise SystemExit(f'{name}: published ports must bind loopback')
        ports.append((name,int(port.get('published',0)),int(port.get('target',0))))
if ports != [(route_service,route_port,ports[0][2] if ports else 0)] or not ports or ports[0][2] < 1: raise SystemExit('exactly one declared loopback route is required')
if value.get('configs'): raise SystemExit('top-level configs are forbidden')
for section in ('networks','volumes'):
    for name,item in value.get(section,{}).items():
        resolved=item.get('name',f'{project}_{name}')
        if item.get('external') or not resolved.startswith(f'{project}_'): raise SystemExit(f'{section}.{name}: external/unscoped names are forbidden')
        if section == 'networks' and (item.get('driver') not in (None,'bridge') or item.get('driver_opts')): raise SystemExit(f'{section}.{name}: custom network drivers are forbidden')
        if section == 'volumes' and (item.get('driver') or item.get('driver_opts')): raise SystemExit(f'{section}.{name}: custom volume drivers are forbidden')
for name,item in value.get('secrets',{}).items():
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
  [[ -f $(state_path "$environment") ]] || install -m 0600 "$prepared_rendered" "$(deployed_compose_path "$environment")"
  write_state
  if ! docker compose -p "$compose_project" -f "$(deployed_compose_path "$environment")" up -d --remove-orphans --wait --wait-timeout 60; then
    rm -f "$prepared_rendered"
    die 'environment activation failed; tracked state retained for cleanup'
  fi
  rm -f "$prepared_rendered"
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

inspect_environment() {
  local target=$1 id compose status=running resource value bytes=0 mount expected running_state
  local -a containers=()
  id=$(basename "$target" .state); secure_file "$target" 600
  while IFS='=' read -r key value; do
    case $key in ENVIRONMENT|PROJECT|OWNER|ROUTE_PORT|EXPIRES_AT|SOURCE_REVISION|IMAGE_DIGESTS|UPDATED_AT) printf '%s=%s ' "$key" "$value" ;; esac
  done <"$target"
  compose=$(awk -F= '$1=="COMPOSE_FILE"{print substr($0,index($0,"=")+1)}' "$target")
  expected=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["services"]))' "$compose")
  mapfile -t containers < <(docker compose -p "$(project_name "$id")" -f "$compose" ps -q)
  [[ ${#containers[@]} == "$expected" ]] || status=unhealthy
  for resource in "${containers[@]}"; do
    running_state=$(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$resource")
    [[ $running_state == 'running healthy' || $running_state == 'running none' ]] || status=unhealthy
  done
  while IFS= read -r resource; do
    [[ -n $resource ]] || continue
    value=$(docker inspect --size --format '{{.SizeRw}}' "$resource"); [[ $value =~ ^[0-9]+$ ]] || die 'container disk size is invalid'; bytes=$((bytes + value))
  done < <(docker ps -aq --filter "label=com.docker.compose.project=$(project_name "$id")")
  while IFS= read -r resource; do
    [[ -n $resource ]] || continue
    mount=$(docker volume inspect --format '{{.Mountpoint}}' "$resource"); value=$(du -sb "$mount" | awk '{print $1}'); [[ $value =~ ^[0-9]+$ ]] || die 'volume disk size is invalid'; bytes=$((bytes + value))
  done < <(docker volume ls -q --filter "label=com.docker.compose.project=$(project_name "$id")")
  printf 'STATUS=%s DISK_BYTES=%s\n' "$status" "$bytes"
}

load_global
docker_socket=$(root_path /var/run/docker.sock)
if [[ ${CI_FLEET_TESTING:-0} == 1 ]]; then
  [[ -f $docker_socket && ! -L $docker_socket ]] || die 'local Docker socket is unavailable'
else
  [[ -S $docker_socket && ! -L $docker_socket && $(stat -c %u "$docker_socket") == 0 ]] || die 'local root-owned Docker socket is unavailable'
fi
unset DOCKER_CONTEXT
export DOCKER_HOST="unix://$docker_socket"
[[ $(docker info --format '{{.DockerRootDir}}') == "$(root_path /var/lib/docker)" ]] || die 'Docker daemon root is not the expected local path'
if [[ ${CI_FLEET_TESTER_LOCK_FD:-} == 8 && -e /proc/$$/fd/8 && $(readlink -f /proc/$$/fd/8) == "$lock_file" ]] && flock -n 8; then
  unset CI_FLEET_TESTER_LOCK_FD
else
  install -d -m 0755 "$(dirname "$lock_file")"
  exec 9>"$lock_file"
  flock -x 9
fi
case $action in
  --check)
    docker info --format '{{.DockerRootDir}}' >/dev/null
    docker compose version >/dev/null
    getent ahosts "$probe_host" >/dev/null || die 'test-host DNS probe failed'
    curl --fail --silent --show-error --head --max-time 10 --output /dev/null "$probe_url" || die 'test-host HTTPS/proxy probe failed'
    used=$(df -P "$(root_path /var/lib/docker)" | awk 'NR==2{gsub(/%/,"",$5);print $5}')
    [[ $used =~ ^[0-9]+$ && $used -lt $disk_warn ]] || die 'Docker storage exceeds configured warning threshold'
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
    ((failed == 0)) || die 'one or more expired environments could not be removed'
    report 'CLEANUP_OK'
    ;;
  --health)
    failed=0
    for target in "$state_dir"/*.state; do [[ -e $target ]] || continue; inspect_environment "$target" | grep -q 'STATUS=running' || failed=1; done
    ((failed == 0)) || die 'one or more test environments are unhealthy'
    report 'HEALTH_OK'
    ;;
esac
