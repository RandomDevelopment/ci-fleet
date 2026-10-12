#!/usr/bin/env bash
set -Eeuo pipefail
printf '%s\n' "$*" >>"${FAKE_TESTER_DOCKER_LOG:?}"
[[ -z ${FAKE_TESTER_EVENT_LOG:-} ]] || printf 'docker %s\n' "$*" >>"$FAKE_TESTER_EVENT_LOG"
if [[ $1 == ps || ( $1 == volume || $1 == network ) && $2 == ls ]] && [[ " $* " == *' label=com.docker.compose.project=ci-fleet-test-'* ]]; then
  project=${*: -1}; project=${project#label=com.docker.compose.project=}
  kind=$1; [[ $kind != ps ]] || kind=container
  case $kind in
    container) [[ ${FAKE_TESTER_PS_FAIL:-0} != 1 ]] || exit 9 ;;
    volume) [[ ${FAKE_TESTER_VOLUME_LS_FAIL:-0} != 1 ]] || exit 9 ;;
    network) [[ ${FAKE_TESTER_NETWORK_LS_FAIL:-0} != 1 ]] || exit 9 ;;
  esac
  resource_state=${FAKE_TESTER_RESOURCE_STATE:?}/$project
  if [[ -f $resource_state && ( $(<"$resource_state") == all || $(<"$resource_state") == "$kind" ) ]]; then printf 'fixture-%s\n' "$kind"; fi
  exit 0
fi
if [[ " $* " == *'|{{.Label "com.docker.compose.project"}}'* ]]; then
  [[ -z ${FAKE_TESTER_UNRELATED_RESOURCE:-} ]] || printf '%s\n' "$FAKE_TESTER_UNRELATED_RESOURCE"
  exit 0
fi
if [[ $1 == context && $2 == show ]]; then printf 'default\n'; exit 0; fi
if [[ $1 == info ]]; then
  if [[ " $* " == *'len .DefaultAddressPools'* ]]; then
    [[ ${FAKE_TESTER_EMPTY_ADDRESS_POOLS:-0} == 1 ]] && printf '0\n' || printf '1\n'
  elif [[ " $* " == *'DefaultAddressPools'* ]]; then
    if [[ ${FAKE_TESTER_MIXED_ADDRESS_POOLS:-0} == 1 ]]; then printf '[{"Base":"2001::/16","Size":28},{"Base":"198.51.100.0/24","Size":28}]\n';
    else printf '[{"Base":"198.51.100.0/24","Size":28}]\n'; fi
  else printf '%s\n' "${FAKE_TESTER_DOCKER_ROOT:?}"; fi
  exit 0
fi
if [[ $1 == ps ]]; then
  [[ ${FAKE_TESTER_PS_FAIL:-0} != 1 ]] || exit 9
  if [[ " $* " == *' label=com.docker.compose.project '* ]]; then
    [[ -z ${FAKE_TESTER_ORPHAN_PROJECT:-} ]] || printf 'fixture-orphan-container-id\n'
  else
    printf 'fixture-container-id\n'
  fi
  exit 0
