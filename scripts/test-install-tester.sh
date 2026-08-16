#!/usr/bin/env bash
set -Eeuo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
runtime=$repo_root/scripts/tester-runtime.sh
installer=$repo_root/scripts/install-tester.sh
tmp=$(mktemp -d)
cleanup() { chmod -R u+w "$tmp" 2>/dev/null || true; rm -rf "$tmp"; }
trap cleanup EXIT
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
[[ -z ${FAKE_TESTER_SYSTEMCTL_FAIL:-} || " $* " != *" $FAKE_TESTER_SYSTEMCTL_FAIL "* ]]
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
original_expiry=$(awk -F= '$1=="EXPIRES_AT"{print $2}' "$state")
grep -v '^CI_FLEET_TESTER_EXPIRES_AT=' "$root/etc/ci-fleet-tester/environments/preview-a.env" >"$tmp/spec"
mv "$tmp/spec" "$root/etc/ci-fleet-tester/environments/preview-a.env"; chmod 600 "$root/etc/ci-fleet-tester/environments/preview-a.env"
FAKE_TESTER_ROUTE_PORT=18080 "$runtime" --converge --environment preview-a >/dev/null
[[ $(awk -F= '$1=="EXPIRES_AT"{print $2}' "$state") == "$original_expiry" ]] || fail 'idempotent converge extended expiration'
write_environment preview-b 18080
if FAKE_TESTER_ROUTE_PORT=18080 "$runtime" --converge --environment preview-b >/dev/null 2>&1; then fail 'duplicate route port was accepted'; fi
for policy in mutable privileged bind broad-port external-network environment configs use-api-socket namespace-share false-nnp custom-volume volumes-from custom-network replicas lifecycle-hook gpu deploy-device; do
  write_environment "bad-$policy" 18081
  if FAKE_TESTER_ROUTE_PORT=18081 FAKE_TESTER_POLICY=$policy "$runtime" --converge --environment "bad-$policy" >/dev/null 2>&1; then fail "unsafe compose policy was accepted: $policy"; fi
done
if FAKE_TESTER_ROUTE_PORT=18080 FAKE_TESTER_POLICY=mutable "$runtime" --reset --environment preview-a >/dev/null 2>&1; then fail 'reset accepted invalid replacement'; fi
[[ -f $state ]] || fail 'reset deleted the incumbent before validation'
if FAKE_TESTER_ROUTE_PORT=18080 FAKE_TESTER_CONTAINER_STATE='exited unhealthy' "$runtime" --health >/dev/null 2>&1; then fail 'health accepted a stopped managed service'; fi
write_environment secret-preview 18082
secret_file=$root/etc/ci-fleet-tester/secrets/secret-preview/credential
printf 'example-test-scope-value\n' >"$secret_file"; chmod 600 "$secret_file"
FAKE_TESTER_ROUTE_PORT=18082 FAKE_TESTER_POLICY=valid-secret FAKE_TESTER_SECRET_FILE=$secret_file "$runtime" --converge --environment secret-preview >/dev/null
FAKE_TESTER_ROUTE_PORT=18082 "$runtime" --remove --environment secret-preview >/dev/null
outside_secret=$root/etc/ci-fleet-tester/secrets/outside
printf 'example-test-scope-value\n' >"$outside_secret"; chmod 600 "$outside_secret"
write_environment outside-secret 18083
if FAKE_TESTER_ROUTE_PORT=18083 FAKE_TESTER_POLICY=outside-secret FAKE_TESTER_SECRET_FILE=$outside_secret "$runtime" --converge --environment outside-secret >/dev/null 2>&1; then fail 'out-of-boundary secret was accepted'; fi
write_environment partial-up 18084
if FAKE_TESTER_ROUTE_PORT=18084 FAKE_TESTER_UP_FAIL=1 "$runtime" --converge --environment partial-up >/dev/null 2>&1; then fail 'partial activation succeeded'; fi
[[ -f $root/var/lib/ci-fleet-tester/environments/partial-up.state && -f $root/var/lib/ci-fleet-tester/environments/partial-up.compose.json ]] || fail 'partial activation was not tracked for cleanup'
FAKE_TESTER_ROUTE_PORT=18084 "$runtime" --remove --environment partial-up >/dev/null
write_environment immutable-remove 18085
FAKE_TESTER_ROUTE_PORT=18085 "$runtime" --converge --environment immutable-remove >/dev/null
rm "$root/etc/ci-fleet-tester/definitions/immutable-remove.yaml"
FAKE_TESTER_ROUTE_PORT=18085 "$runtime" --remove --environment immutable-remove >/dev/null
FAKE_TESTER_ROUTE_PORT=18080 "$runtime" --reset --environment preview-a >/dev/null
grep -q 'down --volumes --remove-orphans' "$tmp/docker.log" || fail 'reset did not remove only the scoped Compose project'
if grep -Eq 'system prune|volume prune|network prune' "$tmp/docker.log"; then fail 'global Docker prune was used'; fi
write_environment expired-a 18087
FAKE_TESTER_ROUTE_PORT=18087 "$runtime" --converge --environment expired-a >/dev/null
write_environment expired-b 18088
FAKE_TESTER_ROUTE_PORT=18088 "$runtime" --converge --environment expired-b >/dev/null
for expired_state in "$state" "$root/var/lib/ci-fleet-tester/environments/expired-a.state" "$root/var/lib/ci-fleet-tester/environments/expired-b.state"; do
  sed -i 's/^EXPIRES_AT=.*/EXPIRES_AT=1/' "$expired_state"
