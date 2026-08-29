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
printf 'root_prefix=%s\n' "${CI_FLEET_ROOT_PREFIX-}"
printf 'docker_socket=%s\n' "${CI_FLEET_DOCKER_SOCKET-}"
printf 'bootstrap=%s\n' "${CI_FLEET_HEALTH_BOOTSTRAP-}"
printf 'suppress_delivery=%s\n' "${CI_FLEET_HEALTH_SUPPRESS_DELIVERY-}"
printf 'docker_host=%s\n' "${DOCKER_HOST-}"
printf 'args=%s\n' "$*"
EOF
chmod +x "$tmp/bin/python3"
printf 'CI_FLEET_CONTROLLER=candidate\n' >"$tmp/candidate.env"

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
grep -Fqx "root_prefix=$tmp/root" <<<"$output"
grep -Fqx "docker_socket=$tmp/docker.sock" <<<"$output"
grep -Fqx 'bootstrap=1' <<<"$output"
grep -Fqx 'suppress_delivery=1' <<<"$output"
grep -Fqx "docker_host=unix://$tmp/docker.sock" <<<"$output"
grep -Fqx "args=$repo_root/scripts/health.py local --monitoring-config $tmp/root/etc/ci-fleet/monitoring.env --output $tmp/root/var/lib/ci-fleet/health/latest.json" <<<"$output"
printf '%s\n' 'Healthcheck environment tests passed.'
