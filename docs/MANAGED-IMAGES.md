# Reproducible managed images

Managed controller and runner images have two independent pins:

1. the engine checkout fixes all build instructions and checked module hashes;
2. the reviewed private desired state maps that full engine commit to exact local Docker image IDs (`sha256:...`).

Convergence rejects an image whose revision label is correct but whose image ID differs. A missing image is rebuilt only from the pinned engine release and accepted only when its rebuilt ID equals the reviewed value. Rollback applies the same check and deterministic rebuild before it starts the restored controller.

## Reviewed upstream inputs

Reviewed on 2026-08-16 from official upstream endpoints:

| Input | Immutable selection | Evidence |
| --- | --- | --- |
| Debian 13.6 slim, both managed images | `sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258` | Docker Hub registry manifest for `library/debian:13.6-slim` |
| Go 1.26.5 Bookworm build image | `sha256:53eeac89074db483fdf0ab3be1df32bf6e47562263d2d0d6baa7f26acb4957dd` | Docker Hub registry manifest for `library/golang:1.26.5-bookworm` |
| Debian packages | signed snapshot `20260815T000000Z` | `snapshot.debian.org/archive/debian/20260815T000000Z/dists/trixie/InRelease` |
| Build timestamps | `SOURCE_DATE_EPOCH=1786752000` | BuildKit reproducible-build timestamp input declared in every stage |
| Docker CLI 29.7.2 amd64 / arm64 | exact package version and SHA-256 `3d1a00d1549f7539606252b1e2b88ae2a0c855e02e5fa92ad962c8a118d7f6ad` / `6bb694f01789b9869e99e34df3492104c0150d12df93192034db5f88cf0f7c8c` | official Docker Debian `trixie` package index |
| Compose plugin 5.4.0 amd64 / arm64 | exact package version and SHA-256 `75391b38459edf75a975504903673fdb87a5a4016d689d9b198e2db1ae2796e0` / `81261f286d48c1c9e586065bc52065bbbe3b023705ebdb72377ec465c216dcaf` | official Docker Debian `trixie` package index |
| Actions Runner 2.335.1 | architecture-specific archive hashes in `runner/Dockerfile` | official GitHub Actions Runner release |
| Controller modules | versions plus checksums in `controller/go.mod` and `controller/go.sum` | Go checksum-verified module inputs |

The Dockerfiles fail closed if an upstream artifact no longer matches. Updating any pin requires a reviewed source change and new managed image IDs.

## Record image IDs for an engine commit

From a reviewed engine checkout on an authorized isolated build host, build both images twice from clean BuildKit state and compare IDs:

```bash
CI_FLEET_COMMIT=$(git rev-parse 'HEAD^{commit}')
CI_FLEET_VERSION=$(git rev-parse --short=12 HEAD)
export CI_FLEET_COMMIT CI_FLEET_VERSION
docker compose -f deploy/compose.yaml build --no-cache runner-image controller
docker image inspect --format '{{.Id}}' \
  "ci-fleet-controller:${CI_FLEET_COMMIT:0:12}" \
  "ci-fleet-runner:${CI_FLEET_COMMIT:0:12}"
```

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
