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
real_systemctl=$(command -v systemctl || true)
mkdir -p "$fake_bin" "$root/etc/ci-fleet-tester/environments" "$root/etc/ci-fleet-tester/definitions" "$root/etc/ci-fleet-tester/secrets" "$root/var/lib/ci-fleet-tester/environments" "$root/var/lib/docker" "$root/var/lib/fake-tester-volume" "$root/var/run" "$root/etc/systemd/system"
mkdir -p "$root/run/lock"; chmod 1777 "$root/run/lock"
chmod 700 "$root/etc/ci-fleet-tester" "$root/etc/ci-fleet-tester/environments" "$root/etc/ci-fleet-tester/definitions" "$root/etc/ci-fleet-tester/secrets" "$root/var/lib/ci-fleet-tester" "$root/var/lib/ci-fleet-tester/environments"
printf 'ID=debian\nVERSION_ID=13\n' >"$root/etc/os-release"
: >"$root/var/run/docker.sock"
printf 'CI_FLEET_TESTER_DEFAULT_TTL_SECONDS=3600\nCI_FLEET_TESTER_MAX_ENVIRONMENTS=3\nCI_FLEET_TESTER_DISK_WARN_PERCENT=80\nCI_FLEET_TESTER_NETWORK_PROBE_HOST=tester-probe.invalid\nCI_FLEET_TESTER_HTTPS_PROBE_URL=https://tester-probe.invalid/health\nCI_FLEET_TESTER_ISOLATION_ACK=test-only-no-production-authority\n' >"$root/etc/ci-fleet-tester/tester.env"
chmod 600 "$root/etc/ci-fleet-tester/tester.env"
cp "$repo_root/scripts/fixtures/fake-tester-docker.sh" "$fake_bin/docker"
chmod 0755 "$fake_bin/docker"
cat >"$fake_bin/df" <<'EOF'
#!/usr/bin/env bash
used=${FAKE_TESTER_DISK_USED_PERCENT:-20}
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\nfixture 100 %s 80 %s%% /fixture\n' "$used" "$used"
EOF
cat >"$fake_bin/systemctl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${FAKE_TESTER_SYSTEMCTL_LOG:?}"
[[ -z ${FAKE_TESTER_EVENT_LOG:-} ]] || printf 'systemctl %s\n' "$*" >>"$FAKE_TESTER_EVENT_LOG"
if [[ ${FAKE_TESTER_SYSTEMCTL_FAIL_IF_UNITS_MISSING:-0} == 1 && $1 == disable && ! -e ${CI_FLEET_ROOT_PREFIX:?}/etc/systemd/system/ci-fleet-tester-health.timer ]]; then exit 5; fi
if [[ ${FAKE_TESTER_SYSTEMCTL_FAIL_WAIT:-0} == 1 && $1 == start && " $* " == *' ci-fleet-tester-health.service '* && " $* " != *' --no-block '* ]]; then exit 7; fi
if [[ $1 == list-unit-files || $1 == list-units ]]; then
  if [[ ${FAKE_TESTER_UNIT_INVENTORY_FAIL:-0} == 1 ]]; then printf 'inventory inspection failed\n' >&2; exit "${FAKE_TESTER_UNIT_INVENTORY_FAILURE_CODE:-7}"; fi
  if [[ ${FAKE_TESTER_EMPTY_UNIT_INVENTORY:-0} == 1 ]]; then
    [[ $1 != list-unit-files ]] || exit 1
    exit 0
  fi
  if [[ $1 == list-unit-files && " $* " == *'ci-fleet-'* && -z ${FAKE_TESTER_FOREIGN_UNIT:-} ]]; then exit 1; fi
  printf 'unrelated.service enabled\n'
  printf 'ci-fleet-tester-health.service enabled\n'
  if [[ -n ${FAKE_TESTER_FOREIGN_UNIT:-} && ${FAKE_TESTER_FOREIGN_UNIT_KIND:-$1} == "$1" ]]; then printf '%s enabled\n' "$FAKE_TESTER_FOREIGN_UNIT"; fi
  exit 0
fi
if [[ $1 == show && " $* " == *' --property=LoadState '* ]]; then
  if [[ -e ${CI_FLEET_ROOT_PREFIX:?}/etc/systemd/system/${*: -1} || ${FAKE_TESTER_EFFECTIVE_TIMERS:-0} == 1 || ${FAKE_TESTER_EFFECTIVE_SERVICES:-0} == 1 ]]; then printf 'loaded\n'; else printf 'not-found\n'; fi
  exit 0
fi
if [[ -n ${FAKE_TESTER_QUEUED_SERVICES:-} ]]; then
  if [[ $* == 'stop ci-fleet-tester-health.service ci-fleet-tester-cleanup.service' ]]; then rm -f "$FAKE_TESTER_QUEUED_SERVICES"; fi
  if [[ $1 == daemon-reload && -e $FAKE_TESTER_QUEUED_SERVICES ]]; then printf 'maintenance services remain queued\n' >&2; exit 9; fi
fi
if [[ $1 == show ]]; then
  reload=no
  [[ -z ${FAKE_TESTER_LOADED_POLICY_DRIFT:-} || ! -e $FAKE_TESTER_LOADED_POLICY_DRIFT ]] || reload=yes
  printf 'FragmentPath=%s/%s\nDropInPaths=%s\nNeedDaemonReload=%s\n' "${FAKE_TESTER_FRAGMENT_ROOT:-${CI_FLEET_ROOT_PREFIX:?}/etc/systemd/system}" "${*: -1}" "${FAKE_TESTER_UNIT_DROPINS:-}" "$reload"
fi
[[ $1 != daemon-reload || -z ${FAKE_TESTER_LOADED_POLICY_DRIFT:-} ]] || rm -f "$FAKE_TESTER_LOADED_POLICY_DRIFT"
if [[ -n ${FAKE_TESTER_SYSTEMCTL_WAIT_READY:-} && $1 == start && " $* " != *' --no-block '* ]]; then
  touch "$FAKE_TESTER_SYSTEMCTL_WAIT_READY"
  while [[ ! -e ${FAKE_TESTER_SYSTEMCTL_WAIT_RELEASE:?} ]]; do sleep 0.05; done
fi
[[ -z ${FAKE_TESTER_SYSTEMCTL_FAIL:-} || " $* " != *" $FAKE_TESTER_SYSTEMCTL_FAIL "* ]]
EOF
# shellcheck disable=SC2016 # Write the expansion for the fake to evaluate.
printf '%s\n' '#!/usr/bin/env bash' 'set -e' '[[ ${FAKE_TESTER_CURL_FAIL:-0} != 1 ]]' '[[ " $* " == *" --noproxy * "* ]]' >"$fake_bin/curl"
# shellcheck disable=SC2016 # Write the expansion for the fake to evaluate.
printf '%s\n' '#!/usr/bin/env bash' '[[ ${FAKE_TESTER_GETENT_FAIL:-0} != 1 ]]' >"$fake_bin/getent"
# shellcheck disable=SC2016 # Write the expansion for the fake to evaluate.
printf '%s\n' '#!/usr/bin/env bash' '[[ ${FAKE_TESTER_TMPFILES_FAIL:-0} != 1 ]]' >"$fake_bin/systemd-tmpfiles"
chmod 0755 "$fake_bin/df" "$fake_bin/systemctl" "$fake_bin/curl" "$fake_bin/getent" "$fake_bin/systemd-tmpfiles"
export FAKE_TESTER_REAL_TIMEOUT
FAKE_TESTER_REAL_TIMEOUT=$(command -v timeout)
cat >"$fake_bin/timeout" <<'EOF'
#!/usr/bin/env bash
if [[ ${3:-} == -c && ${4:-} == *'/dev/tcp/127.0.0.1/'* ]]; then
  printf '%s\n' "${6:-}" >>"${FAKE_TESTER_ROUTE_LOG:?}"
  [[ ${FAKE_TESTER_ROUTE_FAIL:-0} != 1 ]]
else
  exec "$FAKE_TESTER_REAL_TIMEOUT" "$@"
fi
EOF
chmod 0755 "$fake_bin/timeout"
export PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$root
export FAKE_TESTER_MODEL_PATH=$tmp/model-path FAKE_TESTER_ROUTE_LOG=$tmp/route.log
export FAKE_TESTER_DOCKER_ROOT=$root/var/lib/docker FAKE_TESTER_VOLUME_ROOT=$root/var/lib/fake-tester-volume FAKE_TESTER_DOCKER_LOG=$tmp/docker.log FAKE_TESTER_SYSTEMCTL_LOG=$tmp/systemctl.log
export FAKE_TESTER_EVENT_LOG=$tmp/events.log
export FAKE_TESTER_RESOURCE_STATE=$tmp/resources
mkdir "$FAKE_TESTER_RESOURCE_STATE"
for service in ci-fleet-tester-health.service ci-fleet-tester-cleanup.service; do
  if grep -q '^ConditionPathExists=/etc/ci-fleet-tester/tester.env$' "$repo_root/host/systemd/$service"; then fail "$service silently skips missing configuration"; fi
done
grep -Fxq 'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6' "$repo_root/host/systemd/ci-fleet-tester-health.service" || fail 'health service blocks required network probe sockets'
grep -Fxq 'RestrictAddressFamilies=AF_UNIX' "$repo_root/host/systemd/ci-fleet-tester-cleanup.service" || fail 'cleanup service permits network sockets'

standalone_installer=$tmp/install-tester.sh
cp "$installer" "$standalone_installer"
uninstall_bin=$tmp/uninstall-bin
mkdir "$uninstall_bin"
for command in bash chmod dirname env find flock grep id install mkdir rm stat; do ln -s "$(command -v "$command")" "$uninstall_bin/$command"; done
ln -s "$fake_bin/docker" "$uninstall_bin/docker"
ln -s "$fake_bin/systemctl" "$uninstall_bin/systemctl"
ln -s /missing "$root/etc/systemd/system/ci-fleet-tester-health.service"
ln -s /missing "$root/etc/systemd/system/ci-fleet-tester-health.timer"
PATH=$uninstall_bin "$standalone_installer" --uninstall --config /etc/ci-fleet-tester/tester.env >/dev/null || fail 'standalone uninstall required installation-only tools or a Git checkout'
[[ ! -L $root/etc/systemd/system/ci-fleet-tester-health.service && ! -L $root/etc/systemd/system/ci-fleet-tester-health.timer ]] || fail 'uninstall left dangling tester unit symlinks'
mv "$root/var/run/docker.sock" "$root/var/run/docker.real"
ln -s docker.real "$root/var/run/docker.sock"
if PATH=$uninstall_bin "$standalone_installer" --uninstall --config /etc/ci-fleet-tester/tester.env >/dev/null 2>&1; then fail 'uninstall accepted a symlinked Docker endpoint'; fi
rm "$root/var/run/docker.sock"; mv "$root/var/run/docker.real" "$root/var/run/docker.sock"
if FAKE_TESTER_DOCKER_ROOT=/remote/docker PATH=$uninstall_bin "$standalone_installer" --uninstall --config /etc/ci-fleet-tester/tester.env >/dev/null 2>&1; then fail 'uninstall accepted a Docker endpoint with the wrong managed root'; fi
chmod 0666 "$root/var/run/docker.sock"
if PATH=$uninstall_bin "$standalone_installer" --uninstall --config /etc/ci-fleet-tester/tester.env >/dev/null 2>&1; then fail 'uninstall accepted a world-writable Docker control socket'; fi
chmod 0600 "$root/var/run/docker.sock"

