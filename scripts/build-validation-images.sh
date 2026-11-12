#!/usr/bin/env bash
set -Eeuo pipefail
# shellcheck source=scripts/validation_diagnostics.sh
source "$(dirname "${BASH_SOURCE[0]}")/validation_diagnostics.sh"
# The cache comparison reuses this direct pair, with identical managed build inputs.
builder=ci-fleet-validation-$(</proc/sys/kernel/random/uuid)
cleanup() { docker buildx rm --force "$builder" >/dev/null; }
docker buildx create --name "$builder" --driver docker-container \
  --driver-opt image=moby/buildkit:v0.32.2@sha256:28a898719c18a33f4e8000685287fa36fd0dd9560c6440227d3a732d79bb41d8 >/dev/null
trap cleanup EXIT
[[ $(docker buildx inspect --bootstrap "$builder" | awk '$1 == "BuildKit" && $2 == "version:" {print $3}') == v0.32.2 ]] || exit 1
build=(docker buildx build --builder "$builder" --no-cache --load --provenance=false --sbom=false
  --build-arg "CI_FLEET_COMMIT=${CI_FLEET_COMMIT:?}" --build-arg SOURCE_DATE_EPOCH=1786752000)
validation_command runner-reference "${build[@]}" --tag "${CI_FLEET_RUNNER_IMAGE:?}" runner
validation_command controller-reference "${build[@]}" --build-arg "CI_FLEET_VERSION=${CI_FLEET_VERSION:?}" \
  --tag "${CI_FLEET_CONTROLLER_IMAGE:?}" --file controller/Dockerfile .
