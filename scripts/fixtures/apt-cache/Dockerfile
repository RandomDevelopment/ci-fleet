# syntax=docker/dockerfile:1.18@sha256:dabfc0969b935b2080555ace70ee69a5261af8a8f1b4df97b9e7fbcf6722eddf
ARG RUNNER_IMAGE=ci-fleet-runner:dev
FROM ${RUNNER_IMAGE}
USER root
# Test tooling only. Package inputs use the runner's reviewed Debian snapshot.
# Restore the HTTP source afterward for the cache and outage checks.
RUN cp /etc/apt/sources.list /tmp/ci-fleet-fixture-sources.list \
    && sed -i 's#http://snapshot.debian.org/#https://snapshot.debian.org/#' /etc/apt/sources.list \
    && apt-get update && apt-get install -y --no-install-recommends apt-cacher-ng python3 \
    && mv /tmp/ci-fleet-fixture-sources.list /etc/apt/sources.list \
    && install -d -o apt-cacher-ng -g apt-cacher-ng /var/cache/apt-cacher-ng /var/log/apt-cacher-ng /run/apt-cacher-ng \
    && rm -rf /var/lib/apt/lists/*
ENTRYPOINT ["/usr/sbin/apt-cacher-ng"]
# Use the pinned APT client's identifier for snapshot index requests.
CMD ["-c", "/etc/apt-cacher-ng", "ForeGround=1", "BindAddress=0.0.0.0", "UserAgent=Debian APT-HTTP/1.3 (2.6.1)"]