write_environment() {
  local id=$1 port=$2
  mkdir -p "$root/etc/ci-fleet-tester/secrets/$id"; chmod 700 "$root/etc/ci-fleet-tester/secrets/$id"
  printf 'services: {}\n' >"$root/etc/ci-fleet-tester/definitions/$id.yaml"
  chmod 644 "$root/etc/ci-fleet-tester/definitions/$id.yaml"
  printf 'CI_FLEET_TESTER_PROJECT=example-project\nCI_FLEET_TESTER_OWNER=example-owner\nCI_FLEET_TESTER_COMPOSE_FILE=%s\nCI_FLEET_TESTER_EXPIRES_AT=%s\nCI_FLEET_TESTER_ROUTE_SERVICE=web\nCI_FLEET_TESTER_ROUTE_PORT=%s\n' \
    "$root/etc/ci-fleet-tester/definitions/$id.yaml" "$(( $(date +%s) + 1800 ))" "$port" >"$root/etc/ci-fleet-tester/environments/$id.env"
  chmod 600 "$root/etc/ci-fleet-tester/environments/$id.env"
}

if [[ -n $real_systemctl ]]; then
  offline_root=$tmp/offline-systemd
  mkdir -p "$offline_root/etc/systemd/system"
  offline_status=0
  SYSTEMD_COLORS=0 "$real_systemctl" --root="$offline_root" list-unit-files --all --full --plain --no-legend --no-pager >"$tmp/offline-units.out" 2>&1 || offline_status=$?
  [[ $offline_status == 0 || $offline_status == 1 && ! -s $tmp/offline-units.out ]] || fail 'empty systemd inventory returned an inspection error'
  [[ ! -s $tmp/offline-units.out ]] || fail 'offline root unexpectedly listed unit files'
  printf '[Unit]\nDescription=Example foreign unit\n[Service]\nExecStart=/bin/true\n' >"$offline_root/etc/systemd/system/ci-fleet-deployer.service"
  SYSTEMD_COLORS=0 "$real_systemctl" --root="$offline_root" list-unit-files --all --full --plain --no-legend --no-pager >"$tmp/offline-units.out"
  grep -q '^ci-fleet-deployer.service ' "$tmp/offline-units.out" || fail 'unfiltered systemd inventory omitted a foreign unit'
fi
clean_root=$tmp/clean-root
mkdir -p "$clean_root/var/run" "$clean_root/var/lib/docker" "$clean_root/run/lock"
chmod 1777 "$clean_root/run/lock"
: >"$clean_root/var/run/docker.sock"; chmod 0600 "$clean_root/var/run/docker.sock"
CI_FLEET_ROOT_PREFIX=$clean_root FAKE_TESTER_DOCKER_ROOT=$clean_root/var/lib/docker "$installer" --uninstall >/dev/null || fail 'uninstall failed on a host without tester state'
if CI_FLEET_ROOT_PREFIX=$clean_root "$installer" --rollback >"$tmp/absent-rollback.out" 2>&1; then fail 'rollback succeeded without an installation'; fi
grep -Fq 'protected directory is unsafe' "$tmp/absent-rollback.out" || fail 'absent rollback failed outside existing-state validation'
for marker in etc/ci-fleet-tester var/lib/ci-fleet-tester opt/ci-fleet-tester; do
  [[ ! -e $clean_root/$marker && ! -L $clean_root/$marker ]] || fail 'a no-op command created tester role state'
done
mkdir "$clean_root/etc"
printf 'ID=debian\nVERSION_ID=13\n' >"$clean_root/etc/os-release"
clean_ref=$(git -C "$repo_root" rev-parse HEAD)
if CI_FLEET_ROOT_PREFIX=$clean_root FAKE_TESTER_DOCKER_ROOT=$clean_root/var/lib/docker "$installer" --install --ref "$clean_ref" >"$tmp/absent-config.out" 2>&1; then fail 'install accepted absent configuration'; fi
grep -Fq 'protected directory is unsafe' "$tmp/absent-config.out" || fail 'absent configuration failed outside input validation'
for marker in etc/ci-fleet-tester var/lib/ci-fleet-tester opt/ci-fleet-tester; do
  [[ ! -e $clean_root/$marker ]] || fail 'missing configuration created tester role state'
done
install -d -m 0700 "$clean_root/etc/ci-fleet-tester"
cp "$root/etc/ci-fleet-tester/tester.env" "$clean_root/etc/ci-fleet-tester/tester.env"
chmod 0644 "$clean_root/etc/ci-fleet-tester/tester.env"
if CI_FLEET_ROOT_PREFIX=$clean_root FAKE_TESTER_DOCKER_ROOT=$clean_root/var/lib/docker "$installer" --install --ref "$clean_ref" >"$tmp/unsafe-config.out" 2>&1; then fail 'install accepted unsafe configuration'; fi
grep -Fq 'protected file is unsafe' "$tmp/unsafe-config.out" || fail 'unsafe configuration failed outside input validation'
chmod 0600 "$clean_root/etc/ci-fleet-tester/tester.env"
if CI_FLEET_ROOT_PREFIX=$clean_root FAKE_TESTER_DOCKER_ROOT=$clean_root/var/lib/docker "$installer" --upgrade --ref "$clean_ref" >"$tmp/absent-upgrade.out" 2>&1; then fail 'upgrade accepted an absent installation'; fi
grep -Fq 'tester is not installed' "$tmp/absent-upgrade.out" || fail 'absent upgrade failed outside input validation'
for marker in var/lib/ci-fleet-tester opt/ci-fleet-tester etc/ci-fleet-tester/environments etc/ci-fleet-tester/definitions etc/ci-fleet-tester/secrets; do
  [[ ! -e $clean_root/$marker ]] || fail 'invalid install input created tester installation directories'
