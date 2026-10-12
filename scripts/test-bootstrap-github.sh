#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
bootstrap=$repo_root/scripts/bootstrap-github.sh
callback=$repo_root/scripts/bootstrap-github-callback.py
real_curl=$(command -v curl)
tmp=$(mktemp -d)
approval_root=
processes=()
cleanup() {
  local process
  for process in "${processes[@]}"; do kill "$process" >/dev/null 2>&1 || true; done
  [[ ${KEEP_TEST_TMP:-0} == 1 ]] || rm -rf "$tmp"
  [[ -z $approval_root ]] || rm -rf "$approval_root"
}
trap cleanup EXIT
fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
callback_socket=
callback_curl() { "$real_curl" --unix-socket "$callback_socket" "$@"; }
port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'; }
wait_http() {
  local url=$1 output=$2 transcript=${3:-}
  for _ in {1..100}; do
    if [[ -n $transcript ]]; then callback_socket=$(awk '$1 == "CALLBACK_SOCKET" {print $2; exit}' "$transcript"); fi
    if [[ -n $callback_socket ]] && callback_curl -fsS "$url" -o "$output" 2>/dev/null; then return; fi
    sleep 0.05
  done
  fail "callback did not become ready: $url"
}

# The callback rejects mismatched state, never logs, and writes the code once with mode 0600.
callback_port=$(port)
callback_socket=$tmp/callback.sock
state_file=$tmp/callback-state
printf fixture-state >"$state_file"
chmod 600 "$state_file"
code_file=$tmp/callback-code
handoff_file=$tmp/callback-handoff
python3 "$callback" --parent-pid "$$" --socket "$callback_socket" --operator-uid "$(id -u)" --port "$callback_port" --organization example-org --state-file "$state_file" \
  --manifest '{"name":"fixture"}' --output "$code_file" --handoff "$handoff_file" --timeout 30 >"$tmp/callback.out" 2>"$tmp/callback.err" &
processes+=("$!")
wait_http "http://127.0.0.1:$callback_port/" "$tmp/callback.html"
[[ $(stat -c %a "$callback_socket") == 600 && $(stat -c %u "$callback_socket") == "$(id -u)" ]] || fail 'callback socket is not operator-private'
if tr '\0' '\n' <"/proc/${processes[-1]}/cmdline" | grep -Fxq fixture-state; then fail 'callback nonce appeared in process arguments'; fi
if [[ $(id -u) == 0 ]] && command -v runuser >/dev/null; then
  if runuser -u nobody -- "$real_curl" --unix-socket "$callback_socket" -fsS "http://127.0.0.1:$callback_port/" -o /dev/null 2>/dev/null; then fail 'another local user could access the callback'; fi
fi
status=$(callback_curl -sS -o "$tmp/wrong.html" -w '%{http_code}' "http://127.0.0.1:$callback_port/callback?code=fixture-code&state=wrong")
[[ $status == 403 && ! -e $code_file ]] || fail 'callback accepted the wrong state'
callback_curl -fsS "http://127.0.0.1:$callback_port/callback?code=fixture-code&state=fixture-state" -o "$tmp/correct.html"
[[ $(<"$code_file") == fixture-code && $(stat -c %a "$code_file") == 600 ]] || fail 'callback did not protect the conversion code'
rm "$code_file"
status=$(callback_curl -sS -o "$tmp/replayed.html" -w '%{http_code}' "http://127.0.0.1:$callback_port/callback?code=fixture-replay&state=fixture-state")
[[ $status == 409 && ! -e $code_file ]] || fail 'callback state was reusable after code cleanup'
printf 'https://github.com/apps/example-fixture/installations/new\n' >"$handoff_file"
callback_curl -fsS "http://127.0.0.1:$callback_port/next" -o "$tmp/next.html"
wait "${processes[-1]}"
[[ ! -s $tmp/callback.out && ! -s $tmp/callback.err ]] || fail 'callback logged request material'
grep -Fq 'https://github.com/apps/example-fixture/installations/new' "$tmp/next.html" || fail 'callback did not provide the installation approval link'
orphan_port=$(port)
if python3 "$callback" --parent-pid 1 --socket "$tmp/orphan.sock" --operator-uid "$(id -u)" --port "$orphan_port" --organization example-org --state-file "$state_file" --manifest '{"name":"fixture"}' --output "$tmp/orphan-code" --handoff "$tmp/orphan-handoff" --timeout 30 >/dev/null 2>&1; then fail 'callback accepted a mismatched bootstrap parent'; fi
[[ ! -e $tmp/orphan.sock ]] || fail 'orphaned callback opened a listener'

