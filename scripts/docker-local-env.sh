#!/usr/bin/env bash

use_local_docker() {
  local socket=${CI_FLEET_DOCKER_SOCKET:-${CI_FLEET_ROOT_PREFIX:-}/var/run/docker.sock}
  [[ -n "$socket" ]] || socket=/var/run/docker.sock
  [[ -z ${DOCKER_HOST:-} || ${DOCKER_HOST} == "unix://$socket" ]] || {
    printf 'ERROR: alternate Docker endpoints are not supported; use the local Docker socket\n' >&2
    return 1
  }
  [[ -z ${DOCKER_CONTEXT:-} || ${DOCKER_CONTEXT} == default ]] || {
    printf 'ERROR: alternate Docker contexts are not supported; use the local Docker socket\n' >&2
    return 1
  }
  export DOCKER_HOST="unix://$socket"
  export DOCKER_CONTEXT=default
  unset DOCKER_TLS_VERIFY DOCKER_CERT_PATH DOCKER_CONFIG XDG_RUNTIME_DIR
}