done
if FAKE_TESTER_EMPTY_ADDRESS_POOLS=1 "$runtime" --check >/dev/null 2>&1; then fail 'runtime accepted absent explicit Docker address pools'; fi
"$runtime" --check | grep -Fq CHECK_OK || fail 'runtime preflight failed'
FAKE_TESTER_EMPTY_UNIT_INVENTORY=1 "$runtime" --check >/dev/null || fail 'empty foreign unit inventory blocked runtime preflight'
cp "$root/etc/ci-fleet-tester/tester.env" "$tmp/tester.env.valid"
sed 's@https://tester-probe.invalid/health@https://elsewhere.invalid/health@' "$tmp/tester.env.valid" >"$root/etc/ci-fleet-tester/tester.env"
if "$runtime" --check >/dev/null 2>&1; then fail 'check accepted different DNS and HTTPS probe hosts'; fi
if "$runtime" --health >/dev/null 2>&1; then fail 'health accepted different DNS and HTTPS probe hosts'; fi
sed 's@https://tester-probe.invalid/health@https://TESTER-PROBE.INVALID:8443/health@' "$tmp/tester.env.valid" >"$root/etc/ci-fleet-tester/tester.env"
"$runtime" --check >/dev/null || fail 'matching probe hostname with different case and an explicit port was rejected'
cp "$tmp/tester.env.valid" "$root/etc/ci-fleet-tester/tester.env"
if FAKE_TESTER_UNRELATED_RESOURCE='other-container|ordinary-ci' "$runtime" --check >/dev/null 2>&1; then fail 'tester admitted unrelated Docker state'; fi
ln -s /missing "$root/var/lib/ci-fleet-deployer"
if "$runtime" --check >/dev/null 2>&1; then fail 'tester runtime admitted a dangling deployer marker'; fi
rm "$root/var/lib/ci-fleet-deployer"
chmod 0666 "$root/var/run/docker.sock"
if "$runtime" --check >/dev/null 2>&1; then fail 'runtime accepted a world-writable Docker control socket'; fi
chmod 0600 "$root/var/run/docker.sock"
[[ $(stat -c %a "$root/run/lock") == 1777 && $(stat -c %a "$root/run/lock/ci-fleet-tester") == 755 ]] || fail 'runtime changed shared lock-directory permissions'
rm -rf "$root/run/lock/ci-fleet-tester"; mkdir "$tmp/lock-target"; chmod 700 "$tmp/lock-target"; ln -s "$tmp/lock-target" "$root/run/lock/ci-fleet-tester"
if "$runtime" --check >/dev/null 2>&1; then fail 'symlinked lifecycle lock directory was accepted'; fi
[[ $(stat -c %a "$tmp/lock-target") == 700 ]] || fail 'symlinked lock target permissions changed'
rm "$root/run/lock/ci-fleet-tester"
if FAKE_TESTER_DOCKER_ROOT=/remote/docker "$runtime" --check >/dev/null 2>&1; then fail 'remote Docker daemon was accepted'; fi
write_environment secretless 18079
: >"$tmp/docker.log"
if FAKE_TESTER_EMPTY_ADDRESS_POOLS=1 FAKE_TESTER_ROUTE_PORT=18079 "$runtime" --converge --environment secretless >/dev/null 2>&1; then fail 'converge accepted absent explicit Docker address pools'; fi
[[ ! -e $root/var/lib/ci-fleet-tester/environments/secretless.state ]] || fail 'missing address pools reached tracked state creation'
if grep -Fq 'up -d' "$tmp/docker.log"; then fail 'missing address pools reached activation'; fi
rmdir "$root/etc/ci-fleet-tester/secrets/secretless"
FAKE_TESTER_ROUTE_PORT=18079 "$runtime" --converge --environment secretless >/dev/null || fail 'secretless environment required an empty secret directory'
FAKE_TESTER_ROUTE_PORT=18079 "$runtime" --remove --environment secretless >/dev/null
write_environment preview-a 18080
FAKE_TESTER_ROUTE_PORT=18080 "$runtime" --converge --environment preview-a | grep -Fq CONVERGED || fail 'converge failed'
state=$root/var/lib/ci-fleet-tester/environments/preview-a.state
[[ -f $state && $(stat -c %a "$state") == 600 ]] || fail 'state was not protected'
if FAKE_TESTER_ROUTE_PORT=18080 FAKE_TESTER_PREEXISTING_VOLUME=ci-fleet-test-preview-a_data "$runtime" --converge --environment preview-a >/dev/null 2>&1; then fail 'converge accepted a pre-existing bind-backed volume'; fi
if FAKE_TESTER_ROUTE_PORT=18080 FAKE_TESTER_PREEXISTING_NETWORK=ci-fleet-test-preview-a_default "$runtime" --converge --environment preview-a >/dev/null 2>&1; then fail 'converge accepted a pre-existing network outside approved IPAM'; fi
inspect_output=$(FAKE_TESTER_ROUTE_PORT=18080 "$runtime" --inspect --environment preview-a)
grep -q 'IMAGE_DIGESTS=sha256:[a-f0-9]\{64\}.*STATUS=running DISK_BYTES=[1-9][0-9]*' <<<"$inspect_output" || fail 'inspect did not report health and disk use'
if FAKE_TESTER_PS_FAIL=1 "$runtime" --inspect --environment preview-a >/dev/null 2>&1; then fail 'container inventory failure was hidden'; fi
if FAKE_TESTER_VOLUME_LS_FAIL=1 "$runtime" --inspect --environment preview-a >/dev/null 2>&1; then fail 'volume inventory failure was hidden'; fi
deployed_inode=$(stat -c %i "$root/var/lib/ci-fleet-tester/environments/preview-a.compose.json")
FAKE_TESTER_ROUTE_PORT=18080 "$runtime" --converge --environment preview-a >/dev/null
[[ $(find "$root/var/lib/ci-fleet-tester/environments" -name '*.state' | wc -l) == 1 ]] || fail 'idempotent converge duplicated state'
[[ $(stat -c %i "$root/var/lib/ci-fleet-tester/environments/preview-a.compose.json") == "$deployed_inode" ]] || fail 'idempotent converge replaced the protected Compose snapshot'
original_expiry=$(awk -F= '$1=="EXPIRES_AT"{print $2}' "$state")
grep -v '^CI_FLEET_TESTER_EXPIRES_AT=' "$root/etc/ci-fleet-tester/environments/preview-a.env" >"$tmp/spec"
mv "$tmp/spec" "$root/etc/ci-fleet-tester/environments/preview-a.env"; chmod 600 "$root/etc/ci-fleet-tester/environments/preview-a.env"
FAKE_TESTER_ROUTE_PORT=18080 "$runtime" --converge --environment preview-a >/dev/null
[[ $(awk -F= '$1=="EXPIRES_AT"{print $2}' "$state") == "$original_expiry" ]] || fail 'idempotent converge extended expiration'
FAKE_TESTER_ROUTE_PORT=18080 FAKE_TESTER_MIXED_ADDRESS_POOLS=1 "$runtime" --converge --environment preview-a >/dev/null || fail 'IPv6 pool prevented matching the approved IPv4 network'
for teardown in remove cleanup; do
  id=role-drift-$teardown
  write_environment "$id" 18097
  FAKE_TESTER_ROUTE_PORT=18097 "$runtime" --converge --environment "$id" >/dev/null
  mkdir -p "$root/opt/ci-fleet-status"
  ln -s /missing "$root/opt/ci-fleet-status/current"
  : >"$tmp/docker.log"
  if [[ $teardown == remove ]]; then
    FAKE_TESTER_FOREIGN_UNIT=ci-fleet-deployer.service FAKE_TESTER_UNRELATED_RESOURCE='other-container|ordinary-ci' "$runtime" --remove --environment "$id" >/dev/null || fail 'role drift blocked scoped removal'
  else
    sed -i 's/^EXPIRES_AT=.*/EXPIRES_AT=1/' "$root/var/lib/ci-fleet-tester/environments/$id.state"
    FAKE_TESTER_FOREIGN_UNIT=ci-fleet-deployer.service FAKE_TESTER_UNRELATED_RESOURCE='other-container|ordinary-ci' "$runtime" --cleanup >/dev/null || fail 'role drift blocked expiration cleanup'
  fi
  [[ ! -e $root/var/lib/ci-fleet-tester/environments/$id.state && -L $root/opt/ci-fleet-status/current ]] || fail 'teardown removed foreign role state or retained tester state'
  grep -Fq "ci-fleet-test-$id" "$tmp/docker.log" || fail 'teardown did not address its Compose project'
  if grep -Eq '(^| )prune( |$)|other-container' "$tmp/docker.log"; then fail 'teardown addressed unrelated Docker resources'; fi
  rm "$root/opt/ci-fleet-status/current"; rmdir "$root/opt/ci-fleet-status"
done
write_environment finite-swap 18099
FAKE_TESTER_ROUTE_PORT=18099 FAKE_TESTER_POLICY=finite-swap "$runtime" --converge --environment finite-swap >/dev/null || fail 'finite memory-plus-swap limit was rejected'
FAKE_TESTER_ROUTE_PORT=18099 "$runtime" --remove --environment finite-swap >/dev/null
write_environment preview-b 18080
if FAKE_TESTER_ROUTE_PORT=18080 "$runtime" --converge --environment preview-b >/dev/null 2>&1; then fail 'duplicate route port was accepted'; fi
for policy in mutable privileged bind broad-port udp-route external-network environment configs use-api-socket namespace-share false-nnp unconfined custom-volume volumes-from external-links userns-host cgroup-host uts-host remote-logging custom-network ipam replicas scale lifecycle-hook provider pre-start models service-models top-models anonymous-volume undeclared-volume gpu deploy-device build profiles label-file runtime unbounded unlimited-swap oom-priority; do
  write_environment "bad-$policy" 18081
  : >"$tmp/docker.log"
  if FAKE_TESTER_ROUTE_PORT=18081 FAKE_TESTER_POLICY=$policy "$runtime" --converge --environment "bad-$policy" >/dev/null 2>&1; then fail "unsafe compose policy was accepted: $policy"; fi
  if grep -Fq 'up -d' "$tmp/docker.log"; then fail "unsafe policy reached activation: $policy"; fi
done
for policy in wrong-volume host-network extra-network bind-volume oom-disabled oom-priority ipv6-enabled ipv6-allocation; do
  if FAKE_TESTER_LIVE_POLICY=$policy "$runtime" --health >/dev/null 2>&1; then fail "unsafe live policy passed health: $policy"; fi
done
if FAKE_TESTER_ROUTE_FAIL=1 "$runtime" --health >/dev/null 2>&1; then fail 'unreachable loopback route passed health'; fi
grep -Fxq 18080 "$tmp/route.log" || fail 'health did not probe the declared route port'
for orphan in FAKE_TESTER_ORPHAN_PROJECT FAKE_TESTER_ORPHAN_VOLUME_PROJECT FAKE_TESTER_ORPHAN_NETWORK_PROJECT; do
  if env "$orphan=ci-fleet-test-missing" "$runtime" --remove --environment missing >/dev/null 2>&1; then fail 'removal falsely succeeded for a project with missing state'; fi
done
for policy in ipv4-disabled ipv6-enabled service-tmpfs device-cgroup duplicate-network duplicate-volume capdrop-extra; do
  write_environment "$policy" 18098
  : >"$tmp/docker.log"
  if FAKE_TESTER_ROUTE_PORT=18098 FAKE_TESTER_POLICY=$policy "$runtime" --converge --environment "$policy" >"$tmp/model-rejection.out" 2>&1; then fail "unsupported model was accepted: $policy"; fi
  case $policy in
    capdrop-extra) grep -Fq 'read_only and cap_drop ALL are required' "$tmp/model-rejection.out" || fail 'extra capability drops failed outside model validation' ;;
    device-cgroup) grep -Fq 'build/device/capability/external mount/global identity is forbidden' "$tmp/model-rejection.out" || fail 'device rules failed outside model validation' ;;
    duplicate-network|duplicate-volume) grep -Fq 'duplicate resolved resource name' "$tmp/model-rejection.out" || fail 'duplicate names failed outside model validation' ;;
    ipv4-disabled) grep -Fq 'IPv4 is required' "$tmp/model-rejection.out" || fail 'IPv4-disabled network failed outside model validation' ;;
    ipv6-enabled) grep -Fq 'IPv6 is unsupported' "$tmp/model-rejection.out" || fail 'IPv6-enabled network failed outside model validation' ;;
    service-tmpfs) grep -Fq 'service-level tmpfs is unsupported' "$tmp/model-rejection.out" || fail 'service tmpfs failed outside model validation' ;;
  esac
  [[ ! -e $root/var/lib/ci-fleet-tester/environments/$policy.state ]] || fail "unsupported model created tracked state: $policy"
  if grep -Fq 'up -d' "$tmp/docker.log"; then fail "unsupported model reached activation: $policy"; fi
done
proxy_config=$tmp/proxy-client
mkdir "$proxy_config"
printf '%s\n' '{"proxies":{"default":{"httpProxy":"http://proxy.invalid:3128"}}}' >"$proxy_config/config.json"
proxy_config_hash=$(sha256sum "$proxy_config/config.json")
for client_config_mode in inherited default; do
  client_environment=(env FAKE_TESTER_EXPECT_EMPTY_CLIENT_CONFIG=1 "FAKE_TESTER_INHERITED_CLIENT_CONFIG=$proxy_config" "FAKE_TESTER_CLIENT_CONFIG_LOG=$tmp/client-config.log")
  if [[ $client_config_mode == inherited ]]; then client_environment+=("DOCKER_CONFIG=$proxy_config"); else client_environment=(env -u DOCKER_CONFIG "${client_environment[@]:1}"); fi
  : >"$tmp/client-config.log"
  "${client_environment[@]}" "$runtime" --converge --environment preview-a >/dev/null || fail 'Docker client isolation prevented valid convergence'
  "${client_environment[@]}" "$runtime" --health >/dev/null || fail 'Docker client isolation prevented health checks'
  [[ -s $tmp/client-config.log ]] || fail 'Compose did not verify its client configuration'
  while IFS= read -r client_config; do [[ ! -e $client_config ]] || fail 'Docker client configuration was not cleaned up'; done <"$tmp/client-config.log"