fi
if [[ $1 == inspect ]]; then
  if [[ " $* " == *' --size '* ]]; then
    printf '1024\n'
  elif [[ " $* " == *'com.docker.compose.project'* ]]; then
    printf '%s\n' "${FAKE_TESTER_ORPHAN_PROJECT:-}"
  elif [[ " $* " == *'com.docker.compose.service'* && " $* " == *'.Config.Image'* ]]; then
    service=web
    [[ ${*: -1} != fixture-worker-id || ${FAKE_TESTER_DUPLICATE_SERVICE:-0} == 1 ]] || service=worker
    printf '%s %s\n' "$service" "${FAKE_TESTER_CONTAINER_IMAGE:-registry.example/example/app@sha256:$(printf 'a%.0s' {1..64})}"
  elif [[ " $* " != *' --format '* ]]; then
    project=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["name"])' "$(<"${FAKE_TESTER_MODEL_PATH:?}")")
    privileged=false; nano_cpus=500000000; memory=134217728; memory_swap=268435456; pids_limit=128; oom_disable=false; oom_score=0; network_mode=${project}_default; networks=$(printf '{"%s":{}}' "$network_mode"); mounts=$(printf '[{"Type":"volume","Name":"%s_data","Destination":"/data","RW":true}]' "$project"); ports=$(printf '{"8080/tcp":[{"HostIp":"127.0.0.1","HostPort":"%s"}]}' "${FAKE_TESTER_ROUTE_PORT:-18080}")
    host_extra=
    case ${FAKE_TESTER_LIVE_POLICY:-valid} in
      device-cgroup) host_extra=',"DeviceCgroupRules":["b 8:* rwm"]' ;;
      devices) host_extra=',"Devices":[{"PathOnHost":"/dev/sda","PathInContainer":"/dev/sda","CgroupPermissions":"rwm"}]' ;;
      device-requests) host_extra=',"DeviceRequests":[{"Driver":"nvidia","Count":-1,"Capabilities":[["gpu"]]}]' ;;
      privileged) privileged=true ;;
      bind) mounts='[{"Type":"bind","Source":"/","Destination":"/host"}]' ;;
      broad-port) ports=$(printf '{"8080/tcp":[{"HostIp":"0.0.0.0","HostPort":"%s"}]}' "${FAKE_TESTER_ROUTE_PORT:-18080}") ;;
      unlimited-swap) memory_swap=-1 ;;
      unbounded) nano_cpus=0; memory=0; pids_limit=0 ;;
      wrong-volume) mounts='[{"Type":"volume","Name":"ci-fleet-test-other_data","Destination":"/data","RW":true}]' ;;
      host-network) network_mode=host ;;
      extra-network) networks=$(printf '{"%s":{},"other-environment":{}}' "$network_mode") ;;
      oom-disabled) oom_disable=true ;;
      oom-priority) oom_score=-1000 ;;
    esac
    [[ ${FAKE_TESTER_POLICY:-valid} != finite-swap ]] || memory_swap=402653184
    if [[ -n ${FAKE_TESTER_SECRET_FILE:-} ]]; then
      secret_target=${FAKE_TESTER_SECRET_TARGET:-/run/secrets/credential}
      [[ $secret_target != credential ]] || secret_target=/run/secrets/credential
      mounts=${mounts%]}$(printf ',{"Type":"bind","Source":"%s","Destination":"%s","RW":false}]' "$FAKE_TESTER_SECRET_FILE" "$secret_target")
    fi
    if [[ ${*: -1} == fixture-worker-id ]]; then mounts='[]'; ports='{}'; fi
    printf '[{"HostConfig":{"Privileged":%s,"ReadonlyRootfs":true,"CapDrop":["ALL"],"SecurityOpt":["no-new-privileges:true"],"NanoCpus":%s,"Memory":%s,"MemorySwap":%s,"PidsLimit":%s,"PortBindings":%s,"OomKillDisable":%s,"OomScoreAdj":%s,"NetworkMode":"%s"%s},"NetworkSettings":{"Networks":%s},"Mounts":%s}]\n' "$privileged" "$nano_cpus" "$memory" "$memory_swap" "$pids_limit" "$ports" "$oom_disable" "$oom_score" "$network_mode" "$host_extra" "$networks" "$mounts"
  else
    printf '%s\n' "${FAKE_TESTER_CONTAINER_STATE:-running healthy}"
  fi
  exit 0
