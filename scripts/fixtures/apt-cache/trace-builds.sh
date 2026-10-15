#!/usr/bin/env bash
# Loaded only in the disposable acceptance client to retain suppressed build errors.
docker() {
  if [[ ${1:-} == buildx && ${2:-} == build ]]; then
    (
      set -o pipefail
      umask 077
      command docker "$@" 2>&1 | tee "${CI_FLEET_BUILD_TRACE:?}"
    )
  else
    command docker "$@"
  fi
}
