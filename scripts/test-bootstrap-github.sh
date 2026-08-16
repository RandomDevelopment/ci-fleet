#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
bootstrap=$repo_root/scripts/bootstrap-github.sh
callback=$repo_root/scripts/bootstrap-github-callback.py
real_curl=$(command -v curl)
tmp=$(mktemp -d)
processes=()
cleanup() {
  local process
  for process in "${processes[@]}"; do kill "$process" >/dev/null 2>&1 || true; done
  [[ ${KEEP_TEST_TMP:-0} == 1 ]] || rm -rf "$tmp"
}
trap cleanup EXIT
fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'; }
wait_http() {
  local url=$1 output=$2
  for _ in {1..100}; do
    if "$real_curl" -fsS "$url" -o "$output" 2>/dev/null; then return; fi
    sleep 0.05
  done
  fail "callback did not become ready: $url"
}

# The callback rejects mismatched state, never logs, and writes the code once with mode 0600.
callback_port=$(port)
code_file=$tmp/callback-code
handoff_file=$tmp/callback-handoff
python3 "$callback" --parent-pid "$$" --bind 127.0.0.1 --port "$callback_port" --organization example-org --state fixture-state \
  --manifest '{"name":"fixture"}' --output "$code_file" --handoff "$handoff_file" --timeout 30 >"$tmp/callback.out" 2>"$tmp/callback.err" &
processes+=("$!")
wait_http "http://127.0.0.1:$callback_port/" "$tmp/callback.html"
status=$("$real_curl" -sS -o "$tmp/wrong.html" -w '%{http_code}' "http://127.0.0.1:$callback_port/callback?code=fixture-code&state=wrong")
[[ $status == 403 && ! -e $code_file ]] || fail 'callback accepted the wrong state'
"$real_curl" -fsS "http://127.0.0.1:$callback_port/callback?code=fixture-code&state=fixture-state" -o "$tmp/correct.html"
printf 'https://github.com/apps/example-fixture/installations/new\n' >"$handoff_file"
"$real_curl" -fsS "http://127.0.0.1:$callback_port/next" -o "$tmp/next.html"
wait "${processes[-1]}"
[[ $(<"$code_file") == fixture-code && $(stat -c %a "$code_file") == 600 ]] || fail 'callback did not protect the conversion code'
[[ ! -s $tmp/callback.out && ! -s $tmp/callback.err ]] || fail 'callback logged request material'
grep -Fq 'https://github.com/apps/example-fixture/installations/new' "$tmp/next.html" || fail 'callback did not provide the installation approval link'
orphan_port=$(port)
if python3 "$callback" --parent-pid 1 --bind 127.0.0.1 --port "$orphan_port" --organization example-org --state fixture-state --manifest '{"name":"fixture"}' --output "$tmp/orphan-code" --handoff "$tmp/orphan-handoff" --timeout 30 >/dev/null 2>&1; then fail 'callback accepted a mismatched bootstrap parent'; fi
if "$real_curl" -fsS "http://127.0.0.1:$orphan_port/" -o /dev/null 2>/dev/null; then fail 'orphaned callback opened a listener'; fi

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
git init -q "$config_checkout"
git -C "$config_checkout" add -A
git -C "$config_checkout" -c user.name=fixture -c user.email=fixture@example.invalid commit -qm fixture
config_ref=$(git -C "$config_checkout" rev-parse HEAD)
routing_mismatch_root=$tmp/routing-mismatch-root
if CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$routing_mismatch_root "$bootstrap" --organization acme-org --instance example-ci-01 --config-repository acme-org/config --runner-group other-group --allow-repository acme-org/example-repo=101 --install --config-repo "$config_checkout" --config-ref "$config_ref" >/dev/null 2>&1; then fail 'desired-state routing mismatch was accepted'; fi
[[ ! -e $routing_mismatch_root ]] || fail 'desired-state routing mismatch wrote local state before rejection'
routing_root=$tmp/routing-root
routing_port=$(port)
CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$routing_root "$bootstrap" --organization acme-org --instance example-ci-01 --config-repository acme-org/config --runner-group example-ci-experimental --allow-repository acme-org/example-repo=101 --install --config-repo "$config_checkout" --config-ref "$config_ref" --port "$routing_port" --timeout 30 >"$tmp/routing.out" 2>"$tmp/routing.err" &
routing_pid=$!
processes+=("$routing_pid")
wait_http "http://127.0.0.1:$routing_port/" "$tmp/routing.html"
second_routing_port=$(port)
if CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$routing_root "$bootstrap" --organization acme-org --instance example-ci-01 --config-repository acme-org/config --runner-group example-ci-experimental --allow-repository acme-org/example-repo=101 --install --config-repo "$config_checkout" --config-ref "$config_ref" --port "$second_routing_port" --timeout 30 >/dev/null 2>&1; then fail 'concurrent first-time bootstrap was accepted'; fi
if "$real_curl" -fsS "http://127.0.0.1:$second_routing_port/" -o /dev/null 2>/dev/null; then fail 'concurrent bootstrap reached callback registration'; fi
kill -TERM "$routing_pid"
if wait "$routing_pid"; then fail 'cancelled matching desired-state bootstrap succeeded'; fi
for _ in {1..100}; do
  if ! "$real_curl" -fsS "http://127.0.0.1:$routing_port/" -o /dev/null 2>/dev/null; then break; fi
  sleep 0.05
