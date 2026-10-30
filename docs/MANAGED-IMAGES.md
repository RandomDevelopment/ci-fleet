# Reproducible managed images

Managed controller and runner images have two independent pins. The engine
checkout fixes build instructions and module hashes. Reviewed desired state maps
that full engine commit and architecture to scalar `sha256:...` values.

The selected runtime source declares their meaning through
`portable_managed_image_ids` in `engine-capabilities.json`. With that capability,
each scalar is the SHA-256 of the original image config bytes, called C here.
Without it, each scalar remains an exact opaque local Docker image ID. A newer
manager does not reinterpret an older engine's values. Digest syntax, API version
and backend names never choose policy semantics. Keep historical entries and
checkpoint IDs unchanged.

## Portable content verification

For portable sources, the root installer proves both roles before publishing
any tag, installing runtime environment, draining or activating a controller.
It inspects a candidate tag once, obtains immutable full Docker ID R, and exports
by full R without a platform filter. Classic R equals C. A containerd R can be an
original manifest or singleton index whose selected manifest binds C. The export's
`index.json` is a generated wrapper. Its hash is not the original root identity.
The verifier hashes original config bytes and authenticates the original
R-to-manifest-to-C descriptor chain, source revision and exact Linux architecture.
Inspect JSON serialization and config-ID lookups are not content proof.

The first protocol supports one runnable platform per role, a direct manifest or
one original singleton index, and regular tar entries. It consumes the complete
bounded response, validates tar checksums and ending, hashes digest-addressed
blobs and checks selected layer closure. Duplicate JSON keys or archive names,
links, unsafe paths, extensions, incomplete descriptors, corruption, truncation,
HTTP framing failures and trailing daemon errors fail preparation. Layers are
streamed without spooling. The proof does not claim root-filesystem replay.

`CI_FLEET_CONTROLLER_IMAGE_DIGEST` and `CI_FLEET_RUNNER_IMAGE_DIGEST` retain C.
The installer derives `CI_FLEET_CONTROLLER_RUNTIME_IMAGE_ID` and
`CI_FLEET_RUNNER_RUNTIME_IMAGE_ID` only after both roles pass. These are rendered
fields, never host configuration inputs. Canonical tags, Compose controller and
proxy startup, controller runner inspection and runner creation use R. Reviewed
labels retain C. The controller receives no root proof, store socket or new secret.
It checks that R resolves exactly with its own source revision and Linux platform.

Versioned `managed_image_bindings` in protected `install-state.json` binds the
engine, architecture, role, C/R/manifest, daemon ID, store and software identity.
It retains authenticated small original descriptor bytes, config hash and approved
source/platform metadata. It saves no raw config or image Env. Checkpoints copy
that metadata with their original environment and raw tag/live image maps.
Warm checks rehash retained descriptors and inspect exact R without exporting
layers. They may reuse the same proof for a separately approved configuration
commit selecting identical content. Candidate fields and metadata are hydrated
before exact environment/state comparisons.

Missing, untrusted or stale proof fails read-only checks and defers storage
removal. Controlled `--upgrade` preparation can create fresh proof before mutation.
Daemon/store/software changes require reproof. A restart alone does not if all
bound identities and immutable objects remain unchanged. Warm proof trusts the
daemon's content-addressed store. It cannot detect hostile root/store replacement
that copies all bound identities.

Preparation captures the incumbent checkpoint before required builds.
Opaque runtime sources validate candidate Compose before checkpoint capture.
Portable sources validate Compose after both image bindings pass, following any
required builds under checkpoint coverage. Neither source drains or publishes
canonical tags after failed Compose validation. Builds use
private temporary tags. Both roles must pass before publication; post-build host
checks and drain still precede canonical tags. Matching verified objects are
reused despite tag drift. Failed preparation preserves incumbent runtime and tags.
Activation publishes environment and binding metadata inside the existing recovery
transaction before runtime health checks. Independent file renames are not a
crash-atomic pair; interrupted or mismatched state fails subsequent checks and
requires checkpoint recovery. Completion markers still follow complete checkpoint
capture.

