# syntax=docker/dockerfile:1.18@sha256:dabfc0969b935b2080555ace70ee69a5261af8a8f1b4df97b9e7fbcf6722eddf
FROM golang:1.27.3-bookworm@sha256:972b4110ba0f47539a975c31e52c64765e1ff19f2d74469ae4a595c6676e80a5 AS build
ENV GOTOOLCHAIN=local
ARG SOURCE_DATE_EPOCH=1786752000
RUN --mount=type=secret,id=ci_fleet_apt,target=/etc/apt/apt.conf.d/99-ci-fleet-cache \
    rm -f /etc/apt/sources.list.d/debian.sources \
    && printf 'deb http://snapshot.debian.org/archive/debian/20261012T000000Z bookworm main\n' >/etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY controller/go.mod controller/go.sum ./
RUN go mod download && go mod verify
COPY controller/*.go ./
COPY scripts/cleanup.sh scripts/docker-local-env.sh scripts/cleanup_networks.py scripts/cleanup_storage.py scripts/managed_image_identity.py scripts/desired_state.py scripts/apt_cache.py /scripts/
COPY scripts/fixtures/apt-cache.json /scripts/fixtures/apt-cache.json
RUN go test ./...
ARG CI_FLEET_VERSION=dev
ARG CI_FLEET_COMMIT=unknown
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w -X main.version=${CI_FLEET_VERSION} -X main.commitSHA=${CI_FLEET_COMMIT}" -o /out/ci-fleet-controller . \
    && touch -d "@${SOURCE_DATE_EPOCH}" /out/ci-fleet-controller

FROM debian:13.7-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f
ARG SOURCE_DATE_EPOCH=1786752000
ARG DEBIAN_SNAPSHOT=20261012T000000Z
ARG CI_FLEET_COMMIT=unknown
LABEL org.opencontainers.image.revision="${CI_FLEET_COMMIT}" \
      io.randomdevelopment.ci-fleet.managed="true" \
      io.randomdevelopment.ci-fleet.network-cleanup-lock="v2"
RUN --mount=type=bind,from=build,source=/out,target=/out \
    --mount=type=secret,id=ci_fleet_apt,target=/etc/apt/apt.conf.d/99-ci-fleet-cache \
    rm -f /etc/apt/sources.list.d/debian.sources \
    && printf 'deb http://snapshot.debian.org/archive/debian/%s trixie main\n' "$DEBIAN_SNAPSHOT" >/etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && install -m 0755 /out/ci-fleet-controller /usr/local/bin/ci-fleet-controller \
    && install -d -o 65532 -g 65532 /run/ci-fleet \
    && rm -rf /var/lib/apt/lists/* /var/log/apt/* /var/log/dpkg.log /var/cache/ldconfig/aux-cache \
    && find /etc /home /run /usr /var -xdev ! -path /etc/hostname ! -path /etc/hosts ! -path /etc/resolv.conf -newermt "@${SOURCE_DATE_EPOCH}" ! -path /etc/apt/apt.conf.d/99-ci-fleet-cache -exec touch -h -d "@${SOURCE_DATE_EPOCH}" {} +
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/ci-fleet-controller"]