done
if "$real_curl" -fsS "http://127.0.0.1:$routing_port/" -o /dev/null 2>/dev/null; then fail 'callback outlived its bootstrap parent'; fi
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
wait_http "http://127.0.0.1:$live_port/" "$tmp/live.html"
state=$(python3 - "$tmp/live.html" <<'PY'
import html,re,sys,urllib.parse
body=html.unescape(open(sys.argv[1]).read())
match=re.search(r"settings/apps/new\?state=([^'&]+)",body)
if not match: raise SystemExit(1)
print(urllib.parse.unquote(match.group(1)))
PY
)
"$real_curl" -fsS --get --data-urlencode code=fixture-conversion-code --data-urlencode "state=$state" \
  "http://127.0.0.1:$live_port/callback" -o "$tmp/live-callback.html"
if wait "$live_pid"; then fail 'injected post-conversion failure succeeded'; fi
[[ -f $live_root/etc/ci-fleet/bootstrap-recovery.json && ! -e $live_root/etc/ci-fleet/secrets/github-app.pem ]] || fail 'converted credentials were not left recoverable'
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
printf 'created\n' >"$FAKE_BOOTSTRAP_STATE"
post_rollback_create_count=$(grep -c '^POST runner-group-create$' "$tmp/api.log")

before=$(sha256sum "$host_env" "$pem")
PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root "$bootstrap" --check --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >"$tmp/check.out" 2>"$tmp/check.err"
[[ $(sha256sum "$host_env" "$pem") == "$before" ]] || fail 'check mode changed credentials'
grep -Fq 'CREDENTIALS_VERIFIED' "$tmp/check.out" || fail 'check mode did not verify persisted credentials'
[[ $(grep -c '^POST runner-group-create$' "$tmp/api.log") == "$post_rollback_create_count" ]] || fail 'check mode mutated the runner group'
PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root FAKE_BOOTSTRAP_SECOND_GROUP_PAGE=1 "$bootstrap" --check --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 --allow-repository example-org/second-repo=102 >/dev/null 2>&1 || fail 'valid second runner-group repository page was rejected'

if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root "$bootstrap" --check --organization example-org --instance other-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'persisted App was rebound to another instance'; fi
if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root FAKE_BOOTSTRAP_ALL_REPOSITORIES=1 "$bootstrap" --check --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'all-repository installation was accepted'; fi
if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root FAKE_BOOTSTRAP_PUBLIC_GROUP=1 "$bootstrap" --check --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'public-repository runner group was accepted'; fi
if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root FAKE_BOOTSTRAP_RESTRICTED_GROUP=1 "$bootstrap" --check --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'workflow-restricted runner group was accepted'; fi
if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root FAKE_BOOTSTRAP_ARCHIVED_REPOSITORY=1 "$bootstrap" --check --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'archived project repository was accepted'; fi
if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root FAKE_BOOTSTRAP_BAD_APP_CLIENT_ID=1 "$bootstrap" --check --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'mismatched App client ID was accepted'; fi
if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root FAKE_BOOTSTRAP_NO_CONTENTS=1 "$bootstrap" --check --organization example-org --instance example-ci-01 \
  --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail 'installation token without contents permission was accepted'; fi
for kind in installation-repositories runner-groups runner-group-repositories; do
  if PATH="$fake_bin:$PATH" CI_FLEET_TESTING=1 CI_FLEET_ROOT_PREFIX=$live_root FAKE_BOOTSTRAP_TRUNCATE_KIND=$kind "$bootstrap" --check --organization example-org --instance example-ci-01 \
    --config-repository example-org/config --runner-group example-ci-experimental --allow-repository example-org/example-repo=101 >/dev/null 2>&1; then fail "truncated $kind page was accepted"; fi
done

printf 'BOOTSTRAP_TESTS_OK\n'
