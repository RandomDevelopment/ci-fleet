#!/usr/bin/env bash
set -Eeuo pipefail

apply=false
instance="${CI_FLEET_INSTANCE:-}"

usage() {
  echo "usage: $0 [--apply] [--instance NAME]" >&2
}

while (($#)); do
  case "$1" in
    --apply) apply=true ;;
    --instance) shift; instance="${1:-}" ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
  shift
done

command -v docker >/dev/null || { echo "ERROR docker is unavailable" >&2; exit 1; }
docker info >/dev/null

label_prefix=io.randomdevelopment.ci-fleet.
filters=(--filter "label=${label_prefix}managed=true")
if [[ -n "$instance" ]]; then
  filters+=(--filter "label=${label_prefix}instance=${instance}")
fi
now=$(date +%s)
found=0

while IFS= read -r id; do
  [[ -n "$id" ]] || continue
  state=$(docker inspect --format '{{.State.Status}}' "$id")
  expires=$(docker inspect --format "{{index .Config.Labels \"${label_prefix}expires-at\"}}" "$id")
  name=$(docker inspect --format '{{.Name}}' "$id")
  if [[ ! "$expires" =~ ^[0-9]+$ ]] || ((expires > now)); then continue; fi
  found=$((found + 1))
  if [[ "$state" == running || "$state" == restarting || "$state" == paused ]]; then
    echo "KEEP container ${name#/} state=$state expired=$expires (routine cleanup never removes active containers)"
    continue
  fi
  if $apply; then
    echo "REMOVE container ${name#/} state=$state expired=$expires"
    docker rm -v "$id"
  else
    echo "WOULD_REMOVE container ${name#/} state=$state expired=$expires"
  fi
done < <(docker ps -aq "${filters[@]}")

for kind in volume network; do
  while IFS= read -r name; do
    [[ -n "$name" ]] || continue
    expires=$(docker "$kind" inspect --format "{{index .Labels \"${label_prefix}expires-at\"}}" "$name")
    if [[ ! "$expires" =~ ^[0-9]+$ ]] || ((expires > now)); then continue; fi
    found=$((found + 1))
    if $apply; then
      echo "REMOVE $kind $name expired=$expires"
      docker "$kind" rm "$name"
    else
      echo "WOULD_REMOVE $kind $name expired=$expires"
    fi
  done < <(docker "$kind" ls -q "${filters[@]}")
done

if ((!found)); then echo "OK no expired ci-fleet resources found"; fi
if ! $apply; then echo "DRY_RUN no changes made; pass --apply to remove listed inactive resources"; fi
