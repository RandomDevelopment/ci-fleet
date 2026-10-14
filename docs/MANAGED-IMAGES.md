# Reproducible managed images

Managed controller and runner images have two independent pins:

1. the engine checkout fixes all build instructions and checked module hashes;
2. the reviewed private desired state maps that full engine commit to exact local Docker image IDs (`sha256:...`).

Convergence captures the incumbent checkpoint, then prepares and verifies images before draining. Builds use private temporary tags and publish canonical tags only after both image IDs and revision labels match reviewed state and post-build host checks and drain succeed, within checkpoint recovery coverage. Matching reviewed image objects are reused even when canonical tags drift. A failed preparation preserves the incumbent runtime and tags. Fresh-install checkpoints retain any original canonical image IDs, protect those objects from storage cleanup, and verify their availability before rollback drains the installed controller. Reverting to a no-install checkpoint tears down the scoped candidate Compose project, including its socket proxy and networks, before restoring original tags and removing installed state. Convergence rejects an image whose revision label is correct but whose image ID differs. A missing image is rebuilt only from the pinned engine release and accepted only when its rebuilt ID equals the reviewed value. Rollback applies the same check before it starts the restored controller.

The controller launches runners by the reviewed image ID. Canonical tags remain build and rollback references.

Legacy checkpoint image evidence is stored separately from the environment restored to the host. An image-free bootstrap last-known-good configuration can be restored only through a complete protected checkpoint matching its repository, configuration commit and controller. Recovery verifies the checkpoint images, runtime and units before replacing installed state. It does not treat an arbitrary image-free configuration as a new installation.

Reconstruction requires the checkpoint release to declare `reproducible_managed_images` and runs its own `scripts/build-managed-images.sh`, retaining that release's client, backend and timestamp pins. Checkpoint image availability, any required reconstruction and exact-ID verification all complete before draining or replacing active state. Retained objects are retagged without invoking Buildx.

Pre-enforcement images have mutable inputs and must remain retained or be restored by separately authorized offline import with exact-ID verification. If a legacy object is lost, rollback fails closed with `ROLLBACK_IMAGE_REINSTALL_REQUIRED`; restore a verified saved image or separately authorize reinstall from an image-enforced configuration. Do not rebuild a legacy Dockerfile and assume its captured ID is reproducible.

Older checkpoints without a versioned image map do not retag or rebuild images. Their canonical tags must already match the checkpoint's recorded image IDs and revision before rollback drains; missing or mismatched evidence requires operator recovery.

## Reviewed upstream inputs

Base-image and runner selections updated on 2026-10-12 from official upstream endpoints. The signed Debian snapshots were reviewed on 2026-10-12. Docker package pins retain their 2026-08-16 review:

The Go build image was updated to 1.27.2 on 2026-10-13. Docker Hub tag metadata confirms the pinned multi-architecture manifest digest below, including amd64 and arm64 variants.

| Input | Immutable selection | Evidence |
| --- | --- | --- |
| Debian 13.7 slim, both managed images | `sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f` | Docker Hub registry manifest for `library/debian:13.7-slim` |
| Go 1.27.2 Bookworm build image | `sha256:5cf287a799e6b94384bad13d16b14904c531f51ba65792237e122ce42b392f61` | Docker Hub registry manifest for `library/golang:1.27.2-bookworm` |
| Debian packages | signed snapshot `20261012T000000Z` | `snapshot.debian.org/archive/debian/20261012T000000Z/dists/trixie/InRelease` |
| Build timestamps | `SOURCE_DATE_EPOCH=1786752000` | BuildKit reproducible-build timestamp input declared in every stage |
| Docker CLI 29.7.2 amd64 / arm64 | exact package version and SHA-256 `3d1a00d1549f7539606252b1e2b88ae2a0c855e02e5fa92ad962c8a118d7f6ad` / `6bb694f01789b9869e99e34df3492104c0150d12df93192034db5f88cf0f7c8c` | official Docker Debian `trixie` package index |
| Compose plugin 5.4.0 amd64 / arm64 | exact package version and SHA-256 `75391b38459edf75a975504903673fdb87a5a4016d689d9b198e2db1ae2796e0` / `81261f286d48c1c9e586065bc52065bbbe3b023705ebdb72377ec465c216dcaf` | official Docker Debian `trixie` package index |
| Actions Runner 2.338.0 | architecture-specific archive hashes in `runner/Dockerfile` | official GitHub Actions Runner release |
| Controller modules | versions plus checksums in `controller/go.mod` and `controller/go.sum` | Go checksum-verified module inputs |

The Dockerfiles fail closed if an upstream artifact no longer matches. Updating any pin requires a reviewed source change and new managed image IDs.

## Two-step desired-state rollout

Older schema-v3 engines reject `managed_images` as an unknown top-level key. Upgrade without deadlocking reconciliation:

1. Separately authorize the bootstrap on each host and stop its reconciliation timer before publishing the staging revision. The incumbent installer cannot stage the new manager. Its attempt to load the new Compose release fails closed when reviewed image IDs are missing.
2. Merge a staging desired-state revision that changes only `engine_ref` and omits `managed_images`. Upgrade mode installs and activates only the new manager; it leaves the running controller and image unchanged. Apply it with the reviewed candidate checkout's `scripts/install-worker-controller.sh --upgrade` and the approved staging configuration ref. Preserve the incumbent runtime capability evidence and all controller settings during this revision.
3. After every activation target reports `MANAGER_STAGED` for the new engine revision, merge a second reviewed revision that adds `managed_images` and its exact IDs; only this revision may build and activate the new runtime. Preserve the incumbent capability evidence for this activation commit, then update it to the verified new runtime in a follow-up revision. Restart reconciliation using the staged manager. A staging rollback may select the retained runtime engine for one controller through the same candidate installer while other controllers retain their staged refs. If unit or timer setup fails after the manager pointer moves, retry the same staging revision to reconcile it; the installer validates the retained runtime and manager tree before re-entry.

Fresh installation, field removal after enforcement, unrelated staging changes, and runtime activation without reviewed IDs all fail closed. Initial strict configuration validation requires image IDs. Image-free staging requires a complete previous configuration and an engine-ref-only transition changing at least one controller. The installer and reconciler supply predecessor context for both staging and image-policy introduction, including retained capability evidence. Configuration fetches include the immediate parent. Drift checks of the exact protected installed ref validate the retained runtime without demanding another staging transition or changing the manager pointer. They report `CHECK_RETAINED` only after the manager tree, runtime and mandatory timers pass verification.

## Record image IDs for an engine commit

From a reviewed engine checkout on an authorized isolated build host, build each host architecture twice from clean state using the reviewed BuildKit backend:

```bash
CI_FLEET_COMMIT=$(git rev-parse 'HEAD^{commit}')
CI_FLEET_VERSION=${CI_FLEET_COMMIT:0:12}
SOURCE_DATE_EPOCH=1786752000
BUILDKIT_IMAGE=moby/buildkit:v0.32.2@sha256:28a898719c18a33f4e8000685287fa36fd0dd9560c6440227d3a732d79bb41d8
BUILDER=ci-fleet-managed-review-$(</proc/sys/kernel/random/uuid)
export CI_FLEET_COMMIT CI_FLEET_VERSION SOURCE_DATE_EPOCH
cleanup() { docker buildx rm --force "$BUILDER" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker buildx create --name "$BUILDER" --driver docker-container \
  --driver-opt "image=$BUILDKIT_IMAGE" --bootstrap

docker buildx build --builder "$BUILDER" --no-cache --load \
  --provenance=false --sbom=false \
  --build-arg CI_FLEET_COMMIT --build-arg SOURCE_DATE_EPOCH \
  --tag "ci-fleet-runner:$CI_FLEET_VERSION" runner
docker buildx build --builder "$BUILDER" --no-cache --load \
  --provenance=false --sbom=false \
  --build-arg CI_FLEET_COMMIT --build-arg CI_FLEET_VERSION \
  --build-arg SOURCE_DATE_EPOCH \
  --tag "ci-fleet-controller:$CI_FLEET_VERSION" --file controller/Dockerfile .
docker image inspect --format '{{.Id}}' \
  "ci-fleet-controller:$CI_FLEET_VERSION" \
  "ci-fleet-runner:$CI_FLEET_VERSION"
```

Managed builds require the reviewed Docker Buildx client v0.32.1. Read-only checks, manager staging, maintenance that reuses matching images, uninstall and retained-image rollback do not require that plugin. Rollback that must rebuild requires the reviewed client. Host tool installation or upgrade remains separately authorized.

Managed installation creates the same digest-pinned v0.32.2 backend for every rebuild, verifies its reported version exactly, and removes only its own temporary builder afterward. Builder and staging names include a per-invocation UUID; an existing operator builder is never removed.

Repeat the build on every supported host architecture. After each architecture's repeated IDs match, add them to one private desired-state entry:

```json
"managed_images": {
  "FULL_ENGINE_COMMIT": {
    "amd64": {
      "controller": "sha256:REVIEWED_AMD64_CONTROLLER_IMAGE_ID",
      "runner": "sha256:REVIEWED_AMD64_RUNNER_IMAGE_ID"
    },
    "arm64": {
      "controller": "sha256:REVIEWED_ARM64_CONTROLLER_IMAGE_ID",
      "runner": "sha256:REVIEWED_ARM64_RUNNER_IMAGE_ID"
    }
  }
}
```

The checked-in template uses conspicuous `111...` / `222...` non-production fixture IDs only so validation and mocked tests are runnable; replace them during initialization. The repository-only marathon does not run a Docker daemon or invent production IDs, so live private configuration remains an external evidence gate.
