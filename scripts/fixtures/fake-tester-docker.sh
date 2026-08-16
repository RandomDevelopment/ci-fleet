#!/usr/bin/env bash
set -Eeuo pipefail
printf '%s\n' "$*" >>"${FAKE_TESTER_DOCKER_LOG:?}"
if [[ $1 == context && $2 == show ]]; then printf 'default\n'; exit 0; fi
if [[ $1 == info ]]; then printf '%s\n' "${FAKE_TESTER_DOCKER_ROOT:?}"; exit 0; fi
if [[ $1 == ps ]]; then printf 'fixture-container-id\n'; exit 0; fi
if [[ $1 == inspect ]]; then printf '1024\n'; exit 0; fi
if [[ $1 == volume && $2 == ls ]]; then printf 'fixture-volume\n'; exit 0; fi
if [[ $1 == volume && $2 == inspect ]]; then printf '%s\n' "${FAKE_TESTER_VOLUME_ROOT:?}"; exit 0; fi
if [[ $1 == compose && $2 == version ]]; then printf 'Docker Compose version v2.fixture\n'; exit 0; fi
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
    privileged=false; read_only=true; host_ip=127.0.0.1; image="registry.example/example/app@sha256:$digest"; network_name="${project}_default"; secrets='{}'
    case ${FAKE_TESTER_POLICY:-valid} in
      mutable) image=registry.example/example/app:latest ;;
      privileged) privileged=true ;;
      bind) volume='{"type":"bind","source":"/","target":"/host"}' ;;
      broad-port) host_ip=0.0.0.0 ;;
      external-network) network_name=shared ;;
      valid-secret|outside-secret) secrets=$(printf '{"credential":{"file":"%s"}}' "${FAKE_TESTER_SECRET_FILE:?}") ;;
    esac
    volume=${volume:-'{"type":"volume","source":"data","target":"/data"}'}
    printf '{"services":{"web":{"image":"%s","privileged":%s,"read_only":%s,"cap_drop":["ALL"],"security_opt":["no-new-privileges:true"],"volumes":[%s],"ports":[{"host_ip":"%s","published":%s,"target":8080,"protocol":"tcp"}]}},"networks":{"default":{"name":"%s"}},"volumes":{"data":{"name":"%s_data"}},"secrets":%s}\n' \
      "$image" "$privileged" "$read_only" "$volume" "$host_ip" "${FAKE_TESTER_ROUTE_PORT:-18080}" "$network_name" "$project" "$secrets"
    ;;
  up|down) ;;
  ps) [[ ${FAKE_TESTER_UNHEALTHY:-0} == 1 ]] || printf 'fixture-container-id\n' ;;
  *) exit 2 ;;
esac
