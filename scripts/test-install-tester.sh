#!/usr/bin/env bash
set -Eeuo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
runtime=$repo_root/scripts/tester-runtime.sh
installer=$repo_root/scripts/install-tester.sh
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
root=$tmp/root
fake_bin=$tmp/bin
mkdir -p "$fake_bin" "$root/etc/ci-fleet-tester/environments" "$root/etc/ci-fleet-tester/definitions" "$root/etc/ci-fleet-tester/secrets" "$root/var/lib/ci-fleet-tester/environments" "$root/var/lib/docker" "$root/var/lib/fake-tester-volume" "$root/var/run" "$root/etc/systemd/system"
chmod 700 "$root/etc/ci-fleet-tester" "$root/etc/ci-fleet-tester/environments" "$root/etc/ci-fleet-tester/definitions" "$root/etc/ci-fleet-tester/secrets" "$root/var/lib/ci-fleet-tester" "$root/var/lib/ci-fleet-tester/environments"
printf 'ID=debian\nVERSION_ID=13\n' >"$root/etc/os-release"
: >"$root/var/run/docker.sock"
printf 'CI_FLEET_TESTER_DEFAULT_TTL_SECONDS=3600\nCI_FLEET_TESTER_MAX_ENVIRONMENTS=3\nCI_FLEET_TESTER_DISK_WARN_PERCENT=80\nCI_FLEET_TESTER_NETWORK_PROBE_HOST=tester-probe.invalid\nCI_FLEET_TESTER_HTTPS_PROBE_URL=https://tester-probe.invalid/health\nCI_FLEET_TESTER_ISOLATION_ACK=test-only-no-production-authority\n' >"$root/etc/ci-fleet-tester/tester.env"
chmod 600 "$root/etc/ci-fleet-tester/tester.env"
cp "$repo_root/scripts/fixtures/fake-tester-docker.sh" "$fake_bin/docker"
chmod 0755 "$fake_bin/docker"
cat >"$fake_bin/df" <<'EOF'
#!/usr/bin/env bash
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\nfixture 100 20 80 20%% /fixture\n'
EOF
cat >"$fake_bin/systemctl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${FAKE_TESTER_SYSTEMCTL_LOG:?}"
EOF
printf '#!/usr/bin/env bash\nexit 0\n' >"$fake_bin/curl"
printf '#!/usr/bin/env bash\nexit 0\n' >"$fake_bin/getent"
chmod 0755 "$fake_bin/df" "$fake_bin/systemctl" "$fake_bin/curl" "$fake_bin/getent"
export PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$root
export FAKE_TESTER_DOCKER_ROOT=$root/var/lib/docker FAKE_TESTER_VOLUME_ROOT=$root/var/lib/fake-tester-volume FAKE_TESTER_DOCKER_LOG=$tmp/docker.log FAKE_TESTER_SYSTEMCTL_LOG=$tmp/systemctl.log

write_environment() {
  local id=$1 port=$2
  mkdir -p "$root/etc/ci-fleet-tester/secrets/$id"; chmod 700 "$root/etc/ci-fleet-tester/secrets/$id"
  printf 'services: {}\n' >"$root/etc/ci-fleet-tester/definitions/$id.yaml"
  chmod 644 "$root/etc/ci-fleet-tester/definitions/$id.yaml"
  printf 'CI_FLEET_TESTER_PROJECT=example-project\nCI_FLEET_TESTER_OWNER=example-owner\nCI_FLEET_TESTER_COMPOSE_FILE=%s\nCI_FLEET_TESTER_EXPIRES_AT=%s\nCI_FLEET_TESTER_ROUTE_SERVICE=web\nCI_FLEET_TESTER_ROUTE_PORT=%s\n' \
    "$root/etc/ci-fleet-tester/definitions/$id.yaml" "$(( $(date +%s) + 1800 ))" "$port" >"$root/etc/ci-fleet-tester/environments/$id.env"
  chmod 600 "$root/etc/ci-fleet-tester/environments/$id.env"
}

"$runtime" --check | grep -Fq CHECK_OK || fail 'runtime preflight failed'
if FAKE_TESTER_DOCKER_ROOT=/remote/docker "$runtime" --check >/dev/null 2>&1; then fail 'remote Docker daemon was accepted'; fi
write_environment preview-a 18080
FAKE_TESTER_ROUTE_PORT=18080 "$runtime" --converge --environment preview-a | grep -Fq CONVERGED || fail 'converge failed'
state=$root/var/lib/ci-fleet-tester/environments/preview-a.state
[[ -f $state && $(stat -c %a "$state") == 600 ]] || fail 'state was not protected'
inspect_output=$(FAKE_TESTER_ROUTE_PORT=18080 "$runtime" --inspect --environment preview-a)
grep -q 'IMAGE_DIGESTS=sha256:[a-f0-9]\{64\}.*STATUS=running DISK_BYTES=[1-9][0-9]*' <<<"$inspect_output" || fail 'inspect did not report health and disk use'
FAKE_TESTER_ROUTE_PORT=18080 "$runtime" --converge --environment preview-a >/dev/null
[[ $(find "$root/var/lib/ci-fleet-tester/environments" -name '*.state' | wc -l) == 1 ]] || fail 'idempotent converge duplicated state'
write_environment preview-b 18080
if FAKE_TESTER_ROUTE_PORT=18080 "$runtime" --converge --environment preview-b >/dev/null 2>&1; then fail 'duplicate route port was accepted'; fi
for policy in mutable privileged bind broad-port external-network; do
  write_environment "bad-$policy" 18081
  if FAKE_TESTER_ROUTE_PORT=18081 FAKE_TESTER_POLICY=$policy "$runtime" --converge --environment "bad-$policy" >/dev/null 2>&1; then fail "unsafe compose policy was accepted: $policy"; fi
