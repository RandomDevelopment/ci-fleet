# syntax=docker/dockerfile:1.18
FROM golang:1.26.5-bookworm AS build
WORKDIR /src
COPY go.mod *.go ./
RUN go mod tidy && go mod verify
RUN go test ./...
ARG CI_FLEET_VERSION=dev
ARG CI_FLEET_COMMIT=unknown
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w -X main.version=${CI_FLEET_VERSION} -X main.commitSHA=${CI_FLEET_COMMIT}" -o /out/ci-fleet-controller .

FROM debian:13.6-slim
ARG CI_FLEET_COMMIT=unknown
LABEL org.opencontainers.image.revision="${CI_FLEET_COMMIT}"
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/ci-fleet-controller /usr/local/bin/ci-fleet-controller
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/ci-fleet-controller"]