fi
if [[ $1 == volume && $2 == ls ]]; then
  [[ ${FAKE_TESTER_VOLUME_LS_FAIL:-0} != 1 ]] || exit 9
  if [[ " $* " == *' name=^'* ]]; then
    name=${*: -1}; name=${name#name=^}; name=${name%\$}
    [[ -z ${FAKE_TESTER_PREEXISTING_VOLUME:-} ]] || { printf '%s\n' "$FAKE_TESTER_PREEXISTING_VOLUME"; exit 0; }
    id=${name#ci-fleet-test-}; id=${id%_data}
    [[ ! -f ${CI_FLEET_ROOT_PREFIX:?}/var/lib/ci-fleet-tester/environments/$id.state ]] || printf '%s\n' "$name"
  elif [[ " $* " == *' label=com.docker.compose.project=ci-fleet-test-'* ]]; then printf 'fixture-volume\n';
  elif [[ -n ${FAKE_TESTER_ORPHAN_VOLUME_PROJECT:-} ]]; then printf 'fixture-orphan-volume-id\n'; fi
  exit 0
fi
if [[ $1 == volume && $2 == inspect ]]; then
  if [[ -n ${FAKE_TESTER_PREEXISTING_VOLUME:-} && ${*: -1} == "$FAKE_TESTER_PREEXISTING_VOLUME" && " $* " != *' --format '* ]]; then
    printf '[{"Name":"%s","Driver":"local","Options":{"type":"none","device":"/","o":"bind"},"Labels":{"com.docker.compose.project":"ci-fleet-test-preview-a","com.docker.compose.volume":"data"}}]\n' "$FAKE_TESTER_PREEXISTING_VOLUME"
  elif [[ " $* " == *'com.docker.compose.project'* ]]; then printf '%s\n' "${FAKE_TESTER_ORPHAN_VOLUME_PROJECT:-}";
  elif [[ " $* " != *' --format '* ]]; then
    options='{}'; [[ ${FAKE_TESTER_LIVE_POLICY:-} != bind-volume ]] || options='{"type":"none","device":"/","o":"bind"}'
    name=${*: -1}; project=${name%_data}
    printf '[{"Name":"%s","Driver":"local","Options":%s,"Labels":{"com.docker.compose.project":"%s","com.docker.compose.volume":"data"}}]\n' "$name" "$options" "$project"
  else printf '%s\n' "${FAKE_TESTER_VOLUME_ROOT:?}"; fi
  exit 0
fi
if [[ $1 == network && $2 == ls ]]; then
  if [[ " $* " == *' name=^'* ]]; then
    name=${*: -1}; name=${name#name=^}; name=${name%\$}; id=${name#ci-fleet-test-}; id=${id%_default}
    if [[ -n ${FAKE_TESTER_PREEXISTING_NETWORK:-} ]]; then printf '%s\n' "$FAKE_TESTER_PREEXISTING_NETWORK";
    elif [[ -f ${CI_FLEET_ROOT_PREFIX:?}/var/lib/ci-fleet-tester/environments/$id.state ]]; then printf '%s\n' "$name"; fi
  elif [[ -n ${FAKE_TESTER_ORPHAN_NETWORK_PROJECT:-} ]]; then printf 'fixture-orphan-network-id\n'; fi
  exit 0
fi
if [[ $1 == network && $2 == inspect ]]; then
  if [[ -n ${FAKE_TESTER_PREEXISTING_NETWORK:-} && ${*: -1} == "$FAKE_TESTER_PREEXISTING_NETWORK" && " $* " != *' --format '* ]]; then
    printf '[{"Name":"%s","Driver":"bridge","Options":{},"Labels":{"com.docker.compose.project":"ci-fleet-test-preview-a","com.docker.compose.network":"default"},"Internal":false,"Attachable":false,"Ingress":false,"EnableIPv6":false,"IPAM":{"Driver":"default","Options":{},"Config":[{"Subnet":"203.0.113.0/24","Gateway":"203.0.113.1"}]}}]\n' "$FAKE_TESTER_PREEXISTING_NETWORK"
  elif [[ " $* " != *' --format '* ]]; then
    name=${*: -1}; project=${name%_default}
    printf '[{"Name":"%s","Driver":"bridge","Options":{},"Labels":{"com.docker.compose.project":"%s","com.docker.compose.network":"default"},"Internal":false,"Attachable":false,"Ingress":false,"EnableIPv6":false,"IPAM":{"Driver":"default","Options":{},"Config":[{"Subnet":"198.51.100.0/28","Gateway":"198.51.100.1"}]}}]\n' "$name" "$project"
  else printf '%s\n' "${FAKE_TESTER_ORPHAN_NETWORK_PROJECT:-}"; fi
  exit 0
fi
if [[ $1 == compose && $2 == version ]]; then printf 'Docker Compose version v2.fixture\n'; exit 0; fi
if [[ $1 == compose && $2 == up && ${3:-} == --help ]]; then [[ ${FAKE_TESTER_NO_WAIT_TIMEOUT:-0} != 1 ]] && printf '%s\n' '  --wait-timeout int'; exit 0; fi
if [[ $1 != compose ]]; then exit 2; fi
shift
project=
while (($#)); do
  case $1 in
    -p) project=$2; shift 2 ;;
    -f) model=$2; shift 2 ;;
    config|up|down|ps) operation=$1; shift; break ;;
    *) shift ;;
  esac
done
case ${operation:-} in
  config)
    digest=$(printf 'a%.0s' {1..64})
    privileged=false; read_only=true; host_ip=127.0.0.1; protocol=tcp; image="registry.example/example/app@sha256:$digest"; network_name="${project}_default"; secrets='{}'; service_extra=',"cpus":0.5,"mem_limit":134217728,"pids_limit":128'; extra_services=; top_extra=; volume_extra=; network_extra=; security='no-new-privileges:true'
    case ${FAKE_TESTER_POLICY:-valid} in
      mutable) image=registry.example/example/app:latest ;;
      privileged) privileged=true ;;
      bind) volume='{"type":"bind","source":"/","target":"/host"}' ;;
      broad-port) host_ip=0.0.0.0 ;;
      udp-route) protocol=udp ;;
      external-network) network_name=shared ;;
      environment) service_extra=',"environment":{"TOKEN":"example"}' ;;
      configs) service_extra=',"configs":[{"source":"credential"}]'; top_extra=',"configs":{"credential":{"file":"/tmp/example"}}' ;;
      use-api-socket) service_extra=',"use_api_socket":true' ;;
      namespace-share) service_extra=',"network_mode":"service:other"' ;;
      false-nnp) security='no-new-privileges:false' ;;
      custom-volume) volume_extra=',"driver":"local"' ;;
      volumes-from) service_extra=',"volumes_from":["container:other:rw"]' ;;
      custom-network) network_extra=',"driver":"macvlan","driver_opts":{"parent":"eth0"}' ;;
      device-cgroup) service_extra+=',"device_cgroup_rules":["b 8:* rwm"]' ;;
      duplicate-network) network_extra=$(printf '},"alias":{"name":"%s_default"' "$project") ;;
      duplicate-volume) volume_extra=$(printf '},"alias":{"name":"%s_data"' "$project") ;;
      ipv4-disabled) network_extra=',"enable_ipv4":false,"enable_ipv6":true' ;;
      service-tmpfs) service_extra+=',"tmpfs":["/tmp"]' ;;
      ipam) network_extra=',"ipam":{"config":[{"subnet":"172.30.0.0/24"}]}' ;;
      replicas) service_extra=',"deploy":{"replicas":2}' ;;
      scale) service_extra=',"scale":2' ;;
      lifecycle-hook) service_extra=',"post_start":[{"command":"true","privileged":true}]' ;;
      provider) service_extra+=',"provider":{"type":"unapproved-host-executable"}' ;;
      pre-start) service_extra=',"pre_start":[{"image":"registry.example/example/init:latest","privileged":true,"command":"true"}]' ;;
      models) service_extra=',"models":["unapproved"]'; top_extra=',"models":{"unapproved":{"model":"example/model:latest"}}' ;;
      service-models) service_extra=',"models":["unapproved"]' ;;
      top-models) top_extra=',"models":{"unapproved":{"model":"example/model:latest"}}' ;;
      anonymous-volume) volume='{"type":"volume","target":"/data"}' ;;
      undeclared-volume) volume='{"type":"volume","source":"missing","target":"/data"}' ;;
      gpu) service_extra=',"gpus":"all"' ;;
      deploy-device) service_extra=',"deploy":{"resources":{"reservations":{"devices":[{"capabilities":["gpu"]}]}}}' ;;
      build) service_extra=',"build":{"context":"/"}' ;;
      changed-model) service_extra=',"pull_policy":"always"' ;;
      unconfined) security='no-new-privileges:true","seccomp=unconfined' ;;
      external-links) service_extra=',"external_links":["other-environment-db:db"]' ;;
      userns-host) service_extra=',"userns_mode":"host"' ;;
      cgroup-host) service_extra=',"cgroup":"host"' ;;
      uts-host) service_extra=',"uts":"host"' ;;
      remote-logging) service_extra=',"logging":{"driver":"syslog","options":{"syslog-address":"tcp://example.invalid:514"}}' ;;
      interpolation) [[ -z ${TOKEN:-} ]] || service_extra=$(printf ',"command":["app","--token=%s"]' "$TOKEN") ;;
      profiles) service_extra=',"profiles":["extra"]' ;;
      label-file) service_extra=',"label_file":"/root/credential.env"' ;;
      runtime) service_extra=',"runtime":"alternative"' ;;
      unbounded) service_extra=',"cpus":0,"mem_limit":0,"pids_limit":-1' ;;
      unlimited-swap) service_extra+=',"memswap_limit":-1' ;;
      finite-swap) service_extra+=',"memswap_limit":402653184' ;;
      oom-priority) service_extra=',"cpus":0.5,"mem_limit":134217728,"pids_limit":128,"oom_kill_disable":true,"oom_score_adj":-1000' ;;
      two-services) extra_services=$(printf ',"worker":{"image":"%s","privileged":false,"read_only":true,"cap_drop":["ALL"],"security_opt":["no-new-privileges:true"],"volumes":[],"cpus":0.5,"mem_limit":134217728,"pids_limit":128}' "$image") ;;
      valid-secret|outside-secret)
        secrets=$(printf '{"credential":{"file":"%s"}}' "${FAKE_TESTER_SECRET_FILE:?}")
        service_extra+=$(printf ',"secrets":[{"source":"credential","target":"%s"}]' "${FAKE_TESTER_SECRET_TARGET:-credential}")
        ;;
    esac
    volume=${volume:-'{"type":"volume","source":"data","target":"/data"}'}
    printf '{"name":"%s","services":{"web":{"image":"%s","privileged":%s,"read_only":%s,"cap_drop":["ALL"],"security_opt":["%s"],"volumes":[%s],"ports":[{"host_ip":"%s","published":%s,"target":8080,"protocol":"%s"}]%s}%s},"networks":{"default":{"name":"%s"%s}},"volumes":{"data":{"name":"%s_data"%s}},"secrets":%s%s}\n' \
      "$project" "$image" "$privileged" "$read_only" "$security" "$volume" "$host_ip" "${FAKE_TESTER_ROUTE_PORT:-18080}" "$protocol" "$service_extra" "$extra_services" "$network_name" "$network_extra" "$project" "$volume_extra" "$secrets" "$top_extra"
    ;;
  up)
    printf 'all\n' >"${FAKE_TESTER_RESOURCE_STATE:?}/$project"
    case ${FAKE_TESTER_UP_SIGNAL:-} in
      TERM) kill -TERM "$PPID"; exit 143 ;;
      KILL) kill -KILL "$PPID"; exit 137 ;;
    esac
    [[ ${FAKE_TESTER_UP_FAIL:-0} != 1 ]]
    ;;
  down)
    [[ ${FAKE_TESTER_DOWN_FAIL:-0} != 1 ]] || exit 1
    if [[ -n ${FAKE_TESTER_DOWN_LEAVES:-} ]]; then printf '%s\n' "$FAKE_TESTER_DOWN_LEAVES" >"${FAKE_TESTER_RESOURCE_STATE:?}/$project";
    else rm -f "${FAKE_TESTER_RESOURCE_STATE:?}/$project"; fi
    ;;
  ps)
    printf '%s\n' "$model" >"${FAKE_TESTER_MODEL_PATH:?}"
    [[ ${FAKE_TESTER_UNHEALTHY:-0} == 1 ]] || {
      printf 'fixture-container-id\n'
      [[ ${FAKE_TESTER_POLICY:-valid} != two-services || $* != -q ]] || printf 'fixture-worker-id\n'
    }
    ;;
  *) exit 2 ;;
esac
