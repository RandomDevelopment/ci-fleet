#!/usr/bin/env bash
set -Eeuo pipefail
release=$1
environment=$2
version=$(docker buildx version 2>/dev/null | awk '{print $2}')
[[ "$version" == v0.32.1 ]] || { printf 'reviewed Docker Buildx v0.32.1 is required\n' >&2; exit 1; }
set -a
# shellcheck disable=SC1090
. "$environment"
set +a
managed_builder=ci-fleet-managed
managed_buildkit='moby/buildkit:v0.32.2@sha256:28a898719c18a33f4e8000685287fa36fd0dd9560c6440227d3a732d79bb41d8'
staged_runner=$CI_FLEET_RUNNER_IMAGE-staged-$$
staged_controller=$CI_FLEET_CONTROLLER_IMAGE-staged-$$
cleanup_build() {
  local image
  for image in "$staged_runner" "$staged_controller"; do docker image rm "$image" >/dev/null 2>&1 || true; done
  docker buildx rm --force "$managed_builder" >/dev/null 2>&1 || true
}
trap cleanup_build EXIT
cleanup_build
docker buildx create --name "$managed_builder" --driver docker-container \
  --driver-opt "image=$managed_buildkit" --bootstrap >/dev/null
[[ $(docker buildx inspect "$managed_builder" | awk '$1 == "BuildKit:" {print $2; exit}') == v0.32.2 ]] \
  || { printf 'reviewed BuildKit backend v0.32.2 is unavailable\n' >&2; exit 1; }
docker buildx build --builder "$managed_builder" --no-cache --load --provenance=false --sbom=false \
  --build-arg CI_FLEET_COMMIT --build-arg SOURCE_DATE_EPOCH=1786752000 \
  --tag "$staged_runner" "$release/runner"
docker buildx build --builder "$managed_builder" --no-cache --load --provenance=false --sbom=false \
  --build-arg CI_FLEET_COMMIT --build-arg CI_FLEET_VERSION --build-arg SOURCE_DATE_EPOCH=1786752000 \
  --tag "$staged_controller" --file "$release/controller/Dockerfile" "$release"
for mapping in "$staged_runner $CI_FLEET_RUNNER_IMAGE_DIGEST" "$staged_controller $CI_FLEET_CONTROLLER_IMAGE_DIGEST"; do
  read -r image identifier <<<"$mapping"
  [[ $(docker image inspect --format '{{.Id}}' "$image") == "$identifier" ]] \
    || { printf 'managed image digest does not match reviewed desired state\n' >&2; exit 1; }
  [[ $(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image") == "$CI_FLEET_ENGINE_REF" ]] \
    || { printf 'managed image revision does not match reviewed desired state\n' >&2; exit 1; }
done
docker image tag "$CI_FLEET_RUNNER_IMAGE_DIGEST" "$CI_FLEET_RUNNER_IMAGE"
docker image tag "$CI_FLEET_CONTROLLER_IMAGE_DIGEST" "$CI_FLEET_CONTROLLER_IMAGE"