done
[[ $(sha256sum "$proxy_config/config.json") == "$proxy_config_hash" ]] || fail 'runtime changed the inherited Docker client configuration'
write_environment source-policy 18096
for source in 'services: {web: {extends: {file: /etc/passwd, service: web}}}' '!!str &external include: [{path: /etc/passwd}]' '!!str &external label_file: /etc/passwd' 'services: {web: {env_file: /etc/passwd}}' 'services: {}\nservices: {}'; do
  printf '%b\n' "$source" >"$root/etc/ci-fleet-tester/definitions/source-policy.yaml"
  : >"$tmp/docker.log"
  if FAKE_TESTER_ROUTE_PORT=18096 "$runtime" --converge --environment source-policy >/dev/null 2>&1; then fail 'external or ambiguous YAML source was accepted'; fi
  if grep -Fq 'config --format json' "$tmp/docker.log"; then fail 'unsafe YAML reached Compose rendering'; fi
done
write_environment bad-include 18091
printf 'include:\n  - path: /etc/passwd\nservices: {}\n' >"$root/etc/ci-fleet-tester/definitions/bad-include.yaml"
if FAKE_TESTER_ROUTE_PORT=18091 "$runtime" --converge --environment bad-include >/dev/null 2>&1; then fail 'Compose include was accepted before rendering'; fi
printf '"incl\\u0075de": [{path: /etc/passwd}]\nservices: {}\n' >"$root/etc/ci-fleet-tester/definitions/bad-include.yaml"
if FAKE_TESTER_ROUTE_PORT=18091 "$runtime" --converge --environment bad-include >/dev/null 2>&1; then fail 'escaped Compose include was accepted'; fi
printf '? "include":\n  - path: /etc/passwd\nservices: {}\n' >"$root/etc/ci-fleet-tester/definitions/bad-include.yaml"
if FAKE_TESTER_ROUTE_PORT=18091 "$runtime" --converge --environment bad-include >/dev/null 2>&1; then fail 'explicit-key Compose include was accepted'; fi
printf "? 'include':\n  - path: /etc/passwd\nservices: {}\n" >"$root/etc/ci-fleet-tester/definitions/bad-include.yaml"
if FAKE_TESTER_ROUTE_PORT=18091 "$runtime" --converge --environment bad-include >/dev/null 2>&1; then fail 'single-quoted explicit-key Compose include was accepted'; fi
printf '%s\n' '? "incl\u0075de"' ': [{path: /etc/passwd, env_file: /etc/passwd}]' 'services: {}' >"$root/etc/ci-fleet-tester/definitions/bad-include.yaml"
if FAKE_TESTER_ROUTE_PORT=18091 "$runtime" --converge --environment bad-include >/dev/null 2>&1; then fail 'escaped explicit-key Compose include was accepted'; fi
write_environment bad-source-label 18092
printf 'services:\n  web:\n    label_file: /root/credential.env\n' >"$root/etc/ci-fleet-tester/definitions/bad-source-label.yaml"
if FAKE_TESTER_ROUTE_PORT=18092 "$runtime" --converge --environment bad-source-label >/dev/null 2>&1; then fail 'source Compose label_file was accepted before rendering'; fi
write_environment bad-tagged-label 18093
printf 'services:\n  web:\n    !<tag:yaml.org,2002:str> label_file: /root/credential.env\n' >"$root/etc/ci-fleet-tester/definitions/bad-tagged-label.yaml"
if FAKE_TESTER_ROUTE_PORT=18093 "$runtime" --converge --environment bad-tagged-label >/dev/null 2>&1; then fail 'verbatim-tagged Compose label_file was accepted before rendering'; fi
write_environment bad-aliased-label 18094
printf 'x-key: &external-key label_file\nservices:\n  web:\n    *external-key: /root/credential.env\n' >"$root/etc/ci-fleet-tester/definitions/bad-aliased-label.yaml"
if FAKE_TESTER_ROUTE_PORT=18094 "$runtime" --converge --environment bad-aliased-label >/dev/null 2>&1; then fail 'aliased Compose label_file was accepted before rendering'; fi
write_environment bad-flow-explicit-alias 18095
printf '{x-key: &external-key include, ? *external-key : [{path: /root/other.yaml, env_file: /root/credential.env}], services: {}}\n' >"$root/etc/ci-fleet-tester/definitions/bad-flow-explicit-alias.yaml"
if FAKE_TESTER_ROUTE_PORT=18095 "$runtime" --converge --environment bad-flow-explicit-alias >/dev/null 2>&1; then fail 'flow-form explicit aliased Compose include was accepted before rendering'; fi
write_environment bad-anchored-key 18096
printf '&external include: [{path: /root/other.yaml, env_file: /root/credential.env}]\nservices: {}\n' >"$root/etc/ci-fleet-tester/definitions/bad-anchored-key.yaml"
if FAKE_TESTER_ROUTE_PORT=18096 "$runtime" --converge --environment bad-anchored-key >/dev/null 2>&1; then fail 'anchored Compose include key was accepted before rendering'; fi
write_environment interpolation 18090
TOKEN=must-not-render FAKE_TESTER_ROUTE_PORT=18090 FAKE_TESTER_POLICY=interpolation "$runtime" --converge --environment interpolation >/dev/null
if grep -Fq must-not-render "$root/var/lib/ci-fleet-tester/environments/interpolation.compose.json"; then fail 'caller environment was interpolated into the Compose model'; fi
FAKE_TESTER_ROUTE_PORT=18090 "$runtime" --remove --environment interpolation >/dev/null
deployed_hash=$(sha256sum "$root/var/lib/ci-fleet-tester/environments/preview-a.compose.json")
if FAKE_TESTER_ROUTE_PORT=18080 FAKE_TESTER_POLICY=changed-model "$runtime" --converge --environment preview-a >/dev/null 2>&1; then fail 'converge replaced the incumbent Compose model without reset'; fi
[[ $(sha256sum "$root/var/lib/ci-fleet-tester/environments/preview-a.compose.json") == "$deployed_hash" ]] || fail 'rejected model replacement changed incumbent state'
if FAKE_TESTER_ROUTE_PORT=18080 FAKE_TESTER_POLICY=mutable "$runtime" --reset --environment preview-a >/dev/null 2>&1; then fail 'reset accepted invalid replacement'; fi
[[ -f $state ]] || fail 'reset deleted the incumbent before validation'
if FAKE_TESTER_ROUTE_PORT=18080 FAKE_TESTER_CONTAINER_STATE='exited unhealthy' "$runtime" --health >/dev/null 2>&1; then fail 'health accepted a stopped managed service'; fi
FAKE_TESTER_ROUTE_PORT=18080 FAKE_TESTER_CONTAINER_STATE='running none' "$runtime" --converge --environment preview-a >/dev/null || fail 'converge rejected a reachable route without a Docker healthcheck'
FAKE_TESTER_ROUTE_PORT=18080 FAKE_TESTER_CONTAINER_STATE='running none' "$runtime" --health >/dev/null || fail 'health rejected a reachable route without a Docker healthcheck'
if FAKE_TESTER_ROUTE_PORT=18080 FAKE_TESTER_CONTAINER_STATE='running none' FAKE_TESTER_ROUTE_FAIL=1 "$runtime" --health >/dev/null 2>&1; then fail 'health accepted an unreachable route without a Docker healthcheck'; fi
if FAKE_TESTER_ROUTE_PORT=18080 FAKE_TESTER_CONTAINER_IMAGE="registry.example/example/app@sha256:$(printf 'b%.0s' {1..64})" "$runtime" --health >/dev/null 2>&1; then fail 'health accepted a container using an unapproved image digest'; fi
for live_policy in privileged bind broad-port unbounded unlimited-swap device-cgroup devices device-requests; do
  if FAKE_TESTER_ROUTE_PORT=18080 FAKE_TESTER_LIVE_POLICY=$live_policy "$runtime" --health >/dev/null 2>&1; then fail "health accepted unsafe live container settings: $live_policy"; fi
done
legacy_model=$root/var/lib/ci-fleet-tester/environments/preview-a.compose.json
cp "$legacy_model" "$tmp/legacy-model.json"
python3 - "$legacy_model" <<'PY_LEGACY'
import json,sys
path=sys.argv[1]; model=json.load(open(path)); model['services']['web']['memswap_limit']=-1
with open(path,'w') as output: json.dump(model,output)
PY_LEGACY
if FAKE_TESTER_ROUTE_PORT=18080 FAKE_TESTER_LIVE_POLICY=unlimited-swap "$runtime" --health >/dev/null 2>&1; then fail 'health accepted unlimited swap from an older protected model'; fi
mv "$tmp/legacy-model.json" "$legacy_model"

if FAKE_TESTER_ROUTE_PORT=18080 FAKE_TESTER_DISK_USED_PERCENT=80 "$runtime" --health >/dev/null 2>&1; then fail 'scheduled health accepted Docker storage at the configured warning threshold'; fi
if FAKE_TESTER_ROUTE_PORT=18080 FAKE_TESTER_GETENT_FAIL=1 "$runtime" --health >/dev/null 2>&1; then fail 'scheduled health ignored a failed DNS probe'; fi
if FAKE_TESTER_ROUTE_PORT=18080 FAKE_TESTER_CURL_FAIL=1 "$runtime" --health >/dev/null 2>&1; then fail 'scheduled health ignored a failed HTTPS probe'; fi
write_environment service-map 18090
FAKE_TESTER_ROUTE_PORT=18090 FAKE_TESTER_POLICY=two-services "$runtime" --converge --environment service-map >/dev/null
if FAKE_TESTER_ROUTE_PORT=18090 FAKE_TESTER_POLICY=two-services FAKE_TESTER_DUPLICATE_SERVICE=1 "$runtime" --inspect --environment service-map | grep -q 'STATUS=running'; then fail 'health accepted duplicate and missing service identities'; fi
FAKE_TESTER_ROUTE_PORT=18090 FAKE_TESTER_POLICY=two-services "$runtime" --remove --environment service-map >/dev/null
write_environment secret-preview 18082
secret_file=$root/etc/ci-fleet-tester/secrets/secret-preview/credential
printf 'example-test-scope-value\n' >"$secret_file"; chmod 600 "$secret_file"
for secret_target in credential /etc/app/token; do
  FAKE_TESTER_ROUTE_PORT=18082 FAKE_TESTER_POLICY=valid-secret FAKE_TESTER_SECRET_FILE=$secret_file FAKE_TESTER_SECRET_TARGET=$secret_target "$runtime" --converge --environment secret-preview >/dev/null || fail "valid secret target was rejected: $secret_target"
  FAKE_TESTER_ROUTE_PORT=18082 "$runtime" --remove --environment secret-preview >/dev/null
