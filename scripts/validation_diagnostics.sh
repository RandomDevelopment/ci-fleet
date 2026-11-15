#!/usr/bin/env bash
# Diagnostic records only: no traps, environment dumps, retries or deadline changes.
validation_record() {
  TZ=UTC printf 'VALIDATION %(%Y-%m-%dT%H:%M:%SZ)T %s\n' -1 "$*" >&2 || :
}

# Call external commands, not shell functions: conditional invocation must not
# suppress errexit inside a caller's function body. Child scripts keep their flags.
validation_command() {
  local id=$1 result
  shift
  validation_record "BEGIN $id"
  if "$@"; then result=0; else result=$?; fi
  validation_record "END $id exit=$result"
  return "$result"
}

# Fixed fixture identifiers survive child output redirection and scratch cleanup.
validation_case() {
  validation_record "CASE $1"
}
