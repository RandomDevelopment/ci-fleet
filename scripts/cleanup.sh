#!/usr/bin/env bash
set -Eeuo pipefail
export PYTHONDONTWRITEBYTECODE=1

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck disable=SC1091
source "$repo_root/scripts/docker-local-env.sh"
use_local_docker

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

use_local_docker

command -v docker >/dev/null || { echo "ERROR docker is unavailable" >&2; exit 1; }

args=(--instance "$instance")
if $apply; then args+=(--apply); fi
exec python3 "$repo_root/scripts/cleanup_storage.py" "${args[@]}"
