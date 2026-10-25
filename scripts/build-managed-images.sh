#!/usr/bin/env bash
set -Eeuo pipefail
release=$1
environment=$2
manifest=${3:-}
metadata=${4:-}
version=$(docker buildx version 2>/dev/null | awk '{print $2}')
[[ "$version" == v0.32.1 ]] || { printf 'reviewed Docker Buildx v0.32.1 is required\n' >&2; exit 1; }
# A caller's ambient proxy settings must not enable a managed cache implicitly.
unset CI_FLEET_APT_HTTP_PROXY CI_FLEET_NO_PROXY
set -a
# shellcheck disable=SC1090
. "$environment"
set +a
apt_temporary=$(mktemp -d "$(dirname "$environment")/.managed-build.XXXXXX")
python3 "$release/scripts/apt_cache.py" --output "$apt_temporary/apt.conf" || { rm -rf "$apt_temporary"; exit 1; }
apt_options=()
if [[ -s "$apt_temporary/apt.conf" ]]; then
  apt_options=(--secret "id=ci_fleet_apt,src=$apt_temporary/apt.conf")
fi
build_id=$(</proc/sys/kernel/random/uuid)
managed_builder=ci-fleet-managed-$build_id
builder_created=false
managed_buildkit='moby/buildkit:v0.32.2@sha256:28a898719c18a33f4e8000685287fa36fd0dd9560c6440227d3a732d79bb41d8'
keep_staged=false
staged_runner=$CI_FLEET_RUNNER_IMAGE-staged-$build_id
staged_controller=$CI_FLEET_CONTROLLER_IMAGE-staged-$build_id
cleanup_build() {
  local image
  if [[ "$keep_staged" != true ]]; then
    for image in "$staged_runner" "$staged_controller"; do docker image rm "$image" >/dev/null 2>&1 || true; done
  fi
  if $builder_created; then docker buildx rm --force "$managed_builder" >/dev/null 2>&1 || true; fi
  rm -rf "$apt_temporary"
}
trap cleanup_build EXIT
build_image() {
  if ((${#apt_options[@]})); then
    # Build tools may echo a failed connection's private endpoint.
    if ! docker buildx build "${apt_options[@]}" "$@" >"$apt_temporary/build.log" 2>&1; then
      printf 'APT-cache-enabled managed image build failed; check cache reachability or disable the optional cache\n' >&2
      return 1
    fi
  else
    docker buildx build "$@"
  fi
}
docker buildx create --name "$managed_builder" --driver docker-container \
  --driver-opt "image=$managed_buildkit" >/dev/null
builder_created=true
[[ $(docker buildx inspect --bootstrap "$managed_builder" | awk '$1 == "BuildKit" && $2 == "version:" {print $3}') == v0.32.2 ]] \
  || { printf 'reviewed BuildKit backend v0.32.2 is unavailable\n' >&2; exit 1; }
build_image --builder "$managed_builder" --no-cache --load --provenance=false --sbom=false \
  --build-arg CI_FLEET_COMMIT --build-arg SOURCE_DATE_EPOCH=1786752000 \
  --tag "$staged_runner" "$release/runner"
build_image --builder "$managed_builder" --no-cache --load --provenance=false --sbom=false \
  --build-arg CI_FLEET_COMMIT --build-arg CI_FLEET_VERSION --build-arg SOURCE_DATE_EPOCH=1786752000 \
  --tag "$staged_controller" --file "$release/controller/Dockerfile" "$release"
portable=false
if [[ -f "$release/engine-capabilities.json" ]] && python3 - "$release/scripts" "$release/engine-capabilities.json" <<'PYTHON'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from desired_state import load_engine_capabilities
raise SystemExit("portable_managed_image_ids" not in load_engine_capabilities(Path(sys.argv[2])))
PYTHON
then
  portable=true
  if [[ -z "$metadata" ]]; then
    metadata=$apt_temporary/metadata.json
    python3 - "$metadata" "$CI_FLEET_ENGINE_REF" <<'PYTHON'
import json, platform, sys
from pathlib import Path
architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
if architecture is None:
    raise SystemExit("unsupported managed-image architecture")
Path(sys.argv[1]).write_text(json.dumps({"engine_ref": sys.argv[2], "architecture": architecture}))
PYTHON
    chmod 0600 "$metadata"
  fi
  python3 "$release/scripts/managed_image_identity.py" prepare \
    --release "$release" --environment "$environment" --metadata "$metadata" \
    --runner-reference "$staged_runner" --controller-reference "$staged_controller"
  # Reload only after the pair has passed the content proof.
  set -a
  # shellcheck disable=SC1090
  . "$environment"
  set +a
else
for mapping in "$staged_runner $CI_FLEET_RUNNER_IMAGE_DIGEST" "$staged_controller $CI_FLEET_CONTROLLER_IMAGE_DIGEST"; do
  read -r image identifier <<<"$mapping"
  [[ $(docker image inspect --format '{{.Id}}' "$image") == "$identifier" ]] \
    || { printf 'managed image digest does not match reviewed desired state\n' >&2; exit 1; }
  [[ $(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image") == "$CI_FLEET_ENGINE_REF" ]] \
    || { printf 'managed image revision does not match reviewed desired state\n' >&2; exit 1; }
done
fi
if [[ -n "$manifest" ]]; then
  (umask 0077; printf '%s\n' "$staged_runner" "$staged_controller" >"$manifest")
  keep_staged=true
else
  if $portable; then
    docker image tag "$CI_FLEET_RUNNER_RUNTIME_IMAGE_ID" "$CI_FLEET_RUNNER_IMAGE"
    docker image tag "$CI_FLEET_CONTROLLER_RUNTIME_IMAGE_ID" "$CI_FLEET_CONTROLLER_IMAGE"
  else
    docker image tag "$CI_FLEET_RUNNER_IMAGE_DIGEST" "$CI_FLEET_RUNNER_IMAGE"
    docker image tag "$CI_FLEET_CONTROLLER_IMAGE_DIGEST" "$CI_FLEET_CONTROLLER_IMAGE"
  fi
fi