done
outside_secret=$root/etc/ci-fleet-tester/secrets/outside
printf 'example-test-scope-value\n' >"$outside_secret"; chmod 600 "$outside_secret"
write_environment outside-secret 18083
if FAKE_TESTER_ROUTE_PORT=18083 FAKE_TESTER_POLICY=outside-secret FAKE_TESTER_SECRET_FILE=$outside_secret "$runtime" --converge --environment outside-secret >/dev/null 2>&1; then fail 'out-of-boundary secret was accepted'; fi
write_environment hardlink-secret 18089
ln "$outside_secret" "$root/etc/ci-fleet-tester/secrets/hardlink-secret/credential"
if FAKE_TESTER_ROUTE_PORT=18089 FAKE_TESTER_POLICY=valid-secret FAKE_TESTER_SECRET_FILE=$root/etc/ci-fleet-tester/secrets/hardlink-secret/credential "$runtime" --converge --environment hardlink-secret >/dev/null 2>&1; then fail 'hard-linked host secret was accepted'; fi
write_environment partial-up 18084
if FAKE_TESTER_ROUTE_PORT=18084 FAKE_TESTER_UP_FAIL=1 "$runtime" --converge --environment partial-up >/dev/null 2>&1; then fail 'partial activation succeeded'; fi
[[ -f $root/var/lib/ci-fleet-tester/environments/partial-up.state && -f $root/var/lib/ci-fleet-tester/environments/partial-up.compose.json ]] || fail 'partial activation was not tracked for cleanup'
grep -q 'up -d --remove-orphans --wait --wait-timeout 60' "$tmp/docker.log" || fail 'Compose activation wait was not bounded'
FAKE_TESTER_ROUTE_PORT=18084 "$runtime" --remove --environment partial-up >/dev/null
for remaining_kind in container volume network; do
  write_environment retained-removal 18084
  FAKE_TESTER_ROUTE_PORT=18084 "$runtime" --converge --environment retained-removal >/dev/null
  retained_state=$root/var/lib/ci-fleet-tester/environments/retained-removal.state
  retained_compose=$root/var/lib/ci-fleet-tester/environments/retained-removal.compose.json
  retained_hash=$(sha256sum "$retained_state" "$retained_compose")
  if FAKE_TESTER_DOWN_LEAVES=$remaining_kind "$runtime" --remove --environment retained-removal >"$tmp/removal.log" 2>&1; then fail "removal ignored a remaining $remaining_kind"; fi
  if grep -Fq REMOVED "$tmp/removal.log"; then fail 'incomplete removal reported success'; fi
  [[ $(sha256sum "$retained_state" "$retained_compose") == "$retained_hash" ]] || fail 'incomplete removal lost recovery state'
  timeout 10 "$runtime" --remove --environment retained-removal >/dev/null || fail 'retained removal state could not be retried'
done
# Cancellation and forced timeout termination must leave scoped cleanup possible.
incumbent_hash=$(sha256sum "$state" "$root/var/lib/ci-fleet-tester/environments/preview-a.compose.json")
for signal in TERM KILL; do
  id="interrupted-${signal,,}"
  write_environment "$id" 18084
  set +e
  FAKE_TESTER_ROUTE_PORT=18084 FAKE_TESTER_UP_SIGNAL=$signal "$runtime" --converge --environment "$id" >"$tmp/interrupted.log" 2>&1
  interrupted_rc=$?
  set -e
  expected_rc=143; [[ $signal != KILL ]] || expected_rc=137
  [[ $interrupted_rc == "$expected_rc" ]] || fail "$signal did not terminate activation"
  interrupted_state=$root/var/lib/ci-fleet-tester/environments/$id.state
  interrupted_compose=$root/var/lib/ci-fleet-tester/environments/$id.compose.json
  [[ -f $interrupted_state && -f $interrupted_compose && $(stat -c %a "$interrupted_state") == 600 && $(stat -c %a "$interrupted_compose") == 600 ]] || fail "$signal lost protected cleanup state"
  python3 -m json.tool "$interrupted_compose" >/dev/null || fail "$signal left an incomplete Compose snapshot"
  sed -i 's/^EXPIRES_AT=.*/EXPIRES_AT=1/' "$interrupted_state"
  : >"$tmp/docker.log"
  timeout 10 "$runtime" --cleanup >/dev/null || fail "$signal blocked subsequent expiration cleanup"
  grep -Fq -- "-p ci-fleet-test-$id -f $interrupted_compose down --timeout 10 --volumes --remove-orphans" "$tmp/docker.log" || fail "$signal cleanup did not target the interrupted project"
  [[ ! -e $interrupted_state && ! -e $interrupted_compose ]] || fail "$signal left expired state behind"
  [[ $(sha256sum "$state" "$root/var/lib/ci-fleet-tester/environments/preview-a.compose.json") == "$incumbent_hash" ]] || fail "$signal cleanup changed the incumbent environment"
  if grep -Fq -- '-p ci-fleet-test-preview-a' "$tmp/docker.log"; then fail "$signal cleanup touched the unexpired environment"; fi
done
write_environment immutable-remove 18085
FAKE_TESTER_ROUTE_PORT=18085 "$runtime" --converge --environment immutable-remove >/dev/null
rm "$root/etc/ci-fleet-tester/definitions/immutable-remove.yaml"
FAKE_TESTER_ROUTE_PORT=18085 "$runtime" --remove --environment immutable-remove >/dev/null
FAKE_TESTER_ROUTE_PORT=18080 "$runtime" --reset --environment preview-a >/dev/null
grep -q 'down --timeout 10 --volumes --remove-orphans' "$tmp/docker.log" || fail 'reset did not use bounded scoped Compose teardown'
if grep -Eq 'system prune|volume prune|network prune' "$tmp/docker.log"; then fail 'global Docker prune was used'; fi
write_environment expired-a 18087
FAKE_TESTER_ROUTE_PORT=18087 "$runtime" --converge --environment expired-a >/dev/null
write_environment expired-b 18088
FAKE_TESTER_ROUTE_PORT=18088 "$runtime" --converge --environment expired-b >/dev/null
for expired_state in "$state" "$root/var/lib/ci-fleet-tester/environments/expired-a.state" "$root/var/lib/ci-fleet-tester/environments/expired-b.state"; do
  sed -i 's/^EXPIRES_AT=.*/EXPIRES_AT=1/' "$expired_state"
done
: >"$tmp/docker.log"
mv "$root/var/lib/ci-fleet-tester/environments/expired-a.compose.json" "$tmp/expired-a.compose.json"
if FAKE_TESTER_DOWN_FAIL=1 "$runtime" --cleanup >/dev/null 2>&1; then fail 'cleanup ignored a damaged protected environment'; fi
for id in preview-a expired-b; do grep -Fq "ci-fleet-test-$id" "$tmp/docker.log" || fail "damaged state stopped cleanup before $id"; done
mv "$tmp/expired-a.compose.json" "$root/var/lib/ci-fleet-tester/environments/expired-a.compose.json"
: >"$tmp/docker.log"
if FAKE_TESTER_DOWN_FAIL=1 "$runtime" --cleanup >/dev/null 2>&1; then fail 'cleanup ignored environment removal failures'; fi
for id in preview-a expired-a expired-b; do
  grep -Fq "ci-fleet-test-$id" "$tmp/docker.log" || fail "cleanup stopped before attempting $id"
done
"$runtime" --cleanup >/dev/null
[[ ! -e $state && ! -e $root/var/lib/ci-fleet-tester/environments/expired-a.state && ! -e $root/var/lib/ci-fleet-tester/environments/expired-b.state ]] || fail 'expired environment survived cleanup'
if FAKE_TESTER_ORPHAN_PROJECT=ci-fleet-test-orphan "$runtime" --cleanup >/dev/null 2>&1; then fail 'scheduled cleanup ignored an orphaned Compose container project'; fi
if FAKE_TESTER_ORPHAN_VOLUME_PROJECT=ci-fleet-test-orphan "$runtime" --cleanup >/dev/null 2>&1; then fail 'scheduled cleanup ignored an orphaned Compose volume project'; fi
if FAKE_TESTER_ORPHAN_NETWORK_PROJECT=ci-fleet-test-orphan "$runtime" --cleanup >/dev/null 2>&1; then fail 'scheduled cleanup ignored an orphaned Compose network project'; fi
cleanup_bin=$tmp/cleanup-bin
mkdir "$cleanup_bin"
for command in awk basename bash chmod cmp date df dirname docker du env find flock grep id install mkdir mktemp mv python3 readlink rm stat wc; do ln -s "$(command -v "$command")" "$cleanup_bin/$command"; done
PATH=$cleanup_bin "$runtime" --cleanup >/dev/null || fail 'cleanup required unrelated network-probe tools'

[[ ${CI_FLEET_TESTER_RUNTIME_ONLY:-0} != 1 ]] || { printf 'TESTER_RUNTIME_TESTS_OK\n'; exit 0; }