# Dry-run validates a complete fictional request and performs no write or API call.
dry_root=$tmp/dry-root
CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$dry_root "$bootstrap" --dry-run --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >"$tmp/dry.out"
grep -Fq 'NO_GITHUB_MUTATION' "$tmp/dry.out" || fail 'dry-run omitted its no-mutation result'
[[ ! -e $dry_root ]] || fail 'dry-run wrote host state'
conflicting_root=$tmp/conflicting-mode-root
if CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$conflicting_root "$bootstrap" --dry-run --check --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'conflicting dry-run/check modes were accepted'; fi
[[ ! -e $conflicting_root ]] || fail 'conflicting mode rejection wrote local state'
duplicate_root=$tmp/duplicate-root
for second in example-org/second-repo=101 example-org/example-repo=102; do
  if CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$duplicate_root "$bootstrap" --organization example-org --instance example-ci-01 \
    --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 --allow-repository "$second" >/dev/null 2>&1; then fail 'non-bijective repository mapping was accepted'; fi
  [[ ! -e $duplicate_root ]] || fail 'duplicate repository mapping wrote host state'
done
if "$bootstrap" --dry-run --organization example-org --instance example-ci-01 --runner-group Default --allow-repository example-org/example-repo >/dev/null 2>&1; then
  fail 'default runner group was accepted'
fi
if "$bootstrap" --dry-run --organization example-org --instance example.ci --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then
  fail 'instance outside the installer contract was accepted'