done
: >"$tmp/docker.log"
if FAKE_TESTER_DOWN_FAIL=1 "$runtime" --cleanup >/dev/null 2>&1; then fail 'cleanup ignored environment removal failures'; fi
for id in preview-a expired-a expired-b; do
  grep -Fq "ci-fleet-test-$id" "$tmp/docker.log" || fail "cleanup stopped before attempting $id"
done
"$runtime" --cleanup >/dev/null
[[ ! -e $state && ! -e $root/var/lib/ci-fleet-tester/environments/expired-a.state && ! -e $root/var/lib/ci-fleet-tester/environments/expired-b.state ]] || fail 'expired environment survived cleanup'

# Commit-backed installer tests run after the implementation commit exists.
ref=$(git -C "$repo_root" rev-parse HEAD)
if DOCKER_HOST=tcp://example.invalid:2375 "$installer" --check --config /etc/ci-fleet-tester/tester.env >/dev/null 2>&1; then fail 'installer accepted a remote Docker selector'; fi
"$installer" --install --config /etc/ci-fleet-tester/tester.env --ref "$ref" | grep -Fq INSTALL_OK || fail 'fresh install failed'
[[ $(readlink -f "$root/opt/ci-fleet-tester/current") == "$root/opt/ci-fleet-tester/releases/$ref" ]] || fail 'current release link is wrong'
"$installer" --install --config /etc/ci-fleet-tester/tester.env --ref "$ref" >/dev/null
check_output=$("$installer" --check --config /etc/ci-fleet-tester/tester.env)
grep -Fq CHECK_OK <<<"$check_output" || fail 'installed check failed'
for unit in ci-fleet-tester-health.service ci-fleet-tester-health.timer ci-fleet-tester-cleanup.service ci-fleet-tester-cleanup.timer; do [[ -f $root/etc/systemd/system/$unit ]] || fail "unit missing: $unit"; done
release=$root/opt/ci-fleet-tester/releases/$ref
chmod u+w "$release/scripts/tester-runtime.sh"; printf '# tamper\n' >>"$release/scripts/tester-runtime.sh"; chmod 0555 "$release/scripts/tester-runtime.sh"
if "$installer" --check --config /etc/ci-fleet-tester/tester.env >/dev/null 2>&1; then fail 'tampered installed release passed check'; fi
chmod u+w "$release/scripts/tester-runtime.sh"; git -C "$repo_root" show "$ref:scripts/tester-runtime.sh" >"$release/scripts/tester-runtime.sh"; chmod 0555 "$release/scripts/tester-runtime.sh"
"$installer" --check --config /etc/ci-fleet-tester/tester.env >/dev/null || fail 'restored release failed check'

# A syntactically valid candidate that fails its post-switch check restores the incumbent.
upgrade_repo=$tmp/upgrade-repo
git clone --quiet --shared "$repo_root" "$upgrade_repo"
printf '#!/usr/bin/env bash\nexit 1\n' >"$upgrade_repo/scripts/tester-runtime.sh"; chmod 0755 "$upgrade_repo/scripts/tester-runtime.sh"
git -C "$upgrade_repo" add scripts/tester-runtime.sh
git -C "$upgrade_repo" -c user.name=Example -c user.email=example@invalid.example commit --quiet -m 'fixture: fail tester activation'
bad_ref=$(git -C "$upgrade_repo" rev-parse HEAD)
if "$upgrade_repo/scripts/install-tester.sh" --upgrade --config /etc/ci-fleet-tester/tester.env --ref "$bad_ref" >/dev/null 2>&1; then fail 'failed candidate activation succeeded'; fi
[[ $(readlink -f "$root/opt/ci-fleet-tester/current") == "$root/opt/ci-fleet-tester/releases/$ref" ]] || fail 'failed upgrade did not restore incumbent release'

