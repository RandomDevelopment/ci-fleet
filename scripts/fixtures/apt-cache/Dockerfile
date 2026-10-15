# syntax=docker/dockerfile:1.18@sha256:dabfc0969b935b2080555ace70ee69a5261af8a8f1b4df97b9e7fbcf6722eddf
ARG RUNNER_IMAGE=ci-fleet-runner:dev
FROM ${RUNNER_IMAGE}
USER root
# Test tooling only. Package inputs use the runner's reviewed Debian snapshot.
RUN apt-get update && apt-get install -y --no-install-recommends apt-cacher-ng python3 \
    && install -d -o apt-cacher-ng -g apt-cacher-ng /var/cache/apt-cacher-ng /var/log/apt-cacher-ng /run/apt-cacher-ng \
    && rm -rf /var/lib/apt/lists/*
ENTRYPOINT ["/usr/sbin/apt-cacher-ng"]
CMD ["-c", "/etc/apt-cacher-ng", "ForeGround=1", "BindAddress=0.0.0.0"]