done
write_environment secret-preview 18082
secret_file=$root/etc/ci-fleet-tester/secrets/secret-preview/credential
printf 'example-test-scope-value\n' >"$secret_file"; chmod 600 "$secret_file"
FAKE_TESTER_ROUTE_PORT=18082 FAKE_TESTER_POLICY=valid-secret FAKE_TESTER_SECRET_FILE=$secret_file "$runtime" --converge --environment secret-preview >/dev/null
FAKE_TESTER_ROUTE_PORT=18082 "$runtime" --remove --environment secret-preview >/dev/null
outside_secret=$root/etc/ci-fleet-tester/secrets/outside
printf 'example-test-scope-value\n' >"$outside_secret"; chmod 600 "$outside_secret"
write_environment outside-secret 18083
if FAKE_TESTER_ROUTE_PORT=18083 FAKE_TESTER_POLICY=outside-secret FAKE_TESTER_SECRET_FILE=$outside_secret "$runtime" --converge --environment outside-secret >/dev/null 2>&1; then fail 'out-of-boundary secret was accepted'; fi
FAKE_TESTER_ROUTE_PORT=18080 "$runtime" --reset --environment preview-a >/dev/null
grep -q 'down --volumes --remove-orphans' "$tmp/docker.log" || fail 'reset did not remove only the scoped Compose project'
if grep -Eq 'system prune|volume prune|network prune' "$tmp/docker.log"; then fail 'global Docker prune was used'; fi
sed -i 's/^EXPIRES_AT=.*/EXPIRES_AT=1/' "$state"
"$runtime" --cleanup >/dev/null
[[ ! -e $state ]] || fail 'expired environment survived cleanup'

# Commit-backed installer tests run after the implementation commit exists.
ref=$(git -C "$repo_root" rev-parse HEAD)
"$installer" --install --config /etc/ci-fleet-tester/tester.env --ref "$ref" | grep -Fq INSTALL_OK || fail 'fresh install failed'
[[ $(readlink -f "$root/opt/ci-fleet-tester/current") == "$root/opt/ci-fleet-tester/releases/$ref" ]] || fail 'current release link is wrong'
"$installer" --install --config /etc/ci-fleet-tester/tester.env --ref "$ref" >/dev/null
check_output=$("$installer" --check --config /etc/ci-fleet-tester/tester.env)
grep -Fq CHECK_OK <<<"$check_output" || fail 'installed check failed'
for unit in ci-fleet-tester-health.service ci-fleet-tester-health.timer ci-fleet-tester-cleanup.service ci-fleet-tester-cleanup.timer; do [[ -f $root/etc/systemd/system/$unit ]] || fail "unit missing: $unit"; done

# A syntactically valid candidate that fails its post-switch check restores the incumbent.
upgrade_repo=$tmp/upgrade-repo
git clone --quiet --shared "$repo_root" "$upgrade_repo"
printf '#!/usr/bin/env bash\nexit 1\n' >"$upgrade_repo/scripts/tester-runtime.sh"; chmod 0755 "$upgrade_repo/scripts/tester-runtime.sh"
git -C "$upgrade_repo" add scripts/tester-runtime.sh
git -C "$upgrade_repo" -c user.name=Example -c user.email=example@invalid.example commit --quiet -m 'fixture: fail tester activation'
bad_ref=$(git -C "$upgrade_repo" rev-parse HEAD)
if "$upgrade_repo/scripts/install-tester.sh" --upgrade --config /etc/ci-fleet-tester/tester.env --ref "$bad_ref" >/dev/null 2>&1; then fail 'failed candidate activation succeeded'; fi
[[ $(readlink -f "$root/opt/ci-fleet-tester/current") == "$root/opt/ci-fleet-tester/releases/$ref" ]] || fail 'failed upgrade did not restore incumbent release'

# Rollback switches only to a complete recorded release and keeps environments intact.
old=0000000000000000000000000000000000000000
cp -a "$root/opt/ci-fleet-tester/releases/$ref" "$root/opt/ci-fleet-tester/releases/$old"
printf '%s\n' "$old" >"$root/opt/ci-fleet-tester/releases/$old/.ci-fleet-source-revision"
printf '%s\n' "$old" >"$root/var/lib/ci-fleet-tester/last-known-good"; chmod 600 "$root/var/lib/ci-fleet-tester/last-known-good"
"$installer" --rollback --config /etc/ci-fleet-tester/tester.env | grep -Fq ROLLBACK_OK || fail 'rollback failed'
[[ $(readlink -f "$root/opt/ci-fleet-tester/current") == "$root/opt/ci-fleet-tester/releases/$old" ]] || fail 'rollback selected the wrong release'

"$installer" --uninstall --config /etc/ci-fleet-tester/tester.env | grep -Fq UNINSTALL_OK || fail 'uninstall failed'
[[ -f $root/etc/ci-fleet-tester/tester.env && ! -L $root/opt/ci-fleet-tester/current ]] || fail 'uninstall did not preserve config/remove runtime'
printf 'TESTER_INSTALLER_TESTS_OK\n'