fi
if "$bootstrap" --dry-run --organization Example-org --instance example-ci-01 --config-repository Example-org/config --runner-group example-ci-experimental --allow-repository Example-org/example-repo=101 >/dev/null 2>&1; then fail 'mixed-case organization was accepted'; fi
if "$bootstrap" --dry-run --organization example-org --instance example-ci-01 --config-repository example-org/config --runner-group example_group --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'runner group outside schema-v3 slug contract was accepted'; fi
if "$bootstrap" --dry-run --organization example-organization --instance example-instance --config-repository example-organization/config --runner-group example-group --allow-repository example-organization/repo=101 >/dev/null 2>&1; then fail 'overlong generated App name was accepted'; fi
if "$bootstrap" --dry-run --organization example-org --instance example-ci-01 --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 --bind ::1 --callback-host ::1 >/dev/null 2>&1; then fail 'unsupported IPv6 callback was accepted'; fi
install_mismatch_root=$tmp/install-mismatch-root
if CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$install_mismatch_root "$bootstrap" --organization example-org --instance example-ci-01 --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 --install --config-repo example-org/other --config-ref 1111111111111111111111111111111111111111 >/dev/null 2>&1; then fail 'mismatched installer configuration repository was accepted'; fi
[[ ! -e $install_mismatch_root ]] || fail 'mismatched installer repository wrote local state before rejection'
config_checkout=$tmp/config-checkout
mkdir "$config_checkout"
cp -a "$repo_root/templates/config-repository/." "$config_checkout/"
fixture_engine_ref=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["controllers"]["example-ci-01"]["engine_ref"])' "$config_checkout/fleet.json")
python3 "$repo_root/templates/config-repository/scripts/init.py" --organization acme-org --project example-repo --repository acme-org/example-repo --runner-group example-ci-experimental --controller example-ci-01 --location example-site --engine-ref "$fixture_engine_ref" --output "$config_checkout/fleet.json" --force
if python3 "$config_checkout/scripts/validate.py" --config "$config_checkout/fleet.json" --strict >/dev/null 2>&1; then fail 'unreviewed initializer network placeholder passed strict validation'; fi
python3 - "$config_checkout/fleet.json" <<'PY'
import json,sys
from pathlib import Path
path=Path(sys.argv[1]); value=json.loads(path.read_text())
value['controllers']['example-ci-01']['docker_network_policy']['default_address_pools'][0]['base']='10.64.0.0/24'
path.write_text(json.dumps(value,indent=2)+'\n')
PY
git init -q "$config_checkout"
git -C "$config_checkout" add -A
git -C "$config_checkout" -c user.name=fixture -c user.email=fixture@example.invalid commit -qm fixture
config_ref=$(git -C "$config_checkout" rev-parse HEAD)
routing_mismatch_root=$tmp/routing-mismatch-root
if CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$routing_mismatch_root "$bootstrap" --organization acme-org --instance example-ci-01 --config-repository acme-org/config --runner-group other-group --allow-repository acme-org/example-repo=101 --install --config-repo "$config_checkout" --config-ref "$config_ref" >/dev/null 2>&1; then fail 'desired-state routing mismatch was accepted'; fi
[[ ! -e $routing_mismatch_root ]] || fail 'desired-state routing mismatch wrote local state before rejection'
routing_root=$tmp/routing-root
routing_port=$(port)
operator_flags=()
if [[ $(id -u) == 0 ]] && command -v runuser >/dev/null; then operator_flags=(--operator-uid "$(id -u nobody)"); fi
CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$routing_root "$bootstrap" --organization acme-org --instance example-ci-01 --config-repository acme-org/config --runner-group example-ci-experimental --allow-repository acme-org/example-repo=101 --install --config-repo "$config_checkout" --config-ref "$config_ref" --port "$routing_port" --timeout 30 "${operator_flags[@]}" >"$tmp/routing.out" 2>"$tmp/routing.err" &
routing_pid=$!
processes+=("$routing_pid")
wait_http "http://127.0.0.1:$routing_port/" "$tmp/routing.html" "$tmp/routing.out"
if ((${#operator_flags[@]})); then
  [[ $(stat -c %u "$callback_socket") == "$(id -u nobody)" && $(stat -c %a "${callback_socket%/*}") == 700 ]] || fail 'explicit operator socket ownership is incorrect'
  runuser -u nobody -- "$real_curl" --unix-socket "$callback_socket" -fsS "http://127.0.0.1:$routing_port/" -o /dev/null || fail 'authorized operator could not access the callback'
  if runuser -u daemon -- "$real_curl" --unix-socket "$callback_socket" -fsS "http://127.0.0.1:$routing_port/" -o /dev/null 2>/dev/null; then fail 'unrelated local user could access the operator callback'; fi
fi
second_routing_port=$(port)
if CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$routing_root "$bootstrap" --organization acme-org --instance example-ci-01 --config-repository acme-org/config --runner-group example-ci-experimental --allow-repository acme-org/example-repo=101 --install --config-repo "$config_checkout" --config-ref "$config_ref" --port "$second_routing_port" --timeout 30 >"$tmp/second-routing.out" 2>&1; then fail 'concurrent first-time bootstrap was accepted'; fi
if grep -Fq CALLBACK_SOCKET "$tmp/second-routing.out"; then fail 'concurrent bootstrap reached callback registration'; fi
kill -TERM "$routing_pid"
if wait "$routing_pid"; then fail 'cancelled matching desired-state bootstrap succeeded'; fi
for _ in {1..100}; do
  if ! callback_curl -fsS "http://127.0.0.1:$routing_port/" -o /dev/null 2>/dev/null; then break; fi
  sleep 0.05
done
if callback_curl -fsS "http://127.0.0.1:$routing_port/" -o /dev/null 2>/dev/null; then fail 'callback outlived its bootstrap parent'; fi
if "$bootstrap" --dry-run --organization example-org --instance example-ci-01 --runner-group example-ci-experimental \
  --allow-repository example-org/example-repo --bind 10.0.0.1 --callback-host 10.0.0.1 >/dev/null 2>&1; then
  fail 'plaintext non-loopback callback was accepted'
fi
if "$bootstrap" --dry-run --organization example-org --instance example-ci-01 --runner-group example-ci-experimental \
  --allow-repository example-org/example-repo --bind 0.0.0.0 --callback-host 0.0.0.0 >/dev/null 2>&1; then
  fail 'unspecified callback bind was accepted'
fi
unsafe_root=$tmp/unsafe-root
mkdir -p "$unsafe_root/etc" "$tmp/redirected"
ln -s "$tmp/redirected" "$unsafe_root/etc/ci-fleet"
if CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$unsafe_root "$bootstrap" --check --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then
  fail 'symlinked protected directory was accepted'
fi
unsafe_state_root=$tmp/unsafe-state-root
mkdir -p "$unsafe_state_root/etc/ci-fleet/secrets"
chmod 700 "$unsafe_state_root/etc/ci-fleet" "$unsafe_state_root/etc/ci-fleet/secrets"
mkdir "$unsafe_state_root/etc/ci-fleet/bootstrap-app.env"
if CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$unsafe_state_root "$bootstrap" --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'unsafe bootstrap state was accepted'; fi
[[ ! -e $unsafe_state_root/etc/ci-fleet/secrets/github-app.pem ]] || fail 'unsafe bootstrap state was rejected after credential mutation'
dual_record_root=$tmp/dual-record-root
mkdir -p "$dual_record_root/etc/ci-fleet/secrets"
chmod 700 "$dual_record_root/etc/ci-fleet" "$dual_record_root/etc/ci-fleet/secrets"
printf '%s\n' '{"id":1,"client_id":"Iv1.pending","pem":"pending","slug":"pending"}' >"$dual_record_root/etc/ci-fleet/bootstrap-recovery.pending"
printf '%s\n' '{"id":2,"client_id":"Iv1.recovery","pem":"recovery","slug":"recovery"}' >"$dual_record_root/etc/ci-fleet/bootstrap-recovery.json"
chmod 600 "$dual_record_root/etc/ci-fleet/bootstrap-recovery.pending" "$dual_record_root/etc/ci-fleet/bootstrap-recovery.json"
dual_before=$(sha256sum "$dual_record_root/etc/ci-fleet/bootstrap-recovery.pending" "$dual_record_root/etc/ci-fleet/bootstrap-recovery.json")
if CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$dual_record_root "$bootstrap" --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'ambiguous dual recovery records were accepted'; fi
[[ $(sha256sum "$dual_record_root/etc/ci-fleet/bootstrap-recovery.pending" "$dual_record_root/etc/ci-fleet/bootstrap-recovery.json") == "$dual_before" ]] || fail 'ambiguous recovery records were overwritten'

# Mock the GitHub API while exercising the complete local callback, conversion, persistence,
# exact private-repository/group checks, idempotent check, and redaction paths.
fake_bin=$tmp/bin
mkdir "$fake_bin"
cp "$repo_root/scripts/fixtures/fake-bootstrap-curl.py" "$fake_bin/curl"
chmod 0755 "$fake_bin/curl"
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$tmp/fixture.pem" >/dev/null 2>&1
export FAKE_BOOTSTRAP_STATE=$tmp/group-created FAKE_BOOTSTRAP_PEM_FILE=$tmp/fixture.pem FAKE_BOOTSTRAP_LOG=$tmp/api.log
live_root=$tmp/live-root
live_port=$(port)
PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_TEST_FAIL_AFTER_CONVERSION=1 CI_FLEET_ROOT_PREFIX=$live_root FAKE_BOOTSTRAP_CONVERSION_NONZERO=1 "$bootstrap" --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 --port "$live_port" --timeout 30 >"$tmp/fault.out" 2>"$tmp/fault.err" &
live_pid=$!
processes+=("$live_pid")
wait_http "http://127.0.0.1:$live_port/" "$tmp/live.html" "$tmp/fault.out"
state=$(python3 - "$tmp/live.html" <<'PY'
import html,re,sys,urllib.parse
body=html.unescape(open(sys.argv[1]).read())
match=re.search(r"settings/apps/new\?state=([^'&]+)",body)
if not match: raise SystemExit(1)
print(urllib.parse.unquote(match.group(1)))
PY
)
callback_curl -fsS --get --data-urlencode code=fixture-conversion-code --data-urlencode "state=$state" \
  "http://127.0.0.1:$live_port/callback" -o "$tmp/live-callback.html"
if wait "$live_pid"; then fail 'injected post-conversion failure succeeded'; fi
[[ -f $live_root/etc/ci-fleet/bootstrap-recovery.json && ! -e $live_root/etc/ci-fleet/secrets/github-app.pem ]] || fail 'converted credentials were not left recoverable'
cp "$live_root/etc/ci-fleet/bootstrap-recovery.json" "$tmp/matching-recovery.json"
if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_TEST_FAIL_AFTER_PEM_PUBLISH=1 CI_FLEET_ROOT_PREFIX=$live_root "$bootstrap" --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'injected interruption between credential publications succeeded'; fi
cmp -s "$FAKE_BOOTSTRAP_PEM_FILE" "$live_root/etc/ci-fleet/secrets/github-app.pem" || fail 'interrupted publication left a partial PEM'
[[ -f $live_root/etc/ci-fleet/bootstrap-recovery.json && ! -e $live_root/etc/ci-fleet/bootstrap-app.env ]] || fail 'interrupted atomic publication lost recovery state'
PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root FAKE_BOOTSTRAP_TRANSIENT_INSTALLATIONS=1 "$bootstrap" --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 --port "$live_port" --timeout 30 >"$tmp/live.out" 2>"$tmp/live.err"
grep -Fq 'BOOTSTRAP_OK organization=example-org instance=example-ci-01 app=ci-fleet-example-org-example-ci-01 app_id=123 installation_id=456 runner_group=example-ci-experimental runner_group_id=789 repositories=1' "$tmp/live.out" || fail 'live bootstrap did not complete'
host_env=$live_root/etc/ci-fleet/host.env
pem=$live_root/etc/ci-fleet/secrets/github-app.pem
[[ -f $host_env && -f $pem && $(stat -c %a "$host_env") == 600 && $(stat -c %a "$pem") == 600 ]] || fail 'credentials were not stored with mode 0600'
grep -Fxq 'CI_FLEET_GITHUB_APP_CLIENT_ID=Iv1FixtureClient' "$host_env" || fail 'client ID was not recorded'
grep -Fxq 'CI_FLEET_GITHUB_APP_INSTALLATION_ID=456' "$host_env" || fail 'installation ID was not recorded'
[[ -f $live_root/etc/ci-fleet/bootstrap-app.env && ! -e $live_root/etc/ci-fleet/bootstrap-recovery.json ]] || fail 'durable identity or recovery cleanup is incorrect'
if grep -R -F -e fixture-conversion-code -e fixture-client-secret -e fixture-webhook-secret -e fixture-installation-token-value "$tmp/live.out" "$tmp/live.err" "$tmp/api.log"; then
  fail 'bootstrap output exposed sensitive fixture material'
fi
grep -Fxq 'POST runner-group-create' "$tmp/api.log" || fail 'runner group was not created in the mocked live flow'
exec {held_bootstrap_lock}<>"$live_root/etc/ci-fleet/bootstrap.lock"
flock -n "$held_bootstrap_lock"
if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root "$bootstrap" --check --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'concurrent bootstrap transaction was accepted'; fi
flock -u "$held_bootstrap_lock"
exec {held_bootstrap_lock}>&-
create_count=$(grep -c '^POST runner-group-create$' "$tmp/api.log")
rm -f "$FAKE_BOOTSTRAP_STATE"
if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root "$bootstrap" --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=999 >/dev/null 2>&1; then fail 'mismatched project repository name/ID pair was accepted'; fi
[[ $(grep -c '^POST runner-group-create$' "$tmp/api.log") == "$((create_count + 1))" && $(grep -c '^DELETE runner-group-delete$' "$tmp/api.log") == 1 && ! -e $FAKE_BOOTSTRAP_STATE ]] || fail 'invalid repository pair was not rolled back'
cancel_delete_count=$(grep -c '^DELETE runner-group-delete$' "$tmp/api.log")
if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_TEST_CANCEL_AFTER_GROUP_CREATE=1 CI_FLEET_ROOT_PREFIX=$live_root "$bootstrap" --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'injected cancellation after group creation succeeded'; fi
[[ $(grep -c '^DELETE runner-group-delete$' "$tmp/api.log") == "$((cancel_delete_count + 1))" && ! -e $FAKE_BOOTSTRAP_STATE ]] || fail 'cancellation did not roll back the new runner group'
for failure in CREATE_TIMEOUT CREATE_TRUNCATED; do
  if env PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX="$live_root" "FAKE_BOOTSTRAP_$failure=1" "$bootstrap" --organization example-org --instance example-ci-01 \
    --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=999 >"$tmp/uncertain.out" 2>"$tmp/uncertain.err"; then fail 'uncertain group creation succeeded'; fi
  [[ $(<"$FAKE_BOOTSTRAP_STATE") == created ]] || fail 'uncertain creation granted repository access'
  group_pending=$live_root/etc/ci-fleet/bootstrap-group.pending
  [[ -f $group_pending && $(stat -c %a "$group_pending") == 600 ]] || fail 'uncertain creation did not leave protected recovery state'
  grep -Fq 'observed group ID=789' "$tmp/uncertain.err" || fail 'uncertain creation was not reconciled read-only'
  api_before=$(wc -l <"$tmp/api.log")
  if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root "$bootstrap" --organization example-org --instance example-ci-01 \
    --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'uncertain group was silently adopted by rerun'; fi
  [[ $(wc -l <"$tmp/api.log") == "$api_before" ]] || fail 'uncertain rerun reached GitHub before owner reconciliation'
  rm -f "$group_pending" "$FAKE_BOOTSTRAP_STATE"
done
printf 'authorized\n' >"$FAKE_BOOTSTRAP_STATE"
post_rollback_create_count=$(grep -c '^POST runner-group-create$' "$tmp/api.log")

before=$(sha256sum "$host_env" "$pem")
PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root "$bootstrap" --check --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >"$tmp/check.out" 2>"$tmp/check.err"
[[ $(sha256sum "$host_env" "$pem") == "$before" ]] || fail 'check mode changed credentials'
grep -Fq 'CREDENTIALS_VERIFIED' "$tmp/check.out" || fail 'check mode did not verify persisted credentials'
[[ $(grep -c '^POST runner-group-create$' "$tmp/api.log") == "$post_rollback_create_count" ]] || fail 'check mode mutated the runner group'
PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root bash -x "$bootstrap" --check --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >"$tmp/traced.out" 2>"$tmp/traced.err"
if grep -Fq -e fixture-installation-token-value -e eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9 "$tmp/traced.out" "$tmp/traced.err"; then fail 'inherited shell tracing exposed credentials'; fi
PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root FAKE_BOOTSTRAP_SECOND_DISCOVERY_PAGE=1 "$bootstrap" --check --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1 || fail 'target runner group on second discovery page was rejected'
PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root FAKE_BOOTSTRAP_SECOND_GROUP_PAGE=1 "$bootstrap" --check --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 --allow-repository example-org/second-repo=102 >/dev/null 2>&1 || fail 'valid second runner-group repository page was rejected'

for host_present in yes no; do
  mixed_root=$tmp/mixed-$host_present
  cp -a "$live_root" "$mixed_root"
  [[ $host_present == yes ]] || rm "$mixed_root/etc/ci-fleet/host.env"
  recovery=$mixed_root/etc/ci-fleet/bootstrap-recovery.json
  cp "$tmp/matching-recovery.json" "$recovery"
  python3 - "$recovery" <<'PY'
import json,sys
from pathlib import Path
path=Path(sys.argv[1]); value=json.loads(path.read_text()); value['id']=999
path.write_text(json.dumps(value))
PY
  mixed_before=$(find "$mixed_root" -type f -exec sha256sum {} + | sort)
  if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$mixed_root "$bootstrap" --organization example-org --instance example-ci-01 \
    --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'recovery beside a different active App was accepted'; fi
  [[ $(find "$mixed_root" -type f -exec sha256sum {} + | sort) == "$mixed_before" ]] || fail 'conflicting recovery changed active credentials or recovery bytes'
done
cp "$tmp/matching-recovery.json" "$live_root/etc/ci-fleet/bootstrap-recovery.json"
PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root "$bootstrap" --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1 || fail 'matching interrupted recovery was rejected'
[[ ! -e $live_root/etc/ci-fleet/bootstrap-recovery.json ]] || fail 'matching recovery was not finalized'

cp "$host_env" "$tmp/original-host.env"
for ttl in 12h omitted 3599s; do
  sed '/^CI_FLEET_RUNNER_TTL=/d' "$tmp/original-host.env" >"$host_env"
  [[ $ttl == omitted ]] || printf 'CI_FLEET_RUNNER_TTL=%s\n' "$ttl" >>"$host_env"
  ttl_before=$(sha256sum "$host_env")
  if [[ $ttl == 3599s ]]; then
    if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root "$bootstrap" --organization example-org --instance example-ci-01 \
      --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'invalid host-local TTL was accepted'; fi
    [[ $(sha256sum "$host_env") == "$ttl_before" ]] || fail 'invalid TTL rejection changed host configuration'
  else
    PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root "$bootstrap" --check --organization example-org --instance example-ci-01 \
      --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1 || fail 'supported host-local TTL failed check'
    [[ $(sha256sum "$host_env") == "$ttl_before" ]] || fail 'TTL check rewrote host configuration'
    PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root "$bootstrap" --organization example-org --instance example-ci-01 \
      --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >"$tmp/ttl-live.out" 2>"$tmp/ttl-live.err" || fail "supported TTL failed live rerun: $(<"$tmp/ttl-live.err")"
    expected_ttl=$ttl
    [[ $ttl != omitted ]] || expected_ttl=6h
    grep -Fxq "CI_FLEET_RUNNER_TTL=$expected_ttl" "$host_env" || fail 'live rerun changed effective host-local TTL'
  fi
done
cp "$tmp/original-host.env" "$host_env"

if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root "$bootstrap" --check --organization example-org --instance other-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'persisted App was rebound to another instance'; fi
if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root FAKE_BOOTSTRAP_ALL_REPOSITORIES=1 "$bootstrap" --check --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'all-repository installation was accepted'; fi
if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root FAKE_BOOTSTRAP_PUBLIC_GROUP=1 "$bootstrap" --check --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'public-repository runner group was accepted'; fi
if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root FAKE_BOOTSTRAP_RESTRICTED_GROUP=1 "$bootstrap" --check --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'workflow-restricted runner group was accepted'; fi
for inherited in true missing; do
  inherited_before=$(sha256sum "$host_env" "$FAKE_BOOTSTRAP_STATE")
  : >"$FAKE_BOOTSTRAP_LOG"
  if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root FAKE_BOOTSTRAP_INHERITED_GROUP=$inherited "$bootstrap" --organization example-org --instance example-ci-01 \
    --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'enterprise-inherited or unknown-origin runner group was accepted'; fi
  [[ $(sha256sum "$host_env" "$FAKE_BOOTSTRAP_STATE") == "$inherited_before" ]] || fail 'inherited group rejection changed existing state'
  if grep -Eq '^(PUT|DELETE) runner-group|^POST runner-group' "$FAKE_BOOTSTRAP_LOG"; then fail 'inherited group rejection mutated runner-group access'; fi
done
if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root FAKE_BOOTSTRAP_ARCHIVED_REPOSITORY=1 "$bootstrap" --check --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'archived project repository was accepted'; fi
if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root FAKE_BOOTSTRAP_BAD_APP_CLIENT_ID=1 "$bootstrap" --check --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'mismatched App client ID was accepted'; fi
if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root FAKE_BOOTSTRAP_NO_CONTENTS=1 "$bootstrap" --check --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'installation token without contents permission was accepted'; fi
for permission in read none; do
  permission_before=$(sha256sum "$host_env" "$pem")
  if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root FAKE_BOOTSTRAP_RUNNER_PERMISSION=$permission "$bootstrap" --check --organization example-org --instance example-ci-01 \
    --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'effective token without runner-write permission was accepted'; fi
  [[ $(sha256sum "$host_env" "$pem") == "$permission_before" ]] || fail 'permission rejection changed active credentials'
done
for kind in installation-repositories runner-groups runner-group-repositories; do
  if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root FAKE_BOOTSTRAP_TRUNCATE_KIND=$kind "$bootstrap" --check --organization example-org --instance example-ci-01 \
    --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail "truncated $kind page was accepted"; fi
done

if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root FAKE_BOOTSTRAP_APPROVAL_READS=1 "$bootstrap" --check --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'extra approval reads were accepted without the protected gate'; fi
approval_root=$(mktemp -d "$repo_root/scripts/.bootstrap-approval.XXXXXX")
cp -a "$live_root/." "$approval_root/"
python3 - "$repo_root/host/config-review-policy.json.example" "$approval_root/etc/ci-fleet/config-review-policy.json" <<'PY'
import json,sys
from pathlib import Path
value=json.load(open(sys.argv[1])); value['configuration_repository_id']=201
Path(sys.argv[2]).write_text(json.dumps(value))
PY
printf '1\n' >"$approval_root/etc/ci-fleet/config-review-required"
chmod 600 "$approval_root/etc/ci-fleet/config-review-policy.json" "$approval_root/etc/ci-fleet/config-review-required"
PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$approval_root FAKE_BOOTSTRAP_APPROVAL_READS=1 "$bootstrap" --check --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1 || fail 'protected approval-gate reads were rejected'
for mismatch in id branch; do
  gate_before=$(sha256sum "$approval_root/etc/ci-fleet/host.env")
  if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$approval_root FAKE_BOOTSTRAP_APPROVAL_READS=1 FAKE_BOOTSTRAP_POLICY_MISMATCH=$mismatch "$bootstrap" --organization example-org --instance example-ci-01 \
    --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'approval repository identity mismatch was accepted'; fi
  [[ $(sha256sum "$approval_root/etc/ci-fleet/host.env") == "$gate_before" ]] || fail 'approval identity rejection changed host configuration'
done
if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$approval_root FAKE_BOOTSTRAP_APPROVAL_READS=1 FAKE_BOOTSTRAP_APPROVAL_WRITE=1 "$bootstrap" --check --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'approval write permission was accepted'; fi
for permission in actions pull_requests; do
  if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$approval_root FAKE_BOOTSTRAP_APPROVAL_READS=1 FAKE_BOOTSTRAP_NO_APPROVAL_READ=$permission "$bootstrap" --check --organization example-org --instance example-ci-01 \
    --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'effective token without approval reads was accepted'; fi
done

# A schema-valid candidate still needs protected approval before routing is inspected.
approval_install_root=$approval_root/install
mkdir "$approval_install_root"
cp -a "$approval_root/etc" "$approval_install_root/etc"
sed -i 's/example-org/acme-org/g' "$approval_install_root/etc/ci-fleet/bootstrap-app.env"
gate_before=$(sha256sum "$approval_install_root/etc/ci-fleet/host.env" "$FAKE_BOOTSTRAP_STATE")
: >"$FAKE_BOOTSTRAP_LOG"
if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$approval_install_root FAKE_BOOTSTRAP_APPROVAL_READS=1 FAKE_BOOTSTRAP_ORGANIZATION=acme-org "$bootstrap" --organization acme-org --instance example-ci-01 \
  --config-repository acme-org/config --runner-group example-ci-experimental --allow-repository acme-org/example-repo=101 \
  --install --config-repo "$config_checkout" --config-ref "$config_ref" >"$tmp/unapproved.out" 2>"$tmp/unapproved.err"; then fail 'unapproved candidate reached installer handoff'; fi
grep -Fq 'configuration approval rejected before runner-group provisioning' "$tmp/unapproved.err" || fail 'unapproved candidate did not reach the protected verifier'
if grep -q 'runner-group' "$FAKE_BOOTSTRAP_LOG"; then fail 'unapproved candidate reached runner-group discovery or mutation'; fi
[[ $(sha256sum "$approval_install_root/etc/ci-fleet/host.env" "$FAKE_BOOTSTRAP_STATE") == "$gate_before" ]] || fail 'approval rejection changed host or routing state'

# A conversion consumed by GitHub with zero response bytes must block registration retries.
uncertain_root=$approval_root/fresh
mkdir -p "$uncertain_root"
cp -a "$approval_root/etc" "$uncertain_root/etc"
rm "$uncertain_root/etc/ci-fleet/bootstrap-app.env" "$uncertain_root/etc/ci-fleet/host.env" "$uncertain_root/etc/ci-fleet/secrets/github-app.pem"
uncertain_port=$(port)
PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$uncertain_root FAKE_BOOTSTRAP_CONVERSION_NO_RESPONSE=1 "$bootstrap" --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 --port "$uncertain_port" --timeout 30 >"$tmp/zero.out" 2>"$tmp/zero.err" &
uncertain_pid=$!
processes+=("$uncertain_pid")
wait_http "http://127.0.0.1:$uncertain_port/" "$tmp/zero.html" "$tmp/zero.out"
python3 - "$tmp/zero.html" <<'PY'
from html.parser import HTMLParser
import json,sys
class Manifest(HTMLParser):
    def handle_starttag(self,tag,attributes):
        values=dict(attributes)
        if tag=='input' and values.get('name')=='manifest':
            self.permissions=json.loads(values['value'])['default_permissions']
page=Manifest(); page.feed(open(sys.argv[1]).read())
assert page.permissions['actions']=='read' and page.permissions['pull_requests']=='read'
PY
zero_state=$(python3 - "$tmp/zero.html" <<'PY'
import html,re,sys,urllib.parse
print(urllib.parse.unquote(re.search(r"settings/apps/new\?state=([^'&]+)",html.unescape(open(sys.argv[1]).read())).group(1)))
PY
)
callback_curl -fsS --get --data-urlencode code=fixture-zero-response --data-urlencode "state=$zero_state" \
  "http://127.0.0.1:$uncertain_port/callback" -o "$tmp/zero-callback.html"
if wait "$uncertain_pid"; then fail 'zero-response conversion succeeded'; fi
zero_pending=$uncertain_root/etc/ci-fleet/bootstrap-recovery.pending
[[ -f $zero_pending && $(stat -c %a "$zero_pending") == 600 && ! -e $uncertain_root/etc/ci-fleet/secrets/github-app.pem ]] || fail 'zero-response conversion lost its protected uncertainty marker'
api_before=$(wc -l <"$tmp/api.log")
pending_before=$(sha256sum "$zero_pending")
if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$uncertain_root "$bootstrap" --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'uncertain conversion rerun succeeded'; fi
[[ $(wc -l <"$tmp/api.log") == "$api_before" && $(sha256sum "$zero_pending") == "$pending_before" ]] || fail 'uncertain conversion rerun registered another App or discarded recovery bytes'

printf 'BOOTSTRAP_TESTS_OK\n'
