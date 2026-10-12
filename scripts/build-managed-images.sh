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
cleanup_builder() { docker buildx rm --force "$managed_builder" >/dev/null 2>&1 || true; }
trap cleanup_builder EXIT
cleanup_builder
docker buildx create --name "$managed_builder" --driver docker-container \
  --driver-opt "image=$managed_buildkit" --bootstrap >/dev/null
[[ $(docker buildx inspect "$managed_builder" | awk '$1 == "BuildKit:" {print $2; exit}') == v0.32.2 ]] \
  || { printf 'reviewed BuildKit backend v0.32.2 is unavailable\n' >&2; exit 1; }
docker buildx build --builder "$managed_builder" --no-cache --load --provenance=false --sbom=false \
  --build-arg CI_FLEET_COMMIT --build-arg SOURCE_DATE_EPOCH=1786752000 \
  --tag "$CI_FLEET_RUNNER_IMAGE" "$release/runner"
docker buildx build --builder "$managed_builder" --no-cache --load --provenance=false --sbom=false \
  --build-arg CI_FLEET_COMMIT --build-arg CI_FLEET_VERSION --build-arg SOURCE_DATE_EPOCH=1786752000 \
  --tag "$CI_FLEET_CONTROLLER_IMAGE" --file "$release/controller/Dockerfile" "$release"