# Commit-backed installer tests run after the implementation commit exists.
ref=$(git -C "$repo_root" rev-parse HEAD)
# A release archive containing a symlinked or non-regular member must be rejected
# before chmod can dereference it and alter a host-side target's permissions.
symlink_repo=$tmp/symlink-repo
git clone --quiet --shared "$repo_root" "$symlink_repo"
cp "$repo_root/scripts/install-tester.sh" "$symlink_repo/scripts/install-tester.sh"
host_target=$tmp/host-target
printf '#!/usr/bin/env bash\nexit 0\n' >"$host_target"; chmod 0640 "$host_target"
rm -f "$symlink_repo/scripts/tester-runtime.sh"
ln -s "$host_target" "$symlink_repo/scripts/tester-runtime.sh"
git -C "$symlink_repo" add -A
git -C "$symlink_repo" -c user.name=Example -c user.email=example@invalid.example commit --quiet -m 'fixture: symlinked release member'
symlink_ref=$(git -C "$symlink_repo" rev-parse HEAD)
if "$symlink_repo/scripts/install-tester.sh" --install --config /etc/ci-fleet-tester/tester.env --ref "$symlink_ref" >/dev/null 2>&1; then fail 'install accepted a release archive with a symlinked member'; fi
[[ $(stat -c %a "$host_target") == 640 ]] || fail 'release validation dereferenced an archive symlink'
if DOCKER_HOST=tcp://example.invalid:2375 "$installer" --check --config /etc/ci-fleet-tester/tester.env >/dev/null 2>&1; then fail 'installer accepted a remote Docker selector'; fi
if FAKE_TESTER_EMPTY_ADDRESS_POOLS=1 "$installer" --install --config /etc/ci-fleet-tester/tester.env --ref "$ref" >/dev/null 2>&1; then fail 'install accepted absent explicit Docker address pools'; fi
"$installer" --install --config /etc/ci-fleet-tester/tester.env --ref "$ref" | grep -Fq INSTALL_OK || fail 'activation failed'
grep -Fq 'start --no-block ci-fleet-tester-health.service ci-fleet-tester-cleanup.service' "$tmp/systemctl.log" || fail 'activation did not queue lock-blocked maintenance services'
rm -rf "$root/run/lock/ci-fleet-tester"
"$root/opt/ci-fleet-tester/tester-runtime" --health >/dev/null || fail 'stable launcher did not recreate the volatile lock directory'
[[ $(readlink -f "$root/opt/ci-fleet-tester/current") == "$root/opt/ci-fleet-tester/releases/$ref" ]] || fail 'current release link is wrong'
[[ -x $root/opt/ci-fleet-tester/tester-runtime ]] || fail 'stable tester launcher was not installed'
for service in ci-fleet-tester-health.service ci-fleet-tester-cleanup.service; do grep -Fq 'ExecStart=/opt/ci-fleet-tester/tester-runtime' "$root/etc/systemd/system/$service" || fail "$service bypasses stable launcher"; done
: >"$tmp/systemctl.log"
"$installer" --install --config /etc/ci-fleet-tester/tester.env --ref "$ref" | grep -Fq 'changed=false' || fail 'unchanged install did not report no change'
if grep -Eq '^(enable|disable|start|daemon-reload)' "$tmp/systemctl.log"; then fail 'unchanged install restarted tester maintenance'; fi
check_output=$("$installer" --check --config /etc/ci-fleet-tester/tester.env)
grep -Fq CHECK_OK <<<"$check_output" || fail 'installed check failed'
if FAKE_TESTER_FRAGMENT_ROOT=$root/run/systemd/generator.early "$installer" --check >/dev/null 2>&1; then fail 'higher-priority systemd fragment passed check'; fi
if FAKE_TESTER_UNIT_DROPINS=$root/etc/systemd/system.control/override.conf "$installer" --check >/dev/null 2>&1; then fail 'effective systemd control drop-in passed check'; fi
: >"$tmp/systemctl.log"
if FAKE_TESTER_FRAGMENT_ROOT=$root/run/systemd/generator.early "$installer" --install --ref "$ref" >/dev/null 2>&1; then fail 'activation accepted an effective unit override'; fi
if grep -q '^start ' "$tmp/systemctl.log"; then fail 'activation started an unreviewed effective unit'; fi
"$installer" --install --ref "$ref" >/dev/null || fail 'unit policy did not recover after effective override removal'
stable_launcher=$root/opt/ci-fleet-tester/tester-runtime
chmod 0777 "$stable_launcher"
if "$installer" --check --config /etc/ci-fleet-tester/tester.env >/dev/null 2>&1; then fail 'writable privileged launcher passed check'; fi
chmod 0555 "$stable_launcher"
for unit in ci-fleet-tester-health.service ci-fleet-tester-health.timer ci-fleet-tester-cleanup.service ci-fleet-tester-cleanup.timer; do [[ -f $root/etc/systemd/system/$unit ]] || fail "unit missing: $unit"; done
for service in ci-fleet-tester-health.service ci-fleet-tester-cleanup.service; do
  grep -Fq 'TimeoutStartSec=300' "$root/etc/systemd/system/$service" || fail "$service does not bound oneshot start time"
done
release=$root/opt/ci-fleet-tester/releases/$ref
chmod 0755 "$release/scripts/tester-runtime.sh"
if "$installer" --check --config /etc/ci-fleet-tester/tester.env >/dev/null 2>&1; then fail 'writable release executable passed check'; fi
chmod 0555 "$release/scripts/tester-runtime.sh"
chmod 0644 "$release/.ci-fleet-release.sha256"
if "$installer" --check --config /etc/ci-fleet-tester/tester.env >/dev/null 2>&1; then fail 'writable release manifest passed check'; fi
chmod 0444 "$release/.ci-fleet-release.sha256"
# Reboot-creatable lock path: the tmpfiles.d drop-in ships in the release and
# recreates the volatile lock directory, so maintenance units survive a reboot.
[[ -f $release/host/systemd/ci-fleet-tester-lock.conf ]] || fail 'tmpfiles.d lock drop-in is missing from the release'
grep -Eq '^d /run/lock/ci-fleet-tester 0755' "$release/host/systemd/ci-fleet-tester-lock.conf" || fail 'tmpfiles.d drop-in does not recreate the tester lock directory'
rm -rf "$root/run/lock/ci-fleet-tester"
if "$release/scripts/tester-runtime.sh" --health >/dev/null 2>&1; then :; fi
[[ -d $root/run/lock/ci-fleet-tester && ! -L $root/run/lock/ci-fleet-tester && $(stat -c %a "$root/run/lock/ci-fleet-tester") == 755 ]] || fail 'stable launcher did not recreate the volatile lock directory'
chmod u+w "$release/scripts/tester-runtime.sh"; printf '# tamper\n' >>"$release/scripts/tester-runtime.sh"; chmod 0555 "$release/scripts/tester-runtime.sh"
if "$installer" --check --config /etc/ci-fleet-tester/tester.env >/dev/null 2>&1; then fail 'tampered installed release passed check'; fi
"$installer" --install --config /etc/ci-fleet-tester/tester.env --ref "$ref" >/dev/null || fail 'same-ref install did not repair an incomplete release'
"$installer" --check --config /etc/ci-fleet-tester/tester.env >/dev/null || fail 'repaired release failed check'
installed_unit=$root/etc/systemd/system/ci-fleet-tester-health.service
export FAKE_TESTER_LOADED_POLICY_DRIFT=$tmp/loaded-policy-drift
touch "$FAKE_TESTER_LOADED_POLICY_DRIFT"
if "$installer" --check >/dev/null 2>&1; then fail 'stale loaded systemd policy passed check'; fi
repair_output=$("$installer" --install --ref "$ref") || fail 'unchanged install did not repair stale loaded systemd policy'
[[ $repair_output == *INSTALL_OK* && $repair_output != *changed=false* && ! -e $FAKE_TESTER_LOADED_POLICY_DRIFT ]] || fail 'stale loaded systemd policy was accepted without a reload'
unset FAKE_TESTER_LOADED_POLICY_DRIFT
"$installer" --check >/dev/null || fail 'reloaded tester policy failed check'
for directory in etc/systemd/system run/systemd/system usr/lib/systemd/system lib/systemd/system; do
  dropin=$root/$directory/ci-fleet-tester-health.service.d
  mkdir -p "$dropin"
  printf '[Service]\nExecStart=\nExecStart=/bin/false\n' >"$dropin/override.conf"
  if "$installer" --check >/dev/null 2>&1; then fail 'unmanaged systemd drop-in passed check'; fi
  if "$installer" --install --ref "$ref" >/dev/null 2>&1; then fail 'unmanaged systemd drop-in passed install'; fi
  rm -r "$dropin"
done
ln -s /missing "$root/var/lib/ci-fleet-deployer"
if "$installer" --check >/dev/null 2>&1; then fail 'tester accepted a dangling deployer role marker'; fi
rm "$root/var/lib/ci-fleet-deployer"
for marker in /opt/ci-fleet/current /opt/ci-fleet-deployer/current /opt/ci-fleet-status/current /var/lib/ci-fleet-status-installer; do
  mkdir -p "$(dirname "$root$marker")"
  for kind in directory symlink; do
    if [[ $kind == directory ]]; then mkdir "$root$marker"; else ln -s /missing "$root$marker"; fi
    for mode in check install; do
      ref_args=()
      [[ $mode != install ]] || ref_args=(--ref "$ref")
      if "$installer" "--$mode" "${ref_args[@]}" >"$tmp/role-rejection.out" 2>&1; then fail "tester installer accepted another role: $marker $kind"; fi
      grep -Fq 'another ci-fleet host role is present' "$tmp/role-rejection.out" || fail 'role marker failed outside admission check'
    done
    if "$runtime" --health >"$tmp/role-rejection.out" 2>&1; then fail "tester runtime accepted another role: $marker $kind"; fi
    grep -Fq 'another ci-fleet host role is present' "$tmp/role-rejection.out" || fail 'runtime marker failed outside admission check'
    if [[ $kind == directory ]]; then rmdir "$root$marker"; else rm "$root$marker"; fi
  done
  [[ $marker != */current ]] || rmdir "$(dirname "$root$marker")"
done
for inventory_kind in list-unit-files list-units; do
  for unit in ci-fleet-deployer.service ci-fleet-status-receiver.service ci-fleet-health.timer actions.runner.example.service; do
    for mode in check install; do
      ref_args=(); [[ $mode != install ]] || ref_args=(--ref "$ref")
      if FAKE_TESTER_FOREIGN_UNIT=$unit FAKE_TESTER_FOREIGN_UNIT_KIND=$inventory_kind "$installer" "--$mode" "${ref_args[@]}" >"$tmp/unit-rejection.out" 2>&1; then fail "installer accepted effective foreign unit: $unit"; fi
      grep -Fq 'another ci-fleet host role is present' "$tmp/unit-rejection.out" || fail 'foreign unit failed outside admission check'
    done
    if FAKE_TESTER_FOREIGN_UNIT=$unit FAKE_TESTER_FOREIGN_UNIT_KIND=$inventory_kind "$runtime" --health >"$tmp/unit-rejection.out" 2>&1; then fail "runtime accepted effective foreign unit: $unit"; fi
    grep -Fq 'another ci-fleet host role is present' "$tmp/unit-rejection.out" || fail 'runtime unit failed outside admission check'
  done
done
FAKE_TESTER_EMPTY_UNIT_INVENTORY=1 "$installer" --check >/dev/null || fail 'empty foreign unit inventory blocked installer check'
for inventory_error in 1 7; do
  if FAKE_TESTER_UNIT_INVENTORY_FAIL=1 FAKE_TESTER_UNIT_INVENTORY_FAILURE_CODE=$inventory_error "$installer" --check >/dev/null 2>&1; then fail 'installer accepted unavailable foreign unit inventory'; fi
  if FAKE_TESTER_UNIT_INVENTORY_FAIL=1 FAKE_TESTER_UNIT_INVENTORY_FAILURE_CODE=$inventory_error "$runtime" --health >/dev/null 2>&1; then fail 'runtime accepted unavailable foreign unit inventory'; fi