# Unit activation failures restore the incumbent symlink and units.
git -C "$upgrade_repo" show "$ref:scripts/tester-runtime.sh" >"$upgrade_repo/scripts/tester-runtime.sh"; chmod 0755 "$upgrade_repo/scripts/tester-runtime.sh"
git -C "$upgrade_repo" add scripts/tester-runtime.sh
git -C "$upgrade_repo" -c user.name=Example -c user.email=example@invalid.example commit --quiet -m 'fixture: valid tester candidate'
unit_fail_ref=$(git -C "$upgrade_repo" rev-parse HEAD)
unit_hash_before=$(sha256sum "$root/etc/systemd/system/ci-fleet-tester-health.service")
if FAKE_TESTER_SYSTEMCTL_FAIL=daemon-reload "$upgrade_repo/scripts/install-tester.sh" --upgrade --config /etc/ci-fleet-tester/tester.env --ref "$unit_fail_ref" >/dev/null 2>&1; then fail 'unit activation failure succeeded'; fi
[[ $(readlink -f "$root/opt/ci-fleet-tester/current") == "$root/opt/ci-fleet-tester/releases/$ref" && $(sha256sum "$root/etc/systemd/system/ci-fleet-tester-health.service") == "$unit_hash_before" ]] || fail 'unit activation failure did not restore incumbent release and units'

# Rollback switches only to a complete recorded release and keeps environments intact.
old=0000000000000000000000000000000000000000
cp -a "$root/opt/ci-fleet-tester/releases/$ref" "$root/opt/ci-fleet-tester/releases/$old"
chmod 0755 "$root/opt/ci-fleet-tester/releases/$old"
chmod 0644 "$root/opt/ci-fleet-tester/releases/$old/.ci-fleet-source-revision" "$root/opt/ci-fleet-tester/releases/$old/.ci-fleet-release.sha256"
printf '%s\n' "$old" >"$root/opt/ci-fleet-tester/releases/$old/.ci-fleet-source-revision"
(cd "$root/opt/ci-fleet-tester/releases/$old" && sha256sum scripts/tester-runtime.sh .ci-fleet-source-revision host/systemd/* >.ci-fleet-release.sha256)
chmod 0444 "$root/opt/ci-fleet-tester/releases/$old/.ci-fleet-source-revision" "$root/opt/ci-fleet-tester/releases/$old/.ci-fleet-release.sha256"
chmod 0555 "$root/opt/ci-fleet-tester/releases/$old"
printf '%s\n' "$old" >"$root/var/lib/ci-fleet-tester/last-known-good"; chmod 600 "$root/var/lib/ci-fleet-tester/last-known-good"
write_environment rollback-env 18086
FAKE_TESTER_ROUTE_PORT=18086 "$runtime" --converge --environment rollback-env >/dev/null
rollback_state_hash=$(sha256sum "$root/var/lib/ci-fleet-tester/environments/rollback-env.state")
"$installer" --rollback --config /etc/ci-fleet-tester/tester.env | grep -Fq ROLLBACK_OK || fail 'rollback failed'
[[ $(readlink -f "$root/opt/ci-fleet-tester/current") == "$root/opt/ci-fleet-tester/releases/$old" ]] || fail 'rollback selected the wrong release'
[[ $(sha256sum "$root/var/lib/ci-fleet-tester/environments/rollback-env.state") == "$rollback_state_hash" ]] || fail 'rollback changed active environment state'
FAKE_TESTER_ROUTE_PORT=18086 "$runtime" --remove --environment rollback-env >/dev/null

"$installer" --uninstall --config /etc/ci-fleet-tester/tester.env | grep -Fq UNINSTALL_OK || fail 'uninstall failed'
[[ -f $root/etc/ci-fleet-tester/tester.env && ! -L $root/opt/ci-fleet-tester/current ]] || fail 'uninstall did not preserve config/remove runtime'
printf 'TESTER_INSTALLER_TESTS_OK\n'