## Recovery and retained images

Image-free bootstrap recovery uses that host's protected captured old IDs. It
does not substitute another host's image catalog. An image-enforced host cannot
enter image-free staging by removing fields. Bootstrap LKG recovery still requires
a complete protected checkpoint matching repository, configuration ref and
controller. Formats 2/3 and unversioned checkpoints retain their original raw tag,
live-image and absence distinctions. Portable checkpoint creation authenticates original metadata and environment before copying.
It reuses warm proof, or performs controlled fresh proof of the same captured R
into the new checkpoint when stale. It never repairs incumbent state during
capture. Portable restoration requires its protected proof and exact retained
runtime and raw rollback objects before drain. Missing
portable objects require a controlled verified offline import and reproof or
separately authorized reinstall. Restoration does not rewrite a checkpoint to a
different runtime root.

Manual opaque adoption requires a complete trusted source for the incumbent's own
engine revision. Source selection checks the installed runtime, that revision's
canonical release, and retained manager source context. A portable candidate is
never a recovery or drain source for a different opaque incumbent. Historical
in-place sources are preserved as canonical pinned source before installing a
candidate beneath them. Checkpoints retain the selected `release-target`.
Preflight, drain and restoration use that source's Compose and image semantics,
so an opaque environment needs no invented portable runtime fields or proof.
A missing, mismatched or unsafe compatible source refuses before drain. Establish
trusted source through controlled preparation or use reviewed reinstall recovery;
retained image objects alone do not supply executable recovery source.

Storage protection reads a checkpoint's authenticated `release-target` when
present, including historical manager source locations. It does not require a
never-installed canonical release for that checkpoint. Current source lookup also
supports protected historical runtime context. Pointer paths, ownership,
permissions, source revision and capability declarations must agree. Portable
sources still require protected bindings; absent raw tags stay absent and opaque
IDs remain exact. Unavailable or untrusted source/proof defers removal.

Old opaque-ID reconstruction still requires the checkpoint release's
`reproducible_managed_images` capability and its own build helper and tool pins.
It completes before drain and is attempted at most once. Pre-enforcement images
have mutable inputs and must never be rebuilt as an alleged match. Lost legacy
objects fail with `ROLLBACK_IMAGE_REINSTALL_REQUIRED`. Retained objects are
retagged without Buildx. Unversioned checkpoints require matching existing tags;
they do not retag or rebuild. Captured raw tag and live IDs remain exact identities for preflight. Inherited
installed restoration later republishes reviewed tags, so it does not guarantee
every final raw tag mapping or absence. Fresh no-install restoration and deferred
prior controller-tag restoration retain their specific exact-ID checks.

The [caller coverage audit](MANAGED-IMAGE-CALLERS.md) records all lifecycle and
retention dispositions and the standalone template follow-up.

## Native acceptance remains separate

Initial protocol limits are 128 KiB reads, 1 MiB per metadata/config object,
8 MiB retained metadata, 4096 members, 8 GiB per role, JSON depth 32 and 1 MiB final
zero padding. The local transport uses API 1.52 and a 180-second overall pair
budget. Docker may prepare an export until that shared deadline before sending
its first byte. Connections, metadata responses and gaps after an export starts
retain 30-second inactivity limits. Starting the second role never resets the
pair deadline. These are bounds, not measured native capacity.
Cold export transfers every layer and classic Docker may need temporary disk.
Measure both-role duration and peak disk on supervised disposable classic and
containerd workers, with empty and populated stores. Prove native export ending,
import, actual container IDs, source/platform, tag races, cache invalidation,
cleanup and every recovery format before host acceptance. No backend migration,
host resize, SDK upgrade or containerd socket access is required by this protocol.

## Reviewed upstream inputs

Base-image and runner selections updated on 2026-10-12 from official upstream endpoints. The signed Debian snapshots were reviewed on 2026-10-12. Docker package pins retain their 2026-08-16 review:

The Go build image was updated to 1.27.2 on 2026-10-13. Docker Hub tag metadata confirms the pinned multi-architecture manifest digest below, including amd64 and arm64 variants.

Runner Buildx 0.32.1 package hashes were checked against the official Docker
package index on 2026-10-15 for amd64 and arm64. The existing Docker CLI and
Compose selections are unchanged.

| Input | Immutable selection | Evidence |
| --- | --- | --- |
| Debian 13.7 slim, both managed images | `sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f` | Docker Hub registry manifest for `library/debian:13.7-slim` |
| Go 1.27.2 Bookworm build image | `sha256:5cf287a799e6b94384bad13d16b14904c531f51ba65792237e122ce42b392f61` | Docker Hub registry manifest for `library/golang:1.27.2-bookworm` |
| Debian packages | signed snapshot `20261012T000000Z` | `snapshot.debian.org/archive/debian/20261012T000000Z/dists/trixie/InRelease` |
| Build timestamps | `SOURCE_DATE_EPOCH=1786752000` | BuildKit reproducible-build timestamp input declared in every stage |
| Docker CLI 29.7.2 amd64 / arm64 | exact package version and SHA-256 `3d1a00d1549f7539606252b1e2b88ae2a0c855e02e5fa92ad962c8a118d7f6ad` / `6bb694f01789b9869e99e34df3492104c0150d12df93192034db5f88cf0f7c8c` | official Docker Debian `trixie` package index |
| Compose plugin 5.4.0 amd64 / arm64 | exact package version and SHA-256 `75391b38459edf75a975504903673fdb87a5a4016d689d9b198e2db1ae2796e0` / `81261f286d48c1c9e586065bc52065bbbe3b023705ebdb72377ec465c216dcaf` | official Docker Debian `trixie` package index |
| Runner Buildx plugin 0.32.1 amd64 / arm64 | exact package version and SHA-256 `c50835adb10d6e9fc16c877d37a7a651b0ef2bac165be3ec52f8f05666cbccc8` / `a9ebb845ba8c2a67a815329bba341e31596c1c1f02d49ffb5670d0222253154e` | official Docker Debian `trixie` package index, added for optional APT build integration |
| Actions Runner 2.338.0 | architecture-specific archive hashes in `runner/Dockerfile` | official GitHub Actions Runner release |
| Controller modules | versions plus checksums in `controller/go.mod` and `controller/go.sum` | Go checksum-verified module inputs |

The Dockerfiles fail closed if an upstream artifact no longer matches. Updating any pin requires a reviewed source change and new managed image IDs.

## Staged desired-state rollout

Older schema-v3 engines reject `managed_images` as an unknown top-level key. Upgrade without deadlocking reconciliation:

1. For an established image-free fleet, first integrate missing incumbent active
   evidence in an evidence-only commit with `fleet.json` unchanged. It must describe
   the already observed runtime and exact source capabilities, even when every flag
   is false. Source review alone cannot establish activation. Retain existing
   records unchanged. In a later evidence-only commit, add matching target pending
   records, again keeping the entire fleet and existing evidence unchanged.
   Review and merge the actual target engine before image production. Stage its
   capability records in `next-engine-rollout-evidence.json` while the incumbent
   engine and active evidence remain unchanged. Include the runtime source's
   `portable_managed_image_ids: true` declaration in its reviewed record.
2. For an existing managed image-free host, change only controller engine refs
   in a later integrated staging commit. Keep incumbent runtime evidence and the
   pending records unchanged. Apply the approved staging ref with the reviewed
   candidate installer. It installs the manager and leaves runtime unchanged.
   Every controller needs an explicit incumbent runtime capability record before
   staging, even when all capability flags are false and optional blocks are absent.
   Empty active evidence cannot identify the runtime after its manager pin changes.
   Image-enforced hosts skip this image-free stage.
