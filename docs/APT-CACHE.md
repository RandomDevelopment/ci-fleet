# Optional APT cache

An existing apt-cacher-ng server can share package downloads across disposable
runners and image builds. Support is disabled by default. Hosts without a cache
keep their existing direct behavior. The fleet does not discover, install, or
reconfigure a cache server.

Use this only in trusted private pools. Jobs can inspect their runtime network
configuration and reach the cache endpoint. Proxy URLs must not contain credentials.

## Enable on a host

First promote a reviewed engine advertising `apt_cache: true` through the normal
[desired-state process](DESIRED-STATE.md), including its reviewed image IDs.
Stage the compatible manager before adding host-local settings; older managers
reject unknown host variables. The renderer rejects cache settings when the
selected engine lacks support.

Edit the root-owned, mode `0600` `/etc/ci-fleet/host.env` locally:

```dotenv
CI_FLEET_APT_HTTP_PROXY=http://apt-cache.example.invalid:3142
# Optional exact HTTP repository hosts that should bypass the cache.
CI_FLEET_NO_PROXY=mirror.example.invalid
```

These addresses are fictional. Keep real values out of Git and public evidence.
The proxy must be an HTTP origin URL with an optional port and trailing slash.
Paths, credentials, query strings, fragments, whitespace, and shell syntax are
rejected. Bracketed IPv6 proxy origins are supported. `CI_FLEET_NO_PROXY`
accepts at most 64 exact DNS names or IPv4 addresses, without ports, CIDRs, or
wildcards. It controls APT HTTP repository bypasses only. It does not export a
general `NO_PROXY` environment variable.

Apply the normal install/upgrade procedure after editing the file. Retain the
existing drain, checkpoint, and rollback gates. Existing runners finish with
their prior settings. A scheduled check alone does not activate an edited file.

The cache must be reachable from runner and BuildKit networks. Inside a
container, `localhost` usually names that container. Check routing and firewall
rules locally. No daemon-wide proxy or host networking is required.

## Coverage

- New runners receive root-owned, mode `0644`
  `/etc/apt/apt.conf.d/99-ci-fleet-cache` before starting. Normal APT reads and
  permitted `sudo apt-get` commands use it. No sudo permissions are added.
  `CI_FLEET_APT_CONFIG` exposes the file path to project adapters.
- Managed runner/controller builds receive an optional BuildKit secret for
  every APT-using stage and the runner dependency installer. The isolated
  builder, `--no-cache`, snapshot pins, signatures, and reviewed image-ID checks
  remain required. The secret is absent from resulting images.
- Project Docker, Compose, and Buildx builds use the opt-in examples below.
  Arbitrary project Dockerfiles do not inherit runner APT settings. The generic
  runner includes the reviewed Buildx plugin alongside Docker and Compose.
- Project containers installing packages at runtime must explicitly copy the
  configuration into their own fresh container before running APT.
- Host APT remains operator-managed. Configure it separately to use the same
  server. Fleet configuration does not alter host APT files.

Git, GitHub, curl, language package managers, and image pulls are outside this
feature. Docker's general client `proxies` configuration is not used because it
also proxies unrelated HTTP traffic.

## Project build examples

Use an optional secret mount for each APT instruction, keeping the project's
reviewed base image and package inputs:

```dockerfile
RUN --mount=type=secret,id=ci_fleet_apt,target=/etc/apt/apt.conf.d/99-ci-fleet-cache \
    apt-get update \
    && apt-get install -y --no-install-recommends YOUR_PACKAGES \
    && rm -rf /var/lib/apt/lists/*
```

For `docker build` or `docker buildx build`, pass the file only when supplied:

```bash
apt_options=()
if [[ -n ${CI_FLEET_APT_CONFIG:-} ]]; then
  apt_options=(--secret "id=ci_fleet_apt,src=$CI_FLEET_APT_CONFIG")
fi
docker build "${apt_options[@]}" -t example-test .
# Or use docker buildx build --load with the same options and your builder.
```

For Compose, use a build secret. `/dev/null` supplies an empty file when disabled:

```yaml
services:
  test:
    build:
      context: .
      secrets:
        - ci_fleet_apt
secrets:
  ci_fleet_apt:
    file: ${CI_FLEET_APT_CONFIG:-/dev/null}
```

Use the project's existing task/shard adapter and run-owned Compose project.
Do not add fixed names or host ports. Secret mounts require BuildKit; the legacy
Docker builder is unsupported. Secret contents do not invalidate image layers.
Use a disposable `--no-cache` build when measuring APT reuse.

For a fresh project container with APT, copy the file before the package
operation. `container` must identify the current task's container:

```bash
if [[ -n ${CI_FLEET_APT_CONFIG:-} ]]; then
  docker cp "$CI_FLEET_APT_CONFIG" "$container:/etc/apt/apt.conf.d/99-ci-fleet-cache"
fi
```

This avoids host bind-mount path assumptions. Use the existing privilege policy
and run-owned cleanup. Do not publish a configured container as an image.

## HTTPS, outages, and removal

HTTP repositories can use apt-cacher-ng. HTTPS repositories stay direct through
`Acquire::https::Proxy "DIRECT"`. Ordinary HTTPS CONNECT tunneling does not
provide shared package caching. The fleet does not rewrite sources, downgrade
HTTPS, intercept TLS, or disable signatures. Cache-specific repository URL
mechanisms require separately reviewed project configuration and tests.

Enabled configurations set a ten-second HTTP connection/data timeout and two APT
retries. Retries allow recovery from transient disconnects during concurrent
apt-cacher-ng metadata requests. `apt-get update` fails if an index still cannot
be fetched. These limits bound individual attempts, not total time across many
repositories. Jobs retain their five-minute timeout. A persistent outage fails
the APT operation. There is no automatic direct fallback.

To disable, remove both variables and apply the normal upgrade procedure. New
runners have no generated cache file or path variable, and new managed builds
receive no cache secret. Existing runners keep their prior settings. Project
adapters must use fresh containers/builds. Remove settings before selecting an
older engine or manager. Checkpoint rollback restores the previous rendered
environment, which may still have a cache enabled.

Platform validation errors and status metadata omit endpoints. Cache-enabled
managed builds suppress raw build output because package tools may print
connection addresses. Controller runner-log forwarding redacts the configured
origin, hostname, and exact bypass hosts, and stops on lines larger than 1 MiB.
Arbitrary job commands, APT error output, and cache server logs can still reveal
network details. Keep those records private and share only safe aggregate evidence.

## Verify the benefit

On an inert validation daemon with the validation images already built:

```bash
python3 scripts/test_apt_cache.py
bash scripts/test-managed-image-build.sh
python3 scripts/test_apt_cache_e2e.py
```

The container test creates a disposable apt-cacher-ng instance. It measures
package bytes and elapsed time for cold/warm and concurrent runners and
Docker/Compose/Buildx builds. It verifies managed rebuilds against images built
without a cache using the same pinned Buildx client, BuildKit backend, and build
arguments. The cache-enabled rebuild must match those exact image IDs. The test
also checks project images for endpoint leakage and tests outage failure and
direct HTTPS. It removes only its own containers, images, and
builders. It never clears or changes a production cache.

Before live acceptance, repeat representative shards on the approved host.
Record elapsed time, upstream package bytes, cache-served package bytes, and
repeated package hits. Report index revalidation separately. `apt-get update`
success or Docker layer reuse alone does not prove cached `.deb` downloads.
Retain success, failure, cancellation, timeout, cleanup, read-only permissions,
and rollback evidence.