done
printf '# drift\n' >>"$installed_unit"
if "$installer" --check --config /etc/ci-fleet-tester/tester.env >/dev/null 2>&1; then fail 'installed unit drift passed check'; fi
cp "$release/host/systemd/ci-fleet-tester-health.service" "$installed_unit"
installed_tmpfiles=$root/usr/lib/tmpfiles.d/ci-fleet-tester-lock.conf
rm "$installed_tmpfiles"
if "$installer" --check --config /etc/ci-fleet-tester/tester.env >/dev/null 2>&1; then fail 'missing installed tmpfiles rule passed check'; fi
cp "$release/host/systemd/ci-fleet-tester-lock.conf" "$installed_tmpfiles"
for override_dir in etc/tmpfiles.d run/tmpfiles.d usr/local/lib/tmpfiles.d; do
  mkdir -p "$root/$override_dir"
  override=$root/$override_dir/ci-fleet-tester-lock.conf
  for override_kind in file symlink; do
    if [[ $override_kind == file ]]; then printf 'd /run/lock/ci-fleet-tester 0700 root root -\n' >"$override"; else ln -s /dev/null "$override"; fi
    for mode in --install --check; do
      ref_args=(); [[ $mode != --install ]] || ref_args=(--ref "$ref")
      if "$installer" "$mode" "${ref_args[@]}" >"$tmp/tmpfiles-override.out" 2>&1; then fail 'installer accepted a boot-effective tmpfiles override'; fi
      grep -Fq 'unmanaged tester tmpfiles overrides are forbidden' "$tmp/tmpfiles-override.out" || fail 'tmpfiles override failed outside policy validation'
    done
    rm "$override"
  done
done
unsafe_policy_accepted=0
for policy_file in "$installed_unit" "$installed_tmpfiles"; do
  chmod 0666 "$policy_file"
  if "$installer" --check --config /etc/ci-fleet-tester/tester.env >/dev/null 2>&1; then unsafe_policy_accepted=1; fi
  chmod 0644 "$policy_file"
done
((unsafe_policy_accepted == 0)) || fail 'writable installed systemd policy passed check'
runtime_state=$root/var/lib/ci-fleet-tester/environments
rmdir "$runtime_state"
if "$installer" --check --config /etc/ci-fleet-tester/tester.env >/dev/null 2>&1; then fail 'missing runtime state directory passed check'; fi
[[ ! -e $runtime_state ]] || fail 'check recreated the missing runtime state directory'
install -d -m 0700 "$runtime_state"

# A syntactically valid candidate that fails its post-switch check restores the incumbent.
upgrade_repo=$tmp/upgrade-repo
git clone --quiet --shared "$repo_root" "$upgrade_repo"
cp "$repo_root/scripts/install-tester.sh" "$upgrade_repo/scripts/install-tester.sh"
printf '#!/usr/bin/env bash\nexit 1\n' >"$upgrade_repo/scripts/tester-runtime.sh"; chmod 0755 "$upgrade_repo/scripts/tester-runtime.sh"
git -C "$upgrade_repo" add scripts/tester-runtime.sh
git -C "$upgrade_repo" -c user.name=Example -c user.email=example@invalid.example commit --quiet -m 'fixture: fail tester activation'
bad_ref=$(git -C "$upgrade_repo" rev-parse HEAD)
if "$upgrade_repo/scripts/install-tester.sh" --upgrade --config /etc/ci-fleet-tester/tester.env --ref "$bad_ref" >/dev/null 2>&1; then fail 'failed candidate activation succeeded'; fi
[[ $(readlink -f "$root/opt/ci-fleet-tester/current") == "$root/opt/ci-fleet-tester/releases/$ref" ]] || fail 'failed upgrade did not restore incumbent release'

# Post-switch checks must use the installed launcher rather than bypassing it.
launcher_repo=$tmp/launcher-repo
git clone --quiet --shared "$repo_root" "$launcher_repo"
printf '#!/usr/bin/env bash\nexit 1\n' >"$launcher_repo/scripts/tester-launcher.sh"; chmod 0755 "$launcher_repo/scripts/tester-launcher.sh"
git -C "$launcher_repo" add scripts/tester-launcher.sh
git -C "$launcher_repo" -c user.name=Example -c user.email=example@invalid.example commit --quiet -m 'fixture: fail tester launcher'
launcher_fail_ref=$(git -C "$launcher_repo" rev-parse HEAD)
if "$launcher_repo/scripts/install-tester.sh" --upgrade --config /etc/ci-fleet-tester/tester.env --ref "$launcher_fail_ref" >/dev/null 2>&1; then fail 'candidate with a broken deployed launcher was accepted'; fi
[[ $(readlink -f "$root/opt/ci-fleet-tester/current") == "$root/opt/ci-fleet-tester/releases/$ref" ]] || fail 'launcher failure did not restore incumbent release'
git -C "$upgrade_repo" checkout --quiet "$ref"
git -C "$upgrade_repo" replace "$ref" "$bad_ref"
if "$upgrade_repo/scripts/install-tester.sh" --install --config /etc/ci-fleet-tester/tester.env --ref "$ref" >/dev/null 2>&1; then fail 'Git replacement metadata was accepted'; fi
git -C "$upgrade_repo" replace -d "$ref" >/dev/null
git -C "$upgrade_repo" checkout --quiet "$bad_ref"

# Unit activation failures restore the incumbent symlink and units.
git -C "$upgrade_repo" show "$ref:scripts/tester-runtime.sh" >"$upgrade_repo/scripts/tester-runtime.sh"; chmod 0755 "$upgrade_repo/scripts/tester-runtime.sh"
git -C "$upgrade_repo" add scripts/tester-runtime.sh
git -C "$upgrade_repo" -c user.name=Example -c user.email=example@invalid.example commit --quiet -m 'fixture: valid tester candidate'
unit_fail_ref=$(git -C "$upgrade_repo" rev-parse HEAD)
if FAKE_TESTER_TMPFILES_FAIL=1 "$upgrade_repo/scripts/install-tester.sh" --upgrade --config /etc/ci-fleet-tester/tester.env --ref "$unit_fail_ref" >/dev/null 2>&1; then fail 'tmpfiles creation failure succeeded'; fi
[[ $(readlink -f "$root/opt/ci-fleet-tester/current") == "$root/opt/ci-fleet-tester/releases/$ref" ]] || fail 'tmpfiles creation failure did not restore incumbent release'
unit_hash_before=$(sha256sum "$root/etc/systemd/system/ci-fleet-tester-health.service")
if FAKE_TESTER_SYSTEMCTL_FAIL=daemon-reload "$upgrade_repo/scripts/install-tester.sh" --upgrade --config /etc/ci-fleet-tester/tester.env --ref "$unit_fail_ref" >"$tmp/unit-restore.log" 2>&1; then fail 'unit activation failure succeeded'; fi
grep -Fq 'incumbent unit restore failed' "$tmp/unit-restore.log" || fail 'secondary incumbent restore failure was not surfaced'
[[ $(readlink -f "$root/opt/ci-fleet-tester/current") == "$root/opt/ci-fleet-tester/releases/$ref" && $(sha256sum "$root/etc/systemd/system/ci-fleet-tester-health.service") == "$unit_hash_before" ]] || fail 'unit activation failure did not restore incumbent release and units'
if FAKE_TESTER_SYSTEMCTL_FAIL='start --no-block ci-fleet-tester-health.service ci-fleet-tester-cleanup.service' "$upgrade_repo/scripts/install-tester.sh" --upgrade --config /etc/ci-fleet-tester/tester.env --ref "$unit_fail_ref" >/dev/null 2>&1; then fail 'candidate activation did not exercise maintenance services'; fi
[[ $(readlink -f "$root/opt/ci-fleet-tester/current") == "$root/opt/ci-fleet-tester/releases/$ref" ]] || fail 'maintenance service failure did not restore incumbent release'
if FAKE_TESTER_SYSTEMCTL_FAIL_WAIT=1 "$upgrade_repo/scripts/install-tester.sh" --upgrade --config /etc/ci-fleet-tester/tester.env --ref "$unit_fail_ref" >/dev/null 2>&1; then fail 'candidate activation did not wait for maintenance services'; fi
[[ $(readlink -f "$root/opt/ci-fleet-tester/current") == "$root/opt/ci-fleet-tester/releases/$ref" ]] || fail 'asynchronous maintenance failure did not restore incumbent release'

# A corrupt incumbent is never recorded as the rollback target.
chmod u+w "$release/scripts/tester-runtime.sh"; printf '# corrupt incumbent\n' >>"$release/scripts/tester-runtime.sh"; chmod 0555 "$release/scripts/tester-runtime.sh"
printf '# candidate\n' >>"$upgrade_repo/docs/TESTER-HOST.md"
git -C "$upgrade_repo" add docs/TESTER-HOST.md
git -C "$upgrade_repo" -c user.name=Example -c user.email=example@invalid.example commit --quiet -m 'fixture: second valid tester candidate'
valid_ref=$(git -C "$upgrade_repo" rev-parse HEAD)
: >"$tmp/systemctl.log"
if FAKE_TESTER_SYSTEMCTL_FAIL='disable --now' "$upgrade_repo/scripts/install-tester.sh" --upgrade --config /etc/ci-fleet-tester/tester.env --ref "$valid_ref" >/dev/null 2>&1; then fail 'upgrade ignored timer quiescence failure'; fi
grep -Fq 'enable --now ci-fleet-tester-health.timer ci-fleet-tester-cleanup.timer' "$tmp/systemctl.log" || fail 'quiescence failure did not restore incumbent timers'
: >"$tmp/events.log"
"$upgrade_repo/scripts/install-tester.sh" --upgrade --config /etc/ci-fleet-tester/tester.env --ref "$valid_ref" >/dev/null || fail 'valid upgrade failed'
[[ ! -e $root/var/lib/ci-fleet-tester/last-known-good ]] || fail 'corrupt incumbent was recorded as last-known-good'
disable_line=$(grep -n '^systemctl disable --now ' "$tmp/events.log" | tail -1 | cut -d: -f1)
check_line=$(grep -n '^docker info ' "$tmp/events.log" | tail -1 | cut -d: -f1)
enable_line=$(grep -n '^systemctl enable --now ' "$tmp/events.log" | tail -1 | cut -d: -f1)
[[ -n $disable_line && -n $check_line && -n $enable_line && $disable_line -lt $check_line && $check_line -lt $enable_line ]] || fail 'timers were not quiesced until candidate validation completed'

