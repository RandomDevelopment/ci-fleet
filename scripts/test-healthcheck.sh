#!/usr/bin/env bash
set -Eeuo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin"
cat >"$tmp/bin/python3" <<'EOF'
#!/usr/bin/env bash
printf 'controller=%s\n' "${CI_FLEET_CONTROLLER-}"
printf 'stale=%s\n' "${CI_FLEET_STALE-unset}"
printf 'testing=%s\n' "${CI_FLEET_TESTING-}"
printf 'testing_present=%s\n' "${CI_FLEET_TESTING+set}"
printf 'root_prefix=%s\n' "${CI_FLEET_ROOT_PREFIX-}"
printf 'root_prefix_present=%s\n' "${CI_FLEET_ROOT_PREFIX+set}"
printf 'docker_socket=%s\n' "${CI_FLEET_DOCKER_SOCKET-}"
printf 'docker_socket_present=%s\n' "${CI_FLEET_DOCKER_SOCKET+set}"
printf 'bootstrap=%s\n' "${CI_FLEET_HEALTH_BOOTSTRAP-}"
printf 'bootstrap_present=%s\n' "${CI_FLEET_HEALTH_BOOTSTRAP+set}"
printf 'suppress_delivery=%s\n' "${CI_FLEET_HEALTH_SUPPRESS_DELIVERY-}"
printf 'suppress_delivery_present=%s\n' "${CI_FLEET_HEALTH_SUPPRESS_DELIVERY+set}"
printf 'docker_host=%s\n' "${DOCKER_HOST-}"
printf 'args=%s\n' "$*"
EOF
chmod +x "$tmp/bin/python3"
cat >"$tmp/candidate.env" <<EOF
CI_FLEET_CONTROLLER=candidate
CI_FLEET_TESTING=candidate
CI_FLEET_ROOT_PREFIX=$tmp/candidate-root
CI_FLEET_DOCKER_SOCKET=$tmp/candidate-docker.sock
CI_FLEET_HEALTH_BOOTSTRAP=candidate
CI_FLEET_HEALTH_SUPPRESS_DELIVERY=candidate
EOF

output=$(env -i \
  "PATH=$tmp/bin:$PATH" \
  CI_FLEET_CONTROLLER=stale \
  CI_FLEET_STALE=stale \
  CI_FLEET_TESTING=1 \
  "CI_FLEET_ROOT_PREFIX=$tmp/root" \
  "CI_FLEET_DOCKER_SOCKET=$tmp/docker.sock" \
  CI_FLEET_HEALTH_BOOTSTRAP=1 \
  CI_FLEET_HEALTH_SUPPRESS_DELIVERY=1 \
  "$repo_root/scripts/healthcheck.sh" --env "$tmp/candidate.env")

grep -Fqx 'controller=candidate' <<<"$output"
grep -Fqx 'stale=unset' <<<"$output"
grep -Fqx 'testing=1' <<<"$output"
grep -Fqx 'testing_present=set' <<<"$output"
grep -Fqx "root_prefix=$tmp/root" <<<"$output"
grep -Fqx 'root_prefix_present=set' <<<"$output"
grep -Fqx "docker_socket=$tmp/docker.sock" <<<"$output"
grep -Fqx 'docker_socket_present=set' <<<"$output"
grep -Fqx 'bootstrap=1' <<<"$output"
grep -Fqx 'bootstrap_present=set' <<<"$output"
grep -Fqx 'suppress_delivery=1' <<<"$output"
grep -Fqx 'suppress_delivery_present=set' <<<"$output"
grep -Fqx "docker_host=unix://$tmp/docker.sock" <<<"$output"
grep -Fqx "args=$repo_root/scripts/health.py local --monitoring-config $tmp/root/etc/ci-fleet/monitoring.env --output $tmp/root/var/lib/ci-fleet/health/latest.json" <<<"$output"

output=$(env -i \
  "PATH=$tmp/bin:$PATH" \
  CI_FLEET_CONTROLLER=stale \
  "$repo_root/scripts/healthcheck.sh" --env "$tmp/candidate.env")

grep -Fqx 'controller=candidate' <<<"$output"
grep -Fqx 'testing=' <<<"$output"
grep -Fqx 'testing_present=' <<<"$output"
grep -Fqx 'root_prefix=' <<<"$output"
grep -Fqx 'root_prefix_present=' <<<"$output"
grep -Fqx 'docker_socket=' <<<"$output"
grep -Fqx 'docker_socket_present=' <<<"$output"
grep -Fqx 'bootstrap=' <<<"$output"
grep -Fqx 'bootstrap_present=' <<<"$output"
grep -Fqx 'suppress_delivery=' <<<"$output"
grep -Fqx 'suppress_delivery_present=' <<<"$output"
grep -Fqx 'docker_host=unix:///var/run/docker.sock' <<<"$output"
grep -Fqx "args=$repo_root/scripts/health.py local" <<<"$output"
printf '%s\n' 'Healthcheck environment tests passed.'
