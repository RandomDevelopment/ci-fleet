# syntax=docker/dockerfile:1.18@sha256:dabfc0969b935b2080555ace70ee69a5261af8a8f1b4df97b9e7fbcf6722eddf
FROM debian:13.7-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad

ARG TARGETARCH
ARG SOURCE_DATE_EPOCH=1786752000
ARG DEBIAN_SNAPSHOT=20261012T000000Z
ARG CI_FLEET_COMMIT=unknown
ARG RUNNER_VERSION=2.338.1
ARG RUNNER_SHA256_AMD64=08e9ccb3604d2462c24b5e33604dc30810296ca15cc65f23cffd4317eb144c37
ARG RUNNER_SHA256_ARM64=475e0c149fd3b611610127b9664ba5bacf5c6928795cc448ab520bb5f324bede
ARG DOCKER_CLI_VERSION=29.7.2-1~debian.13~trixie
ARG DOCKER_CLI_SHA256_AMD64=3d1a00d1549f7539606252b1e2b88ae2a0c855e02e5fa92ad962c8a118d7f6ad
ARG DOCKER_CLI_SHA256_ARM64=6bb694f01789b9869e99e34df3492104c0150d12df93192034db5f88cf0f7c8c
ARG COMPOSE_VERSION=5.4.0-1~debian.13~trixie
ARG COMPOSE_SHA256_AMD64=75391b38459edf75a975504903673fdb87a5a4016d689d9b198e2db1ae2796e0
ARG COMPOSE_SHA256_ARM64=81261f286d48c1c9e586065bc52065bbbe3b023705ebdb72377ec465c216dcaf
ARG BUILDX_VERSION=0.32.1-1~debian.13~trixie
ARG BUILDX_SHA256_AMD64=c50835adb10d6e9fc16c877d37a7a651b0ef2bac165be3ec52f8f05666cbccc8
ARG BUILDX_SHA256_ARM64=a9ebb845ba8c2a67a815329bba341e31596c1c1f02d49ffb5670d0222253154e
LABEL org.opencontainers.image.revision="${CI_FLEET_COMMIT}" \
      io.randomdevelopment.ci-fleet.managed="true"

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN --mount=type=secret,id=ci_fleet_apt,target=/etc/apt/apt.conf.d/99-ci-fleet-cache \
    rm -f /etc/apt/sources.list.d/debian.sources \
    && printf 'deb http://snapshot.debian.org/archive/debian/%s trixie main\n' "$DEBIAN_SNAPSHOT" >/etc/apt/sources.list \
    && printf 'Acquire::Check-Valid-Until "false";\n' >/etc/apt/apt.conf.d/99-ci-fleet-snapshot \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git jq sudo \
    && case "$TARGETARCH" in \
      amd64) docker_sha="$DOCKER_CLI_SHA256_AMD64"; compose_sha="$COMPOSE_SHA256_AMD64"; buildx_sha="$BUILDX_SHA256_AMD64" ;; \
      arm64) docker_sha="$DOCKER_CLI_SHA256_ARM64"; compose_sha="$COMPOSE_SHA256_ARM64"; buildx_sha="$BUILDX_SHA256_ARM64" ;; \
      *) echo "unsupported TARGETARCH: $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && curl -fsSLo docker-cli.deb "https://download.docker.com/linux/debian/dists/trixie/pool/stable/${TARGETARCH}/docker-ce-cli_${DOCKER_CLI_VERSION#*:}_${TARGETARCH}.deb" \
    && curl -fsSLo compose.deb "https://download.docker.com/linux/debian/dists/trixie/pool/stable/${TARGETARCH}/docker-compose-plugin_${COMPOSE_VERSION#*:}_${TARGETARCH}.deb" \
    && curl -fsSLo buildx.deb "https://download.docker.com/linux/debian/dists/trixie/pool/stable/${TARGETARCH}/docker-buildx-plugin_${BUILDX_VERSION#*:}_${TARGETARCH}.deb" \
    && printf '%s  docker-cli.deb\n%s  compose.deb\n%s  buildx.deb\n' "$docker_sha" "$compose_sha" "$buildx_sha" | sha256sum -c - \
    && apt-get install -y --no-install-recommends ./docker-cli.deb ./compose.deb ./buildx.deb \
    && rm docker-cli.deb compose.deb buildx.deb \
    && rm -rf /var/lib/apt/lists/* /var/log/apt/* /var/log/dpkg.log /var/cache/ldconfig/aux-cache \
    && find /etc /home /run /usr /var -xdev ! -path /etc/hostname ! -path /etc/hosts ! -path /etc/resolv.conf -newermt "@${SOURCE_DATE_EPOCH}" ! -path /etc/apt/apt.conf.d/99-ci-fleet-cache -exec touch -h -d "@${SOURCE_DATE_EPOCH}" {} +

RUN useradd --create-home --no-log-init --uid 1001 --shell /bin/bash runner \
    && chage -d "$((SOURCE_DATE_EPOCH / 86400))" runner \
    && rm -f /etc/shadow- \
    && install -d -o runner -g runner /home/runner/_work \
    && find /etc /home /run /usr /var -xdev ! -path /etc/hostname ! -path /etc/hosts ! -path /etc/resolv.conf -newermt "@${SOURCE_DATE_EPOCH}" ! -path /etc/apt/apt.conf.d/99-ci-fleet-cache -exec touch -h -d "@${SOURCE_DATE_EPOCH}" {} +

WORKDIR /home/runner
RUN --mount=type=secret,id=ci_fleet_apt,target=/etc/apt/apt.conf.d/99-ci-fleet-cache \
    case "${TARGETARCH}" in \
      amd64) archive_arch=x64; expected="${RUNNER_SHA256_AMD64}" ;; \
      arm64) archive_arch=arm64; expected="${RUNNER_SHA256_ARM64}" ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSLo actions-runner.tar.gz "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-${archive_arch}-${RUNNER_VERSION}.tar.gz" \
    && echo "${expected}  actions-runner.tar.gz" | sha256sum -c - \
    && tar xzf actions-runner.tar.gz \
    && rm actions-runner.tar.gz \
    && ./bin/installdependencies.sh \
    && chown -R runner:runner /home/runner \
    && rm -rf /var/log/apt/* /var/log/dpkg.log /var/cache/ldconfig/aux-cache \
    && find /etc /home /run /usr /var -xdev ! -path /etc/hostname ! -path /etc/hosts ! -path /etc/resolv.conf -newermt "@${SOURCE_DATE_EPOCH}" ! -path /etc/apt/apt.conf.d/99-ci-fleet-cache -exec touch -h -d "@${SOURCE_DATE_EPOCH}" {} +

ENV RUNNER_ALLOW_RUNASROOT=0
USER runner
ENTRYPOINT ["/home/runner/run.sh"]