# Maintenance probes release the runtime lock but retain lifecycle serialization.
probe_ready=$tmp/probe-ready
probe_release=$tmp/probe-release
# Force the repair path so this test reaches the maintenance probes.
printf '# fixture drift\n' >>"$root/etc/systemd/system/ci-fleet-tester-health.service"
FAKE_TESTER_SYSTEMCTL_WAIT_READY=$probe_ready FAKE_TESTER_SYSTEMCTL_WAIT_RELEASE=$probe_release \
  "$upgrade_repo/scripts/install-tester.sh" --upgrade --config /etc/ci-fleet-tester/tester.env --ref "$valid_ref" >/dev/null & probe_pid=$!
while [[ ! -e $probe_ready ]]; do kill -0 "$probe_pid" 2>/dev/null || fail 'maintenance probe fixture exited early'; done
set +e
timeout 2 "$installer" --check --config /etc/ci-fleet-tester/tester.env >/dev/null 2>&1
probe_lock_rc=$?
set -e
touch "$probe_release"
wait "$probe_pid"
[[ $probe_lock_rc == 124 ]] || fail 'maintenance probe allowed another lifecycle action'

# Rollback switches only to a complete recorded release and keeps environments intact.
old=0000000000000000000000000000000000000000
cp -a "$root/opt/ci-fleet-tester/releases/$ref" "$root/opt/ci-fleet-tester/releases/$old"
chmod 0755 "$root/opt/ci-fleet-tester/releases/$old"
chmod 0644 "$root/opt/ci-fleet-tester/releases/$old/.ci-fleet-source-revision" "$root/opt/ci-fleet-tester/releases/$old/.ci-fleet-release.sha256"
printf '%s\n' "$old" >"$root/opt/ci-fleet-tester/releases/$old/.ci-fleet-source-revision"
(cd "$root/opt/ci-fleet-tester/releases/$old" && sha256sum scripts/tester-runtime.sh scripts/tester-launcher.sh .ci-fleet-source-revision host/systemd/* >.ci-fleet-release.sha256)
chmod 0444 "$root/opt/ci-fleet-tester/releases/$old/.ci-fleet-source-revision" "$root/opt/ci-fleet-tester/releases/$old/.ci-fleet-release.sha256"
chmod 0555 "$root/opt/ci-fleet-tester/releases/$old"
printf '%s\n' "$old" >"$root/var/lib/ci-fleet-tester/last-known-good"; chmod 600 "$root/var/lib/ci-fleet-tester/last-known-good"
write_environment rollback-env 18086
FAKE_TESTER_ROUTE_PORT=18086 "$runtime" --converge --environment rollback-env >/dev/null
rollback_state_hash=$(sha256sum "$root/var/lib/ci-fleet-tester/environments/rollback-env.state")
pre_rollback_release=$(readlink -f "$root/opt/ci-fleet-tester/current")
chmod u+w "$pre_rollback_release/scripts/tester-runtime.sh"
printf '# corrupt before rollback\n' >>"$pre_rollback_release/scripts/tester-runtime.sh"
chmod 0555 "$pre_rollback_release/scripts/tester-runtime.sh"
FAKE_TESTER_ROUTE_PORT=18086 "$installer" --rollback --config /etc/ci-fleet-tester/tester.env | grep -Fq ROLLBACK_OK || fail 'rollback failed'
[[ $(readlink -f "$root/opt/ci-fleet-tester/current") == "$root/opt/ci-fleet-tester/releases/$old" ]] || fail 'rollback selected the wrong release'
[[ $(<"$root/var/lib/ci-fleet-tester/last-known-good") == "$old" ]] || fail 'rollback recorded a corrupt prior release'
[[ $(sha256sum "$root/var/lib/ci-fleet-tester/environments/rollback-env.state") == "$rollback_state_hash" ]] || fail 'rollback changed active environment state'
FAKE_TESTER_ROUTE_PORT=18086 "$runtime" --remove --environment rollback-env >/dev/null

lock_file=$root/run/lock/ci-fleet-tester/runtime.lock
lock_ready=$tmp/lock-ready
flock "$lock_file" -c "touch '$lock_ready'; sleep 1" & lock_pid=$!
while [[ ! -e $lock_ready ]]; do kill -0 "$lock_pid" 2>/dev/null || fail 'could not acquire fixture lifecycle lock'; done
set +e
timeout 0.2 "$installer" --install --config /etc/ci-fleet-tester/tester.env --ref "$ref" >/dev/null 2>&1
lock_rc=$?
set -e
[[ $lock_rc == 124 ]] || fail 'installer lifecycle mutation ignored the shared lock'
set +e
timeout 0.2 "$installer" --reset --environment absent >/dev/null 2>&1
reset_lock_rc=$?
set -e
[[ $reset_lock_rc == 124 ]] || fail 'reset resolved the active release before acquiring the lifecycle lock'
set +e
timeout 0.2 "$root/opt/ci-fleet-tester/tester-runtime" --inspect --environment absent >/dev/null 2>&1
launcher_lock_rc=$?
set -e
[[ $launcher_lock_rc == 124 ]] || fail 'stable launcher resolved the active release before acquiring the lifecycle lock'
wait "$lock_pid"
: >"$tmp/docker.log"
if DOCKER_HOST=tcp://example.invalid:2375 "$installer" --uninstall --config /etc/ci-fleet-tester/tester.env >/dev/null 2>&1; then fail 'uninstall accepted a remote Docker selector'; fi
[[ -L $root/opt/ci-fleet-tester/current ]] || fail 'remote-selector uninstall removed the active release'
[[ ! -s $tmp/docker.log ]] || fail 'remote-selector uninstall contacted Docker'
if DOCKER_CONTEXT=remote "$installer" --uninstall --config /etc/ci-fleet-tester/tester.env >/dev/null 2>&1; then fail 'uninstall accepted a remote Docker context'; fi
if FAKE_TESTER_ORPHAN_PROJECT=ci-fleet-test-missing-state "$installer" --uninstall --config /etc/ci-fleet-tester/tester.env >/dev/null 2>&1; then fail 'uninstall ignored a managed Compose project with missing state'; fi
[[ -L $root/opt/ci-fleet-tester/current ]] || fail 'orphan-project uninstall removed the active release'
if FAKE_TESTER_ORPHAN_VOLUME_PROJECT=ci-fleet-test-missing-state "$installer" --uninstall --config /etc/ci-fleet-tester/tester.env >/dev/null 2>&1; then fail 'uninstall ignored a managed Compose volume with missing state'; fi
if FAKE_TESTER_ORPHAN_NETWORK_PROJECT=ci-fleet-test-missing-state "$installer" --uninstall --config /etc/ci-fleet-tester/tester.env >/dev/null 2>&1; then fail 'uninstall ignored a managed Compose network with missing state'; fi
if FAKE_TESTER_SYSTEMCTL_FAIL='disable --now' "$installer" --uninstall --config /etc/ci-fleet-tester/tester.env >/dev/null 2>&1; then fail 'uninstall ignored systemd teardown failure'; fi
[[ -L $root/opt/ci-fleet-tester/current ]] || fail 'failed uninstall removed the active release'
if FAKE_TESTER_SYSTEMCTL_FAIL=stop "$installer" --uninstall >/dev/null 2>&1; then fail 'uninstall ignored maintenance service stop failure'; fi
[[ -L $root/opt/ci-fleet-tester/current && -f $root/etc/systemd/system/ci-fleet-tester-health.service ]] || fail 'failed service stop removed the active release or unit files'
queued_services=$tmp/queued-services
touch "$queued_services"
export FAKE_TESTER_QUEUED_SERVICES=$queued_services
"$installer" --uninstall --config /etc/ci-fleet-tester/tester.env | grep -Fq UNINSTALL_OK || fail 'uninstall failed'
[[ ! -e $queued_services ]] || fail 'uninstall left maintenance services queued'
unset FAKE_TESTER_QUEUED_SERVICES
[[ -f $root/etc/ci-fleet-tester/tester.env && ! -L $root/opt/ci-fleet-tester/current ]] || fail 'uninstall did not preserve config/remove runtime'
[[ ! -e $root/usr/lib/tmpfiles.d/ci-fleet-tester-lock.conf ]] || fail 'uninstall left the tester tmpfiles rule installed'
FAKE_TESTER_SYSTEMCTL_FAIL_IF_UNITS_MISSING=1 "$installer" --uninstall --config /etc/ci-fleet-tester/tester.env | grep -Fq UNINSTALL_OK || fail 'repeated uninstall failed on absent units'
git -C "$upgrade_repo" checkout --quiet "$bad_ref"
if FAKE_TESTER_SYSTEMCTL_FAIL='disable --now' "$upgrade_repo/scripts/install-tester.sh" --install --config /etc/ci-fleet-tester/tester.env --ref "$bad_ref" >/dev/null 2>&1; then fail 'invalid fresh install succeeded'; fi
[[ -L $root/opt/ci-fleet-tester/current ]] || fail 'failed fresh-install teardown removed the recovery link'
"$upgrade_repo/scripts/install-tester.sh" --uninstall --config /etc/ci-fleet-tester/tester.env >/dev/null
# Partial unit set (interrupted install) must still uninstall cleanly rather
# than failing because a timer unit file is missing.
"$installer" --install --config /etc/ci-fleet-tester/tester.env --ref "$ref" >/dev/null || fail 'fresh install failed before partial-unit test'
rm -f "$root/etc/systemd/system/ci-fleet-tester-cleanup.timer"
rm -f "$root/etc/systemd/system/ci-fleet-tester-cleanup.service"
: >"$tmp/systemctl.log"
FAKE_TESTER_EFFECTIVE_TIMERS=1 FAKE_TESTER_EFFECTIVE_SERVICES=1 PATH=$uninstall_bin "$standalone_installer" --uninstall --config /etc/ci-fleet-tester/tester.env | grep -Fq UNINSTALL_OK || fail 'standalone uninstall required installation-only tools or a Git checkout'
grep -Fxq 'disable --now ci-fleet-tester-health.timer ci-fleet-tester-cleanup.timer' "$tmp/systemctl.log" || fail 'uninstall skipped an effective timer without a local fragment'
grep -Fxq 'stop ci-fleet-tester-health.service ci-fleet-tester-cleanup.service' "$tmp/systemctl.log" || fail 'uninstall skipped an effective service without a local fragment'
[[ -L $root/opt/ci-fleet-tester/current ]] && fail 'partial uninstall left the active release link'
printf 'TESTER_INSTALLER_TESTS_OK\n'