3. After manager staging, introduce reviewed image policy while retaining every
   other fleet setting and both evidence files. Only this step activates the
   portable runtime. After verified runtime activation, promote matching pending
   evidence unchanged in an evidence-only commit and remove the empty sidecar.
4. For ordinary image-enforced engine upgrades, select the engine and promote its
   matching previously integrated pending record unchanged, then remove its pending
   record. Never stage and select a record in the same commit.

Installer and reconciler validation use the selected commit's immediate first
parent. Missing history, changed pending records, early evidence promotion,
image-field removal, unrelated bootstrap edits and no-change image-free staging
fail. Evidence preparation permits additions to established active/pending maps,
with all prior records and the entire fleet unchanged. Before manager selection,
it also permits canceling or replacing unused pending records in an evidence-only
commit. The complete fleet and active evidence must remain unchanged, and every
controller needs an explicit active record matching its selected engine.
Validate against the actual immediate predecessor. Remove the pending sidecar
when canceling its last record. A replacement still needs normal target-capability
review and must be integrated before selection. This exception cannot change
active evidence, repair a manager/runtime mismatch, introduce images, or combine
retargeting with selection. The trusted validator and executing manager must
support this route independently of the unchanged incumbent runtime pin.
Candidate-local validation cannot override an older trusted policy.
A true no-op, same-commit
preparation and selection, or fresh image-free installation remains invalid. A suite base chosen elsewhere does not replace this predecessor. The
protected exact installed-ref drift exception and protected bootstrap LKG recovery
remain separate; neither admits a new image-free installation. Keep approval,
compatible-manager recovery and runtime verification gates in force.

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
# Use a root-owned private evidence directory outside Git.
ARCHITECTURE=amd64 # Select the actual build host architecture, or arm64.
EVIDENCE_DIR=/results/managed-image-review
install -d -m 0700 "$EVIDENCE_DIR"
for role in controller runner; do
  python3 scripts/managed_image_identity.py describe \
    --reference "ci-fleet-$role:$CI_FLEET_VERSION" \
    --engine "$CI_FLEET_COMMIT" --architecture "$ARCHITECTURE" \
    >"$EVIDENCE_DIR/$role.json"
done
```

Managed builds require the reviewed Docker Buildx client v0.32.1. Read-only checks, manager staging, maintenance that reuses matching images, uninstall and retained-image rollback do not require that plugin. Rollback that must rebuild requires the reviewed client. Host tool installation or upgrade remains separately authorized.

Managed installation creates the same digest-pinned v0.32.2 backend for every rebuild, verifies its reported version exactly, and removes only its own temporary builder afterward. Builder and staging names include a per-invocation UUID; an existing operator builder is never removed.

`describe` produces prospective build evidence, not host authorization. On
containerd it uses a discovery export for R/manifest, then a second full-R export
for the discovered C. Host `prepare` always requires reviewed C and uses one cold
export per role. Compare `config_digest` across the two clean builds, retain full
source/tree/tool identities and private stream evidence outside Git, then review
both roles into policy. Do not choose backend R as the new scalar.

Repeat the build on every supported host architecture. After each architecture's
repeated config digests match, add them to one private desired-state entry:

```json
"managed_images": {
  "FULL_ENGINE_COMMIT": {
    "amd64": {
      "controller": "sha256:REVIEWED_AMD64_CONTROLLER_CONFIG_DIGEST",
      "runner": "sha256:REVIEWED_AMD64_RUNNER_CONFIG_DIGEST"
    },
    "arm64": {
      "controller": "sha256:REVIEWED_ARM64_CONTROLLER_CONFIG_DIGEST",
      "runner": "sha256:REVIEWED_ARM64_RUNNER_CONFIG_DIGEST"
    }
  }
}
```

The checked-in template uses conspicuous `111...` / `222...` non-production fixture IDs only so validation and mocked tests are runnable; replace them during initialization. The repository-only marathon does not run a Docker daemon or invent production IDs, so live private configuration remains an external evidence gate.
