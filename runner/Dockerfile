# syntax=docker/dockerfile:1.18
FROM debian:13.6-slim

ARG TARGETARCH
ARG CI_FLEET_COMMIT=unknown
ARG RUNNER_VERSION=2.335.1
ARG RUNNER_SHA256_AMD64=4ef2f25285f0ae4477f1fe1e346db76d2f3ebf03824e2ddd1973a2819bf6c8cf
ARG RUNNER_SHA256_ARM64=6d1e85bfd1a506a8b17c1f1b9b57dba458ffed90898799aaa9f599520b0d9207
LABEL org.opencontainers.image.revision="${CI_FLEET_COMMIT}"

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git gnupg jq sudo \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian trixie stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1001 --shell /bin/bash runner \
    && install -d -o runner -g runner /home/runner/_work

WORKDIR /home/runner
RUN case "${TARGETARCH}" in \
      amd64) archive_arch=x64; expected="${RUNNER_SHA256_AMD64}" ;; \
      arm64) archive_arch=arm64; expected="${RUNNER_SHA256_ARM64}" ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSLo actions-runner.tar.gz "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-${archive_arch}-${RUNNER_VERSION}.tar.gz" \
    && echo "${expected}  actions-runner.tar.gz" | sha256sum -c - \
    && tar xzf actions-runner.tar.gz \
    && rm actions-runner.tar.gz \
    && ./bin/installdependencies.sh \
    && chown -R runner:runner /home/runner

ENV RUNNER_ALLOW_RUNASROOT=0
USER runner
ENTRYPOINT ["/home/runner/run.sh"]
