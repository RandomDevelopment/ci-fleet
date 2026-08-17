# syntax=docker/dockerfile:1.18
FROM golang:1.26.5-bookworm@sha256:53eeac89074db483fdf0ab3be1df32bf6e47562263d2d0d6baa7f26acb4957dd AS build
ARG SOURCE_DATE_EPOCH=1786752000
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download && go mod verify
COPY *.go ./
RUN go test ./...
ARG CI_FLEET_VERSION=dev
ARG CI_FLEET_COMMIT=unknown
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w -X main.version=${CI_FLEET_VERSION} -X main.commitSHA=${CI_FLEET_COMMIT}" -o /out/ci-fleet-controller . \
    && touch -d "@${SOURCE_DATE_EPOCH}" /out/ci-fleet-controller

FROM debian:13.6-slim@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258
ARG SOURCE_DATE_EPOCH=1786752000
ARG DEBIAN_SNAPSHOT=20260815T000000Z
ARG CI_FLEET_COMMIT=unknown
LABEL org.opencontainers.image.revision="${CI_FLEET_COMMIT}" \
      io.randomdevelopment.ci-fleet.managed="true"
RUN rm -f /etc/apt/sources.list.d/debian.sources \
    && printf 'deb http://snapshot.debian.org/archive/debian/%s trixie main\n' "$DEBIAN_SNAPSHOT" >/etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && install -d -o 65532 -g 65532 /run/ci-fleet \
    && rm -rf /var/lib/apt/lists/* /var/log/apt/* /var/log/dpkg.log \
    && find /etc /home /run /usr /var -xdev ! -path /etc/hostname ! -path /etc/hosts ! -path /etc/resolv.conf -newermt "@${SOURCE_DATE_EPOCH}" -exec touch -h -d "@${SOURCE_DATE_EPOCH}" {} +
COPY --from=build /out/ci-fleet-controller /usr/local/bin/ci-fleet-controller
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/ci-fleet-controller"]
