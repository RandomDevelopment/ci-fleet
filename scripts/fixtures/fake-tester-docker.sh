#!/usr/bin/env bash
set -Eeuo pipefail
printf '%s\n' "$*" >>"${FAKE_TESTER_DOCKER_LOG:?}"
[[ -z ${FAKE_TESTER_EVENT_LOG:-} ]] || printf 'docker %s\n' "$*" >>"$FAKE_TESTER_EVENT_LOG"
if [[ $1 == context && $2 == show ]]; then printf 'default\n'; exit 0; fi
if [[ $1 == info ]]; then printf '%s\n' "${FAKE_TESTER_DOCKER_ROOT:?}"; exit 0; fi
if [[ $1 == ps ]]; then [[ ${FAKE_TESTER_PS_FAIL:-0} != 1 ]] || exit 9; printf 'fixture-container-id\n'; exit 0; fi
if [[ $1 == inspect ]]; then
  if [[ " $* " == *' --size '* ]]; then printf '1024\n'; else printf '%s\n' "${FAKE_TESTER_CONTAINER_STATE:-running healthy}"; fi
  exit 0
fi
if [[ $1 == volume && $2 == ls ]]; then [[ ${FAKE_TESTER_VOLUME_LS_FAIL:-0} != 1 ]] || exit 9; printf 'fixture-volume\n'; exit 0; fi
if [[ $1 == volume && $2 == inspect ]]; then printf '%s\n' "${FAKE_TESTER_VOLUME_ROOT:?}"; exit 0; fi
if [[ $1 == compose && $2 == version ]]; then printf 'Docker Compose version v2.fixture\n'; exit 0; fi
if [[ $1 == compose && $2 == up && ${3:-} == --help ]]; then [[ ${FAKE_TESTER_NO_WAIT_TIMEOUT:-0} != 1 ]] && printf '%s\n' '  --wait-timeout int'; exit 0; fi
if [[ $1 != compose ]]; then exit 2; fi
shift
project=
while (($#)); do
  case $1 in
    -p) project=$2; shift 2 ;;
    -f) shift 2 ;;
    config|up|down|ps) operation=$1; shift; break ;;
    *) shift ;;
  esac
done
case ${operation:-} in
  config)
    digest=$(printf 'a%.0s' {1..64})
    privileged=false; read_only=true; host_ip=127.0.0.1; image="registry.example/example/app@sha256:$digest"; network_name="${project}_default"; secrets='{}'; service_extra=',"cpus":0.5,"mem_limit":134217728,"pids_limit":128'; top_extra=; volume_extra=; network_extra=; security='no-new-privileges:true'
    case ${FAKE_TESTER_POLICY:-valid} in
      mutable) image=registry.example/example/app:latest ;;
      privileged) privileged=true ;;
      bind) volume='{"type":"bind","source":"/","target":"/host"}' ;;
      broad-port) host_ip=0.0.0.0 ;;
      external-network) network_name=shared ;;
      environment) service_extra=',"environment":{"TOKEN":"example"}' ;;
      configs) service_extra=',"configs":[{"source":"credential"}]'; top_extra=',"configs":{"credential":{"file":"/tmp/example"}}' ;;
      use-api-socket) service_extra=',"use_api_socket":true' ;;
      namespace-share) service_extra=',"network_mode":"service:other"' ;;
      false-nnp) security='no-new-privileges:false' ;;
      custom-volume) volume_extra=',"driver":"local"' ;;
      volumes-from) service_extra=',"volumes_from":["container:other:rw"]' ;;
      custom-network) network_extra=',"driver":"macvlan","driver_opts":{"parent":"eth0"}' ;;
      ipam) network_extra=',"ipam":{"config":[{"subnet":"172.30.0.0/24"}]}' ;;
      replicas) service_extra=',"deploy":{"replicas":2}' ;;
      scale) service_extra=',"scale":2' ;;
      lifecycle-hook) service_extra=',"post_start":[{"command":"true","privileged":true}]' ;;
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
      oom-priority) service_extra=',"cpus":0.5,"mem_limit":134217728,"pids_limit":128,"oom_kill_disable":true,"oom_score_adj":-1000' ;;
      valid-secret|outside-secret) secrets=$(printf '{"credential":{"file":"%s"}}' "${FAKE_TESTER_SECRET_FILE:?}") ;;
    esac
    volume=${volume:-'{"type":"volume","source":"data","target":"/data"}'}
    printf '{"services":{"web":{"image":"%s","privileged":%s,"read_only":%s,"cap_drop":["ALL"],"security_opt":["%s"],"volumes":[%s],"ports":[{"host_ip":"%s","published":%s,"target":8080,"protocol":"tcp"}]%s}},"networks":{"default":{"name":"%s"%s}},"volumes":{"data":{"name":"%s_data"%s}},"secrets":%s%s}\n' \
      "$image" "$privileged" "$read_only" "$security" "$volume" "$host_ip" "${FAKE_TESTER_ROUTE_PORT:-18080}" "$service_extra" "$network_name" "$network_extra" "$project" "$volume_extra" "$secrets" "$top_extra"
    ;;
  up) [[ ${FAKE_TESTER_UP_FAIL:-0} != 1 ]] ;;
  down) [[ ${FAKE_TESTER_DOWN_FAIL:-0} != 1 ]] ;;
  ps) [[ ${FAKE_TESTER_UNHEALTHY:-0} == 1 ]] || printf 'fixture-container-id\n' ;;
  *) exit 2 ;;
esac
